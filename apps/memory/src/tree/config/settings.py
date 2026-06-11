import os
from typing import Literal

from pydantic import SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

_env_file = os.environ.get("ENV_FILE_PATH", ".env")


class MongoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=_env_file, extra="ignore")

    # "mongodb" targets the local single-node replica set; "mongodb+srv"
    # targets an Atlas cluster (TLS implied, MONGO_PORT ignored — SRV URIs
    # forbid ports and directConnection).
    mongo_scheme: Literal["mongodb", "mongodb+srv"] = "mongodb"
    mongo_host: str = "localhost"
    mongo_port: int = 27017
    mongo_initdb_root_username: str = "tree"
    mongo_initdb_root_password: SecretStr = SecretStr("tree")
    mongo_initdb_database: str = "tree"
    mongot_port: int = 27028

    @computed_field
    @property
    def mongo_uri(self) -> SecretStr:
        username = self.mongo_initdb_root_username
        password = self.mongo_initdb_root_password.get_secret_value()
        if self.mongo_scheme == "mongodb+srv":
            return SecretStr(
                f"mongodb+srv://{username}:{password}@{self.mongo_host}"
                f"/?retryWrites=true&w=majority"
            )
        return SecretStr(
            f"mongodb://{username}:{password}"
            f"@{self.mongo_host}:{self.mongo_port}"
            f"/?directConnection=true&authSource=admin"
        )


class Settings(BaseSettings):
    """Credentials- and infrastructure-only settings.

    The project's split is:

    * **``.env`` / ``Settings``** — credentials (API keys) and per-environment
      infrastructure endpoints (Mongo host/port, Prefect URL). This module.
    * **``apps/memory/configs/default.yaml`` / ``app_config``** — behavior
      knobs (model names, chunk sizes, dedup thresholds, etc.). See
      :mod:`tree.config.app_config`.

    The rule is enforced by keeping behavior knobs out of this class.
    A new tunable goes in YAML + ``app_config.py``; a new credential or
    infra endpoint goes here and in ``.env.example``.

    See ``tracker/034-voyage-3-yaml-default.groomed.md`` for the migration
    that codified the split and the ``## Configuration`` section in
    ``CLAUDE.md``.
    """

    model_config = SettingsConfigDict(env_prefix="", env_file=_env_file, extra="ignore")

    mongo: MongoSettings = MongoSettings()
    google_api_key: SecretStr = SecretStr("")
    voyage_api_key: SecretStr = SecretStr("")
    modal_embedding_api_key: SecretStr = SecretStr("")
    brightdata_api_key: SecretStr = SecretStr("")
    brightdata_unlocker_zone: str = ""
    brightdata_serp_zone: str = ""
    # Opik observability (monitoring only). All optional: with no
    # ``opik_api_key`` the observability layer no-ops cleanly (decorators
    # inert, nothing breaks). ``opik_workspace`` is optional (defaults to the
    # API key's default workspace); ``opik_project_name`` defaults to
    # ``"tree-memory"``.
    opik_api_key: SecretStr = SecretStr("")
    opik_workspace: str = ""
    opik_project_name: str = "tree-memory"
    # Prefect Horizon (FastMCP Cloud) — the managed hosting for the
    # ``tree-memory`` MCP server. ``tree_memory_cloud_url`` is the deployed
    # server endpoint (``https://<name>.fastmcp.app/mcp``). The server is
    # protected by Horizon Authentication (OAuth + org membership), so clients
    # authenticate via the browser OAuth flow — there is no static token to
    # configure here (see :func:`tree.mcp.client.get_cloud_client`).
    tree_memory_cloud_url: str = "https://tree-memory.fastmcp.app/mcp"
    # Skip the index-bootstrap (ensure_indexes + live vector-index assertion)
    # in the MCP lifespan. On a serverless host (Prefect Horizon / Lambda) that
    # boot work — creating the Atlas vector index and polling mongot until it
    # syncs — can take minutes and blocks port readiness past the host's 60s
    # window, so the server never starts. Indexes are created by the indexing
    # pipeline; the MCP server only queries, so it can assume they exist. Set
    # MCP_SKIP_INDEX_BOOTSTRAP=true on Horizon. Local default keeps the
    # self-healing boot behaviour.
    mcp_skip_index_bootstrap: bool = False


settings = Settings()
