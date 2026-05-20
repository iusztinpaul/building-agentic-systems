"""Dream-consolidation flow: incremental sweep + auto-merge / flag + audit (#051).

The dream pipeline re-runs the existing three-tier dedup across the
knowledge graph **incrementally**, catching near-duplicate nodes that
parallel ingestion's inline write-time dedup missed. The default path is
the NORMAL semantic + fuzzy dedup across ALL node types (NO LLM). The LLM
contradiction / supersession judge is OUT of this task — it lands behind
``app_config.dream.enable_supersession_judge`` in #052, which plugs into the
seam left at :func:`_supersession_sweep`. Scheduling / per-user fan-out is
also #052 (this flow registers no deployment).

THE TWO-SET RULE (the correctness crux)
---------------------------------------
The sweep iterates over ``(user_id, type)`` partitions. Within each:

* The **driving set** — the nodes we iterate over and call
  :func:`tree.memory.extraction.dedup.dedupe_entity` for — is
  **watermark-filtered**: non-tombstoned (``merged_into`` absent/null),
  embedded (non-empty ``embedding``), AND ``updated_at > last_run_at``.
* The **search space** — the ``$vectorSearch`` comparison target inside
  ``dedupe_entity`` — is the **FULL graph** (tombstone-excluded only), NOT
  watermark-filtered. We never add a watermark filter to ``dedupe_entity``'s
  pipeline.

Rationale: a node ingested in parallel must still find its OLDER twin; we
restrict which nodes DRIVE comparisons, never what they compare against.
This catches new↔old and new↔new; old↔old was checked in a prior run.

Idempotency / safety
---------------------
* The watermark advances ONLY on a successful non-dry-run (#050's
  ``record_dream_run`` with ``last_run_at = run_start``, captured BEFORE any
  processing).
* Tombstoned losers are excluded from the search space by ``dedupe_entity``'s
  ``merged_into`` filter — never re-merged.
* Pairs already carrying a SAME_AS edge (any status — ``pending`` /
  ``confirmed`` / ``rejected``) are skipped, respecting prior decisions and
  human rejections.
* ``review_duplicate(CONFIRM)`` is idempotent; a re-run is a no-op.
* ``id1 < id2`` ordering + a per-run ``seen`` set process each pair once.

Per ``CLAUDE.md`` the Prefect ``@task`` / ``@flow`` wiring is covered by
integration tests; the pure decision logic
(:func:`_collect_dream_candidates`) is unit-tested directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from beanie import PydanticObjectId
from prefect import flow, get_run_logger, task
from prefect.cache_policies import NO_CACHE
from prefect.context import get_run_context

from tree.config.app_config import load_app_config
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.entities.ontology import NODE_REGISTRY
from tree.memory.consolidation.meta_state import load_watermark, record_dream_run
from tree.memory.extraction.add_entity import _upsert_pending_same_as_edge
from tree.memory.extraction.dedup import (
    DeduplicationConfig,
    MergeStrategy,
    decide_from_candidates,
    dedupe_entity,
)
from tree.memory.review.core import review_duplicate
from tree.memory.review.types import ReviewDecision

if TYPE_CHECKING:
    from pymongo.asynchronous.database import AsyncDatabase

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"

# The dream sweep drives over EVERY persisted node type. Structural rows
# (document / chunk) carry no semantic embedding worth deduping and are
# never re-merged, so they are excluded from the partition list.
_STRUCTURAL_NODE_TYPES: frozenset[str] = frozenset({"document", "chunk"})


def _get_run_logger() -> logging.Logger:
    """Prefect run logger inside a flow/task; the module logger otherwise.

    Lets the pure helpers log through ``caplog`` when invoked outside a flow
    run (unit tests call them directly).
    """

    try:
        return get_run_logger()  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 — Prefect raises a typed context error
        return logger


def _current_flow_run_id() -> str | None:
    """Return the active Prefect flow-run id, or ``None`` outside a flow."""

    try:
        ctx = get_run_context()
    except Exception:  # noqa: BLE001 — no active run context
        return None
    flow_run = getattr(ctx, "flow_run", None)
    if flow_run is None:
        return None
    return str(flow_run.id)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _live_app_config() -> Any:
    """Freshly-loaded :class:`AppConfig` so env-var / YAML changes are seen."""

    return load_app_config()


def _build_dedup_config() -> DeduplicationConfig:
    """Translate the YAML dedup block to the runtime :class:`DeduplicationConfig`.

    Thresholds are read from ``extraction.dedup`` and NOT duplicated under a
    dream-specific block — the dream sweep and the inline write-path share
    one source of truth (``auto_merge_threshold`` / ``flag_threshold`` /
    ``fuzzy_threshold`` / ``match_same_type_only`` / ...).
    """

    cfg = _live_app_config().extraction.dedup
    return DeduplicationConfig(
        enabled=cfg.enabled,
        auto_merge_threshold=cfg.auto_merge_threshold,
        flag_threshold=cfg.flag_threshold,
        use_fuzzy_matching=cfg.use_fuzzy_matching,
        fuzzy_threshold=cfg.fuzzy_threshold,
        max_candidates=cfg.max_candidates,
        match_same_type_only=cfg.match_same_type_only,
        merge_strategy=MergeStrategy(cfg.merge_strategy),
    )


def _partition_node_types() -> list[NodeType]:
    """Every registered, non-structural node type, as :class:`NodeType`.

    Driven off ``NODE_REGISTRY`` so a future ontology addition is swept
    automatically. Structural types (document / chunk) are excluded.
    """

    out: list[NodeType] = []
    for name in NODE_REGISTRY:
        if name in _STRUCTURAL_NODE_TYPES:
            continue
        try:
            out.append(NodeType(name))
        except ValueError:
            # Registered type the back-compat enum can't express yet — the
            # driving query speaks the raw string anyway, but we key the
            # partition list off NodeType so dedupe_entity's type filter
            # matches. Skip unrepresentable types defensively.
            continue
    return out


# ---------------------------------------------------------------------------
# Decision transit types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DreamPair:
    """One actionable duplicate pair the sweep decided on.

    ``id1`` / ``id2`` are ordered (``id1 < id2``) so each unordered pair is
    represented once. ``driving_id`` is the watermark-fresh node that drove
    the comparison; ``matched_id`` is the node it matched (its older or
    parallel twin). ``action`` is the dedup tier (``"merged"`` or
    ``"flagged"``).
    """

    driving_id: str
    matched_id: str
    id1: str
    id2: str
    entity_type: NodeType
    action: str  # "merged" | "flagged"
    similarity_score: float
    match_type: str
    driving_name: str
    matched_name: str


@dataclass
class DreamStats:
    """Counts the sweep records and (on a real run) persists in the watermark."""

    partitions: int = 0
    nodes_driven: int = 0
    pairs_examined: int = 0
    auto_merged: int = 0
    flagged: int = 0
    skipped_existing_same_as: int = 0
    cap_hit: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "partitions": self.partitions,
            "nodes_driven": self.nodes_driven,
            "pairs_examined": self.pairs_examined,
            "auto_merged": self.auto_merged,
            "flagged": self.flagged,
            "skipped_existing_same_as": self.skipped_existing_same_as,
            "cap_hit": self.cap_hit,
        }


@dataclass
class DreamReport:
    """Full result of one ``dream_consolidation`` invocation.

    ``pairs`` lists the actionable duplicate pairs (merged + flagged). On a
    dry-run these are the would-be decisions; nothing was written. ``stats``
    holds the run counters. ``dry_run`` / ``watermark_advanced`` make the
    report self-describing for operators and tests.
    """

    user_id: PydanticObjectId
    dry_run: bool
    run_start: datetime
    last_run_at: datetime
    pairs: list[DreamPair] = field(default_factory=list)
    stats: DreamStats = field(default_factory=DreamStats)
    watermark_advanced: bool = False


# ---------------------------------------------------------------------------
# Pair hygiene helpers
# ---------------------------------------------------------------------------


def _ordered(a: str, b: str) -> tuple[str, str]:
    """Return ``(a, b)`` reordered so the first element is the smaller id."""

    return (a, b) if a < b else (b, a)


async def _same_as_edge_exists(
    *,
    database: AsyncDatabase,
    user_id: PydanticObjectId,
    node_a: str,
    node_b: str,
) -> bool:
    """True iff a SAME_AS edge (ANY status) joins ``node_a`` and ``node_b``.

    Direction-agnostic. We skip such pairs so the sweep respects prior
    decisions: a ``confirmed`` pair was already merged, a ``pending`` pair is
    already queued for review, and a ``rejected`` pair is a human "not a
    duplicate" we must not re-flag.
    """

    collection = database[_KG_COLLECTION]
    existing = await collection.find_one(
        {
            "user_id": user_id,
            "kind": "edge",
            "type": EdgeType.SAME_AS.value,
            "$or": [
                {"source_node_id": node_a, "target_node_id": node_b},
                {"source_node_id": node_b, "target_node_id": node_a},
            ],
        },
        {"_id": 1},
    )
    return existing is not None


async def _iter_driving_nodes(
    *,
    database: AsyncDatabase,
    user_id: PydanticObjectId,
    entity_type: NodeType,
    last_run_at: datetime,
) -> list[dict[str, Any]]:
    """Fetch the watermark-filtered driving set for one ``(user_id, type)``.

    The driving set is non-tombstoned (``merged_into`` absent/null/empty),
    embedded (non-empty ``embedding``), and ``updated_at > last_run_at``.
    This is the ONLY place the watermark filter is applied — the search
    space inside ``dedupe_entity`` stays the full graph.
    """

    collection = database[_KG_COLLECTION]
    cursor = collection.find(
        {
            "user_id": user_id,
            "kind": "node",
            "type": entity_type.value,
            "merged_into": {"$in": [None, "", False]},
            "embedding": {"$exists": True, "$not": {"$size": 0}},
            "updated_at": {"$gt": last_run_at},
        }
    )
    return [doc async for doc in cursor]


# ---------------------------------------------------------------------------
# Sweep — the two-set rule
# ---------------------------------------------------------------------------


async def _collect_dream_candidates(
    *,
    database: AsyncDatabase,
    user_id: PydanticObjectId,
    last_run_at: datetime,
    dedup_config: DeduplicationConfig,
    max_pairs: int,
) -> tuple[list[DreamPair], DreamStats]:
    """Run the two-set sweep and return the actionable pairs + stats.

    Pure decision logic (no writes) so it is unit-testable directly. For
    every watermark-fresh driving node it:

    1. Calls :func:`dedupe_entity` with the node's OWN stored embedding +
       ``incoming_node_id=self`` (so the reject-pair filter applies). The
       search space is the full graph — NOT watermark-filtered.
    2. Re-decides the tier over the returned candidates with the self node
       EXCLUDED (it matches itself at cos≈1.0) via
       :func:`decide_from_candidates`.
    3. Applies pair hygiene: ``id1 < id2`` ordering, a per-run ``seen`` set,
       and a skip for any pair already carrying a SAME_AS edge (any status).
    4. Stops driving once ``max_pairs`` actionable pairs are collected,
       recording ``cap_hit``.
    """

    log = _get_run_logger()
    pairs: list[DreamPair] = []
    stats = DreamStats()
    seen: set[tuple[str, str]] = set()

    for entity_type in _partition_node_types():
        if stats.cap_hit:
            break
        driving_nodes = await _iter_driving_nodes(
            database=database,
            user_id=user_id,
            entity_type=entity_type,
            last_run_at=last_run_at,
        )
        if not driving_nodes:
            continue
        stats.partitions += 1

        for node in driving_nodes:
            if len(pairs) >= max_pairs:
                stats.cap_hit = True
                break

            self_id = str(node["_id"])
            embedding = node.get("embedding") or []
            if not embedding:
                # Defensive: the driving query already excludes unembedded
                # nodes, but never feed an empty vector to $vectorSearch.
                continue

            stats.nodes_driven += 1

            # SEARCH SPACE = full graph (tombstone-excluded only). dedupe_entity
            # scopes by user_id + type and never sees the watermark.
            raw = await dedupe_entity(
                database=database,
                user_id=user_id,
                name=str(node.get("name") or self_id),
                entity_type=entity_type,
                embedding=embedding,
                config=dedup_config,
                incoming_node_id=self_id,
            )
            if raw.action == "none":
                continue

            # Self-match exclusion is the caller's job: the driving node
            # matches itself at cos≈1.0, so re-decide with self dropped.
            decision = decide_from_candidates(
                name=str(node.get("name") or self_id),
                candidates=raw.candidates,
                config=dedup_config,
                exclude_ids={self_id},
            )
            if decision.action == "none" or decision.matched_node_id is None:
                continue

            matched_id = decision.matched_node_id
            if matched_id == self_id:
                # Defensive — should be impossible after exclusion.
                continue

            id1, id2 = _ordered(self_id, matched_id)
            pair_key = (id1, id2)
            if pair_key in seen:
                # new↔new within the same delta: both nodes drive, but the
                # ordered key collapses them to a single processed pair.
                continue
            seen.add(pair_key)
            stats.pairs_examined += 1

            if await _same_as_edge_exists(
                database=database,
                user_id=user_id,
                node_a=id1,
                node_b=id2,
            ):
                stats.skipped_existing_same_as += 1
                continue

            pair = DreamPair(
                driving_id=self_id,
                matched_id=matched_id,
                id1=id1,
                id2=id2,
                entity_type=entity_type,
                action=decision.action,
                similarity_score=decision.similarity_score,
                match_type=decision.match_type or "embedding",
                driving_name=str(node.get("name") or self_id),
                matched_name=str(decision.matched_node_name or matched_id),
            )
            pairs.append(pair)
            if decision.action == "merged":
                stats.auto_merged += 1
            elif decision.action == "flagged":
                stats.flagged += 1

    log.info(
        "dream sweep: partitions=%d nodes_driven=%d pairs_examined=%d "
        "auto_merged=%d flagged=%d skipped_existing_same_as=%d cap_hit=%s",
        stats.partitions,
        stats.nodes_driven,
        stats.pairs_examined,
        stats.auto_merged,
        stats.flagged,
        stats.skipped_existing_same_as,
        stats.cap_hit,
    )
    return pairs, stats


# ---------------------------------------------------------------------------
# Apply decisions — mirrors the inline write-path policy
# ---------------------------------------------------------------------------


async def _apply_dream_decisions(
    *,
    database: AsyncDatabase,
    user_id: PydanticObjectId,
    pairs: list[DreamPair],
    dry_run: bool,
) -> None:
    """Act on the swept pairs using the EXISTING appliers.

    * ``action == "merged"`` ⇒ upsert the ``SAME_AS`` audit edge (via the
      reused :func:`_upsert_pending_same_as_edge` so the edge shape matches
      the inline write-path and ``$setOnInsert`` respects a prior decision),
      THEN confirm it through
      :func:`tree.memory.review.core.review_duplicate` with
      ``decision=CONFIRM, reviewed_by="dream"`` (idempotent rewire →
      tombstone → audit). ``review_duplicate`` operates on an EXISTING
      SAME_AS edge — exactly how the human-review queue works (a pending
      edge exists, then it is confirmed) — so the edge must be written
      first. The function picks the winner via its own tiebreaker; we pass
      the ordered pair.
    * ``action == "flagged"`` ⇒ upsert a ``SAME_AS{status:"pending"}`` edge
      via the same helper so it lands in the existing review queue.

    ``dry_run=True`` ⇒ NO writes at all (no merges, no pending edges).
    """

    log = _get_run_logger()
    if dry_run:
        log.info("dream apply: dry_run=True — %d pair(s), no writes", len(pairs))
        return

    now = datetime.now(tz=UTC)
    for pair in pairs:
        # Both tiers first materialize the SAME_AS audit edge (same shape the
        # inline dedup/extraction path emits). The flagged tier stops here so
        # a human can review; the merged tier confirms it immediately.
        await _upsert_pending_same_as_edge(
            collection=database[_KG_COLLECTION],
            user_id=user_id,
            source_node_id=pair.id1,
            source_type=pair.entity_type,
            target_node_id=pair.id2,
            target_type=pair.entity_type,
            confidence=pair.similarity_score,
            match_type=pair.match_type,
            now=now,
        )
        if pair.action == "merged":
            await review_duplicate(
                database,
                user_id=user_id,
                source_node_id=pair.id1,
                target_node_id=pair.id2,
                decision=ReviewDecision.CONFIRM,
                reviewed_by="dream",
            )

    log.info(
        "dream apply: merged=%d flagged=%d written",
        sum(1 for p in pairs if p.action == "merged"),
        sum(1 for p in pairs if p.action == "flagged"),
    )


# ---------------------------------------------------------------------------
# #052 seam — supersession sweep (flag-gated, NOT implemented here)
# ---------------------------------------------------------------------------


async def _supersession_sweep(
    *,
    database: AsyncDatabase,
    user_id: PydanticObjectId,
    last_run_at: datetime,
    dry_run: bool,
) -> None:
    """Seam for #052's flag-gated LLM supersession / contradiction sweep.

    Intentionally a no-op in #051. #052 fills this in and the flow only
    invokes it when ``app_config.dream.enable_supersession_judge`` is True.
    Keeping the seam here means #052 plugs into one well-named spot rather
    than re-threading the flow.
    """

    return None


# ---------------------------------------------------------------------------
# Prefect tasks (thin wrappers over the pure helpers)
# ---------------------------------------------------------------------------


sweep_node_duplicates = task(
    _collect_dream_candidates,
    name="dream-sweep-node-duplicates",
    cache_policy=NO_CACHE,
    retries=1,
)

apply_dream_decisions = task(
    _apply_dream_decisions,
    name="dream-apply-decisions",
    cache_policy=NO_CACHE,
    retries=2,
    retry_delay_seconds=10,
)


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------


@flow(name="dream-consolidation", log_prints=True)
async def dream_consolidation(
    user_id: PydanticObjectId,
    *,
    dry_run: bool | None = None,
) -> DreamReport:
    """Incremental dream-consolidation sweep for ``user_id``.

    Steps:

    1. **load_watermark** — read ``(user_id, "dream")``; capture
       ``run_start = now(UTC)`` BEFORE any processing. Missing watermark ⇒
       EPOCH ⇒ full sweep.
    2. **sweep_node_duplicates** — the two-set rule (driving set is
       watermark-filtered; search space is the full graph). Embedding-READ
       only — it reuses each node's stored vector, so the sweep makes ZERO
       Voyage embedding calls.
    3. **apply_dream_decisions** — merge (``review_duplicate(CONFIRM)``) or
       flag (pending SAME_AS) via the existing appliers. ``dry_run`` ⇒ no
       writes.
    4. **record_dream_run** — ONLY on a successful non-dry-run, advance the
       watermark to ``run_start``.

    Args:
        user_id: Required tenant scope (Prefect parameter).
        dry_run: When ``None`` (default), falls back to
            ``app_config.dream.dry_run``. ``True`` reports the would-be
            decisions and writes nothing (no merges, no SAME_AS, no watermark
            advance).

    Returns:
        A :class:`DreamReport` describing the run.
    """

    log = _get_run_logger()
    app_cfg = _live_app_config()
    dream_cfg = app_cfg.dream
    effective_dry_run = dream_cfg.dry_run if dry_run is None else dry_run
    dedup_config = _build_dedup_config()

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = client[settings.mongo.mongo_initdb_database]

    # --- Step 1: watermark + run_start (captured BEFORE any processing) -------
    run_start = datetime.now(tz=UTC)
    watermark = await load_watermark(database=database, user_id=user_id)
    last_run_at = watermark.last_run_at
    log.info(
        "dream_consolidation: user_id=%s dry_run=%s last_run_at=%s run_start=%s",
        user_id,
        effective_dry_run,
        last_run_at.isoformat(),
        run_start.isoformat(),
    )

    if not dream_cfg.enabled:
        log.info("dream_consolidation: disabled via app_config.dream.enabled=False")
        return DreamReport(
            user_id=user_id,
            dry_run=effective_dry_run,
            run_start=run_start,
            last_run_at=last_run_at,
        )

    # --- Step 2: sweep (two-set rule) -----------------------------------------
    pairs, stats = await sweep_node_duplicates(
        database=database,
        user_id=user_id,
        last_run_at=last_run_at,
        dedup_config=dedup_config,
        max_pairs=dream_cfg.max_pairs,
    )

    # --- Step 3: apply decisions ----------------------------------------------
    await apply_dream_decisions(
        database=database,
        user_id=user_id,
        pairs=pairs,
        dry_run=effective_dry_run,
    )

    # --- #052 seam: flag-gated supersession sweep -----------------------------
    if dream_cfg.enable_supersession_judge:
        await _supersession_sweep(
            database=database,
            user_id=user_id,
            last_run_at=last_run_at,
            dry_run=effective_dry_run,
        )

    # --- Step 4: advance watermark (real runs only) ---------------------------
    watermark_advanced = False
    if not effective_dry_run:
        await record_dream_run(
            database=database,
            user_id=user_id,
            run_start=run_start,
            last_run_id=_current_flow_run_id(),
            last_stats=stats.as_dict(),
        )
        watermark_advanced = True
        log.info(
            "dream_consolidation: watermark advanced to run_start=%s",
            run_start.isoformat(),
        )
    else:
        log.info("dream_consolidation: dry_run — watermark NOT advanced")

    return DreamReport(
        user_id=user_id,
        dry_run=effective_dry_run,
        run_start=run_start,
        last_run_at=last_run_at,
        pairs=pairs,
        stats=stats,
        watermark_advanced=watermark_advanced,
    )
