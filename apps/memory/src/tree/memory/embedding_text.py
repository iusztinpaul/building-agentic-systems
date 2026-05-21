"""Shared node-text embedding for dedup and indexing.

Turns a knowledge-graph node ``dict`` into the text we embed and embeds a
batch of such nodes with the search model. Lives at the ``memory/`` layer
because both ``indexing/`` and ``extraction/`` depend on it.

PREFERENCE and FACT nodes must NOT be routed through this generic path:
``extraction.pipeline._dispatch_entity_write`` embeds
``properties.statement`` / ``properties.object`` instead, so the
supersession resolver compares statement-to-statement (resp.
object-to-object). Unifying them would silently break supersession.
"""

import logging
import re
from typing import Any

from tree.models.base import BaseEmbeddingModel
from tree.models.exceptions import ExtractionError

logger = logging.getLogger(__name__)

# Voyage's embeddings endpoint 400s on control characters and unpaired
# surrogates (common in HTML->markdown-scraped chunk content). Strip the C0
# controls (except tab/newline/carriage-return), DEL + C1 range, and the
# surrogate range before embedding so one bad chunk can't fail the whole batch.
_INVALID_EMBED_CHARS_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]"
)


def _sanitize_for_embedding(text: str) -> str:
    return _INVALID_EMBED_CHARS_RE.sub("", text)


# Batching packs many texts into fewer synchronous /v1/multimodalembeddings
# requests, bounded by the per-request caps below (Voyage voyage-multimodal-3:
# max 1,000 inputs; each input ≤ 32,000 tokens; ≤ 320,000 tokens total). Not
# Voyage's async Batch API: it has a ~12h completion window (too slow for
# mid-flow dedup) and doesn't support /v1/multimodalembeddings (our endpoint).

# Conservative chars/token bound (3 vs the ~4 typical for English) so the
# estimate over-counts and requests stay under the Voyage token cap. The model
# sends truncation=True, so an oversized input is truncated server-side.
_CHARS_PER_TOKEN: float = 3.0


def estimate_tokens(text: str) -> int:
    """Conservative (over-counting) token estimate; keeps batches under the cap."""

    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN) + 1)


def _chunk_indices_by_caps(
    texts: list[str],
    *,
    max_inputs: int,
    max_total_tokens: int,
    max_input_tokens: int,
) -> list[tuple[int, int]]:
    """Greedily group ``texts`` into contiguous ``(start, end)`` slices that
    each respect both the count and total-token caps.

    Contiguous + left-to-right, so concatenating per-slice results restores
    input order. A single oversized text is clamped to ``max_input_tokens`` for
    accounting (the model truncates it server-side) so it still forms a valid
    single-input request rather than blocking the batcher.
    """

    chunks: list[tuple[int, int]] = []
    start = 0
    cur_count = 0
    cur_tokens = 0
    for i, text in enumerate(texts):
        tokens = min(estimate_tokens(text), max_input_tokens)

        would_exceed_count = cur_count + 1 > max_inputs
        # Roll over on the token cap only when the chunk is non-empty, so a lone
        # oversized text still goes out as its own request.
        would_exceed_tokens = cur_count > 0 and cur_tokens + tokens > max_total_tokens

        if would_exceed_count or would_exceed_tokens:
            chunks.append((start, i))
            start = i
            cur_count = 0
            cur_tokens = 0

        cur_count += 1
        cur_tokens += tokens

    if cur_count > 0:
        chunks.append((start, len(texts)))
    return chunks


async def embed_in_batches(
    texts: list[str],
    embedding_model: BaseEmbeddingModel,
    *,
    max_inputs: int = 1000,
    max_total_tokens: int = 320_000,
    max_input_tokens: int = 32_000,
) -> list[list[float]]:
    """Embed ``texts`` in as few synchronous requests as the Voyage caps allow.

    Returned vectors are positionally aligned with ``texts`` (chunks are
    contiguous and the endpoint preserves order within a request). The 429
    backoff lives inside ``.embed()``; this batcher is strictly upstream of it.
    Defaults sit at the Voyage per-request caps for ``voyage-multimodal-3``.
    """

    if not texts:
        return []

    chunks = _chunk_indices_by_caps(
        texts,
        max_inputs=max_inputs,
        max_total_tokens=max_total_tokens,
        max_input_tokens=max_input_tokens,
    )

    # ADR-002 §1: ``dispatch_concurrency`` is the local fan-out seam. Default 1
    # keeps dispatch strictly sequential (today's exact request count + order);
    # the cross-flow ``voyage-embeddings`` GCL is the real throttle. The knob is
    # flipped >1 only after the Voyage cap is lifted — do NOT default it higher.
    from tree.config.app_config import app_config

    dispatch_concurrency = app_config.models.embedding_batch.dispatch_concurrency
    logger.info(
        "embed_in_batches: %d texts -> %d request(s) "
        "(max_inputs=%d, max_total_tokens=%d, dispatch_concurrency=%d)",
        len(texts),
        len(chunks),
        max_inputs,
        max_total_tokens,
        dispatch_concurrency,
    )

    vectors: list[list[float]] = []
    for start, end in chunks:
        vectors.extend(await _embed_chunk_resilient(embedding_model, texts[start:end]))
    return vectors


async def _embed_chunk_resilient(
    embedding_model: BaseEmbeddingModel, chunk: list[str]
) -> list[list[float]]:
    """Embed one request's chunk, skipping inputs Voyage rejects as content.

    On an HTTP 400 ("invalid elements / unsupported tokens" — a poison input
    that no retry fixes) the chunk is bisected to isolate the offending text(s),
    which are skipped with an aligned empty-vector ``[]`` placeholder so one bad
    chunk can't fail the whole run. Rate-limit (429) and server (5xx) errors
    propagate untouched — they are transient and must not silently drop data.

    The skip-vs-reraise decision keys off the exception's structured
    ``status_code`` (``ExtractionError.status_code``), NOT a substring of the
    human-readable message. The Voyage client interpolates the server response
    body verbatim into 429/5xx messages, so a transient error whose body merely
    contains the digit-run "400" (a token count, a ``Retry-After``, a request
    id) must never be misread as a content rejection and silently skipped.
    """

    try:
        # ADR-002 §1: the shared ``voyage-embeddings`` rate limit lives at the
        # real network POST inside the Voyage provider clients, NOT here. Routing
        # the inline dedup embed through this function still earns the Voyage-400
        # bisect-and-skip resilience, but a ``_CachedSingleEmbedding`` cache hit
        # (extraction hot path) never reaches a Voyage client, so it acquires no
        # slot — that was the timeout this relocation fixes.
        return await embedding_model.embed(chunk)
    except ExtractionError as exc:
        # Only a structured HTTP 400 is a content rejection we skip; everything
        # else (429, 5xx, or a status-less ExtractionError) is transient/unknown
        # and must re-raise — never silently drop data.
        if getattr(exc, "status_code", None) != 400:
            raise
        if len(chunk) <= 1:
            logger.warning(
                "skipping un-embeddable input (Voyage 400): %.120r", chunk[0]
            )
            return [[]]
        mid = len(chunk) // 2
        left = await _embed_chunk_resilient(embedding_model, chunk[:mid])
        right = await _embed_chunk_resilient(embedding_model, chunk[mid:])
        return left + right


def node_to_embedding_text(node: dict[str, Any]) -> str:
    """Build an embeddable text representation from a node document.

    Headlines on ``name`` (or ``canonical_name``), falling back to ``_id`` only
    when both are missing — the ``_id``'s ``"{user_id}:"`` prefix is constant
    per tenant and adds no semantic value. Layout: ``"{type}: {headline}"``,
    then one ``"{key}: {value}"`` line per non-``content`` property, then
    ``content`` last. Generic path only — PREFERENCE/FACT embed elsewhere.
    """

    headline = node.get("name") or node.get("canonical_name") or node.get("_id", "")
    parts = [f"{node.get('type', '')}: {headline}"]
    props = node.get("properties", {})
    for key, value in props.items():
        if value and key != "content":
            parts.append(f"{key}: {value}")
    if props.get("content"):
        parts.append(str(props["content"]))
    return _sanitize_for_embedding("\n".join(parts))


async def embed_node_texts(
    nodes: list[dict[str, Any]],
    embedding_model: BaseEmbeddingModel,
    *,
    max_inputs: int | None = None,
    max_total_tokens: int | None = None,
    max_input_tokens: int | None = None,
) -> list[list[float]]:
    """Embed a list of node documents via their generic node-text.

    Vectors are aligned positionally with ``nodes``. Caps default to
    ``app_config.models.embedding_batch``; pass explicit caps to override.
    """

    if not nodes:
        return []

    caps = _resolve_batch_caps(max_inputs, max_total_tokens, max_input_tokens)
    texts = [node_to_embedding_text(node) for node in nodes]
    return await embed_in_batches(texts, embedding_model, **caps)


def _resolve_batch_caps(
    max_inputs: int | None,
    max_total_tokens: int | None,
    max_input_tokens: int | None,
) -> dict[str, int]:
    """Fill any unspecified batch cap from ``app_config.models.embedding_batch``.

    Imported lazily so caps reflect any env-var override applied since import.
    """

    from tree.config.app_config import app_config

    batch_cfg = app_config.models.embedding_batch
    return {
        "max_inputs": max_inputs if max_inputs is not None else batch_cfg.max_inputs,
        "max_total_tokens": (
            max_total_tokens
            if max_total_tokens is not None
            else batch_cfg.max_total_tokens
        ),
        "max_input_tokens": (
            max_input_tokens
            if max_input_tokens is not None
            else batch_cfg.max_input_tokens
        ),
    }
