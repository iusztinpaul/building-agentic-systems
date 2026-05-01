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
    text: str = "",
) -> httpx.Response:
    """Build an httpx.Response object suitable as an AsyncClient.post return value."""

    request = httpx.Request("POST", "https://api.brightdata.com/request")
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


def _serp_html(entries: list[dict]) -> str:
    """Render a stub SERP HTML body with one ``<a><h3>`` per entry.

    Each entry is a dict with keys:
      - ``title``: the visible title text inside ``<h3>``.
      - ``link``: the destination URL on the wrapping ``<a>``. Empty / falsy
        means the anchor has no ``href`` (mirrors Google's UI chrome where a
        non-organic block has no destination — exercised by the
        "skip without link" test).
      - ``description``: optional snippet text rendered as a sibling block
        inside the result container.

    The structure mirrors Google's organic block: a ``<div>`` containing an
    ``<a href="...">`` whose first child is the title ``<h3>``, followed by a
    sibling ``<span>`` carrying the snippet. The parser keys off
    "h3 with an ancestor anchor whose href is an organic external URL", so
    this layout is sufficient.
    """

    parts: list[str] = ["<!doctype html><html><body><div id='search'>"]
    for entry in entries:
        link = entry.get("link") or ""
        title = entry.get("title", "")
        snippet = entry.get("description", "")
        href_attr = f' href="{link}"' if link else ""
        # Snippet padding ensures the extracted text crosses the parser's
        # 20-char minimum threshold for non-empty snippets when the test
        # expects a populated snippet.
        snippet_text = snippet if not snippet else snippet
        parts.append(
            f"<div class='g'>"
            f"<a{href_attr}><h3>{title}</h3></a>"
            f"<span>{snippet_text}</span>"
            f"</div>"
        )
    parts.append("</div></body></html>")
    return "".join(parts)


def _organic_entry(
    *,
    rank: int,
    title: str = "Title",
    link: str = "https://example.com",
    description: str = "Snippet that is long enough to survive the parser threshold.",
) -> dict:
    """Build an entry dict consumed by ``_serp_html``.

    ``rank`` is unused at the HTML layer (the parser assigns rank by document
    order) but kept in the signature so existing test call-sites that pass
    ``rank=...`` for clarity still compile.
    """

    _ = rank  # documentation aid only; parser assigns positional rank
    return {
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
        html = _serp_html(
            [
                _organic_entry(
                    rank=1,
                    title="Python.org",
                    link="https://python.org",
                    description=(
                        "Official site for the Python programming language "
                        "with downloads, docs, and news."
                    ),
                ),
                _organic_entry(
                    rank=2,
                    title="Docs",
                    link="https://docs.python.org",
                    description=(
                        "The official Python documentation, covering tutorial, "
                        "library, and language reference."
                    ),
                ),
            ]
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=html)])

        # Act
        results = await search("python", engine="google", num_results=10)

        # Assert
        assert len(results) == 2
        assert all(isinstance(r, SearchResult) for r in results)
        assert results[0].rank == 1
        assert results[0].title == "Python.org"
        assert results[0].url == "https://python.org"
        assert "Official site" in results[0].snippet
        assert results[1].rank == 2
        assert results[1].title == "Docs"
        assert results[1].url == "https://docs.python.org"
        assert "official Python documentation" in results[1].snippet

    async def test_returns_empty_list_when_no_organic_entries(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        # SERP HTML with no organic blocks (e.g. the "no results" page).
        empty_html = (
            "<!doctype html><html><body>"
            "<p>Your search did not match any documents.</p>"
            "</body></html>"
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=empty_html)])

        # Act
        results = await search("python")

        # Assert
        assert results == []

    async def test_returns_empty_list_when_organic_key_missing(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        # SERP HTML where the body has no h3 / anchor structure at all —
        # equivalent to the old "JSON missing the organic key" case: nothing
        # for the parser to anchor on.
        bare_html = "<!doctype html><html><body></body></html>"
        _patch_async_client(mocker, [_build_response(status_code=200, text=bare_html)])

        # Act
        results = await search("python")

        # Assert
        assert results == []

    async def test_skips_entries_without_link(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        html = _serp_html(
            [
                # First entry has no link — parser must skip.
                {
                    "title": "No link",
                    "link": "",
                    "description": "Some description that is long enough.",
                },
                _organic_entry(
                    rank=2,
                    title="Has link",
                    link="https://a.com",
                    description="Has a link and a description that is long enough.",
                ),
            ]
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=html)])

        # Act
        results = await search("python")

        # Assert
        assert len(results) == 1
        assert results[0].url == "https://a.com"

    async def test_assigns_positional_rank_when_entry_lacks_rank(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        # The HTML parser always assigns positional rank — there is no
        # upstream "rank" field to read. This test pins that contract.
        html = _serp_html(
            [
                _organic_entry(
                    rank=99,  # ignored by the parser
                    title="A",
                    link="https://a.com",
                    description="A description that is long enough to survive.",
                ),
                _organic_entry(
                    rank=99,
                    title="B",
                    link="https://b.com",
                    description="Another description that is long enough to survive.",
                ),
            ]
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=html)])

        # Act
        results = await search("python")

        # Assert
        assert [r.rank for r in results] == [1, 2]


class TestSearchRequestShape:
    async def test_posts_expected_body_and_headers(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker, api_key="my-key", zone="my-serp-zone")
        # Empty SERP body — we only care about the outbound request shape.
        mock_client = _patch_async_client(
            mocker,
            [_build_response(status_code=200, text="<html></html>")],
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
        # data_format=html is required: the configured SERP zone returns a
        # 226-byte metadata stub for the JSON shortcut, so we must request
        # the rendered HTML and parse it ourselves (tracker #010 / #012).
        assert body["data_format"] == "html"
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
            [_build_response(status_code=200, text="<html></html>")],
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
        # brd_json must NOT be present — the configured SERP zone (cli_serp)
        # returns only a metadata stub when this flag is set (tracker #010).
        assert "brd_json" not in qs
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
        page1_html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(1, 11)
            ]
        )
        page2_html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(11, 21)
            ]
        )
        mock_client = _patch_async_client(
            mocker,
            [
                _build_response(status_code=200, text=page1_html),
                _build_response(status_code=200, text=page2_html),
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
        html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(1, 4)
            ]
        )
        mock_client = _patch_async_client(
            mocker, [_build_response(status_code=200, text=html)]
        )

        # Act
        results = await search("python", num_results=50)

        # Assert
        assert len(results) == 3
        assert mock_client.post.await_count == 1

    async def test_truncates_to_num_results(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(1, 11)
            ]
        )
        mock_client = _patch_async_client(
            mocker, [_build_response(status_code=200, text=html)]
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
            [_build_response(status_code=200, text="<html></html>")],
        )

        # Act
        await search("python", engine="bing", country="us", language="en-US")

        # Assert
        body = mock_client.post.call_args.kwargs["json"]
        parsed = urlparse(body["url"])
        qs = parse_qs(parsed.query)
        assert parsed.netloc == "www.bing.com"
        assert qs["q"] == ["python"]
        # brd_json is NOT sent on any engine post-#012.
        assert "brd_json" not in qs
        assert qs["cc"] == ["us"]
        assert qs["setLang"] == ["en-US"]
        assert qs["first"] == ["1"]

    async def test_yandex_url_uses_text_param(self, mocker) -> None:
        # Arrange
        _patch_settings(mocker)
        mock_client = _patch_async_client(
            mocker,
            [_build_response(status_code=200, text="<html></html>")],
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
        assert "brd_json" not in qs
        assert qs["lr"] == ["ru"]
