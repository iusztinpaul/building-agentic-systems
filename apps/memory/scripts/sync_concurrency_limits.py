"""
Sync the server-side ``voyage-embeddings`` Prefect global concurrency limit
(GCL) from ``app_config.concurrency`` (#054 / ADR-002).

YAML is the single source of truth: this script reads
``app_config.concurrency.voyage_rpm`` and issues a ``prefect gcl create``
(or ``update`` if the limit already exists) so the server-side limit always
matches config. Raising the cap after a payment method is added is: edit
``concurrency.voyage_rpm`` in ``configs/default.yaml`` and re-run this script —
no code change.

The limit derivation (ADR-002 §1):

    limit                  = voyage_rpm
    slot-decay-per-second  = voyage_rpm / 60

Requires:
    - Prefect server running (make local-start)

Usage:
    make memory-sync-concurrency-limits
    uv run python scripts/sync_concurrency_limits.py
"""

import logging
import subprocess
import sys

import click

from tree.config.app_config import app_config
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

LIMIT_NAME = "voyage-embeddings"


def _build_command(action: str, limit: int, slot_decay_per_second: float) -> list[str]:
    """Build the ``prefect gcl <action>`` argv for ``voyage-embeddings``.

    ``action`` is ``"create"`` or ``"update"``; both accept the same
    ``--limit`` / ``--slot-decay-per-second`` flags.
    """

    return [
        "prefect",
        "gcl",
        action,
        LIMIT_NAME,
        "--limit",
        str(limit),
        "--slot-decay-per-second",
        str(slot_decay_per_second),
    ]


def _limit_exists() -> bool:
    """True iff a ``voyage-embeddings`` GCL already exists on the server."""

    result = subprocess.run(
        ["prefect", "gcl", "inspect", LIMIT_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _run() -> None:
    voyage_rpm = app_config.concurrency.voyage_rpm
    slot_decay_per_second = voyage_rpm / 60

    action = "update" if _limit_exists() else "create"
    command = _build_command(action, voyage_rpm, slot_decay_per_second)

    logger.info(
        "Syncing GCL %r: limit=%d slot-decay-per-second=%.6f (action=%s)",
        LIMIT_NAME,
        voyage_rpm,
        slot_decay_per_second,
        action,
    )
    logger.info("Running: %s", " ".join(command))

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.stdout:
        logger.info(result.stdout.strip())
    if result.returncode != 0:
        logger.error(
            "prefect gcl %s failed (exit %d): %s",
            action,
            result.returncode,
            result.stderr.strip(),
        )
        sys.exit(1)

    logger.info("GCL %r synced successfully.", LIMIT_NAME)


@click.command()
def main() -> None:
    """Create/update the ``voyage-embeddings`` GCL from app_config."""

    _run()


if __name__ == "__main__":
    main()
