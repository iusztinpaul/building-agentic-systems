"""Unit tests for the unified Substack pipeline (``ingest_substack_batch``).

The unified flow resolves a shard's RSS feeds up front (expanded from feed-embedded
content), appends the single-article URLs, then walks that work list in
``app_config.data.batch_size`` chunks: per chunk it scrapes ONLY the chunk's article URLs
(wrapped in a synthetic feed-entry) and loads the chunk through the SHARED
``load_document`` — so both kinds take the identical load/dedup path. Resolve and load
are mocked here; their internals are covered by ``test_substack.py``.

The article scrape and the load are wrapped in the batch-grain tasks
``extract-substack-article-batch`` / ``load-substack-batch`` (ADR-002 amendment #096), so
both carry retries like web's. The tasks run once per chunk — never per row.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

import tree.data.substack.substack_pipeline_batch as sub
from tree.config.app_config import DataConfig, app_config
from tree.config.sources import (
    SubstackArticleSource,
    SubstackRssSource,
)
from tree.data.batch import track_item_failures

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


class TestBatchTaskMetadata:
    """Retry grain per ADR-002 amendment #096: both batch tasks are Tier F (3 x 5 s)."""

    def test_extract_article_batch_is_a_task_with_tier_f_retries(self) -> None:
        assert sub.extract_article_batch.name == "extract-substack-article-batch"
        # Tier F: substack articles are fetched with plain httpx — replay is free.
        assert sub.extract_article_batch.retries == 3
        assert sub.extract_article_batch.retry_delay_seconds == 5

    def test_load_batch_is_a_task_with_tier_f_retries(self) -> None:
        assert sub.load_batch.name == "load-substack-batch"
        # Tier F: idempotent Mongo write, 15 s outlasts a primary election.
        assert sub.load_batch.retries == 3
        assert sub.load_batch.retry_delay_seconds == 5

    def test_feed_fetch_task_retries(self) -> None:
        assert sub.fetch_rss_feed.retries == 3
        assert sub.fetch_rss_feed.retry_delay_seconds == 5

    def test_flow_does_not_stack_retries_on_its_tasks(self) -> None:
        # Rule 5: a flow whose tasks retry must not itself retry — attempts multiply.
        assert not sub.ingest_substack_batch.retries


@pytest.fixture(autouse=True)
def _stub_mongo(mocker) -> None:
    mocker.patch(
        "tree.data.substack.substack_pipeline_batch.init_mongodb",
        new_callable=AsyncMock,
    )


async def test_flattens_feeds_and_articles_into_one_load(mocker) -> None:
    # 1 feed (2 items) + 2 single articles fit one default-size chunk → 4 loads.
    feed_items = [
        (MagicMock(), {"content": [{"value": "f1"}]}),
        (MagicMock(), {"content": [{"value": "f2"}]}),
    ]

    async def fake_resolve_feed(feed_url, user_id):
        assert user_id == _USER_ID
        return feed_items

    async def fake_resolve_article(url, user_id):
        return MagicMock(), {"content": [{"value": f"scraped:{url}"}]}

    mocker.patch.object(
        sub, "_resolve_feed", new_callable=AsyncMock, side_effect=fake_resolve_feed
    )
    mocker.patch.object(
        sub,
        "_resolve_article",
        new_callable=AsyncMock,
        side_effect=fake_resolve_article,
    )
    load = mocker.patch.object(
        sub, "load_document", new_callable=AsyncMock, side_effect=lambda doc, entry: doc
    )

    entries = [
        SubstackRssSource(uri="feed://A"),
        SubstackArticleSource(uri="https://a.example/p/1"),
        SubstackArticleSource(uri="https://a.example/p/2"),
    ]
    result = await sub.ingest_substack_batch.fn(entries, _USER_ID)

    assert load.await_count == 4  # 2 feed items + 2 articles, one load each
    assert len(result) == 4
    # Article items reach load as a SYNTHETIC feed-entry (so reference extraction in
    # load_document works identically to the RSS path).
    article_entries = [
        call.args[1] for call in load.await_args_list if "scraped:" in str(call.args[1])
    ]
    assert len(article_entries) == 2


async def test_isolates_a_failing_feed_and_drops_duplicates(mocker) -> None:
    async def fake_resolve_feed(feed_url, user_id):
        if feed_url == "feed://bad":
            raise RuntimeError("feed fetch failed")
        return [(MagicMock(), {"content": [{"value": "ok"}]})]

    mocker.patch.object(
        sub, "_resolve_feed", new_callable=AsyncMock, side_effect=fake_resolve_feed
    )
    # load_document returns None for the surviving item → duplicate, dropped from result.
    load = mocker.patch.object(
        sub, "load_document", new_callable=AsyncMock, return_value=None
    )

    entries = [
        SubstackRssSource(uri="feed://bad"),
        SubstackRssSource(uri="feed://good"),
    ]
    result = await sub.ingest_substack_batch.fn(entries, _USER_ID)

    # Bad feed isolated → only the good feed's 1 item reached load; it deduped → [].
    assert load.await_count == 1
    assert result == []


async def test_no_items_skips_load(mocker) -> None:
    mocker.patch.object(sub, "_resolve_feed", new_callable=AsyncMock, return_value=[])
    load = mocker.patch.object(sub, "load_document", new_callable=AsyncMock)

    result = await sub.ingest_substack_batch.fn(
        [SubstackRssSource(uri="feed://A")], _USER_ID
    )

    assert result == []
    load.assert_not_awaited()


# --- Chunked streaming: the work list runs in ``data.batch_size`` chunks ---


def _article_urls(count: int) -> list[str]:
    return [f"https://a.example/p/{i}" for i in range(count)]


def _scraped_item(url: str) -> tuple[MagicMock, dict]:
    return MagicMock(name=url), {"content": [{"value": f"scraped:{url}"}]}


@pytest.fixture
def _batch_size_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "data", DataConfig(batch_size=2))


@pytest.fixture
def _spy_tasks(mocker) -> tuple[AsyncMock, AsyncMock]:
    """Patch both batch tasks: extract scrapes deterministically, load echoes docs."""

    scraped = {url: _scraped_item(url) for url in _article_urls(5)}
    extract = mocker.patch.object(
        sub,
        "extract_article_batch",
        new_callable=AsyncMock,
        side_effect=lambda urls, _uid: [scraped[url] for url in urls],
    )
    load = mocker.patch.object(
        sub,
        "load_batch",
        new_callable=AsyncMock,
        side_effect=lambda items: [doc for doc, _entry in items],
    )
    return extract, load


@pytest.mark.usefixtures("_batch_size_two")
async def test_articles_are_scraped_and_loaded_once_per_chunk(_spy_tasks) -> None:
    extract, load = _spy_tasks
    urls = _article_urls(5)

    await sub.ingest_substack_batch.fn(
        [SubstackArticleSource(uri=url) for url in urls], _USER_ID
    )

    assert [call.args[0] for call in extract.await_args_list] == [
        urls[0:2],
        urls[2:4],
        urls[4:5],
    ]
    assert [len(call.args[0]) for call in load.await_args_list] == [2, 2, 1]


async def test_chunked_result_equals_the_single_chunk_result(
    mocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed_items = [
        (MagicMock(name=f"f{i}"), {"content": [{"value": "f"}]}) for i in (1, 2, 3)
    ]
    mocker.patch.object(
        sub, "_resolve_feed", new_callable=AsyncMock, return_value=feed_items
    )
    scraped = {url: _scraped_item(url) for url in _article_urls(2)}
    mocker.patch.object(
        sub,
        "_resolve_article",
        new_callable=AsyncMock,
        side_effect=lambda url, _uid: scraped[url],
    )
    mocker.patch.object(
        sub, "load_document", new_callable=AsyncMock, side_effect=lambda doc, _e: doc
    )
    entries = [
        SubstackRssSource(uri="feed://A"),
        *(SubstackArticleSource(uri=url) for url in _article_urls(2)),
    ]

    monkeypatch.setattr(app_config, "data", DataConfig(batch_size=2))
    chunked = await sub.ingest_substack_batch.fn(entries, _USER_ID)
    monkeypatch.setattr(app_config, "data", DataConfig(batch_size=500))
    single = await sub.ingest_substack_batch.fn(entries, _USER_ID)

    assert len(chunked) == 5
    assert chunked == single


@pytest.mark.usefixtures("_batch_size_two")
async def test_rss_items_are_never_scraped(mocker, _spy_tasks) -> None:
    # Work list [f1, f2 | f3, a0 | a1]: chunk 1 is RSS-only → no scrape call;
    # chunks 2-3 scrape ONLY their article URLs, never a feed item.
    extract, load = _spy_tasks
    feed_items = [
        (MagicMock(name=f"f{i}"), {"content": [{"value": "f"}]}) for i in (1, 2, 3)
    ]
    resolve_feed = mocker.patch.object(
        sub, "_resolve_feed", new_callable=AsyncMock, return_value=feed_items
    )
    urls = _article_urls(2)

    await sub.ingest_substack_batch.fn(
        [
            SubstackRssSource(uri="feed://A"),
            *(SubstackArticleSource(uri=u) for u in urls),
        ],
        _USER_ID,
    )

    resolve_feed.assert_awaited_once()
    assert [call.args[0] for call in extract.await_args_list] == [[urls[0]], [urls[1]]]
    assert [call.args[0] for call in load.await_args_list][0] == feed_items[0:2]
    assert load.await_count == 3


@pytest.mark.usefixtures("_batch_size_two")
async def test_an_rss_only_shard_makes_no_scrape_call(mocker, _spy_tasks) -> None:
    extract, load = _spy_tasks
    feed_items = [(MagicMock(), {"content": [{"value": "f"}]}) for _ in range(3)]
    mocker.patch.object(
        sub, "_resolve_feed", new_callable=AsyncMock, return_value=feed_items
    )

    await sub.ingest_substack_batch.fn([SubstackRssSource(uri="feed://A")], _USER_ID)

    extract.assert_not_awaited()
    assert [len(call.args[0]) for call in load.await_args_list] == [2, 1]


@pytest.mark.usefixtures("_batch_size_two")
async def test_a_failure_in_one_chunk_does_not_stop_the_next(mocker) -> None:
    # Chunks [a0, a1 | a2, a3 | a4]: a0 fails in chunk 1, a2 fails in chunk 2.
    urls = _article_urls(5)
    failing = {urls[0], urls[2]}

    async def fake_resolve_article(url: str, _uid: PydanticObjectId) -> tuple:
        if url in failing:
            raise RuntimeError(f"scrape failed: {url}")
        return _scraped_item(url)

    mocker.patch.object(
        sub,
        "_resolve_article",
        new_callable=AsyncMock,
        side_effect=fake_resolve_article,
    )
    load = mocker.patch.object(
        sub, "load_document", new_callable=AsyncMock, side_effect=lambda doc, _e: doc
    )

    with track_item_failures() as tally:
        result = await sub.ingest_substack_batch.fn(
            [SubstackArticleSource(uri=url) for url in urls], _USER_ID
        )

    assert len(result) == 3
    assert load.await_count == 3
    assert tally.count == 2
