"""Unit tests for ``tree.flow_runs`` — the status both dispatchers report.

``run_deployment(..., timeout=0)`` hands back a just-CREATED flow run, so the
status a dispatcher returns must be whatever Prefect assigned that run rather
than a hardcoded string. These tests pin the vocabulary (Prefect's own state
name, lowercased) and the stateless-run edge.
"""

from types import SimpleNamespace

import pytest
from prefect.client.schemas.objects import State, StateType

from tree.flow_runs import flow_run_status


@pytest.mark.parametrize(
    ("state_type", "expected"),
    [
        (StateType.SCHEDULED, "scheduled"),
        (StateType.PENDING, "pending"),
        (StateType.RUNNING, "running"),
        (StateType.COMPLETED, "completed"),
    ],
)
def test_returns_prefects_state_name_lowercased(
    state_type: StateType, expected: str
) -> None:
    # Arrange
    flow_run = SimpleNamespace(id="run-1", state=State(type=state_type))

    # Act
    status = flow_run_status(flow_run)

    # Assert
    assert status == expected


def test_flow_run_without_a_state_is_unknown() -> None:
    # Arrange — the API may briefly return a fresh run with no state yet.
    flow_run = SimpleNamespace(id="run-1", state=None)

    # Act
    status = flow_run_status(flow_run)

    # Assert — a status string is never worth an AttributeError.
    assert status == "unknown"


def test_state_without_a_type_is_unknown() -> None:
    # Arrange
    flow_run = SimpleNamespace(id="run-1", state=SimpleNamespace(type=None))

    # Act
    status = flow_run_status(flow_run)

    # Assert
    assert status == "unknown"
