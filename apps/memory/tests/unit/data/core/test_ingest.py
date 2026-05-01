"""Unit tests for tree.data.core.ingest — URL dispatcher."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from tree.config.app_config import (
    HuggingFaceDatasetSource,
    SourcesConfig,
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
)
from tree.data.core.ingest import (
    _get_configured_substack_domains,
    ingest_url,
)


@pytest.fixture(autouse=True)
def _clear_substack_domain_cache() -> None:
    """Reset the cached helper between tests so mocked configs are observed."""

    _get_configured_substack_domains.cache_clear()
    yield
    _get_configured_substack_domains.cache_clear()


def _patch_sources(mocker, entries: list) -> None:
    """Replace ``app_config.sources`` with a real ``SourcesConfig``."""

    mock_config = MagicMock()
    mock_config.sources = SourcesConfig(sources=entries)
    mocker.patch("tree.data.core.ingest.app_config", mock_config)


class TestGetConfiguredSubstackDomains:
    def test_extracts_domains_from_substack_rss_entries(self, mocker) -> None:
        _patch_sources(
            mocker,
            [
                SubstackRssSource(uri="https://www.decodingai.com/feed"),
                SubstackRssSource(uri="https://newsletter.example.com/feed"),
            ],
        )

        domains = _get_configured_substack_domains()

        assert "decodingai.com" in domains
        assert "newsletter.example.com" in domains

    def test_extracts_domains_from_substack_article_entries(self, mocker) -> None:
        _patch_sources(
            mocker,
            [
                SubstackArticleSource(uri="https://www.custom.blog/p/my-post"),
            ],
        )

        domains = _get_configured_substack_domains()

        assert "custom.blog" in domains

    def test_strips_www_prefix(self, mocker) -> None:
        _patch_sources(
            mocker,
            [SubstackRssSource(uri="https://www.example.com/feed")],
        )

        domains = _get_configured_substack_domains()

        assert "example.com" in domains
        assert "www.example.com" not in domains

    def test_deduplicates_domains_across_rss_and_article(self, mocker) -> None:
        _patch_sources(
            mocker,
            [
                SubstackRssSource(uri="https://decodingai.com/feed"),
                SubstackRssSource(uri="https://www.decodingai.com/feed"),
                SubstackArticleSource(uri="https://decodingai.com/p/article"),
            ],
        )

        domains = _get_configured_substack_domains()

        assert len([d for d in domains if "decodingai" in d]) == 1

    def test_excludes_web_source_entries(self, mocker) -> None:
        """A WebSource on a Substack-looking domain must NOT be registered."""

        _patch_sources(
            mocker,
            [
                SubstackRssSource(uri="https://decodingai.com/feed"),
                WebSource(uri="https://anthropic.com/some-article"),
                # Even a WebSource whose host *looks* like a custom Substack
                # blog must be excluded — the type discriminates, not the URL.
                WebSource(uri="https://web-only.blog/post"),
            ],
        )

        domains = _get_configured_substack_domains()

        assert domains == {"decodingai.com"}
        assert "anthropic.com" not in domains
        assert "web-only.blog" not in domains

    def test_ignores_huggingface_dataset_entries(self, mocker) -> None:
        """A HuggingFace dataset id has no host — must not crash or be added."""

        _patch_sources(
            mocker,
            [
                HuggingFaceDatasetSource(uri="arxiv-community/arxiv_dataset"),
                SubstackRssSource(uri="https://decodingai.com/feed"),
            ],
        )

        domains = _get_configured_substack_domains()

        assert domains == {"decodingai.com"}
        assert "arxiv-community/arxiv_dataset" not in domains

    def test_empty_sources_returns_empty_set(self, mocker) -> None:
        _patch_sources(mocker, [])

        domains = _get_configured_substack_domains()

        assert domains == set()


class TestIngestUrl:
    async def test_routes_substack_url(self, mocker) -> None:
        mock_handler = AsyncMock(return_value=MagicMock())
        mocker.patch(
            "tree.data.core.ingest._URL_HANDLERS",
            [("substack.com", mock_handler)],
        )

        await ingest_url("https://newsletter.substack.com/p/article")

        mock_handler.assert_awaited_once_with(
            "https://newsletter.substack.com/p/article"
        )

    async def test_routes_custom_substack_domain(self, mocker) -> None:
        mock_substack = AsyncMock(return_value=MagicMock())
        mock_fallback = AsyncMock(return_value=MagicMock())
        mocker.patch("tree.data.core.ingest._URL_HANDLERS", [])
        mocker.patch(
            "tree.data.core.ingest._get_configured_substack_domains",
            return_value={"decodingai.com"},
        )
        mocker.patch(
            "tree.data.core.ingest._ingest_substack_article",
            mock_substack,
        )
        mocker.patch(
            "tree.data.core.ingest._ingest_web_url",
            mock_fallback,
        )

        await ingest_url("https://decodingai.com/p/my-article")

        mock_substack.assert_awaited_once_with("https://decodingai.com/p/my-article")
        mock_fallback.assert_not_awaited()

    async def test_static_registry_takes_precedence(self, mocker) -> None:
        static_handler = AsyncMock(return_value=MagicMock())
        mocker.patch(
            "tree.data.core.ingest._URL_HANDLERS",
            [("example.com", static_handler)],
        )
        mocker.patch(
            "tree.data.core.ingest._get_configured_substack_domains",
            return_value={"example.com"},
        )

        await ingest_url("https://example.com/p/article")

        static_handler.assert_awaited_once()

    async def test_falls_through_to_web_for_unmatched_http_url(self, mocker) -> None:
        mock_fallback = AsyncMock(return_value=MagicMock())
        mocker.patch("tree.data.core.ingest._URL_HANDLERS", [])
        mocker.patch(
            "tree.data.core.ingest._get_configured_substack_domains",
            return_value=set(),
        )
        mocker.patch(
            "tree.data.core.ingest._ingest_web_url",
            mock_fallback,
        )

        await ingest_url("https://martinfowler.com/articles/microservices.html")

        mock_fallback.assert_awaited_once_with(
            "https://martinfowler.com/articles/microservices.html"
        )

    async def test_falls_through_to_web_for_github_url(self, mocker) -> None:
        mock_fallback = AsyncMock(return_value=MagicMock())
        mocker.patch("tree.data.core.ingest._URL_HANDLERS", [])
        mocker.patch(
            "tree.data.core.ingest._get_configured_substack_domains",
            return_value=set(),
        )
        mocker.patch(
            "tree.data.core.ingest._ingest_web_url",
            mock_fallback,
        )

        await ingest_url("https://github.com/anthropics/claude-code")

        mock_fallback.assert_awaited_once_with(
            "https://github.com/anthropics/claude-code"
        )

    async def test_fallback_emits_info_log(self, mocker, caplog) -> None:
        mock_fallback = AsyncMock(return_value=MagicMock())
        mocker.patch("tree.data.core.ingest._URL_HANDLERS", [])
        mocker.patch(
            "tree.data.core.ingest._get_configured_substack_domains",
            return_value=set(),
        )
        mocker.patch(
            "tree.data.core.ingest._ingest_web_url",
            mock_fallback,
        )

        url = "https://martinfowler.com/articles/microservices.html"
        with caplog.at_level(logging.INFO, logger="tree.data.core.ingest"):
            await ingest_url(url)

        expected = f"Routing URL to 'web (Bright Data fallback)' pipeline: {url}"
        assert any(expected in record.getMessage() for record in caplog.records)

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/data.tar",
            "file:///tmp/x",
            "",
        ],
        ids=["ftp", "file", "empty"],
    )
    async def test_rejects_unsupported_scheme(self, mocker, url: str) -> None:
        mock_fallback = AsyncMock()
        mock_substack = AsyncMock()
        mocker.patch(
            "tree.data.core.ingest._ingest_web_url",
            mock_fallback,
        )
        mocker.patch(
            "tree.data.core.ingest._ingest_substack_article",
            mock_substack,
        )

        with pytest.raises(ValueError, match="scheme"):
            await ingest_url(url)

        mock_fallback.assert_not_awaited()
        mock_substack.assert_not_awaited()

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=eYaWxljC4sA",
            "https://youtube.com/watch?v=eYaWxljC4sA",
            "https://m.youtube.com/watch?v=eYaWxljC4sA",
            "https://youtu.be/eYaWxljC4sA",
        ],
    )
    async def test_routes_youtube_video_url(self, mocker, url: str) -> None:
        mock_youtube = AsyncMock(return_value=MagicMock())
        mock_substack = AsyncMock()
        mock_fallback = AsyncMock()
        # The static registry captures handler references at module load,
        # so we replace it wholesale to inject the mocked YouTube handler.
        mocker.patch(
            "tree.data.core.ingest._URL_HANDLERS",
            [
                ("youtube.com", mock_youtube),
                ("youtu.be", mock_youtube),
                ("substack.com", mock_substack),
            ],
        )
        mocker.patch(
            "tree.data.core.ingest._ingest_web_url",
            mock_fallback,
        )

        await ingest_url(url)

        mock_youtube.assert_awaited_once_with(url)
        mock_substack.assert_not_awaited()
        mock_fallback.assert_not_awaited()

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/feeds/videos.xml?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw",
            "https://youtube.com/feeds/videos.xml?channel_id=UC1",
            "https://m.youtube.com/feeds/videos.xml?channel_id=UC2",
        ],
    )
    async def test_rejects_youtube_rss_feed_url(self, mocker, url: str) -> None:
        mock_youtube = AsyncMock()
        mock_fallback = AsyncMock()
        mocker.patch(
            "tree.data.core.ingest._ingest_youtube_video",
            mock_youtube,
        )
        mocker.patch(
            "tree.data.core.ingest._ingest_web_url",
            mock_fallback,
        )

        with pytest.raises(ValueError, match="youtube_rss"):
            await ingest_url(url)

        mock_youtube.assert_not_awaited()
        mock_fallback.assert_not_awaited()

    @pytest.mark.parametrize(
        "url",
        [
            "https://",
            "http://",
        ],
        ids=["https-no-host", "http-no-host"],
    )
    async def test_rejects_missing_host(self, mocker, url: str) -> None:
        mock_fallback = AsyncMock()
        mock_substack = AsyncMock()
        mocker.patch(
            "tree.data.core.ingest._ingest_web_url",
            mock_fallback,
        )
        mocker.patch(
            "tree.data.core.ingest._ingest_substack_article",
            mock_substack,
        )

        with pytest.raises(ValueError, match="missing a host"):
            await ingest_url(url)

        mock_fallback.assert_not_awaited()
        mock_substack.assert_not_awaited()
