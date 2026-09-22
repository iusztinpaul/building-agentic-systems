"""Shared node-text embedding for dedup and indexing.

Four functions: ``node_to_embedding_text`` turns a knowledge-graph node ``dict``
into the GENERIC text we embed, ``entity_embedding_text`` picks the per-type text
for ONE persisted entity row (the PREFERENCE / FACT exception below, generic
otherwise), ``prospective_entity_embedding_text`` builds that persisted-row shape
for an entity that does not exist yet and hands it over, and ``embed_texts``
embeds already-built texts with the search model in as few requests as the
provider caps allow. They are separate because the indexing backfill embeds
**Child chunk**s by their **Contextual header** rather than by a node-text. Lives at
the ``memory/`` layer because both ``rag/`` and ``graph/`` depend on it (``rag/`` may
not import ``graph/`` — ADR-006).

PREFERENCE and FACT nodes must NOT be routed through the GENERIC path: they
embed ``properties.statement`` / ``properties.object`` so the supersession
resolver compares statement-to-statement (resp. object-to-object). That choice
lives in ``entity_embedding_text`` here — NOT in each caller — so the two inline
write paths (``graph/add_entity``, ``memory/pipeline``, both through
``prospective_entity_embedding_text``) and the indexing backfill
(``rag/indexing.node_embedding_text``) cannot drift into two different vectors
for the same row. Drift matters most after an **Embedding reset** (ADR-009 §7):
the backfill re-embeds EVERY preference, and on the generic text supersession
would silently stop matching.
"""

import logging
from typing import Any

from tree.entities.memory import NodeType
from tree.memory.rag.cleaning import strip_invalid_chars
from tree.models.base import BaseEmbeddingModel, EmbeddingRole
from tree.models.exceptions import ExtractionError

logger = logging.getLogger(__name__)

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
    input_type: EmbeddingRole | None = None,
    max_inputs: int = 1000,
    max_total_tokens: int = 320_000,
    max_input_tokens: int = 32_000,
) -> list[list[float]]:
    """Embed ``texts`` in as few synchronous requests as the Voyage caps allow.

    Returned vectors are positionally aligned with ``texts`` (chunks are
    contiguous and the endpoint preserves order within a request). The 429
    backoff lives inside ``.embed()``; this batcher is strictly upstream of it.
    Defaults sit at the Voyage per-request caps for ``voyage-multimodal-3``.

    ``input_type`` is the **Embedding role** (ADR-009 §5) and is forwarded
    UNCHANGED to every request this call fans out to — one batch of texts is
    one role. The default ``None`` is the symmetric case (resolution); callers
    that persist their vectors pass ``"document"``.
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
        vectors.extend(
            await _embed_chunk_resilient(
                embedding_model, texts[start:end], input_type=input_type
            )
        )
    return vectors


async def _embed_chunk_resilient(
    embedding_model: BaseEmbeddingModel,
    chunk: list[str],
    input_type: EmbeddingRole | None = None,
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

    ``input_type`` (the **Embedding role**, ADR-009 §5) rides through the
    bisect recursion unchanged: both halves of a split chunk keep the role the
    caller asked for, so a 400 can never downgrade part of a batch to a
    different embedding space.
    """

    try:
        # ADR-002 §1: the shared ``voyage-embeddings`` rate limit lives at the
        # real network POST inside the Voyage provider clients, NOT here. Routing
        # the inline dedup embed through this function still earns the Voyage-400
        # bisect-and-skip resilience, but a ``_CachedSingleEmbedding`` cache hit
        # (extraction hot path) never reaches a Voyage client, so it acquires no
        # slot — that was the timeout this relocation fixes.
        return await embedding_model.embed(chunk, input_type=input_type)
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
        left = await _embed_chunk_resilient(
            embedding_model, chunk[:mid], input_type=input_type
        )
        right = await _embed_chunk_resilient(
            embedding_model, chunk[mid:], input_type=input_type
        )
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
    return strip_invalid_chars("\n".join(parts))


def entity_embedding_text(node: dict[str, Any]) -> str:
    """The text ONE entity row is embedded on — the per-type choice, in ONE place.

    ``preference`` embeds ``properties.statement`` and ``fact`` embeds
    ``properties.object`` (``object_`` on rows written before the rename), so
    supersession's statement<->statement (resp. object<->object) comparison stays
    apples-to-apples. ``object`` wins whenever the key is PRESENT and not ``None``
    — the legacy ``object_`` is read only when ``object`` is absent. A present but
    blank (or non-string, or all-invalid-character) ``object`` therefore falls back
    to :func:`node_to_embedding_text`, never to the ``object_`` of the same row: a
    row written across the rename carries the STALE pre-rename value there, and
    embedding it would silently put the row in the wrong place in vector space.
    The special text is sanitized with
    :func:`~tree.memory.rag.cleaning.strip_invalid_chars` exactly like the generic
    path — Voyage 400s on control characters and lone surrogates, and sanitizing
    HERE (not in each caller) keeps all three call sites on the same bytes, so the
    ``_CachedSingleEmbedding`` key and the embedded text stay identical. Order is
    sanitize-then-``strip()``: a statement made only of invalid characters (or of
    invalid characters and whitespace) sanitizes to blank and therefore falls back
    to :func:`node_to_embedding_text`, as a blank or missing statement does — a row
    is never embedded on a blank string. Only the embedding INPUT is cleaned; the
    persisted ``properties.statement`` / ``properties.object`` are untouched.

    One embed input does NOT come through here: the supersession resolver
    (``graph/preference_supersession._maybe_supersede``) embeds the statement of
    a row it is about to write, and on a blank result it must SKIP the embed
    rather than fall back to the node-text — so it applies the same
    ``strip_invalid_chars(...).strip()`` itself. Same bytes for every non-blank
    statement, which is what keeps that vector equal to the one the backfill
    would rebuild.

    Takes the PERSISTED row shape (``type`` / ``name`` / ``canonical_name`` /
    ``properties``) because both sides must agree on it: the inline path builds
    that shape for an entity that does not exist yet, the backfill reads it
    straight out of Mongo. Chunk rows never reach here — they embed their
    **Contextual header** (``rag.indexing.node_embedding_text``).
    """

    properties = node.get("properties") or {}
    node_type = node.get("type")

    if node_type == NodeType.PREFERENCE:
        special = properties.get("statement")
    elif node_type == NodeType.FACT:
        # Presence, not truthiness: a present-but-blank ``object`` must NOT let
        # the legacy ``object_`` (the stale pre-rename value on the same row)
        # take over.
        special = (
            properties["object"]
            if properties.get("object") is not None
            else properties.get("object_")
        )
    else:
        special = None

    if isinstance(special, str):
        cleaned = strip_invalid_chars(special).strip()
        if cleaned:
            return cleaned

    return node_to_embedding_text(node)


# ``aliases`` and ``confidence`` are promoted to TOP-LEVEL columns by
# ``add_entity._upsert_node`` and never live under ``properties`` on the stored
# row, so the prospective shape strips them: otherwise the dedup-time text would
# carry properties the backfill's text (built from the stored row) does not.
_TOP_LEVEL_ONLY_PROPERTIES = frozenset({"aliases", "confidence"})


def prospective_entity_embedding_text(
    *,
    entity_type: NodeType,
    name: str,
    canonical_name: str,
    properties: dict[str, Any],
) -> str:
    """The text an entity that does not exist YET is embedded on.

    The ONE builder of the persisted-row shape for the two inline write paths:
    ``graph.add_entity`` (dedup query vector, reused verbatim as the new row's
    ``embedding``) and the pipeline's pre-computed ``embeddable_text_by_key``
    (task ④, consumed by ``_CachedSingleEmbedding`` in task ⑥). Both used to
    assemble this dict by hand and promise "byte-for-byte" agreement in a
    docstring; one function makes the promise structural — and it extends to the
    indexing backfill, which hands the STORED row to the same
    :func:`entity_embedding_text`.

    That three-way equality is what an **Embedding reset** (ADR-009 §7) rests on:
    the backfill must rebuild the exact text the inline writer used, or a reset
    moves every preference/fact vector and supersession stops matching.
    """

    node = {
        "type": entity_type.value,
        "name": name,
        "canonical_name": canonical_name,
        "properties": {
            k: v
            for k, v in (properties or {}).items()
            if k not in _TOP_LEVEL_ONLY_PROPERTIES
        },
    }
    return entity_embedding_text(node)


async def embed_texts(
    texts: list[str],
    embedding_model: BaseEmbeddingModel,
    *,
    input_type: EmbeddingRole | None = None,
    max_inputs: int | None = None,
    max_total_tokens: int | None = None,
    max_input_tokens: int | None = None,
) -> list[list[float]]:
    """Embed already-built texts under the configured per-request caps.

    The seam the indexing backfill uses, because its texts are NOT all generic
    node-texts: a **Child chunk** embeds its **Contextual header**
    (:func:`tree.memory.rag.embedding.child_embedding_text`) while an entity row
    embeds ``node_to_embedding_text``. Vectors are aligned positionally with
    ``texts``. Caps default to ``app_config.models.embedding_batch``;
    ``input_type`` is the **Embedding role** (ADR-009 §5), forwarded as-is.
    """

    if not texts:
        return []

    caps = _resolve_batch_caps(max_inputs, max_total_tokens, max_input_tokens)
    return await embed_in_batches(texts, embedding_model, input_type=input_type, **caps)


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
