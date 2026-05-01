"""Unit tests for tree.data.web.web_serp.search."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tree.data.web import SearchResult, search
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)


def _patch_settings(
    mocker,
    *,
    api_key: str = "test-api-key",
    zone: str = "test-serp-zone",
) -> None:
    """Patch the settings singleton in the web_serp module."""

    fake_secret = MagicMock()
    fake_secret.get_secret_value.return_value = api_key

    fake_settings = MagicMock()
    fake_settings.brightdata_api_key = fake_secret
    fake_settings.brightdata_serp_zone = zone

    mocker.patch("tree.data.web.web_serp.settings", fake_settings)


def _build_response(
    *,
    status_code: int,
    json_payload: dict | None = None,
    text: str = "",
) -> httpx.Response:
    """Build an httpx.Response object suitable as an AsyncClient.post return value."""

    request = httpx.Request("POST", "https://api.brightdata.com/request")
    if json_payload is not None:
        return httpx.Response(
            status_code=status_code, json=json_payload, request=request
        )
    return httpx.Response(status_code=status_code, text=text, request=request)


def _patch_async_client(mocker, responses: list[httpx.Response]) -> AsyncMock:
    """Patch ``httpx.AsyncClient`` so successive ``.post()`` calls return ``responses``.

    Returns the mock client so tests can assert on call args.
    """

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=responses)

    mock_client_cm = MagicMock()
    mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cm.__aexit__ = AsyncMock(return_value=None)

    mocker.patch(
        "tree.data.web.web_serp.httpx.AsyncClient",
        return_value=mock_client_cm,
    )
    return mock_client


def _organic_entry(
    *,
    rank: int,
    title: str = "Title",
    link: str = "https://example.com",
    description: str = "Snippet",
) -> dict:
    return {
        "rank": rank,
        "title": title,
        "link": link,
        "description": description,
    }


class TestSearchInputValidation:
    @pytest.mark.parametrize(
        "bad_query",
        ["", "   ", "\n\t "],
        ids=["empty", "spaces", "whitespace"],
    )
    async def test_raises_value_error_for_empty_query(
        self, mocker, bad_query: str
    ) -> None:
        # Arrange
        _patch_settings(mocker)

        # Act & Assert
        with pytest.raises(ValueError, match="query must not be empty"):
            await search(bad_query)

    async def test_raises_value_error_for_non_string_query(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)

        # Act & Assert
        with pytest.raises(ValueError, match="query must not be empty"):
            await search(None)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad_count", [0, -1, -10], ids=["zero", "neg-1", "neg-10"])
    async def test_raises_value_error_when_num_results_below_one(
        self, mocker, bad_count: int
    ) -> None:
        # Arrange
        _patch_settings(mocker)

        # Act & Assert
        with pytest.raises(ValueError, match="num_results must be >= 1"):
            await search("python", num_results=bad_count)


class TestSearchConfiguration:
    async def test_raises_configuration_error_when_api_key_empty(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker, api_key="", zone="some-zone")

        # Act & Assert
        with pytest.raises(BrightDataConfigurationError, match="BRIGHTDATA_API_KEY"):
            await search("python")

    async def test_raises_configuration_error_when_serp_zone_empty(
        self, mocker
    ) -> None:
        # Arrange
        _patch_settings(mocker, api_key="key", zone="")

        # Act & Assert
        with pytest.raises(BrightDataConfigurationError, match="BRIGHTDATA_SERP_ZONE"):
            await search("python")


class TestSearchHttpBehavior:
    @pytest.mark.parametrize(
        "status_code",
        [400, 401, 403, 404, 429, 500, 502, 503],
        ids=["400", "401", "403", "404", "429", "500", "502", "503"],
    )
    async def test_raises_request_error_on_non_2xx(
        self, mocker, status_code: int
    ) -> None:
        # Arrange
        _patch_settings(mocker)
        response = _build_response(status_code=status_code, text="boom")
        _patch_async_client(mocker, [response])

        # Act & Assert
        with pytest.raises(BrightDataRequestError, match=str(status_code)):
            await search("python")

    async def test_returns_search_results_on_200(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        payload = {
            "general": {"search_engine": "google", "query": "python"},
            "organic": [
                _organic_entry(
                    rank=1,
                    title="Python.org",
                    link="https://python.org",
                    description="Official site",
                ),
                _organic_entry(
                    rank=2,
                    title="Docs",
                    link="https://docs.python.org",
                    description="Docs site",
                ),
            ],
        }
        _patch_async_client(
            mocker, [_build_response(status_code=200, json_payload=payload)]
        )

        # Act
        results = await search("python", engine="google", num_results=10)

        # Assert
        assert len(results) == 2
        assert all(isinstance(r, SearchResult) for r in results)
        assert results[0].rank == 1
        assert results[0].title == "Python.org"
        assert results[0].url == "https://python.org"
        assert results[0].snippet == "Official site"
        assert results[1].rank == 2
        assert results[1].title == "Docs"
        assert results[1].url == "https://docs.python.org"
        assert results[1].snippet == "Docs site"

    async def test_returns_empty_list_when_no_organic_entries(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        _patch_async_client(
            mocker,
            [_build_response(status_code=200, json_payload={"organic": []})],
        )

        # Act
        results = await search("python")

        # Assert
        assert results == []

    async def test_returns_empty_list_when_organic_key_missing(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        _patch_async_client(mocker, [_build_response(status_code=200, json_payload={})])

        # Act
        results = await search("python")

        # Assert
        assert results == []

    async def test_skips_entries_without_link(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        payload = {
            "organic": [
                {"rank": 1, "title": "No link"},
                _organic_entry(rank=2, title="Has link", link="https://a.com"),
            ],
        }
        _patch_async_client(
            mocker, [_build_response(status_code=200, json_payload=payload)]
        )

        # Act
        results = await search("python")

        # Assert
        assert len(results) == 1
        assert results[0].url == "https://a.com"

    async def test_assigns_positional_rank_when_entry_lacks_rank(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        payload = {
            "organic": [
                {"title": "A", "link": "https://a.com", "description": ""},
                {"title": "B", "link": "https://b.com", "description": ""},
            ],
        }
        _patch_async_client(
            mocker, [_build_response(status_code=200, json_payload=payload)]
        )

        # Act
        results = await search("python")

        # Assert
        assert [r.rank for r in results] == [1, 2]


class TestSearchRequestShape:
    async def test_posts_expected_body_and_headers(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker, api_key="my-key", zone="my-serp-zone")
        payload = {"organic": []}
        mock_client = _patch_async_client(
            mocker, [_build_response(status_code=200, json_payload=payload)]
        )

        # Act
        await search("hello world", engine="google")

        # Assert
        mock_client.post.assert_awaited_once()
        call = mock_client.post.call_args
        assert call.args[0] == "https://api.brightdata.com/request"
        body = call.kwargs["json"]
        assert body["zone"] == "my-serp-zone"
        assert body["format"] == "raw"
        assert "https://www.google.com/search?" in body["url"]
        headers = call.kwargs["headers"]
        assert headers["Authorization"] == "Bearer my-key"
        assert headers["Content-Type"] == "application/json"

    @pytest.mark.parametrize(
        "country, language",
        [
            (None, None),
            ("us", "en"),
            ("us", None),
            (None, "en"),
        ],
        ids=["none", "us-en", "us-only", "en-only"],
    )
    async def test_google_url_includes_required_params(
        self, mocker, country: str | None, language: str | None
    ) -> None:
        # Arrange
        _patch_settings(mocker)
        mock_client = _patch_async_client(
            mocker,
            [_build_response(status_code=200, json_payload={"organic": []})],
        )

        # Act
        await search(
            "best laptops 2025",
            engine="google",
            country=country,
            language=language,
        )

        # Assert
        body = mock_client.post.call_args.kwargs["json"]
        parsed = urlparse(body["url"])
        qs = parse_qs(parsed.query)

        assert parsed.netloc == "www.google.com"
        assert parsed.path == "/search"
        assert qs["q"] == ["best laptops 2025"]
        assert qs["brd_json"] == ["1"]
        # offset=0 should NOT include start
        assert "start" not in qs

        if country:
            assert qs["gl"] == [country]
        else:
            assert "gl" not in qs

        if language:
            assert qs["hl"] == [language]
        else:
            assert "hl" not in qs


class TestSearchPagination:
    async def test_paginates_when_num_results_exceeds_page_size(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        page1 = {
            "organic": [
                _organic_entry(rank=i, link=f"https://a.com/{i}") for i in range(1, 11)
            ]
        }
        page2 = {
            "organic": [
                _organic_entry(rank=i, link=f"https://a.com/{i}") for i in range(11, 21)
            ]
        }
        mock_client = _patch_async_client(
            mocker,
            [
                _build_response(status_code=200, json_payload=page1),
                _build_response(status_code=200, json_payload=page2),
            ],
        )

        # Act
        results = await search("python", engine="google", num_results=15)

        # Assert
        assert len(results) == 15
        assert mock_client.post.await_count == 2

        # First call: no start param
        first_url = mock_client.post.call_args_list[0].kwargs["json"]["url"]
        first_qs = parse_qs(urlparse(first_url).query)
        assert "start" not in first_qs

        # Second call: start=10
        second_url = mock_client.post.call_args_list[1].kwargs["json"]["url"]
        second_qs = parse_qs(urlparse(second_url).query)
        assert second_qs["start"] == ["10"]

    async def test_stops_paginating_when_page_returns_fewer_than_page_size(
        self, mocker
    ) -> None:
        # Arrange
        _patch_settings(mocker)
        # Only 3 entries on the only page — caller wants 50 but we should
        # stop after one fetch and return what we got.
        payload = {
            "organic": [
                _organic_entry(rank=i, link=f"https://a.com/{i}") for i in range(1, 4)
            ]
        }
        mock_client = _patch_async_client(
            mocker, [_build_response(status_code=200, json_payload=payload)]
        )

        # Act
        results = await search("python", num_results=50)

        # Assert
        assert len(results) == 3
        assert mock_client.post.await_count == 1

    async def test_truncates_to_num_results(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        payload = {
            "organic": [
                _organic_entry(rank=i, link=f"https://a.com/{i}") for i in range(1, 11)
            ]
        }
        mock_client = _patch_async_client(
            mocker, [_build_response(status_code=200, json_payload=payload)]
        )

        # Act
        results = await search("python", num_results=3)

        # Assert
        assert len(results) == 3
        # No second page fetched — page returned == page size, but we have
        # enough results to satisfy the caller, so we stop.
        assert mock_client.post.await_count == 1


class TestSearchEngines:
    async def test_bing_url_uses_first_offset(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        mock_client = _patch_async_client(
            mocker,
            [_build_response(status_code=200, json_payload={"organic": []})],
        )

        # Act
        await search("python", engine="bing", country="us", language="en-US")

        # Assert
        body = mock_client.post.call_args.kwargs["json"]
        parsed = urlparse(body["url"])
        qs = parse_qs(parsed.query)
        assert parsed.netloc == "www.bing.com"
        assert qs["q"] == ["python"]
        assert qs["brd_json"] == ["1"]
        assert qs["cc"] == ["us"]
        assert qs["setLang"] == ["en-US"]
        assert qs["first"] == ["1"]

    async def test_yandex_url_uses_text_param(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        mock_client = _patch_async_client(
            mocker,
            [_build_response(status_code=200, json_payload={"organic": []})],
        )

        # Act
        await search("python", engine="yandex", country="ru")

        # Assert
        body = mock_client.post.call_args.kwargs["json"]
        parsed = urlparse(body["url"])
        qs = parse_qs(parsed.query)
        assert parsed.netloc == "yandex.com"
        assert parsed.path == "/search/"
        assert qs["text"] == ["python"]
        assert qs["brd_json"] == ["1"]
        assert qs["lr"] == ["ru"]
