"""Unit tests for :mod:`tree.mcp.deep_search` — the per-row files + YAML index."""

from pathlib import Path

import pytest

from tree.entities.memory import to_stored_vector
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


def test_the_relevance_rank_reaches_no_file(mocker, tmp_path: Path) -> None:
    # Arrange: a query view's rows carry `doc_rank` (task 168) — a viz concern.
    mocker.patch.object(deep_search, "MEMORY_DIR", tmp_path)
    ranked = QueryResult(
        nodes=[{**_row("n1", "node"), "doc_rank": 1}],
        edges=[_row("e1", "edge")],
    )
    plain = QueryResult(nodes=[_row("n1", "node")], edges=[_row("e1", "edge")])

    _, ranked_index = write_deep_search_results("q", ranked, "ranked")
    _, plain_index = write_deep_search_results("q", plain, "plain")

    # Assert: no file and no index line names it; every row file is
    # byte-identical to the unranked run's.
    assert "doc_rank" not in ranked_index
    for name in ("n1.md", "e1.md"):
        written = (tmp_path / "ranked" / name).read_text()
        assert "doc_rank" not in written
        assert written == (tmp_path / "plain" / name).read_text()


def test_a_stored_bindata_embedding_reaches_no_file(mocker, tmp_path: Path) -> None:
    # Task 175: a row read straight from Mongo carries a float32 ``binData``
    # vector; the per-row file must strip it like the float list it replaced.
    mocker.patch.object(deep_search, "MEMORY_DIR", tmp_path)
    result = QueryResult(
        nodes=[{**_row("n1", "node"), "embedding": to_stored_vector([0.1, 0.2])}],
        edges=[],
    )

    write_deep_search_results("q", result, "s1")

    written = (tmp_path / "s1" / "n1.md").read_text()
    assert "embedding" not in written
