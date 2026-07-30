"""Shared ``run_deployment`` test doubles (#095).

Both coordinators now classify a shard by the TERMINAL STATE of the flow run
``run_deployment`` returns, so a fake that returns ``None`` is no longer a
faithful stand-in: the real entrypoint always returns a
:class:`~prefect.client.schemas.objects.FlowRun`. These two factories build that
return value once for every fake (imported directly).

They live in a plain module rather than a ``conftest.py`` because the root
conftest must set ``OPIK_TRACK_DISABLE`` before ANY import that could pull in
``opik``; keeping third-party imports out of it preserves that ordering.
"""

from __future__ import annotations

from uuid import uuid4

from prefect.client.schemas.objects import FlowRun, State, StateType


def flow_run_in_state(state_type: StateType, message: str | None = None) -> FlowRun:
    """A ``run_deployment`` return value that settled in ``state_type``."""

    return FlowRun(flow_id=uuid4(), state=State(type=state_type, message=message))


def completed_flow_run() -> FlowRun:
    """The happy-path ``run_deployment`` return value: a COMPLETED flow run."""

    return flow_run_in_state(StateType.COMPLETED)
