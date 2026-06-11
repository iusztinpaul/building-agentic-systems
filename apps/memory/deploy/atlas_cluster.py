"""MongoDB Atlas cluster Infrastructure-as-Code.

Spin up, update, inspect, and tear down the shared Atlas cluster **entirely
through code** — no console clicking. The desired state lives in
:class:`ClusterSpec` (defaults mirror the current prod cluster: an M0 free-tier
replica set on GCP / ``WESTERN_EUROPE`` in project ``Tree``); the CLI reconciles
Atlas to that spec idempotently.

Auth is a MongoDB Atlas **Service Account** (OAuth2 client-credentials): set
``MDB_MCP_API_CLIENT_ID`` and ``MDB_MCP_API_CLIENT_SECRET`` (the same pair the
MongoDB MCP server uses) and nothing else is needed. The service account must
hold Project Cluster Manager + Database Access Admin + Network Access Manager
roles (or Project Owner) on the target project.

Commands::

    uv run python deploy/atlas_cluster.py up       # create cluster + db user + IP access (idempotent)
    uv run python deploy/atlas_cluster.py update   # PATCH cluster to match the spec (e.g. tier change)
    uv run python deploy/atlas_cluster.py status    # print state + connection strings
    uv run python deploy/atlas_cluster.py down      # delete the cluster

All commands accept ``--project``, ``--cluster``, ``--tier``, ``--provider``,
``--region`` to override the defaults, so the same code manages many clusters.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import time
from dataclasses import dataclass, field

import click
import httpx

from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

# Atlas Admin API v2. The resource-version date is REQUIRED on every call; an
# unknown date returns HTTP 406. Pin one stable date (bump deliberately).
ATLAS_BASE_URL = "https://cloud.mongodb.com/api/atlas/v2"
ATLAS_OAUTH_TOKEN_URL = "https://cloud.mongodb.com/api/oauth/token"
ATLAS_API_VERSION = "2025-03-12"
ATLAS_ACCEPT = f"application/vnd.atlas.{ATLAS_API_VERSION}+json"

# Dedicated tiers (M10+) use ``providerName`` directly; shared/free M0 is a
# TENANT cluster backed by a cloud provider.
FREE_TIER = "M0"

CLIENT_ID_ENV = "MDB_MCP_API_CLIENT_ID"
CLIENT_SECRET_ENV = "MDB_MCP_API_CLIENT_SECRET"


@dataclass(frozen=True)
class ClusterSpec:
    """Declarative desired state for one Atlas cluster.

    Defaults intentionally match the live prod cluster so ``up`` with no flags
    re-creates it exactly. ``db_username``/``db_password`` drive the seed
    database user; ``access_cidrs`` drive the project IP access list.
    """

    project_name: str = "Tree"
    cluster_name: str = "tree"
    tier: str = FREE_TIER
    provider: str = "GCP"
    region: str = "WESTERN_EUROPE"
    db_username: str | None = None
    db_password: str | None = None
    access_cidrs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_free_tier(self) -> bool:
        return self.tier.upper() == FREE_TIER

    def region_config(self) -> dict:
        """Build the ``regionConfigs[0]`` entry for this spec.

        Free tier (M0) is a TENANT cluster (``backingProviderName`` names the
        real cloud); dedicated tiers (M10+) name the provider directly and carry
        a 3-node electable set.
        """

        if self.is_free_tier:
            return {
                "providerName": "TENANT",
                "backingProviderName": self.provider,
                "regionName": self.region,
                "priority": 7,
                "electableSpecs": {"instanceSize": self.tier},
            }
        return {
            "providerName": self.provider,
            "regionName": self.region,
            "priority": 7,
            "electableSpecs": {"instanceSize": self.tier, "nodeCount": 3},
        }

    def create_body(self) -> dict:
        """Full cluster-create request body for this spec."""

        return {
            "name": self.cluster_name,
            "clusterType": "REPLICASET",
            "replicationSpecs": [{"regionConfigs": [self.region_config()]}],
        }

    def db_user_body(self) -> dict:
        """Seed database-user create body (SCRAM, admin auth db)."""

        return {
            "username": self.db_username,
            "password": self.db_password,
            "databaseName": "admin",
            "roles": [{"roleName": "readWriteAnyDatabase", "databaseName": "admin"}],
        }


class AtlasClient:
    """Thin Atlas Admin API v2 client with service-account OAuth2.

    Mints a bearer token from the client-credentials grant on construction and
    sets the mandatory versioned ``Accept`` header on every request. Methods map
    1:1 to the REST endpoints and surface the Atlas error envelope on failure.
    """

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._token = self._fetch_token(client_id, client_secret)
        self._http = httpx.Client(
            base_url=ATLAS_BASE_URL,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": ATLAS_ACCEPT,
                "Content-Type": ATLAS_ACCEPT,
            },
            timeout=30.0,
        )

    @staticmethod
    def _fetch_token(client_id: str, client_secret: str) -> str:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        resp = httpx.post(
            ATLAS_OAUTH_TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"grant_type": "client_credentials"},
            timeout=30.0,
        )
        if resp.status_code != httpx.codes.OK:
            raise click.ClickException(
                f"Atlas OAuth token request failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()["access_token"]

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = self._http.request(method, path, **kwargs)
        if resp.status_code >= httpx.codes.BAD_REQUEST:
            # Atlas error envelope: {error, errorCode, detail, reason}.
            raise click.ClickException(
                f"Atlas API {method} {path} -> {resp.status_code}: {resp.text}"
            )
        return resp

    # -- Projects -----------------------------------------------------------

    def get_project_id(self, project_name: str) -> str:
        resp = self._request("GET", f"/groups/byName/{project_name}")
        return resp.json()["id"]

    # -- Clusters -----------------------------------------------------------

    def get_cluster(self, project_id: str, cluster_name: str) -> dict | None:
        resp = self._http.get(f"/groups/{project_id}/clusters/{cluster_name}")
        if resp.status_code == httpx.codes.NOT_FOUND:
            return None
        if resp.status_code >= httpx.codes.BAD_REQUEST:
            raise click.ClickException(
                f"Atlas get cluster -> {resp.status_code}: {resp.text}"
            )
        return resp.json()

    def create_cluster(self, project_id: str, body: dict) -> dict:
        return self._request("POST", f"/groups/{project_id}/clusters", json=body).json()

    def update_cluster(self, project_id: str, cluster_name: str, body: dict) -> dict:
        return self._request(
            "PATCH", f"/groups/{project_id}/clusters/{cluster_name}", json=body
        ).json()

    def delete_cluster(self, project_id: str, cluster_name: str) -> None:
        self._request("DELETE", f"/groups/{project_id}/clusters/{cluster_name}")

    # -- Database users & access list --------------------------------------

    def ensure_db_user(self, project_id: str, body: dict) -> None:
        resp = self._http.post(f"/groups/{project_id}/databaseUsers", json=body)
        # 409 == user already exists; treat as idempotent success.
        if resp.status_code == httpx.codes.CONFLICT:
            logger.info("DB user %s already exists; skipping.", body.get("username"))
            return
        if resp.status_code >= httpx.codes.BAD_REQUEST:
            raise click.ClickException(
                f"Atlas create db user -> {resp.status_code}: {resp.text}"
            )

    def add_access_list(self, project_id: str, cidrs: tuple[str, ...]) -> None:
        if not cidrs:
            return
        entries = [{"cidrBlock": c, "comment": "tree IaC"} for c in cidrs]
        self._request("POST", f"/groups/{project_id}/accessList", json=entries)


def _load_credentials() -> tuple[str, str]:
    client_id = os.environ.get(CLIENT_ID_ENV)
    client_secret = os.environ.get(CLIENT_SECRET_ENV)
    if not client_id or not client_secret:
        raise click.ClickException(
            f"Set {CLIENT_ID_ENV} and {CLIENT_SECRET_ENV} (Atlas service-account credentials)."
        )
    return client_id, client_secret


def _wait_until_idle(
    client: AtlasClient, project_id: str, cluster_name: str, timeout_s: int = 900
) -> dict:
    """Poll the cluster until ``stateName == IDLE`` (create/update is async)."""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cluster = client.get_cluster(project_id, cluster_name)
        state = (cluster or {}).get("stateName")
        logger.info("Cluster %s state=%s", cluster_name, state)
        if state == "IDLE":
            return cluster
        time.sleep(15)
    raise click.ClickException(
        f"Cluster {cluster_name} did not reach IDLE within {timeout_s}s."
    )


def _spec_from_options(
    project: str, cluster: str, tier: str, provider: str, region: str
) -> ClusterSpec:
    return ClusterSpec(
        project_name=project,
        cluster_name=cluster,
        tier=tier,
        provider=provider,
        region=region,
        db_username=os.environ.get("MONGO_INITDB_ROOT_USERNAME"),
        db_password=os.environ.get("MONGO_INITDB_ROOT_PASSWORD"),
        access_cidrs=tuple(
            c.strip()
            for c in os.environ.get("ATLAS_ACCESS_CIDRS", "").split(",")
            if c.strip()
        ),
    )


# Shared CLI options so every command manages an arbitrary cluster, not a hardcoded one.
def _cluster_options(func):
    func = click.option("--project", default="Tree", show_default=True)(func)
    func = click.option("--cluster", default="tree", show_default=True)(func)
    func = click.option("--tier", default=FREE_TIER, show_default=True)(func)
    func = click.option("--provider", default="GCP", show_default=True)(func)
    func = click.option("--region", default="WESTERN_EUROPE", show_default=True)(func)
    return func


@click.group()
def cli() -> None:
    """MongoDB Atlas cluster Infrastructure-as-Code."""


@cli.command()
@_cluster_options
def up(project: str, cluster: str, tier: str, provider: str, region: str) -> None:
    """Create the cluster (idempotent) + seed DB user + IP access list."""

    spec = _spec_from_options(project, cluster, tier, provider, region)
    client = AtlasClient(*_load_credentials())
    project_id = client.get_project_id(spec.project_name)

    existing = client.get_cluster(project_id, spec.cluster_name)
    if existing is None:
        logger.info(
            "Creating cluster %s (%s/%s/%s)...", cluster, tier, provider, region
        )
        client.create_cluster(project_id, spec.create_body())
    else:
        logger.info(
            "Cluster %s already exists (state=%s).", cluster, existing.get("stateName")
        )

    if spec.db_username and spec.db_password:
        client.ensure_db_user(project_id, spec.db_user_body())
    client.add_access_list(project_id, spec.access_cidrs)

    final = _wait_until_idle(client, project_id, spec.cluster_name)
    srv = final.get("connectionStrings", {}).get("standardSrv", "<pending>")
    click.echo(srv)


@cli.command()
@_cluster_options
def update(project: str, cluster: str, tier: str, provider: str, region: str) -> None:
    """PATCH the cluster to match the spec (e.g. change tier/region)."""

    spec = _spec_from_options(project, cluster, tier, provider, region)
    client = AtlasClient(*_load_credentials())
    project_id = client.get_project_id(spec.project_name)
    logger.info("Updating cluster %s to match spec...", cluster)
    client.update_cluster(project_id, spec.cluster_name, spec.create_body())
    _wait_until_idle(client, project_id, spec.cluster_name)
    click.echo(f"Updated {cluster}.")


@cli.command()
@_cluster_options
def status(project: str, cluster: str, tier: str, provider: str, region: str) -> None:
    """Print the cluster state and connection strings."""

    client = AtlasClient(*_load_credentials())
    project_id = client.get_project_id(project)
    found = client.get_cluster(project_id, cluster)
    if found is None:
        click.echo(f"Cluster {cluster} does not exist in project {project}.")
        sys.exit(1)
    click.echo(f"state={found.get('stateName')} version={found.get('mongoDBVersion')}")
    click.echo(found.get("connectionStrings", {}).get("standardSrv", "<none>"))


@cli.command()
@_cluster_options
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def down(
    project: str, cluster: str, tier: str, provider: str, region: str, yes: bool
) -> None:
    """Delete the cluster (irreversible)."""

    if not yes:
        click.confirm(
            f"Delete Atlas cluster '{cluster}' in project '{project}'? This is irreversible.",
            abort=True,
        )
    client = AtlasClient(*_load_credentials())
    project_id = client.get_project_id(project)
    logger.info("Deleting cluster %s...", cluster)
    client.delete_cluster(project_id, cluster)
    click.echo(f"Delete requested for {cluster}.")


if __name__ == "__main__":
    cli()
