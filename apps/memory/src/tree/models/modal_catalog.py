"""Pure read helpers over the **Embedding catalog** (ADR-009 §2/§3).

The ONE place that turns ``modal.embedding_models`` in
``configs/default.yaml`` into the names, flags and prompts the deploy driver
(#139), the fallback scripts (#142) and the ``ModalEmbeddingModel`` client
(#140) all need — so a model's app name, dimensions and prompts cannot drift
between the thing that deploys it and the thing that calls it.

This module imports **no** ``modal``: it is configuration, not infrastructure.
The MCP boot path and the unit suite therefore never pay for the SDK, and the
module stays importable without the ``local-models`` extra
(``tests/unit/models/test_modal_catalog.py::test_catalog_does_not_import_modal``).
"""

import math
from typing import Literal

from pydantic import BaseModel, Field

from tree.config.app_config import (
    ModalEmbeddingModelConfig,
    ServingPath,
    app_config,
)
from tree.config.settings import settings
from tree.models.base import EmbeddingRole
from tree.models.exceptions import ModelError

# The server class name on ALL three Serving paths: it is the class name in the
# ``serve.py`` Modal generates for a Dedicated endpoint, and the fallback
# scripts reuse it — so ONE `Server.from_name(app_name, "Server")` lookup
# resolves every model regardless of how it is served.
EMBEDDING_SERVER_NAME = "Server"

# The env var the fallback scripts bake ``EmbeddingDeploySpec.model_dump_json()``
# into: the catalog logic lives in ``src/tree/``, but ``tree`` is not installed
# inside the Modal container, so the resolved spec crosses as ONE JSON string.
DEPLOY_SPEC_ENV = "EMBEDDING_DEPLOY_SPEC"

# Engines with a fallback deploy script. `endpoint` has none by design (Modal
# picks the engine), which is why the builders take an engine, not a path.
FallbackEngine = Literal["sglang", "vllm"]

_SERVING_PATHS: tuple[ServingPath, ...] = ("endpoint", "sglang", "vllm")

# Where a Dedicated endpoint's requests ENTER Modal (`--routing-region`;
# Modal's own default is `us-west`). One region for the whole catalog: the
# memory's callers are European, and compute placement stays Modal's choice —
# `--compute-region` costs a region-selection price multiplier.
MODAL_ROUTING_REGION = "eu-west"

# The CLI flag that carries the Hugging Face token. Verified on the PINNED
# client (modal 1.5.5, `modal endpoint create --help`, 2026-09-19): the flag
# takes a TEXT value and lists NO `[env var: ...]` form, so the token travels
# as argv and every logged copy must pass through `redact_argv` (ADR-009 §9).
_HF_TOKEN_FLAG = "--custom-hf-token"

# What the driver says when a deploy or a smoke test fails with NO token set.
# The gated-repo download fails inside the engine at container start, so this
# is a hint, not a diagnosis — there is no pre-flight Hub call (ADR-009 §9).
HF_TOKEN_HINT = (
    "If {repo_id} is a private or gated Hugging Face repo, set HF_TOKEN in "
    ".env (read-scope token: https://huggingface.co/settings/tokens) and "
    "deploy again."
)

# The strings the Hub and `huggingface_hub` use for a repo you may not read,
# lower-cased (the match is case-insensitive). `gatedrepoerror` is listed even
# though `gated` subsumes it: the list is read as documentation of what we
# recognise, and one redundant element is cheaper than that ambiguity.
_GATED_MARKERS = (
    "401",
    "403",
    "gated",
    "gatedrepoerror",
    "repositorynotfounderror",
    "access to model",
)

# The fallback deploy script per engine, relative to `apps/memory` (the cwd of
# every `make memory-*` target). They arrive in #142; until then an
# `sglang`/`vllm` deploy exits 2 on the missing file.
_FALLBACK_SCRIPTS: dict[FallbackEngine, str] = {
    "sglang": "deploy/modal_sglang_embedding.py",
    "vllm": "deploy/modal_vllm_embedding.py",
}


class EmbeddingDeploySpec(BaseModel):
    """Everything ONE fallback deploy needs, resolved locally (ADR-009 §3).

    Configuration only — a credential never enters it (the Hugging Face token
    travels as an ephemeral ``modal.Secret``, ADR-009 §9), because the spec is
    baked into a cached, inspectable image layer.
    """

    repo_id: str = Field(description="Hugging Face repo id of the weights to serve.")
    revision: str = Field(description="Commit sha or branch of those weights.")
    engine: FallbackEngine = Field(description="The engine the calling SCRIPT runs.")
    app_name: str = Field(description="Modal app name — the same on every path.")
    server_name: str = Field(description="Server class name inside that app.")
    gpu: str = Field(description="Modal GPU string, e.g. A10.")
    cpu: float
    memory_mb: int
    native_dimensions: int = Field(
        description="Vector width the server returns untruncated."
    )
    engine_version: str = Field(description="Pinned version of `engine`.")
    autoinference_utils_version: str
    server_args: dict[str, str] = Field(
        description="Full engine argv: the catalog's extras + the builder-owned keys."
    )


def get_catalog_entry(model: str) -> ModalEmbeddingModelConfig:
    """Return the catalog entry whose ``repo_id`` is exactly ``model``.

    Matching is exact, never fuzzy: a near-miss id must fail loudly rather
    than silently deploy or query the wrong weights.

    Raises:
        ModelError: ``model`` is not in the catalog. The message lists every
            id that IS, sorted, and names the file to edit.
    """

    for entry in app_config.modal.embedding_models:
        if entry.repo_id == model:
            return entry

    known = ", ".join(sorted(e.repo_id for e in app_config.modal.embedding_models))
    raise ModelError(
        f"Unknown Modal embedding model {model!r}. "
        f"Embedding catalog ids: {known}. "
        "Add an entry under modal.embedding_models in configs/default.yaml."
    )


def resolve_serving(
    entry: ModalEmbeddingModelConfig, override: str | None
) -> ServingPath:
    """Resolve the **Serving path** for ONE command.

    ``override`` is the operator's ``SERVING=<path>`` (``None`` or ``""`` when
    unset), which walks the ladder for a single command without editing YAML;
    the catalog stays the value the client reads.

    Raises:
        ModelError: the override is not one of the three paths, or the
            resolved path is ``endpoint`` on an entry with no ``base_model``
            (a Dedicated endpoint needs one to pass as ``--model``).
    """

    serving = override or entry.serving
    if serving not in _SERVING_PATHS:
        raise ModelError(
            f"Unknown Serving path {serving!r}. "
            f"Use one of: {', '.join(_SERVING_PATHS)}."
        )

    if serving == "endpoint" and not entry.base_model:
        raise ModelError(
            f"{entry.repo_id} has no base_model — a Dedicated endpoint needs "
            "one. Add base_model to its Embedding catalog entry."
        )
    return serving


def build_server_args(
    entry: ModalEmbeddingModelConfig, engine: FallbackEngine
) -> dict[str, str]:
    """The full engine argv for ``entry`` under ``engine``.

    The catalog's ``extra_server_args`` plus the keys the builder owns, so one
    entry runs under EITHER fallback script: the embedding-mode flag
    (``--runner pooling`` for vLLM, ``--is-embedding`` for SGLang), the pinned
    revision, the served model id, and the context window only when the entry
    sets one (otherwise the engine's own default wins).

    ``engine`` comes from the calling SCRIPT, not from ``entry.serving``, so
    ``SERVING=`` works.
    """

    args = dict(entry.extra_server_args)
    args["--revision"] = entry.revision
    args["--served-model-name"] = entry.repo_id

    if engine == "vllm":
        args["--runner"] = "pooling"
        if entry.max_model_len is not None:
            args["--max-model-len"] = str(entry.max_model_len)
    else:
        args["--is-embedding"] = ""
        if entry.max_model_len is not None:
            args["--context-length"] = str(entry.max_model_len)
    return args


def build_deploy_spec(model: str, engine: FallbackEngine) -> EmbeddingDeploySpec:
    """Resolve ``model`` into the spec one fallback deploy is driven by.

    Raises:
        ModelError: ``model`` is not in the catalog, or ``engine`` has no
            pinned version under ``modal.engines``.
    """

    entry = get_catalog_entry(model)
    engine_config = app_config.modal.engines.get(engine)
    if engine_config is None:
        known = ", ".join(sorted(app_config.modal.engines))
        raise ModelError(
            f"No pinned version for engine {engine!r}. "
            f"Engines in modal.engines: {known}."
        )

    return EmbeddingDeploySpec(
        repo_id=entry.repo_id,
        revision=entry.revision,
        engine=engine,
        app_name=entry.app_name,
        server_name=EMBEDDING_SERVER_NAME,
        gpu=entry.gpu,
        cpu=entry.cpu,
        memory_mb=entry.memory_mb,
        native_dimensions=entry.native_dimensions,
        engine_version=engine_config.version,
        autoinference_utils_version=app_config.modal.autoinference_utils_version,
        server_args=build_server_args(entry, engine),
    )


def prompt_for(
    entry: ModalEmbeddingModelConfig, input_type: EmbeddingRole | None
) -> str:
    """The prefix an **Embedding role** prepends for this model.

    OpenAI-compatible ``/v1/embeddings`` has no role field on any Serving
    path, so the role becomes a client-side prefix (ADR-009 §5). A model that
    defines no prompt for a role — or a role-less (``None``) call — gets
    ``""``: a role is a hint, never a correctness input.
    """

    if input_type == "query":
        return entry.query_prompt
    if input_type == "document":
        return entry.document_prompt
    return ""


def modal_proxy_bearer() -> str:
    """The ``Authorization: Bearer`` value for every catalog app.

    Modal joins a **Proxy token**'s two halves with a ``.`` — the same scheme
    OpenAI clients use, so the value doubles as the ``api_key`` of an
    OpenAI-compatible client (ADR-009 §4).

    Raises:
        ModelError: either half is empty. A half token is no token: Modal's
            edge would answer 401 before any container wakes.
    """

    token_id = settings.modal_proxy_token_id.get_secret_value()
    token_secret = settings.modal_proxy_token_secret.get_secret_value()
    if not token_id or not token_secret:
        raise ModelError(
            "Modal proxy token is required. Set MODAL_PROXY_TOKEN_ID and "
            "MODAL_PROXY_TOKEN_SECRET."
        )
    return f"{token_id}.{token_secret}"


def modal_cli_command(
    action: Literal["deploy", "stop"],
    entry: ModalEmbeddingModelConfig,
    serving: ServingPath,
) -> list[str]:
    """The ``modal`` argv that deploys or stops ``entry`` on ``serving``.

    Every name in it comes from the catalog's ONE derivation, so it always
    carries the ``tree-`` namespace (``tree-<slug>`` for an endpoint,
    ``ep-tree-<slug>`` for an app). The driver checks that again on the name it
    is about to pass to ``modal`` (``modal_cli.assert_owned_name``): this
    function builds the command, it does not authorise it.

    ALWAYS token-free, so the driver can log it verbatim and a test can assert
    on it (ADR-009 §9). The Hugging Face token is appended separately by
    :func:`hf_token_args`, at the ``subprocess.run`` boundary.

    ``--unauthenticated`` is never passed: **Proxy token** auth is the only
    auth (ADR-009 §4), and Modal requires it by default.

    Verified on modal 1.5.5 (2026-09-19): ``modal endpoint create --name
    --model --routing-region --custom-hf-repo --custom-hf-revision``;
    ``modal endpoint stop [-y] ENDPOINT_IDENTIFIER`` and ``modal app stop
    [-y] APP_IDENTIFIER`` both resolve a NAME and both prompt without ``-y``.
    """

    if serving == "endpoint":
        if action == "stop":
            return ["modal", "endpoint", "stop", "-y", entry.endpoint_name]

        argv = [
            "modal",
            "endpoint",
            "create",
            "--name",
            entry.endpoint_name,
            "--model",
            entry.base_model,
        ]
        # Custom weights: Modal serves `repo_id` on `base_model`'s recipe.
        if entry.repo_id != entry.base_model:
            argv += [
                "--custom-hf-repo",
                entry.repo_id,
                "--custom-hf-revision",
                entry.revision,
            ]
        return argv + ["--routing-region", MODAL_ROUTING_REGION]

    if action == "stop":
        return ["modal", "app", "stop", "-y", entry.app_name]
    return ["modal", "deploy", _FALLBACK_SCRIPTS[serving]]


def fallback_script(serving: ServingPath) -> str | None:
    """The deploy script ``serving`` needs, or ``None`` for ``endpoint``.

    A Dedicated endpoint has no script of ours — which is exactly why the
    driver must not look for one before running the command.
    """

    return _FALLBACK_SCRIPTS.get(serving)


def hf_token_args(
    entry: ModalEmbeddingModelConfig, serving: ServingPath, token: str
) -> list[str]:
    """The ``--custom-hf-token`` pair, or ``[]`` (ADR-009 §9).

    Appended ONLY for custom weights on a Dedicated endpoint: Modal documents
    the flag as the token "for private --custom-hf-repo" (re-verified in
    ``modal endpoint create --help`` on modal 1.5.5). The fallback scripts get
    the token as an ephemeral ``modal.Secret`` instead (#142), and an empty
    token changes no argv at all.
    """

    if not token or serving != "endpoint" or entry.repo_id == entry.base_model:
        return []
    return [_HF_TOKEN_FLAG, token]


def hf_token_env() -> dict[str, str]:
    """The env dict a fallback script's ephemeral ``modal.Secret`` carries.

    ``{"HF_TOKEN": <token>}`` when the setting is non-empty, ``{}`` otherwise
    (ADR-009 §9) — so ``modal.Secret.from_dict(hf_token_env())`` is built the
    same way with or without a token and the ``secrets=`` list always has
    exactly one element. ``HF_TOKEN`` is the variable ``huggingface_hub``,
    vLLM and SGLang read natively, and the engine subprocess inherits the
    container env (``autoinference-utils`` 0.2.6 calls ``subprocess.Popen(cmd)``
    with no ``env=``), so nothing forwards it by hand.

    A credential, so it never joins ``EmbeddingDeploySpec``: the spec is baked
    into a cached, inspectable image layer and this is not.
    """

    token = settings.hf_token.get_secret_value()
    return {"HF_TOKEN": token} if token else {}


def redact_argv(argv: list[str]) -> list[str]:
    """A COPY of ``argv`` with every token value replaced by ``***``.

    The single door every logged argv passes through. It copies rather than
    redacts in place because the caller runs the original argv right after
    logging this one.
    """

    redacted = list(argv)
    # Scan the ORIGINAL, write into the copy: scanning the copy would read a
    # `***` it just wrote and let a second token slip through.
    for index, item in enumerate(argv[:-1]):
        if item == _HF_TOKEN_FLAG:
            redacted[index + 1] = "***"
    return redacted


def redact_text(text: str, token: str) -> str:
    """``text`` with every occurrence of ``token`` replaced by ``***``.

    Belt and braces for the captured output of ``modal endpoint create``: Modal
    is not known to echo the argv it was given, but the output is logged line
    by line and a token is forever. An EMPTY token is a no-op — ``"".replace``
    would otherwise splice ``***`` between every character.
    """

    return text.replace(token, "***") if token else text


def looks_gated(text: str) -> bool:
    """Does ``text`` look like "you may not read this Hugging Face repo"?

    The gate on the one ``HF_TOKEN`` hint (ADR-009 §9). Before this, the hint
    was printed under EVERY failure with an empty token — under an
    architecture mismatch, under "is not available for dedicated Endpoints"
    and under a cold-start 503, none of which a token fixes.

    Substrings, not a parser: the text is whatever Modal's CLI, the Hub or
    ``huggingface_hub`` wrote, and the cost of a false positive is one extra
    line of advice.
    """

    lowered = text.lower()
    return any(marker in lowered for marker in _GATED_MARKERS)


def truncate_embedding(vector: list[float], dimensions: int) -> list[float]:
    """The first ``dimensions`` components of ``vector``, L2-renormalised.

    Matryoshka truncation done CLIENT-side (ADR-009 §3), because no request of
    ours ever carries an OpenAI ``dimensions`` parameter: vLLM answers 400 to
    it unless the model's HF config is flagged Matryoshka (voyage-4-nano's is
    not) and a managed recipe exposes no flag to change that. Slice-then-
    normalise is what a server-side truncation does, and it behaves
    identically on all three Serving paths.

    A zero vector is returned sliced (no division by zero) — a degenerate
    vector is a server problem, caught by the length and sanity assertions
    around this call, not here.

    Raises:
        ModelError: ``dimensions`` is not positive, or is wider than
            ``vector`` — truncation cannot invent components. The lower bound
            is explicit because the widths come from a CALLER (the client's
            effective ``dimensions``), and Python's negative-slice semantics
            would otherwise turn ``-64`` into a silent success.
    """

    if dimensions <= 0:
        raise ModelError(f"cannot truncate to {dimensions}-d: a width must be positive")
    if dimensions > len(vector):
        raise ModelError(f"cannot truncate a {len(vector)}-d vector to {dimensions}-d")

    head = vector[:dimensions]
    norm = math.sqrt(sum(value * value for value in head))
    if norm == 0.0:
        return head
    return [value / norm for value in head]
