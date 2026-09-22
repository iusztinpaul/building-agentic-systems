"""Unit tests for ``tree.models.modal_embedding`` — the **Embedding catalog**
client (ADR-009 §3/§4/§5).

Nothing here touches Modal or the network: the three
:mod:`tree.models.modal_server` helpers and ``AsyncOpenAI`` are replaced by
fakes, and the **Proxy token** is always the FAKE pair ``wk-1`` / ``ws-2`` —
``make`` exports the developer's real ``.env`` into the test process, so a test
that read the real token would both leak it and pass for the wrong reason.

What is pinned is the contract that keeps a wrong-width vector out of the
1024-d mongot index: no ``dimensions`` on the wire, the wire width asserted
against the catalog entry, client-side truncation, and the returned width
asserted again.

Two layers of doubles, on purpose. Most tests replace ``client.embeddings``
with a recorder, which is enough to read back the call kwargs. The wire
contract and the initialisation race are tested against the REAL
``AsyncOpenAI`` over an in-process ``httpx.MockTransport`` (``wire``) instead:
an empty batch and the request encoding are decided INSIDE the SDK, so a
recorder that stands in for it cannot see either.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai as openai_sdk
import pytest
from openai import AsyncOpenAI

from tests.unit.models.modal_fixtures import (
    SuspendingServer,
    patch_server,
    patch_suspending_server,
)
from tree.config.app_config import app_config
from tree.models import modal_embedding
from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_catalog import get_catalog_entry, truncate_embedding
from tree.models.modal_embedding import ModalEmbeddingModel

_QWEN = "Qwen/Qwen3-Embedding-0.6B"
_VOYAGE = "voyageai/voyage-4-nano"
_URL = "https://acme--ep-tree-voyage-4-nano-server.modal.run"
_BEARER = "wk-1.ws-2"


# --- doubles ----------------------------------------------------------------


def _vector(width: int, seed: int = 0) -> list[float]:
    """A deterministic, non-degenerate vector of ``width`` components."""

    return [((index + seed) % 97 + 1) / 97 for index in range(width)]


def _response(vectors: list[list[float]], total_tokens: int | None = 7) -> Any:
    """An OpenAI-shaped embeddings response over ``vectors``."""

    return SimpleNamespace(
        data=[SimpleNamespace(embedding=vector) for vector in vectors],
        usage=SimpleNamespace(total_tokens=total_tokens),
    )


def _cold(status: int = 503) -> openai_sdk.InternalServerError:
    """What a scaled-to-zero container looks like AT CALL TIME: the SDK maps
    every status >= 500 to ``InternalServerError``."""

    request = httpx.Request("POST", f"{_URL}/v1/embeddings")
    return openai_sdk.InternalServerError(
        "boom", response=httpx.Response(status, request=request), body=None
    )


def _bad_request() -> openai_sdk.BadRequestError:
    """A 400 — never a cold start, so it must reach the existing wrapper."""

    request = httpx.Request("POST", f"{_URL}/v1/embeddings")
    return openai_sdk.BadRequestError(
        "nope", response=httpx.Response(400, request=request), body=None
    )


class _Embeddings:
    """``client.embeddings`` — records every call, answers the recorder.

    ``answers`` is a queue for the re-warm tests (cold first, vectors after);
    ``answer`` is the single standing answer every other test sets.
    """

    def __init__(self, recorder: "_OpenAI") -> None:
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
        self.answer: Any = _response([])

    def build(self, **kwargs: Any) -> SimpleNamespace:
        self.built.append(kwargs)
        return SimpleNamespace(embeddings=_Embeddings(self))


@pytest.fixture
def openai(mocker) -> _OpenAI:
    """Patch ``AsyncOpenAI`` in the module under test."""

    recorder = _OpenAI()
    mocker.patch.object(modal_embedding, "AsyncOpenAI", recorder.build)
    return recorder


@pytest.fixture
def server(mocker) -> SimpleNamespace:
    """The warm body's three coroutines, patched on THIS client's bindings."""

    return patch_server(mocker, modal_embedding, url=_URL, served_model=_VOYAGE)


class _WireStub:
    """An OpenAI-compatible server the REAL ``AsyncOpenAI`` talks to.

    ``_OpenAI`` above replaces ``client.embeddings`` and therefore skips every
    decision the SDK makes for us — the default ``encoding_format``, the
    response post-parser, the JSON body. Both defects this file now regresses
    lived exactly there, so the wire tests keep the SDK and replace only its
    transport.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.vectors: list[list[float]] = [_vector(2048)]
        #: While this answers True every POST is a 503 — a server that scaled
        #: to zero between the warm and the call. A PERIOD, not a request
        #: count: the real server stays cold until a poll boots it, so the
        #: predicate is normally "until the re-warm polled it". With
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
                "object": "list",
                "model": "served/stub",
                "data": [
                    {"object": "embedding", "index": index, "embedding": vector}
                    for index, vector in enumerate(self.vectors)
                ],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
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
        # request would inflate the counts these tests assert on instead of
        # passing silently (#155).
        client = AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(stub.handle)),
        )
        clients.append(client)
        return client

    mocker.patch.object(modal_embedding, "AsyncOpenAI", build)
    yield stub
    for client in clients:
        await client.close()


@pytest.fixture
def suspending_server(mocker) -> SuspendingServer:
    """The same three coroutines, as doubles that really SUSPEND."""

    return patch_suspending_server(mocker, modal_embedding, url=_URL)


def _model(model: str = _VOYAGE, **kwargs: Any) -> ModalEmbeddingModel:
    """A client on the FAKE proxy token."""

    return ModalEmbeddingModel(proxy_token=_BEARER, model=model, **kwargs)


# --- tests ------------------------------------------------------------------


class TestInit:
    """Everything that can be known offline fails offline (stories 5 and 8)."""

    def test_an_empty_proxy_token_names_both_env_vars(self) -> None:
        with pytest.raises(ModelError) as excinfo:
            ModalEmbeddingModel(proxy_token="", model=_VOYAGE)

        message = str(excinfo.value)
        assert "Modal proxy token is required" in message
        assert "MODAL_PROXY_TOKEN_ID" in message
        assert "MODAL_PROXY_TOKEN_SECRET" in message

    def test_an_unknown_model_fails_with_the_catalog_error(self) -> None:
        with pytest.raises(ModelError) as excinfo:
            _model("BAAI/bge-m3")

        message = str(excinfo.value)
        assert "Unknown Modal model 'BAAI/bge-m3'" in message
        assert _QWEN in message
        assert _VOYAGE in message

    async def test_construction_makes_no_call(self, server, openai) -> None:
        """Story 5: a missing token (or a wrong id) must cost nothing — the
        first network call happens in ``embed()``, not here."""

        _model()

        server.resolve.assert_not_awaited()
        server.poll.assert_not_awaited()
        assert openai.built == []


class TestUrlResolution:
    async def test_the_first_embed_resolves_the_entry_and_builds_one_v1_url(
        self, server, openai
    ) -> None:
        model = _model()
        openai.answer = _response([_vector(2048)])

        await model.embed(["cats"])

        server.resolve.assert_awaited_once_with(get_catalog_entry(_VOYAGE))
        base_url = openai.built[0]["base_url"]
        assert base_url == f"{_URL}/v1"
        assert base_url.count("/v1") == 1

    async def test_a_second_embed_resolves_nothing_again(self, server, openai) -> None:
        model = _model()
        openai.answer = _response([_vector(2048)])

        await model.embed(["cats"])
        await model.embed(["dogs"])

        assert server.resolve.await_count == 1
        assert server.poll.await_count == 1
        assert server.served.await_count == 1
        assert len(openai.built) == 1
        assert len(openai.calls) == 2

    async def test_a_model_error_from_resolve_propagates_unchanged(
        self, server, openai
    ) -> None:
        """Story 7: the model was stopped — the operator gets the deploy
        command, not a wrapped transport error."""

        failure = (
            "Failed to resolve Modal server ep-tree-voyage-4-nano/Server. Is the "
            "model deployed (...)? Run: make memory-deploy-model "
            f"MODEL={_VOYAGE}"
        )
        server.resolve.side_effect = ModelError(failure)
        model = _model()

        with pytest.raises(ModelError) as excinfo:
            await model.embed(["cats"])

        # Unchanged means both: the same message AND the same type — a
        # stopped model is not a transient failure to retry.
        assert type(excinfo.value) is ModelError
        assert str(excinfo.value) == failure
        assert openai.calls == []

    @pytest.mark.parametrize("repo_id", [_QWEN, _VOYAGE])
    async def test_serving_path_is_invisible_to_the_client(
        self, server, openai, repo_id: str
    ) -> None:
        """Story 6: the endpoint seed and the vllm seed resolve identically —
        moving a model down the ladder changes YAML, never the client."""

        entry = get_catalog_entry(repo_id)
        server.served.return_value = repo_id
        model = _model(repo_id)
        openai.answer = _response([_vector(entry.native_dimensions)])

        await model.embed(["cats"])

        server.resolve.assert_awaited_once_with(entry)
        assert openai.built[0]["base_url"] == f"{_URL}/v1"
        assert openai.calls[0]["model"] == repo_id

    def test_the_client_never_reads_the_entrys_serving_field(self) -> None:
        """The same source assertion as the task's grep: a branch on how the
        model is served would make the ladder a code change."""

        source = Path(modal_embedding.__file__).read_text()

        assert ".serving" not in source


class TestProxyAuth:
    async def test_the_openai_client_gets_the_bearer_as_its_api_key(
        self, server, openai
    ) -> None:
        """Modal joins the Proxy token halves with a '.' — the same scheme the
        OpenAI API uses, so the joined value IS the api_key."""

        model = _model()
        openai.answer = _response([_vector(2048)])

        await model.embed(["cats"])

        assert openai.built[0]["api_key"] == _BEARER

    async def test_the_health_poll_gets_the_bearer_header_and_the_deadline(
        self, server, openai
    ) -> None:
        """The poll sends the SAME ``Authorization: Bearer`` header the
        embeddings call does (ADR-009 §4) — Modal's edge checks it before a GPU
        container wakes."""

        model = _model(warmup_deadline_s=42.0)
        openai.answer = _response([_vector(2048)])

        await model.embed(["cats"])

        server.poll.assert_awaited_once_with(
            f"{_URL}/health",
            {"Authorization": f"Bearer {_BEARER}"},
            deadline_s=42.0,
        )

    async def test_the_deadline_defaults_to_the_configured_budget(
        self, server, openai
    ) -> None:
        """ONE knob (ADR-009 §11), read at CONSTRUCTION — the client's own
        300 s default is gone."""

        model = _model()
        openai.answer = _response([_vector(2048)])

        await model.embed(["cats"])

        assert (
            server.poll.await_args.kwargs["deadline_s"]
            == app_config.modal.warmup_deadline_s
        )

    async def test_a_401_model_error_propagates_and_embeds_nothing(
        self, server, openai
    ) -> None:
        """A wrong token is a configuration error: no retry can fix it, so the
        401 must NOT be re-typed into a retryable ``ExtractionError``."""

        server.poll.side_effect = ModelError(
            "Health poll of .../health returned HTTP 401 — not a cold start, "
            "giving up after 1 attempt."
        )
        model = _model()

        with pytest.raises(ModelError) as excinfo:
            await model.embed(["cats"])

        # `ExtractionError` IS a `ModelError`, so the type must be exact —
        # matching the base class alone would accept the retryable wrapping.
        assert type(excinfo.value) is ModelError
        assert "401" in str(excinfo.value)
        assert openai.calls == []

    async def test_a_spent_deadline_propagates_as_a_retryable_error(
        self, server, openai
    ) -> None:
        """Story 5: a server that never comes up IS retryable — the spent
        budget stays an ExtractionError, wrapped in nothing."""

        server.poll.side_effect = ExtractionError(
            "Health poll of .../health gave up after 600s (deadline 600s); "
            "last result: HTTP 503",
            status_code=503,
        )
        model = _model()

        with pytest.raises(ExtractionError) as excinfo:
            await model.embed(["cats"])

        assert "gave up after 600s" in str(excinfo.value)
        assert excinfo.value.status_code == 503


class TestServedModel:
    async def test_the_request_uses_the_discovered_id(
        self, server, openai, mocker
    ) -> None:
        """A managed recipe — not us — names the served model, so the id comes
        from /v1/models; Opik keeps recording the catalog repo id."""

        record = mocker.patch.object(modal_embedding, "record_embedding_usage")
        server.served.return_value = "served/other-name"
        model = _model()
        openai.answer = _response([_vector(2048)])

        await model.embed(["cats"])

        assert openai.calls[0]["model"] == "served/other-name"
        server.served.assert_awaited_once_with(_URL, _BEARER, default=_VOYAGE)
        assert record.call_args.kwargs["model"] == _VOYAGE
        assert record.call_args.kwargs["provider"] == "modal"
        assert record.call_args.kwargs["total_cost"] == 0.0


class TestMatryoshkaGuard:
    """Story 8: a width the model cannot produce fails at construction."""

    @pytest.mark.parametrize(
        "repo_id,requested,expected",
        [
            (_VOYAGE, None, 2048),
            (_VOYAGE, 2048, 2048),
            (_VOYAGE, 1024, 1024),
            (_VOYAGE, 512, 512),
            (_QWEN, None, 1024),
            (_QWEN, 1024, 1024),
        ],
    )
    def test_an_allowed_width_becomes_the_effective_dimensions(
        self, repo_id: str, requested: int | None, expected: int
    ) -> None:
        model = _model(repo_id, dimensions=requested)

        assert model.dimensions == expected

    def test_a_width_no_matryoshka_size_covers_is_refused(self) -> None:
        with pytest.raises(ModelError) as excinfo:
            _model(_VOYAGE, dimensions=768)

        message = str(excinfo.value)
        assert "cannot produce 768-d" in message
        assert "native 2048" in message
        assert "[256, 512, 1024, 2048]" in message

    def test_a_model_that_lists_no_matryoshka_size_refuses_every_other_width(
        self,
    ) -> None:
        """Story 8's exact case: Qwen3 at 512."""

        with pytest.raises(ModelError) as excinfo:
            _model(_QWEN, dimensions=512)

        message = str(excinfo.value)
        assert "cannot produce 512-d" in message
        assert "native 1024" in message
        assert "matryoshka_dimensions []" in message


class TestClientSideTruncation:
    """ADR-009 §3: the wire always carries the native width; the memory stores
    the truncated one."""

    @pytest.mark.parametrize("requested", [None, 2048, 1024, 512])
    async def test_no_dimensions_parameter_is_ever_sent(
        self, server, openai, requested: int | None
    ) -> None:
        """vLLM 400s on `dimensions` unless the HF config is flagged
        Matryoshka, and a managed recipe cannot be given that flag."""

        model = _model(dimensions=requested)
        openai.answer = _response([_vector(2048)])

        await model.embed(["cats"])

        assert "dimensions" not in openai.calls[0]
        assert openai.calls[0]["encoding_format"] == "float"

    async def test_a_truncated_vector_is_unit_length_and_matches_the_helper(
        self, server, openai
    ) -> None:
        """Story 3: the caller receives exactly what the smoke test checked
        under `sanity@1024`."""

        raw = [_vector(2048, seed=1), _vector(2048, seed=2)]
        model = _model(dimensions=1024)
        openai.answer = _response(raw)

        vectors = await model.embed(["cats", "dogs"])

        assert [len(vector) for vector in vectors] == [1024, 1024]
        for vector, source in zip(vectors, raw, strict=True):
            assert math.sqrt(sum(v * v for v in vector)) == pytest.approx(1.0, abs=1e-6)
            assert vector == pytest.approx(truncate_embedding(source, 1024))

    async def test_the_native_width_is_returned_unchanged(self, server, openai) -> None:
        raw = [_vector(2048, seed=3)]
        model = _model(dimensions=None)
        openai.answer = _response(raw)

        vectors = await model.embed(["cats"])

        assert vectors == raw


class TestResponseLength:
    """Story 4: the catalog was wrong about a model once — never again
    silently."""

    async def test_a_narrower_response_than_the_entry_claims_raises(
        self, server, openai
    ) -> None:
        model = _model(dimensions=1024)
        openai.answer = _response([_vector(1024)])

        with pytest.raises(ExtractionError) as excinfo:
            await model.embed(["cats"])

        message = str(excinfo.value)
        assert "returned 1024-d vectors" in message
        assert "native_dimensions 2048" in message
        assert "No vector was returned." in message

    async def test_a_wider_response_than_the_entry_claims_raises(
        self, server, openai
    ) -> None:
        server.served.return_value = _QWEN
        model = _model(_QWEN)
        openai.answer = _response([_vector(2048)])

        with pytest.raises(ExtractionError) as excinfo:
            await model.embed(["cats"])

        message = str(excinfo.value)
        assert "returned 2048-d vectors" in message
        assert "native_dimensions 1024" in message

    async def test_one_wrong_item_at_the_end_of_a_batch_raises(
        self, server, openai
    ) -> None:
        """Every vector is checked, not a sample: one bad row in a batch is
        one bad row in the index."""

        model = _model(dimensions=1024)
        openai.answer = _response([_vector(2048), _vector(2048), _vector(1024)])

        with pytest.raises(ExtractionError, match="returned 1024-d vectors"):
            await model.embed(["a", "b", "c"])


class TestRolePrompts:
    """ADR-009 §5: the **Embedding role** is a client-side prefix, because
    OpenAI-compatible /v1/embeddings has no role field on any Serving path."""

    @pytest.mark.parametrize(
        "role,expected",
        [
            (
                "query",
                "Represent the query for retrieving supporting documents: cats",
            ),
            ("document", "Represent the document for retrieval: cats"),
            (None, "cats"),
        ],
    )
    async def test_the_voyage_prompts_are_prepended(
        self, server, openai, role, expected: str
    ) -> None:
        model = _model()
        openai.answer = _response([_vector(2048)])

        await model.embed(["cats"], input_type=role)

        assert openai.calls[0]["input"] == [expected]

    async def test_a_model_with_no_document_prompt_sends_the_text_untouched(
        self, server, openai
    ) -> None:
        """Qwen3 instructs the query side only — an empty prompt is not an
        error, it is the model's contract."""

        server.served.return_value = _QWEN
        model = _model(_QWEN)
        openai.answer = _response([_vector(1024)])

        await model.embed(["cats"], input_type="document")

        assert openai.calls[0]["input"] == ["cats"]

    async def test_the_qwen_query_prompt_comes_from_the_catalog(
        self, server, openai
    ) -> None:
        """Story 9: the bytes of the prompt are the contract — read them from
        the entry rather than re-typing them here."""

        server.served.return_value = _QWEN
        model = _model(_QWEN)
        openai.answer = _response([_vector(1024)])

        await model.embed(["cats"], input_type="query")

        prompt = get_catalog_entry(_QWEN).query_prompt
        assert prompt
        assert openai.calls[0]["input"] == [f"{prompt}cats"]


class TestReadyLog:
    async def test_the_ready_line_carries_both_widths(
        self, server, openai, caplog
    ) -> None:
        """Stories 1-3: one line tells the operator which app answered, under
        which model id, and whether the vectors are truncated."""

        model = _model(dimensions=1024)
        openai.answer = _response([_vector(2048)])

        with caplog.at_level(logging.INFO):
            await model.embed(["cats"])

        line = next(
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("ModalEmbeddingModel ready")
        )
        assert "app=ep-tree-voyage-4-nano" in line
        assert "server=Server" in line
        assert f"served_model={_VOYAGE}" in line
        assert "native=2048" in line
        assert "dimensions=1024 (truncated client-side)" in line
        assert line.endswith(f"url={_URL}/v1")


class TestEmbedFailure:
    async def test_a_failing_request_becomes_a_retryable_extraction_error(
        self, server, openai
    ) -> None:
        openai.answer = RuntimeError("timeout")
        model = _model()

        with pytest.raises(ExtractionError, match="Embedding call failed"):
            await model.embed(["cats"])


class TestRequestTimeout:
    """ADR-009 §11: one call, one bound, one retry — and that retry is the
    gate's. The embedding client gets the identical treatment to the LLM one:
    a huge batch against a small GPU is slow the same way a thinking model is.
    """

    async def test_the_client_is_bounded_and_never_retries_silently(
        self, server, openai
    ) -> None:
        """``modal.request_timeout_s`` is read at CONSTRUCTION, like the warm-up
        deadline, and ``max_retries=0`` leaves the ONE retry to the gate."""

        model = _model()
        openai.answer = _response([_vector(2048)])

        await model.embed(["cats"])

        assert openai.built[0]["timeout"] == 300.0
        assert openai.built[0]["max_retries"] == 0

    async def test_the_operator_can_widen_the_budget_for_one_process(
        self, server, openai, mocker
    ) -> None:
        """Story 3's twin: ``TREE_MODAL__REQUEST_TIMEOUT_S`` on the serving
        process, and nothing else changes."""

        mocker.patch.object(app_config.modal, "request_timeout_s", 45.0)
        model = _model()
        openai.answer = _response([_vector(2048)])

        await model.embed(["cats"])

        assert openai.built[0]["timeout"] == 45.0

    async def test_the_real_sdk_client_carries_the_bound_and_no_retries(
        self, server, wire
    ) -> None:
        """Read off the SDK object, not off the kwargs: ``timeout=`` and
        ``max_retries=`` are what the REAL ``AsyncOpenAI`` will enforce."""

        model = _model()

        await model.embed(["cats"])

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
        """Through the REAL SDK: both httpx timeouts become
        ``APITimeoutError``, the request is sent ONCE, nothing re-warms, and
        the message says which knob to turn.

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
                await model.embed(["cats"])

        assert str(excinfo.value) == (
            "Modal embedding call timed out after 300s (modal.request_timeout_s)"
            " — raise TREE_MODAL__REQUEST_TIMEOUT_S"
        )
        # NOT 400: `_embed_chunk_resilient` skips a 400 as a poison input and
        # bisects the batch around it. A slow server is neither poison nor an
        # HTTP answer at all, so the chunk must re-raise, not be dropped.
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
            await model.embed(["cats"])

        assert "timed out after 900s" in str(excinfo.value)

    async def test_a_cold_answer_still_costs_exactly_one_re_warm(
        self, suspending_server, wire, caplog
    ) -> None:
        """The regression pin: the carve-out took ONLY the timeout out of the
        cold class — a real 503 still re-warms once and retries once, over the
        REAL SDK with the production retry budget."""

        model = _model()
        await model.ensure_warm()
        wire.cold_while = lambda: suspending_server.poll_calls < 2

        with caplog.at_level(logging.WARNING):
            vectors = await model.embed(["cats"])

        assert len(vectors[0]) == 2048
        assert suspending_server.poll_calls == 2
        # 2, not 6: the SDK's two silent retries per call are gone.
        assert len(wire.requests) == 2
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "answered HTTP 503" in warnings[0].getMessage()


class TestWireContract:
    """What the REAL ``AsyncOpenAI`` puts on the wire, read off the transport.

    Everything here would pass against a recorder standing in for the SDK
    while failing against the SDK itself — which is how an empty batch came to
    raise and how the body came to carry base64.
    """

    async def test_the_body_carries_exactly_model_input_and_float_encoding(
        self, server, wire
    ) -> None:
        """``encoding_format`` is explicit because the SDK otherwise defaults
        to base64, while the shared smoke test (``modal_server._embed``) sends
        ``float`` — one wire contract on all three Serving paths, and no
        engine or recipe has to implement base64 for us."""

        model = _model(dimensions=1024)

        vectors = await model.embed(["cats"], input_type="query")

        body = wire.bodies[0]
        assert set(body) == {"model", "input", "encoding_format"}
        assert body["encoding_format"] == "float"
        assert "dimensions" not in body
        assert body["input"] == [
            "Represent the query for retrieving supporting documents: cats"
        ]
        assert len(vectors[0]) == 1024

    async def test_the_proxy_token_reaches_the_wire_as_a_bearer(
        self, server, wire
    ) -> None:
        """Stronger than the constructor kwarg: Modal's edge checks the
        header, not what we passed to ``AsyncOpenAI``."""

        model = _model()

        await model.embed(["cats"])

        assert wire.requests[0].headers["authorization"] == f"Bearer {_BEARER}"

    async def test_an_empty_batch_returns_early_without_waking_the_endpoint(
        self, server, wire
    ) -> None:
        """``input=[]`` makes the server answer ``{"data": []}``, which the
        SDK's own post-parser rejects — and waking a scaled-to-zero GPU to ask
        for nothing bills the operator. Same guard as
        ``VoyageTextEmbeddingModel.embed``, above the initialisation."""

        model = _model()

        assert await model.embed([]) == []

        server.resolve.assert_not_awaited()
        server.poll.assert_not_awaited()
        server.served.assert_not_awaited()
        assert wire.requests == []


class TestConcurrentInitialisation:
    """One shared model instance, several callers embedding at once.

    The **Warm gate** subsumed #140's double-checked ``_init_lock``: the URL
    lookup, the health poll, the served-model discovery and the client
    construction are ONE all-or-nothing single-flight body. N racing first
    ``embed()`` calls would otherwise mean N poll loops (up to 600s each)
    against a billed endpoint plus N orphaned HTTP clients.
    """

    @pytest.mark.parametrize("callers", [3, 8, 10])
    async def test_concurrent_first_embeds_initialise_exactly_once(
        self, suspending_server, wire, callers: int
    ) -> None:
        """Story 4: eight (here also three and ten) coroutines embed on one
        fresh instance — one URL lookup, one poll loop, one /v1/models call."""

        model = _model()

        results = await asyncio.gather(
            *(model.embed([str(index)]) for index in range(callers))
        )

        assert suspending_server.resolve_calls == 1
        assert suspending_server.poll_calls == 1
        assert suspending_server.models_calls == 1
        assert [len(vectors[0]) for vectors in results] == [2048] * callers
        assert len(wire.requests) == callers

    async def test_a_failed_initialisation_is_retried_in_full(
        self, suspending_server, wire
    ) -> None:
        """A spent deadline must leave NOTHING half-built: the next call re-runs
        resolve -> poll -> /v1/models before it embeds."""

        suspending_server.poll_error = ExtractionError("gave up after 600s")
        model = _model()

        with pytest.raises(ExtractionError, match="gave up after 600s"):
            await model.embed(["cats"])
        assert wire.requests == []

        suspending_server.poll_error = None
        vectors = await model.embed(["cats"])

        assert suspending_server.resolve_calls == 2
        assert suspending_server.poll_calls == 2
        # 1, not 2: the failed attempt never got past the poll, so the retry is
        # the first time the served id is discovered at all.
        assert suspending_server.models_calls == 1
        assert len(vectors[0]) == 2048

    async def test_every_concurrent_waiter_sees_the_initialisation_failure(
        self, suspending_server, wire
    ) -> None:
        """No waiter may fall through to the embeddings call with an unbuilt
        client — and each keeps the unwrapped poll error."""

        suspending_server.poll_error = ExtractionError("gave up after 600s")
        model = _model()

        results = await asyncio.gather(
            model.embed(["a"]),
            model.embed(["b"]),
            model.embed(["c"]),
            return_exceptions=True,
        )

        assert [type(result) for result in results] == [ExtractionError] * 3
        assert all("gave up after 600s" in str(result) for result in results)
        assert wire.requests == []

    async def test_a_cancelled_waiter_leaves_the_instance_usable(
        self, suspending_server, wire
    ) -> None:
        """An MCP tool call that its client gave up on must not deadlock the
        next embed, nor leave a half-built client behind."""

        model = _model()
        callers = [asyncio.create_task(model.ensure_warm()) for _ in range(3)]
        await asyncio.sleep(0)
        # The winner is INSIDE the warm body and the other two are queued on
        # its lock — without this the cancelled task might never reach it.
        assert suspending_server.resolve_calls == 1

        callers[-1].cancel()
        results = await asyncio.gather(*callers, return_exceptions=True)

        assert isinstance(results[-1], asyncio.CancelledError)
        assert results[:-1] == [None, None]
        assert suspending_server.poll_calls == 1
        # The instance still works, on the warm the winner finished.
        assert len((await model.embed(["again"]))[0]) == 2048
        assert suspending_server.poll_calls == 1


class TestWarmAtUse:
    """ADR-009 §11: the warm flag is a HINT with an expiry — Modal scales the
    server back to zero after its idle window."""

    async def test_embed_rewarms_once_when_cold_again(
        self, server, openai, caplog
    ) -> None:
        """Story 3: a long ingestion paused on a slow LLM stage, Modal scaled
        the embedding server to zero, and the next embed answers 503. One
        re-warm, one retry, no failed document."""

        model = _model(dimensions=1024)
        openai.answer = _response([_vector(2048)])
        await model.embed(["warm me"])
        openai.answers = [_cold(), _response([_vector(2048)])]

        with caplog.at_level(logging.INFO):
            returned = await model.embed(["cats"])

        assert [len(vector) for vector in returned] == [1024]
        assert server.poll.await_count == 2
        warnings = [
            record for record in caplog.records if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert (
            warnings[0].getMessage()
            == "Cold again: ep-tree-voyage-4-nano answered HTTP 503 — re-warming once"
        )

    async def test_embed_wraps_non_cold_errors(self, server, openai) -> None:
        """A 400 is the caller's problem, not a cold start: no re-warm, and the
        existing retryable wrapper still applies."""

        model = _model()
        openai.answer = _response([_vector(2048)])
        await model.embed(["warm me"])
        openai.answer = _bad_request()

        with pytest.raises(ExtractionError, match="Embedding call failed"):
            await model.embed(["cats"])

        assert server.poll.await_count == 1

    async def test_the_retry_uses_the_client_the_re_warm_built(
        self, server, openai
    ) -> None:
        """The re-warm replaces ``_client`` and the served id, so the retry must
        read both at CALL time — a lambda closed over the old client would talk
        to the dead one."""

        model = _model()
        openai.answer = _response([_vector(2048)])
        await model.embed(["warm me"])
        server.served.return_value = "served/after-rewarm"
        openai.answers = [_cold(), _response([_vector(2048)])]

        await model.embed(["cats"])

        assert len(openai.built) == 2
        assert openai.calls[-1]["model"] == "served/after-rewarm"

    async def test_model_exposes_ensure_warm_and_warm_key(self, server, openai) -> None:
        """#149's pre-warm is duck-typed on ``ensure_warm`` and warms each
        distinct Modal app once — hence ``warm_key``."""

        model = _model()

        await model.ensure_warm()
        await model.ensure_warm()

        assert model.warm_key == "ep-tree-voyage-4-nano"
        assert server.poll.await_count == 1
        assert server.resolve.await_count == 1

    async def test_eight_concurrent_cold_embeds_share_one_re_warm(
        self, suspending_server, wire, caplog
    ) -> None:
        """The burst the ADR's consequence names: eight extractions share ONE
        gate, all eight get a 503 from a server that idled out, and exactly one
        of them re-warms — nine re-warms would be nine boots of a billed GPU,
        each with its own 600 s budget.

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
                *(model.embed([str(index)]) for index in range(8))
            )

        assert [len(vectors[0]) for vectors in results] == [2048] * 8
        # 2, not 9: one warm at the start, one re-warm for the whole burst.
        assert suspending_server.resolve_calls == 2
        assert suspending_server.poll_calls == 2
        assert suspending_server.models_calls == 2
        # At least one retry on top of the eight calls. How many of the eight
        # see the 503 at all depends on how the loop interleaves them — the
        # deterministic "all eight answer cold" proof is the gate's own
        # ``test_eight_concurrent_cold_callers_log_one_warning``.
        assert len(wire.requests) >= 9
        warnings = [
            record for record in caplog.records if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1

    async def test_empty_batch_never_warms(self, server, openai) -> None:
        """An empty call must never wake a scaled-to-zero GPU: the guard sits
        ABOVE the gate."""

        model = _model()

        assert await model.embed([]) == []

        server.poll.assert_not_awaited()
        server.resolve.assert_not_awaited()
        assert openai.calls == []
