"""Unit tests for ``tree.models.modal_server`` — the ONE URL lookup, the ONE
served-model discovery and the ONE smoke test PER KIND that every **Serving
path** shares (ADR-009 §3/§4/§10). Waiting out a cold start lives in
``tree.models.modal_warmup`` and is tested there; here what matters is that
both smoke tests call THAT poller, with the configured budget (ADR-009 §11).

No test touches the network: ``modal.Server`` and ``aiohttp.ClientSession``
are replaced by fakes, and the **Proxy token** is always the fake pair
``wk-1`` / ``ws-2``. What is asserted is the CONTRACT the driver and the two
clients depend on — the wire shape (no ``dimensions`` key, the prompted
inputs, the discovered model id, the strict ``city_facts`` schema), the vector
width the memory stores, and that a public server is caught.
"""

from __future__ import annotations

import logging
import math
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from pydantic import SecretStr

from tree.config.app_config import ModalLLMModelConfig, app_config
from tree.models.exceptions import ExtractionError, ModelError
from tree.models import modal_catalog, modal_llm
from tree.models.modal_llm import ModalLLM
from tree.models.modal_server import (
    chat_smoke_test,
    resolve_server_url,
    served_model_id,
    smoke_test,
)
from tree.models.modal_catalog import (
    chat_request_knobs,
    get_catalog_entry,
    get_llm_entry,
)

_QWEN = "Qwen/Qwen3-Embedding-0.6B"
_VOYAGE = "voyageai/voyage-4-nano"
_LFM = "LiquidAI/LFM2.5-350M"
_QWEN_LLM = "Qwen/Qwen3.5-0.8B"
# A catalog id the ``bare_entry`` fixture adds: an LLM entry with NEITHER
# request knob, which no seed is any more.
_BARE = "acme/bare-llm"
_URL = "https://acme--ep-tree-qwen3-embedding-0-6b-server.modal.run"
_BEARER = "wk-1.ws-2"
# What a thinking model spent its budget on, at the LENGTH the live run
# measured (``tasks/141`` round 1). Only that length may ever be reported.
_REASONING = ("The user wants JSON facts about Tokyo. " * 21)[:812]


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
def bare_entry(mocker) -> str:
    """One catalog LLM entry carrying NEITHER request knob, and its repo id.

    BOTH seeds set ``max_tokens`` (ADR-009 §10), so the smoke test's own 256
    default — the cost bound of one command — has nothing to run against
    otherwise.
    """

    mocker.patch.object(
        app_config.modal,
        "llm_models",
        [*app_config.modal.llm_models, ModalLLMModelConfig(repo_id=_BARE)],
    )
    return _BARE


@pytest.fixture
def client_calls(mocker) -> list[dict[str, Any]]:
    """Every ``chat.completions.create`` kwargs dict a real ``ModalLLM`` sent.

    The parity assertion below compares two EMITTED requests, so the client
    here is the real one — only its warm body (URL lookup, health poll, served
    id) and the SDK object are replaced. Re-stated locally rather than shared
    with ``test_modal_llm.py``: one duplicated double is cheaper than a
    conftest that hides which module a test is about.
    """

    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=_CITY_FACTS), finish_reason="stop"
                )
            ],
            usage=None,
        )

    mocker.patch.object(
        modal_llm,
        "AsyncOpenAI",
        lambda **_: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    mocker.patch.object(
        modal_llm,
        "resolve_server_url",
        new_callable=mocker.AsyncMock,
        return_value=_URL,
    )
    mocker.patch.object(
        modal_llm, "poll_health", new_callable=mocker.AsyncMock, return_value=1.5
    )
    mocker.patch.object(
        modal_llm,
        "served_model_id",
        new_callable=mocker.AsyncMock,
        side_effect=lambda url, bearer, default: default,
    )
    return calls


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
        assert f"make memory-deploy-model MODEL={_QWEN}" in message
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

        assert "make memory-deploy-model" in str(excinfo.value)


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


@pytest.fixture
def poll(mocker):
    """Patch the SHARED poller (ADR-009 §11) on the smoke test's binding.

    With it patched, ``_smoke_responses``' first canned 200 is not consumed —
    hence ``responses[1:]`` in these tests.
    """

    return mocker.patch(
        "tree.models.modal_server.poll_health",
        new_callable=mocker.AsyncMock,
        return_value=113.0,
    )


@pytest.mark.usefixtures("proxy_token", "modal_server")
class TestSmokeTestWarmUp:
    """The smoke test waits out the cold start on the SAME poller the client
    uses — before #144 it failed in ~1 s on the 503 a scaled-to-zero server
    answers, right after the deploy that made it worth testing."""

    async def test_the_poll_gets_the_health_url_the_bearer_and_the_budget(
        self, http, poll, caplog
    ) -> None:
        """Story 1: no new CLI flag — the ONE knob is the budget, and the
        operator raises it for one command with TREE_MODAL__WARMUP_DEADLINE_S."""

        http.responses = _smoke_responses(_voyage_vectors())[1:]

        with caplog.at_level(logging.INFO):
            report = await smoke_test(_VOYAGE)

        poll.assert_awaited_once_with(
            f"{_URL}/health",
            {"Authorization": f"Bearer {_BEARER}"},
            deadline_s=app_config.modal.warmup_deadline_s,
        )
        # The poll's own measurement is what the report and the log carry.
        assert report.cold_start_seconds == 113.0
        assert "health 200 after 113.0s" in [r.getMessage() for r in caplog.records]

    async def test_an_explicit_deadline_wins(self, http, poll) -> None:
        http.responses = _smoke_responses(_voyage_vectors())[1:]

        await smoke_test(_VOYAGE, deadline_s=1200.0)

        assert poll.await_args.kwargs["deadline_s"] == 1200.0

    async def test_a_wrong_proxy_token_fails_fast_and_embeds_nothing(
        self, http, poll
    ) -> None:
        """Story 2: ONE poll, then exit — not 600 s of waiting on a 401."""

        poll.side_effect = ModelError(
            f"Health poll of {_URL}/health returned HTTP 401 — not a cold "
            "start, giving up after 1 attempt. Check MODAL_PROXY_TOKEN_ID and "
            "MODAL_PROXY_TOKEN_SECRET in .env."
        )
        http.responses = _smoke_responses(_voyage_vectors())[1:]

        with pytest.raises(ModelError) as excinfo:
            await smoke_test(_VOYAGE)

        # Exactly ModelError: the driver's ExtractionError branch is the one
        # that prints the HF_TOKEN hint, and a 401 here is a Proxy token.
        assert type(excinfo.value) is ModelError
        assert "giving up after 1 attempt" in str(excinfo.value)
        assert http.calls == []

    async def test_a_spent_budget_is_retryable(self, http, poll) -> None:
        """Story 5: a server that never comes up — a transient failure."""

        poll.side_effect = ExtractionError(
            f"Health poll of {_URL}/health gave up after 600s (deadline 600s); "
            "last result: HTTP 503",
            status_code=503,
        )
        http.responses = _smoke_responses(_voyage_vectors())[1:]

        with pytest.raises(ExtractionError) as excinfo:
            await smoke_test(_VOYAGE)

        assert excinfo.value.status_code == 503
        assert "gave up after 600s" in str(excinfo.value)
        assert http.calls == []


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


# --- the LLM half ------------------------------------------------------------

# Modal's own `city_facts` answer, as a server that honoured the strict schema
# returns it: the CONTENT of `choices[0].message.content` is a JSON string.
_CITY_FACTS = '{"city": "Tokyo", "population": 13960000}'

# The seven INFO lines one passing chat smoke test logs, in this order — the
# knobs BEFORE the POST they are part of, so an operator reading a failure
# sees what was asked for above what came back.
_CHAT_LOG_LINES = [
    "health 200 after 113.0s",
    "served model id: modal-recipe/lfm2-5-350m",
    "chat knobs: max_tokens=4096 chat_template_kwargs={}",
    f"chat completion: {_CITY_FACTS}",
    "strict JSON schema honoured: city=Tokyo population=13960000",
    "unauthenticated health -> 401",
    "Smoke test passed",
]


def _completion(
    content: str | None,
    *,
    finish_reason: str = "stop",
    reasoning_content: Any = None,
) -> dict[str, Any]:
    """One OpenAI-compatible chat completion carrying ``content``.

    ``reasoning_content`` is what a server started with a ``--reasoning-parser``
    puts the thinking in (SGLang ``serving_chat.py:700`` at v0.5.18) — present
    only when asked for, because a non-thinking server sends no such key. It is
    typed ``Any``: no schema pins it, so a proxy may put any JSON value there.
    """

    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }


def _chat_responses(
    completion: _FakeResponse | Exception | None = None,
    *,
    served_id: str = "modal-recipe/lfm2-5-350m",
    unauthenticated_status: int = 401,
) -> list[_FakeResponse | Exception]:
    """The three calls one chat smoke test makes AFTER the patched poll."""

    return [
        _FakeResponse(200, {"data": [{"id": served_id}]}),  # GET /v1/models
        (
            _FakeResponse(200, _completion(_CITY_FACTS))
            if completion is None
            else completion
        ),
        _FakeResponse(unauthenticated_status),  # header-less GET /health
    ]


@pytest.mark.usefixtures("proxy_token", "modal_server")
class TestChatSmokeTest:
    """ADR-009 §10: the LLM twin of the embedding smoke test — path-blind, and
    asserting the SHAPE of the answer, never its content."""

    async def test_the_happy_path_logs_seven_lines_in_order(
        self, http, poll, caplog
    ) -> None:
        """Story 2: what the operator reads after
        ``make memory-deploy-model-test MODEL=LiquidAI/LFM2.5-350M``."""

        http.responses = _chat_responses()

        with caplog.at_level(logging.INFO):
            report = await chat_smoke_test(_LFM)

        assert report.url == _URL
        assert report.served_model == "modal-recipe/lfm2-5-350m"
        assert report.cold_start_seconds == 113.0
        assert report.city == "Tokyo"
        assert report.population == 13960000
        assert report.unauthenticated_status == 401
        assert [
            record.getMessage()
            for record in caplog.records
            if record.getMessage() in _CHAT_LOG_LINES
        ] == _CHAT_LOG_LINES

    async def test_it_waits_the_cold_start_out_on_the_shared_poller(
        self, http, poll
    ) -> None:
        """ADR-009 §11: the SAME poller the clients use, with the SAME budget
        — a smoke test right after a deploy must not fail on the 503 a
        scaled-to-zero server answers in a second."""

        http.responses = _chat_responses()

        await chat_smoke_test(_LFM)

        poll.assert_awaited_once_with(
            f"{_URL}/health",
            {"Authorization": f"Bearer {_BEARER}"},
            deadline_s=app_config.modal.warmup_deadline_s,
        )

    async def test_an_explicit_deadline_wins(self, http, poll) -> None:
        http.responses = _chat_responses()

        await chat_smoke_test(_LFM, deadline_s=1200.0)

        assert poll.await_args.kwargs["deadline_s"] == 1200.0

    async def test_the_chat_post_is_bounded(self, http, poll) -> None:
        """Story 4 (ADR-009 §11): the smoke test's POST carried no timeout at
        all — it inherited ``aiohttp``'s 300 s default by accident. The bound
        is now the SAME knob both clients use, so the command that proves a
        model cannot outlast the memory that will call it."""

        http.responses = _chat_responses()

        await chat_smoke_test(_LFM)

        post = next(call for call in http.calls if call["method"] == "POST")
        assert post["timeout"].total == 300.0
        assert post["timeout"].total == app_config.modal.request_timeout_s

    async def test_a_hung_server_times_the_smoke_test_out_loudly(
        self, http, poll
    ) -> None:
        """``str(TimeoutError())`` is EMPTY, so the generic wrapper alone would
        print ``POST …/v1/chat/completions failed: `` and leave the operator
        guessing. Exit 1 with the reason and the knob instead."""

        http.responses = _chat_responses(TimeoutError())

        with pytest.raises(ExtractionError) as excinfo:
            await chat_smoke_test(_LFM)

        assert str(excinfo.value) == (
            f"POST {_URL}/v1/chat/completions timed out after 300s "
            "(modal.request_timeout_s)"
        )
        assert excinfo.value.status_code is None

    async def test_it_posts_the_strict_city_facts_schema(self, http, poll) -> None:
        """The request is the script's warm-up payload with the ENTRY's token
        budget: a reasoning model may spend tokens before the JSON, and a
        completion cut short would fail as "not valid JSON"."""

        http.responses = _chat_responses()

        await chat_smoke_test(_LFM)

        post = next(call for call in http.calls if call["method"] == "POST")
        assert post["url"] == f"{_URL}/v1/chat/completions"
        body = post["json"]
        assert body["model"] == "modal-recipe/lfm2-5-350m"
        assert body["messages"] == [
            {"role": "user", "content": "Reply with JSON facts about Tokyo."}
        ]
        assert body["max_tokens"] == 4096
        assert body["temperature"] == 0
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "city_facts",
                "schema": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                        "population": {"type": "integer"},
                    },
                    "required": ["city", "population"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    @pytest.mark.parametrize(
        "model,expected",
        [
            (
                _QWEN_LLM,
                {
                    "max_tokens": 4096,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            ),
            (_BARE, {"max_tokens": 256}),
        ],
        ids=["the-seeded-thinking-model", "an-entry-with-no-knobs"],
    )
    async def test_the_smoke_test_sends_the_clients_knobs(
        self, http, poll, bare_entry, model: str, expected: dict[str, Any]
    ) -> None:
        """Story 1 / story 3 (ADR-009 §10): the POST carries the ENTRY's
        request knobs — the same ones ``ModalLLM`` sends. An entry that sets no
        budget keeps 256 as the cost bound of one smoke test, and sends no
        ``chat_template_kwargs`` key at all."""

        http.responses = _chat_responses()

        await chat_smoke_test(model)

        body = next(call for call in http.calls if call["method"] == "POST")["json"]
        assert {key: body[key] for key in expected} == expected
        assert ("chat_template_kwargs" in body) == ("chat_template_kwargs" in expected)

    @pytest.mark.parametrize(
        "model,expected",
        [
            (
                _QWEN_LLM,
                'chat knobs: max_tokens=4096 chat_template_kwargs={"enable_thinking": false}',
            ),
            (_BARE, "chat knobs: max_tokens=256 chat_template_kwargs={}"),
        ],
        ids=["the-seeded-thinking-model", "an-entry-with-no-knobs"],
    )
    async def test_the_knobs_are_logged_before_the_post(
        self, http, poll, caplog, bare_entry, model: str, expected: str
    ) -> None:
        """Story 2: the operator changes a knob and re-runs ``-test`` with no
        redeploy, so the command must say which request it actually sent.
        Configuration, never a secret — the **Proxy token** is not in it."""

        http.responses = _chat_responses()

        with caplog.at_level(logging.INFO):
            await chat_smoke_test(model)

        assert expected in [record.getMessage() for record in caplog.records]

    @pytest.mark.parametrize("model", [_QWEN_LLM, _LFM], ids=["qwen3-5", "lfm2-5"])
    async def test_wire_parity_between_client_and_smoke_test(
        self, http, poll, client_calls: list[dict[str, Any]], model: str
    ) -> None:
        """Story 4: ONE helper feeds both paths, asserted on what each one
        EMITTED — the POSTed body here, the ``create`` kwargs of a real
        ``ModalLLM`` call — so a knob added later cannot reach one and miss the
        other. The client's ``extra_body`` is compared as the top-level fields
        the SDK merges it into (``openai`` 2.28.0, ``_base_client.py:500``)."""

        http.responses = _chat_responses()
        await chat_smoke_test(model)
        smoke_body = next(c for c in http.calls if c["method"] == "POST")["json"]

        await ModalLLM(proxy_token=_BEARER, model=model).generate_json("hi")

        call = client_calls[0]
        client_fields = {
            key: value for key, value in call.items() if key == "max_tokens"
        } | call.get("extra_body", {})
        knobs = chat_request_knobs(get_llm_entry(model))
        assert client_fields == {key: smoke_body[key] for key in knobs}
        assert client_fields == knobs

    async def test_an_empty_answer_names_the_reasoning_budget(
        self, http, poll, caplog
    ) -> None:
        """Story 2: the live failure of ``tasks/141`` round 1 — a 200 whose
        ``content`` is null because the budget went into thinking. The message
        IS the fix, and carries the reasoning's LENGTH only: the trace itself
        is the user's content and reaches neither a message nor a log line."""

        http.responses = _chat_responses(
            _FakeResponse(
                200,
                _completion(None, finish_reason="length", reasoning_content=_REASONING),
            )
        )

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ModelError) as excinfo:
                await chat_smoke_test(_QWEN_LLM)

        assert str(excinfo.value) == (
            "the chat completion carried no content (finish_reason=length, "
            "reasoning_content: 812 chars) — a thinking model spent its budget "
            "reasoning: set chat_template_kwargs: {enable_thinking: false} on "
            "the entry, or raise its max_tokens"
        )
        assert _REASONING[:40] not in "\n".join(
            record.getMessage() for record in caplog.records
        )

    async def test_an_empty_answer_without_reasoning_names_the_finish_reason(
        self, http, poll
    ) -> None:
        """A server with no ``--reasoning-parser`` sends no
        ``reasoning_content``: the old sentence stands, plus the one word that
        tells a truncation from a model with nothing to say."""

        http.responses = _chat_responses(
            _FakeResponse(200, _completion(None, finish_reason="stop"))
        )

        with pytest.raises(ModelError) as excinfo:
            await chat_smoke_test(_LFM)

        assert str(excinfo.value) == (
            "the chat completion carried no content (finish_reason=stop) — the "
            "served model answered with an empty message"
        )

    @pytest.mark.parametrize(
        "reasoning",
        [5, 1.5, True, ["a", "b"], {"text": "a"}],
        ids=["int", "float", "bool", "list", "dict"],
    )
    async def test_a_non_string_reasoning_content_still_raises_the_plain_error(
        self, http, poll, reasoning: Any
    ) -> None:
        """Nothing types ``reasoning_content``, so a proxy may answer a JSON
        number or object. Measuring it would raise ``TypeError: object of type
        'int' has no len()`` from inside the diagnosis — the smoke test would
        crash instead of REPORTING that the completion was empty. Unknown
        length, unknown thinking: the plain sentence plus what is known."""

        http.responses = _chat_responses(
            _FakeResponse(
                200,
                _completion(None, finish_reason="length", reasoning_content=reasoning),
            )
        )

        with pytest.raises(ModelError) as excinfo:
            await chat_smoke_test(_QWEN_LLM)

        assert str(excinfo.value) == (
            "the chat completion carried no content (finish_reason=length) — "
            "the served model answered with an empty message"
        )

    async def test_a_non_json_completion_fails_with_an_excerpt(
        self, http, poll
    ) -> None:
        """Story 4: the model ignored ``response_format``. The message carries
        the first characters, which is what tells a `<think>` block apart from
        a chatty preamble."""

        http.responses = _chat_responses(
            _FakeResponse(200, _completion("<think>Tokyo is in Japan</think> Sure!"))
        )

        with pytest.raises(ModelError) as excinfo:
            await chat_smoke_test(_LFM)

        assert "chat completion is not valid JSON" in str(excinfo.value)
        assert "<think>Tokyo is in Japan" in str(excinfo.value)

    async def test_a_long_bad_completion_is_truncated(self, http, poll) -> None:
        """A 4000-character apology must not become a 4000-character log
        line."""

        http.responses = _chat_responses(
            _FakeResponse(200, _completion("Sure! " + "very long " * 400))
        )

        with pytest.raises(ModelError) as excinfo:
            await chat_smoke_test(_LFM)

        assert len(str(excinfo.value)) < 300

    async def test_json_that_is_not_an_object_fails(self, http, poll) -> None:
        http.responses = _chat_responses(
            _FakeResponse(200, _completion('["Tokyo", 13960000]'))
        )

        with pytest.raises(ModelError) as excinfo:
            await chat_smoke_test(_LFM)

        assert "is not a JSON object" in str(excinfo.value)

    async def test_a_missing_key_is_named(self, http, poll) -> None:
        http.responses = _chat_responses(
            _FakeResponse(200, _completion('{"city": "Tokyo"}'))
        )

        with pytest.raises(ModelError) as excinfo:
            await chat_smoke_test(_LFM)

        assert "missing ['population']" in str(excinfo.value)

    async def test_an_extra_key_is_named(self, http, poll) -> None:
        """``additionalProperties: false`` is part of the schema, so a server
        that added a key did not honour it."""

        http.responses = _chat_responses(
            _FakeResponse(
                200,
                _completion(
                    '{"city": "Tokyo", "population": 13960000, "country": "Japan"}'
                ),
            )
        )

        with pytest.raises(ModelError) as excinfo:
            await chat_smoke_test(_LFM)

        assert "unexpected ['country']" in str(excinfo.value)

    @pytest.mark.parametrize(
        "content,expected",
        [
            ('{"city": "Tokyo", "population": "many"}', "types 'population' as str"),
            ('{"city": "Tokyo", "population": true}', "types 'population' as bool"),
            ('{"city": 13960000, "population": 13960000}', "types 'city' as int"),
        ],
        ids=["population-string", "population-bool", "city-int"],
    )
    async def test_a_wrongly_typed_value_fails(
        self, http, poll, content: str, expected: str
    ) -> None:
        """A strict schema types both keys. ``true`` is worth its own row:
        ``bool`` IS an ``int`` in Python, so a naive check would accept it."""

        http.responses = _chat_responses(_FakeResponse(200, _completion(content)))

        with pytest.raises(ModelError) as excinfo:
            await chat_smoke_test(_LFM)

        assert expected in str(excinfo.value)

    async def test_an_empty_completion_fails(self, http, poll) -> None:
        http.responses = _chat_responses(_FakeResponse(200, {"choices": []}))

        with pytest.raises(ModelError) as excinfo:
            await chat_smoke_test(_LFM)

        assert "carried no content" in str(excinfo.value)

    async def test_a_400_is_retryable_and_carries_the_status(self, http, poll) -> None:
        """A server that cannot compile the schema answers 400 — server-side,
        so it is the class the driver may print the HF_TOKEN hint under."""

        http.responses = _chat_responses(_FakeResponse(400))

        with pytest.raises(ExtractionError) as excinfo:
            await chat_smoke_test(_LFM)

        assert excinfo.value.status_code == 400
        assert "answered 400, expected 200" in str(excinfo.value)

    async def test_a_public_server_is_caught(self, http, poll) -> None:
        """Auth is the thing a deploy can silently get wrong."""

        http.responses = _chat_responses(unauthenticated_status=200)

        with pytest.raises(ModelError) as excinfo:
            await chat_smoke_test(_LFM)

        assert "the server is public" in str(excinfo.value)

    async def test_the_unauthenticated_probe_sends_no_authorization_header(
        self, http, poll
    ) -> None:
        http.responses = _chat_responses()

        await chat_smoke_test(_LFM)

        assert "Authorization" not in http.session_kwargs[-1].get("headers", {})

    async def test_the_proxy_token_reaches_no_log_record(
        self, http, poll, caplog
    ) -> None:
        """ADR-009 §9: the token authenticates every call here and appears in
        none of them."""

        http.responses = _chat_responses()

        with caplog.at_level(logging.DEBUG):
            await chat_smoke_test(_LFM)

        assert _BEARER not in "\n".join(r.getMessage() for r in caplog.records)
        assert "ws-2" not in "\n".join(r.getMessage() for r in caplog.records)

    async def test_an_embedding_entry_is_refused_before_any_request(
        self, http, poll
    ) -> None:
        """Story 5's client-side twin: the chat test cannot run against an
        embedding model, and says so before resolving anything."""

        with pytest.raises(ModelError) as excinfo:
            await chat_smoke_test(_VOYAGE)

        assert "is an embedding entry" in str(excinfo.value)
        assert http.calls == []

    async def test_a_wrong_proxy_token_fails_fast(self, http, poll) -> None:
        """ONE poll, then exit — and exactly ``ModelError``, because the
        driver's ``ExtractionError`` branch is the one that prints the
        HF_TOKEN hint."""

        poll.side_effect = ModelError(
            f"Health poll of {_URL}/health returned HTTP 401 — not a cold "
            "start, giving up after 1 attempt."
        )
        http.responses = _chat_responses()

        with pytest.raises(ModelError) as excinfo:
            await chat_smoke_test(_LFM)

        assert type(excinfo.value) is ModelError
        assert http.calls == []
