"""Unit tests for ``tree.models.modal_server`` — the ONE URL lookup, the ONE
authenticated health check and the ONE smoke test every **Serving path**
shares (ADR-009 §3/§4).

No test touches the network: ``modal.Server`` and ``aiohttp.ClientSession``
are replaced by fakes, and the **Proxy token** is always the fake pair
``wk-1`` / ``ws-2``. What is asserted is the CONTRACT the driver and the
``ModalEmbeddingModel`` client (#140) both depend on — the wire shape (no
``dimensions`` key, the prompted inputs, the discovered model id), the vector
width the memory stores, and that a public server is caught.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import aiohttp
import pytest
from pydantic import SecretStr

from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_server import (
    resolve_server_url,
    served_model_id,
    smoke_test,
    wait_until_healthy,
)
from tree.models import modal_catalog
from tree.models.modal_catalog import get_catalog_entry

_QWEN = "Qwen/Qwen3-Embedding-0.6B"
_VOYAGE = "voyageai/voyage-4-nano"
_URL = "https://acme--ep-tree-qwen3-embedding-0-6b-server.modal.run"
_BEARER = "wk-1.ws-2"


# --- HTTP doubles -----------------------------------------------------------


class _FakeResponse:
    """One canned aiohttp response, usable as an async context manager."""

    def __init__(self, status: int, payload: dict[str, Any] | None = None) -> None:
        self.status = status
        self._payload = payload if payload is not None else {}

    async def json(self) -> dict[str, Any]:
        return self._payload

    async def text(self) -> str:
        return str(self._payload)

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _Recorder:
    """Every HTTP call the module made, in order."""

    def __init__(self, responses: list[_FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.session_kwargs: list[dict[str, Any]] = []

    def next_response(self, **call: Any) -> _FakeResponse:
        self.calls.append(call)
        if not self.responses:
            raise AssertionError(f"unexpected extra HTTP call: {call}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeSession:
    def __init__(self, recorder: _Recorder, **kwargs: Any) -> None:
        self._recorder = recorder
        recorder.session_kwargs.append(kwargs)

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._recorder.next_response(method="GET", url=url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._recorder.next_response(method="POST", url=url, **kwargs)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


@pytest.fixture
def http(mocker):
    """Patch ``aiohttp.ClientSession``; the returned recorder is programmed
    per test with :meth:`_Recorder.responses`."""

    recorder = _Recorder([])
    mocker.patch.object(
        aiohttp,
        "ClientSession",
        lambda **kwargs: _FakeSession(recorder, **kwargs),
    )
    return recorder


@pytest.fixture
def proxy_token(mocker):
    """A FAKE proxy token — never the developer's own (Make exports ``.env``)."""

    mocker.patch.object(
        modal_catalog.settings, "modal_proxy_token_id", SecretStr("wk-1")
    )
    mocker.patch.object(
        modal_catalog.settings, "modal_proxy_token_secret", SecretStr("ws-2")
    )


@pytest.fixture
def modal_server(mocker):
    """Patch ``modal.Server`` so the lookup resolves to a known URL."""

    server = mocker.MagicMock()
    server.get_url.aio = mocker.AsyncMock(return_value=_URL)
    server_cls = mocker.patch("tree.models.modal_server.modal.Server")
    server_cls.from_name.return_value = server
    return server_cls


# --- vector helpers ---------------------------------------------------------


def _sparse(width: int, components: dict[int, float]) -> list[float]:
    """A ``width``-wide vector with ``components`` set and the rest zero."""

    vector = [0.0] * width
    for index, value in components.items():
        vector[index] = value
    return vector


def _embeddings_payload(vectors: list[list[float]]) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
    }


def _voyage_vectors() -> list[list[float]]:
    """Query / relevant / unrelated at voyage-4-nano's NATIVE 2048 width, with
    the ranking signal inside the first 1024 components (so truncation keeps
    the ordering)."""

    query = _sparse(2048, {0: 1.0})
    relevant = _sparse(2048, {0: 0.9, 1: 0.436})
    unrelated = _sparse(2048, {0: 0.1, 1: 0.995})
    return [query, relevant, unrelated]


def _smoke_responses(
    vectors: list[list[float]],
    *,
    served_id: str = "modal-recipe/qwen3-embedding-0-6b",
    unauthenticated_status: int = 401,
) -> list[_FakeResponse]:
    """The four calls one smoke test makes, in order."""

    return [
        _FakeResponse(200),  # authenticated GET /health
        _FakeResponse(200, {"data": [{"id": served_id}]}),  # GET /v1/models
        _FakeResponse(200, _embeddings_payload(vectors)),  # POST /v1/embeddings
        _FakeResponse(unauthenticated_status),  # header-less GET /health
    ]


class TestResolveServerUrl:
    """H1 (ADR-009 §3): a Dedicated endpoint named ``N`` is the Modal app
    ``ep-N`` with a server class ``Server`` — ONE lookup for all three paths."""

    async def test_looks_the_app_up_by_its_derived_name(self, modal_server) -> None:
        entry = get_catalog_entry(_QWEN)

        url = await resolve_server_url(entry)

        modal_server.from_name.assert_called_once_with(
            "ep-tree-qwen3-embedding-0-6b", "Server"
        )
        assert url == _URL

    @pytest.mark.parametrize(
        "resolved",
        [f"{_URL}/", f"{_URL}/v1", f"{_URL}/v1/"],
        ids=["trailing-slash", "v1", "v1-slash"],
    )
    async def test_returns_the_root_url(self, mocker, resolved: str) -> None:
        """Callers append ``/health`` and ``/v1/embeddings`` themselves, so the
        lookup must hand back the ROOT."""

        server = mocker.MagicMock()
        server.get_url.aio = mocker.AsyncMock(return_value=resolved)
        server_cls = mocker.patch("tree.models.modal_server.modal.Server")
        server_cls.from_name.return_value = server

        assert await resolve_server_url(get_catalog_entry(_QWEN)) == _URL

    async def test_a_failing_lookup_tells_the_operator_what_to_run(
        self, mocker
    ) -> None:
        server_cls = mocker.patch("tree.models.modal_server.modal.Server")
        server_cls.from_name.side_effect = RuntimeError("not found")

        with pytest.raises(ModelError) as excinfo:
            await resolve_server_url(get_catalog_entry(_QWEN))

        message = str(excinfo.value)
        assert "ep-tree-qwen3-embedding-0-6b/Server" in message
        assert f"make memory-deploy-embedding-model MODEL={_QWEN}" in message
        assert "modal endpoint list" in message

    async def test_an_empty_url_is_a_failure(self, mocker) -> None:
        """A hydrated Server with no URL is not a usable server — failing here
        beats a request to ``"/health"``."""

        server = mocker.MagicMock()
        server.get_url.aio = mocker.AsyncMock(return_value="")
        server_cls = mocker.patch("tree.models.modal_server.modal.Server")
        server_cls.from_name.return_value = server

        with pytest.raises(ModelError) as excinfo:
            await resolve_server_url(get_catalog_entry(_QWEN))

        assert "make memory-deploy-embedding-model" in str(excinfo.value)


class TestWaitUntilHealthy:
    async def test_sends_the_proxy_token_and_returns_the_elapsed_seconds(
        self, http
    ) -> None:
        http.responses = [_FakeResponse(200)]

        elapsed = await wait_until_healthy(_URL, _BEARER, 30.0)

        assert elapsed >= 0
        assert http.session_kwargs[0]["headers"] == {
            "Authorization": f"Bearer {_BEARER}"
        }
        assert http.calls[0]["url"] == f"{_URL}/health"

    async def test_401_names_both_env_vars_and_is_not_retryable(self, http) -> None:
        """A wrong **Proxy token** is an operator error, not a transient one —
        so it must NOT surface as the retryable ``ExtractionError``."""

        http.responses = [_FakeResponse(401)]

        with pytest.raises(ModelError) as excinfo:
            await wait_until_healthy(_URL, _BEARER, 30.0)

        assert not isinstance(excinfo.value, ExtractionError)
        message = str(excinfo.value)
        assert "MODAL_PROXY_TOKEN_ID" in message
        assert "MODAL_PROXY_TOKEN_SECRET" in message

    async def test_any_other_status_is_transient(self, http) -> None:
        http.responses = [_FakeResponse(503)]

        with pytest.raises(ExtractionError) as excinfo:
            await wait_until_healthy(_URL, _BEARER, 30.0)

        assert "503" in str(excinfo.value)

    async def test_a_transport_error_is_transient(self, http) -> None:
        http.responses = [TimeoutError("timed out")]

        with pytest.raises(ExtractionError):
            await wait_until_healthy(_URL, _BEARER, 30.0)


class TestServedModelId:
    """A managed recipe — not us — names the model it serves, and vLLM rejects
    an unknown ``model`` (ADR-009 §3)."""

    async def test_returns_the_first_served_id(self, http) -> None:
        http.responses = [_FakeResponse(200, {"data": [{"id": _QWEN}]})]

        assert await served_model_id(_URL, _BEARER, "fallback") == _QWEN
        assert http.calls[0]["url"] == f"{_URL}/v1/models"

    async def test_an_empty_list_falls_back_with_a_warning(self, http, caplog) -> None:
        http.responses = [_FakeResponse(200, {"data": []})]

        with caplog.at_level(logging.WARNING):
            assert await served_model_id(_URL, _BEARER, _QWEN) == _QWEN

        assert [r.levelname for r in caplog.records] == ["WARNING"]

    async def test_a_failing_call_falls_back_with_a_warning(self, http, caplog) -> None:
        """Discovery is best-effort: the catalog id is a good guess, and the
        embeddings POST is where a wrong name surfaces loudly."""

        http.responses = [_FakeResponse(500)]

        with caplog.at_level(logging.WARNING):
            assert await served_model_id(_URL, _BEARER, _QWEN) == _QWEN

        assert [r.levelname for r in caplog.records] == ["WARNING"]


@pytest.mark.usefixtures("proxy_token", "modal_server")
class TestSmokeTest:
    async def test_voyage_reports_native_and_truncated_widths(
        self, http, caplog
    ) -> None:
        """Story 6: the run proves BOTH the 2048-d wire width and the 1024-d
        vector the memory actually stores."""

        http.responses = _smoke_responses(_voyage_vectors())

        with caplog.at_level(logging.INFO):
            report = await smoke_test(_VOYAGE)

        assert report.url == _URL
        assert report.served_model == "modal-recipe/qwen3-embedding-0-6b"
        assert report.dimensions == 2048
        assert report.truncated_dimensions == 1024
        assert report.unauthenticated_status == 401
        assert report.cos_relevant > report.cos_unrelated

        messages = [record.getMessage() for record in caplog.records]
        assert "3 embeddings, 2048 dims" in messages
        assert any(
            m.startswith("truncated 2048 -> 1024 dims client-side") for m in messages
        )
        assert any(m.startswith("sanity@1024:") for m in messages)
        assert "unauthenticated health -> 401" in messages
        assert "Smoke test passed" in messages

    async def test_posts_three_prompted_inputs_without_a_dimensions_key(
        self, http
    ) -> None:
        """The **Embedding role** is a client-side prefix (ADR-009 §5), the
        model name is the DISCOVERED one, and no request of ours ever carries
        an OpenAI ``dimensions`` parameter (ADR-009 §3)."""

        http.responses = _smoke_responses(_voyage_vectors())

        await smoke_test(_VOYAGE)

        post = next(call for call in http.calls if call["method"] == "POST")
        assert post["url"] == f"{_URL}/v1/embeddings"
        body = post["json"]
        assert body["model"] == "modal-recipe/qwen3-embedding-0-6b"
        assert body["encoding_format"] == "float"
        assert "dimensions" not in body
        inputs = body["input"]
        assert len(inputs) == 3
        assert inputs[0].startswith(
            "Represent the query for retrieving supporting documents: "
        )
        assert inputs[1].startswith("Represent the document for retrieval: ")
        assert inputs[2].startswith("Represent the document for retrieval: ")

    async def test_a_narrower_vector_than_the_catalog_says_fails(self, http) -> None:
        """Story 2's most likely failure: a Dedicated endpoint over base Qwen3
        has no 1024->2048 projection head."""

        narrow = [_sparse(1024, {0: 1.0}) for _ in range(3)]
        http.responses = _smoke_responses(narrow)

        with pytest.raises(ModelError) as excinfo:
            await smoke_test(_VOYAGE)

        assert "expected 2048 dims, got 1024" in str(excinfo.value)

    async def test_an_unrelated_document_scoring_higher_fails(self, http) -> None:
        """Well-shaped garbage: the right width, the wrong architecture."""

        query = _sparse(2048, {0: 1.0})
        relevant = _sparse(2048, {0: 0.3, 1: 0.954})
        unrelated = _sparse(2048, {0: 0.8, 1: 0.6})
        http.responses = _smoke_responses([query, relevant, unrelated])

        with pytest.raises(ModelError) as excinfo:
            await smoke_test(_VOYAGE)

        assert "sanity check failed" in str(excinfo.value)

    async def test_an_ordering_that_flips_under_truncation_fails(self, http) -> None:
        """The ordering at 2048 is not the ordering the 1024-d index sees —
        and 1024 is the width the memory stores."""

        query = _sparse(2048, {0: 0.1, 1500: 0.99499})
        relevant = _sparse(2048, {1: 0.01, 1500: 0.99995})
        unrelated = _sparse(2048, {0: 1.0})
        http.responses = _smoke_responses([query, relevant, unrelated])

        with pytest.raises(ModelError) as excinfo:
            await smoke_test(_VOYAGE)

        assert "sanity@1024" in str(excinfo.value)

    async def test_a_public_server_is_caught(self, http) -> None:
        """Story 9: auth is the thing a deploy can silently get wrong."""

        http.responses = _smoke_responses(_voyage_vectors(), unauthenticated_status=200)

        with pytest.raises(ModelError) as excinfo:
            await smoke_test(_VOYAGE)

        assert "expected 401" in str(excinfo.value)

    async def test_qwen_has_no_truncation_step(self, http, caplog) -> None:
        """An entry whose ``matryoshka_dimensions`` is empty is stored at its
        native width — nothing to truncate, nothing to log."""

        vectors = [
            _sparse(1024, {0: 1.0}),
            _sparse(1024, {0: 0.9, 1: 0.436}),
            _sparse(1024, {0: 0.1, 1: 0.995}),
        ]
        http.responses = _smoke_responses(vectors)

        with caplog.at_level(logging.INFO):
            report = await smoke_test(_QWEN)

        assert report.dimensions == 1024
        assert report.truncated_dimensions is None
        messages = [record.getMessage() for record in caplog.records]
        assert "3 embeddings, 1024 dims" in messages
        assert not any("truncated" in message for message in messages)

    async def test_the_unauthenticated_probe_sends_no_authorization_header(
        self, http
    ) -> None:
        http.responses = _smoke_responses(_voyage_vectors())

        await smoke_test(_VOYAGE)

        # Four sessions in order: health, /v1/models, embeddings, public probe.
        assert "Authorization" not in http.session_kwargs[-1].get("headers", {})

    async def test_the_truncated_vector_is_unit_length(self, http) -> None:
        """What the report promises is what ``truncate_embedding`` produced —
        the 1024-d unit vector the mongot index receives."""

        http.responses = _smoke_responses(_voyage_vectors())

        report = await smoke_test(_VOYAGE)

        assert report.truncated_dimensions == 1024
        assert math.isclose(report.cos_relevant, 0.9, abs_tol=1e-3)
