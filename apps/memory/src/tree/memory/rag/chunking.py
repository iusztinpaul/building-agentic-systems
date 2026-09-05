"""Two-level document splitting: **Parent chunk**s and **Child chunk**s (ADR-006 §6).

``split_document`` is the ONE entry point. It splits the cleaned document text
into parents with ``config.parent``, then re-splits each parent's own text into
children with ``config.child`` — the SAME strategy at both levels, so a child
can never cross a parent boundary.

Two strategies:

* ``fixed_tokens`` — the Chapter-4 sliding token window (the algorithm the
  pre-ADR-006 single-level splitter used). Structure blind: every parent's
  ``heading_path`` is empty.
* ``recursive`` — split on the first separator level that actually cuts the
  range: markdown ATX headings (tracking the heading stack) -> blank-line
  paragraphs -> sentences -> raw tokens. Adjacent pieces are merged greedily
  while the merged text stays within ``size`` tokens; a single piece bigger than
  ``size`` recurses to the next separator level.

Every chunk under ``recursive`` is a CONTIGUOUS SLICE of the source text (the
splitter merges character spans, never re-joins strings), which is what makes
"every child is a substring of its parent" true even with overlap on.

Determinism is a hard requirement, not a nicety: the extraction flow caches task
① on Prefect ``INPUTS``, so the same (text, config) must always produce the same
chunks. No randomness, no dict-ordering dependence, no clock.

NOT LangChain: this is an in-house port (ADR-006 rejects the dependency).
"""

from __future__ import annotations

import re

import tiktoken

from tree.config.app_config import ChunkingConfig, ChunkLevelConfig
from tree.memory.rag.types import ChildChunk, ParentChunk

# ONE module-level encoder (same pattern as ``extraction/core.py``): building it
# costs a BPE-file load, and every size check goes through it.
_ENCODER = tiktoken.get_encoding("cl100k_base")

# ``disallowed_special=()`` because scraped documents legitimately contain the
# literal text "<|endoftext|>", and tiktoken's default raises on it. Chunking
# must never crash on a document's content.
_NO_SPECIAL: tuple[str, ...] = ()

# Markdown ATX heading: 1-6 hashes, a space, then the heading text.
# ``clean_text`` has already normalised "#Heading" to "# Heading" and CRLF to
# LF by the time the pipeline calls us.
_ATX_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$", re.MULTILINE)
# Opening / closing marker of a fenced code block. A "# comment" line inside a
# fence is shell syntax, not a heading — treating it as one used to leak
# "# ensure MONGO_SCHEME=mongodb+srv" into a real tutorial's heading path.
_CODE_FENCE_MARKERS = ("```", "~~~")
# A blank line (possibly carrying spaces/tabs) separates two paragraphs.
_PARAGRAPH_SEP_RE = re.compile(r"\n[ \t]*\n")
# End of sentence: ".", "!" or "?" followed by whitespace.
_SENTENCE_SEP_RE = re.compile(r"(?<=[.!?])\s+")

# A piece / chunk of the source text: (start, end, heading_path). Half-open
# character offsets into the WHOLE text, so merging two adjacent pieces is just
# taking the first start and the last end — separators included, no re-joining.
_Span = tuple[int, int, tuple[str, ...]]

_HEADINGS, _PARAGRAPHS, _SENTENCES, _TOKENS = range(4)
_LAST_LEVEL = _TOKENS


def split_document(text: str, config: ChunkingConfig) -> list[ParentChunk]:
    """Split ``text`` into **Parent chunk**s, each holding its **Child chunk**s.

    Whitespace-only input yields ``[]`` — an empty document has nothing to
    embed, and writing a blank chunk row would poison hybrid search.
    """

    if not text.strip():
        return []

    if config.strategy == "fixed_tokens":
        parent_texts = [(content, []) for content in _fixed_window(text, config.parent)]
    else:
        parent_texts = [
            (text[start:end], list(heading_path))
            for start, end, heading_path in _recursive_spans(text, config.parent)
        ]

    parents: list[ParentChunk] = []
    for content, heading_path in parent_texts:
        if not content.strip():
            continue
        children = _split_children(content, config)
        parents.append(
            ParentChunk(
                index=len(parents),
                content=content,
                heading_path=heading_path,
                children=[
                    ChildChunk(index=index, content=child)
                    for index, child in enumerate(children)
                ],
            )
        )
    return parents


# ---------------------------------------------------------------------------
# Level helpers
# ---------------------------------------------------------------------------


def _split_children(parent_content: str, config: ChunkingConfig) -> list[str]:
    """Split ONE parent's text into child texts with ``config.child``."""

    if _token_count(parent_content) <= config.child.size:
        # A parent that already fits the child budget has exactly one child
        # equal to it — no re-tokenisation round trip, no drift.
        return [parent_content]

    if config.strategy == "fixed_tokens":
        texts = _fixed_window(parent_content, config.child)
    else:
        texts = [
            parent_content[start:end]
            for start, end, _ in _recursive_spans(parent_content, config.child)
        ]
    return [text for text in texts if text.strip()]


def _fixed_window(text: str, level: ChunkLevelConfig) -> list[str]:
    """The Chapter-4 sliding token window (the pre-ADR-006 algorithm).

    ``overlap < size`` is enforced by :class:`ChunkLevelConfig`, so the stride
    is always positive and the loop always terminates.
    """

    tokens = _ENCODER.encode(text, disallowed_special=_NO_SPECIAL)
    if not tokens:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        chunks.append(_ENCODER.decode(tokens[start : start + level.size]))
        start += level.size - level.overlap
    return chunks


def _recursive_spans(text: str, level: ChunkLevelConfig) -> list[_Span]:
    """Structure-aware split of the whole ``text`` at ONE level's budget."""

    spans: list[_Span] = []
    _emit_range(
        text=text,
        start=0,
        end=len(text),
        heading_path=(),
        level=level,
        separator=_HEADINGS,
        out=spans,
    )
    return spans


def _emit_range(
    *,
    text: str,
    start: int,
    end: int,
    heading_path: tuple[str, ...],
    level: ChunkLevelConfig,
    separator: int,
    out: list[_Span],
) -> None:
    """Append the chunks of ``text[start:end]`` to ``out``, greedily merged.

    ``out`` is threaded through the recursion (rather than returned) so the
    overlap seed can always read the chunk that was emitted immediately before
    this one — including across a recursion into a finer separator level.
    """

    if start >= end:
        return

    pieces = _pieces(text, start, end, heading_path, separator, level.size)
    if len(pieces) < 2 and separator < _LAST_LEVEL:
        # This separator does not actually cut the range — drop to the next one,
        # carrying whatever heading path this level established.
        _emit_range(
            text=text,
            start=start,
            end=end,
            heading_path=pieces[0][2] if pieces else heading_path,
            level=level,
            separator=separator + 1,
            out=out,
        )
        return

    buffer: _Span | None = None
    for piece_start, piece_end, piece_path in pieces:
        oversized = _span_tokens(text, piece_start, piece_end) > level.size
        if oversized and separator < _LAST_LEVEL:
            # Too big on its own: flush what we have and cut it finer. At the
            # token level there is no finer separator, so an oversized piece
            # (a lone token that re-encodes wider than ``size``) falls through
            # and is emitted as its own chunk rather than recursing forever.
            _flush(text, buffer, out)
            buffer = None
            _emit_range(
                text=text,
                start=piece_start,
                end=piece_end,
                heading_path=piece_path,
                level=level,
                separator=separator + 1,
                out=out,
            )
            continue

        if buffer is None:
            buffer = (
                _overlap_start(text, out, piece_start, piece_end, level),
                piece_end,
                piece_path,
            )
        elif _span_tokens(text, buffer[0], piece_end) <= level.size:
            buffer = (buffer[0], piece_end, buffer[2])
        else:
            _flush(text, buffer, out)
            buffer = (
                _overlap_start(text, out, piece_start, piece_end, level),
                piece_end,
                piece_path,
            )

    _flush(text, buffer, out)


def _flush(text: str, buffer: _Span | None, out: list[_Span]) -> None:
    """Append ``buffer`` to ``out``, whitespace-trimmed, unless it is blank.

    Trimming happens HERE, not at the call site, so every emitted span is
    exactly the text the caller will store. That matters for the size bound:
    a leading space changes the tokenisation of the word after it (" retrieval"
    is 1 cl100k token, "retrieval" is 2), so a span measured untrimmed and
    stored trimmed can silently exceed ``size``.

    A whitespace-only chunk carries nothing to embed, so it is dropped instead
    of becoming a blank row.
    """

    if buffer is None:
        return
    start, end = _trim(text, buffer[0], buffer[1])
    if start < end:
        out.append((start, end, buffer[2]))


def _overlap_start(
    text: str,
    out: list[_Span],
    piece_start: int,
    piece_end: int,
    level: ChunkLevelConfig,
) -> int:
    """Where a chunk beginning at ``piece_start`` starts once overlap is applied.

    The carried tail counts against ``size`` (as LangChain's splitter does and
    as the property test asserts): if the previous chunk's last ``overlap``
    tokens would push this chunk over budget, the chunk starts at
    ``piece_start`` with no overlap rather than exceeding ``size``.
    """

    if level.overlap <= 0 or not out:
        return piece_start

    previous_start, previous_end, _ = out[-1]
    if previous_end > piece_start:
        # Defensive: the previous chunk already covers this piece's start.
        return piece_start

    offsets = _token_char_offsets(text[previous_start:previous_end])
    tail_index = max(0, len(offsets) - 1 - level.overlap)
    candidate = previous_start + offsets[tail_index]
    if candidate >= piece_start:
        return piece_start
    if _span_tokens(text, candidate, piece_end) > level.size:
        return piece_start
    return candidate


# ---------------------------------------------------------------------------
# Separator levels — each returns spans covering [start, end) in order
# ---------------------------------------------------------------------------


def _pieces(
    text: str,
    start: int,
    end: int,
    heading_path: tuple[str, ...],
    separator: int,
    size: int,
) -> list[_Span]:
    if separator == _HEADINGS:
        return _heading_pieces(text, start, end, heading_path)
    if separator == _PARAGRAPHS:
        return _regex_pieces(text, start, end, heading_path, _PARAGRAPH_SEP_RE)
    if separator == _SENTENCES:
        return _regex_pieces(text, start, end, heading_path, _SENTENCE_SEP_RE)
    return _token_pieces(text, start, end, heading_path, size)


def _heading_pieces(
    text: str, start: int, end: int, heading_path: tuple[str, ...]
) -> list[_Span]:
    """One piece per ATX section (heading line + body), plus any preamble.

    The heading path of a section is the stack of heading TEXTS from ``#`` down
    to that section's own level, so ``# Memory`` then ``## Parent retrieval``
    yields ``("Memory", "Parent retrieval")``.
    """

    window = text[start:end]
    fenced = _fenced_ranges(window)
    matches = [
        match
        for match in _ATX_HEADING_RE.finditer(window)
        if not any(
            fence_start <= match.start() < fence_end
            for fence_start, fence_end in fenced
        )
    ]
    if not matches:
        return []

    # Levels are unknown for an inherited path (children re-split a parent whose
    # text may start mid-hierarchy); assume one level per entry — the resulting
    # paths are discarded at the child level anyway.
    stack: list[tuple[int, str]] = [
        (depth + 1, title) for depth, title in enumerate(heading_path)
    ]

    pieces: list[_Span] = []
    if window[: matches[0].start()].strip():
        pieces.append((start, start + matches[0].start(), heading_path))

    for position, match in enumerate(matches):
        piece_end = (
            matches[position + 1].start()
            if position + 1 < len(matches)
            else len(window)
        )
        heading_level = len(match.group(1))
        while stack and stack[-1][0] >= heading_level:
            stack.pop()
        stack.append((heading_level, match.group(2).strip()))
        pieces.append(
            (start + match.start(), start + piece_end, tuple(t for _, t in stack))
        )
    return pieces


def _fenced_ranges(window: str) -> list[tuple[int, int]]:
    """Character ranges covered by fenced code blocks (backticks or tildes).

    Per CommonMark, an UNCLOSED fence runs to the end of the document, so no
    later ``#`` line can be a heading either. Paragraph and sentence splitting
    stay fence-blind on purpose: a blank line inside a fence only ever shifts a
    chunk boundary, whereas a fake heading corrupts every child's contextual
    header downstream.
    """

    ranges: list[tuple[int, int]] = []
    opened_at: int | None = None
    position = 0
    for line in window.splitlines(keepends=True):
        if line.strip().startswith(_CODE_FENCE_MARKERS):
            if opened_at is None:
                opened_at = position
            else:
                ranges.append((opened_at, position + len(line)))
                opened_at = None
        position += len(line)
    if opened_at is not None:
        ranges.append((opened_at, len(window)))
    return ranges


def _regex_pieces(
    text: str,
    start: int,
    end: int,
    heading_path: tuple[str, ...],
    separator: re.Pattern[str],
) -> list[_Span]:
    """Cut AFTER each separator match, so a piece keeps its trailing whitespace
    and the NEXT piece starts on its first real character."""

    window = text[start:end]
    cuts = [match.end() for match in separator.finditer(window)]
    if not cuts:
        return []

    pieces: list[_Span] = []
    previous = 0
    for cut in cuts:
        if cut > previous:
            pieces.append((start + previous, start + cut, heading_path))
            previous = cut
    if previous < len(window):
        pieces.append((start + previous, end, heading_path))
    return pieces


def _token_pieces(
    text: str, start: int, end: int, heading_path: tuple[str, ...], size: int
) -> list[_Span]:
    """The last resort: hard windows of ``size`` tokens, on character boundaries.

    A window is shrunk until the text it covers RE-ENCODES to ``size`` tokens or
    fewer — cutting mid-word can re-tokenise to one token more than the slice we
    took, and the ``<= size`` guarantee is what downstream embedding batching
    relies on.
    """

    offsets = _token_char_offsets(text[start:end])
    token_total = len(offsets) - 1
    pieces: list[_Span] = []
    cursor = 0
    while cursor < token_total:
        stop = min(cursor + size, token_total)
        while (
            stop > cursor + 1
            and _span_tokens(text, start + offsets[cursor], start + offsets[stop])
            > size
        ):
            stop -= 1
        pieces.append((start + offsets[cursor], start + offsets[stop], heading_path))
        cursor = stop
    return pieces


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _token_count(text: str) -> int:
    return len(_ENCODER.encode(text, disallowed_special=_NO_SPECIAL))


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    """Shrink a span past its leading/trailing whitespace."""

    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _span_tokens(text: str, start: int, end: int) -> int:
    """Token count of the text a span will ACTUALLY emit (i.e. trimmed)."""

    trimmed_start, trimmed_end = _trim(text, start, end)
    return _token_count(text[trimmed_start:trimmed_end])


def _token_char_offsets(text: str) -> list[int]:
    """Character offset of every token boundary — ``len(tokens) + 1`` entries.

    ``offsets[i]`` is where token ``i`` starts and ``offsets[-1] == len(text)``.
    Computed through UTF-8 byte lengths because a single character (an emoji,
    an accented letter) can be split across two tokens; such a boundary is
    snapped FORWARD to the next character so every offset is a valid slice
    index and no chunk can ever contain a replacement character.
    """

    tokens = _ENCODER.encode(text, disallowed_special=_NO_SPECIAL)
    char_at_byte: dict[int, int] = {}
    byte_position = 0
    for char_index, char in enumerate(text):
        char_at_byte[byte_position] = char_index
        byte_position += len(char.encode("utf-8"))
    char_at_byte[byte_position] = len(text)

    offsets = [0]
    consumed = 0
    for token_bytes in _ENCODER.decode_tokens_bytes(tokens):
        consumed = min(consumed + len(token_bytes), byte_position)
        while consumed not in char_at_byte:
            consumed += 1
        offsets.append(char_at_byte[consumed])
    return offsets
