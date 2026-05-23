"""Unit tests for ``tree.orchestrator`` (#065, finalized #069).

Guards the one thing CI never exercises and the fan-out tests never reach:
the actual ``prefect.serve(...)`` invocation. ``make memory-serve-workflows``
crashed on startup because the runner-limit kwarg was named ``global_limit``,
which ``prefect.serve`` does not accept (it forwards through ``**kwargs`` into
``Runner.__init__``, which rejects it) — the parameter is ``limit`` in the
installed Prefect 3.6.19. We do NOT unit-test Prefect internals here; this is a
focused contract check on OUR call to ``serve``.

#069 finalizes the registered-deployment-name set after BOTH the memory (#067)
and data (#068) orchestrator/worker splits have landed: the registration
assertion now reflects the FINAL topology — the four new orchestrator/worker
deployments plus the unchanged indexing, ingest, and dream-cron deployments —
and the two retired single-flow deployments (``memory-extraction-etl``,
``data-pipeline-etl``) are asserted ABSENT. The #065 admission-control guards
(``limit`` not ``global_limit``; binding to the real ``prefect.serve``
signature) and the dream-cron schedule guard are preserved.
"""

from __future__ import annotations

import inspect

import prefect

from tree import orchestrator
from tree.config.app_config import app_config


def test_serve_deployments_passes_limit_not_global_limit(mocker):
    """``serve_deployments`` calls ``serve`` with ``limit=`` (NOT ``global_limit=``).

    The old code passed ``global_limit=...``, which is not a parameter of
    ``prefect.serve`` and blew up at runtime. This asserts the runner-limit kwarg
    is named ``limit`` and that ``global_limit`` is not among the kwargs.
    """

    # Arrange
    spy = mocker.patch("tree.orchestrator.serve")

    # Act
    orchestrator.serve_deployments(limit=4)

    # Assert
    spy.assert_called_once()
    call = spy.call_args
    assert call.kwargs.get("limit") == 4
    assert "global_limit" not in call.kwargs


def test_serve_deployments_kwargs_bind_to_real_serve_signature(mocker):
    """The kwargs we pass bind cleanly to the REAL ``prefect.serve`` signature.

    ``serve`` accepts ``**kwargs``, so binding alone would not reject a stray
    ``global_limit`` — but combined with the name assertion above, this guards
    against passing a runner-limit under any name the real signature would not
    route to its dedicated ``limit`` parameter.
    """

    # Arrange
    spy = mocker.patch("tree.orchestrator.serve")
    real_signature = inspect.signature(prefect.serve)

    # Act
    orchestrator.serve_deployments(limit=4)

    # Assert
    call = spy.call_args
    bound = real_signature.bind(*call.args, **call.kwargs)
    assert bound.arguments["limit"] == 4


def test_serve_deployments_registers_all_deployments(mocker):
    """The registered deployment set is EXACTLY the final post-split topology (#069).

    Compares the FULL set of registered deployment names (not a subset / membership
    check) so any future drift — a dropped, renamed, or accidentally added
    deployment — fails this test. After the memory (#067) and data (#068)
    orchestrator/worker splits, the set is the four new orchestrator/worker
    deployments plus the unchanged indexing, ingest, and dream deployments. The two
    retired single-flow deployments are explicitly asserted ABSENT.
    """

    # Arrange
    spy = mocker.patch("tree.orchestrator.serve")

    # Act
    orchestrator.serve_deployments(limit=4)

    # Assert
    call = spy.call_args
    deployment_names = {dep.name for dep in call.args}
    assert deployment_names == {
        # #068: the single ``data-pipeline-etl`` deployment is split into the
        # orchestrator + worker. The old name is GONE.
        "data-etl-orchestrator",
        "data-etl-worker",
        # #067: the single ``memory-extraction-etl`` deployment is split into the
        # orchestrator + worker. The old name is GONE.
        "memory-extract-etl-orchestrator",
        "memory-extract-etl-worker",
        "memory-indexing-etl",
        "ingest-file-etl",
        "ingest-conversation-etl",
        "ingest-youtube-video-batch-etl",
        "ingest-youtube-rss-feed-batch-etl",
        "dream-consolidation-etl",
    }
    # The two retired single-flow deployments must not linger in the registration.
    assert "memory-extraction-etl" not in deployment_names
    assert "data-pipeline-etl" not in deployment_names
    # The pre-#066 fan-out deployment is also gone.
    assert "memory-extraction-fanout-etl" not in deployment_names


def test_serve_deployments_registers_dream_with_its_cron(mocker):
    """The dream deployment carries its cron schedule, and ONLY it is scheduled.

    Preserves the #065 dream-cron guard and makes it robust: asserts the
    ``dream-consolidation-etl`` deployment is registered with the configured cron
    (``app_config.dream.cron``) and that no OTHER deployment is given a schedule —
    so a cron accidentally dropped from dream or attached to a worker/orchestrator
    deployment is caught.
    """

    # Arrange
    spy = mocker.patch("tree.orchestrator.serve")

    # Act
    orchestrator.serve_deployments(limit=4)

    # Assert
    call = spy.call_args
    crons_by_name = {
        dep.name: [
            schedule.schedule.cron
            for schedule in (dep.schedules or [])
            if getattr(schedule.schedule, "cron", None) is not None
        ]
        for dep in call.args
    }
    scheduled = {name: crons for name, crons in crons_by_name.items() if crons}
    assert scheduled == {"dream-consolidation-etl": [app_config.dream.cron]}
