"""Unit tests for the two-level splitter (``tree.memory.rag.chunking``).

Covers both strategies (ADR-006 §6): ``fixed_tokens`` (the Chapter-4 sliding
token window) and ``recursive`` (headings -> paragraphs -> sentences -> tokens),
the parent/child hierarchy invariants every downstream stage relies on
(children never cross a parent, ``index`` is dense, contents stay token-bounded)
and determinism — the extraction flow caches on Prefect ``INPUTS``, which is only
valid while chunking is a pure function of the text plus the config.
"""

from __future__ import annotations

import json
import random

import pytest

from tree.config.app_config import ChunkingConfig
from tree.memory.rag import chunking as chunking_module
from tree.memory.rag.chunking import _ENCODER, split_document
from tree.memory.rag.cleaning import fenced_ranges
from tree.memory.rag.types import ParentChunk

_STRATEGIES = ["fixed_tokens", "recursive"]

# The AC-5 fixture: three sections, an h2 nested under the first h1.
_HEADED_TEXT = "# A\n\npara1\n\n## B\n\npara2\n\n# C\n\npara3"

_WORDS = [
    "memory",
    "agent",
    "retrieval",
    "parent",
    "child",
    "chunk",
    "graph",
    "vector",
    "token",
    "ontology",
    "pipeline",
    "embedding",
]


# Mixed CJK + ASCII: 3-byte characters whose token boundaries land mid-character.
_CJK_TEXT = (
    "人工知能はメモリを必要とします。知識グラフは関係を保存します。"
    "エージェントは検索で文脈を取り戻す。RAG と GraphRAG は別のモードです。"
) * 8


def _ntok(text: str) -> int:
    return len(_ENCODER.encode(text, disallowed_special=()))


def _assert_within_budget(chunks: list[str], *, size: int) -> None:
    """Every chunk re-encodes to ``<= size`` tokens (#112 Issue 8).

    The ONE exception is a chunk of a single character that alone costs more
    than ``size`` tokens (a 3-token 🧠 under ``child.size=2``): splitting it
    would mean storing U+FFFD, which the same AC forbids. Over-budget chunks
    are asserted to be exactly that case, so the carve-out cannot hide a real
    budget violation.
    """

    assert chunks
    over = [chunk for chunk in chunks if _ntok(chunk) > size]
    assert all(len(chunk) == 1 for chunk in over), (
        f"chunks over the {size}-token budget that are not a single "
        f"indivisible character: "
        f"{[(chunk[:20], _ntok(chunk)) for chunk in over if len(chunk) != 1][:5]}"
    )


def _config(
    *,
    strategy: str = "recursive",
    parent_size: int = 4096,
    parent_overlap: int = 0,
    child_size: int = 256,
    child_overlap: int | None = None,
) -> ChunkingConfig:
    """A config for one scenario. ``child_overlap`` defaults to the shipped 32
    scaled to ``child_size``, since ``overlap < size`` is a config invariant."""

    if child_overlap is None:
        child_overlap = min(32, child_size // 8)
    return ChunkingConfig(
        strategy=strategy,
        parent={"size": parent_size, "overlap": parent_overlap},
        child={"size": child_size, "overlap": child_overlap},
    )


def _text_of_tokens(count: int) -> str:
    """A text that encodes to exactly ``count`` cl100k_base tokens."""

    rng = random.Random(1234)
    tokens = [_ENCODER.encode(" " + rng.choice(_WORDS))[0] for _ in range(count)]
    text = _ENCODER.decode(tokens)
    assert _ntok(text) == count
    return text


def _generated_text(seed: int) -> str:
    """A deterministic markdown-ish text: headings, paragraphs, long sentences."""

    rng = random.Random(seed)
    blocks: list[str] = []
    for section in range(rng.randint(1, 4)):
        if rng.random() < 0.8:
            blocks.append(f"{'#' * rng.randint(1, 3)} Section {section}")
        for _ in range(rng.randint(1, 4)):
            sentences = [
                " ".join(rng.choice(_WORDS) for _ in range(rng.randint(3, 60))) + "."
                for _ in range(rng.randint(1, 3))
            ]
            blocks.append(" ".join(sentences))
        if rng.random() < 0.3:
            # One unpunctuated monster paragraph: no sentence boundary exists,
            # so the splitter is forced down to the raw-token level.
            blocks.append(" ".join(rng.choice(_WORDS) for _ in range(400)))
    return "\n\n".join(blocks)


class TestEmptyInput:
    @pytest.mark.parametrize("strategy", _STRATEGIES)
    @pytest.mark.parametrize("text", ["", "   \n", "\n\n\t  \n"])
    def test_blank_text_yields_no_parents(self, strategy: str, text: str) -> None:
        assert split_document(text, _config(strategy=strategy)) == []


class TestFixedTokensStrategy:
    """The Chapter-4 sliding window: no structure, pure token arithmetic."""

    def test_splits_into_full_windows(self) -> None:
        text = _text_of_tokens(250)

        parents = split_document(
            text, _config(strategy="fixed_tokens", parent_size=100, child_size=40)
        )

        assert len(parents) == 3

    def test_concatenated_parents_reproduce_the_token_sequence(self) -> None:
        text = _text_of_tokens(250)

        parents = split_document(
            text, _config(strategy="fixed_tokens", parent_size=100, child_size=40)
        )

        joined = "".join(parent.content for parent in parents)
        assert _ENCODER.encode(joined) == _ENCODER.encode(text)

    def test_overlap_stops_at_the_last_window_instead_of_repeating_the_tail(
        self,
    ) -> None:
        """250 tokens at size 100 / overlap 20 is 4 windows: 0, 80, 160, 240.

        The window that reaches the end of the text is the LAST one. Advancing
        past it by the stride once emitted 511 near-identical tail windows for a
        1207-token document (caught by the e2e run, not by the ``> without``
        assertion in the test below).
        """

        text = _text_of_tokens(250)

        parents = split_document(
            text,
            _config(
                strategy="fixed_tokens",
                parent_size=100,
                parent_overlap=20,
                child_size=40,
            ),
        )

        assert len(parents) == 4
        contents = [parent.content for parent in parents]
        assert len(set(contents)) == 4
        assert contents[-1].endswith(text[-20:])

    def test_a_document_shorter_than_the_window_is_one_parent_even_with_overlap(
        self,
    ) -> None:
        text = _text_of_tokens(50)

        parents = split_document(
            text,
            _config(
                strategy="fixed_tokens",
                parent_size=100,
                parent_overlap=20,
                child_size=40,
            ),
        )

        assert [parent.content for parent in parents] == [text]

    def test_overlap_yields_more_parents_than_no_overlap(self) -> None:
        text = _text_of_tokens(250)

        without = split_document(
            text,
            _config(strategy="fixed_tokens", parent_size=100, child_size=40),
        )
        with_overlap = split_document(
            text,
            _config(
                strategy="fixed_tokens",
                parent_size=100,
                parent_overlap=20,
                child_size=40,
            ),
        )

        assert len(with_overlap) > len(without)

    def test_every_parent_has_an_empty_heading_path(self) -> None:
        """The fixed window is structure-blind — no heading path exists."""

        text = _HEADED_TEXT + "\n\n" + _text_of_tokens(250)

        parents = split_document(
            text, _config(strategy="fixed_tokens", parent_size=100, child_size=40)
        )

        assert [parent.heading_path for parent in parents] == [[]] * len(parents)

    def test_no_parent_exceeds_the_parent_size(self) -> None:
        text = _text_of_tokens(250)

        parents = split_document(
            text, _config(strategy="fixed_tokens", parent_size=100, child_size=40)
        )

        assert all(_ntok(parent.content) <= 100 for parent in parents)

    def test_multi_token_emoji_never_decodes_to_a_replacement_char(self) -> None:
        # #112 Issue 8: a window boundary that lands INSIDE a 3-token emoji used
        # to decode to U+FFFD and store "�" as the chunk's content. Windows
        # are mapped to character offsets now, exactly as ``recursive`` does.
        text = "🧠" * 300

        parents = split_document(
            text, _config(strategy="fixed_tokens", parent_size=5, child_size=2)
        )

        joined = "".join(parent.content for parent in parents)
        assert "�" not in joined
        assert _ENCODER.encode(joined) == _ENCODER.encode(text)

    def test_multi_token_emoji_parents_stay_within_the_parent_size(self) -> None:
        # #112 Issue 8 round 2: killing U+FFFD must not blow the token budget.
        # A 🧠 is 3 cl100k tokens, so a 5-token parent holds exactly one.
        text = "🧠" * 300

        parents = split_document(
            text, _config(strategy="fixed_tokens", parent_size=5, child_size=2)
        )

        assert parents
        _assert_within_budget([parent.content for parent in parents], size=5)

    def test_multi_token_emoji_children_carry_no_replacement_char(self) -> None:
        text = "🧠" * 300

        parents = split_document(
            text, _config(strategy="fixed_tokens", parent_size=5, child_size=2)
        )

        children = [child.content for parent in parents for child in parent.children]
        assert children
        assert all("�" not in child for child in children)

    def test_multi_token_emoji_children_stay_within_the_child_size(self) -> None:
        text = "🧠" * 300

        parents = split_document(
            text, _config(strategy="fixed_tokens", parent_size=5, child_size=2)
        )

        children = [child.content for parent in parents for child in parent.children]
        assert children
        # child_size=2 is SMALLER than one 🧠 (3 tokens): the only chunk allowed
        # over budget is a lone indivisible character (see ``_assert_within_budget``).
        _assert_within_budget(children, size=2)

    def test_mixed_cjk_parents_and_children_stay_within_their_sizes(self) -> None:
        # Every CJK character is 3 UTF-8 bytes and most are 1-2 tokens, so token
        # boundaries land mid-character constantly — the exact input that made
        # ``_token_char_offsets`` compound its overshoot (parent.size=20 emitted
        # 26-31 token parents before the fix).
        parents = split_document(
            _CJK_TEXT,
            _config(
                strategy="fixed_tokens", parent_size=20, child_size=8, child_overlap=0
            ),
        )

        assert parents
        _assert_within_budget([parent.content for parent in parents], size=20)
        _assert_within_budget(
            [child.content for parent in parents for child in parent.children], size=8
        )
        joined = "".join(parent.content for parent in parents)
        assert "�" not in joined
        assert _ENCODER.encode(joined) == _ENCODER.encode(_CJK_TEXT)


class TestRecursiveHeadingPaths:
    def test_heading_stack_is_tracked_across_levels(self) -> None:
        # parent_size=8 holds any single section (6, 6 and 5 tokens) but not
        # two of them merged (>= 11) — so each section becomes its own parent
        # and the heading stack is observable one parent at a time.
        parents = split_document(
            _HEADED_TEXT, _config(parent_size=8, child_size=4, child_overlap=0)
        )

        assert [parent.heading_path for parent in parents] == [
            ["A"],
            ["A", "B"],
            ["C"],
        ]

    def test_each_parent_starts_with_its_heading_line(self) -> None:
        parents = split_document(
            _HEADED_TEXT, _config(parent_size=8, child_size=4, child_overlap=0)
        )

        assert [parent.content.splitlines()[0] for parent in parents] == [
            "# A",
            "## B",
            "# C",
        ]

    def test_text_before_the_first_heading_has_an_empty_path(self) -> None:
        text = "preamble prose\n\n" + _HEADED_TEXT

        parents = split_document(
            text, _config(parent_size=8, child_size=4, child_overlap=0)
        )

        assert parents[0].heading_path == []
        assert parents[0].content.startswith("preamble")

    def test_sections_merge_greedily_and_keep_the_first_path(self) -> None:
        """A parent that swallows several sections reports the heading stack in
        force at its FIRST token — not a union of everything it covers."""

        parents = split_document(
            _HEADED_TEXT, _config(parent_size=4096, child_size=256)
        )

        assert len(parents) == 1
        assert parents[0].heading_path == ["A"]

    def test_children_inherit_nothing_of_their_own(self) -> None:
        """Heading paths live on the parent only (ADR-006 §6)."""

        parents = split_document(
            _HEADED_TEXT, _config(parent_size=8, child_size=4, child_overlap=0)
        )

        assert all(not hasattr(child, "heading_path") for child in parents[0].children)


class TestFencedCodeBlocks:
    """A ``#`` line inside a fenced code block is a shell comment, not a heading.

    Regression: chunking a real tutorial produced the heading path
    ``["→ ensure MONGO_SCHEME=mongodb+srv"]`` — a comment from a ```bash block —
    which then rode along on every child's contextual header. Sizes below are
    small on purpose so a bogus heading WOULD open its own parent.
    """

    def test_hash_comment_inside_a_fence_is_not_a_heading(self) -> None:
        text = (
            "# Real Heading\n\nbody words here.\n\n"
            "```bash\n"
            "# ensure MONGO_SCHEME=mongodb+srv\n"
            "export MONGO_SCHEME=mongodb+srv\n"
            "```\n\n"
            "closing prose."
        )

        parents = split_document(text, _config(parent_size=24, child_size=8))

        paths = {tuple(parent.heading_path) for parent in parents}
        assert paths == {("Real Heading",)}

    def test_headings_after_a_closed_fence_are_still_found(self) -> None:
        text = (
            "# First\n\n```\n# not a heading\n```\n\nbody one.\n\n# Second\n\nbody two."
        )

        parents = split_document(text, _config(parent_size=12, child_size=4))

        assert ["First"] in [parent.heading_path for parent in parents]
        assert ["Second"] in [parent.heading_path for parent in parents]
        assert all(
            "not a heading" not in " ".join(parent.heading_path) for parent in parents
        )

    def test_tilde_fences_are_honoured(self) -> None:
        text = (
            "# Real Heading\n\nbody words here.\n\n~~~\n# not a heading\n~~~\n\nbody."
        )

        parents = split_document(text, _config(parent_size=12, child_size=4))

        paths = {tuple(parent.heading_path) for parent in parents}
        assert paths == {("Real Heading",)}

    def test_fence_detection_has_one_definition_shared_with_the_cleaner(
        self,
    ) -> None:
        # #112 Issue 3: the splitter and the Clean step must agree on what a
        # fence is, so the cleaner owns the helper and chunking imports it.
        assert chunking_module.fenced_ranges is fenced_ranges

    def test_unclosed_fence_swallows_the_rest_of_the_document(self) -> None:
        """CommonMark: an unclosed fence runs to the end of the document, so no
        later ``#`` line can be a heading."""

        text = "# Real Heading\n\nbody words here.\n\n```\n# not a heading\nstill code."

        parents = split_document(text, _config(parent_size=12, child_size=4))

        paths = {tuple(parent.heading_path) for parent in parents}
        assert paths == {("Real Heading",)}


class TestRecursiveSeparatorLadder:
    def test_paragraphs_are_used_when_no_headings_exist(self) -> None:
        text = "alpha beta gamma delta.\n\nepsilon zeta eta theta."

        parents = split_document(text, _config(parent_size=8, child_size=4))

        assert len(parents) == 2
        assert parents[0].content == "alpha beta gamma delta."
        assert parents[1].content == "epsilon zeta eta theta."

    def test_sentences_are_used_when_no_paragraph_break_exists(self) -> None:
        text = "alpha beta gamma delta. epsilon zeta eta theta."

        parents = split_document(text, _config(parent_size=8, child_size=4))

        assert len(parents) == 2
        assert parents[0].content == "alpha beta gamma delta."
        assert parents[1].content == "epsilon zeta eta theta."

    def test_raw_tokens_are_the_last_resort(self) -> None:
        """A single unpunctuated run still gets cut — and stays bounded."""

        text = " ".join(_WORDS * 20)

        parents = split_document(text, _config(parent_size=16, child_size=8))

        assert len(parents) > 1
        assert all(_ntok(parent.content) <= 16 for parent in parents)

    def test_overlap_repeats_the_previous_tail(self) -> None:
        text = "alpha beta gamma delta. epsilon zeta eta theta."

        # 6 tokens for the first sentence (its trailing space included) + 6 for
        # the second: they cannot share a 10-token parent, and a 3-token tail of
        # the first one still fits at the head of the second.
        parents = split_document(
            text, _config(parent_size=10, parent_overlap=3, child_size=4)
        )

        assert len(parents) == 2
        # The second parent starts inside the first one's tail.
        assert parents[1].content.endswith("epsilon zeta eta theta.")
        assert not parents[1].content.startswith("epsilon")
        assert parents[1].content.split()[0] in parents[0].content


class TestMultiTokenCharacterBudget:
    """#112 Issue 8: both strategies share ``_token_char_offsets``.

    A character split across several tokens (an emoji is 3 cl100k tokens, a CJK
    character 1-2) must neither decode to U+FFFD nor let a chunk run over its
    configured ``size``.
    """

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    @pytest.mark.parametrize(
        ("text", "parent_size", "child_size"),
        [("🧠" * 300, 5, 2), (_CJK_TEXT, 20, 8)],
        ids=["emoji", "cjk"],
    )
    def test_no_chunk_exceeds_its_size_on_multi_token_characters(
        self, strategy: str, text: str, parent_size: int, child_size: int
    ) -> None:
        parents = split_document(
            text,
            _config(
                strategy=strategy,
                parent_size=parent_size,
                child_size=child_size,
                child_overlap=0,
            ),
        )

        assert parents
        _assert_within_budget([p.content for p in parents], size=parent_size)
        _assert_within_budget(
            [child.content for p in parents for child in p.children], size=child_size
        )
        assert "�" not in "".join(p.content for p in parents)

    def test_token_offsets_track_the_true_cumulative_byte_count(self) -> None:
        """The root cause, pinned at the helper.

        A 🧠 is 4 UTF-8 bytes encoded as 3 tokens of 2/1/1 bytes, so the two
        inner boundaries snap FORWARD to the end of the same character: five
        emoji must map to ``[0, 1,1,1, 2,2,2, 3,3,3, 4,4,4, 5,5,5]``. Feeding
        the snapped value back into the accumulator produced the strictly
        increasing ``[0, 1, 2, 3, 4, 5, 5, ...]`` — three characters of
        overshoot per emoji, which is what tripled the chunk sizes.
        """

        offsets = chunking_module._token_char_offsets("🧠" * 5)

        assert offsets == [0, 1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4, 5, 5, 5]


class TestChunkBoundsProperty:
    """AC: ``recursive`` never emits a parent or child longer than ``size``."""

    @pytest.mark.parametrize("seed", range(20))
    @pytest.mark.parametrize("size", [64, 256, 1024])
    def test_no_chunk_exceeds_its_configured_size(self, seed: int, size: int) -> None:
        text = _generated_text(seed)
        config = _config(
            parent_size=size,
            parent_overlap=size // 8,
            child_size=size // 4,
            child_overlap=size // 16,
        )

        parents = split_document(text, config)

        assert parents, "generated text must produce at least one parent"
        for parent in parents:
            assert _ntok(parent.content) <= size
            for child in parent.children:
                assert _ntok(child.content) <= size // 4

    @pytest.mark.parametrize("seed", range(20))
    def test_generated_text_survives_the_default_config(self, seed: int) -> None:
        """The shipped 4096/256 defaults never drop text on the floor."""

        text = _generated_text(seed)

        parents = split_document(text, _config())

        assert "".join(parent.content for parent in parents).replace(" ", "").replace(
            "\n", ""
        ) == text.replace(" ", "").replace("\n", "")


class TestParentChildHierarchy:
    @pytest.mark.parametrize("strategy", _STRATEGIES)
    def test_every_child_is_a_substring_of_its_parent(self, strategy: str) -> None:
        text = _generated_text(7)

        parents = split_document(
            text, _config(strategy=strategy, parent_size=512, child_size=64)
        )

        for parent in parents:
            for child in parent.children:
                assert child.content in parent.content

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    def test_children_with_overlap_stay_inside_their_parent(
        self, strategy: str
    ) -> None:
        """Overlap is carried WITHIN the parent — a child never reaches back
        into the previous parent's text."""

        text = _generated_text(11)

        parents = split_document(
            text,
            _config(
                strategy=strategy,
                parent_size=512,
                child_size=64,
                child_overlap=16,
            ),
        )

        for parent in parents:
            for child in parent.children:
                assert child.content in parent.content

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    def test_indexes_are_dense_and_ordered(self, strategy: str) -> None:
        text = _generated_text(3)

        parents = split_document(
            text, _config(strategy=strategy, parent_size=256, child_size=64)
        )

        assert [parent.index for parent in parents] == list(range(len(parents)))
        for parent in parents:
            assert [child.index for child in parent.children] == list(
                range(len(parent.children))
            )

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    def test_parent_shorter_than_child_size_has_exactly_one_child(
        self, strategy: str
    ) -> None:
        text = "alpha beta gamma delta."

        parents = split_document(
            text, _config(strategy=strategy, parent_size=64, child_size=32)
        )

        assert len(parents) == 1
        assert len(parents[0].children) == 1
        assert parents[0].children[0].content == parents[0].content

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    def test_every_parent_has_at_least_one_child(self, strategy: str) -> None:
        text = _generated_text(5)

        parents = split_document(
            text, _config(strategy=strategy, parent_size=256, child_size=64)
        )

        assert all(parent.children for parent in parents)

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    def test_no_chunk_is_blank(self, strategy: str) -> None:
        text = _generated_text(9)

        parents = split_document(
            text, _config(strategy=strategy, parent_size=256, child_size=64)
        )

        for parent in parents:
            assert parent.content.strip()
            assert all(child.content.strip() for child in parent.children)


class TestDeterminismAndSerialisation:
    @pytest.mark.parametrize("strategy", _STRATEGIES)
    def test_two_calls_return_equal_results(self, strategy: str) -> None:
        text = _generated_text(13)
        config = _config(strategy=strategy, parent_size=256, child_size=64)

        first = split_document(text, config)
        second = split_document(text, config)

        assert first == second

    @pytest.mark.parametrize("strategy", _STRATEGIES)
    def test_result_is_json_serialisable(self, strategy: str) -> None:
        text = _generated_text(17)

        parents = split_document(
            text, _config(strategy=strategy, parent_size=256, child_size=64)
        )

        payload = json.dumps([parent.model_dump() for parent in parents])

        assert json.loads(payload)[0]["children"][0]["index"] == 0

    def test_returns_parent_chunk_models(self) -> None:
        parents = split_document(_HEADED_TEXT, _config())

        assert all(isinstance(parent, ParentChunk) for parent in parents)
