"""Auto-routing a **Modal catalog** model: Modal is the oracle (ADR-009 §2).

The YAML says WHICH model; this module decides HOW it ends up served, once, at
deploy time, by ASKING Modal — because Modal has no "can you serve X?" API and
the answer is only in the refusal text of ``modal endpoint create``:

1. ``modal endpoint create --model <repo_id>``. Exit 0 -> a **Dedicated
   endpoint**, zero code of ours.
2. ``… is not available for dedicated Endpoints`` -> read the model's
   fine-tune lineage from the Hub and, for the first ancestor that is IN the
   list Modal just printed, try it again as CUSTOM WEIGHTS.
3. ``… is not a servable checkpoint of base model …``, or no ancestor in that
   list -> deploy our App for the entry's KIND (embedding -> vLLM,
   llm -> SGLang).
4. ANY OTHER failure (auth, quota, network, a text we do not know) -> abort
   with Modal's own message. **Never a silent fallback**: an unknown failure
   is not evidence that a model is ineligible.

Everything soft here degrades to "deploy the App", which is correct and only
costlier: an unparsable model list or an unreachable Hub skips step 2, and the
two verdicts are matched on stable SUBSTRINGS of live texts (2026-09-20) — a
reworded refusal becomes ``other`` and ABORTS rather than routing wrongly.

No ``subprocess`` and no ``modal`` import: every process this module starts
goes through :func:`tree.models.modal_cli.run_modal`, which owns the dry-run
and redaction rails. What is built here are the decisions and the ONE
``Routing …`` line that records each one.
"""

import logging
import os
import re
from pathlib import Path
from typing import Literal

import httpx

from tree.config.app_config import ModalModelConfig, ModelKind
from tree.models.exceptions import ModelError
from tree.models.modal_catalog import (
    HF_TOKEN_HINT,
    app_script,
    hf_token_args,
    looks_gated,
    modal_cli_command,
    redact_argv,
    redact_text,
)
from tree.models.modal_cli import (
    ExistingKind,
    ModalResult,
    assert_owned_name,
    guard_deploy,
    is_dry_run,
    read_existing_kind,
    run_modal,
)

logger = logging.getLogger(__name__)

# What Modal's refusal MEANS. `not_in_catalog` and `not_servable` are the two
# "ineligible" verdicts; `other` is everything else and always aborts.
EndpointVerdict = Literal["not_in_catalog", "not_servable", "other"]

# The ops escape hatch (`SERVING=`): "" is the router, the two others pin one
# Serving path for ONE command. It is never configuration (ADR-009 §2).
ServingOverride = Literal["", "endpoint", "app"]

_OVERRIDES: tuple[str, ...] = ("endpoint", "app")

# The two stable substrings the whole design rests on, captured from the live
# CLI on 2026-09-20 (modal 1.5.5). Both arrive inside a Rich box and may be
# WRAPPED across lines, so the match runs on whitespace/border-normalised text.
_NOT_IN_CATALOG_MARKER = "is not available for dedicated Endpoints"
_NOT_SERVABLE_MARKER = "is not a servable checkpoint of base model"

# The header the bullet list of servable models follows.
_CATALOG_HEADER = "Models available for dedicated Endpoints:"

# Rich box drawing + the bullets Modal's list may use.
_BORDER_CHARS = "│┃|╭╮╰╯─━┌┐└┘┏┓┗┛"
_BORDER_RE = re.compile(f"[{re.escape(_BORDER_CHARS)}]")
_MARKUP_RE = re.compile(r"\[/?[a-zA-Z][^\]]*\]")
_BULLETS = ("-", "*", "•", "–", "—")

# `<org>/<name>`, the shape a Hugging Face repo id takes on those bullet lines.
_REPO_ID_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

_HF_API = "https://huggingface.co/api/models/{repo_id}"

# One sync GET per hop in a sync CLI driver; 10 s is generous for a JSON card
# and short enough that a hanging Hub cannot stall a deploy.
_HF_TIMEOUT_S = 10.0

# At most model -> base -> base's base. Two hops cover every real fine-tune
# chain seen on the Hub and bound the cost of a lineage nobody curated.
_HF_MAX_HOPS = 2

# `base_model` relations that are NOT plain fine-tuned weights, so Modal's
# "same-architecture checkpoint" rule cannot hold for them.
_SKIPPED_RELATIONS = frozenset({"adapter", "merge", "quantized"})

# The App that serves each kind, as the operator reads it in the Routing line.
_APP_LABELS: dict[ModelKind, str] = {"embedding": "vLLM App", "llm": "SGLang App"}

_VERDICT_PHRASES: dict[EndpointVerdict, str] = {
    "not_servable": "not a servable checkpoint",
    "not_in_catalog": "not in the catalog either",
    "other": "refused",
}

# The env var the App deploy scripts resolve their own catalog entry from.
_MODEL_ENV = "MODAL_MODEL"

_DRY_RUN_SKIPPED_CHECK = (
    "DRY RUN — skipped the Modal existence check (no modal process is started)."
)

# A dry run cannot consult the oracle, so it says what each verdict would lead
# to instead of pretending to have routed.
_DRY_RUN_UNDECIDED = (
    'DRY RUN — routing is undecided without Modal: on "not available for '
    'dedicated Endpoints" the next command would be: %s'
)


def classify_endpoint_refusal(output: str) -> EndpointVerdict:
    """What Modal's failed ``endpoint create`` output MEANS.

    Two substrings, not a parser of Modal's prose: the text is boxed and
    wrapped by Rich, so borders and line breaks are normalised away first and
    the match runs on the flattened line. Anything we do not recognise is
    ``other`` — which aborts the deploy with Modal's own message, because an
    unknown failure (auth, quota, network) is not evidence that a model is
    ineligible.
    """

    flattened = _flatten(output)
    if _NOT_IN_CATALOG_MARKER in flattened:
        return "not_in_catalog"
    if _NOT_SERVABLE_MARKER in flattened:
        return "not_servable"
    return "other"


def parse_endpoint_catalog(output: str) -> list[str]:
    """The repo ids Modal printed after ``Models available for dedicated
    Endpoints:`` (44 of them on 2026-09-20).

    BEST-EFFORT by design: a line that does not look like ``<bullet>
    <org>/<name>`` is skipped and an output without the header yields ``[]``.
    An empty list simply skips the custom-weights attempt, which costs one App
    deploy instead of one endpoint — the safe direction. It never raises.
    """

    ids: list[str] = []
    seen_header = False
    for raw in output.splitlines():
        line = _strip_decoration(raw)
        if not seen_header:
            seen_header = _CATALOG_HEADER in line
            continue
        if not line.startswith(_BULLETS):
            continue
        candidate = line[1:].strip()
        if _REPO_ID_RE.match(candidate):
            ids.append(candidate)
    return ids


def hf_base_models(repo_id: str, token: str = "") -> list[str]:
    """The fine-tune ancestors of ``repo_id`` on the Hub, nearest first.

    ``GET /api/models/<repo>`` -> ``cardData.base_model``, which is a STRING
    on some cards and a LIST on others (verified 2026-09-20:
    ``LiquidAI/LFM2.5-350M`` -> ``"LiquidAI/LFM2.5-350M-Base"``,
    ``Qwen/Qwen3.5-0.8B`` -> ``["Qwen/Qwen3.5-0.8B-Base"]``,
    ``voyageai/voyage-4-nano`` -> absent). A card whose
    ``base_model_relation`` is ``adapter`` / ``merge`` / ``quantized`` is
    skipped: those are not the same-architecture checkpoint Modal's custom
    weights require.

    ANY failure — network, 404, 401, bad JSON — degrades to ``[]`` with one
    WARNING, never an exception: the custom-weights attempt is an
    optimisation, and losing it costs an App deploy, not a deploy.

    The token, when set, travels as a Bearer header and is never logged.
    """

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    bases: list[str] = []
    repo = repo_id
    with httpx.Client(timeout=_HF_TIMEOUT_S, headers=headers) as client:
        for _ in range(_HF_MAX_HOPS):
            try:
                response = client.get(_HF_API.format(repo_id=repo))
                response.raise_for_status()
                base = _card_base_model(response.json())
            except Exception as exc:  # noqa: BLE001 — every failure degrades the same way
                logger.warning(
                    "Could not read the Hugging Face lineage of %s (%s) — "
                    "skipping the custom-weights attempt.",
                    repo,
                    _failure_reason(exc),
                )
                return []
            if base is None:
                break
            bases.append(base)
            repo = base
    return bases


def run_deploy(
    entry: ModalModelConfig,
    *,
    serving: str = "",
    force: bool = False,
    dry_run: bool = False,
    token: str = "",
) -> int:
    """Serve ``entry`` on Modal, deciding the **Serving path** as it goes.

    Returns the exit code the driver should exit with: ``0`` (deployed, or a
    dry run) or the ``modal`` command's own code.

    Raises:
        ModelError: the ``SERVING=`` override is unknown, a name lacks the
            ``tree-`` prefix, or the App script for this kind is missing.
        ModalGuardError: the existence guard refused the deploy (exit 3).
    """

    route = _validate_override(serving)

    # BOTH names, because which one this command will act on is not known yet.
    assert_owned_name(entry.endpoint_name, "deploy")
    assert_owned_name(entry.app_name, "deploy")

    if is_dry_run(dry_run):
        return _dry_run_deploy(entry, route)

    kind = read_existing_kind(entry, force)

    if route == "app":
        return _deploy_app(
            entry,
            reason="SERVING=app",
            target=f"{_APP_LABELS[entry.kind]} (no endpoint attempt)",
            force=force,
            kind=kind,
        )
    if kind == "app" and route == "":
        # What is already live stays on its route: `create` cannot update an
        # App, and re-routing is an explicit `-stop` away. NOT under
        # SERVING=endpoint: the operator pinned one path, so the guard refuses
        # rather than quietly deploying the other one.
        return _deploy_app(
            entry,
            reason=f"{entry.app_name!r} is already live as an App",
            target=f"redeploying the {_APP_LABELS[entry.kind]} "
            "(stop it first to re-route)",
            force=force,
            kind=kind,
        )

    if route == "endpoint":
        # Pinned by the operator, so THIS is the decision: it replaces the
        # reason the router would otherwise have derived, and no second
        # `Routing` line follows on either outcome.
        _log_route(
            entry, "SERVING=endpoint", "Dedicated endpoint only (no App fallback)"
        )

    guard_deploy(entry, "endpoint", force, path="endpoint", kind=kind)

    # Step 1: ask the oracle.
    argv = modal_cli_command("deploy", entry, "endpoint")
    result = run_modal(argv, capture_output=True, env=_deploy_env(entry))
    output = _log_modal_output(result, token)

    if result.returncode == 0:
        if not route:
            _log_route(
                entry, "Modal accepted it", f"Dedicated endpoint {entry.endpoint_name}"
            )
        return 0

    verdict = classify_endpoint_refusal(output)
    reason = "Modal refused the weights (not a servable checkpoint)"

    # Step 2: the model itself is unknown to Modal, but a fine-tune ancestor
    # of it may be in the list Modal just printed.
    if verdict == "not_in_catalog":
        reason = "not in Modal's endpoint catalog, no catalog base"
        base = _catalog_base(entry, output, token)
        if base is not None:
            argv = modal_cli_command(
                "deploy", entry, "endpoint", base_model=base
            ) + hf_token_args(base, token)
            result = run_modal(argv, capture_output=True, env=_deploy_env(entry))
            output = _log_modal_output(result, token)

            if result.returncode == 0:
                if not route:
                    _log_route(
                        entry,
                        f"not in Modal's endpoint catalog, fine-tune of {base}",
                        f"Dedicated endpoint {entry.endpoint_name} (custom weights)",
                    )
                return 0

            verdict = classify_endpoint_refusal(output)
            reason = (
                f"not in Modal's endpoint catalog, {base} refused the weights "
                f"({_VERDICT_PHRASES[verdict]})"
            )

    # Step 4 before step 3: an unknown failure is not a verdict, and
    # SERVING=endpoint asked for one path only — so neither falls back.
    if verdict == "other" or route == "endpoint":
        return _abort(entry, argv, result, token, output)

    # Step 3.
    return _deploy_app(
        entry,
        reason=reason,
        target=_APP_LABELS[entry.kind],
        force=force,
        kind=kind,
    )


def run_stop(
    entry: ModalModelConfig,
    *,
    serving: str = "",
    dry_run: bool = False,
    token: str = "",
) -> int:
    """Stop ``entry`` on Modal WITHOUT knowing how it was served.

    The endpoint stop first; if Modal says no, the App stop — so an operator
    never has to remember the route (and nothing records it). No list call:
    two stops of names that carry our prefix are cheaper and safer than a
    lookup (ADR-009 §3).

    Returns the exit code of the command that actually did the stopping.
    """

    route = _validate_override(serving)

    assert_owned_name(entry.endpoint_name, "stop")
    assert_owned_name(entry.app_name, "stop")

    endpoint_argv = modal_cli_command("stop", entry, "endpoint")
    app_argv = modal_cli_command("stop", entry, "app")

    if is_dry_run(dry_run):
        if route != "app":
            run_modal(endpoint_argv, dry_run=True)
        if route != "endpoint":
            run_modal(app_argv, dry_run=True)
        return 0

    if route == "app":
        return _stop_with(app_argv, entry)

    result = run_modal(endpoint_argv, capture_output=True, env=_deploy_env(entry))
    output = _log_modal_output(result, token)
    if result.returncode == 0:
        return 0
    if route == "endpoint":
        return _report_failure(endpoint_argv, result)

    # The endpoint stop failing is not a failure of the COMMAND — it is how a
    # path-blind stop learns which route the model is on, so it gets no error
    # summary, only this line.
    logger.info(
        "Not a Dedicated endpoint (%s) — stopping the App instead.",
        _first_line(output),
    )
    return _stop_with(app_argv, entry)


def hint_if_gated(repo_id: str, token: str, text: str) -> None:
    """The ONE gated-repo hint (ADR-009 §9), for gated-looking failures only.

    It used to fire on every failure with an empty token, and was printed
    under an architecture mismatch, under "is not available for dedicated
    Endpoints" and under a cold-start 503 — none of which a token fixes.
    """

    if not token and looks_gated(text):
        logger.warning(HF_TOKEN_HINT.format(repo_id=repo_id))


def _validate_override(serving: str) -> ServingOverride:
    """``SERVING=`` as the router understands it, or a loud exit.

    Raises:
        ModelError: anything but ``""``, ``endpoint`` or ``app`` — including
            the retired ``sglang`` / ``vllm``, which named an ENGINE, never a
            path.
    """

    if not serving:
        return ""
    if serving not in _OVERRIDES:
        raise ModelError(
            f"Unknown Serving path {serving!r}. Use one of: {', '.join(_OVERRIDES)}."
        )
    return serving  # type: ignore[return-value]


def _dry_run_deploy(entry: ModalModelConfig, route: ServingOverride) -> int:
    """Log the first command and what each verdict WOULD lead to.

    A dry run cannot ask the oracle, so it must not claim a route. It prints
    the command it would run and, when the router is in charge, the command
    the "not in the catalog" verdict would lead to next.
    """

    if route == "app":
        run_modal(modal_cli_command("deploy", entry, "app"), dry_run=True)
    else:
        run_modal(modal_cli_command("deploy", entry, "endpoint"), dry_run=True)
        if route == "":
            logger.info(
                _DRY_RUN_UNDECIDED, " ".join(modal_cli_command("deploy", entry, "app"))
            )
    logger.info(_DRY_RUN_SKIPPED_CHECK)
    return 0


def _catalog_base(entry: ModalModelConfig, output: str, token: str) -> str | None:
    """The first Hub ancestor of ``entry`` that IS in the list Modal printed.

    ``None`` when the list could not be parsed, the Hub could not be read, or
    no ancestor is servable — all three mean the same thing to the caller:
    there is no custom-weights create to try.
    """

    catalog = parse_endpoint_catalog(output)
    if not catalog:
        return None
    return next((b for b in hf_base_models(entry.repo_id, token) if b in catalog), None)


def _deploy_app(
    entry: ModalModelConfig,
    *,
    reason: str,
    target: str,
    force: bool,
    kind: ExistingKind,
) -> int:
    """Step 3: ``modal deploy`` the App script this entry's KIND is served by.

    Raises:
        ModelError: that script does not exist (the SGLang one arrives in
            #147) — said here rather than letting Modal fail on a missing path.
        ModalGuardError: the guard refused the App deploy (exit 3).
    """

    script = app_script(entry.kind)
    if not Path(script).exists():
        raise ModelError(
            f"Deploying {entry.repo_id} needs the App script {script}, which "
            "does not exist."
        )

    guard_deploy(entry, "app", force, path="app", kind=kind)
    _log_route(entry, reason, target)

    # NOT captured: `modal deploy` builds an image and must keep streaming.
    argv = modal_cli_command("deploy", entry, "app")
    result = run_modal(argv, env=_deploy_env(entry))
    return 0 if result is None else _report_failure(argv, result)


def _abort(
    entry: ModalModelConfig,
    argv: list[str],
    result: ModalResult,
    token: str,
    output: str,
) -> int:
    """Step 4: fail with Modal's own exit code, having logged its own text.

    NEVER a fallback: a text we do not recognise may be an expired CLI token
    or a quota, and deploying an App "just in case" would spend a GPU on a
    guess.
    """

    code = _report_failure(argv, result)
    hint_if_gated(entry.repo_id, token, output)
    return code


def _report_failure(argv: list[str], result: ModalResult) -> int:
    """Log the ONE summary line of a failed ``modal`` command; return its code.

    Every non-zero command an operator's terminal ends on gets it — including
    a streamed ``modal deploy``, whose own output scrolls past — because the
    line is the redacted, re-runnable record of what was attempted.

    A plain join, not ``shlex.join``: every element is catalog-derived (a repo
    id, a git sha, a derived name, the region, a script path) and carries no
    whitespace, while ``shlex.join`` would quote the redaction into ``'***'``
    — hiding the marker an operator (and the leak tests) look for.
    """

    if result.returncode != 0:
        logger.error(
            "modal command failed (exit %d): %s",
            result.returncode,
            " ".join(redact_argv(argv)),
        )
    return result.returncode


def _stop_with(argv: list[str], entry: ModalModelConfig) -> int:
    """Run one stop command and report its failure, if any."""

    result = run_modal(argv, env=_deploy_env(entry))
    return 0 if result is None else _report_failure(argv, result)


def _log_route(entry: ModalModelConfig, reason: str, target: str) -> None:
    """The ONE line that records the decision and why (ADR-009 §2).

    It is where the **Serving path** lives now — there is no field to read it
    off, so this line (and ``modal endpoint list`` / ``modal app list``) is
    the record.
    """

    logger.info("Routing %s: %s → %s", entry.repo_id, reason, target)


def _log_modal_output(result: ModalResult, token: str) -> str:
    """Log captured ``modal`` output line by line, redacted; return it.

    Captured output is output the operator would otherwise have seen on their
    terminal (the endpoint URL on success, the verdict on failure), so it is
    logged either way — and only ever after ``redact_text``.
    """

    text = redact_text(f"{result.stdout or ''}{result.stderr or ''}", token)
    for line in text.splitlines():
        if line.strip():
            logger.info("modal: %s", line)
    return text


def _deploy_env(entry: ModalModelConfig) -> dict[str, str]:
    """The environment a ``modal`` command runs with.

    ``MODAL_MODEL`` is the ONE thing that crosses from the driver into a
    deploy script: the script re-resolves the entry from the catalog with it
    (the `tree` package is importable there, locally, but the model id is not).
    """

    return {**os.environ, _MODEL_ENV: entry.repo_id}


def _card_base_model(card: object) -> str | None:
    """The ONE base model a Hub card names, or ``None``."""

    if not isinstance(card, dict):
        return None
    data = card.get("cardData")
    if not isinstance(data, dict):
        return None
    if str(data.get("base_model_relation") or "").lower() in _SKIPPED_RELATIONS:
        return None

    base = data.get("base_model")
    if isinstance(base, list):
        base = next((item for item in base if isinstance(item, str)), None)
    if isinstance(base, str) and _REPO_ID_RE.match(base.strip()):
        return base.strip()
    return None


def _failure_reason(exc: Exception) -> str:
    """A short, credential-free reason for the lineage WARNING."""

    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _flatten(text: str) -> str:
    """``text`` with Rich borders dropped and every line joined by ONE space.

    Modal's refusals arrive inside a box and are wrapped, so a substring of
    the sentence can be split by a line break plus two box borders. Flattening
    first is what makes the match independent of the terminal width it was
    rendered at.
    """

    return " ".join(_BORDER_RE.sub(" ", _MARKUP_RE.sub(" ", text)).split())


def _strip_decoration(line: str) -> str:
    """One line without box borders, Rich markup or padding."""

    return _BORDER_RE.sub(" ", _MARKUP_RE.sub(" ", line)).strip()


def _first_line(text: str) -> str:
    """The first line with content, for a one-line INFO about a failure."""

    for line in text.splitlines():
        stripped = _strip_decoration(line)
        if stripped:
            return stripped
    return "no output"
