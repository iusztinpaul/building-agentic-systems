from __future__ import annotations

import base64
import logging
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


_END_OF_RESULTS_HTML = (
    "<!doctype html><html><body><ol id='b_results'>"
    "<li class='b_no'><h1>There are no results for <strong>x</strong></h1></li>"
    "</ol></body></html>"
)


def _patch_async_client(mocker, responses: list[httpx.Response]) -> AsyncMock:
    """Patch ``httpx.AsyncClient`` so successive ``.post()`` calls return ``responses``.

    Once ``responses`` is exhausted, every further page is Bing's "There are no
    results for" page — the end of the SERP — so a test that cares only about
    the first page need not script the page that stops pagination.

    Returns the mock client so tests can assert on call args.
    """

    pending = iter(responses)

    async def _post(*_args: object, **_kwargs: object) -> httpx.Response:
        return next(
            pending, _build_response(status_code=200, text=_END_OF_RESULTS_HTML)
        )

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=_post)

    mock_client_cm = MagicMock()
    mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cm.__aexit__ = AsyncMock(return_value=None)

    mocker.patch(
        "tree.data.web.web_serp.httpx.AsyncClient",
        return_value=mock_client_cm,
    )
    return mock_client


def _ck_href(url: str) -> str:
    """Wrap ``url`` the way Bing does: a ``/ck/a`` redirect with ``u=a1<b64url>``.

    Bing strips the base64 ``=`` padding, so the token here is stripped too —
    the resolver must restore it.
    """

    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return (
        "https://www.bing.com/ck/a?!&&p=0c0ffee&ptn=3&ver=2&hsh=4"
        f"&fclid=3c53c355&u=a1{token}&ntb=1"
    )


def _serp_html(entries: list[dict]) -> str:
    """Render a stub Bing SERP body with one ``li.b_algo`` per entry.

    Each entry is a dict with keys:
      - ``title``: the visible title text inside ``h2 > a``.
      - ``link``: the ``href`` on that anchor. Empty / falsy means the anchor
        has no ``href`` (the "skip without link" test).
      - ``description``: snippet text rendered as ``.b_caption p``.

    The structure mirrors Bing's organic block as captured live on 2026-10-03:
    ``<li class="b_algo"><h2><a href>title</a></h2><div class="b_caption">
    <p>snippet</p></div></li>`` inside ``ol#b_results``.
    """

    parts: list[str] = ["<!doctype html><html><body><ol id='b_results'>"]
    for entry in entries:
        link = entry.get("link") or ""
        title = entry.get("title", "")
        snippet = entry.get("description", "")
        href_attr = f' href="{link}"' if link else ""
        parts.append(
            f"<li class='b_algo'>"
            f"<h2><a{href_attr}>{title}</a></h2>"
            f"<div class='b_caption'><p>{snippet}</p></div>"
            f"</li>"
        )
    parts.append("</ol></body></html>")
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
        _patch_settings(mocker)

        with pytest.raises(ValueError, match="query must not be empty"):
            await search(bad_query)

    async def test_raises_value_error_for_non_string_query(self, mocker) -> None:
        _patch_settings(mocker)

        with pytest.raises(ValueError, match="query must not be empty"):
            await search(None)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad_count", [0, -1, -10], ids=["zero", "neg-1", "neg-10"])
    async def test_raises_value_error_when_num_results_below_one(
        self, mocker, bad_count: int
    ) -> None:
        _patch_settings(mocker)

        with pytest.raises(ValueError, match="num_results must be >= 1"):
            await search("python", num_results=bad_count)


class TestSearchConfiguration:
    async def test_raises_configuration_error_when_api_key_empty(self, mocker) -> None:
        _patch_settings(mocker, api_key="", zone="some-zone")

        with pytest.raises(BrightDataConfigurationError, match="BRIGHTDATA_API_KEY"):
            await search("python")

    async def test_raises_configuration_error_when_serp_zone_empty(
        self, mocker
    ) -> None:
        _patch_settings(mocker, api_key="key", zone="")

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
        _patch_settings(mocker)
        response = _build_response(status_code=status_code, text="boom")
        _patch_async_client(mocker, [response])

        with pytest.raises(BrightDataRequestError, match=str(status_code)):
            await search("python")

    async def test_returns_search_results_on_200(self, mocker) -> None:
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

        results = await search("python", num_results=10)

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

    async def test_returns_empty_list_when_no_organic_entries(
        self, mocker, caplog
    ) -> None:
        _patch_settings(mocker)
        # SERP HTML with no organic blocks (e.g. the "no results" page).
        empty_html = (
            "<!doctype html><html><body><ol id='b_results'>"
            "<li class='b_no'><h1>There are no results for "
            "<strong>python</strong></h1></li>"
            "</ol></body></html>"
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=empty_html)])

        with caplog.at_level(logging.DEBUG, logger="tree.data.web.web_serp"):
            results = await search("python")

        assert results == []
        # Tighten the contract: a recognized "no-results" SERP must NOT emit
        # a WARNING. WARNING is reserved for unexpected response shapes
        # (regression signal). See TestSearchEmptyResultLogging below.
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records == []

    async def test_returns_empty_list_when_organic_key_missing(self, mocker) -> None:
        _patch_settings(mocker)
        # SERP HTML where the body has no h3 / anchor structure at all —
        # equivalent to the old "JSON missing the organic key" case: nothing
        # for the parser to anchor on.
        bare_html = "<!doctype html><html><body></body></html>"
        _patch_async_client(mocker, [_build_response(status_code=200, text=bare_html)])

        results = await search("python")

        assert results == []

    async def test_skips_entries_without_link(self, mocker) -> None:
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

        results = await search("python")

        assert len(results) == 1
        assert results[0].url == "https://a.com"

    async def test_assigns_positional_rank_when_entry_lacks_rank(self, mocker) -> None:
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

        results = await search("python")

        assert [r.rank for r in results] == [1, 2]


class TestSearchEmptyResultLogging:
    """Verify the empty-result branching: legitimate-empty → INFO,
    unexpected-shape → WARNING. Both paths return ``[]`` (public contract
    unchanged); only the log signal distinguishes them.
    """

    async def test_logs_info_on_legitimate_empty_serp(self, mocker, caplog) -> None:
        _patch_settings(mocker)
        # Well-formed SERP HTML carrying Bing's "no results" indicator —
        # the structural anchor that says "this is a real SERP, just empty".
        empty_html = (
            "<!doctype html><html><body><ol id='b_results'>"
            "<li class='b_no'><h1>There are no results for "
            "<strong>asdfqwerzxcvuiop1234567890nope</strong></h1></li>"
            "</ol></body></html>"
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=empty_html)])

        with caplog.at_level(logging.DEBUG, logger="tree.data.web.web_serp"):
            results = await search("asdfqwerzxcvuiop1234567890nope")

        # Assert: returns [] AND emits exactly one INFO line about 0 organic.
        assert results == []
        info_records = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "0 organic" in r.getMessage()
        ]
        assert len(info_records) == 1, (
            f"expected exactly one '0 organic' INFO record, got "
            f"{[r.getMessage() for r in caplog.records]}"
        )
        message = info_records[0].getMessage()
        assert "query=asdfqwerzxcvuiop1234567890nope" in message
        # No WARNING — this is a legitimate empty, not a regression.
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records == []

    @pytest.mark.parametrize(
        "body, content_type",
        [
            pytest.param("", "text/html", id="empty-body"),
            pytest.param(
                "<html><body>Sorry, an error occurred</body></html>",
                "text/html",
                id="error-page-no-anchors",
            ),
            pytest.param(
                '{"organic": [], "_unexpected_root": true}',
                "application/json",
                id="json-instead-of-html",
            ),
        ],
    )
    async def test_logs_warning_on_unexpected_response_shape(
        self, mocker, caplog, body: str, content_type: str
    ) -> None:
        api_key = "secret-test-api-key-do-not-leak"
        _patch_settings(mocker, api_key=api_key)
        response = httpx.Response(
            status_code=200,
            text=body,
            headers={"content-type": content_type},
            request=httpx.Request("POST", "https://api.brightdata.com/request"),
        )
        _patch_async_client(mocker, [response])

        with caplog.at_level(logging.DEBUG, logger="tree.data.web.web_serp"):
            results = await search("python")

        # Assert: returns [] without raising.
        assert results == []
        # Exactly one WARNING from the SERP module on this branch.
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 1, (
            f"expected exactly one WARNING record, got "
            f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
        )
        message = warning_records[0].getMessage()
        assert "status=200" in message
        assert f"content_type={content_type}" in message
        assert "body_preview=" in message
        # Body preview must be truncated to <= 200 chars. Extract it from
        # the message and check.
        preview_marker = "body_preview="
        idx = message.index(preview_marker) + len(preview_marker)
        preview = message[idx:].rstrip(")").rstrip()
        assert len(preview) <= 200

        # API key never appears anywhere in any log record's message.
        for record in caplog.records:
            rendered = record.getMessage()
            assert api_key not in rendered, (
                f"API key leaked into log record: {rendered!r}"
            )
            assert "Bearer" not in rendered, (
                f"Authorization header leaked into log record: {rendered!r}"
            )


class TestSearchRequestShape:
    async def test_posts_expected_body_and_headers(self, mocker) -> None:
        _patch_settings(mocker, api_key="my-key", zone="my-serp-zone")
        # Empty SERP body — we only care about the outbound request shape.
        mock_client = _patch_async_client(
            mocker,
            [_build_response(status_code=200, text=_END_OF_RESULTS_HTML)],
        )

        await search("hello world")

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
        assert body["url"].startswith("https://www.bing.com/search?")
        headers = call.kwargs["headers"]
        assert headers["Authorization"] == "Bearer my-key"
        assert headers["Content-Type"] == "application/json"


class TestSearchPagination:
    async def test_paginates_when_num_results_exceeds_page_size(self, mocker) -> None:
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

        results = await search("python", num_results=15)

        assert len(results) == 15
        assert mock_client.post.await_count == 2

        first_url = mock_client.post.call_args_list[0].kwargs["json"]["url"]
        first_qs = parse_qs(urlparse(first_url).query)
        assert first_qs["first"] == ["1"]

        second_url = mock_client.post.call_args_list[1].kwargs["json"]["url"]
        second_qs = parse_qs(urlparse(second_url).query)
        assert second_qs["first"] == ["11"]
        # Rank continues across pages: page 2 starts at 11, not 1.
        assert [r.rank for r in results] == list(range(1, 16))

    async def test_stops_paginating_on_the_no_results_page(self, mocker) -> None:
        _patch_settings(mocker)
        # 3 results, then Bing's "There are no results for" page: the SERP is
        # exhausted, so the caller wanting 50 gets the 3 after two fetches.
        html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(1, 4)
            ]
        )
        mock_client = _patch_async_client(
            mocker,
            [
                _build_response(status_code=200, text=html),
                _build_response(status_code=200, text=_END_OF_RESULTS_HTML),
            ],
        )

        results = await search("python", num_results=50)

        assert len(results) == 3
        assert mock_client.post.await_count == 2

    async def test_keeps_paging_after_a_short_page(self, mocker) -> None:
        # Regression (QA 2026-10-03): Bing serves 5–10 ``b_algo`` per page,
        # so a 6-result page 1 is NOT the end of the SERP.
        _patch_settings(mocker)
        page1_html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(1, 7)
            ]
        )
        page2_html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(7, 17)
            ]
        )
        mock_client = _patch_async_client(
            mocker,
            [
                _build_response(status_code=200, text=page1_html),
                _build_response(status_code=200, text=page2_html),
            ],
        )

        results = await search("python", num_results=15)

        assert [r.url for r in results] == [f"https://a.com/{i}" for i in range(1, 16)]
        assert [r.rank for r in results] == list(range(1, 16))
        assert mock_client.post.await_count == 2
        # The offset advances by Bing's page size, not by what page 1 served.
        firsts = [
            parse_qs(urlparse(call.kwargs["json"]["url"]).query)["first"]
            for call in mock_client.post.call_args_list
        ]
        assert firsts == [["1"], ["11"]]

    async def test_drops_a_url_already_returned_by_an_earlier_page(
        self, mocker
    ) -> None:
        # Regression (QA 2026-10-03): the same url at ranks 8 and 13.
        _patch_settings(mocker)
        page1_html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(1, 11)
            ]
        )
        page2_html = _serp_html(
            [
                _organic_entry(rank=11, link="https://a.com/11", title="T11"),
                _organic_entry(
                    rank=12, link=_ck_href("https://a.com/8"), title="T8 again"
                ),
                _organic_entry(rank=13, link="https://a.com/12", title="T12"),
            ]
        )
        _patch_async_client(
            mocker,
            [
                _build_response(status_code=200, text=page1_html),
                _build_response(status_code=200, text=page2_html),
            ],
        )

        results = await search("python", num_results=12)

        urls = [r.url for r in results]
        assert len(urls) == len(set(urls)) == 12
        assert urls[10:] == ["https://a.com/11", "https://a.com/12"]
        # Ranks stay contiguous — the skipped duplicate consumes no rank.
        assert [r.rank for r in results] == list(range(1, 13))

    async def test_tolerates_one_replayed_page(self, mocker, caplog) -> None:
        # Bing once ignored ``first=11`` and re-served page 1 (seen live):
        # one page with nothing new does not end the search.
        _patch_settings(mocker)
        page1_html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(1, 11)
            ]
        )
        page3_html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(11, 21)
            ]
        )
        mock_client = _patch_async_client(
            mocker,
            [
                _build_response(status_code=200, text=page1_html),
                _build_response(status_code=200, text=page1_html),
                _build_response(status_code=200, text=page3_html),
            ],
        )

        with caplog.at_level(logging.DEBUG, logger="tree.data.web.web_serp"):
            results = await search("python", num_results=15)

        assert [r.url for r in results] == [f"https://a.com/{i}" for i in range(1, 16)]
        assert [r.rank for r in results] == list(range(1, 16))
        assert mock_client.post.await_count == 3
        # A replayed page is a real SERP, not an unexpected shape.
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("no new results" in r.getMessage() for r in caplog.records)

    async def test_stops_on_the_second_consecutive_page_with_nothing_new(
        self, mocker
    ) -> None:
        _patch_settings(mocker)
        page_html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(1, 11)
            ]
        )
        mock_client = _patch_async_client(
            mocker,
            [_build_response(status_code=200, text=page_html) for _ in range(5)],
        )

        results = await search("python", num_results=30)

        assert [r.url for r in results] == [f"https://a.com/{i}" for i in range(1, 11)]
        # Page 1, then two replays; the cap (ceil(30/10)+2 = 5) is not reached.
        assert mock_client.post.await_count == 3

    async def test_a_no_new_page_between_new_pages_resets_the_tolerance(
        self, mocker
    ) -> None:
        _patch_settings(mocker)

        def page(first: int, last: int) -> httpx.Response:
            return _build_response(
                status_code=200,
                text=_serp_html(
                    [
                        _organic_entry(rank=i, link=f"https://a.com/{i}")
                        for i in range(first, last + 1)
                    ]
                ),
            )

        mock_client = _patch_async_client(
            mocker,
            [page(1, 5), page(1, 5), page(6, 10), page(6, 10), page(11, 15)],
        )

        # Cap = ceil(25 / 10) + 2 = 5 pages; without the reset the second
        # no-new page (the 4th fetch) would stop the search at 10.
        results = await search("python", num_results=25)

        assert len(results) == 15
        assert mock_client.post.await_count == 5

    async def test_page_count_is_capped(self, mocker) -> None:
        # Every page adds exactly one new url: without the cap this would
        # page 15 times. Cap = ceil(15 / 10) + 2 = 4 pages.
        _patch_settings(mocker)
        mock_client = _patch_async_client(
            mocker,
            [
                _build_response(
                    status_code=200,
                    text=_serp_html(
                        [_organic_entry(rank=1, link=f"https://a.com/{page}")]
                    ),
                )
                for page in range(20)
            ],
        )

        results = await search("python", num_results=15)

        assert len(results) == 4
        assert mock_client.post.await_count == 4

    async def test_truncates_to_num_results(self, mocker) -> None:
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

        results = await search("python", num_results=3)

        assert len(results) == 3
        # No second page fetched — page returned == page size, but we have
        # enough results to satisfy the caller, so we stop.
        assert mock_client.post.await_count == 1


class TestBingUrl:
    @pytest.mark.parametrize(
        "country, language",
        [
            (None, None),
            ("us", "en-US"),
            ("us", None),
            (None, "en-US"),
        ],
        ids=["none", "us-en", "us-only", "en-only"],
    )
    async def test_url_maps_locale_to_cc_and_setlang(
        self, mocker, country: str | None, language: str | None
    ) -> None:
        _patch_settings(mocker)
        mock_client = _patch_async_client(
            mocker,
            [_build_response(status_code=200, text=_END_OF_RESULTS_HTML)],
        )

        await search("best laptops 2025", country=country, language=language)

        body = mock_client.post.call_args.kwargs["json"]
        parsed = urlparse(body["url"])
        qs = parse_qs(parsed.query)
        assert parsed.netloc == "www.bing.com"
        assert parsed.path == "/search"
        assert qs["q"] == ["best laptops 2025"]
        # brd_json must NOT be present — the configured SERP zone (cli_serp)
        # returns only a metadata stub when this flag is set (tracker #010).
        assert "brd_json" not in qs
        # Bing's offset is 1-indexed: the first page is first=1.
        assert qs["first"] == ["1"]
        if country:
            assert qs["cc"] == [country]
        else:
            assert "cc" not in qs
        if language:
            assert qs["setLang"] == [language]
        else:
            assert "setLang" not in qs

    async def test_search_has_no_engine_parameter(self, mocker) -> None:
        # Bing is the only engine (ADR-013 §1): a stale caller passing
        # ``engine`` fails loudly instead of being silently ignored.
        _patch_settings(mocker)

        with pytest.raises(TypeError, match="engine"):
            await search("python", engine="google")  # type: ignore[call-arg]


class TestBingParser:
    async def test_parses_direct_and_ck_redirect_hrefs(self, mocker) -> None:
        _patch_settings(mocker)
        html = _serp_html(
            [
                _organic_entry(
                    rank=1,
                    title="Beanie",
                    link="https://beanie-odm.dev/",
                    description="Asynchronous Python ODM for MongoDB, built on Pydantic.",
                ),
                _organic_entry(
                    rank=2,
                    title="Example",
                    link=_ck_href("https://example.com/x"),
                    description="A redirect-wrapped organic result with a snippet.",
                ),
            ]
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=html)])

        results = await search("beanie")

        assert [(r.rank, r.title, r.url) for r in results] == [
            (1, "Beanie", "https://beanie-odm.dev/"),
            (2, "Example", "https://example.com/x"),
        ]
        assert results[0].snippet == (
            "Asynchronous Python ODM for MongoDB, built on Pydantic."
        )
        assert results[1].snippet == "A redirect-wrapped organic result with a snippet."

    async def test_skips_bing_chrome_and_h2_links_outside_b_algo(self, mocker) -> None:
        _patch_settings(mocker)
        html = (
            "<html><body>"
            # A top-answer / ad block: ``h2 a`` NOT inside ``li.b_algo``.
            "<div class='b_ad'><h2><a href='https://ads.example.com/'>Ad</a></h2></div>"
            "<ol id='b_results'>"
            "<li class='b_algo'><h2><a href='https://www.bing.com/images/search?q=x'>"
            "Images for x</a></h2></li>"
            "<li class='b_algo'><h2><a href='/videos/search?q=x'>Videos</a></h2></li>"
            "<li class='b_algo'><h2><a href='https://real.example.org/page'>Real</a>"
            "</h2><div class='b_caption'><p>The one organic result on this page.</p>"
            "</div></li>"
            "</ol></body></html>"
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=html)])

        results = await search("x")

        assert [r.url for r in results] == ["https://real.example.org/page"]
        assert results[0].rank == 1

    async def test_drops_duplicates_on_the_resolved_url(self, mocker) -> None:
        _patch_settings(mocker)
        html = _serp_html(
            [
                _organic_entry(rank=1, title="Direct", link="https://example.com/x"),
                _organic_entry(
                    rank=2, title="Wrapped", link=_ck_href("https://example.com/x")
                ),
                _organic_entry(rank=3, title="Other", link="https://other.example/"),
            ]
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=html)])

        results = await search("x")

        assert [(r.rank, r.url) for r in results] == [
            (1, "https://example.com/x"),
            (2, "https://other.example/"),
        ]

    async def test_snippet_falls_back_to_a_paragraph_outside_b_caption(
        self, mocker
    ) -> None:
        # The live ``b_imgcap`` layout (captured 2026-10-03): the snippet ``p``
        # sits beside the title and ``.b_caption`` is empty.
        _patch_settings(mocker)
        html = (
            "<html><body><ol id='b_results'><li class='b_algo'>"
            "<div class='b_imgcap_main'>"
            "<h2><a href='https://github.com/BeanieODM'>BeanieODM · GitHub</a></h2>"
            "<p class='b_lineclamp2'>Beanie: An asynchronous ODM for MongoDB.</p>"
            "</div><div class='b_caption b_rich'></div>"
            "</li></ol></body></html>"
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=html)])

        results = await search("beanie")

        assert results[0].snippet == "Beanie: An asynchronous ODM for MongoDB."

    async def test_snippet_falls_back_to_the_result_text_minus_title(
        self, mocker
    ) -> None:
        _patch_settings(mocker)
        html = (
            "<html><body><ol id='b_results'>"
            "<li class='b_algo'><h2><a href='https://a.example/'>Title A</a></h2>"
            "<div>Free text describing result A at some length.</div></li>"
            "<li class='b_algo'><h2><a href='https://b.example/'>Title B</a></h2>"
            "<div>Text that belongs to result B only, never to A.</div></li>"
            "</ol></body></html>"
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=html)])

        results = await search("x")

        # Bounded to the ``li``: A's snippet never bleeds into B's text.
        assert results[0].snippet == "Free text describing result A at some length."
        assert results[1].snippet == "Text that belongs to result B only, never to A."

    async def test_snippet_is_capped(self, mocker) -> None:
        _patch_settings(mocker)
        long_text = "word " * 200
        html = _serp_html(
            [_organic_entry(rank=1, link="https://a.example/", description=long_text)]
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=html)])

        results = await search("x")

        # 300 chars + the "..." ellipsis.
        assert len(results[0].snippet) <= 303
        assert results[0].snippet.endswith("...")


class TestResolveBingHref:
    @pytest.mark.parametrize(
        "href, expected",
        [
            pytest.param(
                "https://beanie-odm.dev/", "https://beanie-odm.dev/", id="direct-https"
            ),
            pytest.param(
                "http://example.com/a?b=1", "http://example.com/a?b=1", id="direct-http"
            ),
            pytest.param(
                # No padding needed: "https://example.com/x" encodes to 28 chars.
                "https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS94&ntb=1",
                "https://example.com/x",
                id="ck-a-unpadded-token",
            ),
            pytest.param(
                # Bing strips the "==" padding of "https://example.com/ab".
                "https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9hYg&ntb=1",
                "https://example.com/ab",
                id="ck-a-padding-restored",
            ),
            pytest.param(
                # The real token captured live for the BeanieODM GitHub result.
                "https://www.bing.com/ck/a?!&&p=82b0&ptn=3&ver=2&hsh=4"
                "&u=a1aHR0cHM6Ly9naXRodWIuY29tL0JlYW5pZU9ETQ&ntb=1",
                "https://github.com/BeanieODM",
                id="ck-a-live-token",
            ),
        ],
    )
    def test_resolves_to_the_real_url(self, href: str, expected: str) -> None:
        from tree.data.web.web_serp import _resolve_bing_href

        assert _resolve_bing_href(href) == expected

    @pytest.mark.parametrize(
        "href",
        [
            pytest.param(
                # base64url("not a url") — decodes, but is not http(s).
                "https://www.bing.com/ck/a?!&&p=abc&u=a1bm90IGEgdXJs&ntb=1",
                id="ck-a-decodes-to-non-http",
            ),
            pytest.param(
                "https://www.bing.com/ck/a?!&&p=abc&u=a1!!!notbase64&ntb=1",
                id="ck-a-undecodable-token",
            ),
            pytest.param("https://www.bing.com/ck/a?!&&p=abc&ntb=1", id="ck-a-no-u"),
            pytest.param("https://www.bing.com/ck/a?u=zzabc", id="ck-a-no-a1-prefix"),
            pytest.param("javascript:void(0)", id="javascript"),
            pytest.param("/images/search?q=x", id="relative-chrome"),
            pytest.param("https://www.bing.com/videos/search?q=x", id="bing-videos"),
            pytest.param("https://bing.com/maps?q=x", id="bing-apex"),
            pytest.param("https://www.bing.com/aclk?ld=e8b1", id="bing-ad"),
            pytest.param("https://microsoft.com/privacy", id="microsoft-apex"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none_for_unresolvable_hrefs(self, href: str) -> None:
        from tree.data.web.web_serp import _resolve_bing_href

        assert _resolve_bing_href(href) is None

    def test_keeps_microsoft_subdomains(self) -> None:
        # Only the ``microsoft.com`` apex is chrome; docs on
        # ``learn.microsoft.com`` are legitimate organic results.
        from tree.data.web.web_serp import _resolve_bing_href

        url = "https://learn.microsoft.com/en-us/azure/"
        assert _resolve_bing_href(url) == url

    @pytest.mark.parametrize(
        "href, expected",
        [
            pytest.param(
                "https://tabelog.com/ramen/tokyo/rank/?msockid=3cd4a1",
                "https://tabelog.com/ramen/tokyo/rank/",
                id="msockid-only",
            ),
            pytest.param(
                "https://a.example/p?msockid=3cd4a1&page=2&q=x+y",
                "https://a.example/p?page=2&q=x+y",
                id="msockid-first-other-params-kept",
            ),
            pytest.param(
                "https://a.example/p?page=2&msockid=3cd4a1#top",
                "https://a.example/p?page=2#top",
                id="msockid-last-fragment-kept",
            ),
            pytest.param(
                _ck_href("https://a.example/p?msockid=3cd4a1&id=7"),
                "https://a.example/p?id=7",
                id="decoded-ck-a-target",
            ),
            pytest.param(
                "https://a.example/p?id=7&flag=",
                "https://a.example/p?id=7&flag=",
                id="no-msockid-untouched",
            ),
        ],
    )
    def test_strips_the_msockid_tracking_param(self, href: str, expected: str) -> None:
        from tree.data.web.web_serp import _resolve_bing_href

        assert _resolve_bing_href(href) == expected

    async def test_urls_differing_only_by_msockid_are_one_result(self, mocker) -> None:
        _patch_settings(mocker)
        html = _serp_html(
            [
                _organic_entry(rank=1, link="https://a.example/p?msockid=AAA"),
                _organic_entry(rank=2, link="https://a.example/p?msockid=BBB"),
            ]
        )
        _patch_async_client(mocker, [_build_response(status_code=200, text=html)])

        results = await search("x")

        assert [r.url for r in results] == ["https://a.example/p"]


_COOLDOWN_BODY = (
    "This query recently failed and cannot be attempted at this time. "
    "Please try again later, after a minimum of 15 seconds."
)


class TestCooldown:
    async def test_cooldown_then_results_retries_once_after_sleeping(
        self, mocker, caplog
    ) -> None:
        _patch_settings(mocker)
        sleep = mocker.patch("tree.data.web.web_serp.asyncio.sleep", AsyncMock())
        html = _serp_html([_organic_entry(rank=1, link="https://a.example/")])
        mock_client = _patch_async_client(
            mocker,
            [
                _build_response(status_code=200, text=_COOLDOWN_BODY),
                _build_response(status_code=200, text=html),
            ],
        )

        with caplog.at_level(logging.DEBUG, logger="tree.data.web.web_serp"):
            results = await search("prefect horizon fastmcp")

        assert [r.url for r in results] == ["https://a.example/"]
        sleep.assert_awaited_once_with(15.0)
        # cooldown, its retry, then the end-of-results page 2.
        assert mock_client.post.await_count == 3
        # The retry re-POSTs the SAME page.
        first, second, _ = mock_client.post.call_args_list
        assert first.kwargs["json"] == second.kwargs["json"]
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "SERP cooldown" in warnings[0]
        assert "prefect horizon fastmcp" in warnings[0]
        assert "15.0s" in warnings[0]

    async def test_cooldown_twice_raises_cooldown_error(self, mocker) -> None:
        from tree.data.web.web_serp import BrightDataCooldownError

        _patch_settings(mocker)
        sleep = mocker.patch("tree.data.web.web_serp.asyncio.sleep", AsyncMock())
        mock_client = _patch_async_client(
            mocker,
            [
                _build_response(status_code=200, text=_COOLDOWN_BODY),
                _build_response(status_code=200, text=_COOLDOWN_BODY),
            ],
        )

        with pytest.raises(BrightDataCooldownError) as exc_info:
            await search("prefect horizon fastmcp")

        assert str(exc_info.value) == _COOLDOWN_BODY
        # A cooldown IS a request failure — existing handlers still catch it.
        assert isinstance(exc_info.value, BrightDataRequestError)
        assert mock_client.post.await_count == 2
        sleep.assert_awaited_once_with(15.0)

    async def test_cooldown_on_a_later_page_returns_the_results_collected(
        self, mocker, caplog
    ) -> None:
        _patch_settings(mocker)
        sleep = mocker.patch("tree.data.web.web_serp.asyncio.sleep", AsyncMock())
        page1_html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(1, 11)
            ]
        )
        mock_client = _patch_async_client(
            mocker,
            [
                _build_response(status_code=200, text=page1_html),
                _build_response(status_code=200, text=_COOLDOWN_BODY),
                _build_response(status_code=200, text=_COOLDOWN_BODY),
            ],
        )

        with caplog.at_level(logging.DEBUG, logger="tree.data.web.web_serp"):
            results = await search("prefect horizon fastmcp", num_results=15)

        assert [r.url for r in results] == [f"https://a.com/{i}" for i in range(1, 11)]
        assert mock_client.post.await_count == 3
        sleep.assert_awaited_once_with(15.0)
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 2
        assert "SERP page failed" in warnings[1]
        assert "offset=10" in warnings[1]
        assert "BrightDataCooldownError" in warnings[1]
        assert "10 result(s)" in warnings[1]

    @pytest.mark.parametrize(
        "network_error",
        [httpx.ReadTimeout("read timed out"), httpx.ConnectError("refused")],
        ids=["read-timeout", "connect-error"],
    )
    async def test_network_error_on_a_later_page_returns_the_results_collected(
        self, mocker, caplog, network_error: Exception
    ) -> None:
        _patch_settings(mocker)
        page1_html = _serp_html(
            [
                _organic_entry(rank=i, link=f"https://a.com/{i}", title=f"T{i}")
                for i in range(1, 11)
            ]
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=[
                _build_response(status_code=200, text=page1_html),
                network_error,
            ]
        )
        mock_client_cm = MagicMock()
        mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cm.__aexit__ = AsyncMock(return_value=None)
        mocker.patch(
            "tree.data.web.web_serp.httpx.AsyncClient", return_value=mock_client_cm
        )

        with caplog.at_level(logging.DEBUG, logger="tree.data.web.web_serp"):
            results = await search("python", num_results=15)

        assert [r.url for r in results] == [f"https://a.com/{i}" for i in range(1, 11)]
        assert mock_client.post.await_count == 2
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "offset=10" in warnings[0]
        assert type(network_error).__name__ in warnings[0]
        assert "10 result(s)" in warnings[0]

    @pytest.mark.parametrize(
        "network_error",
        [httpx.ReadTimeout("read timed out"), httpx.ConnectError("refused")],
        ids=["read-timeout", "connect-error"],
    )
    async def test_network_error_on_the_first_page_raises(
        self, mocker, network_error: Exception
    ) -> None:
        _patch_settings(mocker)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=network_error)
        mock_client_cm = MagicMock()
        mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cm.__aexit__ = AsyncMock(return_value=None)
        mocker.patch(
            "tree.data.web.web_serp.httpx.AsyncClient", return_value=mock_client_cm
        )

        with pytest.raises(type(network_error)):
            await search("python", num_results=15)

    async def test_default_timeout_is_sixty_seconds(self, mocker) -> None:
        _patch_settings(mocker)
        _patch_async_client(mocker, [])

        await search("python")

        from tree.data.web import web_serp

        web_serp.httpx.AsyncClient.assert_called_once_with(timeout=60.0)

    async def test_cooldown_detection_is_case_insensitive(self, mocker) -> None:
        from tree.data.web.web_serp import _is_cooldown_body

        assert _is_cooldown_body(_COOLDOWN_BODY.upper())
        assert _is_cooldown_body("  \n" + _COOLDOWN_BODY)
        assert not _is_cooldown_body("<html><body>results</body></html>")
        # Only the head of the body is inspected: a SERP that merely QUOTES
        # the sentence deep inside a page is not a cooldown.
        assert not _is_cooldown_body("x" * 400 + _COOLDOWN_BODY)

    async def test_non_cooldown_body_is_not_retried(self, mocker) -> None:
        _patch_settings(mocker)
        sleep = mocker.patch("tree.data.web.web_serp.asyncio.sleep", AsyncMock())
        mock_client = _patch_async_client(
            mocker, [_build_response(status_code=200, text="<html></html>")]
        )

        assert await search("x") == []

        sleep.assert_not_awaited()
        # No same-page re-POST: the second request is page 2 (an unexpected
        # page is one tolerated no-new page), which is the no-results page.
        firsts = [
            parse_qs(urlparse(call.kwargs["json"]["url"]).query)["first"]
            for call in mock_client.post.call_args_list
        ]
        assert firsts == [["1"], ["11"]]
