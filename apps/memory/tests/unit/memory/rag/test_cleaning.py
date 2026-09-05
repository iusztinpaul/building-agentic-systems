"""Unit tests for the Clean step (``tree.memory.rag.cleaning``).

Covers the four stages (``strip_invalid_chars``, ``normalize_markdown``,
``drop_boilerplate_lines``, ``collapse_whitespace``), their composition in
``clean_text``, and the two properties the pipeline depends on: stdlib-only
purity (so a fine-tuning process can import the SAME function) and
idempotence/determinism (so Prefect ``INPUTS`` caching downstream stays valid).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from tree.memory.rag import cleaning
from tree.memory.rag.cleaning import (
    clean_text,
    collapse_whitespace,
    drop_boilerplate_lines,
    normalize_markdown,
    strip_invalid_chars,
)


# A code-heavy article: the corpus this repo ingests (decodingai.com and other
# technical Substacks) is full of these. Everything BETWEEN the fences has to
# survive byte-for-byte; the "#Heading" outside still gets normalised.
_FENCED_ARTICLE = """#Heading

Prose   with   runs.

```python
#comment
def loop():
    if True:
        return  "x"


def other():
    pass
```

Closing   prose.
"""


# Every text the idempotence / determinism properties are checked against.
# Add a fixture here whenever a new cleaning behaviour is introduced.
_FIXTURES: list[str] = [
    "",
    "plain text",
    "a\x00b\ud800c",
    "Title\n=====\n\ntext\r\nmore",
    "#Heading\n\nbody",
    "Agents\r\n======\r\n\r\n\r\n\r\nBody   text",
    "Intro\nAccept all cookies\nBody",
    "Intro\n\nSubscribe\n\nMenu\n\nBody",
    "Para one.\n\nThanks for reading!\nShare this post\n",
    "You should subscribe to good newsletters.\nHome\nBody   text\t\ttail",
    ("A repeated footer line!!\nbody 1\n" * 3) + "tail",
    "a\n\n\n\n\n\n\nb",
    "Sub\n-----\n\n- item\n- item\n",
    "text\n\n-----\n\nmore",
    _FENCED_ARTICLE,
    "# Doc\n\n```\n#unclosed comment\n    indented    code\n",
    "```\na\n\n\nb\n```\n\n\n\ntail",
]


class TestModulePurity:
    """The Clean step must stay importable from a process with no Mongo/Prefect."""

    def test_cleaning_module_imports_only_stdlib(self) -> None:
        # Arrange: parse the module source rather than trusting the import to
        # fail loudly — a `tree.*` import would succeed here yet drag config,
        # Mongo and Prefect into a fine-tuning process.
        source_path = Path(cleaning.__file__)
        module_ast = ast.parse(source_path.read_text(encoding="utf-8"))

        # Act: collect the root module of every import statement.
        roots: set[str] = set()
        for node in ast.walk(module_ast):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "no relative imports in the pure module"
                assert node.module is not None
                roots.add(node.module.split(".")[0])

        # Assert: only stdlib roots, and none of the banned heavyweight ones.
        assert roots <= {"re", "collections", "typing", "__future__"}
        assert roots <= set(sys.stdlib_module_names)
        assert roots.isdisjoint({"tree", "prefect", "pymongo", "beanie"})


class TestStripInvalidChars:
    """Adversarial: strip exactly the chars Voyage 400s on, nothing else."""

    def test_removes_control_chars_and_lone_surrogates(self) -> None:
        assert strip_invalid_chars("a\x00b\ud800c") == "abc"

    def test_keeps_newline_and_tab(self) -> None:
        assert strip_invalid_chars("a\nb\tc") == "a\nb\tc"

    def test_strips_each_invalid_class(self) -> None:
        # Arrange: one char from each stripped class — C0 (NUL, BEL, VT, FF),
        # DEL, C1 (0x80, 0x9f), and an unpaired surrogate (0xd800).
        text = "x\x00\x07\x0b\x0c\x1f\x7f\x80\x9f\ud800y"

        assert strip_invalid_chars(text) == "xy"

    def test_preserves_legitimate_unicode(self) -> None:
        # Smart quotes, emoji, accents and a tab are legitimate content.
        text = "café “smart” \U0001f600\taccenté"

        assert strip_invalid_chars(text) == text


class TestNormalizeMarkdown:
    def test_converts_setext_h1_and_crlf(self) -> None:
        assert normalize_markdown("Title\n=====\n\ntext\r\nmore") == (
            "# Title\n\ntext\nmore"
        )

    def test_converts_setext_h2_dashes(self) -> None:
        assert normalize_markdown("Sub\n-----\nbody") == "## Sub\nbody"

    def test_adds_space_after_atx_hashes(self) -> None:
        assert normalize_markdown("#Heading") == "# Heading"

    def test_keeps_already_normalised_atx_heading(self) -> None:
        assert normalize_markdown("## Heading") == "## Heading"

    def test_collapses_five_blank_lines_to_two_newlines(self) -> None:
        # Arrange: "a", 5 blank lines, "b".
        text = "a" + "\n" * 6 + "b"

        # Assert: exactly two newlines survive (one blank line) — the same
        # collapse the "\r\n\r\n\r\n\r\n" scraped-page fixture relies on.
        assert normalize_markdown(text) == "a\n\nb"

    def test_strips_trailing_whitespace_per_line(self) -> None:
        assert normalize_markdown("a   \nb\t\n") == "a\nb\n"

    def test_keeps_thematic_break_after_blank_line(self) -> None:
        # A dash run preceded by a blank line is a rule, not a setext underline.
        assert normalize_markdown("text\n\n-----\n\nmore") == "text\n\n-----\n\nmore"


class TestDropBoilerplateLines:
    def test_drops_cookie_banner_line(self) -> None:
        assert drop_boilerplate_lines("Intro\nAccept all cookies\nBody") == (
            "Intro\nBody"
        )

    @pytest.mark.parametrize(
        "line",
        [
            "We use cookies",
            "Home",
            "Menu",
            "Subscribe",
            "Share",
            "Sign in",
            "Log in",
            "Thanks for reading",
            "Thanks for reading!",
            "Share this post",
            "  subscribe  ",
        ],
    )
    def test_drops_each_closed_pattern(self, line: str) -> None:
        assert drop_boilerplate_lines(f"Intro\n{line}\nBody") == "Intro\nBody"

    def test_keeps_paragraph_that_merely_contains_a_pattern_word(self) -> None:
        # The patterns match the WHOLE trimmed line only.
        text = "You should subscribe to good newsletters."

        assert drop_boilerplate_lines(text) == text

    def test_drops_line_repeated_three_times(self) -> None:
        # Arrange: a 21-char line (over the 20-char floor) three times over.
        repeated = "Copyright ACME 2026!!"
        assert len(repeated) == 21
        text = f"{repeated}\npara one\n{repeated}\npara two\n{repeated}"

        assert drop_boilerplate_lines(text) == "para one\npara two"

    def test_keeps_line_repeated_twice(self) -> None:
        # Threshold is 3 — two occurrences are ordinary content.
        repeated = "Copyright ACME 2026!!"
        text = f"{repeated}\npara one\n{repeated}"

        assert drop_boilerplate_lines(text) == text

    def test_keeps_short_repeated_line(self) -> None:
        # Below the 20-char floor, repetition means nothing (list markers, "OK").
        text = "hello\npara\nhello\npara two\nhello"

        assert drop_boilerplate_lines(text) == text


class TestCollapseWhitespace:
    def test_collapses_space_and_tab_runs_without_touching_newlines(self) -> None:
        assert collapse_whitespace("a   b\t\tc\n\nd") == "a b c\n\nd"

    def test_leaves_single_spaces_alone(self) -> None:
        assert collapse_whitespace("a b\nc d") == "a b\nc d"


class TestCleanText:
    def test_empty_string_returns_empty(self) -> None:
        assert clean_text("") == ""

    @pytest.mark.parametrize("text", _FIXTURES)
    def test_is_idempotent(self, text: str) -> None:
        once = clean_text(text)

        assert clean_text(once) == once

    @pytest.mark.parametrize("text", _FIXTURES)
    def test_is_deterministic_across_calls(self, text: str) -> None:
        # The Prefect INPUTS cache on task ① is only valid if the cleaned text
        # is a pure function of the raw text.
        assert clean_text(text) == clean_text(text)

    def test_cleans_scraped_page_with_crlf_and_setext_heading(self) -> None:
        # User story: a scraped page becomes clean markdown.
        raw = "Agents\r\n======\r\n\r\n\r\n\r\nBody   text"

        assert clean_text(raw) == "# Agents\n\nBody text"

    def test_drops_substack_footer_and_preserves_body(self) -> None:
        # User story: an ingested Substack article loses its footer boilerplate.
        raw = "The body paragraph stays.\n\nThanks for reading!\nShare this post\n"

        cleaned = clean_text(raw)

        assert "Thanks for reading" not in cleaned
        assert "Share this post" not in cleaned
        assert "The body paragraph stays." in cleaned

    def test_applies_all_four_stages(self) -> None:
        # Control char + CRLF + setext + boilerplate line + whitespace run.
        raw = "Title\r\n=====\r\nSubscribe\r\nBo\x00dy   text"

        assert clean_text(raw) == "# Title\nBody text"


class TestFencedCodeBlocks:
    """The Clean step must not rewrite code (#112 Issue 3).

    ``#comment`` is Python syntax, not a sloppy ATX heading, and indentation is
    load-bearing — before this fix every fenced sample came out of memory
    un-runnable, and that text is what ``search_memory`` hands the agent to
    quote.
    """

    def test_code_inside_a_fence_survives_byte_for_byte(self) -> None:
        cleaned = clean_text(_FENCED_ARTICLE)

        fenced = cleaned.split("```python\n")[1].split("```")[0]
        assert fenced == (
            "#comment\n"
            "def loop():\n"
            "    if True:\n"
            '        return  "x"\n'
            "\n"
            "\n"
            "def other():\n"
            "    pass\n"
        )

    def test_prose_outside_the_fence_is_still_normalised(self) -> None:
        cleaned = clean_text(_FENCED_ARTICLE)

        assert cleaned.startswith("# Heading")
        assert "Prose with runs." in cleaned
        assert "Closing prose." in cleaned

    def test_hash_comment_in_a_fence_is_not_turned_into_a_heading(self) -> None:
        assert normalize_markdown("```\n#comment\n```") == "```\n#comment\n```"

    def test_indentation_in_a_fence_is_not_collapsed(self) -> None:
        text = "```\n    deep    indent\n```"

        assert collapse_whitespace(text) == text

    def test_setext_underline_inside_a_fence_is_not_a_heading(self) -> None:
        text = "```\nTitle\n=====\n```"

        assert normalize_markdown(text) == text

    def test_blank_lines_inside_a_fence_are_not_collapsed(self) -> None:
        # PEP 8 puts two blank lines between top-level defs; collapsing them
        # rewrites the sample.
        text = "```\na\n\n\nb\n```"

        assert normalize_markdown(text) == text

    def test_repeated_code_line_inside_a_fence_is_not_dropped_as_boilerplate(
        self,
    ) -> None:
        line = "        raise ValueError(message)"
        text = "```python\n" + f"{line}\n" * 3 + "```"

        assert drop_boilerplate_lines(text) == text

    def test_unclosed_fence_protects_the_rest_of_the_document(self) -> None:
        # CommonMark: an unclosed fence runs to the end of the document — the
        # SAME rule the splitter applies, from the SAME helper.
        text = "```\n#comment\n    indented\n"

        assert clean_text(text) == text

    def test_blank_run_after_a_closing_fence_collapses_to_one_blank_line(self) -> None:
        # The closing fence line owns the "\n" that ends it. Keeping that
        # newline inside the fenced range hid it from the blank-run collapse,
        # so the SAME 3-newline run left two blank lines after a fence and one
        # after a paragraph.
        assert clean_text("```\ncode\n```\n\n\nafter") == "```\ncode\n```\n\nafter"
        assert clean_text("para\n\n\nafter") == "para\n\nafter"

    def test_collapsing_at_the_boundary_leaves_the_code_byte_identical(self) -> None:
        # Only the closing fence's terminator moves outside the range, so the
        # body keeps its two blank lines and the fence keeps its own line.
        cleaned = clean_text("```\na\n\n\nb\n```\n\n\n\ntail")

        assert cleaned.split("```")[1] == "\na\n\n\nb\n"
        assert cleaned == "```\na\n\n\nb\n```\n\ntail"
