import asyncio
import logging

from beanie import PydanticObjectId
from prefect import flow, task

from tree.config.settings import settings
from tree.config.sources import HuggingFaceDatasetSource, default_configured_sources
from tree.data.batch import gather_isolated
from tree.data.huggingface.arxiv_dataset import (
    extract_document as _extract_document,
    fetch_dataset_batches as _fetch_dataset_batches,
    fetch_paper_content as _fetch_paper_content,
    load_document as _load_document,
)
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)

ARXIV_DATASET_ID = "librarian-bots/arxiv-metadata-snapshot"


def arxiv_window_entries(
    entry: HuggingFaceDatasetSource,
) -> list[HuggingFaceDatasetSource]:
    """Fan ONE HuggingFace dataset entry into its disjoint offset-**Window**s.

    Pure decision logic (no DB, no Prefect) shared by the data coordinator (#072,
    ADR-002 §3 amendment #070–#074): the coordinator calls this per configured
    ``HuggingFaceDatasetSource`` and dispatches one ``data-etl-worker`` run per
    returned window-entry. Each returned entry is a COPY of ``entry`` (the configured
    entry is NEVER mutated — ``offset`` is a dispatch-time runtime coordinate, #070)
    stamped with that window's ``offset`` + ``max_samples`` via ``model_copy``.

    Window math (``n = entry.num_workers``, ``m = entry.max_samples``):

    * ``window_size = m // n``; window ``i`` ⇒ ``offset = i * window_size`` and
      ``max_samples = window_size``, EXCEPT the LAST window which takes the remainder
      ``m - offset`` so the windows tile ``[0, m)`` exactly (no gap, no overlap, no
      dropped rows when ``m`` isn't divisible by ``n``).
    * ``num_workers == 1`` ⇒ a single window with ``offset`` left UNSET (``None``) and
      ``max_samples`` unchanged — byte-identical to the pre-feature single HF run.
    * ``max_samples == 0`` ⇒ NO windows (empty list — a clean no-op for that entry).
    * ``num_workers > max_samples`` (with ``m >= 1``) ⇒ CLAMP the effective worker
      count to ``m`` so no window has ``max_samples <= 0``: emit ``m`` windows of
      size 1 tiling ``[0, m)``.

    Returns an order-stable list (window 0 first) so callers/tests can assert exact
    shard contents.
    """

    max_samples = entry.max_samples
    if max_samples <= 0:
        return []

    # A single worker reproduces today's run exactly: no offset, full max_samples.
    if entry.num_workers <= 1:
        return [entry.model_copy()]

    # Clamp so no window collapses to <= 0 rows: at most one window per row.
    effective_workers = min(entry.num_workers, max_samples)
    window_size = max_samples // effective_workers

    windows: list[HuggingFaceDatasetSource] = []
    for i in range(effective_workers):
        offset = i * window_size
        is_last = i == effective_workers - 1
        window_max_samples = max_samples - offset if is_last else window_size
        windows.append(
            entry.model_copy(
                update={"offset": offset, "max_samples": window_max_samples}
            )
        )
    return windows


# --- ETL-phase BATCH tasks --------------------------------------------------
# One task per ETL phase, each operating over ONE ``batch_size``-chunk (the list
# of raw dicts ``fetch_dataset_batches`` yields) instead of per row. This keeps a
# window worker at a few TENS of Prefect task runs (a small constant per chunk)
# rather than ~1000 (one per row).
#
# Result persistence is OFF by default in Prefect 3.6 (the repo sets no
# ``persist_result`` / ``result_storage`` / ``cache_policy`` and no
# ``PREFECT_RESULTS_PERSIST_BY_DEFAULT``), so these side-effecting Extract/Load
# tasks already do NOT persist results — matching every other data-layer task. We
# add no ``persist_result`` flag (it would only matter alongside a ``cache_policy``,
# which we do not introduce).
#
# Per-element isolation: per-element / bad-data failures are caught by an
# ``asyncio.gather(return_exceptions=True)``, logged at WARNING, and the element is
# skipped. NOT uniform across the two phases, deliberately: ``load_batch`` uses the
# shared ``gather_isolated`` (a failed element is DROPPED), while ``enrich_batch``
# keeps its own gather because a failed enrich must PASS THE DOC THROUGH with empty
# content rather than lose it — enrichment is optional, the document is not.
# A task hard-fails (so Prefect retries the WHOLE batch)
# only on a batch-WIDE failure, which is SAFE because ``load_document`` dedups on
# ``(user_id, source_uri)`` so a retried batch never double-inserts.


@task(name="transform-arxiv-batch", retries=0)
async def transform_batch(
    batch: list[dict], user_id: PydanticObjectId
) -> list[Document]:
    """Pure map ``list[dict] -> list[Document]`` over one chunk.

    Runs the pure ``arxiv_dataset.extract_document`` per raw entry and drops the
    ``None`` results (entries with no id, already warned by the core fn). No
    network, no DB → ``retries=0``.
    """

    documents = [_extract_document(entry, user_id) for entry in batch]
    return [doc for doc in documents if doc is not None]


@task(name="enrich-arxiv-batch", retries=3, retry_delay_seconds=5)
async def enrich_batch(docs: list[Document], concurrency: int) -> list[Document]:
    """Fetch paper HTML per element under ``asyncio.Semaphore(concurrency)``.

    Network Extract — only invoked when ``fetch_content`` is true. Each element's
    fetch runs under the existing concurrency bound; a per-element fetch failure is
    logged + the doc passes through with empty content (NEVER sinks the batch).
    Every input doc is returned (enriched in place where the fetch succeeded).
    """

    semaphore = asyncio.Semaphore(concurrency)

    async def _enrich(doc: Document) -> Document:
        async with semaphore:
            content = await _fetch_paper_content(doc.source_uri)
        if content:
            doc.content = content
        return doc

    results = await asyncio.gather(
        *[_enrich(doc) for doc in docs], return_exceptions=True
    )

    enriched: list[Document] = []
    for doc, result in zip(docs, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "Failed to fetch content for %s; passing through empty",
                doc.source_uri,
                exc_info=result,
            )
            enriched.append(doc)
        else:
            enriched.append(result)
    return enriched


@task(name="load-arxiv-batch", retries=3, retry_delay_seconds=5)
async def load_batch(docs: list[Document]) -> list[Document]:
    """Dedup + persist one chunk via a SINGLE ``gather(return_exceptions=True)``.

    Awaits the pure ``arxiv_dataset.load_document`` per element via the shared
    ``gather_isolated`` helper and returns the successful, non-``None`` subset
    (duplicates drop out as ``None``). A per-element load failure is logged at WARNING +
    skipped, NOT propagated — so one bad row never sinks the chunk. Retried whole-batch
    on a batch-WIDE infra failure, safe via the ``(user_id, source_uri)`` dedup. Tier F
    (idempotent Mongo write) → ``retries=3`` / 5 s = a 15 s budget (ADR-002 #096).
    """

    ingested, failures = await gather_isolated(docs, _load_document)
    if failures:
        logger.warning("load_batch: %d/%d elements failed", failures, len(docs))
    return ingested


def _get_huggingface_arxiv_defaults() -> HuggingFaceDatasetSource:
    """The configured arxiv HF source entry, or a defaults-only one.

    Walks the shared source loader's ``default_configured_sources()`` list
    (backfill + listen, read-only) and picks the first ``HuggingFaceDatasetSource``
    entry whose ``uri`` matches the arxiv dataset id. Falls back to
    ``HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID)`` defaults if no such entry
    exists.

    Returns the ENTRY rather than a positional tuple of its fields, so adding a
    knob to the source model doesn't mean editing a tuple shape in four places.
    """

    for entry in default_configured_sources():
        if (
            isinstance(entry, HuggingFaceDatasetSource)
            and entry.uri == ARXIV_DATASET_ID
        ):
            return entry

    return HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID)


@flow(name="ingest-arxiv-dataset-etl", log_prints=True)
async def ingest_arxiv_dataset(
    user_id: PydanticObjectId,
    max_samples: int | None = None,
    fetch_content: bool | None = None,
    offset: int | None = None,
) -> list[Document]:
    """Ingest the arxiv HF dataset for ``user_id``.

    ``offset`` (#071) selects a disjoint window of the stream: the ingest skips the
    first ``offset`` rows and then streams ``max_samples`` rows — i.e. this run
    persists rows ``[offset, offset + max_samples)``. ``offset=None`` (the default,
    and what a non-windowed entry forwards) applies NO skip and reproduces today's
    single-run ingest exactly.

    ACCEPTED RETRY EXCEPTION (ADR-002 amendment #096): ``_fetch_dataset_batches`` is a
    streamed generator this flow iterates, so it CANNOT be a task — it is the one
    unretried network read in the data layer. A retry would mean re-streaming from row 0,
    so the flow carries no ``retries`` either; the per-chunk tasks below cover everything
    after the read.
    """

    defaults = _get_huggingface_arxiv_defaults()
    batch_size = defaults.batch_size
    concurrency = defaults.concurrency
    if max_samples is None:
        max_samples = defaults.max_samples
    if fetch_content is None:
        fetch_content = defaults.fetch_content

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    ingested: list[Document] = []

    # Extract stays the flow-level loop: ``fetch_dataset_batches`` is a streamed
    # generator (NOT a task). Per yielded chunk we run the batch-grain ETL tasks.
    for batch in _fetch_dataset_batches(max_samples, batch_size, offset=offset):
        documents = await transform_batch(batch, user_id)

        if fetch_content:
            documents = await enrich_batch(documents, concurrency)

        ingested.extend(await load_batch(documents))

        logger.info("Batch processed: %d ingested so far", len(ingested))

    logger.info("Ingested %d new arxiv documents", len(ingested))

    return ingested
