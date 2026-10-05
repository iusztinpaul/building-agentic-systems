"""The ``graph_files`` collection: one row per **Graph file** (ADR-014 §4).

A rendered graph is written during ``tools/call`` and read back during a LATER
``resources/read``. Horizon gives neither instance affinity nor a durable
filesystem, so the bytes live here instead of on the server's disk: the gzip of
the self-contained HTML, keyed by the **Graph download** URI tail and scoped to
the user who rendered it.

Rows clean themselves up: a TTL index on ``created_at`` drops them after
``mcp.graph_file_ttl_seconds``. A plain document, not GridFS — the worst-case
gzip (~2.4 MB) is far under the 16 MB BSON limit.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document as BeanieDocument
from beanie import PydanticObjectId
from pydantic import Field, field_validator
from pymongo import IndexModel

from tree.config.app_config import app_config

GRAPH_FILES_COLLECTION = "graph_files"
"""Name of the collection holding every **Graph file** row — the ONE spelling."""

GRAPH_FILE_TTL_INDEX = "created_at_ttl"
"""Name of the TTL index on ``created_at``; ``collMod`` addresses it by name."""


def graph_file_indexes(ttl_seconds: int) -> list[IndexModel]:
    """The two indexes ``graph_files`` carries, with the TTL in seconds.

    Exactly two: the TTL, and the compound unique ``{name, user_id}`` the read
    uses. No separate index on ``name`` — the random token already makes it
    unique. :func:`tree.db.init_mongodb` rebinds ``GraphFile.Settings.indexes``
    through this builder when the boot-time reconcile keeps the live TTL.
    """

    return [
        IndexModel(
            [("created_at", 1)],
            expireAfterSeconds=ttl_seconds,
            name=GRAPH_FILE_TTL_INDEX,
        ),
        IndexModel(
            [("name", 1), ("user_id", 1)],
            unique=True,
            name="name_user_id_unique",
        ),
    ]


class GraphFile(BeanieDocument):
    """A rendered graph's gzipped HTML, readable by its owner until the TTL.

    Written by a graph tool's file branch and read by the ``graphs://{name}``
    resource for the **Request user**; a missing, foreign or expired row all
    read as "not found".
    """

    user_id: PydanticObjectId = Field(
        description="Tenant scope: only this user can read the row.",
    )
    name: str = Field(
        description=(
            "The Graph download URI tail, '<token>.html.gz', where the token is "
            "secrets.token_urlsafe(16) — unique by construction."
        ),
    )
    html_gz: bytes = Field(
        description="The gzip of the self-contained graph HTML (BSON binary).",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the row was written. Timezone-aware; the TTL counts from it.",
    )

    @field_validator("created_at", mode="after")
    @classmethod
    def _require_tz_aware(cls, value: datetime) -> datetime:
        """Reject naive datetimes: all datetimes are timezone aware (AGENTS.md)."""

        if value.tzinfo is None:
            raise ValueError(
                f"created_at must be timezone-aware (UTC); got naive datetime {value!r}"
            )
        return value

    class Settings:
        name = GRAPH_FILES_COLLECTION
        indexes = graph_file_indexes(app_config.mcp.graph_file_ttl_seconds)
