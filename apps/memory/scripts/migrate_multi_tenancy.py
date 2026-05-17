"""
One-shot Phase-1 multi-tenancy migration.

Bootstraps a pre-multi-tenancy ``tree`` deployment into the post-Phase-1
schema:

    1. Find-or-create the seed :class:`User` (`identifier`, optional
       display ``name`` carried into ``attributes``). On INSERT the
       ``after_insert`` hook upserts the ``person:self`` node — into the
       soon-to-be-dropped ``knowledge_graph`` collection. That's fine;
       step 4 re-creates it.
    2. ``documents.update_many({}, {"$set": {"user_id": seed.id}})`` —
       backfill every existing ``Document``. Aborts if the collection
       already has more than one distinct ``user_id`` populated (the
       script is a one-shot bootstrap, NOT a multi-tenant rebalance).
    3. ``db.knowledge_graph.drop()`` — wipe the KG; extraction will
       rebuild it.
    4. Re-fire ``seed.after_insert()`` to land a fresh ``person:self``
       node post-drop. Idempotent ``$setOnInsert`` upsert keyed by ``_id``.
    4.5. Run ``ensure_indexes`` inline on ``knowledge_graph`` so the
       freshly created collection ships with text, vector, and
       ``user_id``-prefixed compound indexes immediately. Without this
       inline call the ``person:self`` row from step 4 would sit in an
       unindexed collection until the fire-and-forget indexing
       deployment in step 5 caught up — this step makes the migration
       script's exit state self-sufficient (#023 Nit 4).
    5. Trigger Prefect deployments (``memory-extraction-etl``,
       ``memory-indexing-etl``) for ``user_id=seed.id``. The script prints
       deployment-run IDs and the Prefect-UI links so the operator can
       poll progress. We do NOT block on completion — the operator can
       follow the run in the UI or use ``make memory-run-memory-pipeline-*``.

Idempotency notes:

* Step 1 re-uses an existing ``User`` keyed by ``identifier``.
* Step 2 ``$set``s ``user_id`` even when documents already carry it — a
  no-op write.
* Step 3 drops a possibly-empty collection — safe.
* Step 4 is an idempotent ``$setOnInsert`` upsert.
* Step 5 re-triggers Prefect deployments; the pipelines are idempotent
  by design (per ``CLAUDE.md``).

Dry-run mode prints a plan with counts and exits without writing.

Usage::

    make memory-migrate-multi-tenancy USER_IDENTIFIER=dev@example.com
    make memory-migrate-multi-tenancy USER_IDENTIFIER=dev@example.com DRY_RUN=1
    uv run python scripts/migrate_multi_tenancy.py \\
        --identifier dev@example.com --name "Dev User"
    uv run python scripts/migrate_multi_tenancy.py \\
        --identifier dev@example.com --dry-run
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import click
from beanie import PydanticObjectId

from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.documents import Document
from tree.entities.knowledge_graph import KnowledgeGraphEntry
from tree.entities.users import User
from tree.logging import init_logger
from tree.memory.indexing.core import ensure_indexes
from tree.models.get_model import get_embedding_model

init_logger()
logger = logging.getLogger(__name__)


_KG_COLLECTION = "knowledge_graph"
_EXTRACTION_DEPLOYMENT = "memory-extraction-etl/memory-extraction-etl"
_INDEXING_DEPLOYMENT = "memory-indexing-etl/memory-indexing-etl"


class MigrationAbort(RuntimeError):
    """Raised when the migration cannot safely run (e.g. multi-tenant data)."""


# ---------------------------------------------------------------------------
# Discovery + planning
# ---------------------------------------------------------------------------


async def _count_documents() -> int:
    """Total documents in the ``documents`` collection."""

    col = Document.get_pymongo_collection()
    return await col.count_documents({})


async def _count_kg_entries() -> int:
    """Total rows in the ``knowledge_graph`` collection."""

    col = KnowledgeGraphEntry.get_pymongo_collection()
    return await col.count_documents({})


async def _distinct_document_user_ids() -> list:
    """Distinct ``user_id`` values currently set on ``documents`` (None included)."""

    col = Document.get_pymongo_collection()
    return await col.distinct("user_id")


async def _assert_safe_to_migrate(seed_user_id: PydanticObjectId) -> None:
    """Refuse to run when ``documents`` is already multi-tenant.

    A populated ``user_id`` is fine ONLY when every populated value
    matches ``seed_user_id`` (idempotent re-run). More than one distinct
    populated value means real cross-tenant data exists and this
    bootstrap script would either corrupt it or surface confusing state.
    """

    distinct = await _distinct_document_user_ids()
    # Treat None / missing as "unmigrated"; everything else as a tenant id.
    populated = [v for v in distinct if v is not None]
    extras = [v for v in populated if v != seed_user_id]
    if extras:
        sample = ", ".join(str(v) for v in extras[:5])
        raise MigrationAbort(
            "Refusing to migrate: the ``documents`` collection already carries "
            f"user_id values different from the seed user ({sample}). This "
            "script is a one-shot bootstrap for a pre-multi-tenancy deployment, "
            "not a multi-tenant rebalance."
        )


# ---------------------------------------------------------------------------
# Step 1 — Seed user
# ---------------------------------------------------------------------------


async def _find_or_create_seed_user(
    identifier: str, name: str | None
) -> tuple[User, bool]:
    """Return ``(user, created)`` for the seed identity.

    On INSERT, the ``User.after_insert`` hook auto-upserts the user's
    ``person:self`` KG node. We let that happen so the user is
    well-formed *during* the migration, even though step 3 will drop the
    KG and step 4 will re-create the self-person node.
    """

    existing = await User.find_one(User.identifier == identifier)
    if existing is not None:
        logger.info(
            "Seed user already exists: identifier=%s id=%s",
            identifier,
            existing.id,
        )
        return existing, False

    attributes: dict[str, object] = {}
    if name:
        attributes["name"] = name

    user = User(identifier=identifier, attributes=attributes)
    await user.insert()
    logger.info("Seed user CREATED: identifier=%s id=%s", identifier, user.id)
    return user, True


# ---------------------------------------------------------------------------
# Step 2 — Backfill documents
# ---------------------------------------------------------------------------


async def _backfill_documents(seed_user_id: PydanticObjectId) -> int:
    """Stamp every existing ``Document`` row with ``user_id=seed_user_id``."""

    col = Document.get_pymongo_collection()
    result = await col.update_many({}, {"$set": {"user_id": seed_user_id}})
    logger.info(
        "documents.update_many: matched=%d modified=%d",
        result.matched_count,
        result.modified_count,
    )
    return result.matched_count


# ---------------------------------------------------------------------------
# Step 3 — Drop knowledge_graph
# ---------------------------------------------------------------------------


async def _drop_knowledge_graph() -> None:
    """Drop the ``knowledge_graph`` collection (and its indexes)."""

    col = KnowledgeGraphEntry.get_pymongo_collection()
    await col.database.drop_collection(_KG_COLLECTION)
    logger.info("knowledge_graph: collection dropped.")


# ---------------------------------------------------------------------------
# Step 4 — Re-create self-person node post-drop
# ---------------------------------------------------------------------------


async def _refire_self_person(user: User) -> None:
    """Re-run ``after_insert`` to land the user's ``person:self`` post-drop."""

    await user.after_insert()
    logger.info("Self-person node re-created for user_id=%s.", user.id)


# ---------------------------------------------------------------------------
# Step 4.5 — Ensure indexes inline (so the KG is queryable immediately)
# ---------------------------------------------------------------------------


async def _ensure_kg_indexes(client, user_id: PydanticObjectId) -> None:
    """Re-create classic + search indexes on the freshly dropped collection.

    Step 3 dropped ``knowledge_graph`` (and every index on it). Step 5
    fires the indexing pipeline as a fire-and-forget Prefect deployment
    (``# We do NOT block on completion``), so without this inline call
    the ``person:self`` node written in step 4 sits in a collection with
    no text/vector/compound indexes until the operator separately polls
    the deployment. Running ``ensure_indexes`` inline here makes the
    collection queryable the moment the migration script returns.
    Idempotent: the indexing pipeline re-issues the same ``ensure_indexes``
    call shortly after — both runs converge on the same shape.
    """

    database = settings.mongo.mongo_initdb_database
    embedding_model = get_embedding_model()
    await ensure_indexes(
        client, database, embedding_model=embedding_model, user_id=user_id
    )
    logger.info("knowledge_graph indexes ensured inline (text + vector + compound).")


# ---------------------------------------------------------------------------
# Step 5 — Trigger Prefect deployments
# ---------------------------------------------------------------------------


async def _trigger_pipelines(seed_user_id: PydanticObjectId) -> None:
    """Kick off extraction + indexing Prefect deployments for the seed user.

    We do NOT block on completion — the operator follows the run via the
    Prefect UI (URL printed below) or via the dedicated
    ``make memory-run-memory-pipeline-*`` scripts that stream logs.
    """

    try:
        from prefect.client.orchestration import get_client
    except ImportError as exc:  # pragma: no cover — Prefect is a hard dep.
        raise RuntimeError(
            "prefect.client.orchestration is required to trigger pipelines"
        ) from exc

    async with get_client() as client:
        for deployment_name in (_EXTRACTION_DEPLOYMENT, _INDEXING_DEPLOYMENT):
            try:
                deployment = await client.read_deployment_by_name(deployment_name)
            except Exception:
                logger.warning(
                    "Deployment %r not registered. Run "
                    "``make memory-serve-workflows`` first, then re-trigger "
                    "manually via ``make memory-run-memory-pipeline-*``.",
                    deployment_name,
                )
                continue

            flow_run = await client.create_flow_run_from_deployment(
                deployment_id=deployment.id,
                parameters={"user_id": str(seed_user_id)},
            )
            base_url = str(client.api_url).rstrip("/").removesuffix("/api")
            logger.info(
                "Triggered %s — flow_run_id=%s — track at %s/runs/flow-run/%s",
                deployment_name,
                flow_run.id,
                base_url,
                flow_run.id,
            )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _print_dry_run_plan(
    identifier: str, name: str | None, seed_user: User | None
) -> None:
    """Print a non-destructive plan with counts."""

    doc_count = await _count_documents()
    kg_count = await _count_kg_entries()
    distinct = await _distinct_document_user_ids()
    populated = [v for v in distinct if v is not None]

    logger.info("DRY RUN — no writes will be performed.")
    if seed_user is None:
        logger.info(
            "Step 1: would CREATE seed User(identifier=%r, attributes={'name': %r}).",
            identifier,
            name,
        )
    else:
        logger.info(
            "Step 1: seed User exists (identifier=%s id=%s); would REUSE.",
            identifier,
            seed_user.id,
        )
    logger.info(
        "Step 2: would backfill user_id on %d document(s) "
        "(distinct populated user_ids today: %d).",
        doc_count,
        len(populated),
    )
    logger.info(
        "Step 3: would DROP knowledge_graph (current row count: %d).",
        kg_count,
    )
    logger.info(
        "Step 4: would re-fire self-person upsert for seed user "
        "(post-drop, idempotent).",
    )
    logger.info(
        "Step 4.5: would ensure knowledge_graph indexes inline (text + "
        "vector + compound) so the collection is queryable immediately "
        "after the migration returns."
    )
    logger.info(
        "Step 5: would trigger Prefect deployments %s and %s with user_id.",
        _EXTRACTION_DEPLOYMENT,
        _INDEXING_DEPLOYMENT,
    )


async def _run_migration(
    identifier: str,
    name: str | None,
    dry_run: bool,
    trigger_pipelines: bool,
) -> User:
    """Execute the full migration end-to-end.

    Returns the seed ``User`` so callers (tests) can inspect downstream
    state. ``trigger_pipelines`` is exposed so tests can skip the Prefect
    side-effect; the CLI always passes ``True``.
    """

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    if dry_run:
        # Try to peek at an existing seed user without creating one.
        existing = await User.find_one(User.identifier == identifier)
        await _print_dry_run_plan(identifier, name, existing)
        if existing is None:
            # Synthesize an in-memory user for the abort check — we cannot
            # safely insert one in dry-run.
            return User(
                identifier=identifier, attributes={"name": name} if name else {}
            )
        return existing

    # Step 1 first so we have a seed_user.id to gate the safety check
    # against (idempotent re-runs of this script must not abort).
    seed_user, _created = await _find_or_create_seed_user(identifier, name)

    await _assert_safe_to_migrate(seed_user.id)

    matched = await _backfill_documents(seed_user.id)
    logger.info("Step 2 complete: %d documents backfilled.", matched)

    await _drop_knowledge_graph()
    logger.info("Step 3 complete: knowledge_graph dropped.")

    await _refire_self_person(seed_user)
    logger.info("Step 4 complete: self-person node re-created.")

    await _ensure_kg_indexes(client, seed_user.id)
    logger.info(
        "Step 4.5 complete: knowledge_graph indexes ensured inline (so "
        "subsequent queries hit text/vector/compound indexes immediately, "
        "without waiting for the fire-and-forget indexing deployment in "
        "step 5)."
    )

    if trigger_pipelines:
        await _trigger_pipelines(seed_user.id)
        logger.info("Step 5 complete: pipelines triggered.")
    else:
        logger.info("Step 5 skipped (trigger_pipelines=False).")

    logger.info(
        "Migration complete. Seed user_id=%s identifier=%s.",
        seed_user.id,
        seed_user.identifier,
    )
    return seed_user


@click.command()
@click.option(
    "--identifier",
    required=True,
    help="Seed user identifier (e.g. email or OIDC sub). Free string.",
)
@click.option(
    "--name",
    default=None,
    help="Display name for the seed user. Stored in attributes.name.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the migration plan with counts; perform no writes.",
)
@click.option(
    "--no-trigger-pipelines",
    is_flag=True,
    default=False,
    help=(
        "Skip the Prefect deployment trigger (step 5). Useful when the "
        "Prefect worker is not running and the operator will trigger the "
        "pipelines manually via make memory-run-memory-pipeline-*."
    ),
)
def main(
    identifier: str, name: str | None, dry_run: bool, no_trigger_pipelines: bool
) -> None:
    """One-shot Phase-1 multi-tenancy migration. See module docstring."""

    # Allow USER_IDENTIFIER env fallback so the Makefile target stays
    # consistent with the data-pipeline scripts' convention.
    if not identifier:  # click guarantees non-empty, defensive only
        identifier = os.environ.get("USER_IDENTIFIER", "")
    if not identifier:
        logger.error("--identifier is required (or set USER_IDENTIFIER env).")
        raise SystemExit(1)

    try:
        asyncio.run(
            _run_migration(
                identifier=identifier,
                name=name,
                dry_run=dry_run,
                trigger_pipelines=not no_trigger_pipelines,
            )
        )
    except MigrationAbort as exc:
        logger.error("Migration aborted: %s", exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
