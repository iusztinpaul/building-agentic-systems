"""
Deep search with progressive disclosure.

Writes search results to `.memory/{session_id}/` as individual markdown
files and builds a lightweight ``index.yaml`` so the agent can scan
summaries first and selectively read full details.
"""

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from twin.memory.types import QueryResult

logger = logging.getLogger(__name__)

_MEMORY_ROOT = Path(".memory")
_CONTEXT_MAX_LEN = 120


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Convert an ``_id`` string to a filename-safe slug.

    Examples:
        >>> slugify("person:alice")
        'person-alice'
        >>> slugify("person:alice|related_to|person:bob")
        'person-alice--related_to--person-bob'
    """

    s = text.replace("|", "--").replace(":", "-").replace(" ", "_")
    s = re.sub(r"[^a-zA-Z0-9._\-]", "", s)
    return s


def _truncate(text: str, max_len: int = _CONTEXT_MAX_LEN) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _summarize(doc: dict[str, Any]) -> str:
    """Build a one-line human-readable context string for a node or edge."""

    kind = doc.get("kind", "")

    if kind == "edge":
        src = doc.get("source_node_id", "?")
        tgt = doc.get("target_node_id", "?")
        etype = doc.get("type", "?")
        return f"{src} —[{etype}]→ {tgt}"

    # Node
    ntype = doc.get("type", "?")
    name = doc.get("name", "?")
    props = doc.get("properties", {})

    content = props.get("content", "")
    if content:
        return f"{ntype}: {name} — {_truncate(content)}"

    # Fallback: show a few property values.
    parts = []
    for key, val in props.items():
        if val and key != "content":
            if isinstance(val, list):
                parts.append(f"{key}: {', '.join(str(v) for v in val)}")
            else:
                parts.append(f"{key}: {val}")
    detail = "; ".join(parts) if parts else ""
    return _truncate(f"{ntype}: {name} — {detail}" if detail else f"{ntype}: {name}")


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _format_value(value: Any) -> str:
    """Format a property value for markdown display."""

    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _render_node_md(doc: dict[str, Any]) -> str:
    """Render a node document as markdown."""

    ntype = doc.get("type", "?")
    name = doc.get("name", "?")
    lines = [
        f"# {ntype}: {name}",
        "",
        f"- **ID:** `{doc.get('_id', '?')}`",
        "- **Kind:** node",
        f"- **Type:** {ntype}",
    ]

    if doc.get("created_at"):
        lines.append(f"- **Created:** {doc['created_at']}")
    if doc.get("updated_at"):
        lines.append(f"- **Updated:** {doc['updated_at']}")
    if doc.get("sources"):
        lines.append(f"- **Sources:** {doc['sources']}")

    props = doc.get("properties", {})
    if props:
        lines.append("")
        lines.append("## Properties")
        lines.append("")
        for key, val in props.items():
            lines.append(f"- **{key}:** {_format_value(val)}")

    lines.append("")
    return "\n".join(lines)


def _render_edge_md(doc: dict[str, Any]) -> str:
    """Render an edge document as markdown."""

    src = doc.get("source_node_id", "?")
    tgt = doc.get("target_node_id", "?")
    etype = doc.get("type", "?")

    lines = [
        f"# {src} —[{etype}]→ {tgt}",
        "",
        f"- **ID:** `{doc.get('_id', '?')}`",
        "- **Kind:** edge",
        f"- **Type:** {etype}",
        f"- **Source:** `{src}` ({doc.get('source_type', '?')})",
        f"- **Target:** `{tgt}` ({doc.get('target_type', '?')})",
    ]

    if doc.get("created_at"):
        lines.append(f"- **Created:** {doc['created_at']}")
    if doc.get("updated_at"):
        lines.append(f"- **Updated:** {doc['updated_at']}")
    if doc.get("sources"):
        lines.append(f"- **Sources:** {doc['sources']}")

    props = doc.get("properties", {})
    if props:
        lines.append("")
        lines.append("## Properties")
        lines.append("")
        for key, val in props.items():
            lines.append(f"- **{key}:** {_format_value(val)}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Index entry builders
# ---------------------------------------------------------------------------


def _build_node_entry(doc: dict[str, Any], filename: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": doc.get("_id", "?"),
        "kind": "node",
        "type": doc.get("type", "?"),
        "name": doc.get("name", "?"),
        "file": filename,
        "context": _summarize(doc),
    }
    props = doc.get("properties", {})
    if props.get("source_type"):
        entry["source_type"] = props["source_type"]
    if props.get("source_uri"):
        entry["source_uri"] = props["source_uri"]
    return entry


def _build_edge_entry(doc: dict[str, Any], filename: str) -> dict[str, Any]:
    return {
        "id": doc.get("_id", "?"),
        "kind": "edge",
        "type": doc.get("type", "?"),
        "source": doc.get("source_node_id", "?"),
        "target": doc.get("target_node_id", "?"),
        "file": filename,
        "context": _summarize(doc),
    }


# ---------------------------------------------------------------------------
# Main writer
# ---------------------------------------------------------------------------


def write_deep_search_results(
    query: str,
    results: QueryResult,
    session_id: str | None = None,
) -> tuple[Path, str]:
    """Write search results to ``.memory/{session_id}/``.

    Returns ``(directory_path, index_yaml_content)``.
    """

    session_id = session_id or uuid4().hex[:12]
    session_dir = _MEMORY_ROOT / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    docs = results.nodes + results.edges
    index_entries: list[dict[str, Any]] = []

    for doc in docs:
        doc_id = str(doc.get("_id", uuid4().hex[:8]))
        kind = doc.get("kind", "node")

        # Strip embedding before writing.
        cleaned = {k: v for k, v in doc.items() if k != "embedding"}

        slug = slugify(doc_id)
        filename = f"{slug}.md"
        filepath = session_dir / filename

        if kind == "edge":
            filepath.write_text(_render_edge_md(cleaned), encoding="utf-8")
            index_entries.append(_build_edge_entry(cleaned, filename))
        else:
            filepath.write_text(_render_node_md(cleaned), encoding="utf-8")
            index_entries.append(_build_node_entry(cleaned, filename))

    node_count = sum(1 for d in docs if d.get("kind") == "node")
    edge_count = sum(1 for d in docs if d.get("kind") == "edge")

    index = {
        "session_id": session_id,
        "query": query,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "directory": str(session_dir),
        "total_nodes": node_count,
        "total_edges": edge_count,
        "results": index_entries,
    }

    index_yaml = yaml.dump(index, default_flow_style=False, sort_keys=False)
    (session_dir / "index.yaml").write_text(index_yaml, encoding="utf-8")

    logger.info(
        "Deep search written: %d nodes, %d edges → %s",
        node_count,
        edge_count,
        session_dir,
    )

    return session_dir, index_yaml
