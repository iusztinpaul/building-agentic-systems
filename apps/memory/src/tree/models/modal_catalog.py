"""Pure read helpers over the **Modal catalog** (ADR-009 §2/§3).

The ONE place that turns ``modal.embedding_models`` / ``modal.llm_models`` in
``configs/default.yaml`` into the names, flags and prompts the deploy driver,
the App scripts and the clients all need — so a model's app name, dimensions
and prompts cannot drift between the thing that deploys it and the thing that
calls it.

The catalog says WHICH model, never HOW it is served: the **Serving path** is
the router's runtime decision (:mod:`tree.models.modal_router`), so nothing
here reads a ``serving`` or a ``base_model`` field — they no longer exist. What
an entry DOES fix is its ``kind``, and the kind picks the App script Modal's
refusal falls back to.

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
    ModalLLMModelConfig,
    ModalModelConfig,
    ModelKind,
    app_config,
)
from tree.config.settings import settings
from tree.models.base import EmbeddingRole
from tree.models.exceptions import ModelError

# The server class name on BOTH Serving paths: it is the class name in the
# ``serve.py`` Modal generates for a Dedicated endpoint, and our App scripts
# reuse it — so ONE `Server.from_name(app_name, "Server")` lookup resolves
# every model regardless of how it is served.
MODAL_SERVER_NAME = "Server"

# The env vars the App scripts bake their resolved spec's ``model_dump_json()``
# into: the catalog logic lives in ``src/tree/``, but ``tree`` is not installed
# inside the Modal container, so the resolved spec crosses as ONE JSON string.
# One per KIND, because the two specs carry different fields — a script reading
# the wrong one would fail on a missing key instead of on a wrong model.
EMBEDDING_DEPLOY_SPEC_ENV = "EMBEDDING_DEPLOY_SPEC"
LLM_DEPLOY_SPEC_ENV = "LLM_DEPLOY_SPEC"

# The tensor-parallel defaults of the SGLang App, applied to every LLM unless
# the entry overrides them (they are NOT builder-owned keys, ADR-009 §2):
# `--trust-remote-code` because Modal sets it on both its LLM recipes and every
# new architecture needs it, `--mem-fraction-static` because SGLang's own
# default leaves too little room for the CUDA graphs on a small GPU. Modal's
# recipes lower it to 0.75 for a 120B and a 35B-MoE model; 0.85 is the generic
# value, and an entry that needs Modal's is one YAML line away.
_LLM_TUNING_DEFAULTS = {"--trust-remote-code": "", "--mem-fraction-static": "0.85"}

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

# The App deploy script per KIND, relative to `apps/memory` (the cwd of every
# `make memory-*` target). One purpose per engine (ADR-009 §2): vLLM serves
# embeddings, SGLang serves LLMs.
_APP_SCRIPTS: dict[ModelKind, str] = {
    "embedding": "deploy/modal_vllm_embedding.py",
    "llm": "deploy/modal_sglang_llm.py",
}


class DeploySpec(BaseModel):
    """What BOTH App deploys need, resolved locally (ADR-009 §3).

    Configuration only — a credential never enters it (the Hugging Face token
    travels as an ephemeral ``modal.Secret``, ADR-009 §9), because the spec is
    baked into a cached, inspectable image layer.

    It carries no ``engine``: the SCRIPT is the engine (vLLM serves the
    embedding entries, SGLang the LLM ones), so the only engine fact a script
    needs is the version it pins.
    """

    repo_id: str = Field(description="Hugging Face repo id of the weights to serve.")
    revision: str = Field(description="Commit sha or branch of those weights.")
    app_name: str = Field(description="Modal app name — the same on every path.")
    server_name: str = Field(description="Server class name inside that app.")
    gpu: str = Field(description="Modal GPU string, e.g. A10.")
    cpu: float
    memory_mb: int
    engine_version: str = Field(
        description="Pinned engine version: a PyPI version for vLLM, a docker "
        "tag of lmsysorg/sglang for SGLang."
    )
    autoinference_utils_version: str
    server_args: dict[str, str] = Field(
        description="Full engine argv: the catalog's extras + the builder-owned keys."
    )


class EmbeddingDeploySpec(DeploySpec):
    """One vLLM App deploy: the shared spec plus the vector width."""

    native_dimensions: int = Field(
        description="Vector width the server returns untruncated."
    )


class LLMDeploySpec(DeploySpec):
    """One SGLang App deploy: the shared spec plus the tensor parallelism.

    ``n_gpus`` is BOTH the ``:N`` of the Modal GPU string and SGLang's ``tp``,
    which is why the script builds ``gpu=f"{gpu}:{n_gpus}"`` from it instead of
    the catalog carrying a second, droppable copy.
    """

    n_gpus: int = Field(description="GPUs of `gpu` this App runs on = SGLang's tp.")


def get_catalog_entry(model: str) -> ModalModelConfig:
    """Return the catalog entry whose ``repo_id`` is exactly ``model``.

    ONE lookup over BOTH lists — the entry's ``kind`` tells the caller which
    one it came from, so no caller passes a kind in. Matching is exact, never
    fuzzy: a near-miss id must fail loudly rather than silently deploy or
    query the wrong weights (a ``repo_id`` is unique across both lists, so the
    answer cannot depend on the order).

    Raises:
        ModelError: ``model`` is in neither list. The message lists every id
            that IS, per group and sorted, and names the file to edit.
    """

    for entry in [*app_config.modal.embedding_models, *app_config.modal.llm_models]:
        if entry.repo_id == model:
            return entry

    embeddings = ", ".join(sorted(e.repo_id for e in app_config.modal.embedding_models))
    llms = ", ".join(sorted(e.repo_id for e in app_config.modal.llm_models))
    raise ModelError(
        f"Unknown Modal model {model!r}. "
        f"Modal catalog ids — embeddings: {embeddings}; llms: {llms}. "
        "Add an entry under modal.embedding_models or modal.llm_models in "
        "configs/default.yaml."
    )


def get_embedding_entry(model: str) -> ModalEmbeddingModelConfig:
    """:func:`get_catalog_entry`, for a caller that can only serve embeddings.

    Raises:
        ModelError: ``model`` is unknown, or it is an LLM entry — which the
            embedding client and the embedding deploy spec cannot use, and
            whose `native_dimensions` do not exist.
    """

    entry = get_catalog_entry(model)
    if not isinstance(entry, ModalEmbeddingModelConfig):
        raise ModelError(
            f"{model} is an LLM entry (modal.llm_models), not an embedding model."
        )
    return entry


def get_llm_entry(model: str) -> ModalLLMModelConfig:
    """:func:`get_catalog_entry`, for a caller that can only serve LLMs.

    Raises:
        ModelError: ``model`` is unknown, or it is an embedding entry.
    """

    entry = get_catalog_entry(model)
    if not isinstance(entry, ModalLLMModelConfig):
        raise ModelError(
            f"{model} is an embedding entry (modal.embedding_models), not an LLM."
        )
    return entry


def app_script(kind: ModelKind) -> str:
    """The deploy script of the App that serves ``kind`` (ADR-009 §2).

    The ONE place the kind becomes a file path, so the router, the driver's
    "does the file exist" check and the dry-run plan cannot disagree.
    """

    return _APP_SCRIPTS[kind]


def build_server_args(entry: ModalEmbeddingModelConfig) -> dict[str, str]:
    """The full vLLM argv for an embedding ``entry``.

    The catalog's ``extra_server_args`` plus the keys the builder owns: the
    pooling mode every embedding server runs in, the pinned revision, the
    served model id, and the context window only when the entry sets one
    (otherwise vLLM's own default wins).

    No ``engine`` parameter: the SCRIPT is the engine, and the only script that
    calls this is ``deploy/modal_vllm_embedding.py`` (ADR-009 §2).
    """

    args = dict(entry.extra_server_args)
    args["--revision"] = entry.revision
    args["--served-model-name"] = entry.repo_id
    args["--runner"] = "pooling"
    if entry.max_model_len is not None:
        args["--max-model-len"] = str(entry.max_model_len)
    return args


def build_llm_server_args(entry: ModalLLMModelConfig) -> dict[str, str]:
    """The full SGLang argv for an LLM ``entry`` (ADR-009 §2).

    Generic-safe flags only: the served model id, the pinned revision, the
    context window when the entry sets one, and the two tuning defaults an
    entry MAY override (``--trust-remote-code``, ``--mem-fraction-static``).
    Speculative decoding, mamba and multimodal flags are Modal's MODEL-SPECIFIC
    tuning for a 120B and a 35B-MoE model — a generic script must not assume
    them, and a model that wants ``--reasoning-parser`` or
    ``--tool-call-parser`` names it in ``extra_server_args``.

    ``--tp`` is NOT here: ``autoinference-utils`` renders it from
    ``SGLangEndpoint(tp=…)``, which the script passes ``n_gpus`` to.

    Overrides are merged by FLAG NAME, not by dict key, because
    ``autoinference-utils`` accepts the attached spelling too: without it,
    ``{"--mem-fraction-static=0.75": ""}`` would leave BOTH keys in the argv
    and SGLang would see the flag twice.
    """

    args = _merge_by_flag(_LLM_TUNING_DEFAULTS, entry.extra_server_args)
    args["--revision"] = entry.revision
    args["--served-model-name"] = entry.repo_id
    if entry.max_model_len is not None:
        args["--context-length"] = str(entry.max_model_len)
    return args


def build_deploy_spec(model: str) -> EmbeddingDeploySpec:
    """Resolve ``model`` into the spec the vLLM App deploy is driven by.

    Raises:
        ModelError: ``model`` is not an embedding entry, or ``vllm`` has no
            pinned version under ``modal.engines``.
    """

    entry = get_embedding_entry(model)

    return EmbeddingDeploySpec(
        repo_id=entry.repo_id,
        revision=entry.revision,
        app_name=entry.app_name,
        server_name=MODAL_SERVER_NAME,
        gpu=entry.gpu,
        cpu=entry.cpu,
        memory_mb=entry.memory_mb,
        native_dimensions=entry.native_dimensions,
        engine_version=_engine_version("vllm"),
        autoinference_utils_version=app_config.modal.autoinference_utils_version,
        server_args=build_server_args(entry),
    )


def build_llm_deploy_spec(model: str) -> LLMDeploySpec:
    """Resolve ``model`` into the spec the SGLang App deploy is driven by.

    Raises:
        ModelError: ``model`` is not an LLM entry, or ``sglang`` has no pinned
            version under ``modal.engines``.
    """

    entry = get_llm_entry(model)

    return LLMDeploySpec(
        repo_id=entry.repo_id,
        revision=entry.revision,
        app_name=entry.app_name,
        server_name=MODAL_SERVER_NAME,
        gpu=entry.gpu,
        n_gpus=entry.n_gpus,
        cpu=entry.cpu,
        memory_mb=entry.memory_mb,
        engine_version=_engine_version("sglang"),
        autoinference_utils_version=app_config.modal.autoinference_utils_version,
        server_args=build_llm_server_args(entry),
    )


def _engine_version(engine: str) -> str:
    """The pinned version of ``engine``, or a loud failure.

    Raises:
        ModelError: ``modal.engines`` has no entry for it — a YAML edit that
            removed the pin the script builds its image from.
    """

    engine_config = app_config.modal.engines.get(engine)  # type: ignore[arg-type]
    if engine_config is None:
        known = ", ".join(sorted(app_config.modal.engines))
        raise ModelError(
            f"No pinned version for engine {engine!r}. "
            f"Engines in modal.engines: {known}."
        )
    return engine_config.version


def _merge_by_flag(
    defaults: dict[str, str], overrides: dict[str, str]
) -> dict[str, str]:
    """``defaults`` overlaid with ``overrides``, keyed by FLAG name.

    ``--flag=value`` and ``--flag value`` set the same flag
    (``autoinference-utils`` 0.2.6, ``server_arg_tokens``), so an override in
    either spelling must REPLACE the default rather than join it.
    """

    merged = {key.partition("=")[0]: (key, value) for key, value in defaults.items()}
    merged.update(
        (key.partition("=")[0], (key, value)) for key, value in overrides.items()
    )
    return dict(merged.values())


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
    entry: ModalModelConfig,
    target: Literal["endpoint", "app"],
    base_model: str | None = None,
) -> list[str]:
    """The ``modal`` argv that deploys or stops ``entry`` as ``target``.

    ``target`` is what the command CREATES (or stops), which the ROUTER
    decides at runtime — the catalog knows nothing about it. ``base_model`` is
    the Modal-catalog ancestor a custom-weights create is built on: given, the
    create serves ``entry.repo_id``'s weights on that base's recipe; ``None``
    (the common case) creates the endpoint for ``repo_id`` itself.

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

    if target == "endpoint":
        if action == "stop":
            return ["modal", "endpoint", "stop", "-y", entry.endpoint_name]

        argv = [
            "modal",
            "endpoint",
            "create",
            "--name",
            entry.endpoint_name,
            "--model",
            base_model or entry.repo_id,
        ]
        # Custom weights: Modal serves `repo_id` on `base_model`'s recipe.
        if base_model and base_model != entry.repo_id:
            argv += [
                "--custom-hf-repo",
                entry.repo_id,
                "--custom-hf-revision",
                entry.revision,
            ]
        return argv + ["--routing-region", MODAL_ROUTING_REGION]

    if action == "stop":
        return ["modal", "app", "stop", "-y", entry.app_name]
    return ["modal", "deploy", app_script(entry.kind)]


def hf_token_args(base_model: str | None, token: str) -> list[str]:
    """The ``--custom-hf-token`` pair, or ``[]`` (ADR-009 §9).

    Appended ONLY to a CUSTOM-WEIGHTS create (``base_model`` given): Modal
    documents the flag as the token "for private --custom-hf-repo"
    (re-verified in ``modal endpoint create --help`` on modal 1.5.5). Our App
    scripts get the token as an ephemeral ``modal.Secret`` instead (#142), and
    an empty token changes no argv at all.
    """

    if not token or not base_model:
        return []
    return [_HF_TOKEN_FLAG, token]


def hf_token_env() -> dict[str, str]:
    """The env dict an App script's ephemeral ``modal.Secret`` carries.

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
    identically on both Serving paths.

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
