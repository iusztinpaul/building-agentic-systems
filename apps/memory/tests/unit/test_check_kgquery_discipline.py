"""Unit tests for the CI grep guard.

The guard fails when raw ``KnowledgeGraphEntry.find(...)`` /
``KnowledgeGraphEntry.find_one(...)`` calls appear outside the allow-list
in :mod:`scripts.check_kgquery_discipline`. We exercise both the
clean-tree path (no violations) and a planted-violation path that uses a
temporary tree to confirm the regex fires.
"""

from __future__ import annotations

import importlib.util
import pathlib


# Load the script as a module so we can call ``_scan`` directly without
# spawning a subprocess.
_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts"
    / "check_kgquery_discipline.py"
)
_spec = importlib.util.spec_from_file_location("check_kgquery_discipline", _SCRIPT)
_module = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
assert _spec is not None
assert _spec.loader is not None
_spec.loader.exec_module(_module)


def test_clean_tree_has_zero_violations() -> None:
    """On the current source tree the scan returns no findings."""

    violations = _module._scan()
    assert violations == [], (
        "KGQuery discipline check found unexpected raw KnowledgeGraphEntry.find "
        f"calls outside the allow-list: {violations}"
    )


def test_planted_violation_is_detected(monkeypatch, tmp_path) -> None:
    """A file outside the allow-list that calls ``KnowledgeGraphEntry.find``
    must be flagged."""

    # Build a fake tree under tmp_path that mimics ``apps/memory/`` layout.
    src = tmp_path / "src"
    src.mkdir()
    bad = src / "leaky_caller.py"
    bad.write_text(
        "from tree.entities.knowledge_graph import KnowledgeGraphEntry\n\n"
        "async def leak():\n"
        "    return await KnowledgeGraphEntry.find({'kind': 'node'}).to_list()\n",
        encoding="utf-8",
    )

    # Re-point the script's search roots at our planted tree.
    monkeypatch.setattr(_module, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(_module, "_SOURCE_DIRS", (src,))

    violations = _module._scan()
    assert violations, "Expected the planted violation to be detected"
    assert any("leaky_caller.py" in str(v[0]) for v in violations)


def test_allow_listed_paths_are_exempt(monkeypatch, tmp_path) -> None:
    """Files inside the allow-list may use the raw call without flagging."""

    src = tmp_path / "src" / "tree" / "memory" / "query"
    src.mkdir(parents=True)
    helper = src / "kgquery.py"
    helper.write_text(
        "from tree.entities.knowledge_graph import KnowledgeGraphEntry\n\n"
        "async def search():\n"
        "    return await KnowledgeGraphEntry.find({'kind': 'node'}).to_list()\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(_module, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(_module, "_SOURCE_DIRS", (tmp_path / "src",))

    violations = _module._scan()
    assert violations == [], (
        f"Allow-listed path should not be flagged; got {violations}"
    )


# ---------------------------------------------------------------------------
# Raw-pymongo bypass (#023): the lint must catch the new gap class.
# ---------------------------------------------------------------------------


class TestRawPymongoBypassDetection:
    """The widened lint must flag raw ``collection.aggregate(...)`` etc.

    The #023 PR-review found that ``review/core.py`` was bypassing the
    Beanie surface entirely via ``database["knowledge_graph"].aggregate(...)``
    — the original lint never saw the gap because it only matched
    ``KnowledgeGraphEntry.find{,_one}(``. The widened lint also flags
    the raw-pymongo patterns on common local handle names
    (``collection`` / ``col`` / ``kg`` / ``coll``).
    """

    def test_planted_raw_aggregate_is_detected(self, monkeypatch, tmp_path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        bad = src / "raw_aggregate_caller.py"
        bad.write_text(
            "async def leak(database):\n"
            '    collection = database["knowledge_graph"]\n'
            "    return await collection.aggregate([{'$match': {}}])\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(_module, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(_module, "_SOURCE_DIRS", (src,))

        violations = _module._scan()
        assert violations, "Expected the planted aggregate() bypass to be detected"
        assert any("raw_aggregate_caller.py" in str(v[0]) for v in violations)

    def test_planted_raw_find_is_detected(self, monkeypatch, tmp_path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        bad = src / "raw_find_caller.py"
        bad.write_text(
            "async def leak(database):\n"
            '    collection = database["knowledge_graph"]\n'
            "    return collection.find({'kind': 'node'})\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(_module, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(_module, "_SOURCE_DIRS", (src,))

        violations = _module._scan()
        assert violations
        assert any("raw_find_caller.py" in str(v[0]) for v in violations)

    def test_planted_raw_find_one_is_detected(self, monkeypatch, tmp_path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        bad = src / "raw_find_one_caller.py"
        bad.write_text(
            "async def leak(database):\n"
            '    coll = database["knowledge_graph"]\n'
            "    return await coll.find_one({'_id': 'x'})\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(_module, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(_module, "_SOURCE_DIRS", (src,))

        violations = _module._scan()
        assert violations
        assert any("raw_find_one_caller.py" in str(v[0]) for v in violations)

    def test_planted_raw_update_many_is_detected(self, monkeypatch, tmp_path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        bad = src / "raw_update_many_caller.py"
        bad.write_text(
            "async def leak(database):\n"
            '    col = database["knowledge_graph"]\n'
            "    await col.update_many({}, {'$set': {'x': 1}})\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(_module, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(_module, "_SOURCE_DIRS", (src,))

        violations = _module._scan()
        assert violations
        assert any("raw_update_many_caller.py" in str(v[0]) for v in violations)

    def test_planted_raw_delete_many_is_detected(self, monkeypatch, tmp_path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        bad = src / "raw_delete_many_caller.py"
        bad.write_text(
            "async def leak(database):\n"
            '    kg = database["knowledge_graph"]\n'
            "    await kg.delete_many({})\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(_module, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(_module, "_SOURCE_DIRS", (src,))

        violations = _module._scan()
        assert violations
        assert any("raw_delete_many_caller.py" in str(v[0]) for v in violations)
