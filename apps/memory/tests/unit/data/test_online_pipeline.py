"""Unit tests for tree.data.online_pipeline — URL dispatcher + online_ingest router."""

import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.config.app_config import (
    HuggingFaceDatasetSource,
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
)
from tree.data.online_pipeline import (
    ConversationSource,
    FileSource,
    UrlSource,
    _get_configured_substack_domains,
    _ingest_url,
    online_ingest,
)

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


@pytest.fixture(autouse=True)
def _clear_substack_domain_cache() -> None:
    """Reset the cached helper between tests so mocked configs are observed."""

    _get_configured_substack_domains.cache_clear()
    yield
    _get_configured_substack_domains.cache_clear()


def _patch_sources(mocker, entries: list) -> None:
    """Point the dispatcher's ``default_configured_sources`` at ``entries``.

    Mirrors the loader's real contract: it hands back a single cached list, so the
    mock returns the SAME list object on every call.
    """

    mocker.patch(
        "tree.data.online_pipeline.default_configured_sources",
        return_value=entries,
    )


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

    def test_does_not_mutate_loader_cached_list(self, mocker) -> None:
        """The helper iterates the loader's cached list read-only.

        ``default_configured_sources()`` returns the process-global cached list
        object; mutating it here would poison every other consumer.
        """

        entries = [
            SubstackRssSource(uri="https://decodingai.com/feed"),
            WebSource(uri="https://anthropic.com/post"),
        ]
        snapshot = list(entries)
        _patch_sources(mocker, entries)

        _get_configured_substack_domains()

        assert entries == snapshot


class TestIngestUrl:
    async def test_routes_substack_url(self, mocker) -> None:
        mock_handler = AsyncMock(return_value=MagicMock())
        mocker.patch(
            "tree.data.online_pipeline._URL_HANDLERS",
            [("substack.com", mock_handler)],
        )

        await _ingest_url("https://newsletter.substack.com/p/article", _USER_ID)

        mock_handler.assert_awaited_once_with(
            "https://newsletter.substack.com/p/article", _USER_ID
        )

    async def test_routes_custom_substack_domain(self, mocker) -> None:
        mock_substack = AsyncMock(return_value=MagicMock())
        mock_fallback = AsyncMock(return_value=MagicMock())
        mocker.patch("tree.data.online_pipeline._URL_HANDLERS", [])
        mocker.patch(
            "tree.data.online_pipeline._get_configured_substack_domains",
            return_value={"decodingai.com"},
        )
        mocker.patch(
            "tree.data.online_pipeline._ingest_substack_article",
            mock_substack,
        )
        mocker.patch(
            "tree.data.online_pipeline._ingest_web_url",
            mock_fallback,
        )

        await _ingest_url("https://decodingai.com/p/my-article", _USER_ID)

        mock_substack.assert_awaited_once_with(
            "https://decodingai.com/p/my-article", _USER_ID
        )
        mock_fallback.assert_not_awaited()

    async def test_static_registry_takes_precedence(self, mocker) -> None:
        static_handler = AsyncMock(return_value=MagicMock())
        mocker.patch(
            "tree.data.online_pipeline._URL_HANDLERS",
            [("example.com", static_handler)],
        )
        mocker.patch(
            "tree.data.online_pipeline._get_configured_substack_domains",
            return_value={"example.com"},
        )

        await _ingest_url("https://example.com/p/article", _USER_ID)

        static_handler.assert_awaited_once()

    async def test_falls_through_to_web_for_unmatched_http_url(self, mocker) -> None:
        mock_fallback = AsyncMock(return_value=MagicMock())
        mocker.patch("tree.data.online_pipeline._URL_HANDLERS", [])
        mocker.patch(
            "tree.data.online_pipeline._get_configured_substack_domains",
            return_value=set(),
        )
        mocker.patch(
            "tree.data.online_pipeline._ingest_web_url",
            mock_fallback,
        )

        await _ingest_url(
            "https://martinfowler.com/articles/microservices.html", _USER_ID
        )

        mock_fallback.assert_awaited_once_with(
            "https://martinfowler.com/articles/microservices.html", _USER_ID
        )

    async def test_falls_through_to_web_for_github_url(self, mocker) -> None:
        mock_fallback = AsyncMock(return_value=MagicMock())
        mocker.patch("tree.data.online_pipeline._URL_HANDLERS", [])
        mocker.patch(
            "tree.data.online_pipeline._get_configured_substack_domains",
            return_value=set(),
        )
        mocker.patch(
            "tree.data.online_pipeline._ingest_web_url",
            mock_fallback,
        )

        await _ingest_url("https://github.com/anthropics/claude-code", _USER_ID)

        mock_fallback.assert_awaited_once_with(
            "https://github.com/anthropics/claude-code", _USER_ID
        )

    async def test_fallback_emits_info_log(self, mocker, caplog) -> None:
        mock_fallback = AsyncMock(return_value=MagicMock())
        mocker.patch("tree.data.online_pipeline._URL_HANDLERS", [])
        mocker.patch(
            "tree.data.online_pipeline._get_configured_substack_domains",
            return_value=set(),
        )
        mocker.patch(
            "tree.data.online_pipeline._ingest_web_url",
            mock_fallback,
        )

        url = "https://martinfowler.com/articles/microservices.html"
        with caplog.at_level(logging.INFO, logger="tree.data.online_pipeline"):
            await _ingest_url(url, _USER_ID)

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
            "tree.data.online_pipeline._ingest_web_url",
            mock_fallback,
        )
        mocker.patch(
            "tree.data.online_pipeline._ingest_substack_article",
            mock_substack,
        )

        with pytest.raises(ValueError, match="scheme"):
            await _ingest_url(url, _USER_ID)

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
            "tree.data.online_pipeline._URL_HANDLERS",
            [
                ("youtube.com", mock_youtube),
                ("youtu.be", mock_youtube),
                ("substack.com", mock_substack),
            ],
        )
        mocker.patch(
            "tree.data.online_pipeline._ingest_web_url",
            mock_fallback,
        )

        await _ingest_url(url, _USER_ID)

        mock_youtube.assert_awaited_once_with(url, _USER_ID)
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
            "tree.data.online_pipeline._ingest_youtube_video",
            mock_youtube,
        )
        mocker.patch(
            "tree.data.online_pipeline._ingest_web_url",
            mock_fallback,
        )

        with pytest.raises(ValueError, match="youtube_rss"):
            await _ingest_url(url, _USER_ID)

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
            "tree.data.online_pipeline._ingest_web_url",
            mock_fallback,
        )
        mocker.patch(
            "tree.data.online_pipeline._ingest_substack_article",
            mock_substack,
        )

        with pytest.raises(ValueError, match="missing a host"):
            await _ingest_url(url, _USER_ID)

        mock_fallback.assert_not_awaited()
        mock_substack.assert_not_awaited()


class TestOnlineIngestRouting:
    """``online_ingest`` distributes each variant to its leaf pipeline + forwards user_id."""

    async def test_url_routes_to_ingest_url(self, mocker) -> None:
        doc = MagicMock()
        mock_url = mocker.patch(
            "tree.data.online_pipeline._ingest_url",
            new_callable=AsyncMock,
            return_value=doc,
        )

        result = await online_ingest(UrlSource(uri="https://example.com"), _USER_ID)

        assert result is doc
        mock_url.assert_awaited_once_with("https://example.com", _USER_ID)

    async def test_file_routes_to_file_pipeline(self, mocker) -> None:
        doc = MagicMock()
        mock_file = mocker.patch(
            "tree.data.online_pipeline._ingest_file",
            new_callable=AsyncMock,
            return_value=doc,
        )

        source = FileSource(path="/tmp/x.md", title="T")
        result = await online_ingest(source, _USER_ID)

        assert result is doc
        mock_file.assert_awaited_once_with(source, _USER_ID)

    async def test_conversation_routes_to_conversation_pipeline(self, mocker) -> None:
        doc = MagicMock()
        mock_conv = mocker.patch(
            "tree.data.online_pipeline._ingest_conversation",
            new_callable=AsyncMock,
            return_value=doc,
        )

        source = ConversationSource(text="hi")
        result = await online_ingest(source, _USER_ID)

        assert result is doc
        mock_conv.assert_awaited_once_with(source, _USER_ID)

    async def test_owns_opik_span_mirroring_offline(self, mocker) -> None:
        # The online data pipeline configures Opik + opens its own span, tagged
        # 1:1 with its Prefect flow-run tags (mirrors the offline trace).
        mock_configure = mocker.patch("tree.data.online_pipeline.configure_opik")
        captured: dict = {}

        @contextlib.contextmanager
        def fake_span(name, **kwargs):
            captured.update(
                name=name, tags=kwargs.get("tags"), meta=kwargs.get("metadata")
            )
            yield

        mocker.patch("tree.data.online_pipeline.span", fake_span)
        mocker.patch(
            "tree.data.online_pipeline._ingest_url",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        )

        await online_ingest(UrlSource(uri="https://example.com"), _USER_ID)

        mock_configure.assert_called_once()
        assert captured["name"] == "online_ingest"
        assert captured["tags"] == ["data-pipeline", "online"]
        assert captured["meta"] == {"pipeline": "data"}
