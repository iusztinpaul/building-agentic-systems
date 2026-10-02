"""Insert a freshly ingested Document, upgrading a LATENT placeholder that won the race.

Every loader checks ``(user_id, source_uri)`` with ``find_one`` before inserting, but a
batch loads its items concurrently (``gather_isolated``): while article A is between its
``find_one`` and its ``insert``, article B's reference resolution can insert a LATENT
placeholder for A. The ``(user_id, source_uri)`` unique index (ADR-010) then rejects A's
real row. Skipping on that ``DuplicateKeyError`` would leave only the placeholder — the
article is silently lost. This helper upgrades the placeholder in place instead, the
same ``replace`` the loaders already do when ``find_one`` sees a LATENT row.
"""

import logging

from pymongo.errors import DuplicateKeyError

from tree.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)


async def insert_or_upgrade_latent(doc: Document) -> Document | None:
    """Insert ``doc``; on a unique-key race, upgrade a LATENT placeholder in place.

    Returns the persisted ``doc``, or ``None`` when a real (non-LATENT) row for the
    same ``(user_id, source_uri)`` won the race — a clean duplicate skip.
    """

    try:
        await doc.insert()
        return doc
    except DuplicateKeyError:
        existing = await Document.find_one(
            {"user_id": doc.user_id, "source_uri": doc.source_uri}
        )
        if existing is None or existing.source_type != SourceType.LATENT:
            logger.debug("Skipping concurrent duplicate: %s", doc.source_uri)
            return None

    doc.id = existing.id
    await doc.replace()
    logger.info("Upgraded concurrently created latent document: %s", doc.source_uri)
    return doc
