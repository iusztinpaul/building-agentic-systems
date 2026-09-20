import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tree.config.settings import settings
from tree.db import init_mongodb
from tree.models import modal_cli

TEST_DATABASE = "unit_tests_twin"


@pytest.fixture(autouse=True)
def _noop_voyage_rate_limit(mocker) -> None:
    """No-op the shared Voyage ``rate_limit`` for unit tests (no Prefect server).

    Both Voyage clients (ADR-002 §1) await
    ``rate_limit("voyage-embeddings", strict=False)`` immediately before every
    real network POST. With ``strict=False`` a missing limit is already a no-op,
    but the call still spends ~3s trying to reach a Prefect server that unit
    boxes don't run. Stub it in BOTH client modules so unit tests stay fast and
    server-independent. Tests that assert on the limiter re-patch the same
    per-module target locally with a spy, which transparently overrides this
    autouse stub for their duration.
    """

    mocker.patch("tree.models.voyage_embedding.rate_limit", new_callable=AsyncMock)
    mocker.patch(
        "tree.models.voyage_multimodal_embedding.rate_limit", new_callable=AsyncMock
    )


@pytest.fixture(autouse=True)
def _modal_dry_run(monkeypatch) -> None:
    """Every unit test runs with ``TREE_MODAL_DRY_RUN=1`` (ADR-009 §3).

    The suite-wide safety rail from the 2026-09-20 incident: a test that
    invokes the deploy driver without mocking ``subprocess.run`` would
    otherwise reach the REAL, authenticated ``modal`` CLI — ``make`` runs
    ``uv run``, which puts ``.venv/bin`` first, so no ``PATH`` shim intercepts
    it. With the variable set, such a test dry-runs and starts no process.

    The ``run`` fixture below is the ONLY thing that leaves the rail, and it
    replaces ``subprocess.run`` in the same act — so "no dry run" and "mocked"
    can never come apart.
    """

    monkeypatch.setenv(modal_cli.DRY_RUN_ENV, "1")


@pytest.fixture
def run(mocker, monkeypatch):
    """The ``modal`` CLI, faked at the ONE ``subprocess.run`` door.

    Returns the spy, with a ``state`` namespace steering what the fake CLI
    answers:

    * ``endpoints`` / ``apps`` — the rows ``modal endpoint list --json`` and
      ``modal app list --json`` return (the existence guard's input);
    * ``list_returncode`` / ``list_stdout`` — make a list call fail or answer
      something that is not a JSON list (the guard must fail CLOSED);
    * ``result`` — the ``CompletedProcess`` every OTHER command returns;
    * ``error`` — an exception that command raises instead (e.g. the
      ``FileNotFoundError`` of a missing CLI), leaving the list calls intact.
    """

    monkeypatch.delenv(modal_cli.DRY_RUN_ENV, raising=False)

    state = SimpleNamespace(
        endpoints=[],
        apps=[],
        list_returncode=0,
        list_stdout=None,
        result=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        error=None,
    )

    def _fake_modal(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[-2:] == ["list", "--json"]:
            rows = state.endpoints if argv[1] == "endpoint" else state.apps
            stdout = (
                json.dumps(rows) if state.list_stdout is None else state.list_stdout
            )
            return subprocess.CompletedProcess(
                args=argv, returncode=state.list_returncode, stdout=stdout, stderr=""
            )
        if state.error is not None:
            raise state.error
        return state.result

    spy = mocker.patch.object(modal_cli.subprocess, "run", side_effect=_fake_modal)
    spy.state = state
    return spy


@pytest.fixture(scope="session", autouse=True)
async def _init_beanie():
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(), TEST_DATABASE
    )

    yield

    await client.drop_database(TEST_DATABASE)
    await client.close()
