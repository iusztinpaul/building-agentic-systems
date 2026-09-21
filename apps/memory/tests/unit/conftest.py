import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tree.config.app_config import EmbeddingConfig, app_config
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.models import modal_cli

TEST_DATABASE = "unit_tests_twin"

# What a unit test is told when it reaches the Modal SDK for real. The message
# names the two ways out, because the engineer reading it is mid-debug.
MODAL_SDK_RAIL_MESSAGE = (
    "live Modal SDK lookup from a unit test — patch resolve_server_url / use "
    "the modal_seam fixture"
)

# Hung on the raiser so a test can prove the rail is installed (or lifted)
# without calling anything — calling is what we are preventing.
MODAL_SDK_RAIL_MARKER = "_tree_modal_sdk_rail"

# The SDK's lookup entry points, as ``(class, attribute)``. ``Server.from_name``
# is the ONE the source actually calls (`grep -rn "modal\." apps/memory/src` ->
# `tree/models/modal_server.py:147`, reached by `ModalEmbeddingModel`,
# `ModalLLM`, both smoke tests and the **Pre-warm**); ``Function.from_name`` and
# ``App.lookup`` are the neighbouring doors a future client would use, closed
# now so the rail does not have to be remembered twice.
#
# NOT closed: ``Volume.from_name`` / ``Secret.from_name`` / ``Dict`` / ``Queue``.
# They hand back a LAZY handle — no lookup happens at the call — so raising
# there would fire on something that is not a live lookup, and they appear only
# in `deploy/modal_*.py`, which the unit suite parses as text and never imports.
_MODAL_SDK_DOORS = (
    ("Server", "from_name"),
    ("Function", "from_name"),
    ("App", "lookup"),
    ("Cls", "from_name"),
)


def _raise_live_modal_lookup(*_args: object, **_kwargs: object) -> object:
    raise AssertionError(MODAL_SDK_RAIL_MESSAGE)


setattr(_raise_live_modal_lookup, MODAL_SDK_RAIL_MARKER, True)


def _install_modal_sdk_rail(mocker) -> list[str]:
    """Replace every SDK lookup door with the raiser; name what was closed.

    ``modal`` is imported HERE, not at conftest import time: it lives in the
    optional ``local-models`` extra (``pyproject.toml``), so an environment
    without it must get a no-op rail — an empty list — rather than a collection
    error for the whole unit suite. Nothing can reach the SDK there anyway.
    """

    try:
        import modal
    except ImportError:
        return []

    closed: list[str] = []
    for class_name, attribute in _MODAL_SDK_DOORS:
        door = getattr(modal, class_name, None)
        if door is not None and hasattr(door, attribute):
            mocker.patch.object(door, attribute, new=_raise_live_modal_lookup)
            closed.append(f"modal.{class_name}.{attribute}")
    return closed


def _embedding_double(cfg: EmbeddingConfig) -> str:
    """Name which embedding BLOCK the factory was asked for, by identity."""

    if cfg is app_config.models.resolution_embedding:
        return "RESOLUTION-EMBEDDING"
    assert cfg is app_config.models.search_embedding
    return "SEARCH-EMBEDDING"


@pytest.fixture
def modal_seam(mocker) -> SimpleNamespace:
    """The **Pre-warm** seam's gate: per-block providers + factory TRIPWIRES.

    ``tree.models.get_model.modal_backed_models`` reads
    ``app_config.models.<block>.provider`` and calls that block's factory in
    the ``get_model`` namespace — so patching ``tree.memory.pipeline.get_llm``
    does NOT reach it, and a real ``ModalLLM`` would lazy-import the Modal SDK
    into the test process. Both callers (the helper's own tests and the
    pipeline seam's) need the same two doubles, hence one fixture:

    * ``providers(llm=…, resolution=…, search=…)`` — defaults are the SHIPPED
      ones (``gemini`` / ``voyage`` / ``voyage``), so a test names only what it
      changes;
    * ``llm`` / ``embedding`` — the patched ``get_llm`` and
      ``_build_embedding_model``. A call to either under a non-Modal provider
      IS the regression of #149's QA finding 2.
    """

    def _providers(
        *,
        llm: str = "gemini",
        resolution: str = "voyage",
        search: str = "voyage",
    ) -> None:
        for block, provider in (
            ("llm", llm),
            ("resolution_embedding", resolution),
            ("search_embedding", search),
        ):
            mocker.patch(
                f"tree.models.get_model.app_config.models.{block}.provider", provider
            )

    return SimpleNamespace(
        providers=_providers,
        llm=mocker.patch("tree.models.get_model.get_llm", return_value="LLM"),
        embedding=mocker.patch(
            "tree.models.get_model._build_embedding_model",
            side_effect=_embedding_double,
        ),
    )


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
def modal_sdk_allowed() -> None:
    """The ONE opt-out from the SDK rail below — request it by name.

    For a test that legitimately needs the REAL ``modal.Server.from_name`` (a
    live-fixture diagnostic, say). Nothing uses it today; it exists so the rail
    is never lifted by deleting it. Requesting it is the whole signal — the
    rail reads ``request.fixturenames``.
    """

    return None


@pytest.fixture(autouse=True)
def _no_live_modal_sdk(request, mocker) -> None:
    """The SECOND suite-wide rail: the Modal **SDK** door (#152 QA).

    :func:`_modal_dry_run` above closes the CLI door (``modal_cli``'s one
    ``subprocess.run``). It does nothing for the SDK door — ``modal.Server
    .from_name`` in :mod:`tree.models.modal_server`, which every Modal client,
    both smoke tests and the **Pre-warm** resolve through. A test that flips a
    provider to ``modal`` without patching the ``get_model``-level factory
    reaches Modal for real; #152's implementation hit exactly that and fixed
    the ONE fixture that tripped it, which is convention, not a rail.

    So: every unit test runs with those entry points replaced by a raiser (see
    :func:`_install_modal_sdk_rail`, which also imports the SDK lazily). It
    composes with the tests that patch ``modal.Server`` themselves — theirs is
    applied inside the test and wins for its duration — and it costs nothing:
    the whole suite passes with the rail in place, with no test opting out.
    """

    if "modal_sdk_allowed" in request.fixturenames:
        return

    _install_modal_sdk_rail(mocker)


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
    * ``results`` — a QUEUE consumed one command at a time when a test needs
      the answers to differ (the router runs up to three: a refused create, a
      second create, a deploy). Empty falls back to ``result``;
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
        results=[],
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
        if state.results:
            return state.results.pop(0)
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
