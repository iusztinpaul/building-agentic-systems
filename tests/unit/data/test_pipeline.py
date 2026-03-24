from unittest.mock import AsyncMock, MagicMock

import pytest

from twin.data.pipeline import ingest_all_data


@pytest.fixture()
def mock_init_mongodb(mocker) -> MagicMock:
    return mocker.patch("twin.data.pipeline.init_mongodb", new_callable=AsyncMock)


@pytest.fixture()
def mock_substack_batch(mocker) -> AsyncMock:
    mock = mocker.patch(
        "twin.data.pipeline.ingest_substack_rss_feed_batch", new_callable=AsyncMock
    )
    mock.return_value = []
    return mock


@pytest.fixture()
def mock_arxiv(mocker) -> AsyncMock:
    mock = mocker.patch(
        "twin.data.pipeline.ingest_arxiv_dataset", new_callable=AsyncMock
    )
    mock.return_value = []
    return mock


def _make_config(
    mocker,
    *,
    substack_enabled: bool = True,
    substack_feeds: list[str] | None = None,
    arxiv_enabled: bool = True,
    arxiv_max_samples: int = 10,
) -> MagicMock:
    mock_config = MagicMock()
    mock_config.data_pipeline.substack.enabled = substack_enabled
    mock_config.data_pipeline.substack.feeds = substack_feeds or []
    mock_config.data_pipeline.huggingface_arxiv_dataset.enabled = arxiv_enabled
    mock_config.data_pipeline.huggingface_arxiv_dataset.max_samples = arxiv_max_samples
    mocker.patch("twin.data.pipeline.app_config", mock_config)
    return mock_config


class TestIngestAllData:
    async def test_runs_both_pipelines_when_enabled(
        self, mocker, mock_init_mongodb, mock_substack_batch, mock_arxiv
    ) -> None:
        _make_config(
            mocker,
            substack_feeds=["https://example.com/feed"],
        )
        doc_a = MagicMock()
        doc_b = MagicMock()
        mock_substack_batch.return_value = [doc_a]
        mock_arxiv.return_value = [doc_b]

        result = await ingest_all_data()

        assert len(result) == 2
        mock_substack_batch.assert_awaited_once_with(["https://example.com/feed"])
        mock_arxiv.assert_awaited_once()

    async def test_skips_substack_when_disabled(
        self, mocker, mock_init_mongodb, mock_substack_batch, mock_arxiv
    ) -> None:
        _make_config(mocker, substack_enabled=False)

        await ingest_all_data()

        mock_substack_batch.assert_not_awaited()
        mock_arxiv.assert_awaited_once()

    async def test_skips_substack_when_no_feeds(
        self, mocker, mock_init_mongodb, mock_substack_batch, mock_arxiv
    ) -> None:
        _make_config(mocker, substack_feeds=[])

        await ingest_all_data()

        mock_substack_batch.assert_not_awaited()
        mock_arxiv.assert_awaited_once()

    async def test_skips_arxiv_when_disabled(
        self, mocker, mock_init_mongodb, mock_substack_batch, mock_arxiv
    ) -> None:
        _make_config(
            mocker,
            arxiv_enabled=False,
            substack_feeds=["https://example.com/feed"],
        )

        await ingest_all_data()

        mock_substack_batch.assert_awaited_once()
        mock_arxiv.assert_not_awaited()

    async def test_skips_all_when_disabled(
        self, mocker, mock_init_mongodb, mock_substack_batch, mock_arxiv
    ) -> None:
        _make_config(mocker, substack_enabled=False, arxiv_enabled=False)

        result = await ingest_all_data()

        assert result == []
        mock_substack_batch.assert_not_awaited()
        mock_arxiv.assert_not_awaited()

    async def test_initializes_mongodb(
        self, mocker, mock_init_mongodb, mock_substack_batch, mock_arxiv
    ) -> None:
        _make_config(mocker, substack_enabled=False, arxiv_enabled=False)

        await ingest_all_data()

        mock_init_mongodb.assert_awaited_once()
