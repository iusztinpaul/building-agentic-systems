"""Unit tests for tree.data.core.ingest — URL dispatcher."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from tree.data.core.ingest import (
    _get_configured_substack_domains,
    ingest_url,
)


class TestGetConfiguredSubstackDomains:
    def test_extracts_domains_from_feeds(self, mocker) -> None:
        mock_config = MagicMock()
        mock_config.sources.substack = [
            "https://www.decodingai.com/feed",
            "https://newsletter.example.com/feed",
        ]
        mock_config.sources.substack_articles = []
        mocker.patch("tree.data.core.ingest.app_config", mock_config)

        domains = _get_configured_substack_domains()

        assert "decodingai.com" in domains
        assert "newsletter.example.com" in domains

    def test_extracts_domains_from_articles(self, mocker) -> None:
        mock_config = MagicMock()
        mock_config.sources.substack = []
        mock_config.sources.substack_articles = [
            "https://www.custom.blog/p/my-post",
        ]
        mocker.patch("tree.data.core.ingest.app_config", mock_config)

        domains = _get_configured_substack_domains()

        assert "custom.blog" in domains

    def test_strips_www_prefix(self, mocker) -> None:
        mock_config = MagicMock()
        mock_config.sources.substack = ["https://www.example.com/feed"]
        mock_config.sources.substack_articles = []
        mocker.patch("tree.data.core.ingest.app_config", mock_config)

        domains = _get_configured_substack_domains()

        assert "example.com" in domains
        assert "www.example.com" not in domains

    def test_deduplicates_domains(self, mocker) -> None:
        mock_config = MagicMock()
        mock_config.sources.substack = [
            "https://decodingai.com/feed",
            "https://www.decodingai.com/feed",
        ]
        mock_config.sources.substack_articles = [
            "https://decodingai.com/p/article",
        ]
        mocker.patch("tree.data.core.ingest.app_config", mock_config)

        domains = _get_configured_substack_domains()

        assert len([d for d in domains if "decodingai" in d]) == 1

    def test_empty_config_returns_empty_set(self, mocker) -> None:
        mock_config = MagicMock()
        mock_config.sources.substack = []
        mock_config.sources.substack_articles = []
        mocker.patch("tree.data.core.ingest.app_config", mock_config)

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
            "tree.data.core.ingest._SUBSTACK_CUSTOM_DOMAINS",
            {"decodingai.com"},
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
            "tree.data.core.ingest._SUBSTACK_CUSTOM_DOMAINS",
            {"example.com"},
        )

        await ingest_url("https://example.com/p/article")

        static_handler.assert_awaited_once()

    async def test_falls_through_to_web_for_unmatched_http_url(self, mocker) -> None:
        mock_fallback = AsyncMock(return_value=MagicMock())
        mocker.patch("tree.data.core.ingest._URL_HANDLERS", [])
        mocker.patch("tree.data.core.ingest._SUBSTACK_CUSTOM_DOMAINS", set())
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
        mocker.patch("tree.data.core.ingest._SUBSTACK_CUSTOM_DOMAINS", set())
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
        mocker.patch("tree.data.core.ingest._SUBSTACK_CUSTOM_DOMAINS", set())
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
