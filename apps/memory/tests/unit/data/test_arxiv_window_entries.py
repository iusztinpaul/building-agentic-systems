"""Unit tests for ``arxiv_window_entries`` — the HuggingFace offset-**Window** math.

Pure decision logic (no DB, no Prefect): given one ``HuggingFaceDatasetSource``, it
returns ``num_workers`` disjoint windows that tile ``[0, max_samples)`` exactly (the
last window takes the remainder). The data orchestrator (#072) dispatches one
``data-etl-worker`` run per returned window-entry. These assert the arithmetic, the
edge-case clamps, and that the configured entry is never mutated (``offset`` is a
dispatch-time coordinate, #070).
"""

from __future__ import annotations

from tree.config.app_config import HuggingFaceDatasetSource
from tree.data.huggingface.arxiv_dataset_pipeline import (
    ARXIV_DATASET_ID,
    arxiv_window_entries,
)


def _hf(*, max_samples: int, num_workers: int) -> HuggingFaceDatasetSource:
    return HuggingFaceDatasetSource(
        uri=ARXIV_DATASET_ID, max_samples=max_samples, num_workers=num_workers
    )


def _windows(entries) -> list[tuple[int | None, int]]:
    return [(e.offset, e.max_samples) for e in entries]


def test_single_worker_is_byte_identical_to_today() -> None:
    """num_workers=1 ⇒ one window, offset unset (None), max_samples unchanged."""

    entry = _hf(max_samples=1000, num_workers=1)

    result = arxiv_window_entries(entry)

    assert len(result) == 1
    assert result[0].offset is None
    assert result[0].max_samples == 1000


def test_divisible_windows_tile_exactly() -> None:
    """max_samples=1000, num_workers=4 ⇒ (0,250),(250,250),(500,250),(750,250)."""

    entry = _hf(max_samples=1000, num_workers=4)

    result = arxiv_window_entries(entry)

    assert _windows(result) == [(0, 250), (250, 250), (500, 250), (750, 250)]


def test_remainder_goes_to_the_last_window() -> None:
    """max_samples=1000, num_workers=3 ⇒ (0,333),(333,333),(666,334)."""

    entry = _hf(max_samples=1000, num_workers=3)

    result = arxiv_window_entries(entry)

    assert _windows(result) == [(0, 333), (333, 333), (666, 334)]


def test_windows_cover_full_range_with_no_gap_or_overlap() -> None:
    """The union of every window is exactly [0, max_samples) — no gap, no overlap."""

    entry = _hf(max_samples=997, num_workers=7)

    result = arxiv_window_entries(entry)

    covered: list[int] = []
    for w in result:
        start = w.offset or 0
        covered.extend(range(start, start + w.max_samples))
    assert covered == list(range(997))


def test_max_samples_zero_emits_no_windows() -> None:
    """max_samples=0 ⇒ a clean no-op (empty list) for that entry."""

    entry = _hf(max_samples=0, num_workers=4)

    assert arxiv_window_entries(entry) == []


def test_num_workers_greater_than_max_samples_clamps_to_size_one_windows() -> None:
    """num_workers > max_samples ⇒ clamp so no window has max_samples <= 0."""

    entry = _hf(max_samples=3, num_workers=10)

    result = arxiv_window_entries(entry)

    # Clamp to at most one window per row: 3 windows of size 1 tiling [0, 3).
    assert _windows(result) == [(0, 1), (1, 1), (2, 1)]
    assert all(w.max_samples >= 1 for w in result)


def test_does_not_mutate_the_configured_entry() -> None:
    """The configured entry is never mutated — windows are copies."""

    entry = _hf(max_samples=1000, num_workers=4)

    arxiv_window_entries(entry)

    assert entry.offset is None
    assert entry.max_samples == 1000


def test_windows_preserve_non_window_fields() -> None:
    """Every window copy carries the entry's other fields (uri, type, etc.)."""

    entry = HuggingFaceDatasetSource(
        uri=ARXIV_DATASET_ID,
        max_samples=100,
        num_workers=2,
        fetch_content=True,
        batch_size=25,
        concurrency=5,
    )

    result = arxiv_window_entries(entry)

    assert len(result) == 2
    for w in result:
        assert w.uri == ARXIV_DATASET_ID
        assert w.type == "huggingface_dataset"
        assert w.fetch_content is True
        assert w.batch_size == 25
        assert w.concurrency == 5
        assert w.num_workers == 2
