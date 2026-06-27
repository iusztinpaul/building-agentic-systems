"""Regression: the URL dispatcher works after the shared-loader migration.

The dispatcher derives its custom-Substack-domain registry from the shared
source loader: it now walks ``default_configured_sources()`` (the committed
``sources/backfill.yaml`` + ``sources/listen.yaml`` set) and picks up the host
of every entry typed ``substack_rss`` / ``substack_article``.

This module exercises the *real* committed source files against the dispatcher
to guard against two regressions:

1. A URL on a custom Substack domain (e.g. ``decodingai.com``) — known to
   the registry through both RSS (listen) and article (backfill) entries —
   must still route to the Substack article handler.
2. A URL on a non-Substack domain (e.g. ``news.ycombinator.com``) must
   still fall back to the generic web (Bright Data) pipeline.

Both underlying handlers are mocked so this test never touches the network.
"""

from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId

from tree.config.sources import default_configured_sources
from tree.data import online_pipeline as ingest_module
from tree.data.online_pipeline import _get_configured_substack_domains, _ingest_url
from tree.entities.documents import Document, SourceType


@pytest.fixture(autouse=True)
def _reset_substack_domain_cache() -> None:
    """The custom-domain registry is ``functools.cache``-d. Reset it so each
    test starts from a fresh derivation against the current ``app_config``.
    """

    _get_configured_substack_domains.cache_clear()
    yield
    _get_configured_substack_domains.cache_clear()


class TestDispatcherAgainstMigratedDefaultConfig:
    async def test_decodingai_post_routes_to_substack_article_handler(
        self, mocker
    ) -> None:
        """A fresh decodingai.com article URL (not present in the source files)
        must still route to the Substack article handler thanks to the
        custom-Substack-domain registry derived from the shared source loader.
        """

        # Sanity-check: the shared source loader still surfaces
        # ``decodingai.com`` as a custom Substack domain.
        domains = _get_configured_substack_domains()
        assert "decodingai.com" in domains, (
            "Expected decodingai.com to be derived from the shared source "
            f"loader; got: {sorted(domains)}"
        )
        # Spot check: the loaded sources list is non-empty and contains
        # at least one Substack-typed entry on decodingai.com.
        assert any(
            getattr(s, "uri", "").startswith("https://www.decodingai.com")
            for s in default_configured_sources()
        ), "source files lost their decodingai.com entries"

        substack_doc = Document(
            source_type=SourceType.SUBSTACK,
            source_uri="https://decodingai.com/p/some-fresh-post",
            user_id=PydanticObjectId(),
            title="Fresh post",
            content="Article body.",
        )
        substack_handler = mocker.patch.object(
            ingest_module,
            "_ingest_substack_article",
            new=AsyncMock(return_value=substack_doc),
        )
        web_handler = mocker.patch.object(
            ingest_module,
            "_ingest_web_url",
            new=AsyncMock(return_value=None),
        )

        user_id = PydanticObjectId()
        result = await _ingest_url("https://decodingai.com/p/some-fresh-post", user_id)

        substack_handler.assert_awaited_once_with(
            "https://decodingai.com/p/some-fresh-post", user_id
        )
        web_handler.assert_not_awaited()
        assert result is substack_doc
        assert result.source_type == SourceType.SUBSTACK

    async def test_unknown_domain_falls_back_to_web_handler(self, mocker) -> None:
        """A non-Substack URL (and not on any configured Substack domain)
        must fall through to the generic web pipeline.
        """

        web_doc = Document(
            source_type=SourceType.WEB,
            source_uri="https://news.ycombinator.com/item?id=1",
            user_id=PydanticObjectId(),
            title="HN item",
            content="HN content.",
        )
        web_handler = mocker.patch.object(
            ingest_module,
            "_ingest_web_url",
            new=AsyncMock(return_value=web_doc),
        )
        substack_handler = mocker.patch.object(
            ingest_module,
            "_ingest_substack_article",
            new=AsyncMock(return_value=None),
        )

        user_id = PydanticObjectId()
        result = await _ingest_url("https://news.ycombinator.com/item?id=1", user_id)

        web_handler.assert_awaited_once_with(
            "https://news.ycombinator.com/item?id=1", user_id
        )
        substack_handler.assert_not_awaited()
        assert result is web_doc
        assert result.source_type == SourceType.WEB
