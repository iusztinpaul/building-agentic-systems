"""Unit tests for ``tree.orchestrator`` (#065).

Guards the one thing CI never exercises and the fan-out tests never reach:
the actual ``prefect.serve(...)`` invocation. ``make memory-serve-workflows``
crashed on startup because the runner-limit kwarg was named ``global_limit``,
which ``prefect.serve`` does not accept (it forwards through ``**kwargs`` into
``Runner.__init__``, which rejects it) — the parameter is ``limit`` in the
installed Prefect 3.6.19. We do NOT unit-test Prefect internals here; this is a
focused contract check on OUR call to ``serve``.
"""

from __future__ import annotations

import inspect

import prefect

from tree import orchestrator


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
    """All existing deployment registrations are preserved (positional args)."""

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
    assert "memory-extraction-etl" not in deployment_names
    assert "data-pipeline-etl" not in deployment_names
