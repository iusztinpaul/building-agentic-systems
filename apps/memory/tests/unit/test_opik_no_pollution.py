"""Regression: the test suite must NEVER emit live Opik telemetry.

The headline bug (2026-06-10): the unit suite is run via ``make memory-unit-tests``,
whose Makefile does ``include .env`` / ``export`` — so a real ``OPIK_API_KEY`` is
present in the test process. The production functions (the MCP tools,
``execute_nl_query`` / ``nl_to_pipeline``, the pipeline flows) carry live
``@track`` / ``span`` instrumentation. When a test calls one of them with a
**mocked** LLM, the decorator still ships a real trace into the production
``tree-memory`` Opik project — a trace with no Gemini span and a 2-9ms duration.
The human inspected one such test-pollution trace in the dashboard and mistook it
for a broken real-server call.

The fix is a single global switch: the root ``tests/conftest.py`` sets
``OPIK_TRACK_DISABLE=true`` before ``opik`` is imported, which makes ``opik.track``
(and every ``span`` / genai wrapper that routes through it) a true no-op. These
tests pin that contract so it can't silently regress.
"""

from __future__ import annotations

import os


def test_opik_track_disable_env_is_set() -> None:
    """The suite-wide kill switch is present and truthy.

    Set by the root ``tests/conftest.py`` at import time (before ``opik`` is
    imported anywhere), so EVERY test — unit or integration — runs with Opik
    tracking disabled and cannot pollute the production project.
    """

    assert os.environ.get("OPIK_TRACK_DISABLE") == "true"


def test_opik_config_reads_track_disable() -> None:
    """Opik's own config object honors the env var → ``opik.track`` no-ops.

    This is the mechanism the env var rides on: ``OpikConfig.track_disable`` is
    backed by ``OPIK_TRACK_DISABLE`` and gates trace/span creation inside the SDK.
    """

    from opik import config

    assert config.OpikConfig().track_disable is True


async def test_tracked_production_fn_emits_no_trace(mocker) -> None:
    """A live ``@track``-decorated production fn creates NO trace under tests.

    Drives the REAL ``opik.track`` decorator (NOT mocked) the way a unit test of
    ``nl_to_pipeline`` would, with a mocked LLM. With the kill switch on, the SDK
    never starts a trace, so ``opik.opik_context.get_current_trace_data()`` stays
    ``None`` inside the decorated body. Before the fix this returned a live trace
    object (the pollution).
    """

    import opik
    from opik import opik_context

    seen: dict[str, object | None] = {}

    @opik.track(name="pollution_probe")
    def _probe() -> None:
        seen["trace"] = opik_context.get_current_trace_data()

    _probe()

    assert seen["trace"] is None


def test_seam_reports_opik_disabled() -> None:
    """The seam itself treats the kill switch as "Opik is off".

    ``opik.track`` honors ``OPIK_TRACK_DISABLE`` natively, but
    ``opik.start_as_current_span`` (which :func:`tree.observability.span` rides on)
    does NOT — it still ships traces to the backend. So the seam folds the env var
    into :func:`is_opik_configured`, which gates EVERY helper (``span`` included).
    This is the assertion that proves a pipeline ``span()`` call won't pollute.
    """

    import tree.observability as obs

    assert obs.is_opik_configured() is False


def test_seam_span_does_not_touch_sdk(mocker) -> None:
    """``span()`` no-ops at the seam — ``start_as_current_span`` is never called.

    The production pipeline flows open their root/task spans via this helper. With
    the kill switch on it must short-circuit BEFORE the SDK, so no trace is created
    or uploaded. Before the seam fix the body still called the SDK and a trace
    reached the backend even though ``@track`` was disabled.
    """

    import tree.observability as obs

    start = mocker.patch("tree.observability.opik.start_as_current_span")

    ran = False
    with obs.span("pollution_probe_span", tags=["ingestion", "batch"]):
        ran = True

    assert ran is True
    start.assert_not_called()
