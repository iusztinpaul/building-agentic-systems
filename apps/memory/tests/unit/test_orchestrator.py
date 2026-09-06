"""Unit tests for ``tree.orchestrator`` (#065, retopologized #100).

Guards the one thing CI never exercises and the fan-out tests never reach:
the actual ``prefect.serve(...)`` invocation. ``make memory-serve-workflows``
crashed on startup because the runner-limit kwarg was named ``global_limit``,
which ``prefect.serve`` does not accept (it forwards through ``**kwargs`` into
``Runner.__init__``, which rejects it) — the parameter is ``limit`` in the
installed Prefect 3.6.19. We do NOT unit-test Prefect internals here; this is a
focused contract check on OUR call to ``serve``.

#100 pins the CURRENT topology: the two coordinator deployments
(``data-etl-coordinator``, ``memory-extract-etl-coordinator``) are dropped — the
flows now run as inline subflows of ``offline-pipeline`` — and the end-to-end
``online-pipeline`` / ``offline-pipeline`` are promoted out of ``optional=True``.
The nightly cron moves onto ``offline-pipeline`` so scheduled ingests also extract
+ index. ``memory-indexing-etl`` then left the set too (same feature):
``memory_indexing`` is an inline subflow — since ADR-007 the third **Offline
phase** of ``offline-pipeline``, plus ``online-pipeline``'s single-doc index — and
``dream-consolidation-all-users`` took the slot it freed — promoted out of
``optional=True`` so its cron actually fires. So the CORE set is 5, EXACTLY the
Prefect free-tier cap, with NO spare slot and NO optional spec left; the
``optional`` flag / ``prefect.deploy_optional`` gate are retained as an extension
point and are pinned here with a synthetic optional spec. The #065
admission-control guards (``limit`` not ``global_limit``; binding to the real
``prefect.serve`` signature) and the schedule guard are preserved.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import prefect
import pytest

import tree
from tree import orchestrator
from tree.config.app_config import app_config

# Entrypoints are REPO-relative (``apps/memory/src/tree/…``) because Prefect
# resolves them inside a fresh clone: .../apps/memory/src/tree/__init__.py → up
# four levels is the repo root.
_REPO_ROOT = Path(tree.__file__).parents[4]
_SRC_PREFIX = "apps/memory/src/"


def _module_name(entrypoint_path: str) -> str:
    """``apps/memory/src/tree/x/y.py`` → ``tree.x.y``."""

    assert entrypoint_path.startswith(_SRC_PREFIX)
    return (
        entrypoint_path.removeprefix(_SRC_PREFIX).removesuffix(".py").replace("/", ".")
    )


def test_serve_deployments_passes_limit_not_global_limit(mocker):
    """``serve_deployments`` calls ``serve`` with ``limit=`` (NOT ``global_limit=``).

    The old code passed ``global_limit=...``, which is not a parameter of
    ``prefect.serve`` and blew up at runtime. This asserts the runner-limit kwarg
    is named ``limit`` and that ``global_limit`` is not among the kwargs.
    """

    spy = mocker.patch("tree.orchestrator.serve")

    orchestrator.serve_deployments(limit=4)

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

    spy = mocker.patch("tree.orchestrator.serve")
    real_signature = inspect.signature(prefect.serve)

    orchestrator.serve_deployments(limit=4)

    call = spy.call_args
    bound = real_signature.bind(*call.args, **call.kwargs)
    assert bound.arguments["limit"] == 4


def test_serve_deployments_registers_all_deployments(mocker):
    """The registered deployment set is EXACTLY the core 5.

    Compares the FULL set of registered deployment names (not a subset / membership
    check) so any future drift — a dropped, renamed, or accidentally added
    deployment — fails this test. The set is the two workers, the promoted
    end-to-end pipelines and the scheduled dream consolidation; every retired
    deployment name is asserted ABSENT.
    """

    spy = mocker.patch("tree.orchestrator.serve")

    orchestrator.serve_deployments(limit=4)

    call = spy.call_args
    deployment_names = {dep.name for dep in call.args}
    assert deployment_names == {
        "data-etl-worker",
        "memory-extract-etl-worker",
        # #100: promoted out of ``optional=True`` — the e2e entrypoints are core.
        "online-pipeline",
        "offline-pipeline",
        # Promoted out of ``optional=True`` into the slot indexing freed: a cron on
        # an UNREGISTERED deployment never fires, so gating dream disabled it.
        "dream-consolidation-all-users",
    }
    # #100: the coordinators run as inline subflows of ``offline-pipeline`` and are
    # no longer deployments of their own. Indexing followed them: ``memory_indexing``
    # runs inline at both its call sites (the offline indexing PHASE and
    # ``online-pipeline``), freeing a free-tier slot.
    assert "data-etl-coordinator" not in deployment_names
    assert "memory-extract-etl-coordinator" not in deployment_names
    assert "memory-indexing-etl" not in deployment_names
    # The retired single-flow deployments must not linger in the registration.
    assert "memory-extraction-etl" not in deployment_names
    assert "data-pipeline-etl" not in deployment_names
    # The pre-#066 fan-out deployment is also gone.
    assert "memory-extraction-fanout-etl" not in deployment_names


def test_memory_worker_entrypoint_points_at_the_one_pipeline_module():
    """ADR-006 decision 8 moved the flows into ``tree/memory/pipeline.py``.

    The entrypoint is a FILE PATH Prefect resolves at run time, so a stale one
    fails only when the deployment next runs — pin it here instead.
    """

    entrypoints = {
        spec.name: spec.entrypoint for spec in orchestrator._DEPLOYMENT_SPECS
    }

    assert entrypoints["memory-extract-etl-worker"] == (
        "apps/memory/src/tree/memory/pipeline.py:memory_extract_etl_worker"
    )
    assert entrypoints["dream-consolidation-all-users"] == (
        "apps/memory/src/tree/memory/graph/consolidation/dream.py"
        ":dream_consolidation_all_users"
    )
    assert len(orchestrator._DEPLOYMENT_SPECS) == 5


def test_clustering_did_not_take_a_deployment_slot():
    """ADR-007 §5: phase 4 is an INLINE subflow of ``offline-pipeline``.

    The five free-tier slots are all spent (see the module docstring), so the
    new clustering phase had to be free. A sixth spec would break the serve —
    hours later, in Cloud — so the absence is asserted here.
    """

    source = Path(orchestrator.__file__).read_text(encoding="utf-8")

    assert "memory_clustering" not in source
    assert "memory-clustering" not in source
    assert len(orchestrator._DEPLOYMENT_SPECS) == 5


class TestEveryEntrypointResolves:
    """Each spec's ``path:function`` must resolve — file AND flow (#111).

    Prefect resolves the entrypoint at RUN time, inside a fresh clone on the
    managed worker, so a path left stale by a package move (ADR-006 §8 moved
    ``dream.py`` under ``graph/``) fails hours later in Cloud rather than in
    CI. These walk every spec and resolve its entrypoint against the tree.
    """

    @pytest.mark.parametrize(
        "spec", orchestrator._DEPLOYMENT_SPECS, ids=lambda spec: spec.name
    )
    def test_the_entrypoint_file_exists(self, spec) -> None:
        path = _REPO_ROOT / spec.entrypoint.split(":")[0]

        assert path.is_file(), f"{spec.name}: no file at {spec.entrypoint}"

    @pytest.mark.parametrize(
        "spec", orchestrator._DEPLOYMENT_SPECS, ids=lambda spec: spec.name
    )
    def test_the_entrypoint_names_a_flow_in_that_module(self, spec) -> None:
        file_path, _, function_name = spec.entrypoint.partition(":")
        module = importlib.import_module(_module_name(file_path))

        attribute = getattr(module, function_name, None)
        assert attribute is not None, (
            f"{spec.name}: {_module_name(file_path)} has no {function_name!r}"
        )
        assert isinstance(attribute, prefect.Flow)

    @pytest.mark.parametrize(
        "spec", orchestrator._DEPLOYMENT_SPECS, ids=lambda spec: spec.name
    )
    def test_the_entrypoint_names_the_same_flow_the_spec_holds(self, spec) -> None:
        """The two halves of a spec must not drift apart.

        ``spec.flow`` drives the local-serve path and ``spec.entrypoint`` the
        Cloud one; an import fixed in only one of them would leave the two
        execution models running different code.
        """

        file_path, _, function_name = spec.entrypoint.partition(":")
        module = importlib.import_module(_module_name(file_path))

        assert getattr(module, function_name) is spec.flow

    def test_the_topology_is_exactly_the_free_tier_five(self) -> None:
        assert len(orchestrator._DEPLOYMENT_SPECS) == 5


def test_every_spec_registers_with_deploy_optional_off(mocker):
    """All 5 specs register under the default flag — because NONE is optional.

    ``prefect.deploy_optional`` defaults to false, and that no longer withholds
    anything: dream was promoted into the core set, so the shipped topology has no
    optional spec. Pins both halves — the flag is off, and the registered set is
    still the full 5 — so re-marking a spec ``optional=True`` (which would silently
    unregister it, and with it any cron it carries) fails here.
    """

    # Arrange — pin the flag off explicitly: an exported
    # ``TREE_PREFECT__DEPLOY_OPTIONAL=true`` in the dev shell must not steer this test.
    mocker.patch.object(app_config.prefect, "deploy_optional", False)
    spy = mocker.patch("tree.orchestrator.serve")

    orchestrator.serve_deployments(limit=4)

    assert type(app_config.prefect).model_fields["deploy_optional"].default is False
    assert not any(spec.optional for spec in orchestrator._DEPLOYMENT_SPECS)
    assert len(spy.call_args.args) == 5


@pytest.mark.parametrize(
    ("deploy_optional", "registers_the_optional_spec"), [(False, False), (True, True)]
)
def test_deploy_optional_gate_filters_an_optional_spec(
    mocker, deploy_optional: bool, registers_the_optional_spec: bool
) -> None:
    """The retained ``optional`` gate still filters — pinned with a synthetic spec.

    No shipped spec is optional today, so the ``optional`` field, the
    ``prefect.deploy_optional`` config knob and the filtering in
    ``_active_deployment_specs`` currently gate nothing. They are kept deliberately
    as the extension point for the next deployment that must sit outside the
    free-tier budget — so this appends a fake optional spec to
    ``_DEPLOYMENT_SPECS`` and asserts the flag still decides its fate. Without this
    the mechanism would rot untested until someone needed it.
    """

    # Arrange — reuse a real flow so the spec is well-formed; only ``optional`` matters.
    fake_optional = orchestrator._DeploymentSpec(
        orchestrator._DEPLOYMENT_SPECS[0].flow,
        "fake-optional-etl",
        "apps/memory/src/tree/data/offline_pipeline.py:data_etl_worker",
        ["data-pipeline"],
        optional=True,
    )
    mocker.patch.object(
        orchestrator,
        "_DEPLOYMENT_SPECS",
        [*orchestrator._DEPLOYMENT_SPECS, fake_optional],
    )
    mocker.patch.object(app_config.prefect, "deploy_optional", deploy_optional)

    names = {spec.name for spec in orchestrator._active_deployment_specs()}

    # Assert — the core 5 always register; only the optional spec follows the flag.
    assert ("fake-optional-etl" in names) is registers_the_optional_spec
    assert len(names) == (6 if registers_the_optional_spec else 5)


def test_serve_deployments_schedules_the_offline_and_dream_pipelines(mocker):
    """EXACTLY two deployments are scheduled: the offline pipeline and dream.

    ``offline-pipeline`` carries the nightly ingest cron whose runs override
    ``source_files=["sources/listen.yaml"]`` (so the schedule ingests the polled
    listen feeds AND extracts + indexes them, while manual runs ingest the
    default/operator-selected set). ``dream-consolidation-all-users`` carries the
    consolidation cron from ``app_config.dream.cron``, with no parameter overrides
    (it sweeps every active user). The three remaining specs — both workers and
    ``online-pipeline`` — must carry NO schedule; a worker fired on a timer would
    run outside its coordinator. Guards a cron dropped from either scheduled
    deployment or attached to the wrong one.
    """

    spy = mocker.patch("tree.orchestrator.serve")

    orchestrator.serve_deployments(limit=4)

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
    assert orchestrator._SCHEDULED_INGEST_CRON == "0 3 * * *"
    assert scheduled == {
        "offline-pipeline": [
            (
                orchestrator._SCHEDULED_INGEST_CRON,
                {"source_files": ["sources/listen.yaml"]},
            )
        ],
        "dream-consolidation-all-users": [(app_config.dream.cron, {})],
    }
    # The unscheduled remainder is explicit, so a stray cron cannot slip in.
    assert set(schedules_by_name) - set(scheduled) == {
        "data-etl-worker",
        "memory-extract-etl-worker",
        "online-pipeline",
    }


def test_serve_deployments_serves_the_built_topology(mocker):
    """``serve_deployments`` serves exactly what ``build_deployments`` returns.

    Guards the refactor (#CD) that split construction out of ``serve``: the
    served positional args must be the build_deployments() list verbatim, so the
    serve and CD-apply paths can never diverge.
    """

    spy = mocker.patch("tree.orchestrator.serve")

    orchestrator.serve_deployments(limit=4)

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

    def names(groups: tuple[str, ...]) -> set[str]:
        return {s.name for s in orchestrator._active_deployment_specs(groups)}

    # Assert — each group covers its pipeline plus the two e2e deployments.
    assert names(("data",)) == {
        "data-etl-worker",
        "online-pipeline",
        "offline-pipeline",
    }
    # Indexing is no longer a deployment, so the ``memory`` group is the extraction
    # worker plus dream plus the two e2e pipelines.
    assert names(("memory",)) == {
        "memory-extract-etl-worker",
        "dream-consolidation-all-users",
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
