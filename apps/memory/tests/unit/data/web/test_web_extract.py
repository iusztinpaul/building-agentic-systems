"""Unit tests for tree.data.web.web_extract — Main-content extraction (ADR-013 §6)."""

import logging

import pytest
import trafilatura

from tree.data.web.web_extract import (
    MAX_HEADING_CHARS,
    MIN_EXTRACTED_CHARS,
    ExtractedPage,
    extract_main_content,
    page_title,
)

_URL = "https://docs.example.com/apps/low-level"
_NAV_TEXTS = [f"NavLink{i}" for i in range(10)]
_ASIDE_TEXTS = ["SidebarOne", "SidebarTwo"]
_FOOTER_TEXTS = ["FooterPrivacy", "FooterTerms"]

_PARAGRAPHS = [
    "The first paragraph explains how a low-level app receives the ontoolresult "
    "content block and renders it inside the host without any extra wiring.",
    "The second paragraph covers the lifecycle of the iframe, including how messages "
    "are posted back to the server and how errors reach the model as results.",
    "The third paragraph links to the "
    '<a href="https://docs.example.com/apps/overview">apps overview</a> '
    "for readers who want the high-level picture first.",
]


def _page(*, h1: str | None = "Low-level MCP Apps", main_extra: str = "") -> str:
    nav = "".join(
        f'<li><a href="/nav{i}">{t}</a></li>' for i, t in enumerate(_NAV_TEXTS)
    )
    aside = "".join(f'<a href="/side{i}">{t}</a>' for i, t in enumerate(_ASIDE_TEXTS))
    footer = "".join(f'<a href="/f{i}">{t}</a>' for i, t in enumerate(_FOOTER_TEXTS))
    heading = f"<h1>{h1}</h1>" if h1 else ""
    paragraphs = "".join(f"<p>{p}</p>" for p in _PARAGRAPHS)
    return (
        "<html><head><title>Low-level Apps Guide</title>"
        '<meta property="og:title" content="Low-level Apps Guide"></head><body>'
        f"<nav><ul>{nav}</ul></nav>"
        f"<aside>{aside}</aside>"
        f"<main><article>{heading}{paragraphs}"
        "<ul><li>First bullet item</li><li>Second bullet item</li></ul>"
        "<table><tr><th>Name</th><th>Value</th></tr>"
        "<tr><td>alpha</td><td>1</td></tr><tr><td>beta</td><td>2</td></tr></table>"
        f"{main_extra}</article></main>"
        f"<footer>{footer}</footer>"
        "</body></html>"
    )


class TestExtractMainContent:
    def test_keeps_main_content_as_markdown(self) -> None:
        page = extract_main_content(_page(), _URL)

        assert isinstance(page, ExtractedPage)
        assert page.markdown.startswith("# Low-level MCP Apps")
        assert "ontoolresult content block" in page.markdown
        assert "lifecycle of the iframe" in page.markdown
        assert (
            "[apps overview](https://docs.example.com/apps/overview)" in page.markdown
        )
        assert "- First bullet item" in page.markdown
        assert "| alpha | 1 |" in page.markdown

    def test_drops_nav_aside_and_footer_links(self) -> None:
        page = extract_main_content(_page(), _URL)

        assert page is not None
        for text in _NAV_TEXTS + _ASIDE_TEXTS + _FOOTER_TEXTS:
            assert text not in page.markdown

    def test_title_is_the_metadata_title(self) -> None:
        page = extract_main_content(_page(), _URL)

        assert page is not None
        assert page.title == "Low-level Apps Guide"

    @pytest.mark.parametrize(
        "html",
        [
            "<html><body><main>ok</main></body></html>",
            "",
            "   ",
        ],
        ids=["thin-main", "empty", "whitespace"],
    )
    def test_thin_or_empty_page_returns_none(self, html: str) -> None:
        assert extract_main_content(html, _URL) is None

    @pytest.mark.parametrize(
        ("non_ws_chars", "kept"),
        [(MIN_EXTRACTED_CHARS - 1, False), (MIN_EXTRACTED_CHARS, True)],
        ids=["299-thin", "300-kept"],
    )
    def test_threshold_counts_only_non_whitespace_chars(
        self, mocker, non_ws_chars: int, kept: bool
    ) -> None:
        # "# T" contributes 2 non-ws chars; whitespace padding must not count.
        body = "# T\n\n" + " \n\t ".join("x" * (non_ws_chars - 2))
        mocker.patch("tree.data.web.web_extract.trafilatura.extract", return_value=body)

        page = extract_main_content(_page(), _URL)

        assert (page is not None) is kept
        assert MIN_EXTRACTED_CHARS == 300


class TestPrependHeading:
    """Option D (task 182): markdown without an H1 gets ``# {page title}`` on top."""

    def test_prepends_page_h1_when_extraction_has_none(self, mocker) -> None:
        mocker.patch(
            "tree.data.web.web_extract.trafilatura.extract",
            return_value="Body paragraph. " * 30,
        )

        page = extract_main_content(_page(h1="Custom HTML Apps"), _URL)

        assert page is not None
        assert page.markdown.startswith("# Custom HTML Apps\n\nBody paragraph.")

    def test_falls_back_to_metadata_title_without_page_h1(self, mocker) -> None:
        mocker.patch(
            "tree.data.web.web_extract.trafilatura.extract",
            return_value="Body paragraph. " * 30,
        )

        page = extract_main_content(_page(h1=None), _URL)

        assert page is not None
        assert page.markdown.startswith("# Low-level Apps Guide\n\n")

    def test_keeps_markdown_that_already_has_an_h1(self, mocker) -> None:
        body = "# Already There\n\n" + "Body paragraph. " * 30
        mocker.patch("tree.data.web.web_extract.trafilatura.extract", return_value=body)

        page = extract_main_content(_page(h1="Other Heading"), _URL)

        assert page is not None
        assert page.markdown == body

    def test_no_title_anywhere_leaves_markdown_unchanged(self, mocker) -> None:
        body = "Body paragraph. " * 30
        mocker.patch("tree.data.web.web_extract.trafilatura.extract", return_value=body)
        html = "<html><body><main><p>" + body + "</p></main></body></html>"

        page = extract_main_content(html, _URL)

        assert page is not None
        assert page.title is None
        assert page.markdown == body


class TestMetadataFailOpen:
    def test_metadata_error_yields_none_title(self, mocker) -> None:
        mocker.patch(
            "tree.data.web.web_extract.trafilatura.extract_metadata",
            side_effect=ValueError("boom"),
        )

        page = extract_main_content(_page(), _URL)

        assert page is not None
        assert page.title is None
        assert page.markdown.startswith("# Low-level MCP Apps")


_STEPS = [
    f"Paragraph {n} explains step {n} of installing the server, configuring its "
    "transport and calling a tool from the client."
    for n in ("one", "two", "three", "four")
]
_NAV = '<nav><a href="/a">Welcome!</a><a href="/b">Installation</a></nav>'


def _mintlify_code(code: str) -> str:
    # Tailwind classes copied from gofastmcp.com: they match trafilatura's discard rules.
    return (
        '<div class="code-block mt-5 not-prose print:print-color-exact">'
        '<div role="presentation" class="size-full py-3.5 px-4 overflow-y-hidden! '
        'base-ui-disable-scrollbar" style="overflow:scroll">'
        f'<div class="font-mono whitespace-pre"><pre class="shiki"><code>{code}'
        "</code></pre></div></div></div>"
    )


_MINTLIFY_LIKE = (
    "<html><head><title>Deploy Guide</title></head><body>"
    + _NAV
    + '<main><div id="content" class="mdx-content prose empty:hidden">'
    "<h1>Deploy Guide</h1>"
    f"<p>{_STEPS[0]}</p>"
    + _mintlify_code("pip install fastmcp\nfastmcp run server.py")
    + f"<p>{_STEPS[1]}</p><p>{_STEPS[2]}</p>"
    + _mintlify_code("fastmcp login")
    + f"<p>{_STEPS[3]}</p></div></main></body></html>"
)

# Blog-like: code is recognised through the ``highlight`` wrappers and the
# ``#primary`` / ``.entry`` main-content markers that the unwrap variant strips.
_BLOG_LIKE = (
    "<html><head><title>A Blog Post</title></head><body>"
    + _NAV
    + '<div id="wrapper"><div id="primary"><div class="entry entryPage">'
    "<h1>A Blog Post</h1>"
    f"<p>{_STEPS[0]}</p>"
    '<div class="highlight highlight-source-shell"><pre>uv run tool.py --flag</pre></div>'
    f"<p>{_STEPS[1]}</p>"
    '<div class="highlight highlight-source-python"><pre>import click\n'
    "print(click)</pre></div>"
    f"<p>{_STEPS[2]}</p><div><pre># /// script\n# requires-python = 3.12</pre></div>"
    f"<p>{_STEPS[3]}</p></div></div>"
    '<div id="secondary"><a href="/x">Recent articles</a></div></div></body></html>'
)


class TestPreUnwrappedVariant:
    """Best-of-two (task 182): retry with ``<pre>`` chains unwrapped when code is lost."""

    def test_mintlify_like_code_wrapper_picks_pre_unwrapped(self) -> None:
        page = extract_main_content(_MINTLIFY_LIKE, _URL)

        assert page is not None
        assert page.variant == "pre_unwrapped"
        assert "```\npip install fastmcp\nfastmcp run server.py\n```" in page.markdown
        assert "Welcome!" not in page.markdown

    def test_blog_like_page_keeps_default_when_unwrapping_loses_code(
        self, mocker
    ) -> None:
        spy = mocker.spy(trafilatura, "extract")

        page = extract_main_content(_BLOG_LIKE, _URL)

        assert spy.call_count == 2  # the unwrap variant ran ...
        assert page is not None
        assert page.variant == "default"  # ... and lost
        assert "```\nimport click\nprint(click)\n```" in page.markdown

    def test_page_without_pre_runs_trafilatura_once(self, mocker) -> None:
        spy = mocker.spy(trafilatura, "extract")

        page = extract_main_content(_page(), _URL)

        assert spy.call_count == 1
        assert page is not None
        assert page.variant == "default"

    def test_tie_keeps_default(self, mocker) -> None:
        body = "# T\n\n" + "Body paragraph. " * 30
        mock_extract = mocker.patch(
            "tree.data.web.web_extract.trafilatura.extract",
            side_effect=[body, body.replace("Body", "Other")],
        )
        html = _page(main_extra="<pre>one</pre>")

        page = extract_main_content(html, _URL)

        assert mock_extract.call_count == 2
        assert page is not None
        assert page.variant == "default"
        assert page.markdown == body

    def test_unwrap_runs_on_a_copy(self, mocker) -> None:
        seen: list[str] = []

        def _extract(html: str, **_: object) -> str:
            seen.append(html)
            return "# T\n\n" + "Body paragraph. " * 30

        mocker.patch(
            "tree.data.web.web_extract.trafilatura.extract", side_effect=_extract
        )
        mocker.patch(
            "tree.data.web.web_extract.trafilatura.extract_metadata", return_value=None
        )

        extract_main_content(_MINTLIFY_LIKE, _URL)

        assert seen[0] == _MINTLIFY_LIKE
        assert "print:print-color-exact" not in seen[1]
        assert 'id="content"' not in seen[1]
        assert "<nav>" in seen[1]  # the rest of the DOM is untouched


class TestPageTitle:
    def test_metadata_title(self) -> None:
        assert page_title(_page()) == "Low-level Apps Guide"

    def test_none_for_empty_html(self) -> None:
        assert page_title("") is None


class TestPrependedHeadingSanitised:
    """Tester follow-up: no ``# # …`` heading, and a bounded one."""

    _BODY = "Body paragraph. " * 30

    @pytest.mark.parametrize(
        "title", ["# evil [x](y)", "##   evil [x](y)", "  #evil [x](y)"]
    )
    def test_leading_hashes_stripped(self, mocker, title: str) -> None:
        mocker.patch(
            "tree.data.web.web_extract.trafilatura.extract", return_value=self._BODY
        )
        html = f"<html><head><title>{title}</title></head><body></body></html>"

        page = extract_main_content(html, _URL)

        assert page is not None
        assert page.markdown.startswith("# evil [x](y)\n\n")

    def test_heading_capped_at_max_chars(self, mocker) -> None:
        mocker.patch(
            "tree.data.web.web_extract.trafilatura.extract", return_value=self._BODY
        )
        html = f"<html><body><h1>{'T' * 1000}</h1></body></html>"

        page = extract_main_content(html, _URL)

        assert page is not None
        heading = page.markdown.split("\n", 1)[0]
        assert heading == "# " + "T" * MAX_HEADING_CHARS
        assert MAX_HEADING_CHARS == 300

    def test_heading_of_only_hashes_is_not_prepended(self, mocker) -> None:
        mocker.patch(
            "tree.data.web.web_extract.trafilatura.extract", return_value=self._BODY
        )
        html = "<html><head><title>###</title></head><body></body></html>"

        page = extract_main_content(html, _URL)

        assert page is not None
        assert page.markdown == self._BODY


class TestTrafilaturaLogNoise:
    def test_trafilatura_errors_do_not_surface(self, caplog) -> None:
        with caplog.at_level(logging.DEBUG):
            assert extract_main_content("\x00\x01 not html", _URL) is None

        assert not [r for r in caplog.records if r.name.startswith("trafilatura")]
