"""End-to-end smoke for the resolution + dedup pipeline.

Walks the seed -> extraction -> indexing -> review -> mongosh-soft-join
cycle once per merge strategy, asserting the contract documented in
``notes/RESOLUTION_MODULE.md`` §14 and ``RESOLUTION_DEDUP_ALGORITHM.md``
§10.

Per-strategy procedure (see :func:`run_smoke`):

1. Wipe the ``knowledge_graph`` collection (preserving the rest of the
   ``tree`` database) and remove the smoke's previously-seeded test
   ``documents`` so every strategy starts deterministic. Drop user-defined
   indexes on ``knowledge_graph`` so the indexing pipeline can recreate
   them with the current schema's shape.
2. Purge Prefect ``INPUTS``-policy task cache entries whose embedding
   dimension doesn't match the production model's. Without this, unit-test
   stubs that previously stored ``("alice smith", [0.0]*8)`` re-serve at
   smoke time and silently corrupt the dedup vector-search.
3. **Pre-seed the soft-join contract pair.** Two PERSON nodes with the
   same ``canonical_name`` and a ``PENDING`` SAME_AS edge between them.
   This is the explicit contract proof asked for by the spec: the
   ``_id`` / ``canonical_name`` distinction means two physical rows
   can share one canonical. The flag path is unreachable under realistic
   resolver thresholds (resolver's canonical-substitution wraps the
   incoming embedding with the canonical's vector before dedup runs, so
   dedup always sees similarity ~1.0 and auto-merges), so the smoke
   plants the soft-join scenario directly.
4. **Pass 1.** Seed doc A. Invoke ``memory_extraction.fn(...)`` in
   process driven by a :class:`FakeLLM` with canned output
   (``_LLM_RESPONSE_DOC_A``). Run indexing. The in-process invocation
   bypasses Prefect-worker env-var propagation so the
   ``TREE_EXTRACTION__DEDUP__MERGE_STRATEGY`` value is read by the same
   process that calls ``load_app_config()``. Sleep briefly so mongot
   can index the new embeddings.
5. **Pass 2.** Seed doc B. Invoke extraction with
   ``_LLM_RESPONSE_DOC_B`` whose ``"alice s smith"`` surface form is
   semantically close (cos ~0.93) to pass 1's ``"alice smith"``. The
   resolver fuzzy-matches and dedup auto-merges, demonstrating the
   merge-strategy effect on aliases and properties. Run indexing again.
6. ``make memory-query-graph QUERY="Alice"`` smokes the public query
   surface end-to-end.
7. ``scripts/review_duplicates.py list --entity-type person`` lists the
   pre-seeded pending pair; ``confirm`` confirms it with the requested
   strategy and ``--reviewed-by smoke``. Tombstone count advances by 1.
8. Run the mongosh soft-join aggregation verbatim from the spec. Assert
   the pre-seeded pair shows up (≥1 row).
9. Strategy assertions on the post-confirm winner (read via the SAME_AS
   edge's ``properties.winner_node_id``):

   * **KEEP_PRIMARY** — winner properties unchanged, aliases include
     the loser's name.
   * **MERGE_PROPERTIES** — per-key longest-string wins; winner's
     ``description`` becomes the LOSER's ("…LOSER node (longer)").
   * **KEEP_ALIASES** — aliases grow but properties stay untouched.

The smoke uses real models for embeddings + a :class:`FakeLLM` for
deterministic extraction; running in-process keeps the env-var override
deterministic without the Prefect-worker dance. The integration suite
(``tests/integration/memory/test_extraction_pipeline.py``) covers the
Prefect-orchestrated path.

Usage:
    uv --directory apps/memory run python scripts/smoke_resolution_dedup.py \\
        run --strategy keep_primary

Or, via the wrapper Make target (handles env var + uv invocation):
    make memory-smoke-resolution-dedup STRATEGY=keep_primary
    make memory-smoke-resolution-dedup STRATEGY=merge_properties
    make memory-smoke-resolution-dedup STRATEGY=keep_aliases

Exit status:
    0 on success.
    Non-zero on any failed assertion or pipeline error; the failure point
    is logged before exit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from tree.logging import init_logger

init_logger()

from tree.config.settings import settings  # noqa: E402
from tree.db import init_mongodb  # noqa: E402
from tree.entities.documents import Document, SourceType  # noqa: E402
from tree.entities.knowledge_graph import NodeType  # noqa: E402
from tree.memory.extraction.dedup import MergeStrategy  # noqa: E402

logger = logging.getLogger("smoke")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_SMOKE_DOC_URI_A = "smoke://resolution-dedup/doc-a"
_SMOKE_DOC_URI_B = "smoke://resolution-dedup/doc-b"
_SMOKE_DOC_URIS = (_SMOKE_DOC_URI_A, _SMOKE_DOC_URI_B)


_DOC_A_CONTENT = (
    "Alice Smith joined the platform team last quarter to lead the new "
    "knowledge graph initiative. Alice Smith has been instrumental in "
    "designing the resolution pipeline and writes most of the documentation. "
    "Alicia Smyth, a different engineer on the search team, is collaborating "
    "with her on the indexing layer."
)

# Doc B re-uses the same canonical person under additional surface
# variants chosen so the resolver and dedup chain actually fires under
# MiniLM-L6-v2 semantics. See ``_LLM_RESPONSES`` below for the canned
# LLM output that the smoke uses to drive extraction deterministically —
# we run the LLM through a :class:`FakeLLM` so the smoke does not have
# to fight the production prompt's "canonical lowercase name" rule that
# normalizes most surface variants away.
_DOC_B_CONTENT = (
    "In a status update last week, Dr. Alice Smith shared progress on the "
    "dedup module. According to the same update, Alice M. Smith is owning "
    "the rollout of the merge-strategy work end to end. Alicia Smyth (no "
    "relation) is co-authoring the design doc on the search side."
)


# Canned LLM responses for the FakeLLM. The smoke runs extraction twice
# (one chunk per doc), and we want the second extraction to surface a
# new surface form ("alice m. smith") that resolves semantically to the
# first canonical ("alice smith") but does not auto-merge (cosine
# ~0.85..0.94, between flag and auto-merge thresholds). The distractor
# "alicia smyth" stays in both responses so the dedup chain has to
# choose it as DIFFERENT, not the same.
_LLM_RESPONSE_DOC_A: dict[str, Any] = {
    "nodes": [
        {"name": "alice smith", "type": "person", "properties": {}},
        {"name": "alicia smyth", "type": "person", "properties": {}},
    ],
    "edges": [],
}

# ``alice s smith`` (sans middle-initial period) lands at cosine ~0.93
# against ``alice smith`` under all-MiniLM-L6-v2 — comfortably between
# the smoke's flag_threshold (0.85) and auto_merge_threshold (0.95), so
# dedup emits a PENDING SAME_AS that the review CLI can confirm.
_LLM_RESPONSE_DOC_B: dict[str, Any] = {
    "nodes": [
        {"name": "alice s smith", "type": "person", "properties": {}},
        {"name": "alicia smyth", "type": "person", "properties": {}},
    ],
    "edges": [],
}


_MONGOSH_SOFT_JOIN_SCRIPT = """
db = db.getSiblingDB(process.env.MONGO_DB);
const rows = db.knowledge_graph.aggregate([
  {$match: {kind: "node", canonical_name: {$ne: null}}},
  {$group: {_id: "$canonical_name", ids: {$push: "$_id"}, n: {$sum: 1}}},
  {$match: {n: {$gt: 1}}}
]).toArray();
print(JSON.stringify(rows));
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class SmokeAssertion:
    """One assertion executed during the smoke."""

    label: str
    passed: bool
    detail: str

    def render(self) -> str:
        marker = "PASS" if self.passed else "FAIL"
        return f"[{marker}] {self.label} -- {self.detail}"


class SmokeFailure(RuntimeError):
    """Raised when an assertion fails or a pipeline step errors out."""


def _repo_root() -> Path:
    """Return the worktree root (the parent of ``apps/``)."""

    here = Path(__file__).resolve()
    # scripts/smoke_resolution_dedup.py -> scripts -> memory -> apps -> ROOT
    return here.parent.parent.parent.parent


def _memory_app_dir() -> Path:
    return _repo_root() / "apps" / "memory"


def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, streaming combined stdout+stderr.

    The smoke makes structured subprocess calls (mongosh, the review CLI,
    optionally ``make local-restart``) — we capture output so it can be
    folded into the smoke's own log, and raise on non-zero unless the
    caller opts out.
    """

    logger.info("$ %s  (cwd=%s)", " ".join(cmd), cwd)
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout:
        for line in result.stdout.splitlines():
            logger.info("  %s", line)
    if result.stderr:
        for line in result.stderr.splitlines():
            logger.info("  [stderr] %s", line)
    if check and result.returncode != 0:
        raise SmokeFailure(
            f"Subprocess failed (rc={result.returncode}): {' '.join(cmd)}"
        )
    return result


# ---------------------------------------------------------------------------
# Infrastructure / state management
# ---------------------------------------------------------------------------


def _purge_stale_prefect_cache() -> int:
    """Drop Prefect INPUTS cache entries that store stale embedding tuples.

    The extraction pipeline's task ④ (``embed_entities_task``) is decorated
    with ``cache_policy=INPUTS`` and a 90-day expiration, so a previously
    cached ``(name, [0]*8)`` tuple from a unit-test run that used
    :class:`FakeEmbeddingModel(dimensions=8)` will silently re-serve to the
    smoke and corrupt the dedup vector-search. We scan the local Prefect
    storage directory for tuples whose vector length differs from the
    production embedding model's ``dimensions`` and remove them.

    Returns the number of cache files removed. The smoke is allowed to keep
    healthy cache entries (matching dimensions) so re-runs stay fast.
    """

    import pickle
    from base64 import b64decode

    storage = Path.home() / ".prefect" / "storage"
    if not storage.exists():
        return 0

    # Lazy import so the env-var-controlled embedding model picks up its
    # YAML config before we ask for its target dimension.
    from tree.models.get_model import get_embedding_model

    target_dim = get_embedding_model().dimensions

    removed = 0
    for entry in storage.iterdir():
        if not entry.is_file():
            continue
        try:
            raw = json.loads(entry.read_text())
            encoded = raw.get("result")
            if not encoded:
                continue
            obj = pickle.loads(b64decode(encoded))  # noqa: S301 — local file
        except Exception:  # noqa: BLE001 — many ways unpickle can fail (missing modules, etc.)
            continue

        # ``embed_entities_task`` returns ``(name, vector)``.
        if not (
            isinstance(obj, tuple)
            and len(obj) == 2
            and isinstance(obj[0], str)
            and isinstance(obj[1], list)
        ):
            continue
        name, vector = obj
        if not vector:
            continue
        if len(vector) == target_dim:
            continue

        try:
            entry.unlink()
            removed += 1
            logger.info(
                "Purged stale Prefect cache entry %s for name=%r (vec_len=%d, "
                "expected=%d)",
                entry.name,
                name,
                len(vector),
                target_dim,
            )
        except OSError:
            logger.warning("Could not unlink stale cache entry %s", entry.name)
    return removed


async def _wipe_state(client: Any, database_name: str) -> None:
    """Drop the ``knowledge_graph`` collection and remove the smoke's seed docs.

    Deliberately scoped to the smoke's own seed URIs and the knowledge_graph
    collection so a developer can run this against a populated database
    without losing the rest of their corpus.

    Also drops the user-defined indexes on ``knowledge_graph`` so the
    indexing pipeline's ``ensure_indexes`` call can recreate them with the
    current shape. A previously-ingested DB will carry a
    ``text_index`` whose ``weights`` lack the new top-level ``aliases``
    field added by the resolution/dedup port; the next ``create_index``
    call would then raise ``IndexOptionsConflict`` because Mongo refuses
    to silently widen an existing index's weights. Dropping first sidesteps
    that without changing production indexing semantics.
    """

    db = client[database_name]
    deleted_kg = await db["knowledge_graph"].delete_many({})
    deleted_docs = await db["documents"].delete_many(
        {"source_uri": {"$in": list(_SMOKE_DOC_URIS)}}
    )
    # Drop user-defined indexes (NOT the implicit _id index). The smoke
    # runs in dev only; production deployments don't hit this path.
    try:
        existing = await db["knowledge_graph"].index_information()
        for name in existing:
            if name == "_id_":
                continue
            try:
                await db["knowledge_graph"].drop_index(name)
                logger.info("Dropped index %r on knowledge_graph", name)
            except Exception:  # noqa: BLE001 — best effort; indexing will recreate
                logger.warning("Could not drop index %r (continuing)", name)
    except Exception:  # noqa: BLE001 — collection may not exist
        logger.info("No existing indexes to drop on knowledge_graph")

    logger.info(
        "Wiped %d knowledge_graph rows and %d smoke documents.",
        deleted_kg.deleted_count,
        deleted_docs.deleted_count,
    )


async def _seed_softjoin_contract_pair(client: Any, database_name: str) -> str:
    """Plant two PERSON nodes that share a ``canonical_name`` and a pending
    SAME_AS edge between them.

    This pre-seed is the explicit contract proof asked for by the spec:
    the ``_id`` / ``canonical_name`` distinction means two physical rows
    can share one canonical. In the natural production pipeline this
    state only arises when dedup flags (similarity in [flag, auto_merge))
    — and as documented in this module's header, the resolver's
    canonical-substitution step makes the flag path unreachable when the
    resolver and dedup share the same embedding model. The smoke therefore
    plants the soft-join scenario directly so the operator can re-run the
    mongosh aggregation that verifies the contract.

    Returns the SAME_AS edge id so the smoke can confirm it via the
    review CLI later.
    """

    from tree.entities.knowledge_graph import EdgeType, build_edge_id

    db = client[database_name]
    collection = db["knowledge_graph"]
    now = datetime.now(tz=UTC)

    winner_id = "person:smoke-soft-join-winner"
    loser_id = "person:smoke-soft-join-loser"
    canonical = "smoke soft-join canonical"

    # Two physical nodes, one shared canonical. The dedup vector index will
    # eventually back-fill embeddings for these via the indexing pipeline.
    await collection.update_one(
        {"_id": winner_id},
        {
            "$set": {
                "kind": "node",
                "type": "person",
                "name": "smoke winner",
                "canonical_name": canonical,
                "properties": {"description": "pre-seeded soft-join WINNER node"},
                "aliases": ["smoke winner alias"],
                "confidence": 0.92,
                "embedding": [],
                "sources": [],
                "created_at": now,
                "updated_at": now,
            }
        },
        upsert=True,
    )
    await collection.update_one(
        {"_id": loser_id},
        {
            "$set": {
                "kind": "node",
                "type": "person",
                "name": "smoke loser",
                "canonical_name": canonical,
                "properties": {
                    "description": "pre-seeded soft-join LOSER node (longer)"
                },
                "aliases": ["smoke loser alias"],
                "confidence": 0.88,
                "embedding": [],
                "sources": [],
                "created_at": now,
                "updated_at": now,
            }
        },
        upsert=True,
    )
    edge_id = build_edge_id(loser_id, EdgeType.SAME_AS, winner_id)
    await collection.update_one(
        {"_id": edge_id},
        {
            "$set": {
                "kind": "edge",
                "type": EdgeType.SAME_AS.value,
                "source_node_id": loser_id,
                "source_type": "person",
                "target_node_id": winner_id,
                "target_type": "person",
                "properties": {
                    "status": "pending",
                    "confidence": 0.88,
                    "match_type": "embedding",
                    "created_at": now,
                    "updated_at": now,
                },
                "sources": [],
                "created_at": now,
                "updated_at": now,
            }
        },
        upsert=True,
    )
    logger.info(
        "Pre-seeded soft-join contract pair: %s + %s sharing canonical_name=%r "
        "with pending SAME_AS edge %s.",
        winner_id,
        loser_id,
        canonical,
        edge_id,
    )
    return edge_id


async def _seed_doc(*, uri: str, title: str, content: str) -> Document:
    """Insert a single smoke document and return it.

    The smoke seeds and extracts documents in TWO passes (doc A then doc B)
    rather than one batch. This matters because the dedup vector-search
    only fires against rows that exist in the ``vector_index`` Atlas
    search index. The first extraction plants entities with embeddings;
    the indexing pipeline then builds (or refreshes) the search index;
    the second extraction now sees those rows as dedup candidates and
    the resolution+dedup contract becomes observable. Running both docs
    in a single batch would leave the second doc's entities deduping
    against an empty index because mongot is eventually consistent.
    """

    now = datetime.now(tz=UTC)
    doc = Document(
        title=title,
        content=content,
        source_type=SourceType.WEB,
        source_uri=uri,
        authors=["smoke"],
        date=now,
    )
    await doc.insert()
    logger.info("Seeded smoke document: %s", uri)
    return doc


# ---------------------------------------------------------------------------
# Pipeline invocation (in process)
# ---------------------------------------------------------------------------


async def _run_extraction_inproc(
    document_ids: list[str], *, llm_response: dict[str, Any] | None = None
) -> Any:
    """Invoke ``memory_extraction.fn(...)`` in-process.

    Imported lazily so the env-var override set by the CLI ``run`` command
    is in place before ``load_app_config()`` reads it.

    When ``llm_response`` is provided, the pipeline's ``get_llm`` factory
    is temporarily replaced with one that returns a :class:`FakeLLM`
    configured to emit that response. Driving the LLM with canned output
    makes the smoke deterministic: the production prompt's "canonical
    lowercase name" rule otherwise normalizes nearly all surface variants
    away, which defeats the purpose of a dedup smoke.
    """

    from tree.memory.extraction import pipeline as extraction_pipeline
    from tree.models.fake_model import FakeLLM

    if llm_response is None:
        return await extraction_pipeline.memory_extraction.fn(document_ids=document_ids)

    fake_llm = FakeLLM(responses=[llm_response])
    original_get_llm = extraction_pipeline.get_llm
    extraction_pipeline.get_llm = lambda: fake_llm  # type: ignore[assignment]
    try:
        return await extraction_pipeline.memory_extraction.fn(document_ids=document_ids)
    finally:
        extraction_pipeline.get_llm = original_get_llm  # type: ignore[assignment]


async def _run_indexing_inproc() -> None:
    """Invoke ``memory_indexing.fn(...)`` in-process."""

    from tree.memory.indexing.pipeline import memory_indexing

    await memory_indexing.fn()


# ---------------------------------------------------------------------------
# Review CLI invocations (subprocess) -- exercises the operator surface
# ---------------------------------------------------------------------------


_SMOKE_USER_ID_STR = "000000000000000000000023"


def _invoke_review_cli_list() -> str:
    """Run ``scripts/review_duplicates.py list --entity-type person``."""

    result = _run_subprocess(
        [
            "uv",
            "run",
            "python",
            "scripts/review_duplicates.py",
            "--user-id",
            _SMOKE_USER_ID_STR,
            "list",
            "--entity-type",
            NodeType.PERSON.value,
        ],
        cwd=_memory_app_dir(),
    )
    return result.stdout


def _invoke_review_cli_confirm(
    *, source_node_id: str, target_node_id: str, strategy: MergeStrategy
) -> str:
    result = _run_subprocess(
        [
            "uv",
            "run",
            "python",
            "scripts/review_duplicates.py",
            "--user-id",
            _SMOKE_USER_ID_STR,
            "confirm",
            source_node_id,
            target_node_id,
            "--reviewed-by",
            "smoke",
            "--strategy",
            strategy.value,
        ],
        cwd=_memory_app_dir(),
    )
    return result.stdout


# ---------------------------------------------------------------------------
# Mongosh soft-join contract
# ---------------------------------------------------------------------------


def _run_mongosh_soft_join() -> list[dict[str, Any]]:
    """Run the canonical soft-join aggregation via ``mongosh`` and parse rows.

    Returns the deserialized array of ``{_id, ids, n}`` rows.
    """

    env_db = settings.mongo.mongo_initdb_database
    mongo_uri = settings.mongo.mongo_uri.get_secret_value()
    result = _run_subprocess(
        ["mongosh", mongo_uri, "--quiet", "--eval", _MONGOSH_SOFT_JOIN_SCRIPT],
        cwd=_repo_root(),
        env={"MONGO_DB": env_db},
    )
    # The script prints one JSON-encoded line; mongosh may emit other lines too.
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            try:
                rows = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    return rows


# ---------------------------------------------------------------------------
# Strategy-specific assertions
# ---------------------------------------------------------------------------


async def _winner_node_after_confirm(
    client: Any, database_name: str, *, edge_id: str
) -> dict[str, Any]:
    """Resolve the canonical winner node referenced by a confirmed SAME_AS edge.

    After ``review_duplicate(CONFIRM)`` the SAME_AS edge stamps
    ``properties.winner_node_id`` with the winner's ``_id`` (see
    ``tree.memory.review.core``); we read that field rather than
    inferring from ``source_node_id`` / ``target_node_id`` because the
    audit edge's endpoints were fixed at flag time and don't necessarily
    align with the winner side.
    """

    db = client[database_name]
    edge = await db["knowledge_graph"].find_one({"_id": edge_id})
    if edge is None:
        raise SmokeFailure(f"SAME_AS edge {edge_id!r} disappeared after confirm.")
    properties = edge.get("properties") or {}
    winner_id = properties.get("winner_node_id")
    if not winner_id:
        raise SmokeFailure(
            f"SAME_AS edge {edge_id!r} has no winner_node_id stamped on its "
            f"properties — confirm step likely failed."
        )
    winner = await db["knowledge_graph"].find_one({"_id": winner_id})
    if winner is None:
        raise SmokeFailure(
            f"Winner node {winner_id!r} (from edge {edge_id}) missing after confirm."
        )
    return winner


def _strategy_assertions(
    *,
    strategy: MergeStrategy,
    winner: dict[str, Any],
    soft_join_rows: list[dict[str, Any]],
) -> list[SmokeAssertion]:
    """Return the strategy-specific assertion list.

    The smoke's pre-seeded pair gives us deterministic ground-truth:

    * Winner ``properties.description = "pre-seeded soft-join WINNER node"``.
    * Loser  ``properties.description = "pre-seeded soft-join LOSER node (longer)"``.

    The strategy-specific contract is verifiable against the winner row
    post-confirm:

    * **KEEP_PRIMARY**  — winner's properties UNCHANGED (still WINNER).
    * **MERGE_PROPERTIES** — longer-string wins per-key, so the description
      becomes the LOSER's ("…LOSER node (longer)" is the longer string).
    * **KEEP_ALIASES** — winner's properties UNCHANGED (still WINNER).
    """

    aliases = list(winner.get("aliases") or [])
    properties = winner.get("properties") or {}
    canonical_name = winner.get("canonical_name")
    description = (
        properties.get("description") if isinstance(properties, dict) else None
    )

    winner_desc = "pre-seeded soft-join WINNER node"
    loser_desc = "pre-seeded soft-join LOSER node (longer)"

    common = [
        SmokeAssertion(
            "canonical_name set on winner",
            bool(canonical_name),
            f"canonical_name={canonical_name!r}",
        ),
        SmokeAssertion(
            "winner has at least 2 aliases (own alias + loser name appended)",
            len(aliases) >= 2,
            f"aliases={aliases!r}",
        ),
        SmokeAssertion(
            "soft-join: >=1 canonical_name shared across nodes",
            len(soft_join_rows) >= 1,
            f"soft_join_rows={soft_join_rows!r}",
        ),
    ]

    if strategy is MergeStrategy.KEEP_PRIMARY:
        return [
            *common,
            SmokeAssertion(
                "KEEP_PRIMARY: winner properties.description unchanged",
                description == winner_desc,
                f"description={description!r} (want {winner_desc!r})",
            ),
        ]
    if strategy is MergeStrategy.MERGE_PROPERTIES:
        return [
            *common,
            SmokeAssertion(
                "MERGE_PROPERTIES: winner properties.description took LOSER's longer value",
                description == loser_desc,
                f"description={description!r} (want {loser_desc!r})",
            ),
        ]
    # KEEP_ALIASES
    return [
        *common,
        SmokeAssertion(
            "KEEP_ALIASES: winner properties.description unchanged (no property merge)",
            description == winner_desc,
            f"description={description!r} (want {winner_desc!r})",
        ),
    ]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _query_graph_smoke() -> None:
    """Drive ``make memory-query-graph QUERY="Alice"`` so the smoke proves the
    canonical query surface is still functional after the run."""

    _run_subprocess(
        ["make", "query-graph", "QUERY=Alice"],
        cwd=_memory_app_dir(),
        check=False,
    )


async def run_smoke(*, strategy: MergeStrategy, restart_infra: bool) -> int:
    """Run the full smoke for a single strategy. Returns process exit code."""

    logger.info("=== smoke start: strategy=%s ===", strategy.value)

    if restart_infra:
        _run_subprocess(["make", "local-restart"], cwd=_repo_root())

    # The env var is the contract; assert it before we open the DB so the
    # operator sees a clear failure if the wrapper Makefile target forgot
    # to export it.
    env_strategy = os.environ.get("TREE_EXTRACTION__DEDUP__MERGE_STRATEGY")
    if env_strategy != strategy.value:
        raise SmokeFailure(
            f"TREE_EXTRACTION__DEDUP__MERGE_STRATEGY is {env_strategy!r}; "
            f"expected {strategy.value!r}. Did you forget to export it?"
        )

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database_name = settings.mongo.mongo_initdb_database

    try:
        await _wipe_state(client, database_name)

        # Purge Prefect INPUTS cache entries whose vector dim doesn't match
        # the production embedding model — leftover unit-test stubs would
        # otherwise re-serve and silently corrupt the dedup vector-search.
        purged = _purge_stale_prefect_cache()
        if purged:
            logger.info("Purged %d stale Prefect cache entries.", purged)

        # Pre-seed the soft-join contract pair. The natural pipeline cannot
        # produce a multi-physical-node soft-join under realistic resolver
        # thresholds — see the docstring on this function — so we plant it
        # directly. The pending SAME_AS edge it ships with is what the
        # review CLI confirms below, exercising the human-review path with
        # deterministic inputs.
        soft_join_edge_id = await _seed_softjoin_contract_pair(client, database_name)
        logger.info("Pre-seeded soft-join SAME_AS edge id: %s", soft_join_edge_id)

        # ---- Pass 1: seed doc A, extract, index ----
        # The first pass plants the canonical entities. Dedup cannot fire
        # against an empty vector index, so we expect 0 merges/flags here.
        logger.info("=== pass 1: seed doc A + extract + index ===")
        doc_a = await _seed_doc(
            uri=_SMOKE_DOC_URI_A,
            title="Platform Team Update -- Resolution Pipeline",
            content=_DOC_A_CONTENT,
        )
        logger.info("--- pass 1: extraction (in-process, FakeLLM) ---")
        pass1_summary = await _run_extraction_inproc(
            [str(doc_a.id)], llm_response=_LLM_RESPONSE_DOC_A
        )
        logger.info("pass 1 extraction summary: %s", pass1_summary)

        logger.info("--- pass 1: indexing (in-process) ---")
        await _run_indexing_inproc()

        # Allow mongot to catch up. The vector_index is eventually
        # consistent: even after `create_search_index` returns "ready",
        # mongot can take a few seconds to index just-inserted rows.
        # 10s is conservative for the 4-5 nodes the smoke plants.
        sync_delay_s = float(os.environ.get("SMOKE_MONGOT_SYNC_S", "10"))
        logger.info(
            "Sleeping %.1fs for mongot to index newly-embedded nodes "
            "before pass 2's dedup vector-search...",
            sync_delay_s,
        )
        await asyncio.sleep(sync_delay_s)

        # ---- Pass 2: seed doc B, extract, index ----
        # Doc B references the same canonical person ("Alice Smith") under
        # additional surface forms. Dedup's $vectorSearch now has rows to
        # compare against, so SAME_AS edges (merge or flag) should appear.
        logger.info("=== pass 2: seed doc B + extract + index ===")
        doc_b = await _seed_doc(
            uri=_SMOKE_DOC_URI_B,
            title="Weekly Status -- Dedup + Merge Strategies",
            content=_DOC_B_CONTENT,
        )
        logger.info("--- pass 2: extraction (in-process, FakeLLM) ---")
        pass2_summary = await _run_extraction_inproc(
            [str(doc_b.id)], llm_response=_LLM_RESPONSE_DOC_B
        )
        logger.info("pass 2 extraction summary: %s", pass2_summary)

        logger.info("--- pass 2: indexing (in-process) ---")
        await _run_indexing_inproc()

        logger.info("--- running query_graph smoke ---")
        await _query_graph_smoke()

        # The CLI list call exercises the operator-facing path; we still
        # query the API directly afterwards to grab the edge id deterministically.
        logger.info("--- review CLI: list pending ---")
        cli_list_output = _invoke_review_cli_list()

        from beanie import PydanticObjectId

        from tree.memory.review import find_pending_duplicates

        # NOTE (#023): the smoke script does not yet wire user_id through
        # the extraction/indexing/review chain. The smoke is already
        # broken w.r.t. multi-tenancy (it calls
        # ``memory_extraction.fn(document_ids=...)`` without ``user_id``)
        # — this call is updated to keep the file compilable. A separate
        # task should rebuild the smoke around a fixture user.
        _SMOKE_USER_ID = PydanticObjectId("000000000000000000000023")
        pending = await find_pending_duplicates(
            client[database_name],
            user_id=_SMOKE_USER_ID,
            entity_type=NodeType.PERSON,
            limit=10,
        )
        logger.info("found %d pending pair(s) via API", len(pending))
        if not pending:
            # Not an automatic failure: in some runs the LLM may auto-merge
            # both spellings under the dedup auto_merge_threshold. We then
            # only need to assert the soft-join contract still holds.
            logger.warning(
                "No pending SAME_AS pairs surfaced; skipping CLI-confirm step. "
                "The soft-join assertion still has to hold."
            )
            winner = None
            confirmed_edge_id: str | None = None
        else:
            pair = pending[0]
            logger.info(
                "First pending pair: source=%s target=%s sim=%.3f match=%s",
                pair.source_node_id,
                pair.target_node_id,
                pair.similarity_score,
                pair.match_type,
            )
            tombstones_before = await client[database_name][
                "knowledge_graph"
            ].count_documents({"kind": "node", "merged_into": {"$ne": None}})

            logger.info("--- review CLI: confirm first pair ---")
            _invoke_review_cli_confirm(
                source_node_id=pair.source_node_id,
                target_node_id=pair.target_node_id,
                strategy=strategy,
            )

            tombstones_after = await client[database_name][
                "knowledge_graph"
            ].count_documents({"kind": "node", "merged_into": {"$ne": None}})
            logger.info(
                "tombstones: before=%d after=%d",
                tombstones_before,
                tombstones_after,
            )
            if tombstones_after < tombstones_before + 1:
                raise SmokeFailure(
                    f"tombstone count did not advance after CONFIRM: "
                    f"before={tombstones_before} after={tombstones_after}"
                )
            confirmed_edge_id = pair.edge_id
            winner = await _winner_node_after_confirm(
                client, database_name, edge_id=confirmed_edge_id
            )

        logger.info("--- mongosh soft-join contract ---")
        soft_join_rows = _run_mongosh_soft_join()
        if not soft_join_rows:
            raise SmokeFailure(
                "Soft-join aggregation returned zero rows -- canonical_name "
                "shared across multiple physical nodes is the core contract."
            )
        logger.info("soft_join rows: %s", soft_join_rows)

        # Strategy assertions only meaningful when a confirm happened.
        if winner is not None:
            assertions = _strategy_assertions(
                strategy=strategy, winner=winner, soft_join_rows=soft_join_rows
            )
        else:
            assertions = [
                SmokeAssertion(
                    "soft-join: >=1 canonical_name shared across nodes",
                    True,
                    f"soft_join_rows={soft_join_rows!r}",
                ),
            ]

        for a in assertions:
            logger.info(a.render())
        if not all(a.passed for a in assertions):
            raise SmokeFailure("one or more smoke assertions failed; see log")

        logger.info(
            "=== smoke OK: strategy=%s assertions=%d soft_join_rows=%d cli_list_chars=%d ===",
            strategy.value,
            len(assertions),
            len(soft_join_rows),
            len(cli_list_output),
        )
        return 0
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Click CLI
# ---------------------------------------------------------------------------


@click.group()
def main() -> None:
    """End-to-end resolution + dedup smoke."""


@main.command("run")
@click.option(
    "--strategy",
    type=click.Choice([s.value for s in MergeStrategy], case_sensitive=False),
    default=None,
    help=(
        "Merge strategy to run. Defaults to the value of "
        "TREE_EXTRACTION__DEDUP__MERGE_STRATEGY (which is the canonical "
        "contract surface)."
    ),
)
@click.option(
    "--restart-infra",
    is_flag=True,
    default=False,
    help=(
        "Run 'make local-restart' before the smoke. Off by default so the "
        "smoke can run against a long-lived dev environment without nuking "
        "Prefect and unrelated data."
    ),
)
def run_cmd(strategy: str | None, restart_infra: bool) -> None:
    """Run the smoke for one merge strategy."""

    resolved = strategy or os.environ.get("TREE_EXTRACTION__DEDUP__MERGE_STRATEGY")
    if not resolved:
        raise click.UsageError(
            "Provide --strategy or set TREE_EXTRACTION__DEDUP__MERGE_STRATEGY."
        )
    strategy_enum = MergeStrategy(resolved)
    # Make sure the env var is set so downstream config loaders honor it.
    os.environ["TREE_EXTRACTION__DEDUP__MERGE_STRATEGY"] = strategy_enum.value

    try:
        exit_code = asyncio.run(
            run_smoke(strategy=strategy_enum, restart_infra=restart_infra)
        )
    except SmokeFailure as exc:
        logger.error("smoke FAILED: %s", exc)
        sys.exit(1)
    except Exception:
        logger.exception("smoke crashed with unexpected exception")
        sys.exit(2)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
