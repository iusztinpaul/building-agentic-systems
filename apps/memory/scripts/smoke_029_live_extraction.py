"""Live Gemini extraction smoke for #029.

Ingests a short ad-hoc document mentioning a person, an organization,
and a location, then runs the extraction pipeline with the live Gemini
LLM and reports the ``related_to`` edges that landed in
``knowledge_graph``. Intended for one-shot operator validation — not
wired into the test suite.

Usage::

    uv run python -m scripts.smoke_029_live_extraction

The script:
1. Ensures a smoke user exists.
2. Inserts a ``Document`` row with the smoke content.
3. Calls :func:`run_extraction_for_documents` directly (no Prefect
   worker required).
4. Prints every ``related_to`` edge written for the smoke document,
   grouped by ``semantic_type``.
"""

from __future__ import annotations

import asyncio
import logging

from beanie import PydanticObjectId

from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.documents import Document, SourceType
from tree.entities.users import User
from tree.logging import init_logger
from tree.memory.extraction.pipeline import run_extraction_for_documents

_SMOKE_CONTENT = (
    "Paul was hired by Anthropic as a research engineer in March 2024. "
    "The team is based in San Francisco. Anthropic was founded in 2021 "
    "and is headquartered in San Francisco as well. Paul knows Sarah, "
    "another engineer at the company."
)

_logger = logging.getLogger("smoke_029")


async def main() -> None:
    init_logger()
    print("[smoke029] booting", flush=True)

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    user_identifier = "smoke029@example.com"
    user = await User.find_one({"identifier": user_identifier})
    if user is None:
        user = User(identifier=user_identifier)
        await user.insert()
    assert user.id is not None
    print("smoke user: %s (id=%s)", user.identifier, user.id)

    # Insert a fresh document each run so caching doesn't hide
    # regressions in the LLM emission shape.
    doc = Document(
        title="029 smoke",
        content=_SMOKE_CONTENT,
        source_type=SourceType.CONVERSATION,
        source_uri=f"smoke://029-{PydanticObjectId()}",
        user_id=user.id,
        authors=["smoke"],
    )
    await doc.insert()
    print("inserted doc id=%s", doc.id)

    await run_extraction_for_documents(
        [str(doc.id)],
        user_id=user.id,
        client=client,
        database_name=settings.mongo.mongo_initdb_database,
    )

    coll = client[settings.mongo.mongo_initdb_database]["knowledge_graph"]
    related_edges_cursor = coll.find(
        {
            "user_id": user.id,
            "kind": "edge",
            "type": "related_to",
            "sources": doc.id,
        }
    )
    by_semantic: dict[str, list[dict]] = {}
    async for edge in related_edges_cursor:
        by_semantic.setdefault(edge.get("semantic_type") or "<missing>", []).append(
            edge
        )

    print("---- related_to edges for doc=%s ----", doc.id)
    if not by_semantic:
        print("[smoke029] WARNING: no related_to edges landed", flush=True)
    for semantic, edges in sorted(by_semantic.items()):
        print("  semantic=%s count=%d", semantic, len(edges))
        for edge in edges:
            print(
                "    %s [%s] -> %s",
                edge.get("source_node_id"),
                edge.get("semantic_type"),
                edge.get("target_node_id"),
            )

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
