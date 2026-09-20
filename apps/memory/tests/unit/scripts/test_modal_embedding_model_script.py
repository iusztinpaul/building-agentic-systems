"""Unit tests for ``scripts/modal_embedding_model.py`` — the deploy driver.

The script is glue over the **Embedding catalog** helpers and the rails in
``tree.models.modal_cli``, so ``subprocess.run`` is mocked (the shared ``run``
fixture) and NOTHING reaches Modal. What is asserted is what an operator (and
a leaked log) would see: the exact argv per **Serving path**, the exit codes
that stop a mistyped or dangerous command before it spends GPU money — 2 for
usage/config, 3 for a guard refusal — and that the optional Hugging Face token
reaches ``subprocess.run`` but never a log line, a message or an exception
(ADR-009 §9).

Every token here is FAKE (``wk-1`` / ``ws-2`` / ``hf_secret123``) and
``settings.hf_token`` is patched in every test: ``make`` exports the
developer's real ``.env`` into the test process, and a test that read it would
both leak it and pass for the wrong reason.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from tree.models import modal_cli
from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_catalog import get_catalog_entry, modal_cli_command

_QWEN = "Qwen/Qwen3-Embedding-0.6B"
_VOYAGE = "voyageai/voyage-4-nano"
_FAKE_TOKEN = "hf_secret123"

_QWEN_ENDPOINT = "tree-qwen3-embedding-0-6b"
_QWEN_APP = "ep-tree-qwen3-embedding-0-6b"
_VOYAGE_ENDPOINT = "tree-voyage-4-nano"
_VOYAGE_APP = "ep-tree-voyage-4-nano"

# The one text the driver must NOT react to any more: a wrong Proxy token
# carries a 401, which reads as "gated" to a substring matcher.
_PROXY_401 = (
    "Modal answered 401 for https://acme--ep-tree-voyage-4-nano-server.modal.run"
    "/health — the Proxy token is wrong"
)


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.modal_embedding_model as module

    return module


@pytest.fixture
def in_app_root(monkeypatch) -> None:
    """Run from ``apps/memory``: the driver checks the fallback script path the
    way a ``make memory-*`` target would, relative to the app root."""

    monkeypatch.chdir(Path(__file__).resolve().parents[3])


@pytest.fixture
def no_token(mocker, cli_module):
    """The default posture: no Hugging Face token configured."""

    mocker.patch.object(cli_module.settings, "hf_token", SecretStr(""))


@pytest.fixture
def with_token(mocker, cli_module):
    """A FAKE token — never the developer's own."""

    mocker.patch.object(cli_module.settings, "hf_token", SecretStr(_FAKE_TOKEN))


@pytest.fixture
def smoke(mocker, cli_module):
    """Patch the smoke test so ``test`` never leaves the process."""

    from tree.models.modal_server import SmokeTestReport

    report = SmokeTestReport(
        url="https://acme--ep-tree-voyage-4-nano-server.modal.run",
        served_model=_VOYAGE,
        dimensions=2048,
        truncated_dimensions=1024,
        cold_start_seconds=1.0,
        cos_relevant=0.7,
        cos_unrelated=0.2,
        unauthenticated_status=401,
    )
    return mocker.patch.object(
        cli_module, "smoke_test", new_callable=mocker.AsyncMock, return_value=report
    )


def _invoke(cli_module, args: list[str], caplog) -> Any:
    """Run the CLI with INFO logging captured."""

    with caplog.at_level(logging.INFO):
        return CliRunner().invoke(cli_module.main, args)


def _output(result: Any, caplog) -> str:
    """Everything the operator could see: click output + every log record."""

    return result.output + "\n".join(record.getMessage() for record in caplog.records)


def _argvs(run) -> list[list[str]]:
    """Every argv handed to ``subprocess.run``."""

    return [call.args[0] for call in run.call_args_list]


def _acting_argvs(run) -> list[list[str]]:
    """The argvs that CHANGE something — everything but the guard's two
    read-only ``list --json`` calls."""

    return [argv for argv in _argvs(run) if argv[-2:] != ["list", "--json"]]


def _endpoint_row(name: str) -> dict[str, str]:
    return {
        "name": name,
        "endpoint_id": "ep-abcdefghijklmnopqrstuv",
        "status": "running",
        "created_at": "2026-08-24T10:00:00Z",
        "created_by": "someone",
    }


def _app_row(description: str, state: str = "deployed") -> dict[str, str]:
    return {
        "app_id": "ap-abcdefghijklmnopqrstuv",
        "description": description,
        "state": state,
        "tasks": "0",
        "created_at": "2026-09-20T10:00:00Z",
        "stopped_at": "",
    }


@pytest.mark.usefixtures("no_token")
class TestDeploy:
    def test_an_endpoint_deploy_runs_the_create_argv(
        self, cli_module, run, caplog
    ) -> None:
        """Story 1: zero code of ours — the driver just runs Modal's CLI, under
        a name that is ours by construction."""

        result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert result.exit_code == 0
        argv = run.call_args.args[0]
        assert argv == [
            "modal",
            "endpoint",
            "create",
            "--name",
            _QWEN_ENDPOINT,
            "--model",
            _QWEN,
            "--routing-region",
            "eu-west",
        ]
        assert run.call_args.kwargs["env"]["EMBEDDING_MODEL"] == _QWEN
        assert run.call_args.kwargs["check"] is False
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            message.startswith("Dedicated endpoint: Modal picks the GPU")
            for message in messages
        )

    def test_an_override_is_logged_and_used(self, cli_module, run, caplog) -> None:
        """Story 2: ``SERVING=`` walks the ladder for ONE command; the YAML
        stays what the client reads, so the divergence is logged."""

        result = _invoke(
            cli_module,
            ["deploy", "--model", _VOYAGE, "--serving", "endpoint"],
            caplog,
        )

        assert result.exit_code == 0
        assert run.call_args.args[0] == modal_cli_command(
            "deploy", get_catalog_entry(_VOYAGE), "endpoint"
        )
        assert "--custom-hf-repo" in run.call_args.args[0]
        assert any(
            "overrides the catalog's serving: vllm" in record.getMessage()
            for record in caplog.records
        )

    def test_the_yaml_path_logs_no_override(self, cli_module, run, caplog) -> None:
        _invoke(
            cli_module, ["deploy", "--model", _QWEN, "--serving", "endpoint"], caplog
        )

        assert not any(
            "overrides the catalog's serving" in record.getMessage()
            for record in caplog.records
        )

    def test_a_missing_fallback_script_exits_two_and_runs_nothing(
        self, cli_module, run, caplog, tmp_path, monkeypatch
    ) -> None:
        """Story 5: run from a directory without the fallback scripts, the
        driver says which file is missing instead of letting Modal fail — and
        before the guard spends two CLI calls on it."""

        monkeypatch.chdir(tmp_path)

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 2
        run.assert_not_called()
        assert (
            "Serving path 'vllm' needs deploy/modal_vllm_embedding.py, "
            "which does not exist." in _output(result, caplog)
        )

    def test_an_unknown_model_exits_two_and_lists_the_catalog_ids(
        self, cli_module, run, caplog
    ) -> None:
        """Story 7: a mistyped MODEL names every id it could have meant."""

        result = _invoke(cli_module, ["deploy", "--model", "BAAI/bge-m3"], caplog)

        assert result.exit_code == 2
        run.assert_not_called()
        output = _output(result, caplog)
        assert _QWEN in output
        assert _VOYAGE in output

    def test_an_unknown_serving_path_exits_two(self, cli_module, run, caplog) -> None:
        result = _invoke(
            cli_module, ["deploy", "--model", _QWEN, "--serving", "tgi"], caplog
        )

        assert result.exit_code == 2
        run.assert_not_called()
        assert "Use one of: endpoint, sglang, vllm." in _output(result, caplog)

    def test_a_failing_modal_command_propagates_its_exit_code(
        self, cli_module, run, caplog
    ) -> None:
        """Modal's own exit code passes through — 7 here, deliberately not 3:
        3 is reserved for a guard refusal, which is a decision of OURS."""

        run.state.result = subprocess.CompletedProcess(
            args=[], returncode=7, stdout="", stderr=""
        )

        result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert result.exit_code == 7
        assert "modal command failed (exit 7)" in _output(result, caplog)


@pytest.mark.usefixtures("no_token")
class TestDeployGuard:
    """Look before you write (ADR-009 §3): the full matrix through the driver.

    Story 1 is the cell that matters most — the operator's hand-made
    ``ep-qwen3-embedding-0-6b`` is not a ``tree-`` name, so it is invisible
    here and untouched.
    """

    @staticmethod
    def _arrange(run, path: str, existing: str) -> None:
        if existing == "endpoint":
            name = _QWEN_ENDPOINT if path == "endpoint" else _VOYAGE_ENDPOINT
            run.state.endpoints = [_endpoint_row(name)]
        elif existing == "app":
            name = _QWEN_APP if path == "endpoint" else _VOYAGE_APP
            run.state.apps = [_app_row(name)]

    @staticmethod
    def _command(path: str) -> tuple[str, list[str]]:
        """The model + argv per path: the endpoint cells use the Qwen entry
        (its YAML path IS ``endpoint``), the App cells the voyage one."""

        if path == "endpoint":
            return _QWEN, ["deploy", "--model", _QWEN]
        return _VOYAGE, ["deploy", "--model", _VOYAGE, "--serving", path]

    @pytest.mark.parametrize("path", ["endpoint", "sglang", "vllm"])
    @pytest.mark.parametrize("existing", ["none", "endpoint", "app"])
    def test_the_matrix_without_force(
        self, cli_module, run, caplog, in_app_root, path: str, existing: str
    ) -> None:
        model, args = self._command(path)
        self._arrange(run, path, existing)
        # An endpoint create is refused by ANYTHING live; an App deploy only by
        # a live Dedicated endpoint — redeploying our own App is an update.
        refuses = existing == "endpoint" or (path == "endpoint" and existing == "app")

        result = _invoke(cli_module, args, caplog)
        output = _output(result, caplog)

        if not refuses:
            assert result.exit_code == 0
            assert len(_acting_argvs(run)) == 1
            assert "Refusing to deploy" not in output
            assert "FORCE=yes" not in output
            return

        assert result.exit_code == 3
        assert _acting_argvs(run) == []
        name = (
            (_QWEN_ENDPOINT if path == "endpoint" else _VOYAGE_ENDPOINT)
            if existing == "endpoint"
            else (_QWEN_APP if path == "endpoint" else _VOYAGE_APP)
        )
        assert f"Refusing to deploy {model} via {path}: {name!r}" in output

    @pytest.mark.parametrize("path", ["endpoint", "sglang", "vllm"])
    @pytest.mark.parametrize("existing", ["none", "endpoint", "app"])
    def test_the_matrix_with_force(
        self, cli_module, run, caplog, in_app_root, path: str, existing: str
    ) -> None:
        """Story 4: ``FORCE=yes`` turns every refusal into ONE warning."""

        _model, args = self._command(path)
        self._arrange(run, path, existing)
        warns = existing == "endpoint" or (path == "endpoint" and existing == "app")

        result = _invoke(cli_module, [*args, "--force"], caplog)
        output = _output(result, caplog)

        assert result.exit_code == 0
        assert len(_acting_argvs(run)) == 1
        assert "Refusing to deploy" not in output
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        if warns:
            assert len(warnings) == 1
            assert warnings[0].startswith("FORCE=yes: deploying over the existing ")
        else:
            assert warnings == []

    def test_a_stopped_app_does_not_block_a_redeploy(
        self, cli_module, run, caplog, in_app_root
    ) -> None:
        run.state.apps = [_app_row(_VOYAGE_APP, state="stopped")]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert "Refusing to deploy" not in _output(result, caplog)

    def test_the_operators_hand_made_endpoint_is_invisible(
        self, cli_module, run, caplog
    ) -> None:
        """Story 1, steps 3-4: Modal named it ``ep-qwen3-embedding-0-6b`` and
        our deploy neither sees it nor writes to it."""

        run.state.endpoints = [_endpoint_row("qwen3-embedding-0-6b")]
        run.state.apps = [_app_row("ep-qwen3-embedding-0-6b")]

        result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert result.exit_code == 0
        create_argv = _acting_argvs(run)[0]
        assert create_argv[3:5] == ["--name", _QWEN_ENDPOINT]


@pytest.mark.usefixtures("no_token")
class TestGuardFailsClosed:
    """Story 6: Modal unreachable is not evidence that nothing is there."""

    def test_guard_failure_is_closed(self, cli_module, run, caplog) -> None:
        run.state.list_returncode = 1

        result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert result.exit_code == 3
        assert _acting_argvs(run) == []
        assert (
            f"Refusing to deploy {_QWEN}: could not list Modal endpoints (exit 1), "
            "so an overwrite cannot be ruled out. Pass FORCE=yes to skip the check."
            in _output(result, caplog)
        )

    def test_force_downgrades_it_to_a_warning(self, cli_module, run, caplog) -> None:
        run.state.list_returncode = 1

        result = _invoke(cli_module, ["deploy", "--model", _QWEN, "--force"], caplog)

        assert result.exit_code == 0
        assert len(_acting_argvs(run)) == 1
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == [
            "FORCE=yes: could not list Modal endpoints (exit 1) — deploying anyway."
        ]


@pytest.mark.usefixtures("with_token")
class TestGuardLinesCarryNoToken:
    """The refusal and the ``FORCE=yes`` warning are structurally token-free —
    pinned here because they are the two NEW lines a deploy can end on, and a
    custom-weights deploy is exactly when a token is in the argv (ADR-009 §9).
    """

    @pytest.fixture(autouse=True)
    def _a_live_endpoint(self, run) -> None:
        run.state.endpoints = [_endpoint_row(_VOYAGE_ENDPOINT)]

    def test_a_refusal_never_carries_the_token(self, cli_module, run, caplog) -> None:
        result = _invoke(
            cli_module, ["deploy", "--model", _VOYAGE, "--serving", "endpoint"], caplog
        )

        assert result.exit_code == 3
        output = _output(result, caplog)
        assert "Refusing to deploy" in output
        assert _FAKE_TOKEN not in output

    def test_the_force_warning_never_carries_the_token(
        self, cli_module, run, caplog
    ) -> None:
        result = _invoke(
            cli_module,
            ["deploy", "--model", _VOYAGE, "--serving", "endpoint", "--force"],
            caplog,
        )

        assert result.exit_code == 0
        output = _output(result, caplog)
        assert "FORCE=yes: deploying over the existing Dedicated endpoint" in output
        assert "--custom-hf-token ***" in output
        assert _FAKE_TOKEN not in output


@pytest.mark.usefixtures("no_token")
class TestStop:
    def test_an_endpoint_is_stopped_by_name(self, cli_module, run, caplog) -> None:
        """Story 1, step 5."""

        result = _invoke(cli_module, ["stop", "--model", _QWEN], caplog)

        assert result.exit_code == 0
        assert run.call_args.args[0] == [
            "modal",
            "endpoint",
            "stop",
            "-y",
            _QWEN_ENDPOINT,
        ]

    def test_a_fallback_app_is_stopped_by_app_name(
        self, cli_module, run, caplog
    ) -> None:
        """``-stop`` needs the same ``SERVING=`` the deploy used — a fallback
        is a Modal app, not an Endpoint."""

        result = _invoke(
            cli_module, ["stop", "--model", _QWEN, "--serving", "sglang"], caplog
        )

        assert result.exit_code == 0
        assert run.call_args.args[0] == ["modal", "app", "stop", "-y", _QWEN_APP]

    def test_a_stop_lists_nothing(self, cli_module, run, caplog) -> None:
        """The prefix IS the guard of a stop: it can only ever name something
        of ours, so there is nothing to look up first."""

        _invoke(cli_module, ["stop", "--model", _QWEN], caplog)

        assert len(_argvs(run)) == 1

    def test_a_stop_never_carries_a_token(
        self, cli_module, run, caplog, mocker
    ) -> None:
        """Only a deploy downloads weights, so only a deploy may carry one."""

        mocker.patch.object(cli_module.settings, "hf_token", SecretStr(_FAKE_TOKEN))

        _invoke(
            cli_module, ["stop", "--model", _VOYAGE, "--serving", "endpoint"], caplog
        )

        assert "--custom-hf-token" not in run.call_args.args[0]


@pytest.mark.usefixtures("no_token")
class TestStopOwnership:
    """The rail the incident bought: we only ever act on a ``tree-`` name.

    The derivation is patched back to what it was BEFORE the namespace, which
    is the only way this can fire today — and exactly the regression it must
    catch.
    """

    @pytest.fixture
    def unprefixed(self, mocker, cli_module) -> None:
        mocker.patch.object(
            cli_module.ModalEmbeddingModelConfig,
            "endpoint_name",
            property(lambda self: "qwen3-embedding-0-6b"),
        )

    @pytest.mark.parametrize(
        "action,extra",
        [("deploy", []), ("deploy", ["--force"]), ("stop", [])],
        ids=["deploy", "deploy-forced", "stop"],
    )
    def test_a_foreign_name_exits_two_and_starts_nothing(
        self, cli_module, run, caplog, unprefixed, action: str, extra: list[str]
    ) -> None:
        result = _invoke(cli_module, [action, "--model", _QWEN, *extra], caplog)

        assert result.exit_code == 2
        run.assert_not_called()
        assert (
            f"Refusing to {action} 'qwen3-embedding-0-6b': it lacks the 'tree-' "
            "prefix, so this project did not create it." in _output(result, caplog)
        )

    def test_a_fallback_deploy_checks_the_app_name(
        self, cli_module, run, caplog, unprefixed, in_app_root
    ) -> None:
        """On a fallback path the argv names a SCRIPT — the name that gets
        created is the app name, so that is what is checked."""

        result = _invoke(
            cli_module, ["deploy", "--model", _QWEN, "--serving", "sglang"], caplog
        )

        assert result.exit_code == 2
        run.assert_not_called()
        assert "Refusing to deploy 'ep-qwen3-embedding-0-6b'" in _output(result, caplog)


class TestDryRun:
    """Story 5 of the task: the ONLY safe way to exercise the driver."""

    @pytest.mark.usefixtures("no_token")
    @pytest.mark.parametrize(
        "args,expected",
        [
            (
                ["deploy", "--model", _QWEN],
                "modal endpoint create --name tree-qwen3-embedding-0-6b",
            ),
            (
                ["stop", "--model", _QWEN],
                "modal endpoint stop -y tree-qwen3-embedding-0-6b",
            ),
        ],
        ids=["deploy", "stop"],
    )
    def test_the_flag_logs_the_command_and_starts_nothing(
        self, cli_module, run, caplog, args: list[str], expected: str
    ) -> None:
        result = _invoke(cli_module, [*args, "--dry-run"], caplog)

        assert result.exit_code == 0
        run.assert_not_called()
        output = _output(result, caplog)
        assert f"DRY RUN — would run: {expected}" in output
        assert (
            "DRY RUN — skipped the Modal existence check (no modal process is "
            "started)." in output
        )

    @pytest.mark.usefixtures("no_token")
    def test_the_env_var_alone_is_enough(
        self, cli_module, run, caplog, monkeypatch
    ) -> None:
        """``TREE_MODAL_DRY_RUN=1`` is read at CALL time, so it covers a
        command that never saw the flag."""

        monkeypatch.setenv(modal_cli.DRY_RUN_ENV, "1")

        result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert result.exit_code == 0
        run.assert_not_called()
        assert "DRY RUN — would run: modal endpoint create" in _output(result, caplog)

    @pytest.mark.usefixtures("with_token")
    def test_a_dry_run_argv_is_redacted(self, cli_module, run, caplog) -> None:
        """Story 5, step 2: a custom-weights create carries the token, so even
        the command an operator is shown must not."""

        result = _invoke(
            cli_module,
            ["deploy", "--model", _VOYAGE, "--serving", "endpoint", "--dry-run"],
            caplog,
        )

        assert result.exit_code == 0
        run.assert_not_called()
        output = _output(result, caplog)
        assert "--custom-hf-token ***" in output
        assert _FAKE_TOKEN not in output


def test_unit_suite_defaults_to_modal_dry_run(cli_module, mocker, caplog) -> None:
    """The contributor story: a driver test with NO ``run`` fixture dry-runs
    instead of reaching the real, authenticated CLI.

    ``subprocess.run`` is patched to RAISE here, so the test fails loudly if
    the rail ever stops holding."""

    assert os.environ[modal_cli.DRY_RUN_ENV] == "1"
    mocker.patch.object(
        modal_cli.subprocess,
        "run",
        side_effect=AssertionError("a unit test must never start `modal`"),
    )

    result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

    assert result.exit_code == 0


def test_the_rail_is_wired_once_in_the_unit_conftest() -> None:
    """The autouse fixture sets it; the ``run`` fixture is the ONE exit. A
    second ``delenv`` anywhere in the test tree would be a hole in the rail."""

    tests_root = Path(__file__).resolve().parents[2]
    conftest = tests_root / "unit" / "conftest.py"

    assert conftest.read_text().count('setenv(modal_cli.DRY_RUN_ENV, "1")') == 1
    # Assembled at runtime so THIS file does not match its own needle.
    needles = ("del" + "env(modal_cli.DRY_RUN_ENV", "del" + 'env("TREE_MODAL_DRY_RUN')
    leavers = [
        path
        for path in sorted(tests_root.rglob("*.py"))
        if any(needle in path.read_text() for needle in needles)
    ]
    assert leavers == [conftest]


@pytest.mark.usefixtures("no_token")
class TestSmokeTestCommand:
    def test_it_awaits_the_shared_smoke_test_once(
        self, cli_module, smoke, caplog
    ) -> None:
        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        smoke.assert_awaited_once_with(_VOYAGE)

    def test_a_failing_smoke_test_exits_one(self, cli_module, smoke, caplog) -> None:
        """Story 3/9: the message carries the numbers, the exit code carries
        the verdict."""

        smoke.side_effect = ModelError("expected 2048 dims, got 1024 — ...")

        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        assert result.exit_code == 1
        assert "expected 2048 dims, got 1024" in _output(result, caplog)


@pytest.mark.usefixtures("with_token")
class TestHfToken:
    """Story 3: a private fine-tune deploys, and the token appears nowhere."""

    def test_custom_weights_get_the_token_appended(
        self, cli_module, run, caplog
    ) -> None:
        result = _invoke(
            cli_module,
            ["deploy", "--model", _VOYAGE, "--serving", "endpoint"],
            caplog,
        )

        assert result.exit_code == 0
        argv = run.call_args.args[0]
        assert argv[-2:] == ["--custom-hf-token", _FAKE_TOKEN]

    def test_the_logged_argv_is_redacted(self, cli_module, run, caplog) -> None:
        """The positive half is the canary: it proves the capture works, so
        the absence assertion below it means something."""

        result = _invoke(
            cli_module,
            ["deploy", "--model", _VOYAGE, "--serving", "endpoint"],
            caplog,
        )

        output = _output(result, caplog)
        assert "--custom-hf-token ***" in output
        assert _FAKE_TOKEN not in output

    def test_a_base_model_endpoint_gets_no_token(self, cli_module, run, caplog) -> None:
        """Modal documents the flag as the token "for private
        --custom-hf-repo", so an entry without custom weights never gets it."""

        _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert "--custom-hf-token" not in run.call_args.args[0]

    def test_a_failing_command_never_leaks_the_token(
        self, cli_module, run, caplog
    ) -> None:
        """``check=True`` would raise ``CalledProcessError``, whose message is
        the full argv — the token. The failure path is the leak path."""

        run.state.result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=""
        )

        result = _invoke(
            cli_module,
            ["deploy", "--model", _VOYAGE, "--serving", "endpoint"],
            caplog,
        )

        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        output = _output(result, caplog)
        assert "modal command failed (exit 1)" in output
        assert "--custom-hf-token ***" in output
        assert _FAKE_TOKEN not in output

    def test_a_missing_modal_cli_says_how_to_install_it_without_the_argv(
        self, cli_module, run, caplog
    ) -> None:
        """Not in the AC: found by QA on #139. Without the extra installed,
        ``subprocess.run`` raised a bare ``FileNotFoundError`` whose traceback
        printed the full argv — which on a custom-weights deploy is the
        Hugging Face token."""

        run.state.error = FileNotFoundError(2, "No such file or directory: 'modal'")

        result = _invoke(
            cli_module,
            ["deploy", "--model", _VOYAGE, "--serving", "endpoint"],
            caplog,
        )

        assert result.exit_code == 2
        output = _output(result, caplog)
        assert "The `modal` CLI is not installed" in output
        assert "uv --directory apps/memory sync --extra local-models" in output
        # The only argv in the output is the REDACTED one logged before the
        # call; the failure itself adds no second, unredacted copy.
        assert _FAKE_TOKEN not in output
        assert output.count("--custom-hf-token ***") == 1

    def test_a_failure_with_a_token_set_logs_no_hint(
        self, cli_module, run, caplog
    ) -> None:
        """The hint is about a MISSING token; with one set it is noise."""

        run.state.result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="401 Client Error", stderr=""
        )

        result = _invoke(
            cli_module,
            ["deploy", "--model", _VOYAGE, "--serving", "endpoint"],
            caplog,
        )

        assert "private or gated Hugging Face repo" not in _output(result, caplog)


@pytest.mark.usefixtures("no_token")
class TestHfHint:
    """The hint fires ONLY for gated-looking failures (ADR-009 §9).

    Before this it was printed under a catalog refusal, an architecture
    mismatch, a cold-start 503 and a wrong Proxy token — four live failures no
    token would have fixed.
    """

    @staticmethod
    def _hints(caplog) -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if "private or gated Hugging Face repo" in record.getMessage()
        ]

    def test_a_catalog_refusal_gets_no_hint(self, cli_module, run, caplog) -> None:
        run.state.result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=f"'{_VOYAGE}' is not available for dedicated Endpoints.",
            stderr="",
        )

        result = _invoke(
            cli_module, ["deploy", "--model", _VOYAGE, "--serving", "endpoint"], caplog
        )

        assert result.exit_code == 1
        assert self._hints(caplog) == []

    def test_a_gated_download_gets_exactly_one_hint(
        self, cli_module, run, caplog
    ) -> None:
        run.state.result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="GatedRepoError: access to model is restricted",
        )

        result = _invoke(
            cli_module, ["deploy", "--model", _VOYAGE, "--serving", "endpoint"], caplog
        )

        assert result.exit_code == 1
        assert len(self._hints(caplog)) == 1
        assert caplog.records[-1].levelname == "WARNING"

    def test_a_failed_fallback_deploy_gets_no_hint(
        self, cli_module, run, caplog, in_app_root
    ) -> None:
        """``modal deploy`` streams to the terminal and is not captured, so
        there is no text to judge — and a gated download fails at container
        START, which is the smoke test's business."""

        run.state.result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=None, stderr=None
        )

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 1
        assert self._hints(caplog) == []

    def test_a_gated_smoke_test_failure_gets_the_hint(
        self, cli_module, smoke, caplog
    ) -> None:
        """A gated download that never finishes surfaces as a server-side
        failure of the smoke test."""

        smoke.side_effect = ExtractionError("server said 403 Forbidden")

        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        assert result.exit_code == 1
        assert len(self._hints(caplog)) == 1

    def test_a_cold_start_503_gets_no_hint(self, cli_module, smoke, caplog) -> None:
        smoke.side_effect = ExtractionError("Health check ... answered 503.")

        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        assert result.exit_code == 1
        assert self._hints(caplog) == []

    def test_a_wrong_proxy_token_gets_no_hint(self, cli_module, smoke, caplog) -> None:
        """Its message carries a 401 — which reads as "gated" to a substring
        matcher — so the TYPE decides: a bare ``ModelError`` is OUR
        configuration, never a server-side gate."""

        smoke.side_effect = ModelError(_PROXY_401)

        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        assert result.exit_code == 1
        assert self._hints(caplog) == []

    def test_a_set_token_never_gets_a_hint(
        self, cli_module, smoke, caplog, mocker
    ) -> None:
        """Patched HERE, not through ``with_token``: the class already applies
        ``no_token``, and two fixtures patching one attribute race."""

        mocker.patch.object(cli_module.settings, "hf_token", SecretStr(_FAKE_TOKEN))
        smoke.side_effect = ExtractionError("server said 403 Forbidden")

        _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        assert self._hints(caplog) == []


@pytest.mark.usefixtures("with_token")
def test_endpoint_create_output_is_captured_and_redacted(
    cli_module, run, caplog, in_app_root
) -> None:
    """Only the create is captured: ``modal deploy`` builds an image and must
    keep streaming. What is captured is logged — redacted, line by line."""

    run.state.result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=f"Endpoint created\ntoken was {_FAKE_TOKEN}\n",
        stderr="",
    )

    result = _invoke(
        cli_module, ["deploy", "--model", _VOYAGE, "--serving", "endpoint"], caplog
    )

    assert result.exit_code == 0
    create_call = run.call_args
    assert create_call.kwargs["capture_output"] is True
    assert create_call.kwargs["text"] is True
    assert create_call.kwargs["check"] is False
    output = _output(result, caplog)
    assert "modal: Endpoint created" in output
    assert "modal: token was ***" in output
    assert _FAKE_TOKEN not in output

    # ... while the fallback path streams: no capture at all.
    caplog.clear()
    _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

    assert run.call_args.args[0] == [
        "modal",
        "deploy",
        "deploy/modal_vllm_embedding.py",
    ]
    assert run.call_args.kwargs["capture_output"] is False


@pytest.mark.usefixtures("no_token")
class TestFallbackScripts:
    """Stories 1 and 4 of #142: the two fallback **Serving paths** end in a
    ``modal deploy`` of a script that EXISTS, told which model to resolve
    through ``EMBEDDING_MODEL`` — the only thing that crosses from the driver
    into the deploy script (the spec itself is resolved there, ADR-009 §3)."""

    @pytest.fixture(autouse=True)
    def _in_app_root(self, in_app_root) -> None:
        """Every test in this class runs from ``apps/memory``."""

    def test_the_catalog_path_deploys_the_vllm_script(
        self, cli_module, run, caplog
    ) -> None:
        """Story 1: ``serving: vllm`` in the YAML needs no flag at all."""

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert run.call_args.args[0] == [
            "modal",
            "deploy",
            "deploy/modal_vllm_embedding.py",
        ]
        assert run.call_args.kwargs["env"]["EMBEDDING_MODEL"] == _VOYAGE

    def test_an_override_deploys_the_sglang_script(
        self, cli_module, run, caplog
    ) -> None:
        """Story 4: the ladder is walked with ``SERVING=sglang`` on an
        ``endpoint`` entry — the script, not the entry, picks the engine."""

        result = _invoke(
            cli_module, ["deploy", "--model", _QWEN, "--serving", "sglang"], caplog
        )

        assert result.exit_code == 0
        assert run.call_args.args[0] == [
            "modal",
            "deploy",
            "deploy/modal_sglang_embedding.py",
        ]
        assert run.call_args.kwargs["env"]["EMBEDDING_MODEL"] == _QWEN

    @pytest.mark.parametrize(
        "args,script",
        [
            (["deploy", "--model", _VOYAGE], "deploy/modal_vllm_embedding.py"),
            (
                ["deploy", "--model", _QWEN, "--serving", "sglang"],
                "deploy/modal_sglang_embedding.py",
            ),
        ],
        ids=["vllm", "sglang"],
    )
    def test_a_token_changes_no_fallback_argv_and_reaches_no_log(
        self, cli_module, run, caplog, mocker, args: list[str], script: str
    ) -> None:
        """On a fallback path the token travels as a Modal Secret built inside
        the deploy script, never as ``--custom-hf-token`` — so a set token
        leaves the argv byte-identical and appears in no log line."""

        # Patched HERE rather than through the ``with_token`` fixture: the
        # class already applies ``no_token``, and this test is only meaningful
        # with a token actually set.
        mocker.patch.object(cli_module.settings, "hf_token", SecretStr(_FAKE_TOKEN))

        result = _invoke(cli_module, args, caplog)

        assert cli_module.settings.hf_token.get_secret_value() == _FAKE_TOKEN

        assert run.call_args.args[0] == ["modal", "deploy", script]
        assert _FAKE_TOKEN not in str(run.call_args.args[0])
        assert _FAKE_TOKEN not in _output(result, caplog)


def test_neither_the_driver_nor_the_cli_module_uses_check_true(cli_module) -> None:
    """``subprocess.run(..., check=True)`` raises ``CalledProcessError``, which
    prints its full argv — the token. Banned by ADR-009 §9, on both sides of
    the door."""

    for module in (cli_module, modal_cli):
        assert "check=True" not in Path(module.__file__).read_text()
