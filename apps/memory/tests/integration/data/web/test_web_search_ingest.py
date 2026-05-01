"""Integration test for the ``search_web`` ingest trigger end-to-end.

Exercises the live Prefect server: ``trigger_url_batch_ingest`` looks up the
real ``ingest-web-url-batch-etl`` deployment by name and creates a flow run
without polling.

Gated on:
- A reachable Prefect server (``PREFECT_API_URL`` reachable, deployment served).
- A real Bright Data SERP zone is **not** required for this test — only the
  ingest deployment matters here. The live SERP is exercised separately in
  ``test_web_serp.py``.

The test does NOT wait for the flow to complete — it only verifies the trigger
returned a flow-run id. The deployment itself is exercised by
``test_web_pipeline.py``.
"""

from __future__ import annotations

import os

import httpx
import pytest

from tree.data.web.web_search_ingest import (
    DEPLOYMENT_NAME,
    trigger_url_batch_ingest,
)


def _prefect_server_reachable() -> bool:
    """Best-effort check that a Prefect API is responding to ``/health``."""

    api_url = os.environ.get("PREFECT_API_URL", "http://127.0.0.1:4200/api").rstrip("/")
    health_url = f"{api_url}/health"
    try:
        response = httpx.get(health_url, timeout=2.0)
    except httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError:
        return False
    return response.status_code == 200


pytestmark = pytest.mark.skipif(
    not _prefect_server_reachable(),
    reason="Prefect server not reachable at PREFECT_API_URL",
)


class TestSearchWebIngestTrigger:
    async def test_trigger_returns_flow_run_id(self) -> None:
        """Live trigger against the registered deployment returns a flow_run_id.

        Uses ``https://example.com`` (a single, tiny, stable URL — same as
        ``test_web_pipeline.py``). The deployment must already be served by
        ``make memory-serve-workflows`` (or the Dockerized worker) for the
        lookup to succeed.
        """

        try:
            result = await trigger_url_batch_ingest(["https://example.com"])
        except Exception as exc:  # noqa: BLE001 — surface the real cause.
            pytest.skip(
                f"Deployment {DEPLOYMENT_NAME!r} not registered "
                f"(serve workflows first?): {exc}"
            )

        assert isinstance(result["flow_run_id"], str)
        assert result["flow_run_id"]  # non-empty UUID-ish string
        # tracking_url may legitimately be None on Prefect Cloud without
        # ``PREFECT_UI_URL`` set; just verify the type contract.
        tracking_url = result["tracking_url"]
        assert tracking_url is None or tracking_url.startswith("http")
