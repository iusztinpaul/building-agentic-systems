"""Prefect flow-run helpers shared by the two pipeline dispatchers.

``tree.online.dispatch_online_pipeline`` and
``tree.offline.dispatch_offline_pipeline`` are peers — neither imports the
other — so the one thing they share, the status they report for a
just-created flow run, lives here (same "neutral top-level helper" rationale
as :mod:`tree.sharding`). It cannot live in :mod:`tree.orchestrator` (that
module imports BOTH dispatch modules — importing back would cycle) nor in
:mod:`tree.cli`, which is CLI glue for ``scripts/`` and sits ABOVE them.

Pure functions only: no Prefect API calls, no ``@flow``.
"""

from typing import Any


def flow_run_status(flow_run: Any) -> str:
    """Prefect's own state name, lowercased (``scheduled``, ``pending``, ...).

    ``run_deployment(..., timeout=0)`` returns the moment the run is CREATED,
    so the state is whatever the API assigned it then — ``scheduled``
    normally, ``pending`` when a worker has already picked it up — never a
    terminal one. Echoing Prefect's vocabulary verbatim keeps the dispatch
    result honest (a caller can look the run up under that exact state name)
    instead of asserting a hardcoded ``"submitted"`` that no API confirms.

    ``"unknown"`` when the run carries no state, which the API may briefly
    return for a fresh run — a status string is never worth an AttributeError
    on an already-created run.
    """

    state = getattr(flow_run, "state", None)
    if state is None or state.type is None:
        return "unknown"
    return str(state.type.value).lower()
