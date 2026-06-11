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

# [Prefect Cloud free-tier cap] Restore together with the dream-cron assertion
# below once ``dream-consolidation-etl`` is re-enabled in the orchestrator.
# from tree.config.app_config import app_config


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
        # --- [Prefect Cloud free-tier cap: 5 deployments] --------------
        # The five names below are temporarily NOT served (free tier allows
        # only 5 deployments). Re-enable them here together with the matching
        # ``.to_deployment(...)`` blocks in ``orchestrator.serve_deployments``
        # once the Cloud plan is upgraded.
        # "ingest-file-etl",
        # "ingest-conversation-etl",
        # "ingest-youtube-video-batch-etl",
        # "ingest-youtube-rss-feed-batch-etl",
        # "dream-consolidation-etl",
        # ---------------------------------------------------------------
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
    # --- [Prefect Cloud free-tier cap: 5 deployments] ----------------------
    # ``dream-consolidation-etl`` (the only scheduled deployment) is temporarily
    # not served under the free tier, so NO deployment carries a cron. Restore
    # the original assertion below when the dream deployment is re-enabled in
    # ``orchestrator.serve_deployments``.
    # assert scheduled == {"dream-consolidation-etl": [app_config.dream.cron]}
    assert scheduled == {}
    # -----------------------------------------------------------------------


def test_serve_deployments_serves_the_built_topology(mocker):
    """``serve_deployments`` serves exactly what ``build_deployments`` returns.

    Guards the refactor (#CD) that split construction out of ``serve``: the
    served positional args must be the build_deployments() list verbatim, so the
    serve and CD-apply paths can never diverge.
    """

    # Arrange
    spy = mocker.patch("tree.orchestrator.serve")

    # Act
    orchestrator.serve_deployments(limit=4)

    # Assert
    built_names = {dep.name for dep in orchestrator.build_deployments()}
    served_names = {dep.name for dep in spy.call_args.args}
    assert served_names == built_names


def test_deploy_cloud_pipelines_binds_each_to_pool_without_serving(mocker):
    """The cloud/CD path deploys every spec to the Managed pool and never serves.

    ``deploy_cloud_pipelines`` replaced the old served ``apply_deployments``: it
    git-sources each flow and ``deploy()``s it bound to the work pool with the
    passed ``job_env`` (no raw secrets, no token). Mocks the Prefect SDK boundary
    (``Flow.from_source`` / ``deploy``, ``Secret``) — no network — and asserts it
    returns the ids, targets the pool, and does NOT serve.
    """

    # Arrange — every from_source(...) yields a fake flow whose deploy returns an id.
    mocker.patch("tree.orchestrator.Secret")
    serve_spy = mocker.patch("tree.orchestrator.serve")
    fake_flow = mocker.Mock()
    fake_flow.deploy = mocker.Mock(side_effect=[f"id-{i}" for i in range(5)])
    mocker.patch.object(prefect.Flow, "from_source", return_value=fake_flow)

    # Act
    ids = orchestrator.deploy_cloud_pipelines(
        work_pool_name="tree-managed",
        git_ref="main",
        job_env={"VOYAGE_API_KEY": "{{ prefect.blocks.secret.tree-voyage-api-key }}"},
    )

    # Assert — one deploy per spec, ids returned in order, never served.
    assert ids == ["id-0", "id-1", "id-2", "id-3", "id-4"]
    serve_spy.assert_not_called()
    assert fake_flow.deploy.call_count == 5
    for call in fake_flow.deploy.call_args_list:
        assert call.kwargs["work_pool_name"] == "tree-managed"
        assert "VOYAGE_API_KEY" in call.kwargs["job_variables"]["env"]
        # Pin the Python-3.14 image (project requires >=3.14).
        assert call.kwargs["job_variables"]["image"] == orchestrator.MANAGED_IMAGE
        # No raw token anywhere: the install is a pull step, not a pip_packages URL.
        assert "pip_packages" not in call.kwargs["job_variables"]
