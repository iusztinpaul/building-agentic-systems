"""Unit tests for :mod:`tree.mcp.deep_search` — the per-row files + YAML index."""

from pathlib import Path

import pytest

from tree.mcp import deep_search
from tree.mcp.deep_search import write_deep_search_results
from tree.memory.types import QueryResult


def _row(row_id: str, kind: str, *, added: bool = False) -> dict:
    row = {
        "_id": row_id,
        "kind": kind,
        "type": "chunk" if kind == "node" else "part_of",
    }
    if kind == "edge":
        row |= {"source_node_id": "a", "target_node_id": "b"}
    if added:
        row["_closure_added"] = True
    return row


@pytest.fixture
def closure_result() -> QueryResult:
    """A hop row, then a row the ``part_of`` closure appended, on both lists."""

    return QueryResult(
        nodes=[_row("n1", "node"), _row("n2", "node", added=True)],
        edges=[_row("e1", "edge"), _row("e2", "edge", added=True)],
    )


def test_the_index_lists_every_original_row_before_the_closure(
    mocker, tmp_path: Path, closure_result: QueryResult
) -> None:
    mocker.patch.object(deep_search, "MEMORY_DIR", tmp_path)

    _, index_yaml = write_deep_search_results("q", closure_result, "s1")

    ids = [
        line.split("id: ")[1]
        for line in index_yaml.splitlines()
        if line.startswith("- id: ")
    ]
    assert ids == ["n1", "e1", "n2", "e2"]


def test_the_closure_marker_reaches_no_file(
    mocker, tmp_path: Path, closure_result: QueryResult
) -> None:
    mocker.patch.object(deep_search, "MEMORY_DIR", tmp_path)

    write_deep_search_results("q", closure_result, "s1")

    written = [path.read_text() for path in (tmp_path / "s1").iterdir()]
    assert len(written) == 5  # 4 rows + index.yaml
    assert all("_closure_added" not in text for text in written)
