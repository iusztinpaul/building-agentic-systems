"""Unit tests for the **Session-end hook** logic (``tree.mcp.hooks``, ADR-008 §5).

The hook is a PURE MCP client: it reads Claude Code's ``SessionEnd`` stdin JSON,
turns the transcript into one ``ingest_conversation`` call and always exits 0.
Two properties carry the ADR and are pinned here before any behaviour:

* **Purity** — the module imports stdlib + ``fastmcp`` + ``pydantic`` and nothing
  else (AST-walked, mirroring ``test_cleaning.py::TestModulePurity``), and
  importing it must not drag the MCP server (Mongo, Prefect, Opik) into the hook
  process at RUNTIME either.
* **Exit 0, always** — a missing transcript, a short session, an unreachable
  server or a **Tool error envelope** are logged and skipped; leaving a session
  must never fail because memory hiccuped.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import pytest

from tree.mcp import hooks
from tree.mcp.hooks import (
    HookInput,
    build_ingest_args,
    load_server_config,
    parse_transcript,
    read_hook_input,
    run,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "session_end_transcript.jsonl"

# Payloads the fixture carries inside a `thinking` block, a `tool_use` block and
# a `tool_result` entry. None of them is conversation text, so none may survive.
_NON_TEXT_PAYLOADS = [
    "SECRET_THINKING_BLOCK",
    "SECRET_TOOL_USE_COMMAND",
    "SECRET_TOOL_RESULT_PAYLOAD",
]

_SESSION_ID = "3f9a1c2b-7d4e-4a1b-9c8d-0e1f2a3b4c5d"


class _FakeClient:
    """Stands in for ``fastmcp.Client``: records the call, answers ``answer``."""

    def __init__(self, answer: str, *, is_error: bool = False) -> None:
        self._answer = answer
        self._is_error = is_error
        self.config: dict[str, Any] | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, config: dict[str, Any]) -> _FakeClient:
        self.config = config
        return self

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def call_tool(
        self, name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> Any:
        self.calls.append((name, arguments))
        block = type("TextBlock", (), {"type": "text", "text": self._answer})()
        return type(
            "CallToolResult",
            (),
            {"content": [block], "is_error": self._is_error, "data": None},
        )()


@pytest.fixture
def mcp_json(tmp_path: Path) -> Path:
    """A repo-root ``.mcp.json`` with the two servers the real one carries."""

    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "tree-memory": {
                        "type": "http",
                        "url": "https://tree-memory.fastmcp.app/mcp",
                    },
                    "tree-memory-local": {
                        "command": "uv",
                        "args": ["--directory", "apps/memory", "run", "python", "s.py"],
                        "env": {"ENV_FILE_PATH": "../../.env"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def hook_stdin(mcp_json: Path) -> TextIO:
    """The SessionEnd JSON Claude Code pipes in, pointed at the fixture."""

    return io.StringIO(
        json.dumps(
            {
                "session_id": _SESSION_ID,
                "transcript_path": str(_FIXTURE),
                "cwd": str(mcp_json.parent),
                "hook_event_name": "SessionEnd",
                "reason": "other",
            }
        )
    )


class TestModulePurity:
    """MCP is the only harness↔memory boundary — the hook stays a client."""

    def test_hooks_imports_only_allowlisted(self) -> None:
        # Arrange: parse the source rather than trusting the import to fail —
        # a `tree.*` import would succeed here yet drag config, Mongo and
        # Prefect into the hook process.
        module_ast = ast.parse(Path(hooks.__file__).read_text(encoding="utf-8"))

        # Act: collect the root module of every import statement.
        roots: set[str] = set()
        for node in ast.walk(module_ast):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "no relative imports in the pure module"
                assert node.module is not None
                roots.add(node.module.split(".")[0])

        # Assert: stdlib + the two allowed third parties, and none of the
        # heavyweight roots the ADR bans by name.
        assert roots <= set(sys.stdlib_module_names) | {"fastmcp", "pydantic"}
        assert roots.isdisjoint({"tree", "pymongo", "beanie", "prefect", "motor"})

    def test_importing_hooks_never_loads_the_mcp_server(self) -> None:
        # AST purity says nothing about the PARENT package: a re-export in
        # `tree/mcp/__init__.py` would import the server (and Mongo, Prefect,
        # Opik with it) the moment the glue script imports the hook. A fresh
        # interpreter is the only honest check.
        probe = (
            "import tree.mcp.hooks, sys, json;"
            "print(json.dumps([m for m in "
            "('tree.mcp.server','tree.mcp.tools','pymongo','prefect','beanie')"
            " if m in sys.modules]))"
        )

        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
        )

        assert json.loads(completed.stdout.strip().splitlines()[-1]) == []


class TestReadHookInput:
    def test_parses_the_session_end_payload(self, hook_stdin: Any) -> None:
        hook_input = read_hook_input(hook_stdin)

        assert hook_input.session_id == _SESSION_ID
        assert hook_input.transcript_path == str(_FIXTURE)

    def test_empty_stdin_yields_empty_fields(self) -> None:
        # An empty stream is not an error: `run` decides to skip, nothing raises.
        hook_input = read_hook_input(io.StringIO(""))

        assert hook_input == HookInput()


class TestParseTranscript:
    def test_keeps_text_blocks_only(self) -> None:
        turns = parse_transcript(_FIXTURE)

        assert [turn.role for turn in turns] == [
            "user",
            "assistant",
            "assistant",
            "user",
        ]
        for payload in _NON_TEXT_PAYLOADS:
            assert all(payload not in turn.text for turn in turns), payload

    def test_timestamps_are_timezone_aware(self) -> None:
        turns = parse_transcript(_FIXTURE)

        assert turns[0].timestamp == datetime(2026, 9, 12, 8, 2, 11, 123000, tzinfo=UTC)
        assert all(turn.timestamp.tzinfo is not None for turn in turns)

    def test_skips_malformed_line(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path
    ) -> None:
        # Arrange: one truncated line between two good ones (the fixture carries
        # the same shape — here it is isolated so the count is unambiguous).
        path = tmp_path / "transcript.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-09-12T08:00:00.000Z",
                    "message": {"role": "user", "content": "first"},
                }
            )
            + '\n{"type": "user", "message": {"role": "us\n'
            + json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-09-12T08:00:05.000Z",
                    "message": {"role": "assistant", "content": "second"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger=hooks.__name__):
            turns = parse_transcript(path)

        assert [turn.text for turn in turns] == ["first", "second"]
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_naive_timestamp_is_read_as_utc(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path
    ) -> None:
        path = tmp_path / "transcript.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-09-12T08:00:00",
                    "message": {"role": "user", "content": "naive"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger=hooks.__name__):
            turns = parse_transcript(path)

        assert turns[0].timestamp == datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
        assert any("naive" in record.getMessage().lower() for record in caplog.records)

    def test_empty_turns_are_dropped(self, tmp_path: Path) -> None:
        # A tool_result-only user entry has no text: it must vanish, not turn
        # into an empty "User: " block in the conversation text.
        path = tmp_path / "transcript.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-09-12T08:00:00.000Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "content": "out"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        assert parse_transcript(path) == []


class TestBuildIngestArgs:
    def test_shape(self) -> None:
        turns = parse_transcript(_FIXTURE)

        args = build_ingest_args(turns, _SESSION_ID)

        assert args["session_uri"] == f"claude-session://{_SESSION_ID}"
        assert args["title"] == "Claude Code session 3f9a1c2b — 2026-09-12"
        assert args["session_started_at"] == "2026-09-12T08:02:11Z"
        assert args["conversation_text"].startswith("User: We spent the morning")
        assert "\n\nAssistant: Reading the helper now." in args["conversation_text"]

    def test_conversation_text_holds_one_block_per_turn(self) -> None:
        turns = parse_transcript(_FIXTURE)

        blocks = build_ingest_args(turns, _SESSION_ID)["conversation_text"].split(
            "\n\n"
        )

        assert len(blocks) == len(turns)
        assert all(block.startswith(("User: ", "Assistant: ")) for block in blocks), (
            blocks
        )


class TestLoadServerConfig:
    def test_picks_one_server_and_pins_cwd_and_bootstrap_flag(
        self, mcp_json: Path
    ) -> None:
        config = load_server_config(mcp_json, "tree-memory-local", cwd=mcp_json.parent)

        assert list(config["mcpServers"]) == ["tree-memory-local"]
        entry = config["mcpServers"]["tree-memory-local"]
        assert entry["cwd"] == str(mcp_json.parent)
        assert entry["env"]["MCP_SKIP_INDEX_BOOTSTRAP"] == "1"
        # The entry's own env survives the merge.
        assert entry["env"]["ENV_FILE_PATH"] == "../../.env"

    def test_remote_entry_passes_through_untouched(self, mcp_json: Path) -> None:
        # Nothing to inject: a remote entry needs no working directory, and NO
        # `auth` either. fastmcp 3.2.0 keeps OAuth tokens in a MemoryStore, so
        # injecting `auth: "oauth"` would open a browser at every session end
        # instead of the one 401 → skip line the hook logs today.
        config = load_server_config(mcp_json, "tree-memory", cwd=mcp_json.parent)

        assert config["mcpServers"]["tree-memory"] == {
            "type": "http",
            "url": "https://tree-memory.fastmcp.app/mcp",
        }

    def test_unknown_server_raises_keyerror_naming_the_known_ones(
        self, mcp_json: Path
    ) -> None:
        with pytest.raises(KeyError) as excinfo:
            load_server_config(mcp_json, "nope", cwd=mcp_json.parent)

        message = str(excinfo.value)
        assert "tree-memory-local" in message and "tree-memory" in message

    def test_config_is_accepted_by_the_fastmcp_client(self, mcp_json: Path) -> None:
        # The stdio entry carries no "type" key — MCPConfig has to infer it, and
        # a config the Client cannot parse would only blow up at session end.
        from fastmcp import Client

        client = Client(
            load_server_config(mcp_json, "tree-memory-local", cwd=mcp_json.parent)
        )

        assert client is not None


class TestRun:
    async def test_logs_receipt(
        self,
        mcp_json: Path,
        hook_stdin: TextIO,
        mocker,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        receipt = json.dumps(
            {
                "source_uri": f"claude-session://{_SESSION_ID}",
                "duplicate": False,
                "document_id": None,
                "flow_run_id": "run-123",
                "status": "scheduled",
            }
        )
        fake = _FakeClient(receipt)
        mocker.patch("tree.mcp.hooks.Client", fake)

        with caplog.at_level(logging.INFO, logger=hooks.__name__):
            exit_code = await run(hook_stdin, mcp_json, "tree-memory-local")

        assert exit_code == 0
        assert fake.calls[0][0] == "ingest_conversation"
        assert fake.calls[0][1]["session_uri"] == f"claude-session://{_SESSION_ID}"
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert f"claude-session://{_SESSION_ID}" in logged
        assert "run-123" in logged

    async def test_duplicate_receipt_is_logged(
        self,
        mcp_json: Path,
        hook_stdin: TextIO,
        mocker,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        receipt = json.dumps(
            {
                "source_uri": f"claude-session://{_SESSION_ID}",
                "duplicate": True,
                "document_id": "68c0",
                "flow_run_id": None,
                "status": "duplicate",
            }
        )
        mocker.patch("tree.mcp.hooks.Client", _FakeClient(receipt))

        with caplog.at_level(logging.INFO, logger=hooks.__name__):
            exit_code = await run(hook_stdin, mcp_json, "tree-memory-local")

        assert exit_code == 0
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "duplicate=True" in logged

    async def test_short_transcript_skips(
        self, mcp_json: Path, tmp_path: Path, mocker, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "short.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-09-12T08:00:00.000Z",
                    "message": {"role": "user", "content": "two lines only"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        fake = _FakeClient("{}")
        mocker.patch("tree.mcp.hooks.Client", fake)
        stdin = io.StringIO(
            json.dumps({"session_id": _SESSION_ID, "transcript_path": str(path)})
        )

        with caplog.at_level(logging.INFO, logger=hooks.__name__):
            exit_code = await run(stdin, mcp_json, "tree-memory-local")

        assert exit_code == 0
        assert fake.calls == []
        assert "200" in "\n".join(record.getMessage() for record in caplog.records)

    async def test_missing_transcript_skips(
        self, mcp_json: Path, tmp_path: Path, mocker
    ) -> None:
        fake = _FakeClient("{}")
        mocker.patch("tree.mcp.hooks.Client", fake)
        stdin = io.StringIO(
            json.dumps(
                {
                    "session_id": _SESSION_ID,
                    "transcript_path": str(tmp_path / "gone.jsonl"),
                }
            )
        )

        assert await run(stdin, mcp_json, "tree-memory-local") == 0
        assert fake.calls == []

    async def test_empty_stdin_exits_zero(self, mcp_json: Path, mocker) -> None:
        fake = _FakeClient("{}")
        mocker.patch("tree.mcp.hooks.Client", fake)

        assert await run(io.StringIO("{}"), mcp_json, "tree-memory-local") == 0
        assert fake.calls == []

    async def test_unreachable_server_exits_zero(
        self,
        mcp_json: Path,
        hook_stdin: TextIO,
        mocker,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Local Mongo is down: the spawned server dies in its lifespan and the
        # client raises on connect. Claude Code still exits normally.
        def _boom(config: dict[str, Any]) -> Any:
            raise ConnectionError("server exited during initialization")

        mocker.patch("tree.mcp.hooks.Client", _boom)

        with caplog.at_level(logging.WARNING, logger=hooks.__name__):
            exit_code = await run(hook_stdin, mcp_json, "tree-memory-local")

        assert exit_code == 0
        assert "ConnectionError" in "\n".join(
            record.getMessage() for record in caplog.records
        )

    async def test_error_envelope_skips(
        self,
        mcp_json: Path,
        hook_stdin: TextIO,
        mocker,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        envelope = json.dumps(
            {
                "error_type": "pipeline_unavailable",
                "retryable": True,
                "message": "Prefect API unreachable at http://localhost:4200.",
            }
        )
        mocker.patch("tree.mcp.hooks.Client", _FakeClient(envelope))

        with caplog.at_level(logging.WARNING, logger=hooks.__name__):
            exit_code = await run(hook_stdin, mcp_json, "tree-memory-local")

        assert exit_code == 0
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "pipeline_unavailable" in logged
        assert "Prefect API unreachable" in logged

    async def test_unknown_server_name_exits_zero(
        self, mcp_json: Path, hook_stdin: TextIO, mocker
    ) -> None:
        # A typo in the hook argument is a config error, not a reason to fail
        # the session exit.
        fake = _FakeClient("{}")
        mocker.patch("tree.mcp.hooks.Client", fake)

        assert await run(hook_stdin, mcp_json, "tree-memory-typo") == 0
        assert fake.calls == []
