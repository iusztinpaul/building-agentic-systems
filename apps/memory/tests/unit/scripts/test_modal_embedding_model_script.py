"""Unit tests for ``scripts/modal_embedding_model.py`` — the deploy driver.

The script is glue over the **Embedding catalog** helpers, so ``subprocess.run``
and ``smoke_test`` are mocked and NOTHING reaches Modal. What is asserted is
what an operator (and a leaked log) would see: the exact argv per **Serving
path**, the exit codes that stop a mistyped command before it spends GPU money,
and — the security property this task exists for — that the optional Hugging
Face token reaches ``subprocess.run`` but never a log line, a message or an
exception (ADR-009 §9).

Every token here is FAKE (``wk-1`` / ``ws-2`` / ``hf_secret123``) and
``settings.hf_token`` is patched in every test: ``make`` exports the
developer's real ``.env`` into the test process, and a test that read it would
both leak it and pass for the wrong reason.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_catalog import get_catalog_entry, modal_cli_command

_QWEN = "Qwen/Qwen3-Embedding-0.6B"
_VOYAGE = "voyageai/voyage-4-nano"
_FAKE_TOKEN = "hf_secret123"


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.modal_embedding_model as module

    return module


@pytest.fixture
def no_token(mocker, cli_module):
    """The default posture: no Hugging Face token configured."""

    mocker.patch.object(cli_module.settings, "hf_token", SecretStr(""))


@pytest.fixture
def with_token(mocker, cli_module):
    """A FAKE token — never the developer's own."""

    mocker.patch.object(cli_module.settings, "hf_token", SecretStr(_FAKE_TOKEN))


@pytest.fixture
def run(mocker, cli_module):
    """Patch ``subprocess.run``; returns the spy (exit 0 by default)."""

    return mocker.patch.object(
        cli_module.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )


@pytest.fixture
def smoke(mocker, cli_module):
    """Patch the smoke test so ``test`` never leaves the process."""

    from tree.models.modal_server import SmokeTestReport

    report = SmokeTestReport(
        url="https://acme--ep-voyage-4-nano-server.modal.run",
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


@pytest.mark.usefixtures("no_token")
class TestDeploy:
    def test_an_endpoint_deploy_runs_the_create_argv(
        self, cli_module, run, caplog
    ) -> None:
        """Story 1: zero code of ours — the driver just runs Modal's CLI."""

        result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert result.exit_code == 0
        argv = run.call_args.args[0]
        assert argv == [
            "modal",
            "endpoint",
            "create",
            "--name",
            "qwen3-embedding-0-6b",
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
        driver says which file is missing instead of letting Modal fail."""

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
        run.return_value = subprocess.CompletedProcess(args=[], returncode=3)

        result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert result.exit_code == 3
        assert "modal command failed (exit 3)" in _output(result, caplog)


@pytest.mark.usefixtures("no_token")
class TestStop:
    def test_an_endpoint_is_stopped_by_name(self, cli_module, run, caplog) -> None:
        """Story 1, step 4."""

        result = _invoke(cli_module, ["stop", "--model", _QWEN], caplog)

        assert result.exit_code == 0
        assert run.call_args.args[0] == [
            "modal",
            "endpoint",
            "stop",
            "-y",
            "qwen3-embedding-0-6b",
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
        assert run.call_args.args[0] == [
            "modal",
            "app",
            "stop",
            "-y",
            "ep-qwen3-embedding-0-6b",
        ]

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

        run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

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

        run.side_effect = FileNotFoundError(2, "No such file or directory: 'modal'")

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

        run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

        result = _invoke(
            cli_module,
            ["deploy", "--model", _VOYAGE, "--serving", "endpoint"],
            caplog,
        )

        assert "private or gated Hugging Face repo" not in _output(result, caplog)


@pytest.mark.usefixtures("no_token")
class TestHfTokenHint:
    """Story 4: the token is optional, so a gated repo fails late — the driver
    explains it in ONE line, at the two places it can surface."""

    def test_without_a_token_the_argv_is_exactly_the_builders(
        self, cli_module, run, caplog
    ) -> None:
        _invoke(
            cli_module, ["deploy", "--model", _VOYAGE, "--serving", "endpoint"], caplog
        )

        assert run.call_args.args[0] == modal_cli_command(
            "deploy", get_catalog_entry(_VOYAGE), "endpoint"
        )

    def test_a_failed_deploy_ends_with_the_hint(self, cli_module, run, caplog) -> None:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=1)

        _invoke(
            cli_module, ["deploy", "--model", _VOYAGE, "--serving", "endpoint"], caplog
        )

        last = caplog.records[-1]
        assert last.levelname == "WARNING"
        assert last.getMessage().startswith(
            f"If {_VOYAGE} is a private or gated Hugging Face repo, set "
            "HF_TOKEN in .env"
        )

    def test_a_failed_smoke_test_ends_with_the_hint(
        self, cli_module, smoke, caplog
    ) -> None:
        """A gated download that never finishes surfaces as a smoke test that
        never gets ``health 200``, not as a failed create."""

        smoke.side_effect = ExtractionError("Health check ... answered 503.")

        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        assert result.exit_code == 1
        last = caplog.records[-1]
        assert last.levelname == "WARNING"
        assert f"If {_VOYAGE} is a private or gated" in last.getMessage()

    def test_a_successful_deploy_logs_no_hint(self, cli_module, run, caplog) -> None:
        result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert "private or gated Hugging Face repo" not in _output(result, caplog)


@pytest.mark.usefixtures("no_token")
class TestFallbackScripts:
    """Stories 1 and 4 of #142: the two fallback **Serving paths** end in a
    ``modal deploy`` of a script that EXISTS, told which model to resolve
    through ``EMBEDDING_MODEL`` — the only thing that crosses from the driver
    into the deploy script (the spec itself is resolved there, ADR-009 §3)."""

    @pytest.fixture(autouse=True)
    def _in_app_root(self, monkeypatch) -> None:
        """Run from ``apps/memory``: the driver checks the script path the way
        a ``make memory-*`` target would, relative to the app root."""

        monkeypatch.chdir(Path(__file__).resolve().parents[3])

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


def test_the_driver_never_uses_check_true(cli_module) -> None:
    """``subprocess.run(..., check=True)`` raises ``CalledProcessError``, which
    prints its full argv — the token. Banned by ADR-009 §9."""

    source = Path(cli_module.__file__).read_text()

    assert "check=True" not in source
