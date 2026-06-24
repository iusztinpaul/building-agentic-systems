"""Unit tests for the unified Substack pipeline (``ingest_substack_batch``).

The unified flow FLATTENS a shard's RSS feeds (expanded from feed-embedded content) and
single articles (scraped during flatten, wrapped in a synthetic feed-entry) into one
``[(Document, raw_entry)]`` list, then runs ONE isolated load gather over the SHARED
``load_document`` — so both kinds take the identical load/dedup path. Resolve and load
are mocked here; their internals are covered by ``test_substack_rss.py`` /
``test_substack_article.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

import tree.data.substack.substack_pipeline_batch as sub
from tree.config.app_config import SubstackArticleSource, SubstackRssSource

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


@pytest.fixture(autouse=True)
def _stub_mongo(mocker) -> None:
    mocker.patch(
        "tree.data.substack.substack_pipeline_batch.init_mongodb",
        new_callable=AsyncMock,
    )


async def test_flattens_feeds_and_articles_into_one_load(mocker) -> None:
    # 1 feed (2 items) + 2 single articles → ONE load gather, 4 load_document calls.
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
