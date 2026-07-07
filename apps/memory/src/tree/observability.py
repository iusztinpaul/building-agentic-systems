"""Opik observability — monitoring only (cost breakdown + retrieval threads).

This is the single seam between the memory app and the Opik SDK. Every call
site imports the helpers from here rather than reaching into ``opik`` directly,
so the **no-key path stays safe everywhere**:

* When ``OPIK_API_KEY`` is unset, :func:`configure_opik` logs a warning and
  leaves Opik unconfigured. The :func:`track` decorator is still applied to
  call sites, but with no configured client the SDK records nothing and the
  decorated function behaves exactly as if undecorated.
* Recording telemetry is **fail-open**: a failure to configure, wrap, or flush
  Opik must NEVER break a model call, a tool call, or a pipeline run. Every
  public helper here swallows its own exceptions and logs a warning.

Scope is MONITORING ONLY — no evals, no datasets/experiments, no guardrails.
The two products are (1) a per-model cost breakdown over every ingestion /
retrieval model call (spans carry usage + ``total_cost``), and (2) traces +
spans grouped into threads for the retrieval side.

Distributed tracing across processes (the Prefect + ``run_deployment`` problem)
----------------------------------------------------------------------------
Prefect ``serve()`` executes flow runs in SUBPROCESSES, and ``run_deployment``
crosses process boundaries entirely. Opik's tracing context lives in
contextvars, which do NOT survive either hop, so a naive ``@track`` on each task
mints a fresh root trace per task (~650 fragmented traces for one extraction
run) and ``configure_opik()`` called only at serve-startup never runs in the
flow-run subprocess, so :func:`track_genai_client` returns the client UNWRAPPED
(no Gemini spans, no usage, no cost).

Two seam features fix this:

1. **Lazy idempotent configuration** (:func:`_ensure_configured`). Every
   configure-dependent helper self-configures on first use in WHATEVER process
   it runs in. Flows also call :func:`configure_opik` at entry, before any model
   factory, so Gemini wrapping actually happens in the flow-run subprocess.
2. **Explicit distributed-trace propagation**. A flow grabs
   :func:`get_distributed_trace_headers` (a plain JSON-serializable dict) and
   passes it to its tasks as an ordinary Prefect parameter. Each task body opens
   its span via :func:`span` (which forwards the headers to
   ``opik.start_as_current_span(opik_distributed_trace_headers=...)``), so every
   task span attaches to the flow's trace instead of starting its own root.
   ``run_deployment`` calls forward the same headers as a flow parameter, so the
   coordinator → worker → indexing chain is one trace.
"""

import contextlib
import functools
import logging
import os
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, TypeVar

import opik
from opik import opik_context

from tree.config.settings import settings

logger = logging.getLogger(__name__)


def _tracking_disabled() -> bool:
    """True when ``OPIK_TRACK_DISABLE`` is set truthy in the environment.

    A hard kill switch that overrides a present ``OPIK_API_KEY``. ``opik.track``
    already honors this env var natively (it no-ops), but ``opik.start_as_current_span``
    — which our :func:`span` helper rides on — does NOT: it still ships traces to
    the backend even with the var set. So the seam treats the var as "Opik is
    disabled" in :func:`is_opik_configured`, which makes EVERY seam helper
    (``span`` / ``track_genai_client`` / ``record_embedding_usage`` / …) short-circuit
    before touching the SDK. The test suite sets this var (see
    ``tests/conftest.py``) so a test that runs a real ``@track``/``span``-decorated
    production function with a mocked model can never pollute the production Opik
    project with a fake trace.
    """

    return os.environ.get("OPIK_TRACK_DISABLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# ---------------------------------------------------------------------------
# Tag families
# ---------------------------------------------------------------------------
# Call sites NEVER hand-write tag strings; they import a combination constant
# from here so the tags are enforced in one place. Two families:
#
#  1. PIPELINE-IDENTITY tags — the data / memory-extraction / memory-indexing
#     pipelines carry these, and they match their Prefect deployment / flow-run
#     tags 1:1: the SAME constant feeds both ``prefect.tags(...)`` and the Opik
#     ``span(tags=...)``, so a Prefect run and its Opik trace read identically.
#  2. The MCP-SURFACE family — two orthogonal axes for the MCP tools + dream:
#     WHAT the work does (``ingestion`` writes vs ``retrieval`` reads) × WHERE it
#     runs (``batch`` offline Prefect vs ``mcp`` online tool).
#
# ``metadata={"pipeline": "<name>"}`` still carries the finer pipeline name on
# the span/trace (see :func:`pipeline_metadata`).
TAG_INGESTION = "ingestion"
TAG_RETRIEVAL = "retrieval"
TAG_BATCH = "batch"
TAG_MCP = "mcp"

# --- Pipeline-identity tags: shared 1:1 by Prefect (deployment + flow-run tags)
# and Opik (span/trace tags). Data ETL splits by mode (offline batch vs online
# single-source); extraction/indexing have no mode (same deployment both ways). ---
TAG_DATA_PIPELINE = "data-pipeline"
TAG_MEMORY_PIPELINE = "memory-pipeline"
TAG_EXTRACTION = "extraction"
TAG_INDEXING = "indexing"
TAG_OFFLINE = "offline"
TAG_ONLINE = "online"

TAGS_DATA_OFFLINE = [TAG_DATA_PIPELINE, TAG_OFFLINE]  # offline data ETL (config batch)
TAGS_DATA_ONLINE = [
    TAG_DATA_PIPELINE,
    TAG_ONLINE,
]  # online ingest (url/file/conversation)
TAGS_EXTRACTION = [
    TAG_MEMORY_PIPELINE,
    TAG_EXTRACTION,
]  # memory extraction (both modes)
TAGS_INDEXING = [TAG_MEMORY_PIPELINE, TAG_INDEXING]  # memory indexing

# Dream consolidation — the remaining batch pipeline still on the MCP-surface family.
TAGS_INGESTION_BATCH = [TAG_INGESTION, TAG_BATCH]
# MCP ingest tools (ingest_url / ingest_file / ingest_conversation / search_web).
TAGS_INGESTION_MCP = [TAG_INGESTION, TAG_MCP]
# MCP retrieval tools (query_memory / search_memory / deep_search_memory and any
# utility tool that reads memory, e.g. visualize_memory_graph).
TAGS_RETRIEVAL_MCP = [TAG_RETRIEVAL, TAG_MCP]
# MCP utility tools that neither read nor write memory (scrape_web, review_*,
# memory_dashboard) — just the surface marker.
TAGS_MCP = [TAG_MCP]


def pipeline_metadata(pipeline: str, **extra: Any) -> dict[str, Any]:
    """Build the span/trace metadata dict carrying the finer pipeline name.

    Complements the coarse pipeline-identity TAGS: the tag says *which* pipeline
    (``data-pipeline`` / ``memory-pipeline`` …) for dashboard filtering, while
    ``metadata={"pipeline": <name>}`` records the finer name (``"data"``,
    ``"extraction"``, ``"indexing"``, ``"file"``, ``"dream"``, …) for grouping.
    Pass any additional metadata keys as keyword args.
    """

    return {"pipeline": pipeline, **extra}


# Process-local memo: have we already attempted ``opik.configure`` in THIS
# process? Reset implicitly per process (module re-imported in each Prefect
# flow-run subprocess), which is exactly the granularity we want — every
# subprocess configures once on first use.
_CONFIGURED: bool = False

# Re-export the SDK's tracing context so call sites can attach usage / cost /
# thread_id without importing ``opik`` directly. ``track`` is re-exported as a
# thin alias (no wrapping needed — ``opik.track`` is already inert when Opik is
# unconfigured, which is exactly the no-key behavior we want).
track = opik.track

_R = TypeVar("_R")

__all__ = [
    "TAGS_DATA_OFFLINE",
    "TAGS_DATA_ONLINE",
    "TAGS_EXTRACTION",
    "TAGS_INDEXING",
    "TAGS_INGESTION_BATCH",
    "TAGS_INGESTION_MCP",
    "TAGS_MCP",
    "TAGS_RETRIEVAL_MCP",
    "TAG_BATCH",
    "TAG_DATA_PIPELINE",
    "TAG_EXTRACTION",
    "TAG_INDEXING",
    "TAG_INGESTION",
    "TAG_MCP",
    "TAG_MEMORY_PIPELINE",
    "TAG_OFFLINE",
    "TAG_ONLINE",
    "TAG_RETRIEVAL",
    "configure_opik",
    "flush_opik",
    "get_distributed_trace_headers",
    "is_opik_configured",
    "pipeline_metadata",
    "record_embedding_usage",
    "span",
    "track",
    "tracked_span",
    "track_genai_client",
    "update_current_span",
    "update_current_trace",
    "opik_context",
]


def configure_opik() -> None:
    """Configure the Opik SDK from settings, fail-open and idempotent.

    When ``OPIK_API_KEY`` is set, calls :func:`opik.configure` with
    ``use_local=False, force=True, automatic_approvals=True`` and the configured
    workspace / project name. Any failure (bad key, network) is swallowed with a
    warning so a misconfigured Opik never blocks server / worker startup. When
    the key is absent we log a warning and leave Opik unconfigured — the
    :func:`track` decorators then no-op cleanly.

    Idempotent: marks the process as configured and short-circuits on repeat
    calls, so flows can call it at entry without re-paying the cost. Flows MUST
    call this at entry (before any model factory) because Prefect runs each flow
    in a subprocess where serve-time configuration did NOT happen — without it
    :func:`track_genai_client` returns the Gemini client unwrapped.
    """

    global _CONFIGURED
    if _CONFIGURED:
        return

    if _tracking_disabled():
        logger.info(
            "OPIK_TRACK_DISABLE is set — Opik observability disabled (no traces)."
        )
        _CONFIGURED = True
        return

    api_key = settings.opik_api_key.get_secret_value()
    if not api_key:
        logger.warning(
            "OPIK_API_KEY is not set — Opik observability disabled. "
            "Set OPIK_API_KEY to enable cost tracking and retrieval threads."
        )
        # Mark configured so we don't re-log the warning on every helper call.
        _CONFIGURED = True
        return

    try:
        opik.configure(
            api_key=api_key,
            workspace=settings.opik_workspace or None,
            use_local=False,
            force=True,
            automatic_approvals=True,
            project_name=settings.opik_project_name,
        )
        logger.info(
            "Opik configured successfully (project=%s).", settings.opik_project_name
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break startup
        logger.warning(
            "Couldn't configure Opik (check OPIK_API_KEY / OPIK_* env vars): %s", exc
        )
    finally:
        _CONFIGURED = True


def _ensure_configured() -> None:
    """Self-configure Opik on first use in the current process (fail-open).

    Module-level configured flags do NOT survive into Prefect flow-run
    subprocesses, so any configure-dependent helper (``track_genai_client``,
    ``record_embedding_usage``, ``span``) calls this first. The actual
    :func:`opik.configure` only happens once per process via the ``_CONFIGURED``
    memo.
    """

    if not _CONFIGURED:
        configure_opik()


def is_opik_configured() -> bool:
    """True iff an ``OPIK_API_KEY`` is present.

    Used to guard SDK-only side effects (e.g. wrapping the genai client). When
    no key is set — or ``OPIK_TRACK_DISABLE`` is set — we skip the wrapping
    entirely so the underlying client is returned untouched and every seam helper
    no-ops before reaching the SDK.
    """

    if _tracking_disabled():
        return False
    return bool(settings.opik_api_key.get_secret_value())


def track_genai_client(client: Any) -> Any:
    """Wrap a google-genai ``Client`` with Opik's genai integration, fail-open.

    Returns the wrapped client (automatic spans + native Gemini token usage and
    cost) when Opik is configured, otherwise returns the client **unchanged**.
    Any failure to wrap is swallowed with a warning and the original client is
    returned, so an Opik problem never breaks Gemini calls.

    Self-configures first (:func:`_ensure_configured`) so wrapping works when
    ``get_llm()`` is called inside a Prefect flow-run subprocess — without this
    the genai client was returned unwrapped there and no Gemini spans / usage /
    cost ever appeared (the headline failure the human caught).
    """

    _ensure_configured()
    if not is_opik_configured():
        return client

    try:
        from opik.integrations.genai import track_genai

        return track_genai(client)
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the client
        logger.warning("Couldn't wrap genai client with Opik tracking: %s", exc)
        return client


def get_distributed_trace_headers() -> dict[str, str] | None:
    """Return the current trace's distributed headers, or ``None``, fail-open.

    Inside an active trace, returns ``{"opik_trace_id": ...,
    "opik_parent_span_id": ...}`` — a plain JSON-serializable dict safe to pass
    as a Prefect flow/task parameter across process boundaries. Returns ``None``
    when Opik is unconfigured or there is no active trace (so downstream code
    starts its own trace), and on any SDK error.
    """

    _ensure_configured()
    if not is_opik_configured():
        return None
    try:
        headers = opik_context.get_distributed_trace_headers()
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the caller
        logger.debug("Opik get_distributed_trace_headers no-op: %s", exc)
        return None
    return dict(headers) if headers else None


@contextlib.contextmanager
def span(
    name: str,
    *,
    type: str = "general",
    tags: list[str] | None = None,
    trace_headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> Iterator[None]:
    """Open an Opik span, optionally attached to a distributed parent, fail-open.

    A guarded wrapper around :func:`opik.start_as_current_span`. When
    ``trace_headers`` is provided, the span attaches to that parent trace via the
    reserved ``opik_distributed_trace_headers`` kwarg — this is how a Prefect
    task span joins its flow's trace across the subprocess boundary (contextvars
    don't propagate, but a header dict passed as a flow parameter does).

    ``create_duplicate_root_span=False`` removes the duplicate root-span child
    the SDK emits by default (the noise the human flagged).

    Fully fail-open: when Opik is unconfigured, has no key, or the SDK raises,
    this is a transparent no-op ``with`` block so the wrapped body always runs.
    """

    _ensure_configured()
    if not is_opik_configured():
        yield
        return

    span_kwargs: dict[str, Any] = {
        "type": type,
        "create_duplicate_root_span": False,
        **kwargs,
    }
    if tags is not None:
        span_kwargs["tags"] = tags
    if trace_headers is not None:
        span_kwargs["opik_distributed_trace_headers"] = trace_headers

    try:
        cm = opik.start_as_current_span(name, **span_kwargs)
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the caller
        logger.debug("Opik start_as_current_span no-op: %s", exc)
        yield
        return

    try:
        with cm:
            yield
    except Exception:
        # Re-raise the BODY's exception (the span context manager already
        # recorded it); telemetry failures inside ``cm`` are swallowed above.
        raise


def tracked_span(
    name: str,
    *,
    type: str = "general",
    tags: list[str] | None = None,
) -> Callable[[Callable[..., Awaitable[_R]]], Callable[..., Awaitable[_R]]]:
    """Decorate an async function so its body runs inside an Opik :func:`span`.

    The decorated function MUST accept an ``opik_trace_headers: dict | None``
    keyword argument (the distributed-trace headers the flow grabs and passes
    down). The decorator pops it from the call kwargs and opens the span attached
    to that parent trace — so the task span joins the flow's trace across the
    Prefect task boundary. When absent / unconfigured the span is a transparent
    no-op and the body runs unchanged.

    Use this for the no-LLM batched tasks (validate / resolve / dedupe /
    apply-writes / indexing) where re-indenting the whole body under a ``with``
    block would be noise; the LLM task wraps its body in :func:`span` directly so
    the nested Gemini spans nest under it via contextvars.
    """

    def decorator(
        func: Callable[..., Awaitable[_R]],
    ) -> Callable[..., Awaitable[_R]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> _R:
            headers = kwargs.get("opik_trace_headers")
            with span(name, type=type, tags=tags, trace_headers=headers):
                return await func(*args, **kwargs)

        return wrapper

    return decorator


def update_current_span(**kwargs: Any) -> None:
    """Attach usage / cost / metadata to the current Opik span, fail-open.

    A thin guard around :func:`opik.opik_context.update_current_span`. When
    there is no active span (Opik unconfigured, or called outside a ``@track``
    function) or the SDK raises, the call is a silent no-op so recording
    telemetry can never fail the caller.
    """

    try:
        opik_context.update_current_span(**kwargs)
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the caller
        logger.debug("Opik update_current_span no-op: %s", exc)


def update_current_trace(**kwargs: Any) -> None:
    """Attach thread_id / tags / metadata to the current Opik trace, fail-open.

    A thin guard around :func:`opik.opik_context.update_current_trace`. No-ops
    when there is no active trace or the SDK raises.
    """

    try:
        opik_context.update_current_trace(**kwargs)
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the caller
        logger.debug("Opik update_current_trace no-op: %s", exc)


def record_embedding_usage(
    *,
    provider: str,
    model: str,
    total_tokens: int | None,
    total_cost: float,
) -> None:
    """Record embedding usage + cost on the current Opik span, fail-open.

    Attaches ``provider``/``model``/``total_cost`` and (when ``total_tokens`` is
    known) a ``usage`` dict in Opik's ``prompt_tokens``/``total_tokens`` shape.
    For embedding calls there are no completion tokens, so ``completion_tokens``
    is 0 and ``prompt_tokens == total_tokens``.

    Self-hosted providers (Modal) pass ``total_cost=0`` — the token counts are
    still recorded. Self-configures first so the recording lands on a real span
    even in a Prefect flow-run subprocess. A failure to record telemetry is
    swallowed.
    """

    _ensure_configured()
    span_kwargs: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "total_cost": total_cost,
    }
    if total_tokens is not None:
        span_kwargs["usage"] = {
            "prompt_tokens": total_tokens,
            "completion_tokens": 0,
            "total_tokens": total_tokens,
        }
    update_current_span(**span_kwargs)


def flush_opik() -> None:
    """Flush batched Opik traces/spans, fail-open.

    Call on shutdown paths and at the end of each Prefect flow so batched
    telemetry isn't lost in a long-lived serve worker. A no-op when Opik is
    unconfigured; swallows any flush failure with a warning.
    """

    if not is_opik_configured():
        return

    try:
        opik.flush_tracker()
    except Exception as exc:  # noqa: BLE001 — flushing must never break shutdown
        logger.warning("Opik flush failed: %s", exc)
