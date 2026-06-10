"""Root test configuration — shared by the unit and integration suites.

Disable Opik telemetry for the WHOLE suite (the headline 2026-06-10 fix)
------------------------------------------------------------------------
Tests run via ``make memory-*-tests``, whose Makefile does ``include .env`` /
``export`` — so a real ``OPIK_API_KEY`` is present in the test process. The
production code paths carry live ``@track`` / ``span`` instrumentation, and a
test that drives one of them with a **mocked** LLM would otherwise ship a real
trace into the production ``tree-memory`` Opik project: a trace with no Gemini
span and a 2-9ms duration. The human inspected one such test-pollution trace in
the dashboard and mistook it for a broken real-server ``query_memory`` call.

Setting ``OPIK_TRACK_DISABLE=true`` BEFORE ``opik`` is first imported makes
``opik.track`` (and every ``span`` / genai-client wrapper that routes through it)
a hard no-op for the entire test session — no trace, no span, no network call —
so the production Opik project only ever shows real server / pipeline traffic.
``setdefault`` so an operator can still flip it off explicitly when they
deliberately want a test run to emit telemetry (rare).

This must live in the ROOT conftest (collected before any test module imports
``tree.observability`` → ``opik``), not in ``tests/unit/conftest.py``, so the
integration suite is covered too.
"""

import os

os.environ.setdefault("OPIK_TRACK_DISABLE", "true")
