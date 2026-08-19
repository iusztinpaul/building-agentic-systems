"""Unit tests for ``tree.orchestrator`` (#065, retopologized #100).

Guards the one thing CI never exercises and the fan-out tests never reach:
the actual ``prefect.serve(...)`` invocation. ``make memory-serve-workflows``
crashed on startup because the runner-limit kwarg was named ``global_limit``,
which ``prefect.serve`` does not accept (it forwards through ``**kwargs`` into
``Runner.__init__``, which rejects it) — the parameter is ``limit`` in the
installed Prefect 3.6.19. We do NOT unit-test Prefect internals here; this is a
focused contract check on OUR call to ``serve``.

#100 pins the CURRENT core-5 topology: the two coordinator deployments
(``data-etl-coordinator``, ``memory-extract-etl-coordinator``) are dropped — the
flows now run as inline subflows of ``offline-pipeline`` — and the end-to-end
``online-pipeline`` / ``offline-pipeline`` are promoted out of ``optional=True``,
leaving ``dream-consolidation-all-users`` the only optional spec. The nightly cron
moves onto ``offline-pipeline`` so scheduled ingests also extract + index. The
#065 admission-control guards (``limit`` not ``global_limit``; binding to the real
``prefect.serve`` signature) and the schedule guard are preserved.
"""

from __future__ import annotations

import inspect

import prefect
import pytest

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
    """The registered deployment set is EXACTLY the core 5 (#100).

    Compares the FULL set of registered deployment names (not a subset / membership
    check) so any future drift — a dropped, renamed, or accidentally added
    deployment — fails this test. The set is the two workers + indexing plus the
    promoted end-to-end pipelines; every retired deployment name is asserted ABSENT.
    """

    # Arrange
    spy = mocker.patch("tree.orchestrator.serve")

    # Act
    orchestrator.serve_deployments(limit=4)

    # Assert
    call = spy.call_args
    deployment_names = {dep.name for dep in call.args}
    assert deployment_names == {
        "data-etl-worker",
        "memory-extract-etl-worker",
        "memory-indexing-etl",
        # #100: promoted out of ``optional=True`` — the e2e entrypoints are core.
        "online-pipeline",
        "offline-pipeline",
        # The optional dream deployment is gated by ``prefect.deploy_optional``
        # (default false) — absent here, asserted present in
        # ``test_deploy_optional_enabled_registers_optional``.
    }
    # #100: the coordinators run as inline subflows of ``offline-pipeline`` and are
    # no longer deployments of their own.
    assert "data-etl-coordinator" not in deployment_names
    assert "memory-extract-etl-coordinator" not in deployment_names
    # The retired single-flow deployments must not linger in the registration.
    assert "memory-extraction-etl" not in deployment_names
    assert "data-pipeline-etl" not in deployment_names
    # The pre-#066 fan-out deployment is also gone.
    assert "memory-extraction-fanout-etl" not in deployment_names


def test_deploy_optional_disabled_by_default(mocker):
    """With ``prefect.deploy_optional`` false (default), only the core 5 register."""

    spy = mocker.patch("tree.orchestrator.serve")

    orchestrator.serve_deployments(limit=4)

    names = {dep.name for dep in spy.call_args.args}
    assert len(names) == 5
    # #100: the e2e pipelines are part of the core 5, not gated by the flag.
    assert "online-pipeline" in names
    assert "offline-pipeline" in names
    assert "dream-consolidation-all-users" not in names


def test_deploy_optional_enabled_registers_optional(mocker):
    """``prefect.deploy_optional`` true adds dream — the only optional spec left."""

    mocker.patch.object(orchestrator.app_config.prefect, "deploy_optional", True)
    spy = mocker.patch("tree.orchestrator.serve")

    orchestrator.serve_deployments(limit=4)

    names = {dep.name for dep in spy.call_args.args}
    assert len(names) == 6
    assert "dream-consolidation-all-users" in names


def test_serve_deployments_schedules_only_the_offline_pipeline(mocker):
    """``offline-pipeline`` is the ONLY scheduled deployment (#100).

    It carries ONE nightly cron whose runs override
    ``source_files=["sources/listen.yaml"]`` (so the schedule ingests the polled
    listen feeds AND extracts + indexes them, while manual runs ingest the
    default/operator-selected set). No worker/indexing deployment may be given a
    schedule — guards against a cron dropped from the e2e pipeline or attached to
    the wrong deployment.
    """

    # Arrange
    spy = mocker.patch("tree.orchestrator.serve")

    # Act
    orchestrator.serve_deployments(limit=4)

    # Assert
    call = spy.call_args
    schedules_by_name = {
        dep.name: [
            (sched.schedule.cron, sched.parameters)
            for sched in (dep.schedules or [])
            if getattr(sched.schedule, "cron", None) is not None
        ]
        for dep in call.args
    }
    scheduled = {name: scheds for name, scheds in schedules_by_name.items() if scheds}
    assert scheduled == {
        "offline-pipeline": [
            (
                orchestrator._SCHEDULED_INGEST_CRON,
                {"source_files": ["sources/listen.yaml"]},
            )
        ]
    }


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


def test_deployment_groups_select_whole_pipelines():
    """``groups`` narrows the deploy set by pipeline; empty keeps everything.

    The selector matches on the identity tag each spec already carries, so a
    group can never drift from its specs. Since #100 the groups do NOT partition:
    ``online-pipeline`` / ``offline-pipeline`` span data AND memory, so they carry
    both identity tags and land in BOTH groups — pinned here as the documented
    overlap. An unknown group is a typo that would otherwise silently deploy
    nothing — it must raise instead.
    """

    # Arrange / Act
    def names(groups: tuple[str, ...]) -> set[str]:
        return {s.name for s in orchestrator._active_deployment_specs(groups)}

    # Assert — each group covers its pipeline plus the two e2e deployments.
    assert names(("data",)) == {
        "data-etl-worker",
        "online-pipeline",
        "offline-pipeline",
    }
    assert names(("memory",)) == {
        "memory-extract-etl-worker",
        "memory-indexing-etl",
        "online-pipeline",
        "offline-pipeline",
    }
    # The union is still the full set; the overlap is EXACTLY the e2e pipelines.
    assert names(("data", "memory")) == names(())
    assert names(("data",)) | names(("memory",)) == names(())
    assert names(("data",)) & names(("memory",)) == {
        "online-pipeline",
        "offline-pipeline",
    }

    # The same selector reaches the names `status`/`down` address deployments by,
    # so a group-scoped teardown stays within that group's specs (the shared e2e
    # pipelines included — the next `up` restores them).
    scoped = orchestrator.deployment_full_names(("data",))
    assert {n.split("/")[-1] for n in scoped} == names(("data",))
    assert len(scoped) < len(orchestrator.deployment_full_names())

    with pytest.raises(ValueError, match="Unknown deployment group"):
        orchestrator._active_deployment_specs(("dta",))
