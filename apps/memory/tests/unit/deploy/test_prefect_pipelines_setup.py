"""Unit tests for the Prefect Cloud managed-pipeline IaC.

Two surfaces:

* Pure logic in :mod:`tree.orchestrator` (importable directly): the slim
  ``WORKER_PIP_PACKAGES`` list, the static ``managed_env_templates`` mapping, the
  ``RUNTIME_CONFIG`` coverage, the 5 deployment specs, and ``_git_ref_kwarg``.
* The ``up``-only ``_seed_config_stores`` in ``deploy/prefect_pipelines_setup.py``
  (loaded by file path like ``test_atlas_cluster.py``) — the Prefect ``Secret`` /
  ``Variable`` boundary is mocked, so no network.
"""

from __future__ import annotations

import importlib.util
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


class TestWorkerPipPackages:
    def test_installs_the_repo_package_at_the_requested_ref(self) -> None:
        (spec,) = orchestrator.worker_pip_packages("main")

        # Installs THIS package from a git+subdirectory URL so the managed run can
        # import ``tree`` (the heavy backends come from the opt-in extra, not here).
        assert spec.startswith("tree-memory @ git+https://")
        assert "@github.com/iusztinpaul/building-agentic-systems.git@main" in spec
        assert spec.endswith("#subdirectory=apps/memory")

    def test_injects_the_pat_via_a_secret_block_template(self) -> None:
        (spec,) = orchestrator.worker_pip_packages("abc123")

        # The token is a Secret-block reference resolved at run time, not a literal.
        assert "{{ prefect.blocks.secret.tree-github-pat }}" in spec
        assert "@build" not in spec  # ref correctly placed: ...git@abc123#...
        assert "git@abc123#subdirectory=apps/memory" in spec


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
    def test_exactly_five_deployments(self) -> None:
        assert len(orchestrator.deployment_full_names()) == 5

    def test_entrypoints_point_at_src_layout_flow_functions(self) -> None:
        for spec in orchestrator._DEPLOYMENT_SPECS:
            path, _, func = spec.entrypoint.partition(":")
            assert path.startswith("apps/memory/src/tree/")
            assert path.endswith("/pipeline.py")
            assert func == spec.flow.fn.__name__


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
