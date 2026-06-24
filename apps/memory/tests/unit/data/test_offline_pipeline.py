"""Unit tests for the data worker's per-platform dispatch.

The worker (``data-etl-worker``) ingests one shard. It groups the shard's sources by
PLATFORM and hands each group (its RSS + single sources together) to one unified
per-platform pipeline — ``ingest_substack_batch`` / ``ingest_youtube_batch`` /
``ingest_web_batch`` — plus a per-entry HuggingFace dispatch. This suite exercises
``data_etl_worker`` directly with typed ``SourceEntry`` objects (which pass through
``_coerce_sources`` unchanged); a separate test covers the serialized-dict round-trip
the orchestrator actually dispatches.

The orchestrator fan-out (partition → dispatch → no-index) is covered in
``test_orchestrator_data.py``; the pure fan-out core in ``test_fanout_data.py``; each
unified platform pipeline's flatten+load in ``test_youtube_pipeline_batch.py`` /
``test_substack_pipeline_batch.py``.
"""

import dataclasses
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.config.app_config import (
    HuggingFaceDatasetSource,
    SourceEntry,
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
    YouTubeRssSource,
    YouTubeVideoSource,
)
from tree.data.offline_pipeline import _PLATFORM_PIPELINES, data_etl_worker

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


def _mock(mocker, name: str) -> AsyncMock:
    mock = mocker.patch(f"tree.data.offline_pipeline.{name}", new_callable=AsyncMock)
    mock.return_value = []
    return mock


class TestDataWorker:
    @pytest.fixture(autouse=True)
    def _stub_index_dim_check(self, mocker) -> None:
        """Skip the live-mongot-vs-settings dim check in unit tests (no mongot)."""

        mocker.patch(
            "tree.data.offline_pipeline.assert_settings_match_live_vector_index",
            new_callable=AsyncMock,
        )

    @pytest.fixture(autouse=True)
    def _stub_init_mongodb(self, mocker) -> AsyncMock:
        return mocker.patch(
            "tree.data.offline_pipeline.init_mongodb", new_callable=AsyncMock
        )

    @pytest.fixture
    def _platforms(self, mocker) -> dict[str, AsyncMock]:
        """Mock every platform flow + the HF connector; default each to []."""

        # Platform flows are direct refs in _PLATFORM_PIPELINES, so swap each entry's
        # batch_fn for a mock; the HF connector is still a patchable module global.
        labels = {"Substack": "substack", "YouTube": "youtube", "Web": "web"}
        mocks = {key: AsyncMock(return_value=[]) for key in labels.values()}
        mocker.patch(
            "tree.data.offline_pipeline._PLATFORM_PIPELINES",
            [
                dataclasses.replace(p, batch_fn=mocks[labels[p.label]])
                for p in _PLATFORM_PIPELINES
            ],
        )
        return {**mocks, "arxiv": _mock(mocker, "ingest_arxiv_dataset")}

    async def test_dispatches_each_platform_with_its_typed_entries(
        self, _platforms
    ) -> None:
        substack = [
            SubstackRssSource(uri="https://example.com/feed"),
            SubstackArticleSource(uri="https://example.com/p/article"),
        ]
        youtube = [
            YouTubeRssSource(
                uri="https://www.youtube.com/feeds/videos.xml?channel_id=UC1"
            ),
            YouTubeVideoSource(uri="https://youtu.be/eYaWxljC4sA"),
        ]
        web = [WebSource(uri="https://martinfowler.com/articles/microservices.html")]
        hf = HuggingFaceDatasetSource(
            uri="librarian-bots/arxiv-metadata-snapshot", max_samples=5
        )

        doc_s, doc_y, doc_w, doc_h = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        _platforms["substack"].return_value = [doc_s]
        _platforms["youtube"].return_value = [doc_y]
        _platforms["web"].return_value = [doc_w]
        _platforms["arxiv"].return_value = [doc_h]

        sources: list[SourceEntry] = [*substack, *youtube, *web, hf]
        result = await data_etl_worker(_USER_ID, sources)

        assert {id(d) for d in result} == {id(doc_s), id(doc_y), id(doc_w), id(doc_h)}
        # Each platform flow gets the SHARD'S TYPED ENTRIES for that platform (both
        # RSS + single kinds together), not bare URIs.
        _platforms["substack"].assert_awaited_once_with(substack, _USER_ID)
        _platforms["youtube"].assert_awaited_once_with(youtube, _USER_ID)
        _platforms["web"].assert_awaited_once_with(web, _USER_ID)
        _platforms["arxiv"].assert_awaited_once_with(
            user_id=_USER_ID, max_samples=5, fetch_content=False, offset=None
        )

    async def test_groups_both_substack_kinds_into_one_call(self, _platforms) -> None:
        # RSS + article entries go to ONE ingest_substack_batch call together.
        substack = [
            SubstackRssSource(uri="https://a.example/feed"),
            SubstackRssSource(uri="https://b.example/feed"),
            SubstackArticleSource(uri="https://a.example/p/post"),
        ]
        await data_etl_worker(_USER_ID, list(substack))

        _platforms["substack"].assert_awaited_once_with(substack, _USER_ID)

    async def test_groups_both_youtube_kinds_into_one_call(self, _platforms) -> None:
        youtube = [
            YouTubeRssSource(
                uri="https://www.youtube.com/feeds/videos.xml?channel_id=UC1"
            ),
            YouTubeVideoSource(uri="https://youtu.be/eYaWxljC4sA"),
        ]
        await data_etl_worker(_USER_ID, list(youtube))

        _platforms["youtube"].assert_awaited_once_with(youtube, _USER_ID)

    @pytest.mark.parametrize("platform_key", ["substack", "youtube", "web"])
    async def test_skips_platform_when_absent(
        self, _platforms, caplog, platform_key
    ) -> None:
        label = {"substack": "Substack", "youtube": "YouTube", "web": "Web"}[
            platform_key
        ]
        # A shard with only a HuggingFace source: every URL platform is skipped.
        sources: list[SourceEntry] = [
            HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot")
        ]

        with caplog.at_level(logging.INFO, logger="tree.data.offline_pipeline"):
            await data_etl_worker(_USER_ID, sources)

        _platforms[platform_key].assert_not_awaited()
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            f"{label} pipeline skipped: no entries configured" in m for m in messages
        )

    async def test_skips_arxiv_when_no_huggingface_dataset_entries(
        self, _platforms
    ) -> None:
        await data_etl_worker(
            _USER_ID, [SubstackRssSource(uri="https://example.com/feed")]
        )

        _platforms["arxiv"].assert_not_awaited()

    async def test_initializes_mongodb(self, _platforms, _stub_init_mongodb) -> None:
        await data_etl_worker(_USER_ID, [])

        _stub_init_mongodb.assert_awaited_once()

    async def test_web_is_dispatched_after_youtube(self, _platforms, mocker) -> None:
        # Web is the LAST platform: its call must be awaited AFTER YouTube's.
        manager = mocker.MagicMock()
        manager.attach_mock(_platforms["youtube"], "youtube")
        manager.attach_mock(_platforms["web"], "web")

        sources: list[SourceEntry] = [
            WebSource(uri="https://martinfowler.com/articles/microservices.html"),
            YouTubeVideoSource(uri="https://youtu.be/eYaWxljC4sA"),
        ]
        await data_etl_worker(_USER_ID, sources)

        assert [c[0] for c in manager.mock_calls] == ["youtube", "web"]

    async def test_returns_platform_docs_without_double_filtering(
        self, _platforms
    ) -> None:
        # Each platform flow already filters None and returns list[Document]; the
        # worker just extends — it must NOT re-filter or transform.
        kept = MagicMock()
        _platforms["web"].return_value = [kept]

        result = await data_etl_worker(
            _USER_ID,
            [
                WebSource(uri="https://dup.example/post"),
                WebSource(uri="https://new.example/post"),
            ],
        )

        assert result == [kept]

    async def test_passes_huggingface_dataset_overrides(self, _platforms) -> None:
        await data_etl_worker(
            _USER_ID,
            [
                HuggingFaceDatasetSource(
                    uri="librarian-bots/arxiv-metadata-snapshot",
                    max_samples=42,
                    fetch_content=True,
                )
            ],
        )

        _platforms["arxiv"].assert_awaited_once_with(
            user_id=_USER_ID, max_samples=42, fetch_content=True, offset=None
        )

    async def test_forwards_huggingface_dataset_offset_window(self, _platforms) -> None:
        await data_etl_worker(
            _USER_ID,
            [
                HuggingFaceDatasetSource(
                    uri="librarian-bots/arxiv-metadata-snapshot",
                    max_samples=250,
                    offset=250,
                )
            ],
        )

        _platforms["arxiv"].assert_awaited_once_with(
            user_id=_USER_ID, max_samples=250, fetch_content=False, offset=250
        )

    async def test_raises_for_unknown_huggingface_dataset_id(self, _platforms) -> None:
        with pytest.raises(ValueError, match="someone/unregistered-dataset"):
            await data_etl_worker(
                _USER_ID, [HuggingFaceDatasetSource(uri="someone/unregistered-dataset")]
            )

    async def test_reconstructs_sources_from_serialized_dicts(self, _platforms) -> None:
        """A shard arrives serialized (``list[dict]``) and is re-parsed to typed
        ``SourceEntry`` objects before grouping — the round-trip the orchestrator
        actually dispatches.
        """

        serialized = [
            SubstackRssSource(uri="https://example.com/feed").model_dump(),
            HuggingFaceDatasetSource(
                uri="librarian-bots/arxiv-metadata-snapshot",
                max_samples=7,
                fetch_content=True,
            ).model_dump(),
        ]

        await data_etl_worker(_USER_ID, serialized)

        # Re-parsed back to typed entries before dispatch.
        _platforms["substack"].assert_awaited_once_with(
            [SubstackRssSource(uri="https://example.com/feed")], _USER_ID
        )
        _platforms["arxiv"].assert_awaited_once_with(
            user_id=_USER_ID, max_samples=7, fetch_content=True, offset=None
        )
