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

from typing import Literal

from pydantic import BaseModel, Field

from tree.config.app_config import (
    ModalEmbeddingModelConfig,
    ServingPath,
    app_config,
)
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
