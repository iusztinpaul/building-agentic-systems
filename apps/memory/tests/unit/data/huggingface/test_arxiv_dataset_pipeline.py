"""Unit tests for ``tree.data.huggingface.arxiv_dataset_pipeline`` (#078).

The arXiv HF leaf pipeline is BATCH-grain: per streamed ``batch_size``-chunk the
flow runs three ETL-phase steps — ``transform_batch`` (pure map, PLAIN function
per ADR-002 #097), ``enrich_batch`` (network task, only when ``fetch_content``),
and ``load_batch`` (DB Load task). Per-element failures are isolated INSIDE
each task via ``asyncio.gather(return_exceptions=True)`` — a bad element is logged
+ skipped and the task returns the successful subset; only a batch-WIDE infra
failure hard-fails the task (Prefect then retries the whole batch, safe because
``load_document`` dedups on ``(user_id, source_uri)``).
"""

from unittest.mock import AsyncMock

from beanie import PydanticObjectId

from tree.config.sources import HuggingFaceDatasetSource
from tree.data.huggingface.arxiv_dataset_pipeline import (
    ARXIV_DATASET_ID,
    enrich_batch,
    ingest_arxiv_dataset,
    load_batch,
    transform_batch,
)
from tree.entities.documents import Document, SourceType


def _entry(**overrides) -> HuggingFaceDatasetSource:
    """A dispatched arxiv window entry — the flow's only config input."""

    return HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID, **overrides)


def _make_doc(arxiv_id: str = "2103.00001") -> Document:
    return Document(
        source_type=SourceType.HUGGINGFACE,
        source_uri=f"https://arxiv.org/abs/{arxiv_id}",
        user_id=PydanticObjectId(),
        title="Test Paper",
        summary="Abstract",
        content="",
        authors=["Author"],
    )


def _raw(arxiv_id: str = "2103.12345") -> dict:
    return {
        "id": arxiv_id,
        "authors": "Jane Doe",
        "title": "Test",
        "abstract": "Abstract",
        "update_date": "2021-03-24",
    }


class TestTaskMetadata:
    """Retry grain lives on the batch tasks (mirrors test_web_pipeline)."""

    def test_transform_batch_is_a_plain_function(self) -> None:
        # #097: a pure map (no I/O) is NOT a task — task-hood buys it nothing.
        assert not hasattr(transform_batch, "fn")

    def test_enrich_batch_retries(self) -> None:
        # Tier F (free arxiv.org HTML) → 3 x 5 s = 15 s (ADR-002 #096).
        assert enrich_batch.retries == 3
        assert enrich_batch.retry_delay_seconds == 5
        assert enrich_batch.name == "enrich-arxiv-batch"

    def test_load_batch_retries(self) -> None:
        # Tier F (idempotent Mongo write) → 3 x 5 s = 15 s, sized to outlast a
        # primary election (~10-30 s).
        assert load_batch.retries == 3
        assert load_batch.retry_delay_seconds == 5
        assert load_batch.name == "load-arxiv-batch"

    def test_flow_name(self) -> None:
        assert ingest_arxiv_dataset.name == "ingest-arxiv-dataset-etl"


class TestTransformBatch:
    """Pure map ``list[dict] -> list[Document]``; drops id-less entries."""

    async def test_maps_valid_entries(self) -> None:
        user_id = PydanticObjectId()

        result = await transform_batch(
            [_raw("2103.00001"), _raw("2103.00002")], user_id
        )

        assert len(result) == 2
        assert {d.source_uri for d in result} == {
            "https://arxiv.org/abs/2103.00001",
            "https://arxiv.org/abs/2103.00002",
        }
        assert all(d.user_id == user_id for d in result)

    async def test_drops_idless_entry(self) -> None:
        bad = {"title": "No id", "abstract": "x"}

        result = await transform_batch([_raw("2103.00001"), bad], PydanticObjectId())

        assert len(result) == 1
        assert result[0].source_uri == "https://arxiv.org/abs/2103.00001"

    async def test_empty_batch_returns_empty(self) -> None:
        result = await transform_batch([], PydanticObjectId())

        assert result == []


class TestEnrichBatch:
    """Network Extract; per-element fetch failures never sink the batch."""

    async def test_sets_content_per_element(self, mocker) -> None:
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._fetch_paper_content",
            new_callable=AsyncMock,
            return_value="Full text",
        )
        docs = [_make_doc("2103.00001"), _make_doc("2103.00002")]

        result = await enrich_batch.fn(docs, concurrency=2)

        assert len(result) == 2
        assert all(d.content == "Full text" for d in result)

    async def test_element_fetch_failure_passes_through_with_empty_content(
        self, mocker
    ) -> None:
        doc_ok = _make_doc("2103.00001")
        doc_bad = _make_doc("2103.00002")

        async def _fetch(source_uri: str) -> str:
            if source_uri == doc_bad.source_uri:
                raise RuntimeError("boom")
            return "Full text"

        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._fetch_paper_content",
            side_effect=_fetch,
        )

        result = await enrich_batch.fn([doc_ok, doc_bad], concurrency=2)

        # The whole batch survives; the failing element passes through empty.
        assert len(result) == 2
        by_uri = {d.source_uri: d for d in result}
        assert by_uri[doc_ok.source_uri].content == "Full text"
        assert by_uri[doc_bad.source_uri].content == ""

    async def test_runs_under_concurrency_semaphore(self, mocker) -> None:
        in_flight = 0
        peak = 0

        async def _fetch(source_uri: str) -> str:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            in_flight -= 1
            return "x"

        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._fetch_paper_content",
            side_effect=_fetch,
        )
        docs = [_make_doc(f"2103.{i:05d}") for i in range(6)]

        await enrich_batch.fn(docs, concurrency=2)

        # The bound is honoured: never more than ``concurrency`` fetches at once.
        assert peak <= 2


class TestLoadBatch:
    """DB Load over a single gather; per-element failures isolated, dups dropped."""

    async def test_returns_persisted_subset_dropping_duplicates(self, mocker) -> None:
        doc_a = _make_doc("2103.00001")
        doc_b = _make_doc("2103.00002")
        load_mock = mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._load_document",
            new_callable=AsyncMock,
            # doc_a persisted, doc_b a duplicate (None).
            side_effect=[doc_a, None],
        )

        result = await load_batch.fn([doc_a, doc_b])

        assert result == [doc_a]
        # ONE awaited gather over the chunk: one load call per element, not N tasks.
        assert load_mock.await_count == 2

    async def test_isolates_one_element_failure(self, mocker) -> None:
        doc_a = _make_doc("2103.00001")
        doc_b = _make_doc("2103.00002")
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._load_document",
            new_callable=AsyncMock,
            side_effect=[doc_a, RuntimeError("bad row")],
        )

        # The raise is caught by gather(return_exceptions=True); NOT propagated.
        result = await load_batch.fn([doc_a, doc_b])

        assert result == [doc_a]

    async def test_empty_batch_returns_empty(self, mocker) -> None:
        load_mock = mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._load_document",
            new_callable=AsyncMock,
        )

        result = await load_batch.fn([])

        assert result == []
        load_mock.assert_not_awaited()


class TestIngestArxivDataset:
    """The flow loops the streamed Extract and calls the batch tasks per chunk."""

    async def test_returns_full_ingested_list(self, mocker) -> None:
        entries = [_raw(f"2103.{i:05d}") for i in range(4)]
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._fetch_dataset_batches",
            return_value=iter([entries[:2], entries[2:]]),
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._load_document",
            new_callable=AsyncMock,
            side_effect=lambda doc: doc,
        )

        result = await ingest_arxiv_dataset.fn(
            entry=_entry(max_samples=4), user_id=PydanticObjectId()
        )

        assert len(result) == 4

    async def test_calls_load_batch_once_per_chunk(self, mocker) -> None:
        """One ``load_batch`` task run per streamed chunk — not one per row."""

        entries = [_raw(f"2103.{i:05d}") for i in range(4)]
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._fetch_dataset_batches",
            return_value=iter([entries[:2], entries[2:]]),
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            new_callable=AsyncMock,
        )
        load_batch_spy = mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.load_batch",
            new_callable=AsyncMock,
            side_effect=lambda docs: docs,
        )

        await ingest_arxiv_dataset.fn(
            entry=_entry(max_samples=4), user_id=PydanticObjectId()
        )

        # Two streamed chunks ⇒ exactly two load_batch task runs.
        assert load_batch_spy.await_count == 2

    async def test_does_not_call_enrich_batch_when_fetch_content_false(
        self, mocker
    ) -> None:
        entries = [_raw(f"2103.{i:05d}") for i in range(2)]
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._fetch_dataset_batches",
            return_value=iter([entries]),
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.load_batch",
            new_callable=AsyncMock,
            side_effect=lambda docs: docs,
        )
        enrich_spy = mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.enrich_batch",
            new_callable=AsyncMock,
        )

        await ingest_arxiv_dataset.fn(
            entry=_entry(max_samples=2), user_id=PydanticObjectId()
        )

        enrich_spy.assert_not_awaited()

    async def test_calls_enrich_batch_once_per_chunk_when_fetch_content_true(
        self, mocker
    ) -> None:
        entries = [_raw(f"2103.{i:05d}") for i in range(2)]
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._fetch_dataset_batches",
            return_value=iter([entries]),
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            new_callable=AsyncMock,
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.load_batch",
            new_callable=AsyncMock,
            side_effect=lambda docs: docs,
        )
        enrich_spy = mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.enrich_batch",
            new_callable=AsyncMock,
            side_effect=lambda docs, concurrency: docs,
        )

        await ingest_arxiv_dataset.fn(
            entry=_entry(max_samples=2, fetch_content=True),
            user_id=PydanticObjectId(),
        )

        enrich_spy.assert_awaited_once()

    async def test_forwards_offset_to_fetch_dataset_batches(self, mocker) -> None:
        fetch_mock = mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._fetch_dataset_batches",
            return_value=iter([]),
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            new_callable=AsyncMock,
        )

        await ingest_arxiv_dataset.fn(
            entry=_entry(max_samples=250, offset=250), user_id=PydanticObjectId()
        )

        assert fetch_mock.call_args.kwargs["offset"] == 250

    async def test_defaults_offset_to_none(self, mocker) -> None:
        fetch_mock = mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._fetch_dataset_batches",
            return_value=iter([]),
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            new_callable=AsyncMock,
        )

        await ingest_arxiv_dataset.fn(
            entry=_entry(max_samples=4), user_id=PydanticObjectId()
        )

        assert fetch_mock.call_args.kwargs["offset"] is None

    async def test_takes_every_knob_from_the_dispatched_entry(self, mocker) -> None:
        """Regression: the flow must NOT re-read the source YAML for its config.

        It used to look its ``batch_size``/``concurrency``/``max_samples`` up via
        ``default_configured_sources()``, which reads ``sources/*.yaml`` at run
        time. In a Prefect Managed run that file is unreachable, so every arxiv
        worker died with ``FileNotFoundError`` — while holding a fully-populated
        entry the coordinator had already resolved.
        """

        # Arrange — entry values deliberately differ from the model defaults.
        fetch_mock = mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline._fetch_dataset_batches",
            return_value=iter([]),
        )
        mocker.patch(
            "tree.data.huggingface.arxiv_dataset_pipeline.init_mongodb",
            new_callable=AsyncMock,
        )

        # Act
        await ingest_arxiv_dataset.fn(
            entry=_entry(max_samples=250, batch_size=7, concurrency=3, offset=250),
            user_id=PydanticObjectId(),
        )

        # Assert — max_samples + batch_size come straight off the entry.
        assert fetch_mock.call_args.args == (250, 7)
