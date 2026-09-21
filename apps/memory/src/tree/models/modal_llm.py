"""The client for ONE **Modal catalog** LLM, on either **Serving path**
(ADR-009 §10).

The LLM twin of :class:`~tree.models.modal_embedding.ModalEmbeddingModel`:
same catalog lookup, same **Proxy token** auth, same **Warm gate** — only the
OpenAI-compatible surface differs (``/v1/chat/completions`` instead of
``/v1/embeddings``). A **Dedicated endpoint** and our own SGLang App expose the
same app name and the same ``Server`` class, so this client never learns which
one answered (H1, ADR-009 §3).

**JSON mode, not a schema.** :meth:`BaseLLM.generate_json` carries no schema —
every caller embeds its own in the prompt — so the default is
``response_format={"type": "json_object"}``, the exact counterpart of
:class:`~tree.models.gemini.GeminiLLM`'s
``response_mime_type="application/json"``. SGLang honours it with REAL
constrained decoding: ``ResponseFormat.type`` is
``Literal["text", "json_object", "json_schema"]`` and a ``json_object`` request
becomes ``sampling_params["json_schema"] = '{"type": "object"}'``
(``python/sglang/srt/entrypoints/openai/protocol.py`` at ``v0.5.18``, :230 and
:1068-1069, read 2026-09-20) — so no permissive-schema fallback is needed. The
optional ``schema=`` keyword (this class only, never on ``BaseLLM``) sends a
STRICT ``json_schema`` instead, for the callers that do have one.

**Failures mirror Gemini's**, with ``Modal LLM`` in place of ``Gemini``, so the
pipeline's existing handling of a chatty small model is unchanged; each one
also carries the HTTP ``status_code`` when the SDK knew it, because a caller
that branches must branch on a number, never on an interpolated message.

Never LOGGED: the **Proxy token**, the prompt, the completion — the ready line
and the gate's own lines are the whole story this module writes to the logger.
The token is never anywhere at all: not in a log record, not in an exception,
not in a ``repr``. The prompt and the completion DO reach an Opik span when
Opik is configured, because ``@track`` records the decorated call's inputs and
output — exactly what :func:`tree.observability.track_genai_client` already
does for Gemini. That is provider parity and telemetry the operator opts into,
not a log leak.
"""

import json
import logging
from typing import Any

import openai
from openai import AsyncOpenAI

from tree.config.app_config import ModalLLMModelConfig, app_config
from tree.models.base import BaseLLM
from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_catalog import (
    MODAL_SERVER_NAME,
    chat_request_knobs,
    empty_answer_details,
    get_llm_entry,
    reasoning_length,
)
from tree.models.modal_server import resolve_server_url, served_model_id
from tree.models.modal_warmup import WarmGate, poll_health
from tree.observability import track, update_current_span

logger = logging.getLogger(__name__)

# How much of a bad completion the failure message carries — the same excerpt
# the chat smoke test quotes: enough to recognise a `<think>` block or a
# "Sure! Here is the JSON" preamble, short enough to stay one terminal line.
_CONTENT_EXCERPT = 200

# The name of the strict schema an optional ``schema=`` is sent under. OpenAI
# requires a name; nothing reads it back, so ONE constant beats a parameter.
_SCHEMA_NAME = "response"


def _response_format(schema: dict[str, Any] | None) -> dict[str, Any]:
    """JSON mode, or STRICT JSON-schema mode when the caller has a schema.

    ``{"type": "json_object"}`` is what a ``BaseLLM`` call can ask for: the
    contract has no schema parameter, so the shape lives in the prompt.
    ``schema`` — an extra, Liskov-safe keyword only this class has — upgrades
    the same request to constrained decoding against that schema.
    """

    if schema is None:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {"name": _SCHEMA_NAME, "strict": True, "schema": schema},
    }


def _knob_kwargs(entry: ModalLLMModelConfig) -> dict[str, Any]:
    """The entry's optional request knobs, as ``create(...)`` keywords.

    The SAME fields the chat smoke test POSTs (ADR-009 §10), rendered for the
    OpenAI SDK: ``max_tokens`` is a parameter it has, ``chat_template_kwargs``
    is not — so it travels as ``extra_body``, which the SDK MERGES into the
    top-level JSON body (``openai`` 2.28.0, ``_base_client.py:500-508``)
    rather than nesting it. That is what makes the two wire requests equal.

    Empty for an entry that sets neither: the ``create`` call then carries
    exactly the four keywords it carried before the knobs existed.
    """

    knobs = chat_request_knobs(entry)
    kwargs: dict[str, Any] = {}
    if "max_tokens" in knobs:
        kwargs["max_tokens"] = knobs["max_tokens"]
    if "chat_template_kwargs" in knobs:
        kwargs["extra_body"] = {"chat_template_kwargs": knobs["chat_template_kwargs"]}
    return kwargs


def _status_code(exc: Exception) -> int | None:
    """The HTTP status the SDK attached, or ``None``.

    The same predicate :mod:`tree.models.modal_warmup` classifies cold calls
    with: only an ``APIStatusError`` really carries a status, so a stray
    ``status_code`` attribute on some other exception can never reach the field
    callers branch on.
    """

    return exc.status_code if isinstance(exc, openai.APIStatusError) else None


def _record_modal_usage(model: str, response: object) -> None:
    """Record Modal chat token usage on the current Opik span.

    Self-hosted, so ``total_cost=0`` — tokens yes, dollars no, the same rule
    :func:`tree.models.modal_embedding._record_modal_usage` follows. A response
    without a ``usage`` object records no ``usage`` key at all rather than three
    zeroes, which would read as "this call was free" instead of "unknown".

    ``model`` is the catalog ``repo_id``, never the discovered served id: the id
    a managed recipe happens to serve under is an implementation detail, while
    the repo id is what the YAML and the cost tables name.

    Fully exception-safe — telemetry must never break a completion.
    """

    try:
        span: dict[str, Any] = {
            "provider": "modal",
            "model": model,
            "total_cost": 0.0,
        }
        usage = getattr(response, "usage", None)
        if usage is not None:
            span["usage"] = {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            }
        update_current_span(**span)
    except Exception as exc:  # noqa: BLE001 — telemetry must never break a call
        logger.debug("Opik Modal LLM usage recording no-op: %s", exc)


def _content(response: Any) -> str:
    """The message content of the first choice, or a loud empty-response error.

    Raises:
        ExtractionError: the server answered 200 with nothing to parse. A
            reasoning model that spent its whole budget thinking lands here —
            the message IS the diagnosis, and it still STARTS with the sentence
            ``GeminiLLM`` gives, so a caller matching on that keeps working.
            What the server said about WHY is appended: ``finish_reason`` and
            the LENGTH of ``reasoning_content`` — never a character of the
            reasoning itself. ``reasoning_content`` is not an OpenAI field, but
            the SDK's models allow extras (``openai`` 2.28.0, ``_models.py``
            ``extra="allow"``), so a server that separates reasoning is read
            here without a cast — and, for the same reason, without a
            guarantee that it is text: a non-string has no reported length
            (:func:`~tree.models.modal_catalog.reasoning_length`) and the
            sentence then names ``finish_reason`` alone.
    """

    choices = response.choices or []
    content = choices[0].message.content if choices else None
    if content:
        return content

    choice = choices[0] if choices else None
    message = getattr(choice, "message", None)
    reasoning_chars = reasoning_length(getattr(message, "reasoning_content", None))
    details = empty_answer_details(
        getattr(choice, "finish_reason", None), reasoning_chars
    )
    raise ExtractionError(f"Modal LLM returned an empty response{details}")


def _parsed_object(content: str) -> dict[str, Any]:
    """``content`` as a JSON OBJECT, or the failure that says which it was.

    Raises:
        ExtractionError: the content is not valid JSON (a small model answered
            with prose), or it is valid JSON that is not an object (a list, a
            string) — which every caller of ``generate_json`` would index into
            as a dict.
    """

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ExtractionError(
            f"Modal LLM returned invalid JSON: {content[:_CONTENT_EXCERPT]}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ExtractionError(
            f"Modal LLM returned JSON that is not an object: {type(parsed).__name__}"
        )
    return parsed


class ModalLLM(BaseLLM):
    """LLM served on Modal, resolved from the **Modal catalog** (ADR-009 §10).

    Construction is offline and total: a missing **Proxy token**, an unknown
    model id and an id that names an EMBEDDING entry all fail HERE, before a
    single network call — so a typo in ``models.llm.model`` costs no GPU boot.
    The first ``generate_json()`` then resolves the URL, polls the container
    warm through proxy auth and discovers the served model id once; every later
    call reuses them until one answers cold.
    """

    def __init__(
        self,
        proxy_token: str,
        model: str,
        warmup_deadline_s: float | None = None,
    ) -> None:
        if not proxy_token:
            raise ModelError(
                "Modal proxy token is required. Set MODAL_PROXY_TOKEN_ID and "
                "MODAL_PROXY_TOKEN_SECRET."
            )

        self._proxy_token = proxy_token
        self._entry = get_llm_entry(model)
        # Read at CONSTRUCTION, like the embedding client: one budget per
        # server (ADR-009 §11), overridable per process with
        # TREE_MODAL__WARMUP_DEADLINE_S.
        self._warmup_deadline_s = (
            app_config.modal.warmup_deadline_s
            if warmup_deadline_s is None
            else warmup_deadline_s
        )
        # The OTHER wait, read at construction the same way: that one waits for
        # a COLD server, this one bounds ONE answer from a living one
        # (TREE_MODAL__REQUEST_TIMEOUT_S).
        self._request_timeout_s = app_config.modal.request_timeout_s
        # Replaced by the id discovered from /v1/models on the first warm; the
        # repo id is the fallback the discovery itself already applies.
        self._served_model = self._entry.repo_id
        self._client: AsyncOpenAI | None = None
        # COMPOSED, not inherited, and not shared with ``ModalEmbeddingModel``
        # through a base class (ADR-009 §11): two ~15-line `_warm` bodies are
        # cheaper than a mixin, and the gate stays testable on its own.
        self._gate = WarmGate(self._warm, label=self._entry.app_name)

    @property
    def warm_key(self) -> str:
        """What identifies the SERVER this model talks to (its Modal app).

        The **Pre-warm** warms every DISTINCT Modal app once, so an LLM and an
        embedding model that happen to share an app share a boot, and two
        instances of the same model share a server even though they never share
        a gate.
        """

        return self._entry.app_name

    async def ensure_warm(self) -> None:
        """Wait until this model's Modal server answers — at most once per
        warm period.

        Public because the **Pre-warm** awaits it directly (duck-typed, so a
        Gemini LLM simply has no such method). Nothing is marked warm until the
        whole body succeeded, so a failed warm is retried in full rather than
        prompting a dead server.
        """

        await self._gate.ensure_warm()

    async def _warm(self) -> None:
        """Resolve the server, poll it warm, discover its id, build a client.

        The gate's single-flight body, ALL-OR-NOTHING: ``_served_model`` and
        ``_client`` are assigned only after every step succeeded, so a failure
        leaves NOTHING half-built and the next call re-runs the whole sequence.

        It calls the RAW helpers only — never ``self._gate`` in any form. The
        gate's lock is a plain ``asyncio.Lock``, which is not re-entrant, so a
        warm body that went back through the gate would deadlock every first
        use (``tests/unit/models/test_modal_llm.py`` pins this).
        """

        url = await resolve_server_url(self._entry)
        # The poll sends the same `Authorization: Bearer <id>.<secret>` the
        # chat call does: Modal's edge checks it before a GPU wakes.
        await poll_health(
            f"{url}/health",
            {"Authorization": f"Bearer {self._proxy_token}"},
            deadline_s=self._warmup_deadline_s,
        )
        served = await served_model_id(
            url, self._proxy_token, default=self._entry.repo_id
        )
        # The Proxy token IS the OpenAI api_key: the client sends it as
        # `Authorization: Bearer <id>.<secret>`.
        #
        # The SDK's defaults are a 600 s timeout and TWO silent retries, so one
        # unbounded completion could hold a caller 30 minutes — and twice that
        # through the gate's re-warm (measured: 13 minutes until killed,
        # tasks/141 cycle 4d). `max_retries=0` leaves the ONE retry to the
        # gate, which is the layer that can actually fix a cold server.
        client = AsyncOpenAI(
            base_url=f"{url}/v1",
            api_key=self._proxy_token,
            timeout=self._request_timeout_s,
            max_retries=0,
        )

        self._served_model = served
        self._client = client

        logger.info(
            "ModalLLM ready: app=%s server=%s served_model=%s url=%s/v1",
            self._entry.app_name,
            MODAL_SERVER_NAME,
            served,
            url,
        )

    @track(type="llm", name="modal-generate-json")
    async def generate_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One chat completion, parsed as a JSON object.

        ``system`` becomes a leading ``system`` message (Gemini's
        ``system_instruction``); ``schema`` — the extra keyword only this class
        has — upgrades JSON mode to STRICT JSON-schema decoding. Both optional,
        so a caller holding a plain :class:`~tree.models.base.BaseLLM` is served
        by exactly the two-argument contract.

        An EMPTY (or whitespace-only) prompt is refused BEFORE the gate: there
        is no correct object to return for "generate JSON from nothing" — ``{}``
        would make an empty chunk look like a successful extraction of zero
        entities — and waking a scaled-to-zero GPU to ask nothing bills the
        operator for minutes. It is the guard
        :meth:`~tree.models.modal_embedding.ModalEmbeddingModel.embed` puts
        above its own gate, with the only answer an LLM can give.

        The call goes through the **Warm gate**: it waits out a cold start
        before the first one, and a container that idled out mid-run costs ONE
        re-warm and ONE retry instead of a failed document.

        The catalog entry's optional request knobs ride on EVERY call
        (ADR-009 §10): its ``max_tokens`` when it sets one, and its
        ``chat_template_kwargs`` — e.g. ``{"enable_thinking": false}`` for a
        thinking model — as ``extra_body``. They are the same two top-level
        fields the chat smoke test POSTs, from the same helper.

        Wrapped in an Opik ``llm``-type span; on success the token counts are
        recorded with ``total_cost=0`` (self-hosted). Recording is fail-open.

        Raises:
            ModelError: the warm failed on something waiting cannot fix (a
                wrong **Proxy token**, an undeployed model).
            ExtractionError: the prompt was empty, the call failed
                (``status_code`` carries the HTTP status when there was one),
                the call outlived ``modal.request_timeout_s`` (sent ONCE — a
                timeout is not a cold server, so nothing re-warms and nothing
                retries), the server stayed cold through one re-warm, or the
                completion was empty / not JSON / not a JSON object.
        """

        if not prompt.strip():
            raise ExtractionError("Modal LLM was given an empty prompt")

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response_format = _response_format(schema)
        # Read per CALL, not cached on the instance: an entry is config, and a
        # knob changed under a running process must reach the next request.
        knobs = _knob_kwargs(self._entry)

        try:
            # The lambda reads `_client` and `_served_model` at CALL time: a
            # re-warm replaces both, and the retry must use the new ones.
            response = await self._gate.call(
                lambda: self._client.chat.completions.create(  # type: ignore[union-attr]
                    model=self._served_model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=0,
                    response_format=response_format,  # type: ignore[arg-type]
                    **knobs,
                )
            )
        except ModelError:
            # Everything the gate raises — a bad token (ModelError), a spent
            # deadline or a server still cold after one re-warm
            # (ExtractionError, a subclass) — already says what to do. Wrapping
            # it in "call failed" would re-type a configuration error as a
            # retryable one.
            raise
        except openai.APITimeoutError as exc:
            # The server is alive and slow, so the generic "call failed: " —
            # whose `{exc}` is the SDK's bare "Request timed out." — would name
            # neither the budget that was spent nor the two ways to change the
            # outcome. `status_code` stays None: there was no HTTP answer, and
            # a caller that branches on 400 (`_embed_chunk_resilient`) must
            # never read a slow server as a poison input.
            raise ExtractionError(
                f"Modal LLM call timed out after {self._request_timeout_s:g}s "
                "(modal.request_timeout_s) — the server is alive but slow: "
                "lower the entry's max_tokens or raise "
                "TREE_MODAL__REQUEST_TIMEOUT_S",
                status_code=None,
            ) from exc
        except Exception as exc:
            raise ExtractionError(
                f"Modal LLM call failed: {exc}", status_code=_status_code(exc)
            ) from exc

        _record_modal_usage(self._entry.repo_id, response)
        # OUTSIDE the try: these three are verdicts on a 200, not call
        # failures, and they must not be swallowed by the wrapper above.
        return _parsed_object(_content(response))
