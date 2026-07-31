"""Prefect Cloud pipeline Infrastructure-as-Code (Managed work pool).

Provision the **persistent worker** side of the pipelines — entirely through code,
mirroring ``deploy/atlas_cluster.py``. Where ``atlas_cluster.py`` owns the MongoDB
cluster, this owns the Prefect Cloud **Managed work pool**, the config blocks the
flows read at run time, and the 5 deployments bound to that pool. Once ``up`` runs,
Prefect hosts the workers, so flow runs the MCP submits (async ingestion) execute
without any self-hosted ``serve`` process.

Split of responsibilities:

* **This script** (``up`` / ``update`` / ``status`` / ``down``) owns the *infra*:
  the Managed work pool, the Secret blocks + Variables that carry runtime config,
  and the deployment definitions (git-sourced from ``main``).
* **The CD path** (``deploy/prefect_pipelines.py``) only pushes *code/spec updates*
  to the same deployments — it reuses :func:`managed_env_templates` (whose values
  are block/variable references, never raw secrets) so CI needs only the Prefect
  API creds.

Local dev is unaffected: ``make memory-serve-workflows`` keeps serving LOCAL code
against the LOCAL Prefect server (``make local-start``). Do NOT run that local
``serve`` against the SAME Prefect Cloud workspace as ``up`` — both register the
same deployment names and would clobber each other (``status`` shows a clobbered
deployment as having no work pool; re-run ``up``/``update`` to restore it).

Auth: ``PREFECT_API_URL`` + ``PREFECT_API_KEY`` (Prefect Cloud) and ``GITHUB_PAT``
(a GitHub PAT with read access to this private repo, so Prefect can clone the flow
code). ``up`` also reads the app's runtime config from the environment to seed the
blocks/variables (Mongo creds, Voyage/Gemini/Bright Data/Opik keys).

Commands::

    uv run python deploy/prefect_pipelines_setup.py up        # pool + blocks + deployments (idempotent)
    uv run python deploy/prefect_pipelines_setup.py update    # re-deploy code/spec only
    uv run python deploy/prefect_pipelines_setup.py status    # print pool + deployment bindings
    uv run python deploy/prefect_pipelines_setup.py down      # delete deployments + pool

Every verb takes ``--groups data|memory`` (comma-separated, defaults to the
``GROUPS`` env var) to scope it to whole pipelines; unset means all.
"""

from __future__ import annotations

import asyncio
import logging
import os

import click
import httpx
from prefect.blocks.system import Secret
from prefect.client.orchestration import get_client
from prefect.client.schemas.actions import WorkPoolCreate
from prefect.exceptions import ObjectAlreadyExists, ObjectNotFound
from prefect.variables import Variable

from tree.logging import init_logger
from tree.orchestrator import (
    GIT_URL,
    MANAGED_WORK_POOL,
    PAT_BLOCK_NAME,
    RUNTIME_CONFIG,
    deploy_cloud_pipelines,
    deployment_full_names,
    managed_env_templates,
)

init_logger()
logger = logging.getLogger(__name__)

MANAGED_POOL_TYPE = "prefect:managed"
GITHUB_PAT_ENV = "GITHUB_PAT"
# ``https://github.com/<owner>/<repo>.git`` → ``https://api.github.com/repos/<owner>/<repo>``
_GITHUB_API_REPO = "https://api.github.com/repos/" + GIT_URL.removeprefix(
    "https://github.com/"
).removesuffix(".git")


def _verify_pat_access(pat: str) -> None:
    """Fail fast (with actionable guidance) if the PAT can't reach the repo.

    Prefect Managed clones the private repo at deploy AND run time; a token that
    can't read it surfaces only as a cryptic ``git clone ... exit code 128`` deep
    inside ``from_source``. A quick GitHub API probe turns that into a clear error.
    """

    resp = httpx.get(
        _GITHUB_API_REPO,
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15.0,
    )
    if resp.status_code != httpx.codes.OK:
        raise click.ClickException(
            f"{GITHUB_PAT_ENV} cannot access {_GITHUB_API_REPO} (HTTP "
            f"{resp.status_code}). Prefect Managed must CLONE this private repo. "
            "Use a CLASSIC PAT with the 'repo' scope, OR a FINE-GRAINED PAT whose "
            "Resource owner is the repo owner, with this repository selected and "
            "'Contents: Read-only' permission."
        )


def _seed_config_stores() -> None:
    """Seed the Secret blocks + Variables (and the GitHub PAT block) from env.

    Idempotent (``overwrite=True``). Run only by ``up`` — the operator has the
    full ``.env``; the CD path never touches these.
    """

    pat = os.environ.get(GITHUB_PAT_ENV)
    if not pat:
        raise click.ClickException(
            f"Set {GITHUB_PAT_ENV} (a GitHub PAT with read access to the private "
            "repo) so Prefect Managed can clone the flow code."
        )
    _verify_pat_access(pat)
    Secret(value=pat).save(PAT_BLOCK_NAME, overwrite=True)
    logger.info("Saved Secret block %s (GitHub PAT)", PAT_BLOCK_NAME)

    for store_name, var, is_secret in RUNTIME_CONFIG:
        value = os.environ.get(var, "")
        if not value:
            logger.warning("Env %s is empty; seeding %s blank", var, store_name)
        if is_secret:
            Secret(value=value).save(store_name, overwrite=True)
        else:
            Variable.set(store_name, value, overwrite=True)
    logger.info("Seeded %d runtime config store(s).", len(RUNTIME_CONFIG))


async def _ensure_work_pool(name: str) -> None:
    """Create the Managed work pool if absent (read-first).

    Read-then-create rather than create-then-catch: on the free tier the
    workspace work-pool limit is 1, and the create endpoint enforces that limit
    BEFORE the duplicate check — so blindly re-creating an existing pool returns
    403 (limit reached), not the 409 we could swallow. Reading first skips the
    create entirely when the pool already exists.
    """

    async with get_client() as client:
        try:
            await client.read_work_pool(name)
            logger.info("Work pool %r already exists.", name)
            return
        except ObjectNotFound:
            pass
        try:
            await client.create_work_pool(
                WorkPoolCreate(name=name, type=MANAGED_POOL_TYPE)
            )
            logger.info("Created %s work pool %r.", MANAGED_POOL_TYPE, name)
        except ObjectAlreadyExists:
            logger.info("Work pool %r already exists (created concurrently).", name)


async def _status(work_pool: str, groups: tuple[str, ...]) -> None:
    async with get_client() as client:
        try:
            pool = await client.read_work_pool(work_pool)
            click.echo(f"work pool: {pool.name} type={pool.type} status={pool.status}")
        except ObjectNotFound:
            click.echo(f"work pool: {work_pool} <missing — run `up`>")
        for full_name in deployment_full_names(groups):
            try:
                dep = await client.read_deployment_by_name(full_name)
                pool_name = dep.work_pool_name or "<none — clobbered, re-run up>"
                click.echo(f"  {full_name}: work_pool={pool_name}")
            except ObjectNotFound:
                click.echo(f"  {full_name}: <missing>")


async def _down(work_pool: str, purge_blocks: bool, groups: tuple[str, ...]) -> None:
    async with get_client() as client:
        for full_name in deployment_full_names(groups):
            try:
                dep = await client.read_deployment_by_name(full_name)
                await client.delete_deployment(dep.id)
                logger.info("Deleted deployment %s", full_name)
            except ObjectNotFound:
                logger.info("Deployment %s already absent", full_name)
        # A group-scoped teardown leaves the pool alone — the other group's
        # deployments may still be bound to it.
        if groups:
            logger.info("Kept work pool %s (group-scoped down).", work_pool)
        else:
            try:
                await client.delete_work_pool(work_pool)
                logger.info("Deleted work pool %s", work_pool)
            except ObjectNotFound:
                logger.info("Work pool %s already absent", work_pool)
    if purge_blocks:
        logger.warning(
            "Block/variable purge not automated — delete %s and the tree-* "
            "blocks/variables in the Prefect UI if desired.",
            PAT_BLOCK_NAME,
        )


def _pool_option(func):
    return click.option("--work-pool", default=MANAGED_WORK_POOL, show_default=True)(
        func
    )


def _parse_groups(ctx, param, value: str) -> tuple[str, ...]:
    """``--groups data,memory`` → ``("data", "memory")``; empty → all.

    Validates here, before any verb's side effects, so a typo (``GROUPS=dta``)
    fails on the parse instead of seeding blocks and then deploying nothing.
    """

    groups = tuple(g.strip() for g in value.split(",") if g.strip())
    try:
        deployment_full_names(groups)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc
    return groups


def _groups_option(func):
    return click.option(
        "--groups",
        envvar="GROUPS",
        default="",
        callback=_parse_groups,
        help="Comma-separated pipeline groups (data|memory). Empty = all.",
    )(func)


@click.group()
def cli() -> None:
    """Prefect Cloud pipeline IaC (Managed work pool)."""


@cli.command()
@_pool_option
@_groups_option
@click.option("--git-ref", default="main", show_default=True, help="Branch or commit.")
def up(work_pool: str, groups: tuple[str, ...], git_ref: str) -> None:
    """Provision blocks + Managed work pool + deployments (idempotent)."""

    _seed_config_stores()
    asyncio.run(_ensure_work_pool(work_pool))
    ids = deploy_cloud_pipelines(
        work_pool_name=work_pool,
        git_ref=git_ref,
        job_env=managed_env_templates(),
        groups=groups,
    )
    click.echo(f"Deployed {len(ids)} pipeline(s) to {work_pool}: {', '.join(ids)}")


@cli.command()
@_pool_option
@_groups_option
@click.option("--git-ref", default="main", show_default=True, help="Branch or commit.")
def update(work_pool: str, groups: tuple[str, ...], git_ref: str) -> None:
    """Re-deploy code/spec only (assumes the pool + blocks already exist)."""

    ids = deploy_cloud_pipelines(
        work_pool_name=work_pool,
        git_ref=git_ref,
        job_env=managed_env_templates(),
        groups=groups,
    )
    click.echo(f"Updated {len(ids)} pipeline(s) on {work_pool}.")


@cli.command()
@_pool_option
@_groups_option
def status(work_pool: str, groups: tuple[str, ...]) -> None:
    """Print the work pool + each deployment's work-pool binding."""

    asyncio.run(_status(work_pool, groups))


@cli.command()
@_pool_option
@_groups_option
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--purge-blocks", is_flag=True, help="Also note block cleanup.")
def down(
    work_pool: str, groups: tuple[str, ...], yes: bool, purge_blocks: bool
) -> None:
    """Delete the deployments (+ the Managed work pool when unscoped)."""

    scope = f"the {', '.join(groups)} " if groups else "all tree "
    pool_note = "" if groups else f" and the '{work_pool}' work pool"
    if not yes:
        click.confirm(f"Delete {scope}deployments{pool_note}?", abort=True)
    asyncio.run(_down(work_pool, purge_blocks, groups))
    click.echo(f"Tore down {scope}deployments{pool_note}.")


if __name__ == "__main__":
    cli()
