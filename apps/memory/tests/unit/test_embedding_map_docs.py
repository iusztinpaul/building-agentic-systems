"""The **Embedding map**'s two surfaces are documented where operators look.

A surface nobody is told about is a surface nobody uses: the map is reachable
only if `apps/memory/README.md` describes the CLI and the tool, the `tree-memory`
skill knows the tool exists in BOTH **Memory modes**, and the e2e skill tells a
verifier to cluster before rendering. These are grep-level assertions on purpose
— they catch the drift that matters (a renamed tool, a stale tool count, the
warning contract silently dropped) without pinning prose.

Also guards the ADR-007 §7 rename: the pre-ADR-007, graph-only MCP App module
is `tree.mcp.viz_app` everywhere except the ADRs, which record the history.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# The pre-ADR-007 name of the MCP App module, spelled as a concatenation so this
# file is not itself a hit for the grep in the rename guard below (it would match
# forever once committed, and the guard would be unfixable without deleting it).
_PRE_ADR_007_APP_MODULE = "graph" + "_app"

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MEMORY_README = _REPO_ROOT / "apps" / "memory" / "README.md"
_MEMORY_SKILL = _REPO_ROOT / ".agents" / "skills" / "tree-memory" / "SKILL.md"
_E2E_SKILL = _REPO_ROOT / ".agents" / "skills" / "run-pipelines-e2e" / "SKILL.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestMemoryReadme:
    @pytest.mark.parametrize(
        "needle",
        [
            "#### Embedding map",  # the CLI subsection
            "make memory-visualize-embeddings",
            "visualize_memory_embeddings",  # the tool table row
            "have no cluster assignment",  # the warning contract
            "No clustering run found for this user",  # the no-run message
        ],
    )
    def test_it_documents_both_map_surfaces(self, needle: str) -> None:
        assert needle in _read(_MEMORY_README)

    @pytest.mark.parametrize("needle", ["*Both modes (7 tools):*", "7 more, 14 total"])
    def test_the_tool_counts_match_the_registered_surface(self, needle: str) -> None:
        # The map tool is registered in both modes, so rag is 7 and graphrag 14
        # — asserted against the real servers in ``mcp/test_tool_gating.py``.
        assert needle in _read(_MEMORY_README)


class TestTreeMemorySkill:
    def test_the_tool_is_listed_as_available_in_both_modes(self) -> None:
        row = next(
            line
            for line in _read(_MEMORY_SKILL).splitlines()
            if line.startswith("| `visualize_memory_embeddings` |")
        )

        # Two ✅ columns = rag AND graphrag; a "—" would tell the agent to skip
        # the tool on a rag server that in fact registers it.
        assert row.count("✅") == 2

    @pytest.mark.parametrize(
        "needle",
        [
            "no cluster assignment (or a stale one)",
            "No clustering run found for this user",
        ],
    )
    def test_it_tells_the_agent_to_relay_the_map_contract_verbatim(
        self, needle: str
    ) -> None:
        assert needle in _read(_MEMORY_SKILL)


class TestE2eSkill:
    @pytest.mark.parametrize(
        "needle",
        [
            "make memory-run-clustering-pipeline",
            "make memory-visualize-embeddings",
            "TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5",
            "numba",  # the ~40 s cold-import gotcha
        ],
    )
    def test_the_verification_ritual_covers_cluster_then_render(
        self, needle: str
    ) -> None:
        assert needle in _read(_E2E_SKILL)


def test_the_mcp_app_layer_is_named_viz_app_in_the_code_and_its_docs() -> None:
    # ADR-007 §7 renamed the graph-only MCP App module to ``tree.mcp.viz_app``
    # (it is mode-neutral, and it serves maps as well as graphs). The ADRs and the
    # task files keep the old name because they record the decision; the code, the
    # skills, the glossary and the READMEs a reader follows may not.
    #
    # ``--untracked`` is load-bearing: plain ``git grep`` skips files that are not
    # yet added, so a brand-new file carrying the old name would pass this guard
    # right up until it is committed.
    hits = subprocess.run(
        [
            "git",
            "grep",
            "--untracked",
            "-n",
            _PRE_ADR_007_APP_MODULE,
            "--",
            "apps/memory",
            ".agents",
            "docs/glossary.md",
            "README.md",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout

    assert hits == ""
