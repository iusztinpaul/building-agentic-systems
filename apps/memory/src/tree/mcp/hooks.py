"""**Session-end hook** logic — persist a finished Claude Code session over MCP.

MCP is the ONLY harness↔memory interface (ADR-008 §5), so this module is a PURE
client: stdlib + ``fastmcp`` + ``pydantic``, nothing else (enforced by
``tests/unit/mcp/test_hooks.py::TestModulePurity``). It reads Claude Code's
``SessionEnd`` JSON on stdin, turns the transcript into ONE
``ingest_conversation`` call against the repo-root ``.mcp.json`` entry for one
server, and ALWAYS returns 0 — a memory hiccup must never block leaving a
session.

Verified against the primary docs (2026-09-12):

* Claude Code hooks — https://docs.claude.com/en/docs/claude-code/hooks
  ``SessionEnd`` stdin carries ``session_id``, ``transcript_path``, ``cwd``,
  ``hook_event_name`` and ``reason``; ``SessionEnd`` hooks share a 1.5 s budget
  raised to the highest configured per-hook ``timeout``, up to 60 s. The
  transcript is JSONL; ``user`` / ``assistant`` entries carry ``timestamp`` and
  ``message.content`` as a string or a list of ``text`` / ``thinking`` /
  ``tool_use`` / ``tool_result`` blocks (shape cross-checked against a live
  ``~/.claude/projects/**/*.jsonl``).
* FastMCP client from a config dict (MCPConfig transport; a SINGLE server means
  tool names are NOT prefixed) — https://gofastmcp.com/clients/client
* ``call_tool(..., raise_on_error=False)`` and ``CallToolResult.is_error`` /
  ``.content`` — https://gofastmcp.com/clients/tools

Pinned to ``fastmcp`` 3.2.0 / ``pydantic`` 2.12.5.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from fastmcp import Client
from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: The one tool the hook calls (ADR-008 §5).
INGEST_TOOL = "ingest_conversation"

#: Ceiling on the tool call itself. Claude Code kills the hook at its 60 s
#: ``SessionEnd`` budget, so a hung server would be SIGKILLed and never log why;
#: 30 s leaves room for the stdio server to boot AND for the skip line to land.
_CALL_TIMEOUT_SECONDS = 30.0


class HookInput(BaseModel):
    """The fields of Claude Code's ``SessionEnd`` stdin JSON the hook uses.

    Every field is optional: malformed or partial input must end in a logged
    skip, never in a non-zero exit. Unknown keys (``hook_event_name``,
    ``reason``, ``permission_mode``, …) are ignored.
    """

    session_id: str = ""
    transcript_path: str = ""
    cwd: str = ""


class TranscriptTurn(BaseModel):
    """One ``user`` / ``assistant` turn, reduced to its plain text."""

    role: str
    text: str
    timestamp: datetime


def read_hook_input(stream: TextIO) -> HookInput:
    """Parse the ``SessionEnd`` JSON on ``stream``; empty input → empty fields."""

    return HookInput.model_validate_json(stream.read().strip() or "{}")


def _block_text(content: Any) -> str:
    """Return the conversation text of one ``message.content``.

    A string is the whole text. A list keeps ``type == "text"`` blocks only, so
    ``thinking``, ``tool_use`` and ``tool_result`` blocks never reach memory.
    """

    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(texts).strip()


def _parse_timestamp(raw: object, *, naive_warned: bool) -> tuple[datetime, bool]:
    """Parse an ISO-8601 transcript timestamp as tz-aware UTC.

    Returns the timestamp and the updated "already warned about a naive
    timestamp" flag — naive input is assumed UTC and warned about ONCE per
    transcript (one line per turn would drown the hook's log).
    """

    if not isinstance(raw, str):
        raise ValueError(f"missing timestamp: {raw!r}")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if not naive_warned:
            logger.warning(
                "Transcript carries naive timestamps (e.g. %s) — assuming UTC.", raw
            )
            naive_warned = True
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC), naive_warned


def parse_transcript(path: Path) -> list[TranscriptTurn]:
    """Read a transcript JSONL into the ``user`` / ``assistant`` text turns.

    Non-conversation entries (``summary``, ``system``, file snapshots) and turns
    whose text is empty once the tool blocks are dropped are skipped silently;
    a line that is not parsable JSON (a truncated tail is normal — the file is
    written asynchronously) is skipped with a WARNING rather than losing the
    whole session.
    """

    turns: list[TranscriptTurn] = []
    naive_warned = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Skipping malformed transcript line %d: %s", number, exc)
            continue
        if not isinstance(entry, dict) or entry.get("type") not in {
            "user",
            "assistant",
        }:
            continue
        message = entry.get("message")
        text = _block_text(
            message.get("content") if isinstance(message, dict) else None
        )
        if not text:
            continue
        try:
            timestamp, naive_warned = _parse_timestamp(
                entry.get("timestamp"), naive_warned=naive_warned
            )
        except ValueError as exc:
            logger.warning("Skipping transcript line %d: %s", number, exc)
            continue
        turns.append(TranscriptTurn(role=entry["type"], text=text, timestamp=timestamp))
    return turns


def build_ingest_args(turns: list[TranscriptTurn], session_id: str) -> dict[str, Any]:
    """Build the ``ingest_conversation`` arguments for a non-empty ``turns``.

    ``session_uri`` is the natural key memory dedupes on (first write wins), so
    a resumed-then-ended session answers ``duplicate: true``.
    """

    first = turns[0]
    return {
        "conversation_text": "\n\n".join(
            f"{turn.role.capitalize()}: {turn.text}" for turn in turns
        ),
        "title": (f"Claude Code session {session_id[:8]} — {first.timestamp.date()}"),
        "session_uri": f"claude-session://{session_id}",
        "session_started_at": first.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def load_server_config(
    mcp_json: Path, server_name: str, *, cwd: Path
) -> dict[str, Any]:
    """Read ``.mcp.json`` and return an MCPConfig holding ONE server.

    One server means FastMCP does not prefix the tool names, so the hook calls
    ``ingest_conversation``, not ``<server>_ingest_conversation``.

    A stdio entry gets ``cwd`` pinned (its ``args`` are repo-root relative) and
    ``MCP_SKIP_INDEX_BOOTSTRAP=1`` merged into its own ``env`` — the spawned
    local server must query indexes, never build them, or the session-end budget
    is gone. Remote entries pass through unchanged (the cloud server
    authenticates over OAuth).
    """

    servers = json.loads(mcp_json.read_text(encoding="utf-8")).get("mcpServers", {})
    if server_name not in servers:
        raise KeyError(
            f"No MCP server {server_name!r} in {mcp_json}; "
            f"known servers: {sorted(servers)}"
        )
    entry = dict(servers[server_name])
    if "command" in entry:
        entry["cwd"] = str(cwd)
        entry["env"] = {**entry.get("env", {}), "MCP_SKIP_INDEX_BOOTSTRAP": "1"}
    return {"mcpServers": {server_name: entry}}


def _answer_text(result: Any) -> str:
    """The first text content block of a ``CallToolResult`` (tools answer JSON)."""

    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            return text
    return ""


async def run(
    stdin: TextIO, mcp_json: Path, server_name: str, *, min_words: int = 200
) -> int:
    """Ingest the finished session through MCP. ALWAYS returns 0.

    Every guard logs exactly one line and returns: no session id or transcript,
    a transcript below ``min_words`` (a two-line session is noise, not memory),
    an unreachable server, or a **Tool error envelope** in the answer.
    """

    try:
        hook_input = read_hook_input(stdin)
    except Exception as exc:  # noqa: BLE001 — stdin is untrusted; never fail here.
        logger.warning("Unreadable SessionEnd input (%s) — skipped.", exc)
        return 0

    if not hook_input.session_id or not hook_input.transcript_path:
        logger.info("SessionEnd input has no session_id / transcript_path — skipped.")
        return 0

    # NOT resolved against `hook_input.cwd`: Claude Code sends an absolute path,
    # and a relative one (the README's smoke test) belongs to the process CWD.
    transcript = Path(hook_input.transcript_path).expanduser()
    if not transcript.is_file():
        logger.info("Transcript %s does not exist — skipped.", transcript)
        return 0

    try:
        turns = parse_transcript(transcript)
    except OSError as exc:
        logger.warning("Unreadable transcript %s (%s) — skipped.", transcript, exc)
        return 0

    words = sum(len(turn.text.split()) for turn in turns)
    if words < min_words:
        logger.info("Transcript %d words < %d — skipped.", words, min_words)
        return 0

    try:
        config = load_server_config(
            mcp_json, server_name, cwd=mcp_json.parent.resolve()
        )
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read MCP server config (%s) — skipped.", exc)
        return 0

    arguments = build_ingest_args(turns, hook_input.session_id)
    try:
        async with Client(config) as client:
            result = await client.call_tool(
                INGEST_TOOL,
                arguments,
                raise_on_error=False,
                timeout=_CALL_TIMEOUT_SECONDS,
            )
    except Exception as exc:  # noqa: BLE001 — a dead server must not fail exit.
        logger.warning(
            "%s unreachable on %r (%s: %s) — session not persisted.",
            INGEST_TOOL,
            server_name,
            type(exc).__name__,
            exc,
        )
        return 0

    answer = _answer_text(result)
    try:
        receipt = json.loads(answer)
    except json.JSONDecodeError:
        receipt = None

    if getattr(result, "is_error", False) or not isinstance(receipt, dict):
        logger.warning("%s answered %s — session not persisted.", INGEST_TOOL, answer)
        return 0

    if "error_type" in receipt:
        logger.warning(
            "%s answered error_type=%s retryable=%s: %s — session not persisted.",
            INGEST_TOOL,
            receipt.get("error_type"),
            receipt.get("retryable"),
            receipt.get("message"),
        )
        return 0

    logger.info(
        "Ingested session %s duplicate=%s flow_run_id=%s status=%s",
        receipt.get("source_uri"),
        receipt.get("duplicate"),
        receipt.get("flow_run_id"),
        receipt.get("status"),
    )
    return 0
