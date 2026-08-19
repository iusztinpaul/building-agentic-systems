"""Unit tests for the Prefect Cloud managed-pipeline IaC.

Two surfaces:

* Pure logic in :mod:`tree.orchestrator` (importable directly): the
  ``_GitRepoWithPipInstall`` pull steps, the static ``managed_env_templates``
  mapping, the ``RUNTIME_CONFIG`` coverage, the 5 core deployment specs, and
  ``_git_ref_kwarg``.
* The ``up``-only ``_seed_config_stores`` in ``deploy/prefect_pipelines_setup.py``
  (loaded by file path like ``test_atlas_cluster.py``) — the Prefect ``Secret`` /
  ``Variable`` boundary is mocked, so no network.
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import sys

import pytest

from tree import orchestrator

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[3]
    / "deploy"
    / "prefect_pipelines_setup.py"
)
_spec = importlib.util.spec_from_file_location("prefect_pipelines_setup", _SCRIPT)
_setup = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
assert _spec is not None and _spec.loader is not None
sys.modules["prefect_pipelines_setup"] = _setup
_spec.loader.exec_module(_setup)


class TestGitRepoPipInstallPullStep:
    def test_appends_pip_install_after_git_clone(self) -> None:
        repo = orchestrator._GitRepoWithPipInstall(
            url=orchestrator.GIT_URL, branch="main"
        )

        steps = repo.to_pull_step()

        # A list: the standard git_clone, then a pip install of this package so the
        # managed run can import ``tree`` — installed from the local clone, so NO
        # token appears in any URL/step.
        assert isinstance(steps, list)
        keys = [next(iter(step)) for step in steps]
        assert any("git_clone" in k for k in keys)
        assert any("run_shell_script" in k for k in keys)
        joined = str(steps)
        assert "./apps/memory" in joined
        assert "--ignore-requires-python" in joined  # install past stale 3.14 metadata
        assert orchestrator.GIT_URL in joined  # clone target, no embedded token


class TestManagedEnvTemplates:
    def test_secrets_route_through_block_references_not_raw_values(self) -> None:
        env = orchestrator.managed_env_templates()

        # Every secret env var is a block reference — never a literal value.
        assert (
            env["VOYAGE_API_KEY"] == "{{ prefect.blocks.secret.tree-voyage-api-key }}"
        )
        assert (
            env["MONGO_INITDB_ROOT_PASSWORD"]
            == "{{ prefect.blocks.secret.tree-mongo-password }}"
        )

    def test_non_secret_config_routes_through_variable_references(self) -> None:
        env = orchestrator.managed_env_templates()

        assert env["MONGO_HOST"] == "{{ prefect.variables.tree_mongo_host }}"

    def test_covers_every_runtime_config_var(self) -> None:
        env = orchestrator.managed_env_templates()

        for store_name, var, is_secret in orchestrator.RUNTIME_CONFIG:
            kind = "blocks.secret" if is_secret else "variables"
            assert env[var] == "{{ prefect.%s.%s }}" % (kind, store_name)


class TestDeploymentSpecs:
    def test_exactly_five_core_deployments(self) -> None:
        # Prefect Cloud's free tier caps a workspace at 5 deployments and the core
        # set now spends EXACTLY 5 — the slot indexing gave back when it became an
        # inline subflow went to ``dream-consolidation-all-users``. There is no
        # spare slot: a 6th spec must displace one of these or be gated behind
        # ``optional=True`` (see ``_active_deployment_specs``), so registering one
        # more must fail here rather than at ``deploy`` time against Cloud.
        full_names = orchestrator.deployment_full_names()

        assert len(full_names) == 5
        assert {name.split("/")[-1] for name in full_names} == {
            "data-etl-worker",
            "memory-extract-etl-worker",
            "online-pipeline",
            "offline-pipeline",
            "dream-consolidation-all-users",
        }

    def test_entrypoints_point_at_src_layout_flow_functions(self) -> None:
        for spec in orchestrator._DEPLOYMENT_SPECS:
            path, _, func = spec.entrypoint.partition(":")
            assert path.startswith("apps/memory/src/tree/")
            assert path.endswith(".py")
            assert func == spec.flow.fn.__name__
            # The entrypoint file must be where the flow function actually lives,
            # so a module rename that forgets to update the entrypoint is caught
            # here (a bare string-suffix check would not notice the drift).
            actual = inspect.getsourcefile(spec.flow.fn)
            assert actual is not None
            assert actual.endswith(path.removeprefix("apps/memory/"))


class TestGitRefKwarg:
    def test_branch_name_routes_to_branch(self) -> None:
        assert orchestrator._git_ref_kwarg("main") == {"branch": "main"}

    def test_commit_sha_routes_to_commit_sha(self) -> None:
        sha = "0" * 40
        assert orchestrator._git_ref_kwarg(sha) == {"commit_sha": sha}


class TestVerifyPatAccess:
    def test_passes_when_repo_is_reachable(self, mocker) -> None:
        mocker.patch.object(
            _setup.httpx, "get", return_value=mocker.Mock(status_code=200)
        )

        _setup._verify_pat_access("ghp_ok")  # no raise

    def test_raises_actionable_error_when_repo_not_accessible(self, mocker) -> None:
        import click

        mocker.patch.object(
            _setup.httpx, "get", return_value=mocker.Mock(status_code=404)
        )

        with pytest.raises(click.ClickException, match="Contents: Read-only"):
            _setup._verify_pat_access("ghp_no_access")


class TestGroupsSelector:
    """``--groups`` must reach the deploy call — the IaC path used to drop it,
    so ``up GROUPS=data`` silently registered the memory deployments too."""

    def _run(self, mocker, args: list[str]):
        from click.testing import CliRunner

        mocker.patch.object(_setup, "_seed_config_stores")
        mocker.patch.object(_setup, "_ensure_work_pool", new=mocker.AsyncMock())
        deploy = mocker.patch.object(_setup, "deploy_cloud_pipelines", return_value=[])
        # env: don't let a GROUPS in the dev shell leak into the unscoped case.
        result = CliRunner().invoke(_setup.cli, args, env={"GROUPS": ""})
        return result, deploy

    def test_up_forwards_groups_to_the_deploy_call(self, mocker) -> None:
        result, deploy = self._run(mocker, ["up", "--groups", "data"])

        assert result.exit_code == 0, result.output
        assert deploy.call_args.kwargs["groups"] == ("data",)

    def test_up_without_groups_deploys_everything(self, mocker) -> None:
        result, deploy = self._run(mocker, ["up"])

        assert result.exit_code == 0, result.output
        assert deploy.call_args.kwargs["groups"] == ()

    def test_unknown_group_fails_before_any_side_effect(self, mocker) -> None:
        result, deploy = self._run(mocker, ["up", "--groups", "dta"])

        assert result.exit_code != 0
        assert "Unknown deployment group" in result.output
        deploy.assert_not_called()
        _setup._seed_config_stores.assert_not_called()


class TestSeedConfigStores:
    def test_requires_github_pat(self, mocker, monkeypatch) -> None:
        import click

        monkeypatch.delenv("GITHUB_PAT", raising=False)
        mocker.patch.object(_setup, "Secret")
        mocker.patch.object(_setup, "Variable")

        with pytest.raises(click.ClickException, match="GITHUB_PAT"):
            _setup._seed_config_stores()

    def test_seeds_pat_block_secrets_and_variables(self, mocker, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_PAT", "ghp_token")
        monkeypatch.setenv("VOYAGE_API_KEY", "vk")
        monkeypatch.setenv("MONGO_HOST", "atlas.example")
        mocker.patch.object(_setup, "_verify_pat_access")  # skip the network probe
        mock_secret = mocker.patch.object(_setup, "Secret")
        mock_variable = mocker.patch.object(_setup, "Variable")

        _setup._seed_config_stores()

        # The PAT block plus every secret in RUNTIME_CONFIG were saved.
        saved_block_names = [
            call.args[0] for call in mock_secret.return_value.save.call_args_list
        ]
        assert orchestrator.PAT_BLOCK_NAME in saved_block_names
        assert "tree-voyage-api-key" in saved_block_names
        # Non-secret config was written as Variables.
        set_var_names = [call.args[0] for call in mock_variable.set.call_args_list]
        assert "tree_mongo_host" in set_var_names
