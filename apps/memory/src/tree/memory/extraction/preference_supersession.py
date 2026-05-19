"""Bi-temporal supersession resolver branch (#032).

Plugs in between the LLM-extraction step and the standard dedup
branch. For every incoming ``preference`` or ``fact`` row, it asks:

  1. Are there candidate prior rows in the same partition?
     - preference partition: ``(user_id, type, properties.category)``.
     - fact partition: ``(user_id, type, properties.subject,
       properties.predicate)``.
  2. (Post-QA #032 fix-1) The resolver no longer pre-filters
     candidates on embedding cosine - under the project's local-dev
     embedder (``sentence-transformers/all-MiniLM-L6-v2``) the cosine
     between e.g. "prefers dark mode" and "prefers light mode" is
     ~0.64, well below ``app_config.extraction.dedup.flag_threshold = 0.85``, so the
     judge was never invoked end-to-end. Instead we pull the K
     most-recent **active** candidates in the same partition (sorted
     by ``valid_from`` desc, then ``created_at`` desc) and call
     :func:`tree.memory.extraction.judge.judge_contradiction` on each
     in turn. K is bounded by
     ``app_config.extraction.dedup.supersession_candidate_cap`` (default 8) so LLM
     cost stays bounded per extraction batch. **First contradiction
     wins** - we stop judging once a contradiction is found.

When the judge fires:
  * The NEW row's ``valid_from`` is stamped to ``now``.
  * The OLD row's ``valid_until`` is set to the same ``now``.
  * A ``superseded_by`` edge is upserted: ``new -[superseded_by]-> old``.
  * The NEW row is upserted with the full ``properties`` payload and
    the typed-slot statement embedding so a queryable
    ``find_current_preferences`` result lands even before
    ``apply_writes`` runs (#032 QA fix - the original resolver only
    set bi-temporal columns and relied on a later step to populate
    ``properties``).
  * The new row is marked as "supersedes" so the downstream dedup
    branch skips this row entirely (supersession trumps dedup -
    matches ``plan.md:534``).

When the judge does NOT fire (all K candidates returned "no
contradiction"), the resolver simply falls through to the regular
same_as dedup branch (#010 / #029).

A helper :func:`write_self_has_preference_edges` is also exported -
it writes the deterministic ``has: person:self -> preference`` edges
post-LLM. The LLM never emits ``has`` (the spec keeps that edge
``llm_extractable=False``); the pipeline owns it.

A helper :func:`canonicalize_preference_names` rewrites LLM-emitted
preference ``name`` fields to a deterministic slug of
``properties.statement`` so the same statement under two runs always
produces the same ``_id``.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId

from tree.config.app_config import load_app_config
from tree.entities.knowledge_graph import (
    EdgeType,
    NodeType,
    build_edge_id,
    build_node_id,
)
from tree.memory.extraction.judge import judge_contradiction
from tree.memory.resolution.types import _normalize
from tree.memory.types import ExtractedNode, RawExtraction
from tree.models.base import BaseEmbeddingModel, BaseLLM

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"

# Maximum slug length. Mirrors PreferenceProperties.statement's
# max_length=80 with a bit of headroom; ``_id`` strings live forever so
# we want them short and stable.
_SLUG_MAX_LEN = 80


# ---------------------------------------------------------------------------
# Slugify util (#032 fix-3)
# ---------------------------------------------------------------------------


def slugify(text: str, *, max_len: int = _SLUG_MAX_LEN) -> str:
    """Return a deterministic kebab-case slug for ``text``.

    Lowercases, strips diacritics, collapses non-alphanumeric runs to a
    single ``-``, and trims to ``max_len`` characters (cutting on a
    word boundary when possible).

    Empty / whitespace-only input returns ``""``. Callers should treat
    an empty result as "no slug available".

    Examples:
        >>> slugify("Prefers DARK Mode for editors")
        'prefers-dark-mode-for-editors'
        >>> slugify("I really love café !!")
        'i-really-love-cafe'
        >>> slugify("   ")
        ''
    """

    if not text:
        return ""
    # Normalise unicode and drop combining marks (cafe -> cafe).
    normalised = unicodedata.normalize("NFKD", text)
    ascii_text = normalised.encode("ascii", "ignore").decode("ascii")
    # Lowercase + collapse any non [a-z0-9] run to a single hyphen.
    lowered = ascii_text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        return ""
    if len(slug) <= max_len:
        return slug
    # Trim to max_len on a word boundary when possible.
    trimmed = slug[:max_len].rstrip("-")
    last_dash = trimmed.rfind("-")
    if last_dash >= max_len // 2:
        return trimmed[:last_dash]
    return trimmed


def canonicalize_preference_names(raws: Iterable[RawExtraction]) -> None:
    """Rewrite every preference node's ``name`` to ``slugify(statement)``.

    Mutates each :class:`RawExtraction` in place. Runs BEFORE the
    supersession resolver so candidate / new-row IDs agree across
    runs even when the LLM drifts between e.g. ``"prefers dark mode
    for editors"`` (full sentence with spaces) and
    ``"prefers-light-mode"`` (kebab-case slug).

    If a preference's ``properties.statement`` is missing or empty,
    the node's existing ``name`` is left untouched (we fall back to
    the LLM-emitted value rather than dropping the node, since the
    envelope validator already passed it).
    """

    for raw in raws:
        for node in raw.extracted.nodes:
            if node.type != NodeType.PREFERENCE:
                continue
            statement = _preference_statement(node)
            if not statement:
                continue
            slug = slugify(statement)
            if not slug:
                continue
            node.name = slug


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass
class SupersessionDecision:
    """Outcome of the supersession-judge branch for one incoming row.

    Attributes:
        node: The originating :class:`ExtractedNode` (preference or fact).
        old_node_id: The ``_id`` of the row being superseded. ``None``
            when no supersession fired.
        old_statement: The OLD row's canonical statement (or fact
            object). Stored on the decision for audit / logging.
        new_node_id: The deterministic ``_id`` of the incoming row.
            Set when supersession fires so the dedup branch can be
            skipped (same key) downstream.
        valid_from: Timestamp stamped on the incoming row.
        valid_until: Timestamp stamped on the OLD row.
        judge_confidence: The judge's self-reported confidence.
        candidates_judged: How many candidates the judge was actually
            asked about before we either fired or fell through. Bounded
            above by ``app_config.extraction.dedup.supersession_candidate_cap``.
    """

    node: ExtractedNode
    old_node_id: str | None = None
    old_statement: str | None = None
    new_node_id: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    judge_confidence: float | None = None
    candidates_judged: int = 0

    @property
    def superseded(self) -> bool:
        """True iff a supersession fired for this row."""

        return self.old_node_id is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _preference_statement(node: ExtractedNode) -> str | None:
    """Return the canonical statement for a preference row, or None.

    Accepts both the new typed-slot shape (``properties.statement``)
    and the legacy free-form shape (``properties.content``) for
    backwards compatibility with cached LLM outputs.
    """

    if not node.properties:
        return None
    statement = node.properties.get("statement")
    if isinstance(statement, str) and statement.strip():
        return statement.strip()
    # Legacy shape fallback (pre-#032 emissions).
    content = node.properties.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return None


def _preference_category(node: ExtractedNode) -> str | None:
    """Return the preference's category slug, or ``None`` when absent."""

    if not node.properties:
        return None
    category = node.properties.get("category")
    if isinstance(category, str) and category.strip():
        return category.strip()
    return None


def _fact_object(node: ExtractedNode) -> str | None:
    """Return the fact's object string (the typed-slot key is
    ``"object"``; ``"object_"`` is the Python attribute alias)."""

    if not node.properties:
        return None
    for key in ("object", "object_"):
        value = node.properties.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _fact_subject_predicate(node: ExtractedNode) -> tuple[str, str] | None:
    """Return ``(subject, predicate)`` when both are populated."""

    if not node.properties:
        return None
    subject = node.properties.get("subject")
    predicate = node.properties.get("predicate")
    if (
        isinstance(subject, str)
        and subject.strip()
        and isinstance(predicate, str)
        and predicate.strip()
    ):
        return subject.strip(), predicate.strip()
    return None


def _candidate_sort_key(doc: dict[str, Any]) -> tuple[int, datetime]:
    """Sort candidates so the most-recent active row is judged first.

    Returns ``(has_timestamp, timestamp)``. ``has_timestamp == 1`` so
    rows with a non-null timestamp sort AFTER rows without one when
    reversed (we reverse later to make the most recent first); rows
    without any timestamp fall to the bottom of the bounded window.
    """

    # Prefer ``valid_from`` (set on the contested winner of a prior
    # supersession); fall back to ``created_at`` (set on every initial
    # insert).
    ts = doc.get("valid_from") or doc.get("created_at")
    if isinstance(ts, datetime):
        return (1, ts)
    return (0, datetime.min.replace(tzinfo=UTC))


async def _find_preference_candidates(
    *,
    database: Any,
    user_id: PydanticObjectId,
    category: str,
    exclude_id: str | None,
    cap: int,
) -> list[dict[str, Any]]:
    """Fetch up to ``cap`` CURRENT preference rows in the same category.

    "Current" means ``valid_until is None``. Superseded rows are
    intentionally invisible to the judge.

    ``exclude_id`` lets the caller skip the row that *is* the incoming
    preference (when its deterministic slug already collides with an
    existing row) so we don't ask the judge "does X contradict X".

    Returned rows are sorted **most-recent first** (by ``valid_from``,
    falling back to ``created_at``). ``cap`` bounds the candidate set
    so judge calls stay bounded per extraction batch.
    """

    cursor = database[_KG_COLLECTION].find(
        {
            "user_id": user_id,
            "kind": "node",
            "type": NodeType.PREFERENCE.value,
            "properties.category": category,
            "$or": [
                {"valid_until": {"$exists": False}},
                {"valid_until": None},
            ],
        }
    )
    docs = [doc async for doc in cursor]
    if exclude_id is not None:
        docs = [d for d in docs if d.get("_id") != exclude_id]
    docs.sort(key=_candidate_sort_key, reverse=True)
    return docs[:cap]


async def _find_fact_candidates(
    *,
    database: Any,
    user_id: PydanticObjectId,
    subject: str,
    predicate: str,
    exclude_id: str | None,
    cap: int,
) -> list[dict[str, Any]]:
    """Fetch up to ``cap`` CURRENT fact rows on the same ``(subject, predicate)``."""

    cursor = database[_KG_COLLECTION].find(
        {
            "user_id": user_id,
            "kind": "node",
            "type": NodeType.FACT.value,
            "properties.subject": subject,
            "properties.predicate": predicate,
            "$or": [
                {"valid_until": {"$exists": False}},
                {"valid_until": None},
            ],
        }
    )
    docs = [doc async for doc in cursor]
    if exclude_id is not None:
        docs = [d for d in docs if d.get("_id") != exclude_id]
    docs.sort(key=_candidate_sort_key, reverse=True)
    return docs[:cap]


def _candidate_statement(cand: dict[str, Any]) -> str | None:
    """Return the candidate row's canonical statement string.

    Falls back through the typed-slot keys preference-then-fact:
    ``properties.statement`` (preferences) → ``properties.content``
    (legacy preferences) → ``properties.object`` /
    ``properties.object_`` (facts).
    """

    cand_props = cand.get("properties") or {}
    for key in ("statement", "content", "object", "object_"):
        value = cand_props.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def _maybe_supersede(
    *,
    database: Any,
    user_id: PydanticObjectId,
    llm: BaseLLM,
    embedding_model: BaseEmbeddingModel,
    new_node: ExtractedNode,
    new_statement: str,
    candidates: list[dict[str, Any]],
    now: datetime,
) -> SupersessionDecision:
    """Run the bound-candidate-set + always-judge pipeline.

    Iterates through ``candidates`` (already capped and sorted
    most-recent-first by the caller). For each, asks
    :func:`judge_contradiction` whether the new statement contradicts
    the candidate's statement. The **first contradiction wins**: we
    stop judging immediately, write the supersession, and return.

    When no candidate is judged a contradiction, returns a
    no-supersession :class:`SupersessionDecision`. The caller's
    downstream dedup branch is unaffected.

    Returns a :class:`SupersessionDecision`. When the judge fires,
    this function ALSO writes the supersession to MongoDB:

      * upserts the NEW row at its deterministic ``_id`` with
        ``valid_from=now, valid_until=None``, the FULL property
        payload, and the new statement's embedding;
      * sets ``valid_until=now`` on the OLD row;
      * upserts the ``superseded_by`` edge.

    The "full property payload + embedding" upsert is intentionally
    redundant with the later ``apply_writes`` upsert (the standard
    extraction path runs ``add_entity`` -> ``_upsert_node`` post
    supersession). It makes the resolver self-sufficient so callers
    that wire the supersession resolver without the full pipeline
    still see a queryable preference row.
    """

    # Embed the new statement once (used both for the candidate-row
    # write and for the OLD comparison-vector debug log).
    try:
        embedded = await embedding_model.embed([new_statement])
    except Exception:  # noqa: BLE001
        logger.warning(
            "preference_supersession: failed to embed statement %r; "
            "writing supersession without embedding column",
            new_statement,
            exc_info=True,
        )
        new_vector: list[float] = []
    else:
        new_vector = embedded[0] if embedded else []

    candidates_judged = 0
    for cand in candidates:
        old_statement = _candidate_statement(cand)
        if not old_statement:
            continue
        candidates_judged += 1
        is_contradiction, judge_confidence = await judge_contradiction(
            llm=llm,
            new_statement=new_statement,
            old_statement=old_statement,
        )
        logger.info(
            "preference_supersession: judge new=%r old=%r -> "
            "is_contradiction=%s confidence=%.2f",
            new_statement,
            old_statement,
            is_contradiction,
            judge_confidence,
        )
        if not is_contradiction:
            continue

        # ------- Write the supersession (idempotent upserts) -------
        new_node_id = build_node_id(user_id, new_node.type, _normalize(new_node.name))
        old_node_id = str(cand["_id"])

        # Defensive: if the slug collapsed onto the existing row's id
        # (same statement, second emission), there is no supersession
        # to write - it's just a re-emit of the same preference.
        if new_node_id == old_node_id:
            logger.info(
                "preference_supersession: incoming row id %s == candidate id; "
                "skipping no-op supersession",
                new_node_id,
            )
            continue

        await _write_supersession(
            database=database,
            user_id=user_id,
            new_node=new_node,
            new_node_id=new_node_id,
            new_vector=new_vector,
            old_node_id=old_node_id,
            now=now,
            judge_confidence=judge_confidence,
        )

        return SupersessionDecision(
            node=new_node,
            old_node_id=old_node_id,
            old_statement=old_statement,
            new_node_id=new_node_id,
            valid_from=now,
            valid_until=now,
            judge_confidence=judge_confidence,
            candidates_judged=candidates_judged,
        )

    return SupersessionDecision(node=new_node, candidates_judged=candidates_judged)


async def _write_supersession(
    *,
    database: Any,
    user_id: PydanticObjectId,
    new_node: ExtractedNode,
    new_node_id: str,
    new_vector: list[float],
    old_node_id: str,
    now: datetime,
    judge_confidence: float,
) -> None:
    """Atomic-from-the-reader's-perspective supersession write.

    Three upserts (each ``upsert=True``, so idempotent re-runs of the
    resolver are safe):

      1. Upsert the NEW row at ``new_node_id`` with the typed-slot
         ``properties`` payload, the new statement's embedding, and
         ``valid_from=now``. The full property write means a follower
         that only calls the resolver (not ``apply_writes``) still
         sees a queryable preference row.
      2. Stamp ``valid_until=now`` on the OLD row at ``old_node_id``.
      3. Upsert the ``superseded_by`` edge ``new -> old``.
    """

    collection = database[_KG_COLLECTION]
    properties = dict(new_node.properties or {})
    set_payload: dict[str, Any] = {
        "valid_from": now,
        "valid_until": None,
        "updated_at": now,
        "properties": properties,
    }
    if new_vector:
        set_payload["embedding"] = new_vector

    await collection.update_one(
        {"_id": new_node_id},
        {
            "$setOnInsert": {
                "user_id": user_id,
                "kind": "node",
                "type": new_node.type.value,
                "name": new_node.name,
                "subtype": new_node.subtype,
                "created_at": now,
            },
            "$set": set_payload,
        },
        upsert=True,
    )
    # Stamp valid_until on the OLD row.
    await collection.update_one(
        {"_id": old_node_id},
        {"$set": {"valid_until": now, "updated_at": now}},
    )
    # Upsert the superseded_by edge: new -> old.
    edge_id = build_edge_id(new_node_id, EdgeType.SUPERSEDED_BY, old_node_id)
    await collection.update_one(
        {"_id": edge_id},
        {
            "$setOnInsert": {
                "user_id": user_id,
                "kind": "edge",
                "type": EdgeType.SUPERSEDED_BY.value,
                "source_node_id": new_node_id,
                "source_type": new_node.type.value,
                "target_node_id": old_node_id,
                "target_type": new_node.type.value,
                "sources": [],
                "created_at": now,
            },
            "$set": {
                "properties": {
                    "superseded_at": now,
                    "reason": "contradiction",
                    "judge_confidence": judge_confidence,
                },
                "updated_at": now,
            },
        },
        upsert=True,
    )


async def resolve_supersessions(
    *,
    database: Any,
    user_id: PydanticObjectId,
    llm: BaseLLM,
    embedding_model: BaseEmbeddingModel,
    raws: Iterable[RawExtraction],
    now: datetime | None = None,
) -> list[SupersessionDecision]:
    """Walk every preference / fact row across ``raws`` and apply the
    supersession resolver branch (#032 fix-1).

    For preferences, candidate-partition is
    ``(user_id, properties.category)``. For facts it is
    ``(user_id, properties.subject, properties.predicate)``.

    The cap on per-partition candidates fed to the judge is
    ``app_config.extraction.dedup.supersession_candidate_cap`` (default 8). No
    cosine pre-filter is applied - the judge is asked to decide on
    every candidate in turn, most-recent first, and the **first
    contradiction wins**.

    Mutates each :class:`RawExtraction` in place: any node whose
    decision fired a supersession is **stamped** with ``valid_from``
    on its ``properties`` so the apply-writes step writes the same
    timestamp. The node is NOT removed - the apply-writes step still
    needs to upsert the new row's full property payload. The
    duplicate ``$set valid_from`` in the apply path is a no-op (same
    value).

    Returns the per-row :class:`SupersessionDecision` list (mainly
    used by tests; the pipeline can ignore it).
    """

    now = now or datetime.now(tz=UTC)
    # Re-load the YAML config every call so test-level
    # ``TREE_EXTRACTION__DEDUP__SUPERSESSION_CANDIDATE_CAP`` overrides and
    # YAML edits are picked up without restarting the process. Per-call
    # cost is a single small ``yaml.safe_load`` of ``default.yaml``,
    # comparable to a dict-attribute read; this is not on a hot path.
    cap = load_app_config().extraction.dedup.supersession_candidate_cap
    decisions: list[SupersessionDecision] = []

    for raw in raws:
        for node in raw.extracted.nodes:
            decision: SupersessionDecision | None = None
            if node.type == NodeType.PREFERENCE:
                statement = _preference_statement(node)
                category = _preference_category(node)
                if not statement or not category:
                    continue
                new_node_id = build_node_id(user_id, node.type, _normalize(node.name))
                candidates = await _find_preference_candidates(
                    database=database,
                    user_id=user_id,
                    category=category,
                    exclude_id=new_node_id,
                    cap=cap,
                )
                if not candidates:
                    continue
                decision = await _maybe_supersede(
                    database=database,
                    user_id=user_id,
                    llm=llm,
                    embedding_model=embedding_model,
                    new_node=node,
                    new_statement=statement,
                    candidates=candidates,
                    now=now,
                )
            elif node.type == NodeType.FACT:
                sp = _fact_subject_predicate(node)
                statement = _fact_object(node)
                if sp is None or not statement:
                    continue
                subject, predicate = sp
                new_node_id = build_node_id(user_id, node.type, _normalize(node.name))
                candidates = await _find_fact_candidates(
                    database=database,
                    user_id=user_id,
                    subject=subject,
                    predicate=predicate,
                    exclude_id=new_node_id,
                    cap=cap,
                )
                if not candidates:
                    continue
                decision = await _maybe_supersede(
                    database=database,
                    user_id=user_id,
                    llm=llm,
                    embedding_model=embedding_model,
                    new_node=node,
                    new_statement=statement,
                    candidates=candidates,
                    now=now,
                )

            if decision is None or not decision.superseded:
                continue

            # The supersession-write path already stamped
            # ``valid_from`` / ``valid_until`` on the new and old rows
            # via direct ``update_one`` upserts; the downstream
            # apply-writes step uses ``$set`` on top-level columns
            # ``user_id`` / ``kind`` / ``type`` / ``name`` /
            # ``properties`` / ``updated_at`` etc. but does NOT touch
            # ``valid_from`` / ``valid_until``, so the bi-temporal
            # values written here survive the rest of the pipeline.
            decisions.append(decision)

    return decisions


# ---------------------------------------------------------------------------
# Deterministic ``has: person:self -> preference`` writer
# ---------------------------------------------------------------------------


async def write_self_has_preference_edges(
    *,
    database: Any,
    user_id: PydanticObjectId,
    raws: Iterable[RawExtraction],
    now: datetime | None = None,
) -> int:
    """Write one ``has`` edge per LLM-emitted ``preference`` row.

    The LLM is told (in the extraction prompt) NOT to emit ``has``
    edges. The pipeline owns them: every preference attaches to
    ``person:self`` via a deterministic structural ``has`` edge.

    Returns the number of edges upserted.

    Idempotent (``upsert=True``): re-running the pipeline on the same
    extraction does not duplicate the edge.
    """

    now = now or datetime.now(tz=UTC)
    self_person_id = build_node_id(user_id, NodeType.PERSON, "self")
    collection = database[_KG_COLLECTION]
    count = 0
    for raw in raws:
        for node in raw.extracted.nodes:
            if node.type != NodeType.PREFERENCE:
                continue
            if not node.name or not node.name.strip():
                continue
            preference_id = build_node_id(
                user_id, NodeType.PREFERENCE, _normalize(node.name)
            )
            edge_id = build_edge_id(self_person_id, EdgeType.HAS, preference_id)
            await collection.update_one(
                {"_id": edge_id},
                {
                    "$setOnInsert": {
                        "user_id": user_id,
                        "kind": "edge",
                        "type": EdgeType.HAS.value,
                        "source_node_id": self_person_id,
                        "source_type": NodeType.PERSON.value,
                        "target_node_id": preference_id,
                        "target_type": NodeType.PREFERENCE.value,
                        "sources": [],
                        "created_at": now,
                    },
                    "$set": {
                        "updated_at": now,
                    },
                },
                upsert=True,
            )
            count += 1
    return count
