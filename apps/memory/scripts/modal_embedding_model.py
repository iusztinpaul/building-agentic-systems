"""Deploy, smoke-test and stop ONE **Embedding catalog** model on Modal.

Make cannot read YAML, so this driver turns ``MODEL=<repo_id>`` into the
``modal`` command the entry's **Serving path** needs (ADR-009 §2/§3):

* ``endpoint`` — ``modal endpoint create``: a **Dedicated endpoint**, where
  Modal picks the recipe, GPU, engine and flags. No code of ours runs.
* ``sglang`` / ``vllm`` — ``modal deploy deploy/modal_*_embedding.py``: our
  fallback scripts, for what a managed recipe cannot express (#142).

``SERVING=<path>`` walks that ladder for ONE command without editing the YAML
the ``ModalEmbeddingModel`` client reads.

Glue only: every rail lives in ``tree.models.modal_cli`` — the ``tree-``
ownership check, the existence guard that looks before a deploy writes, and the
dry run that starts no ``modal`` process at all. This file decides only which
of them applies to which command, and which exit code each failure gets:

* ``0`` ok (or a dry run), ``1`` the smoke test failed, ``2`` usage or
  configuration (unknown ``MODEL``, bad ``SERVING``, missing script, ``modal``
  not installed, a name without the ``tree-`` prefix), ``3`` a guard refusal,
  otherwise ``modal``'s own exit code.

The optional ``HF_TOKEN`` (private or gated Hugging Face repos, ADR-009 §9)
joins the argv HERE and nowhere else, and every argv that is logged passes
through ``redact_argv`` first. ``subprocess.run`` is always called with
``check=False``: its raising form embeds the full argv — the token — in the
``CalledProcessError`` message, so the first failed create would print it.

Usage:
    make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B
    make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano SERVING=vllm
    make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes
    make memory-deploy-embedding-model-test MODEL=Qwen/Qwen3-Embedding-0.6B
    make memory-deploy-embedding-model-stop MODEL=Qwen/Qwen3-Embedding-0.6B
"""

import asyncio
import logging
import os
from pathlib import Path
from subprocess import CompletedProcess

import click

from tree.config.app_config import ModalEmbeddingModelConfig, ServingPath
from tree.config.settings import settings
from tree.logging import init_logger
from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_catalog import (
    HF_TOKEN_HINT,
    fallback_script,
    get_catalog_entry,
    hf_token_args,
    looks_gated,
    modal_cli_command,
    redact_argv,
    redact_text,
    resolve_serving,
)
from tree.models.modal_cli import (
    DeployTarget,
    ModalAction,
    ModalGuardError,
    assert_owned_name,
    guard_deploy,
    is_dry_run,
    run_modal,
)
from tree.models.modal_server import smoke_test

init_logger()
logger = logging.getLogger(__name__)

_DEDICATED_ENDPOINT_NOTE = (
    "Dedicated endpoint: Modal picks the GPU, engine and flags — this entry's "
    "gpu/cpu/memory_mb/max_model_len/extra_server_args are used only by "
    "SERVING=sglang|vllm."
)

_DRY_RUN_SKIPPED_CHECK = (
    "DRY RUN — skipped the Modal existence check (no modal process is started)."
)

model_option = click.option(
    "--model",
    required=True,
    help="Embedding catalog repo_id, e.g. Qwen/Qwen3-Embedding-0.6B.",
)
serving_option = click.option(
    "--serving",
    default="",
    help="Override the entry's Serving path for THIS command: endpoint|sglang|vllm.",
)
dry_run_option = click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Log the modal command and exit 0 — no modal process is started.",
)


def _resolve(model: str, serving: str) -> tuple[ModalEmbeddingModelConfig, ServingPath]:
    """The catalog entry and the path this command runs on.

    A mistyped ``MODEL`` or ``SERVING`` exits 2 BEFORE anything is spent: the
    catalog's own message already lists every id / path it could have meant.
    """

    entry = get_catalog_entry(model)
    path = resolve_serving(entry, serving)

    if serving and path != entry.serving:
        logger.info(
            "SERVING=%s overrides the catalog's serving: %s for THIS command "
            "only — the ModalEmbeddingModel client keeps reading "
            "configs/default.yaml.",
            path,
            entry.serving,
        )
    return entry, path


def _run_modal(
    action: ModalAction,
    model: str,
    serving: str,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    """Run ``action``, turning every rail's refusal into its exit code."""

    try:
        _drive(action, model, serving, force=force, dry_run=dry_run)
    except ModalGuardError as exc:
        logger.error("%s", exc)
        raise SystemExit(3) from exc
    except ModelError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc


def _drive(
    action: ModalAction, model: str, serving: str, *, force: bool, dry_run: bool
) -> None:
    """Build the argv, walk the rails in order, run it."""

    entry, path = _resolve(model, serving)
    argv = modal_cli_command(action, entry, path)

    # Keyed on what the command CREATES, not on the path name, so #145 reuses
    # the guard when `serving` disappears.
    target: DeployTarget = "endpoint" if path == "endpoint" else "app"
    # The name `modal` will act on. On a fallback deploy the argv names a
    # SCRIPT, and the app it creates is `app_name` — so the ownership check
    # reads the name from the entry, not from the argv.
    assert_owned_name(
        entry.endpoint_name if target == "endpoint" else entry.app_name, action
    )

    script = fallback_script(path) if action == "deploy" else None
    if script is not None and not Path(script).exists():
        raise ModelError(f"Serving path '{path}' needs {script}, which does not exist.")

    token = settings.hf_token.get_secret_value()
    run_argv = argv
    if action == "deploy":
        run_argv = argv + hf_token_args(entry, path, token)
        if path == "endpoint":
            logger.info(_DEDICATED_ENDPOINT_NOTE)

    if action == "deploy" and not is_dry_run(dry_run):
        guard_deploy(entry, target, force, path=path)

    # `modal deploy` builds an image and must keep streaming to the terminal;
    # only the create is captured, so its verdict can be read (ADR-009 §9).
    capture = action == "deploy" and path == "endpoint"
    result = run_modal(
        run_argv,
        dry_run=dry_run,
        capture_output=capture,
        env={**os.environ, "EMBEDDING_MODEL": entry.repo_id},
    )
    if result is None:
        logger.info(_DRY_RUN_SKIPPED_CHECK)
        return

    output = _log_modal_output(result, token) if capture else ""

    if result.returncode != 0:
        # A plain join, not `shlex.join`: every element is catalog-derived (a
        # repo id, a git sha, a derived name, the region, a script path) and
        # carries no whitespace, while `shlex.join` would quote the redaction
        # into `'***'` — hiding the marker an operator (and the leak tests)
        # look for.
        logger.error(
            "modal command failed (exit %d): %s",
            result.returncode,
            " ".join(redact_argv(run_argv)),
        )
        _hint_if_gated(entry.repo_id, token, output)
        raise SystemExit(result.returncode)


def _log_modal_output(result: CompletedProcess[str], token: str) -> str:
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


def _hint_if_gated(repo_id: str, token: str, text: str) -> None:
    """The ONE gated-repo hint (ADR-009 §9), for gated-looking failures only.

    It used to fire on every failure with an empty token, and was printed
    under an architecture mismatch, under "is not available for dedicated
    Endpoints" and under a cold-start 503 — none of which a token fixes.
    """

    if not token and looks_gated(text):
        logger.warning(HF_TOKEN_HINT.format(repo_id=repo_id))


@click.group()
def main() -> None:
    """Serve one Embedding catalog model on Modal."""


@main.command()
@model_option
@serving_option
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Deploy even when the name is already live on Modal (the tree- "
    "ownership check is never overridden).",
)
@dry_run_option
def deploy(model: str, serving: str, force: bool, dry_run: bool) -> None:
    """Serve MODEL on Modal through its Serving path."""

    _run_modal("deploy", model, serving, force=force, dry_run=dry_run)


@main.command()
@model_option
@serving_option
@dry_run_option
def stop(model: str, serving: str, dry_run: bool) -> None:
    """Stop MODEL on Modal (pass the same SERVING you deployed with).

    No ``--force``: a stop runs no existence check, and the ownership check it
    does run is not overridable.
    """

    _run_modal("stop", model, serving, dry_run=dry_run)


@main.command("test")
@model_option
def test_command(model: str) -> None:
    """Smoke-test the served MODEL through proxy-token auth.

    No ``--serving``: under H1 the lookup is identical on every path. No dry
    run either — it starts no CLI.
    """

    try:
        asyncio.run(smoke_test(model))
    except ExtractionError as exc:
        # Server-side: the model answered, or failed to. Only these can be a
        # gated download — a bare `ModelError` is OUR configuration (a wrong
        # Proxy token, whose message carries a 401 that would otherwise read
        # as gated).
        logger.error("%s", exc)
        _hint_if_gated(model, settings.hf_token.get_secret_value(), str(exc))
        raise SystemExit(1) from exc
    except ModelError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
