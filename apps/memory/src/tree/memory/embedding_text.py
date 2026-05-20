"""Shared node-text embedding for dedup and indexing.

This module is the single source of truth for the **generic node-text**
path: turn a knowledge-graph node ``dict`` into the text we embed, and
embed a batch of such nodes with the search embedding model. It lives at
the ``memory/`` layer (rather than under ``indexing/`` or ``extraction/``)
because both subpackages depend on it and neither should import the
other.

After this refactor the indexing backfill (``indexing.core.embed_nodes``)
routes through :func:`node_to_embedding_text`; #042 migrates generic
dedup onto the same function so the two call sites cannot drift — a node
embedded at creation and the same node re-embedded by the backfill
produce identical text in, hence identical vectors out.

INTENTIONALLY SEPARATE — do NOT fold in
----------------------------------------
PREFERENCE and FACT nodes do **not** use this generic node-text path.
``extraction.pipeline._dispatch_entity_write`` embeds
``properties.statement`` (PREFERENCE) and ``properties.object`` (FACT)
instead — the #032 supersession contract requires the stored vector to
be a statement<->statement (resp. object<->object) comparison so the
supersession resolver compares apples to apples. That statement-embedding
logic stays in ``_dispatch_entity_write`` and is OUT OF SCOPE for this
module. A future maintainer must NOT "unify" the two: replacing the
statement embedding with :func:`node_to_embedding_text` would silently
break preference/fact supersession.
"""

import logging
from typing import Any

from tree.models.base import BaseEmbeddingModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Real-time request batching (#044)
# ---------------------------------------------------------------------------
#
# REJECTED ALTERNATIVE — Voyage async Batch API
# (https://docs.voyageai.com/docs/batch-inference). Do NOT "optimize" this
# onto the async Batch API. Two hard, verified reasons:
#   (a) Its submit-poll-retrieve model has up to a 12-hour completion window.
#       The extraction/dedup pipeline needs embeddings synchronously mid-flow
#       to make dedup decisions, so a 12h-latency job cannot drive it.
#   (b) The async Batch API only supports /v1/embeddings,
#       /v1/contextualizedembeddings, and /v1/rerank — NOT
#       /v1/multimodalembeddings. Our pinned model ``voyage-multimodal-3``
#       lives on /v1/multimodalembeddings, so the async Batch API cannot embed
#       with our model at all.
# Therefore "batch the embedding to speed up" == pack many texts into FEWER
# SYNCHRONOUS /v1/multimodalembeddings requests, bounded by the per-request
# caps below, preserving the 429 backoff that lives inside ``.embed()``.
#
# Authoritative Voyage per-request caps for voyage-multimodal-3
# (source: https://docs.voyageai.com/docs/multimodal-embeddings — do NOT
# re-derive): max 1,000 inputs; each input ≤ 32,000 tokens; total across all
# inputs ≤ 320,000 tokens.

# Conservative chars-per-token heuristic. Voyage does not expose its exact
# tokenizer over the REST endpoint, and we only need a CONSERVATIVE bound that
# keeps a request under the API caps — not exact tokenization. English text is
# roughly ~4 chars/token; we use 3 (i.e. assume MORE tokens per char than
# reality) so our token estimate over-counts and we err on the side of smaller,
# safe requests. The model already sends ``truncation=True``, so a single input
# that exceeds the per-input cap is truncated server-side rather than 400-ing;
# this estimate only governs how the batcher packs texts into requests.
_CHARS_PER_TOKEN: float = 3.0


def estimate_tokens(text: str) -> int:
    """Conservative token-count estimate for ``text``.

    Uses a chars/``_CHARS_PER_TOKEN`` heuristic that OVER-counts tokens (3
    chars/token vs. the ~4 typical for English) so the batcher stays safely
    under the Voyage per-request token caps. Always at least ``1`` for a
    non-empty text so an input is never accounted as free. Not exact Voyage
    tokenization — see the module-level note on why a conservative bound is
    sufficient.
    """

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
    """Greedily group ``texts`` into ``(start, end)`` slices that respect the
    Voyage per-request caps.

    Each returned slice satisfies BOTH the ``max_inputs`` count cap AND the
    ``max_total_tokens`` total-token cap. A single text whose estimated tokens
    exceed ``max_input_tokens`` is CLAMPED to ``max_input_tokens`` for
    accounting purposes (the model truncates it server-side via
    ``truncation=True``) so one oversized text alone still forms a valid
    single-input request rather than blocking the batcher.

    Order is preserved: slices are contiguous and cover ``texts`` left to
    right, so concatenating per-slice results restores input order.
    """

    chunks: list[tuple[int, int]] = []
    start = 0
    cur_count = 0
    cur_tokens = 0
    for i, text in enumerate(texts):
        # Clamp a single oversized input to the per-input cap. The model's
        # truncation=True handles the actual truncation; we just bound the
        # token ACCOUNTING so one huge text doesn't overflow the total cap.
        tokens = min(estimate_tokens(text), max_input_tokens)

        would_exceed_count = cur_count + 1 > max_inputs
        # Only roll over on the token cap when the current chunk is non-empty —
        # a single text that alone exceeds the total cap (already clamped to
        # max_input_tokens ≤ max_total_tokens by config) must still go out.
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

    Greedily packs ``texts`` into request-sized chunks that respect BOTH the
    ``max_inputs`` count cap AND the ``max_total_tokens`` total-token cap, never
    accounting a single input above ``max_input_tokens`` (the model truncates
    oversized inputs server-side via ``truncation=True``). Calls
    ``embedding_model.embed(chunk)`` ONCE per chunk and concatenates the
    per-chunk results, so the returned vectors are positionally aligned with
    ``texts`` — both within a request (the multimodal endpoint preserves order)
    and across requests (chunks are contiguous, left to right).

    The per-request 429 exponential-backoff lives INSIDE ``.embed()`` (see
    :class:`tree.models.voyage_multimodal_embedding.VoyageMultimodalEmbeddingModel`);
    this batcher is strictly upstream of it and does not touch the retry loop.

    Defaults sit at the authoritative Voyage per-request caps for
    ``voyage-multimodal-3``. An empty input yields an empty list without
    calling the model.
    """

    if not texts:
        return []

    chunks = _chunk_indices_by_caps(
        texts,
        max_inputs=max_inputs,
        max_total_tokens=max_total_tokens,
        max_input_tokens=max_input_tokens,
    )
    logger.info(
        "embed_in_batches: %d texts -> %d request(s) "
        "(max_inputs=%d, max_total_tokens=%d)",
        len(texts),
        len(chunks),
        max_inputs,
        max_total_tokens,
    )

    vectors: list[list[float]] = []
    for start, end in chunks:
        vectors.extend(await embedding_model.embed(texts[start:end]))
    return vectors


def node_to_embedding_text(node: dict[str, Any]) -> str:
    """Build an embeddable text representation from a node document.

    Uses the node's surface ``name`` (or ``canonical_name`` if absent) as
    the headline token rather than ``_id``. Post-Phase-1 every ``_id``
    starts with ``"{user_id}:"`` — a 24-char ObjectId hex prefix that is
    constant per tenant and adds no semantic value (we already filter
    ``$vectorSearch`` by ``user_id`` server-side). Falling back to the
    ``_id`` only when both name fields are missing preserves backward
    compatibility with legacy rows.

    Layout (preserved verbatim from the pre-refactor
    ``indexing.core._node_to_text``): ``"{type}: {headline}"`` first, then
    one ``"{key}: {value}"`` line per non-empty, non-``content`` property,
    then the ``content`` value last (it may be long).

    This is the GENERIC node-text builder. PREFERENCE/FACT nodes embed
    ``statement``/``object`` elsewhere (see the module docstring) and must
    not be routed through here.
    """

    headline = node.get("name") or node.get("canonical_name") or node.get("_id", "")
    parts = [f"{node.get('type', '')}: {headline}"]
    props = node.get("properties", {})
    for key, value in props.items():
        if value and key != "content":
            parts.append(f"{key}: {value}")
    # Include content last (may be long).
    if props.get("content"):
        parts.append(str(props["content"]))
    return "\n".join(parts)


async def embed_node_texts(
    nodes: list[dict[str, Any]],
    embedding_model: BaseEmbeddingModel,
    *,
    max_inputs: int | None = None,
    max_total_tokens: int | None = None,
    max_input_tokens: int | None = None,
) -> list[list[float]]:
    """Embed a list of node documents via their generic node-text.

    Maps each node through :func:`node_to_embedding_text` and routes the
    texts through :func:`embed_in_batches` (#044) so a large set is packed
    into as few synchronous requests as the Voyage per-request caps allow
    instead of one giant call that would 400 on the 1000-input / 320K-token
    limit. The returned vectors are aligned positionally with ``nodes``. An
    empty input yields an empty list without calling the model.

    The batch caps default to the YAML
    ``app_config.models.embedding_batch`` values (which sit at the Voyage
    caps); pass explicit caps to override (e.g. tests, or a caller that
    already read the config).
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

    Imported lazily so this module stays importable without a loaded config
    (mirrors the rest of the codebase's config-access discipline) and so the
    caps reflect any env-var override applied since import.
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
