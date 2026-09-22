"""The Modal test doubles more than one test module needs (PR #44, Nit 3).

Imported into ``tests/unit/models/conftest.py`` — which is how the fixtures
below reach every module in this package — and imported DIRECTLY by
``tests/unit/scripts/test_modal_model_script.py``, because a conftest only
serves its own directory tree and that module lives in a sibling one. Same
mechanism as ``tests/prefect_doubles.py``, already imported by four modules.

What lives here is what was byte-identical in two places, or differed only in
which MODULE it patches (``tree.models.modal_embedding`` vs
``tree.models.modal_llm``) — hence the two ``patch_*`` factories rather than
two fixtures. The doubles that differ in SHAPE (the embeddings recorder vs the
chat one, the two wire stubs) stay in their own modules: one class serving both
wire contracts reads worse than the two it replaces.
"""

from __future__ import annotations

import asyncio
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

from tree.models import modal_router


def endpoint_row(name: str, status: str = "live") -> dict[str, str]:
    """A ``modal endpoint list --json`` row (modal 1.5.5 columns).

    ``live`` and ``provisioning`` are the two statuses the live run saw
    (``tasks/141``, 2026-09-21); the id is fake, as every id here is.
    """

    return {
        "name": name,
        "endpoint_id": "ep-FAKE0000000000000000",
        "status": status,
        "created_at": "2026-08-24T10:00:00Z",
        "created_by": "someone",
    }


def app_row(description: str, state: str = "deployed") -> dict[str, str]:
    """A ``modal app list --json`` row (modal 1.5.5 columns)."""

    return {
        "app_id": "ap-abcdefghijklmnopqrstuv",
        "description": description,
        "state": state,
        "tasks": "0",
        "created_at": "2026-09-20T10:00:00Z",
        "stopped_at": "",
    }


@pytest.fixture
def hub(mocker) -> SimpleNamespace:
    """The Hugging Face API, faked at the transport.

    Returns a namespace with ``cards`` (repo id -> the JSON to answer),
    ``status`` (repo id -> an HTTP status to answer instead), ``requests``
    (every request made) and ``error`` (an exception the transport raises).
    The default answer is a card with no ``base_model``, which is the
    voyage-4-nano case (no lineage -> the App).

    The router reads a model's fine-tune lineage whenever Modal answers "not in
    the catalog", so a test that drives the router un-mocked reaches
    huggingface.co — which is why the driver's module requests this for EVERY
    test (an autouse wrapper there), while the router's own tests take it by
    name.
    """

    state = SimpleNamespace(cards={}, status={}, requests=[], error=None, body=None)

    def _handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        if state.error is not None:
            raise state.error
        repo_id = str(request.url).split("/api/models/")[-1]
        if repo_id in state.status:
            return httpx.Response(state.status[repo_id], json={"error": "nope"})
        if state.body is not None:
            return httpx.Response(200, content=state.body)
        return httpx.Response(200, json=state.cards.get(repo_id, {"cardData": {}}))

    transport = httpx.MockTransport(_handler)

    def _client(**kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=transport, **kwargs)

    # The SHIM replaces the httpx reference the router holds, never
    # `httpx.Client` itself: openai subclasses that class at import time, and a
    # patched-out class breaks every later import in the session.
    mocker.patch.object(
        modal_router,
        "httpx",
        SimpleNamespace(Client=_client, HTTPStatusError=httpx.HTTPStatusError),
    )
    return state


def patch_server(
    mocker, module: ModuleType, *, url: str, served_model: str
) -> SimpleNamespace:
    """Patch the three coroutines a Modal client's warm body awaits.

    Patched on the CLIENT's bindings (``module`` is ``modal_embedding`` or
    ``modal_llm``), so the real modules — and the ``modal`` SDK
    ``modal_server`` imports — are never exercised.
    """

    return SimpleNamespace(
        resolve=mocker.patch.object(
            module,
            "resolve_server_url",
            new_callable=mocker.AsyncMock,
            return_value=url,
        ),
        poll=mocker.patch.object(
            module,
            "poll_health",
            new_callable=mocker.AsyncMock,
            return_value=1.5,
        ),
        served=mocker.patch.object(
            module,
            "served_model_id",
            new_callable=mocker.AsyncMock,
            return_value=served_model,
        ),
    )


class SuspendingServer:
    """The three coroutines of the warm body — counting, and yielding.

    An ``AsyncMock`` completes without ever suspending, so a ``gather`` over it
    runs each coroutine to completion in turn and a check-then-act race cannot
    even appear. The real ones hold a network round trip (the poll holds up to
    600 s of them); these hold the smallest thing that reschedules,
    ``asyncio.sleep(0)``.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self.resolve_calls = 0
        self.poll_calls = 0
        self.models_calls = 0
        self.poll_error: Exception | None = None

    async def resolve(self, entry: Any) -> str:
        self.resolve_calls += 1
        await asyncio.sleep(0)
        return self._url

    async def poll(
        self, url: str, headers: dict[str, str], *, deadline_s: float
    ) -> float:
        self.poll_calls += 1
        await asyncio.sleep(0)
        if self.poll_error is not None:
            raise self.poll_error
        return 1.5

    async def served(self, url: str, bearer: str, default: str) -> str:
        self.models_calls += 1
        await asyncio.sleep(0)
        return default


def patch_suspending_server(
    mocker, module: ModuleType, *, url: str
) -> SuspendingServer:
    """Patch the warm body's three coroutines with suspending doubles."""

    helpers = SuspendingServer(url)
    mocker.patch.object(module, "resolve_server_url", helpers.resolve)
    mocker.patch.object(module, "poll_health", helpers.poll)
    mocker.patch.object(module, "served_model_id", helpers.served)
    return helpers
