"""The Clean step — the FIRST memory-pipeline stage (ADR-006 §7).

``clean_text`` is a deterministic, idempotent, PURE function: stdlib imports
only, no Prefect, no DB, no config, no ``tree.*`` import (a unit test walks
this module's AST and enforces it). Purity is the point — a future
fine-tuning pipeline imports this exact function, so training text and served
text cannot drift, and a process with neither Mongo nor a Prefect server can
import it with no side effects.

Determinism and idempotence are hard requirements: the extraction pipeline's
task ① caches on Prefect ``INPUTS``, which is only valid while the cleaned
text is a pure function of the raw text.

The four stages run in this order::

    strip_invalid_chars -> normalize_markdown -> drop_boilerplate_lines
                        -> collapse_whitespace

Everything inside a FENCED CODE BLOCK is content, not prose: only the
CRLF->LF rewrite and per-line trailing-whitespace stripping reach it, so
``#comment`` stays a Python comment, four-space indentation stays four spaces
and the two blank lines between two ``def``s stay two. This module owns the ONE
fence definition (:func:`fenced_line_flags` / :func:`fenced_ranges`); the
splitter imports it, so cleaner and chunker can never disagree about where code
starts.

HTML->markdown conversion, language detection, cross-document dedup and PII
scrubbing are explicitly NOT done here.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence

# Voyage's embeddings endpoint 400s on control characters and unpaired
# surrogates (common in HTML->markdown-scraped chunk content). Strip the C0
# controls (except tab/newline/carriage-return), DEL + C1 range, and the
# surrogate range. THE one definition — ``tree.memory.embedding_text`` imports
# ``strip_invalid_chars`` rather than keeping a second copy of this regex.
_INVALID_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")

# ATX heading: 1-6 hashes plus its text, however sloppily spaced ("#Heading").
_ATX_RE = re.compile(r"^(#{1,6})[ \t]*(.*)$")
# Setext underlines. 2+ chars so a lone "-" list bullet is never an underline.
_SETEXT_H1_RE = re.compile(r"^={2,}$")
_SETEXT_H2_RE = re.compile(r"^-{2,}$")
# 3+ newlines (2+ blank lines) collapse to one blank line.
_BLANK_RUN_RE = re.compile(r"\n{3,}")
# Runs of spaces/tabs INSIDE a line; newlines are never touched — paragraph
# structure is what the recursive splitter keys on.
_INTRA_LINE_WS_RE = re.compile(r"[ \t]+")

# Closed pattern list, matched case-insensitively against the WHOLE trimmed
# line (minus trailing punctuation) — never as a substring. Good: a standalone
# "Subscribe" line between paragraphs is dropped. Bad: "You should subscribe
# to good newsletters." must survive untouched.
_BOILERPLATE_PHRASES = frozenset(
    {
        # Cookie / consent banners.
        "accept all cookies",
        "we use cookies",
        # Navigation residue.
        "home",
        "menu",
        "subscribe",
        "share",
        "sign in",
        "log in",
        # Substack residue.
        "thanks for reading",
        "share this post",
    }
)
# Trailing punctuation ignored when matching a phrase ("Thanks for reading!").
_TRAILING_PUNCT = "!.,:;…"

# A line repeated this many times or more is a header/footer, not content —
# but only above the length floor, so "OK" or a repeated list marker is safe.
_REPEAT_MIN_COUNT = 3
_REPEAT_MIN_LENGTH = 20

# Opening / closing marker of a fenced code block. A "#comment" line inside a
# fence is Python or shell syntax, not a sloppy heading, and its indentation is
# load-bearing — the corpus this repo ingests is code-heavy, and a cleaned
# sample is what ``search_memory`` hands an agent to quote.
_CODE_FENCE_MARKERS = ("```", "~~~")


def clean_text(text: str) -> str:
    """Run the full Clean step over raw document content.

    Idempotent (``clean_text(clean_text(x)) == clean_text(x)``) and
    deterministic — no randomness, no clock, no config read. ``""`` in,
    ``""`` out; ``None`` is not accepted (callers pass
    ``document.content or ""``).
    """

    return collapse_whitespace(
        drop_boilerplate_lines(normalize_markdown(strip_invalid_chars(text)))
    )


def strip_invalid_chars(text: str) -> str:
    """Remove control characters and lone surrogates; keep ``\\n`` and ``\\t``."""

    return _INVALID_CHARS_RE.sub("", text)


def fenced_line_flags(lines: Sequence[str]) -> list[bool]:
    """``True`` for every line inside (or delimiting) a fenced code block.

    THE fence definition — :func:`fenced_ranges` is a character-offset view of
    this same scan, and :mod:`tree.memory.rag.chunking` imports that view so a
    heading and a shell comment are told apart the same way in both stages.

    Per CommonMark an UNCLOSED fence runs to the end of the document, so every
    later line is code too.
    """

    flags = [False] * len(lines)
    opened_at: int | None = None
    for index, line in enumerate(lines):
        if not line.strip().startswith(_CODE_FENCE_MARKERS):
            continue
        if opened_at is None:
            opened_at = index
        else:
            flags[opened_at : index + 1] = [True] * (index + 1 - opened_at)
            opened_at = None
    if opened_at is not None:
        flags[opened_at:] = [True] * (len(lines) - opened_at)
    return flags


def fenced_ranges(text: str) -> list[tuple[int, int]]:
    """Half-open character ranges covered by fenced code blocks.

    The offset view of :func:`fenced_line_flags`; adjacent blocks merge into one
    range, which is irrelevant to every caller (all of them ask "is this offset
    inside a fence?").
    """

    lines = text.splitlines(keepends=True)
    ranges: list[tuple[int, int]] = []
    position = 0
    run_start: int | None = None
    for line, fenced in zip(lines, fenced_line_flags(lines), strict=True):
        if fenced and run_start is None:
            run_start = position
        elif not fenced and run_start is not None:
            ranges.append((run_start, position))
            run_start = None
        position += len(line)
    if run_start is not None:
        ranges.append((run_start, position))
    return ranges


def normalize_markdown(text: str) -> str:
    """Normalise line endings, headings and blank-line runs.

    CRLF/CR become LF; every line loses its trailing whitespace; setext
    headings become ATX (``Title\\n=====`` -> ``# Title``, ``-----`` -> ``## ``);
    sloppy ATX headings gain their space (``#Heading`` -> ``# Heading``); runs
    of 3+ newlines collapse to 2 (one blank line).

    Fenced lines get the line-ending rewrite and the trailing ``rstrip`` only:
    inside a fence ``#comment`` is code and a blank-line run is formatting.
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    out: list[str] = []
    previous_fenced = False
    for raw_line, fenced in zip(lines, fenced_line_flags(lines), strict=True):
        line = raw_line.rstrip()
        if fenced:
            out.append(line)
            previous_fenced = True
            continue
        previous = out[-1] if out else ""
        # A setext underline retitles the line ABOVE it — but only when that
        # line is plain text (a blank line means a thematic break, a line
        # already starting with "#" is a heading, not a setext title, and a
        # fenced line is code).
        if previous and not previous_fenced and not previous.startswith("#"):
            if _SETEXT_H1_RE.match(line):
                out[-1] = f"# {previous}"
                continue
            if _SETEXT_H2_RE.match(line):
                out[-1] = f"## {previous}"
                continue
        atx = _ATX_RE.match(line)
        out.append(f"{atx.group(1)} {atx.group(2)}".rstrip() if atx else line)
        previous_fenced = False
    return _collapse_blank_runs("\n".join(out))


def drop_boilerplate_lines(text: str) -> str:
    """Drop cookie/nav/Substack residue lines and repeated header/footer lines.

    A line goes when its whole trimmed form (trailing punctuation ignored,
    case-insensitive) is in the closed phrase list, or when that identical
    trimmed form is at least ``20`` characters long and occurs ``3`` or more
    times in the document. Fenced lines are never dropped and never counted —
    a ``raise ValueError(...)`` repeated three times is a code sample, not a
    footer.
    """

    lines = text.split("\n")
    stripped = [line.strip() for line in lines]
    fenced = fenced_line_flags(lines)

    counts = Counter(
        s
        for s, in_fence in zip(stripped, fenced, strict=True)
        if not in_fence and len(s) >= _REPEAT_MIN_LENGTH
    )
    repeated = {s for s, n in counts.items() if n >= _REPEAT_MIN_COUNT}

    kept = [
        line
        for line, s, in_fence in zip(lines, stripped, fenced, strict=True)
        if in_fence or (not _is_boilerplate_phrase(s) and s not in repeated)
    ]
    # Dropping a line between two blank ones would otherwise leave a 3+ newline
    # run that ``normalize_markdown`` already forbids — collapsing here is what
    # keeps ``clean_text`` idempotent.
    return _collapse_blank_runs("\n".join(kept))


def collapse_whitespace(text: str) -> str:
    """Collapse runs of spaces/tabs inside a line to one space; keep newlines.

    Skips fenced code blocks — collapsing a 4-space indent to one space turns a
    Python sample into a syntax error.
    """

    return _outside_fences(text, lambda part: _INTRA_LINE_WS_RE.sub(" ", part))


def _is_boilerplate_phrase(stripped_line: str) -> bool:
    """Whole-line (not substring) match against the closed phrase list."""

    return (
        stripped_line.rstrip(_TRAILING_PUNCT).strip().casefold() in _BOILERPLATE_PHRASES
    )


def _collapse_blank_runs(text: str) -> str:
    """Collapse 3+ consecutive newlines to 2 (at most one blank line).

    Outside fences only: PEP 8 puts TWO blank lines between top-level ``def``s.
    """

    return _outside_fences(text, lambda part: _BLANK_RUN_RE.sub("\n\n", part))


def _outside_fences(text: str, rewrite: Callable[[str], str]) -> str:
    """Apply ``rewrite`` to every part of ``text`` that is NOT fenced code.

    Fenced ranges start at a fence line's first character, so no run of spaces
    or newlines is ever split across the boundary — rewriting the parts
    separately gives the same result as rewriting a fence-free document.
    """

    ranges = fenced_ranges(text)
    if not ranges:
        return rewrite(text)

    parts: list[str] = []
    cursor = 0
    for start, end in ranges:
        parts.append(rewrite(text[cursor:start]))
        parts.append(text[start:end])
        cursor = end
    parts.append(rewrite(text[cursor:]))
    return "".join(parts)
