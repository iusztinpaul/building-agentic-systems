"""Talking to a served **Modal catalog** model — the same way on every
**Serving path** (ADR-009 §3/§4).

ONE URL lookup, ONE served-model discovery and ONE smoke test PER KIND
(embeddings: :func:`smoke_test`; LLMs: :func:`chat_smoke_test`), shared by the
deploy driver (``scripts/modal_model.py``) and the
``ModalEmbeddingModel`` client (#140); waiting out a cold start belongs to the
**Warm gate**'s poller (:mod:`tree.models.modal_warmup`), which the smoke test
below and the client both call. They rest on assumption H1 — a
**Dedicated endpoint** named ``N`` is the Modal app ``ep-N`` with a server class
``Server``, which our App scripts copy — so the caller never learns which
path served it. Every ``N`` of ours is ``tree-<slug>``, hence every app is
``ep-tree-<slug>``: H1 needs the shape, not a particular name. ``tasks/141`` proves H1 live and owns the correction if it is false.

This module imports the ``modal`` SDK, so it is imported by the driver and the
client only — never by :mod:`tree.models.modal_catalog` and never at MCP boot.
"""

import json
import logging
import math
from typing import Any

import aiohttp
import modal
from pydantic import BaseModel, Field

from tree.config.app_config import (
    ModalEmbeddingModelConfig,
    ModalLLMModelConfig,
    ModalModelConfig,
    app_config,
)
from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_catalog import (
    CHAT_RESPONSE_FORMAT,
    MODAL_SERVER_NAME,
    chat_request_knobs,
    empty_answer_details,
    get_embedding_entry,
    get_llm_entry,
    modal_proxy_bearer,
    prompt_for,
    reasoning_length,
    truncate_embedding,
)
from tree.models.modal_warmup import poll_health

logger = logging.getLogger(__name__)

# The ONE question every chat smoke test asks, in JSON mode with its schema in
# the PROMPT — as every caller of `generate_json` carries its own (ADR-009
# §10). Gating on the SHAPE, never on the content: a 350M model may believe
# anything about Tokyo, but a server that cannot return a JSON object is broken
# for `ModalLLM`, which is the thing this proves.
CITY_FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "population": {"type": "integer"},
    },
    "required": ["city", "population"],
    "additionalProperties": False,
}
CHAT_SMOKE_PROMPT = (
    "Reply with JSON facts about Tokyo. Answer with ONE JSON object matching "
    "this JSON Schema: " + json.dumps(CITY_FACTS_SCHEMA, separators=(",", ":"))
)

# The in-container warm-up spends 64; from the laptop the budget is wider
# because a REASONING model may spend tokens thinking before the JSON, and a
# completion cut short would fail as "not valid JSON" — a wrong diagnosis.
CHAT_SMOKE_MAX_TOKENS = 256

# How much of a bad completion the failure message carries: enough to
# recognise a `<think>` block or a "Sure! Tokyo is…" preamble, short enough to
# stay one readable line in a terminal.
_CONTENT_EXCERPT = 200

# The three texts every smoke test embeds: one question, the document that
# answers it and one that does not. A wrong architecture or pooling returns
# well-shaped floats, so the ONLY cheap check is that the relevant document
# scores higher than the unrelated one (ADR-009 §2).
SMOKE_QUERY = "how do I reset my password?"
SMOKE_RELEVANT = "To reset your password, open Settings and choose Reset password."
SMOKE_UNRELATED = "The Eiffel Tower is 330 metres tall."

# How far a truncated vector may sit from unit length before we call the
# client-side Matryoshka truncation broken.
_NORM_TOLERANCE = 1e-3


class ChatSmokeTestReport(BaseModel):
    """What one CHAT smoke test measured — the evidence ``tasks/141`` records."""

    url: str = Field(description="Root URL of the served model (no /v1).")
    served_model: str = Field(description="The id discovered from /v1/models.")
    cold_start_seconds: float = Field(description="Seconds until /health was 200.")
    keys: list[str] = Field(
        description="The completion object's keys, sorted — what JSON mode "
        "actually returned."
    )
    follows_prompt_schema: bool = Field(
        description="Did that object match the city_facts shape the PROMPT "
        "asked for? RECORDED, never gated: JSON mode promises an object, "
        "never its keys."
    )
    unauthenticated_status: int = Field(
        description="What /health answers WITHOUT the Proxy token — 401."
    )


class SmokeTestReport(BaseModel):
    """What one smoke test measured — the evidence ``tasks/141`` records."""

    url: str = Field(description="Root URL of the served model (no /v1).")
    served_model: str = Field(description="The id discovered from /v1/models.")
    dimensions: int = Field(description="Vector width on the wire.")
    truncated_dimensions: int | None = Field(
        description="Width after client-side truncation, or None when the "
        "entry lists no Matryoshka size below its native width."
    )
    cold_start_seconds: float = Field(description="Seconds until /health was 200.")
    cos_relevant: float
    cos_unrelated: float
    unauthenticated_status: int = Field(
        description="What /health answers WITHOUT the Proxy token — 401."
    )


async def resolve_server_url(entry: ModalModelConfig) -> str:
    """The root URL of the Modal app serving ``entry`` (no trailing ``/v1``).

    Takes an entry of EITHER kind: it reads only ``app_name`` and ``repo_id``,
    which every catalog entry has, and under H1 the lookup is the same on both
    **Serving paths** — so the embedding smoke test, the chat smoke test and
    both clients share this one function.

    Resolved without blocking the event loop (``get_url.aio()`` on the pinned
    client, modal 1.5.5).

    Raises:
        ModelError: the lookup failed or the server has no URL — most often
            because the model is not deployed, or a Dedicated endpoint is
            still provisioning.
    """

    failure = (
        f"Failed to resolve Modal server {entry.app_name}/{MODAL_SERVER_NAME}. "
        "Is the model deployed (a Dedicated endpoint may still be provisioning "
        "— check `modal endpoint list`)? Run: "
        f"make memory-deploy-model MODEL={entry.repo_id}"
    )

    try:
        server = modal.Server.from_name(entry.app_name, MODAL_SERVER_NAME)
        url = await server.get_url.aio()
    except Exception as exc:  # noqa: BLE001 — every lookup failure is the same operator action
        raise ModelError(failure) from exc

    if not url:
        raise ModelError(failure)
    return url.rstrip("/").removesuffix("/v1").rstrip("/")


async def served_model_id(url: str, bearer: str, default: str) -> str:
    """The model id the server answers to, discovered from ``/v1/models``.

    We do not control the name a managed recipe serves under (custom weights
    especially) and vLLM rejects an unknown ``model``, so the id is read from
    the server. Best-effort: a failing call or an empty list falls back to
    ``default`` with a WARNING, because the embeddings POST is where a wrong
    name surfaces loudly anyway.
    """

    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {bearer}"}
        ) as session:
            async with session.get(f"{url}/v1/models") as response:
                if response.status != 200:
                    logger.warning(
                        "GET %s/v1/models answered %d — using %s as the model id.",
                        url,
                        response.status,
                        default,
                    )
                    return default
                payload = await response.json()
    except Exception as exc:  # noqa: BLE001 — discovery is best-effort by design
        logger.warning(
            "Could not read %s/v1/models (%s) — using %s as the model id.",
            url,
            exc,
            default,
        )
        return default

    served = [item.get("id") for item in payload.get("data", []) if item.get("id")]
    if not served:
        logger.warning(
            "GET %s/v1/models listed no model — using %s as the model id.",
            url,
            default,
        )
        return default
    return served[0]


async def smoke_test(model: str, deadline_s: float | None = None) -> SmokeTestReport:
    """Prove one served model end-to-end, on whatever path serves it.

    Health through proxy auth, the DISCOVERED model id, three prompted inputs,
    the native vector width, a relevant-vs-unrelated ordering, the same
    ordering at the truncated width the memory stores, and a 401 without the
    token.

    The health check is the SAME poller the clients use (ADR-009 §11), so a
    smoke test right after a deploy waits the cold start out instead of failing
    in a second on the 503 a scaled-to-zero server answers. ``deadline_s``
    defaults to ``modal.warmup_deadline_s``; a first-ever deploy that also
    downloads weights gets a longer budget for ONE command with
    ``TREE_MODAL__WARMUP_DEADLINE_S=1200``.

    Raises:
        ModelError: any assertion failed (the message carries the numbers), or
            the health poll failed on something waiting cannot fix.
        ExtractionError: the server was unreachable, or still cold when the
            budget ran out.
    """

    entry = get_embedding_entry(model)
    bearer = modal_proxy_bearer()

    url = await resolve_server_url(entry)
    elapsed = await poll_health(
        f"{url}/health",
        {"Authorization": f"Bearer {bearer}"},
        deadline_s=(
            app_config.modal.warmup_deadline_s if deadline_s is None else deadline_s
        ),
    )
    logger.info("health 200 after %.1fs", elapsed)

    served = await served_model_id(url, bearer, entry.repo_id)
    logger.info("served model id: %s", served)

    texts = [
        prompt_for(entry, "query") + SMOKE_QUERY,
        prompt_for(entry, "document") + SMOKE_RELEVANT,
        prompt_for(entry, "document") + SMOKE_UNRELATED,
    ]
    vectors = await _embed(url, bearer, served, texts)

    for vector in vectors:
        if len(vector) != entry.native_dimensions:
            raise ModelError(
                f"expected {entry.native_dimensions} dims, got {len(vector)} — "
                "the Modal catalog's native_dimensions is wrong for this "
                "Serving path, or the served architecture dropped the model's "
                "projection head"
            )
    logger.info("%d embeddings, %d dims", len(vectors), entry.native_dimensions)

    cos_relevant, cos_unrelated = _rank(vectors)
    if cos_relevant <= cos_unrelated:
        raise ModelError(
            f"sanity check failed: cos(query, relevant)={cos_relevant:.2f} <= "
            f"cos(query, unrelated)={cos_unrelated:.2f} — the served "
            "architecture or pooling is probably wrong"
        )
    logger.info(
        "sanity: cos(query, relevant)=%.2f > cos(query, unrelated)=%.2f",
        cos_relevant,
        cos_unrelated,
    )

    truncated_dimensions = _truncation_target(entry)
    if truncated_dimensions is not None:
        _check_truncation(vectors, entry.native_dimensions, truncated_dimensions)

    unauthenticated_status = await _unauthenticated_health_status(url)
    if unauthenticated_status != 401:
        raise ModelError(
            f"unauthenticated /health answered {unauthenticated_status}, "
            "expected 401 — the server is public"
        )
    logger.info("unauthenticated health -> %d", unauthenticated_status)
    logger.info("Smoke test passed")

    return SmokeTestReport(
        url=url,
        served_model=served,
        dimensions=entry.native_dimensions,
        truncated_dimensions=truncated_dimensions,
        cold_start_seconds=elapsed,
        cos_relevant=cos_relevant,
        cos_unrelated=cos_unrelated,
        unauthenticated_status=unauthenticated_status,
    )


async def chat_smoke_test(
    model: str, deadline_s: float | None = None
) -> ChatSmokeTestReport:
    """Prove one served LLM end-to-end, on whatever path serves it.

    The twin of :func:`smoke_test` for the LLM half of the catalog, and just as
    path-blind: an endpoint-routed ``Qwen/Qwen3.5-0.8B`` and our SGLang App
    answer the same OpenAI-compatible API through the same **Proxy token**
    auth, so nothing here branches on the route (ADR-009 §3/§10).

    Health through the SHARED poller (ADR-009 §11), the DISCOVERED model id,
    ONE chat completion in JSON mode — the request ``ModalLLM`` sends, from the
    same :data:`~tree.models.modal_catalog.CHAT_RESPONSE_FORMAT` constant and
    with the ``city_facts`` schema in the PROMPT — and a 401 without the token.

    It GATES on the client's own three verdicts and nothing more: content came
    back, it is valid JSON, and it is an object. Whether the model followed the
    prompt's SHAPE is RECORDED (one INFO line, one WARNING when it did not) —
    JSON mode promises an object, never its keys, and a gate on key-following
    would fail a deploy for a reason no redeploy fixes.

    Raises:
        ModelError: a gate failed (the message carries the numbers or the first
            characters of the content), or the health poll failed on something
            waiting cannot fix.
        ExtractionError: the server was unreachable, answered non-200, or was
            still cold when the budget ran out.
    """

    entry = get_llm_entry(model)
    bearer = modal_proxy_bearer()

    url = await resolve_server_url(entry)
    elapsed = await poll_health(
        f"{url}/health",
        {"Authorization": f"Bearer {bearer}"},
        deadline_s=(
            app_config.modal.warmup_deadline_s if deadline_s is None else deadline_s
        ),
    )
    logger.info("health 200 after %.1fs", elapsed)

    served = await served_model_id(url, bearer, entry.repo_id)
    logger.info("served model id: %s", served)

    content = await _chat(url, bearer, served, entry)
    logger.info("chat completion: %s", content)

    parsed = _json_object(content)
    keys = sorted(parsed)
    logger.info("JSON mode honoured: keys=%s", keys)

    mismatch = _prompt_schema_mismatch(parsed)
    if mismatch is not None:
        logger.warning("prompt schema not followed (recorded, not gated): %s", mismatch)

    unauthenticated_status = await _unauthenticated_health_status(url)
    if unauthenticated_status != 401:
        raise ModelError(
            f"unauthenticated /health answered {unauthenticated_status}, "
            "expected 401 — the server is public"
        )
    logger.info("unauthenticated health -> %d", unauthenticated_status)
    logger.info("Smoke test passed")

    return ChatSmokeTestReport(
        url=url,
        served_model=served,
        cold_start_seconds=elapsed,
        keys=keys,
        follows_prompt_schema=mismatch is None,
        unauthenticated_status=unauthenticated_status,
    )


async def _chat(url: str, bearer: str, served: str, entry: ModalLLMModelConfig) -> str:
    """``POST /v1/chat/completions`` in JSON mode; the content.

    The body is the ENTRY's request, not a fixed one: ``chat_request_knobs``
    adds its ``max_tokens`` and its ``chat_template_kwargs`` (ADR-009 §10),
    which is what makes this command prove the request ``ModalLLM`` will send.
    An entry that sets no budget keeps :data:`CHAT_SMOKE_MAX_TOKENS` — 256 is
    the cost bound of one smoke test, not a claim about the model.

    Bounded by ``modal.request_timeout_s`` — the SAME knob both clients use, so
    the command that proves a model can never outlast the memory that will call
    it (it had no explicit timeout at all before, and inherited ``aiohttp``'s
    300 s default by accident).

    Raises:
        ExtractionError: the transport failed, the server answered nothing
            within ``modal.request_timeout_s``, or it did not answer 200
            (``status_code`` carries it — a 400 is how a server refuses the
            request outright).
        ModelError: the 200 carried no message content, which no retry fixes.
    """

    knobs = chat_request_knobs(entry)
    body = {
        "model": served,
        "messages": [{"role": "user", "content": CHAT_SMOKE_PROMPT}],
        "max_tokens": CHAT_SMOKE_MAX_TOKENS,
        "temperature": 0,
        "response_format": CHAT_RESPONSE_FORMAT,
    } | knobs
    # Configuration, never a secret — and the line an operator reads when a
    # knob is wrong, so it goes out BEFORE the POST it describes.
    logger.info(
        "chat knobs: max_tokens=%s chat_template_kwargs=%s",
        body["max_tokens"],
        json.dumps(knobs.get("chat_template_kwargs", {})),
    )
    timeout_s = app_config.modal.request_timeout_s
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {bearer}"}
        ) as session:
            async with session.post(
                f"{url}/v1/chat/completions",
                json=body,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as response:
                status = response.status
                payload = await response.json() if status == 200 else {}
    except TimeoutError as exc:
        # BEFORE the generic wrapper: `str(TimeoutError())` is EMPTY, so the
        # line below would read "… failed: " and name neither the wait nor the
        # knob. `asyncio.TimeoutError` IS `TimeoutError` on 3.11+.
        raise ExtractionError(
            f"POST {url}/v1/chat/completions timed out after {timeout_s:g}s "
            "(modal.request_timeout_s)"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — a transport failure is retryable
        raise ExtractionError(f"POST {url}/v1/chat/completions failed: {exc}") from exc

    if status != 200:
        raise ExtractionError(
            f"POST {url}/v1/chat/completions answered {status}, expected 200.",
            status_code=status,
        )

    choices = payload.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    content = message.get("content")
    if content:
        return content

    # WHY it was empty, in one sentence the operator can act on. A server
    # started with a `--reasoning-parser` puts the thinking in
    # `reasoning_content` and leaves `content` empty when the budget ran out
    # (SGLang v0.5.18, `serving_chat.py:700`) — the live failure of tasks/141
    # round 1. Only the LENGTH of that trace is ever reported — and only when
    # the server sent TEXT: an untyped extra that comes back as a number or an
    # object has no length to report (`reasoning_length`) and no proof of
    # thinking, so it takes the plain branch. The diagnosis is an addition to
    # the sentence below, never a reason to lose it.
    reasoning_chars = reasoning_length(message.get("reasoning_content"))
    details = empty_answer_details(
        choices[0].get("finish_reason") if choices else None,
        reasoning_chars,
    )
    if reasoning_chars:
        raise ModelError(
            f"the chat completion carried no content{details} — a thinking "
            "model spent its budget reasoning: set chat_template_kwargs: "
            "{enable_thinking: false} on the entry, or raise its max_tokens"
        )
    raise ModelError(
        f"the chat completion carried no content{details} — the served model "
        "answered with an empty message"
    )


def _json_object(content: str) -> dict[str, Any]:
    """The completion as a JSON OBJECT — the client's own two verdicts.

    Exactly what :func:`tree.models.modal_llm._parsed_object` gates on, in this
    command's words: JSON mode's whole promise is an object back, and a server
    that cannot keep it is broken for ``ModalLLM``. Nothing about the object's
    KEYS is decided here (:func:`_prompt_schema_mismatch` records those).

    Raises:
        ModelError: the content is not JSON, or is JSON that is not an object.
            The message quotes the first :data:`_CONTENT_EXCERPT` characters,
            which is what tells a `<think>` block apart from a chatty preamble
            or an unbounded digit run.
    """

    excerpt = content[:_CONTENT_EXCERPT]
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise ModelError(f"chat completion is not valid JSON: {excerpt!r}") from exc

    if not isinstance(parsed, dict):
        raise ModelError(f"chat completion is not a JSON object: {excerpt!r}")
    return parsed


def _prompt_schema_mismatch(parsed: dict[str, Any]) -> str | None:
    """Why ``parsed`` is not the ``city_facts`` object the PROMPT asked for.

    ``None`` when it is: exactly the two keys, a string and an integer. The
    reason is RECORDED as one WARNING and never gates the smoke test (ADR-009
    §10) — JSON mode promises an object, never its keys, so a 350M model
    answering JSON of its own shape is a quality call for the operator, not a
    failed deploy. It never raises, and it never quotes the completion: the
    whole content is already on the ``chat completion:`` line above it.
    """

    expected = {"city", "population"}
    missing = sorted(expected - set(parsed))
    unexpected = sorted(set(parsed) - expected)
    if missing or unexpected:
        return f"missing {missing}, unexpected {unexpected}"

    city, population = parsed["city"], parsed["population"]
    if not isinstance(city, str):
        return (
            f"the completion types 'city' as {type(city).__name__}, expected a string"
        )
    # `bool` is an `int` in Python, and `true` is not a population.
    if not isinstance(population, int) or isinstance(population, bool):
        return (
            f"the completion types 'population' as {type(population).__name__}"
            ", expected an integer"
        )
    return None


async def _embed(
    url: str, bearer: str, served: str, texts: list[str]
) -> list[list[float]]:
    """``POST /v1/embeddings`` for ``texts`` — never with a ``dimensions`` key.

    vLLM answers 400 to an OpenAI ``dimensions`` parameter unless the model's
    HF config is flagged Matryoshka, and a managed recipe exposes no flag for
    it, so every Serving path is asked for the native width (ADR-009 §3).
    """

    body = {"model": served, "input": texts, "encoding_format": "float"}
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {bearer}"}
        ) as session:
            async with session.post(f"{url}/v1/embeddings", json=body) as response:
                status = response.status
                payload = await response.json() if status == 200 else {}
    except Exception as exc:  # noqa: BLE001 — a transport failure is retryable
        raise ExtractionError(f"POST {url}/v1/embeddings failed: {exc}") from exc

    if status != 200:
        raise ExtractionError(
            f"POST {url}/v1/embeddings answered {status}, expected 200.",
            status_code=status,
        )

    vectors = [item["embedding"] for item in payload.get("data", [])]
    if len(vectors) != len(texts):
        raise ModelError(
            f"expected {len(texts)} embeddings, got {len(vectors)} — the "
            "server dropped or merged inputs"
        )
    return vectors


async def _unauthenticated_health_status(url: str) -> int:
    """What ``GET {url}/health`` answers with NO ``Authorization`` header.

    Modal's edge rejects unauthenticated traffic BEFORE a GPU wakes, so 401 is
    the expected answer and anything else means the server is public.
    """

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{url}/health") as response:
                return response.status
    except Exception as exc:  # noqa: BLE001 — a transport failure is retryable
        raise ExtractionError(
            f"Unauthenticated health probe on {url}/health failed: {exc}"
        ) from exc


def _truncation_target(entry: ModalEmbeddingModelConfig) -> int | None:
    """The widest Matryoshka size BELOW the native width, or ``None``.

    ``None`` means the memory stores what the wire carries — there is nothing
    to prove about a truncation that never happens.
    """

    smaller = [
        size for size in entry.matryoshka_dimensions if size < entry.native_dimensions
    ]
    return max(smaller) if smaller else None


def _check_truncation(vectors: list[list[float]], native: int, target: int) -> None:
    """Prove the vector the memory actually stores (ADR-009 §3).

    The SAME three vectors, truncated client-side with no second request: each
    unit length at ``target``, and the ranking unchanged — the first ``target``
    components are what the 1024-d mongot index sees.

    Raises:
        ModelError: a truncated vector is the wrong width, is not unit length,
            or the ordering flipped under truncation.
    """

    truncated = [truncate_embedding(vector, target) for vector in vectors]

    for vector in truncated:
        if len(vector) != target:
            raise ModelError(
                f"expected {target} dims after truncation, got {len(vector)}"
            )
        norm = math.sqrt(sum(value * value for value in vector))
        if abs(norm - 1.0) > _NORM_TOLERANCE:
            raise ModelError(
                f"truncated norm={norm:.3f}, expected 1.0 ± {_NORM_TOLERANCE}"
            )

    logger.info(
        "truncated %d -> %d dims client-side, norm=%.3f",
        native,
        target,
        math.sqrt(sum(value * value for value in truncated[0])),
    )

    cos_relevant, cos_unrelated = _rank(truncated)
    if cos_relevant <= cos_unrelated:
        raise ModelError(
            f"sanity@{target} check failed: cos(query, relevant)="
            f"{cos_relevant:.2f} <= cos(query, unrelated)={cos_unrelated:.2f} — "
            f"the first {target} components do not carry the model's ranking, "
            f"so the {target}-d vector the memory would store is unusable"
        )
    logger.info(
        "sanity@%d: cos(query, relevant)=%.2f > cos(query, unrelated)=%.2f",
        target,
        cos_relevant,
        cos_unrelated,
    )


def _rank(vectors: list[list[float]]) -> tuple[float, float]:
    """``(cos(query, relevant), cos(query, unrelated))`` for the three vectors."""

    query, relevant, unrelated = vectors
    return _cosine(query, relevant), _cosine(query, unrelated)


def _cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity; ``0.0`` when either side is a zero vector."""

    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / (norm_left * norm_right)
