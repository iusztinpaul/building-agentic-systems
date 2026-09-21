"""Unit tests for ``tree.models.modal_llm`` — the **Modal catalog** LLM client
(ADR-009 §3/§4/§10/§11).

Nothing here touches Modal or the network: the three
:mod:`tree.models.modal_server` helpers and ``AsyncOpenAI`` are replaced by
fakes, and the **Proxy token** is always the FAKE pair ``wk-1`` / ``ws-2`` —
``make`` exports the developer's real ``.env`` into the test process, so a test
that read the real token would both leak it and pass for the wrong reason.

What is pinned is the contract the extraction pipeline already depends on for
Gemini: a dict back, or one of four ``ExtractionError``s whose message says
which failure it was — plus the two things only the Modal path can get wrong,
the **Warm gate** composition (a re-entrant warm would DEADLOCK) and JSON mode
on the wire.

Two layers of doubles, on purpose (the lesson of #140). Most tests replace
``client.chat.completions`` with a recorder, which is enough to read back the
call kwargs. The wire contract, the empty-prompt guard and the initialisation
race are tested against the REAL ``AsyncOpenAI`` over an in-process
``httpx.MockTransport`` (``wire``) instead: the request body and the response
parsing are decided INSIDE the SDK, so a recorder standing in for it cannot
see either.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import openai as openai_sdk
import pytest
from openai import AsyncOpenAI

from tree.config.app_config import app_config
from tree.models import modal_llm
from tree.models.base import BaseLLM
from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_catalog import get_catalog_entry
from tree.models.modal_llm import ModalLLM

_LFM = "LiquidAI/LFM2.5-350M"
_QWEN_LLM = "Qwen/Qwen3.5-0.8B"
_VOYAGE = "voyageai/voyage-4-nano"
_APP = "ep-tree-lfm2-5-350m"
_URL = "https://acme--ep-tree-lfm2-5-350m-server.modal.run"
_BEARER = "wk-1.ws-2"


# --- doubles ----------------------------------------------------------------


def _response(content: str | None = '{"a": 1}', usage: Any = ...) -> Any:
    """An OpenAI-shaped chat completion carrying ``content``."""

    if usage is ...:
        usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=usage,
    )


def _cold(status: int = 503) -> openai_sdk.InternalServerError:
    """What a scaled-to-zero container looks like AT CALL TIME: the SDK maps
    every status >= 500 to ``InternalServerError``."""

    request = httpx.Request("POST", f"{_URL}/v1/chat/completions")
    return openai_sdk.InternalServerError(
        "boom", response=httpx.Response(status, request=request), body=None
    )


def _bad_request() -> openai_sdk.BadRequestError:
    """A 400 — never a cold start, so it must never trigger a re-warm."""

    request = httpx.Request("POST", f"{_URL}/v1/chat/completions")
    return openai_sdk.BadRequestError(
        "nope", response=httpx.Response(400, request=request), body=None
    )


def _rate_limited() -> openai_sdk.RateLimitError:
    """A 429 — retryable by the caller, but never by WAITING for a boot."""

    request = httpx.Request("POST", f"{_URL}/v1/chat/completions")
    return openai_sdk.RateLimitError(
        "slow down", response=httpx.Response(429, request=request), body=None
    )


class _Completions:
    """``client.chat.completions`` — records every call, answers the recorder.

    ``answers`` is a queue for the re-warm tests (cold first, content after);
    ``answer`` is the single standing answer every other test sets.
    """

    def __init__(self, recorder: _OpenAI) -> None:
        self._recorder = recorder

    async def create(self, **kwargs: Any) -> Any:
        self._recorder.calls.append(kwargs)
        answer = (
            self._recorder.answers.pop(0)
            if self._recorder.answers
            else self._recorder.answer
        )
        if isinstance(answer, Exception):
            raise answer
        return answer


class _OpenAI:
    """Stands in for ``AsyncOpenAI``: records how it was built and called."""

    def __init__(self) -> None:
        self.built: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.answers: list[Any] = []
        self.answer: Any = _response()

    def build(self, **kwargs: Any) -> SimpleNamespace:
        self.built.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions(self)))


@pytest.fixture
def openai(mocker) -> _OpenAI:
    """Patch ``AsyncOpenAI`` in the module under test."""

    recorder = _OpenAI()
    mocker.patch.object(modal_llm, "AsyncOpenAI", recorder.build)
    return recorder


@pytest.fixture
def server(mocker) -> SimpleNamespace:
    """Patch the three coroutines the warm body awaits.

    Patched on the CLIENT's bindings, so the real modules (and the ``modal``
    SDK ``modal_server`` imports) are never exercised.
    """

    return SimpleNamespace(
        resolve=mocker.patch.object(
            modal_llm,
            "resolve_server_url",
            new_callable=mocker.AsyncMock,
            return_value=_URL,
        ),
        poll=mocker.patch.object(
            modal_llm,
            "poll_health",
            new_callable=mocker.AsyncMock,
            return_value=1.5,
        ),
        served=mocker.patch.object(
            modal_llm,
            "served_model_id",
            new_callable=mocker.AsyncMock,
            return_value=_LFM,
        ),
    )


class _WireStub:
    """An OpenAI-compatible chat server the REAL ``AsyncOpenAI`` talks to.

    ``_OpenAI`` above replaces ``client.chat.completions`` and therefore skips
    every decision the SDK makes for us — the JSON body, the response parser,
    what an omitted argument does. The wire tests keep the SDK and replace only
    its transport.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.content: str = '{"a": 1}'
        #: While this answers True every POST is a 503 — a server that scaled
        #: to zero between the warm and the call. A PERIOD, not a request
        #: count: the real server stays cold until a poll boots it. With
        #: ``max_retries=0`` the SDK turns each 503 into the
        #: ``InternalServerError`` the gate classifies as cold.
        self.cold_while: Any = lambda: False
        #: An ``httpx`` transport failure raised INSTEAD of answering — a
        #: server that accepted the request and never finished it. Recorded
        #: first, so a test can still count the requests that were sent.
        self.raises: Exception | None = None
        #: Every REAL ``AsyncOpenAI`` the client under test built, so a test
        #: can read the timeout and the retry budget off the SDK object rather
        #: than off the kwargs we happened to pass it.
        self.clients: list[AsyncOpenAI] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        if self.cold_while():
            return httpx.Response(503, request=request, json={"error": "cold"})
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "served/stub",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 5,
                    "total_tokens": 8,
                },
            },
        )

    @property
    def bodies(self) -> list[dict[str, Any]]:
        """The JSON body of every request, as the server would parse it."""

        return [json.loads(request.content) for request in self.requests]


@pytest.fixture
async def wire(mocker) -> AsyncIterator[_WireStub]:
    """Build REAL ``AsyncOpenAI`` clients against an in-process transport."""

    stub = _WireStub()
    clients = stub.clients

    def build(**kwargs: Any) -> AsyncOpenAI:
        # ONLY the transport is replaced: ``timeout`` and ``max_retries`` come
        # from the client under test, so a production default that retried a
        # 503 twice would inflate the request counts these tests assert on
        # instead of passing silently (#155).
        client = AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(stub.handle)),
        )
        clients.append(client)
        return client

    mocker.patch.object(modal_llm, "AsyncOpenAI", build)
    yield stub
    for client in clients:
        await client.close()


class _SuspendingServer:
    """The three coroutines of the warm body — counting, and yielding.

    An ``AsyncMock`` completes without ever suspending, so a ``gather`` over it
    runs each coroutine to completion in turn and a check-then-act race cannot
    even appear. The real ones hold a network round trip (the poll holds up to
    600 s of them); these hold the smallest thing that reschedules,
    ``asyncio.sleep(0)``.
    """

    def __init__(self) -> None:
        self.resolve_calls = 0
        self.poll_calls = 0
        self.models_calls = 0
        self.poll_error: Exception | None = None

    async def resolve(self, entry: Any) -> str:
        self.resolve_calls += 1
        await asyncio.sleep(0)
        return _URL

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


@pytest.fixture
def suspending_server(mocker) -> _SuspendingServer:
    """Patch the warm body's three coroutines with suspending doubles."""

    helpers = _SuspendingServer()
    mocker.patch.object(modal_llm, "resolve_server_url", helpers.resolve)
    mocker.patch.object(modal_llm, "poll_health", helpers.poll)
    mocker.patch.object(modal_llm, "served_model_id", helpers.served)
    return helpers


def _model(model: str = _LFM, **kwargs: Any) -> ModalLLM:
    """A client on the FAKE proxy token."""

    return ModalLLM(proxy_token=_BEARER, model=model, **kwargs)


# --- tests ------------------------------------------------------------------


class TestInit:
    """Story 3: everything that can be known offline fails offline — a typo
    must not wake a GPU."""

    def test_an_empty_proxy_token_names_both_env_vars(self) -> None:
        with pytest.raises(ModelError) as excinfo:
            ModalLLM(proxy_token="", model=_LFM)

        message = str(excinfo.value)
        assert "Modal proxy token is required" in message
        assert "MODAL_PROXY_TOKEN_ID" in message
        assert "MODAL_PROXY_TOKEN_SECRET" in message

    def test_an_unknown_model_lists_both_catalog_groups(self) -> None:
        with pytest.raises(ModelError) as excinfo:
            _model("nope/x")

        message = str(excinfo.value)
        assert "Unknown Modal model 'nope/x'" in message
        assert _LFM in message
        assert _VOYAGE in message

    def test_a_typo_in_the_case_of_a_known_id_is_still_unknown(self) -> None:
        """Story 3's exact case: ``…-350m`` instead of ``…-350M``. Matching is
        exact, never fuzzy — a near-miss must not query the wrong weights."""

        with pytest.raises(ModelError, match="Unknown Modal model"):
            _model("LiquidAI/LFM2.5-350m")

    def test_an_embedding_id_is_refused_by_kind(self) -> None:
        with pytest.raises(ModelError) as excinfo:
            _model(_VOYAGE)

        assert str(excinfo.value) == (
            f"{_VOYAGE} is an embedding entry (modal.embedding_models), not an LLM."
        )

    async def test_construction_makes_no_call(self, server, openai) -> None:
        """The first network call happens in ``generate_json()``, not here."""

        _model()

        server.resolve.assert_not_awaited()
        server.poll.assert_not_awaited()
        assert openai.built == []

    def test_construction_never_looks_a_modal_server_up(self, mocker) -> None:
        """Stronger than "no await": an unknown id must fail BEFORE the
        ``modal.Server.from_name`` lookup is even reached."""

        server_cls = mocker.patch("tree.models.modal_server.modal.Server")

        with pytest.raises(ModelError):
            _model("nope/x")
        _model()

        server_cls.from_name.assert_not_called()


class TestFirstUse:
    """ADR-009 §11: one all-or-nothing single-flight warm, whatever the
    concurrency."""

    async def test_the_first_call_resolves_the_entry_and_builds_one_v1_url(
        self, server, openai
    ) -> None:
        model = _model()

        await model.generate_json("hi")

        server.resolve.assert_awaited_once_with(get_catalog_entry(_LFM))
        base_url = openai.built[0]["base_url"]
        assert base_url == f"{_URL}/v1"
        assert base_url.count("/v1") == 1
        assert openai.built[0]["api_key"] == _BEARER

    async def test_a_second_call_warms_nothing_again(self, server, openai) -> None:
        model = _model()

        await model.generate_json("hi")
        await model.generate_json("again")

        assert server.resolve.await_count == 1
        assert server.poll.await_count == 1
        assert server.served.await_count == 1
        assert len(openai.built) == 1
        assert len(openai.calls) == 2

    async def test_the_lookup_asks_for_the_app_and_the_server_class(
        self, mocker, openai
    ) -> None:
        """H1 (ADR-009 §3): ``("ep-tree-lfm2-5-350m", "Server")`` resolves a
        Dedicated endpoint and our SGLang App alike — the client never learns
        which one answered. The REAL ``resolve_server_url`` runs here; only
        ``modal.Server`` is replaced."""

        modal_server = mocker.MagicMock()
        modal_server.get_url.aio = mocker.AsyncMock(return_value=_URL)
        server_cls = mocker.patch("tree.models.modal_server.modal.Server")
        server_cls.from_name.return_value = modal_server
        mocker.patch.object(
            modal_llm, "poll_health", new_callable=mocker.AsyncMock, return_value=1.0
        )
        mocker.patch.object(
            modal_llm,
            "served_model_id",
            new_callable=mocker.AsyncMock,
            return_value=_LFM,
        )
        model = _model()

        await model.generate_json("hi")

        server_cls.from_name.assert_called_once_with(_APP, "Server")

    @pytest.mark.parametrize("repo_id", [_LFM, _QWEN_LLM])
    async def test_the_serving_path_is_invisible_to_the_client(
        self, server, openai, repo_id: str
    ) -> None:
        """Story 2: an endpoint-routed model and an App-served one differ in
        the catalog entry alone — same lookup, same token, same code."""

        server.served.return_value = repo_id
        model = _model(repo_id)

        await model.generate_json("hi")

        server.resolve.assert_awaited_once_with(get_catalog_entry(repo_id))
        assert openai.calls[0]["model"] == repo_id

    async def test_the_health_poll_gets_the_bearer_header_and_the_deadline(
        self, server, openai
    ) -> None:
        """The poll sends the SAME ``Authorization: Bearer`` header the chat
        call does (ADR-009 §4) — Modal's edge checks it before a GPU wakes."""

        model = _model(warmup_deadline_s=42.0)

        await model.generate_json("hi")

        server.poll.assert_awaited_once_with(
            f"{_URL}/health",
            {"Authorization": f"Bearer {_BEARER}"},
            deadline_s=42.0,
        )

    async def test_the_deadline_defaults_to_the_configured_budget(
        self, server, openai
    ) -> None:
        """ONE knob (ADR-009 §11), read at CONSTRUCTION."""

        model = _model()

        await model.generate_json("hi")

        assert (
            server.poll.await_args.kwargs["deadline_s"]
            == app_config.modal.warmup_deadline_s
        )

    async def test_a_failing_poll_leaves_no_client_and_the_next_call_retries(
        self, server, openai
    ) -> None:
        """ALL-OR-NOTHING: a spent deadline must leave NOTHING half-built, so
        the next call re-runs resolve -> poll -> /v1/models before it prompts."""

        server.poll.side_effect = ExtractionError("gave up after 600s")
        model = _model()

        with pytest.raises(ExtractionError, match="gave up after 600s"):
            await model.generate_json("hi")
        assert model._client is None
        assert openai.calls == []

        server.poll.side_effect = None
        assert await model.generate_json("hi") == {"a": 1}
        assert server.resolve.await_count == 2
        assert server.poll.await_count == 2
        # 1, not 2: the failed attempt never got past the poll.
        assert server.served.await_count == 1

    async def test_a_model_error_from_the_warm_propagates_unchanged(
        self, server, openai
    ) -> None:
        """A wrong token or a stopped model is a CONFIGURATION error: no retry
        fixes it, so it must not be re-typed as a retryable failure."""

        failure = (
            "Failed to resolve Modal server ep-tree-lfm2-5-350m/Server. Is the "
            f"model deployed (...)? Run: make memory-deploy-model MODEL={_LFM}"
        )
        server.resolve.side_effect = ModelError(failure)
        model = _model()

        with pytest.raises(ModelError) as excinfo:
            await model.generate_json("hi")

        # `ExtractionError` IS a `ModelError`, so the type must be exact.
        assert type(excinfo.value) is ModelError
        assert str(excinfo.value) == failure
        assert openai.calls == []

    async def test_the_ready_line_carries_the_app_the_served_id_and_the_url(
        self, server, openai, caplog
    ) -> None:
        """Story 1: one line tells the operator which app answered and under
        which model id — and it carries no token."""

        server.served.return_value = "served/other-name"
        model = _model()

        with caplog.at_level(logging.INFO):
            await model.generate_json("hi")

        line = next(
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("ModalLLM ready")
        )
        assert f"app={_APP}" in line
        assert "server=Server" in line
        assert "served_model=served/other-name" in line
        assert line.endswith(f"url={_URL}/v1")
        assert _BEARER not in line

    @pytest.mark.parametrize("callers", [3, 8, 10])
    async def test_concurrent_first_calls_initialise_exactly_once(
        self, suspending_server, wire, callers: int
    ) -> None:
        """Eight (here also three and ten) coroutines prompt one fresh
        instance: one URL lookup, one poll loop, one /v1/models call, one
        client. N poll loops would be N boots of a billed GPU."""

        model = _model()

        results = await asyncio.gather(
            *(model.generate_json(f"chunk {index}") for index in range(callers))
        )

        assert suspending_server.resolve_calls == 1
        assert suspending_server.poll_calls == 1
        assert suspending_server.models_calls == 1
        assert results == [{"a": 1}] * callers
        assert len(wire.requests) == callers

    async def test_every_concurrent_waiter_sees_the_warm_failure(
        self, suspending_server, wire
    ) -> None:
        """No waiter may fall through to the chat call with an unbuilt
        client — and each keeps the unwrapped poll error."""

        suspending_server.poll_error = ExtractionError("gave up after 600s")
        model = _model()

        results = await asyncio.gather(
            model.generate_json("a"),
            model.generate_json("b"),
            model.generate_json("c"),
            return_exceptions=True,
        )

        assert [type(result) for result in results] == [ExtractionError] * 3
        assert all("gave up after 600s" in str(result) for result in results)
        assert wire.requests == []


class TestWarmGateComposition:
    """ADR-009 §11: the gate is COMPOSED — and composing it wrongly deadlocks."""

    async def test_the_warm_body_never_re_enters_its_own_gate(
        self, server, openai, mocker
    ) -> None:
        """``asyncio.Lock`` is NOT re-entrant. A ``_warm`` that went back
        through ``gate.call`` (to "just reuse the retry") would hang every
        first use forever, behind a lock its own caller holds. The warm body
        may therefore touch the RAW helpers only."""

        model = _model()
        mocker.patch.object(
            model._gate,
            "call",
            side_effect=AssertionError("_warm re-entered its own gate"),
        )

        await model.ensure_warm()

        assert model._client is not None
        assert server.poll.await_count == 1

    async def test_model_exposes_ensure_warm_and_warm_key(self, server, openai) -> None:
        """#149's pre-warm is duck-typed on ``ensure_warm`` and warms each
        distinct Modal app once — hence ``warm_key``."""

        model = _model()

        await model.ensure_warm()
        await model.ensure_warm()

        assert model.warm_key == _APP
        assert server.poll.await_count == 1
        assert server.resolve.await_count == 1

    async def test_a_prompt_after_a_pre_warm_costs_no_second_warm(
        self, server, openai
    ) -> None:
        """What #149 buys: the pre-warm pays the boot, document 1 does not."""

        model = _model()
        await model.ensure_warm()

        assert await model.generate_json("hi") == {"a": 1}

        assert server.poll.await_count == 1
        assert len(openai.built) == 1


class TestGenerateJson:
    """The request this client puts on the wire (ADR-009 §10)."""

    async def test_without_a_system_prompt_there_is_exactly_one_user_message(
        self, server, openai
    ) -> None:
        model = _model()

        await model.generate_json("extract this")

        assert openai.calls[0]["messages"] == [
            {"role": "user", "content": "extract this"}
        ]

    async def test_a_system_prompt_leads_the_message_list(self, server, openai) -> None:
        """Gemini's ``system_instruction``, in the shape the chat API has for
        it — and in that ORDER, which is what makes it a system turn."""

        model = _model()

        await model.generate_json("extract this", system="you are terse")

        assert openai.calls[0]["messages"] == [
            {"role": "system", "content": "you are terse"},
            {"role": "user", "content": "extract this"},
        ]

    async def test_an_empty_system_prompt_adds_no_message(self, server, openai) -> None:
        """``system=""`` is "no system prompt", not "an empty system turn"."""

        model = _model()

        await model.generate_json("extract this", system="")

        assert openai.calls[0]["messages"] == [
            {"role": "user", "content": "extract this"}
        ]

    async def test_the_request_uses_the_discovered_served_id(
        self, server, openai
    ) -> None:
        """A managed recipe — not us — names the served model, so the id comes
        from /v1/models."""

        server.served.return_value = "served/other-name"
        model = _model()

        await model.generate_json("hi")

        server.served.assert_awaited_once_with(_URL, _BEARER, default=_LFM)
        assert openai.calls[0]["model"] == "served/other-name"

    async def test_the_temperature_is_zero(self, server, openai) -> None:
        """Extraction is not creative writing: the same chunk must yield the
        same JSON."""

        model = _model()

        await model.generate_json("hi")

        assert openai.calls[0]["temperature"] == 0

    async def test_json_mode_is_the_default_response_format(
        self, server, openai
    ) -> None:
        """``BaseLLM.generate_json`` carries no schema — callers embed theirs in
        the prompt — so the default is JSON MODE, the counterpart of Gemini's
        ``response_mime_type="application/json"``."""

        model = _model()

        await model.generate_json("hi")

        assert openai.calls[0]["response_format"] == {"type": "json_object"}

    async def test_a_schema_upgrades_the_request_to_strict_json_schema(
        self, server, openai
    ) -> None:
        """The extra, Liskov-safe keyword only this class has (#141 uses it)."""

        schema = {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        }
        model = _model()

        await model.generate_json("hi", schema=schema)

        assert openai.calls[0]["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "response", "strict": True, "schema": schema},
        }

    async def test_valid_json_comes_back_as_a_dict(self, server, openai) -> None:
        model = _model()
        openai.answer = _response('{"a": 1}')

        assert await model.generate_json("hi") == {"a": 1}

    async def test_no_max_tokens_is_sent(self, server, openai) -> None:
        """Out of scope by ADR-009 §10's "what would justify upgrading": the
        server's own default wins, and a completion cut short would surface as
        "empty response", not as a silent truncation."""

        model = _model()

        await model.generate_json("hi")

        assert "max_tokens" not in openai.calls[0]


class TestFailures:
    """Gemini's three messages with ``Modal LLM`` in place of ``Gemini``, plus
    the one shape only an OpenAI-compatible server can return."""

    @pytest.mark.parametrize("content", [None, ""], ids=["none", "empty"])
    async def test_an_empty_completion_says_so(
        self, server, openai, content: str | None
    ) -> None:
        """A reasoning model that spent its budget thinking lands here — the
        message IS the diagnosis."""

        model = _model()
        openai.answer = _response(content)

        with pytest.raises(ExtractionError) as excinfo:
            await model.generate_json("hi")

        assert str(excinfo.value) == "Modal LLM returned an empty response"

    async def test_prose_instead_of_json_quotes_the_completion(
        self, server, openai
    ) -> None:
        """Story 4: the same shape the pipeline already handles for Gemini."""

        model = _model()
        openai.answer = _response("<think>hmm")

        with pytest.raises(ExtractionError) as excinfo:
            await model.generate_json("hi")

        assert str(excinfo.value) == "Modal LLM returned invalid JSON: <think>hmm"

    async def test_a_long_bad_completion_is_cut_to_200_characters(
        self, server, openai
    ) -> None:
        model = _model()
        openai.answer = _response("Sure! " + "x" * 500)

        with pytest.raises(ExtractionError) as excinfo:
            await model.generate_json("hi")

        excerpt = str(excinfo.value).removeprefix("Modal LLM returned invalid JSON: ")
        assert len(excerpt) == 200

    @pytest.mark.parametrize(
        "content,type_name",
        [("[1, 2]", "list"), ('"nope"', "str"), ("7", "int")],
    )
    async def test_valid_json_that_is_not_an_object_is_refused(
        self, server, openai, content: str, type_name: str
    ) -> None:
        """Every caller of ``generate_json`` indexes the result as a dict, so a
        list must fail HERE rather than as a ``TypeError`` three frames up."""

        model = _model()
        openai.answer = _response(content)

        with pytest.raises(ExtractionError) as excinfo:
            await model.generate_json("hi")

        assert str(excinfo.value) == (
            f"Modal LLM returned JSON that is not an object: {type_name}"
        )

    async def test_a_400_is_wrapped_with_its_status_and_never_re_warms(
        self, server, openai
    ) -> None:
        """A 400 is the caller's problem, not a cold start: waiting for a boot
        would burn the deadline on a request that can never succeed."""

        model = _model()
        await model.generate_json("warm me")
        openai.answer = _bad_request()

        with pytest.raises(ExtractionError) as excinfo:
            await model.generate_json("hi")

        assert str(excinfo.value).startswith("Modal LLM call failed:")
        assert excinfo.value.status_code == 400
        assert server.poll.await_count == 1

    async def test_a_429_is_wrapped_with_its_status_and_never_re_warms(
        self, server, openai
    ) -> None:
        """A 429 never becomes a 200 by waiting out a boot (ADR-009 §11's
        closed classification)."""

        model = _model()
        await model.generate_json("warm me")
        openai.answer = _rate_limited()

        with pytest.raises(ExtractionError) as excinfo:
            await model.generate_json("hi")

        assert excinfo.value.status_code == 429
        assert server.poll.await_count == 1

    async def test_a_transport_failure_has_no_status_code(self, server, openai) -> None:
        """``status_code`` is the SDK's, never invented: an exception that
        carries no HTTP status must not get one."""

        model = _model()
        openai.answer = RuntimeError("boom")

        with pytest.raises(ExtractionError) as excinfo:
            await model.generate_json("hi")

        assert str(excinfo.value) == "Modal LLM call failed: boom"
        assert excinfo.value.status_code is None

    @pytest.mark.parametrize(
        "prompt", ["", "   ", "\n\t "], ids=["empty", "spaces", "ws"]
    )
    async def test_an_empty_prompt_never_wakes_the_endpoint(
        self, suspending_server, wire, prompt: str
    ) -> None:
        """There is no correct dict for "generate JSON from nothing" — ``{}``
        would make an empty chunk look like a successful extraction of zero
        entities — and waking a scaled-to-zero GPU to ask nothing bills the
        operator for minutes. The guard sits ABOVE the gate, like
        ``ModalEmbeddingModel.embed``'s empty-batch return."""

        model = _model()

        with pytest.raises(ExtractionError) as excinfo:
            await model.generate_json(prompt)

        assert str(excinfo.value) == "Modal LLM was given an empty prompt"
        assert suspending_server.resolve_calls == 0
        assert suspending_server.poll_calls == 0
        assert suspending_server.models_calls == 0
        assert wire.requests == []


class TestRequestTimeout:
    """ADR-009 §11: one call, one bound, one retry — and that retry is the
    gate's (``tasks/141``, cycle 4d: a thinking model hung a call for 13
    minutes under the SDK's 600 s x 3 default)."""

    async def test_the_client_is_bounded_and_never_retries_silently(
        self, server, openai
    ) -> None:
        """``modal.request_timeout_s`` is read at CONSTRUCTION, like the warm-up
        deadline, and ``max_retries=0`` leaves the ONE retry to the gate: the
        SDK's two silent ones multiplied the hang by three and logged nothing
        but ``Retrying request to /chat/completions``."""

        model = _model()
        await model.generate_json("hi")

        assert openai.built[0]["timeout"] == 300.0
        assert openai.built[0]["max_retries"] == 0

    async def test_the_operator_can_widen_the_budget_for_one_process(
        self, server, openai, mocker
    ) -> None:
        """Story 3: a 35B model whose answers take seven minutes runs with
        ``TREE_MODAL__REQUEST_TIMEOUT_S=900`` and nothing else changes."""

        mocker.patch.object(app_config.modal, "request_timeout_s", 45.0)
        model = _model()

        await model.generate_json("hi")

        assert openai.built[0]["timeout"] == 45.0

    async def test_the_real_sdk_client_carries_the_bound_and_no_retries(
        self, server, wire
    ) -> None:
        """Read off the SDK object, not off the kwargs: ``timeout=`` and
        ``max_retries=`` are what the REAL ``AsyncOpenAI`` will enforce."""

        model = _model()

        await model.generate_json("hi")

        assert [client.timeout for client in wire.clients] == [300.0]
        assert wire.clients[0].max_retries == 0

    @pytest.mark.parametrize(
        "failure",
        [httpx.ReadTimeout("timed out"), httpx.ConnectTimeout("timed out")],
        ids=["read-timeout", "connect-timeout"],
    )
    async def test_a_timeout_is_an_extraction_error_naming_the_knob(
        self, suspending_server, wire, caplog, failure: Exception
    ) -> None:
        """Story 1, through the REAL SDK: both httpx timeouts (waiting for the
        answer, and waiting for the connection) become ``APITimeoutError``, the
        request is sent ONCE, nothing re-warms, and the message says which knob
        to turn.

        A connect timeout is in the same class on purpose: Modal's edge answers
        a scaled-to-zero server's ``/health`` with 503 in about a second, so a
        connection that never completes is a network fault, never a cold start
        (ADR-009 §11).

        The transport records the request and THEN raises, so in the connect
        case the count means "the SDK built one request and gave up", not "one
        request reached a server" — which is exactly the property at stake: two
        SDK retries would make it three either way.
        """

        wire.raises = failure
        model = _model()

        with caplog.at_level(logging.INFO):
            with pytest.raises(ExtractionError) as excinfo:
                await model.generate_json("hi")

        message = str(excinfo.value)
        assert message == (
            "Modal LLM call timed out after 300s (modal.request_timeout_s) — "
            "the server is alive but slow: lower the entry's max_tokens or "
            "raise TREE_MODAL__REQUEST_TIMEOUT_S"
        )
        # NOT 400: `_embed_chunk_resilient` skips a 400 as a poison input, and
        # a slow server is neither poison nor an HTTP answer at all.
        assert excinfo.value.status_code is None
        assert isinstance(excinfo.value.__cause__, openai_sdk.APITimeoutError)
        # ONE request: no SDK retry underneath, no gate retry above it.
        assert len(wire.requests) == 1
        assert suspending_server.poll_calls == 1
        lines = [record.getMessage() for record in caplog.records]
        assert not any("Cold again" in line for line in lines)
        assert not any("Retrying request" in line for line in lines)

    async def test_the_timed_out_message_carries_the_configured_number(
        self, suspending_server, wire, mocker
    ) -> None:
        """The message quotes the budget that was actually spent — an operator
        who raised the knob must not read the default back."""

        mocker.patch.object(app_config.modal, "request_timeout_s", 900.0)
        wire.raises = httpx.ReadTimeout("timed out")
        model = _model()

        with pytest.raises(ExtractionError) as excinfo:
            await model.generate_json("hi")

        assert "timed out after 900s" in str(excinfo.value)

    async def test_a_cold_answer_still_costs_exactly_one_re_warm(
        self, suspending_server, wire, caplog
    ) -> None:
        """Story 2 (the regression pin): the carve-out took ONLY the timeout
        out of the cold class — a real 503 still re-warms once and retries
        once, over the REAL SDK with the production retry budget."""

        model = _model()
        await model.ensure_warm()
        wire.cold_while = lambda: suspending_server.poll_calls < 2

        with caplog.at_level(logging.WARNING):
            result = await model.generate_json("hi")

        assert result == {"a": 1}
        assert suspending_server.poll_calls == 2
        # 2, not 6: the SDK's two silent retries per call are gone.
        assert len(wire.requests) == 2
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert f"Cold again: {_APP} answered HTTP 503" in warnings[0].getMessage()


class TestColdAgain:
    """ADR-009 §11: the warm flag is a HINT with an expiry — Modal scales the
    server back to zero after its idle window."""

    async def test_one_cold_answer_costs_one_re_warm_and_one_retry(
        self, server, openai, caplog
    ) -> None:
        """A long ingestion paused on a slow stage, Modal scaled the server to
        zero, and the next completion answers 503. One re-warm, one retry, no
        failed document."""

        model = _model()
        await model.generate_json("warm me")
        openai.answers = [_cold(), _response('{"a": 1}')]

        with caplog.at_level(logging.INFO):
            result = await model.generate_json("hi")

        assert result == {"a": 1}
        assert server.poll.await_count == 2
        warnings = [
            record for record in caplog.records if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert warnings[0].getMessage() == (
            f"Cold again: {_APP} answered HTTP 503 — re-warming once"
        )

    async def test_a_second_cold_answer_ends_it_with_the_status(
        self, server, openai
    ) -> None:
        model = _model()
        await model.generate_json("warm me")
        openai.answer = _cold()

        with pytest.raises(ExtractionError) as excinfo:
            await model.generate_json("hi")

        assert str(excinfo.value) == (
            f"{_APP} still answered HTTP 503 after one re-warm"
        )
        assert excinfo.value.status_code == 503
        assert server.poll.await_count == 2

    async def test_the_retry_uses_the_client_the_re_warm_built(
        self, server, openai
    ) -> None:
        """The re-warm replaces ``_client`` and the served id, so the retry must
        read both at CALL time — a lambda closed over the old client would talk
        to the dead one."""

        model = _model()
        await model.generate_json("warm me")
        server.served.return_value = "served/after-rewarm"
        openai.answers = [_cold(), _response('{"a": 1}')]

        await model.generate_json("hi")

        assert len(openai.built) == 2
        assert openai.calls[-1]["model"] == "served/after-rewarm"

    async def test_eight_concurrent_cold_calls_share_one_re_warm(
        self, suspending_server, wire, caplog
    ) -> None:
        """Eight extractions share ONE gate, all eight get a 503 from a server
        that idled out, and exactly one of them re-warms — nine re-warms would
        be nine boots of a billed GPU, each with its own 600 s budget.

        The REAL SDK over the wire (``max_retries=0``), so the 503 becomes the
        ``InternalServerError`` the gate classifies.
        """

        model = _model()
        await model.ensure_warm()
        # Cold until something polls it warm AGAIN — the server's own state,
        # not a request count.
        wire.cold_while = lambda: suspending_server.poll_calls < 2

        with caplog.at_level(logging.WARNING):
            results = await asyncio.gather(
                *(model.generate_json(f"chunk {index}") for index in range(8))
            )

        assert results == [{"a": 1}] * 8
        # 2, not 9: one warm at the start, one re-warm for the whole burst.
        assert suspending_server.resolve_calls == 2
        assert suspending_server.poll_calls == 2
        assert suspending_server.models_calls == 2
        warnings = [
            record for record in caplog.records if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1


class TestUsage:
    """Self-hosted: tokens yes, dollars no (ADR-009 §10)."""

    async def test_the_three_token_counts_are_recorded_under_the_repo_id(
        self, server, openai, mocker
    ) -> None:
        """``model`` is the catalog ``repo_id``, never the served id: the id a
        managed recipe serves under is an implementation detail, the repo id is
        what the YAML and the cost tables name."""

        record = mocker.patch.object(modal_llm, "update_current_span")
        server.served.return_value = "served/other-name"
        model = _model()

        await model.generate_json("hi")

        assert record.call_args.kwargs == {
            "provider": "modal",
            "model": _LFM,
            "total_cost": 0.0,
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        }

    async def test_a_response_without_usage_records_no_usage_key(
        self, server, openai, mocker
    ) -> None:
        """Three zeroes would read as "this call was free"; no key reads as
        "unknown", which is what it is."""

        record = mocker.patch.object(modal_llm, "update_current_span")
        model = _model()
        openai.answer = _response('{"a": 1}', usage=None)

        await model.generate_json("hi")

        assert record.call_args.kwargs == {
            "provider": "modal",
            "model": _LFM,
            "total_cost": 0.0,
        }

    async def test_a_failing_recorder_does_not_fail_the_completion(
        self, server, openai, mocker
    ) -> None:
        """Telemetry is fail-open — a broken Opik must never cost a document."""

        mocker.patch.object(
            modal_llm, "update_current_span", side_effect=RuntimeError("opik down")
        )
        model = _model()

        assert await model.generate_json("hi") == {"a": 1}

    async def test_nothing_is_recorded_for_a_failed_call(
        self, server, openai, mocker
    ) -> None:
        """A span that recorded usage for a call that raised would make a
        failed run look like a successful, cheap one."""

        record = mocker.patch.object(modal_llm, "update_current_span")
        model = _model()
        openai.answer = _bad_request()

        with pytest.raises(ExtractionError):
            await model.generate_json("hi")

        record.assert_not_called()


class TestWireContract:
    """What the REAL ``AsyncOpenAI`` puts on the wire, read off the transport.

    Everything here would pass against a recorder standing in for the SDK while
    failing against the SDK itself — which is how #140's body came to carry
    base64.
    """

    async def test_the_body_carries_the_model_messages_temperature_and_format(
        self, server, wire
    ) -> None:
        model = _model()

        result = await model.generate_json("extract this", system="be terse")

        body = wire.bodies[0]
        assert set(body) == {"model", "messages", "temperature", "response_format"}
        assert body["model"] == _LFM
        assert body["messages"] == [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "extract this"},
        ]
        assert body["temperature"] == 0
        assert body["response_format"] == {"type": "json_object"}
        assert result == {"a": 1}

    async def test_a_schema_reaches_the_wire_as_strict_json_schema(
        self, server, wire
    ) -> None:
        """SGLang compiles ``json_schema`` + ``strict: true`` into constrained
        decoding (``protocol.py`` v0.5.18) — the shape #141's live check sends."""

        model = _model()

        await model.generate_json("hi", schema={"type": "object"})

        assert wire.bodies[0]["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "strict": True,
                "schema": {"type": "object"},
            },
        }

    async def test_the_proxy_token_reaches_the_wire_as_a_bearer(
        self, server, wire
    ) -> None:
        """Stronger than the constructor kwarg: Modal's edge checks the header,
        not what we passed to ``AsyncOpenAI``."""

        model = _model()

        await model.generate_json("hi")

        assert wire.requests[0].headers["authorization"] == f"Bearer {_BEARER}"

    async def test_the_request_goes_to_v1_chat_completions(self, server, wire) -> None:
        model = _model()

        await model.generate_json("hi")

        assert str(wire.requests[0].url) == f"{_URL}/v1/chat/completions"

    async def test_prose_from_a_real_sdk_response_is_still_invalid_json(
        self, server, wire
    ) -> None:
        """Story 4 end to end: the SDK parses the completion, we reject it."""

        wire.content = "Sure! Here is the JSON…"
        model = _model()

        with pytest.raises(ExtractionError) as excinfo:
            await model.generate_json("hi")

        assert str(excinfo.value) == (
            "Modal LLM returned invalid JSON: Sure! Here is the JSON…"
        )


class TestBaseContract:
    """The pipeline holds a ``BaseLLM``, never a ``ModalLLM``."""

    def test_modal_llm_is_a_base_llm(self) -> None:
        assert issubclass(ModalLLM, BaseLLM)

    async def test_the_two_argument_contract_is_enough(self, server, openai) -> None:
        """``schema`` is an EXTRA keyword: a caller that knows only ``BaseLLM``
        must be served by ``(prompt, system=…)`` alone."""

        llm: BaseLLM = _model()

        result = await llm.generate_json("extract this", system="be terse")

        assert result == {"a": 1}
        assert "response_format" in openai.calls[0]


class TestProxyTokenIsNeverLeaked:
    """ADR-009 §9: the token value never reaches a log line, an exception or a
    repr — a token is forever, and these lines end up in Prefect run logs."""

    async def test_no_log_record_of_a_full_run_carries_the_token(
        self, server, openai, caplog
    ) -> None:
        model = _model()

        with caplog.at_level(logging.DEBUG):
            await model.generate_json("hi", system="be terse")
            await model.generate_json("again")

        assert caplog.records
        for record in caplog.records:
            assert _BEARER not in record.getMessage()
            assert "ws-2" not in record.getMessage()

    async def test_no_failure_message_carries_the_token(
        self, server, openai, caplog
    ) -> None:
        model = _model()
        await model.generate_json("warm me")
        openai.answer = _cold()

        with caplog.at_level(logging.DEBUG), pytest.raises(ExtractionError) as excinfo:
            await model.generate_json("hi")

        assert _BEARER not in str(excinfo.value)
        for record in caplog.records:
            assert _BEARER not in record.getMessage()

    def test_the_repr_carries_no_token(self) -> None:
        model = _model()

        assert _BEARER not in repr(model)
        assert "ws-2" not in repr(model)
