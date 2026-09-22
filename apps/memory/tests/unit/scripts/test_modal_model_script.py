"""Unit tests for ``scripts/modal_model.py`` — the deploy driver and its router.

The script is glue over the **Modal catalog** helpers, the auto-router and the
rails in ``tree.models.modal_cli``, so ``subprocess.run`` is mocked (the shared
``run`` fixture), the Hugging Face API is mocked (the shared ``hub`` fixture of
``tests/unit/models/modal_fixtures.py``, requested for every test here) and
NOTHING leaves the process. What is asserted is what an operator (and a
leaked log) would see: the exact argv SEQUENCE per routing verdict, the ONE
``Routing …`` line that records the decision, the exit codes that stop a
mistyped or dangerous command before it spends GPU money — 2 for
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
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from tests.unit.models.modal_fixtures import app_row as _app_row
from tests.unit.models.modal_fixtures import endpoint_row as _endpoint_row
from tree.config.app_config import ModalModelConfig
from tree.models import modal_cli, modal_router
from tree.models.exceptions import ExtractionError, ModelError

_QWEN = "Qwen/Qwen3-Embedding-0.6B"
_VOYAGE = "voyageai/voyage-4-nano"
_LFM = "LiquidAI/LFM2.5-350M"
_QWEN_LLM = "Qwen/Qwen3.5-0.8B"
_FAKE_TOKEN = "hf_secret123"

_QWEN_ENDPOINT = "tree-qwen3-embedding-0-6b"
_QWEN_APP = "ep-tree-qwen3-embedding-0-6b"
_VOYAGE_ENDPOINT = "tree-voyage-4-nano"
_VOYAGE_APP = "ep-tree-voyage-4-nano"
_LFM_ENDPOINT = "tree-lfm2-5-350m"
_LFM_APP = "ep-tree-lfm2-5-350m"

_VLLM_SCRIPT = "deploy/modal_vllm_embedding.py"
_SGLANG_SCRIPT = "deploy/modal_sglang_llm.py"

# The live refusal texts, 2026-09-20 (see tests/unit/models/test_modal_router.py
# for the boxed / wrapped variants — here they only need to carry the verdict).
_NOT_IN_CATALOG = (
    "'{model}' is not available for dedicated Endpoints.\n"
    "Models available for dedicated Endpoints:\n"
    "- Qwen/Qwen3-Embedding-0.6B\n"
    "- Qwen/Qwen3-Embedding-8B\n"
    "- Qwen/Qwen3.5-0.8B\n"
)
_NOT_SERVABLE = (
    "The custom model is not a servable checkpoint of base model "
    "'Qwen/Qwen3-Embedding-0.6B': Custom model hidden_size=2560 does not "
    "match base model hidden_size=1024"
)

# The one text the driver must NOT react to any more: a wrong Proxy token
# carries a 401, which reads as "gated" to a substring matcher.
_PROXY_401 = (
    "Modal answered 401 for https://acme--ep-tree-voyage-4-nano-server.modal.run"
    "/health — the Proxy token is wrong"
)


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.modal_model as module

    return module


@pytest.fixture
def in_app_root(monkeypatch) -> None:
    """Run from ``apps/memory``: the driver checks the App script path the way
    a ``make memory-*`` target would, relative to the app root."""

    monkeypatch.chdir(Path(__file__).resolve().parents[3])


@pytest.fixture
def app_scripts(tmp_path, monkeypatch, in_app_root) -> None:
    """A workspace where BOTH App scripts exist, as EMPTY files.

    Both ship for real now, so this fixture proves the driver checks a PATH
    and nothing more — it never reads, imports or runs the script. It takes
    ``in_app_root`` so it always chdirs LAST, whatever order pytest resolves
    the class fixtures in.
    """

    (tmp_path / "deploy").mkdir()
    (tmp_path / _VLLM_SCRIPT).write_text("# fake\n")
    (tmp_path / _SGLANG_SCRIPT).write_text("# fake\n")
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _hub_for_every_test(hub) -> None:
    """Request the shared ``hub`` (``tests/unit/scripts/conftest.py``) for
    EVERY test in this module.

    The router reads a model's fine-tune lineage whenever Modal answers "not in
    the catalog", so an un-mocked test here would reach huggingface.co. The
    tests that steer the answer take ``hub`` by name as well — same object,
    pytest builds it once per test.
    """


@pytest.fixture
def no_token(mocker, cli_module) -> None:
    """The default posture: no Hugging Face token configured."""

    mocker.patch.object(cli_module.settings, "hf_token", SecretStr(""))


@pytest.fixture
def with_token(mocker, cli_module) -> None:
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


@pytest.fixture
def chat_smoke(mocker, cli_module):
    """Patch the LLM smoke test so ``test`` never leaves the process."""

    from tree.models.modal_server import ChatSmokeTestReport

    report = ChatSmokeTestReport(
        url="https://acme--ep-tree-lfm2-5-350m-server.modal.run",
        served_model=_LFM,
        cold_start_seconds=96.0,
        keys=["city", "population"],
        follows_prompt_schema=True,
        unauthenticated_status=401,
    )
    return mocker.patch.object(
        cli_module,
        "chat_smoke_test",
        new_callable=mocker.AsyncMock,
        return_value=report,
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


def _routing_lines(caplog) -> list[str]:
    """Every ``Routing …`` decision line the command logged."""

    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Routing ")
    ]


def _failed(model: str, output: str, code: int = 1) -> subprocess.CompletedProcess[str]:
    """What Modal answers when it refuses — on stdout, captured."""

    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout=output.format(model=model), stderr=""
    )


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _create_argv(name: str, model: str, base: str | None = None) -> list[str]:
    """The ``modal endpoint create`` argv, plain or with custom weights."""

    argv = ["modal", "endpoint", "create", "--name", name, "--model", base or model]
    if base:
        argv += ["--custom-hf-repo", model, "--custom-hf-revision", _revision(model)]
    return argv + ["--routing-region", "eu-west"]


def _revision(model: str) -> str:
    from tree.models.modal_catalog import get_catalog_entry

    return get_catalog_entry(model).revision


def _no_hub(**_: object) -> None:
    """A Hugging Face client that must never be built."""

    raise AssertionError("this command must not reach the Hub")


@pytest.mark.usefixtures("no_token", "in_app_root")
class TestRouter:
    """ADR-009 §2: Modal is the oracle, and ONE line records its answer."""

    def test_a_model_modal_accepts_becomes_a_dedicated_endpoint(
        self, cli_module, run, caplog
    ) -> None:
        """Story 1: one create, zero code of ours, no image build."""

        result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert result.exit_code == 0
        assert _acting_argvs(run) == [_create_argv(_QWEN_ENDPOINT, _QWEN)]
        assert run.call_args.kwargs["env"]["MODAL_MODEL"] == _QWEN
        assert run.call_args.kwargs["check"] is False
        assert _routing_lines(caplog) == [
            f"Routing {_QWEN}: Modal accepted it → Dedicated endpoint {_QWEN_ENDPOINT}"
        ]

    def test_a_refused_embedding_model_goes_to_the_vllm_app(
        self, cli_module, run, caplog
    ) -> None:
        """Story 2: the operator never learns what a Serving path is — the
        refusal, the Hub lookup and the App deploy are one command."""

        run.state.results = [_failed(_VOYAGE, _NOT_IN_CATALOG), _ok()]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert _acting_argvs(run) == [
            _create_argv(_VOYAGE_ENDPOINT, _VOYAGE),
            ["modal", "deploy", _VLLM_SCRIPT],
        ]
        assert _routing_lines(caplog) == [
            f"Routing {_VOYAGE}: not in Modal's endpoint catalog, no catalog "
            "base → vLLM App"
        ]
        assert run.call_args.kwargs["env"]["MODAL_MODEL"] == _VOYAGE

    @pytest.mark.usefixtures("app_scripts")
    def test_a_refused_llm_goes_to_the_sglang_app(
        self, cli_module, run, caplog
    ) -> None:
        """The KIND comes from which LIST the entry lives in — never from
        guessing at the model."""

        run.state.results = [_failed(_LFM, _NOT_IN_CATALOG), _ok()]

        result = _invoke(cli_module, ["deploy", "--model", _LFM], caplog)

        assert result.exit_code == 0
        assert _acting_argvs(run) == [
            _create_argv(_LFM_ENDPOINT, _LFM),
            ["modal", "deploy", _SGLANG_SCRIPT],
        ]
        assert _routing_lines(caplog) == [
            f"Routing {_LFM}: not in Modal's endpoint catalog, no catalog base "
            "→ SGLang App"
        ]

    def test_a_verdict_split_across_stdout_and_stderr_still_routes(
        self, cli_module, run, caplog
    ) -> None:
        """The two captured streams are joined by a NEWLINE, not concatenated.

        Modal writes its refusal to one stream and nothing guarantees a
        trailing newline on the other: glued together, ``…is not available for
        dedicated`` + ``Endpoints.`` reads as ``dedicatedEndpoints.``, the
        marker no longer matches, and a model that should route to the App
        ABORTS the deploy as an unknown failure instead.
        """

        run.state.results = [
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=f"'{_VOYAGE}' is not available for dedicated",
                stderr="Endpoints.",
            ),
            _ok(),
        ]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert _acting_argvs(run)[-1] == ["modal", "deploy", _VLLM_SCRIPT]
        assert _routing_lines(caplog) == [
            f"Routing {_VOYAGE}: not in Modal's endpoint catalog, no catalog "
            "base → vLLM App"
        ]

    def test_a_fine_tune_of_a_catalog_model_becomes_custom_weights(
        self, cli_module, run, caplog, hub, with_token
    ) -> None:
        """Story 3: the Hub names a base that IS in the list Modal just
        printed, so the second create carries the weights.

        The seed catalog holds no fine-tune, so voyage-4-nano plays that role
        here with a faked Hub card — the argv shape is what is pinned."""

        hub.cards[_VOYAGE] = {"cardData": {"base_model": _QWEN}}
        run.state.results = [_failed(_VOYAGE, _NOT_IN_CATALOG), _ok()]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert _acting_argvs(run) == [
            _create_argv(_VOYAGE_ENDPOINT, _VOYAGE),
            _create_argv(_VOYAGE_ENDPOINT, _VOYAGE, base=_QWEN)
            + ["--custom-hf-token", _FAKE_TOKEN],
        ]
        # The token is appended at the subprocess boundary, and only there.
        assert run.call_args.args[0][-2:] == ["--custom-hf-token", _FAKE_TOKEN]
        output = _output(result, caplog)
        assert "--custom-hf-token ***" in output
        assert _FAKE_TOKEN not in output
        assert _routing_lines(caplog) == [
            f"Routing {_VOYAGE}: not in Modal's endpoint catalog, fine-tune of "
            f"{_QWEN} → Dedicated endpoint {_VOYAGE_ENDPOINT} (custom weights)"
        ]

    def test_weights_the_base_refuses_fall_back_to_the_app(
        self, cli_module, run, caplog, hub
    ) -> None:
        """ "Custom weights" is a SAME-ARCHITECTURE fine-tune: a different
        hidden_size is refused, and that refusal is a verdict, not an error."""

        hub.cards[_VOYAGE] = {"cardData": {"base_model": _QWEN}}
        run.state.results = [
            _failed(_VOYAGE, _NOT_IN_CATALOG),
            _failed(_VOYAGE, _NOT_SERVABLE),
            _ok(),
        ]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert len(_acting_argvs(run)) == 3
        assert _acting_argvs(run)[-1] == ["modal", "deploy", _VLLM_SCRIPT]
        assert _routing_lines(caplog) == [
            f"Routing {_VOYAGE}: not in Modal's endpoint catalog, {_QWEN} "
            "refused the weights (not a servable checkpoint) → vLLM App"
        ]

    def test_a_base_outside_modals_list_is_never_tried(
        self, cli_module, run, caplog, hub
    ) -> None:
        """The lineage is only useful for a base Modal just SAID it can serve
        — anything else would spend a second refused create."""

        hub.cards[_VOYAGE] = {"cardData": {"base_model": "acme/not-in-the-list"}}
        run.state.results = [_failed(_VOYAGE, _NOT_IN_CATALOG), _ok()]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert _acting_argvs(run) == [
            _create_argv(_VOYAGE_ENDPOINT, _VOYAGE),
            ["modal", "deploy", _VLLM_SCRIPT],
        ]

    def test_an_unparsable_model_list_skips_the_custom_weights_attempt(
        self, cli_module, run, caplog, hub
    ) -> None:
        """Best-effort parsing degrades to the App — correct, only costlier —
        and never to a second create against a base we did not read."""

        hub.cards[_VOYAGE] = {"cardData": {"base_model": _QWEN}}
        run.state.results = [
            _failed(_VOYAGE, "'{model}' is not available for dedicated Endpoints."),
            _ok(),
        ]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert _acting_argvs(run) == [
            _create_argv(_VOYAGE_ENDPOINT, _VOYAGE),
            ["modal", "deploy", _VLLM_SCRIPT],
        ]

    def test_an_unknown_failure_aborts_and_deploys_nothing(
        self, cli_module, run, caplog
    ) -> None:
        """Story 4: Modal down is not a verdict. ONE error block with Modal's
        own text, Modal's own exit code, and no image build."""

        run.state.results = [_failed(_VOYAGE, "Token missing.")]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        output = _output(result, caplog)
        assert result.exit_code == 1
        assert _acting_argvs(run) == [_create_argv(_VOYAGE_ENDPOINT, _VOYAGE)]
        assert "modal: Token missing." in output
        assert "modal command failed (exit 1)" in output
        assert _routing_lines(caplog) == []

    def test_a_live_app_of_ours_is_redeployed_without_an_endpoint_attempt(
        self, cli_module, run, caplog
    ) -> None:
        """What is already live stays on its route: `create` cannot update an
        App, so re-routing needs an explicit stop."""

        run.state.apps = [_app_row(_VOYAGE_APP)]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert _acting_argvs(run) == [["modal", "deploy", _VLLM_SCRIPT]]
        assert _routing_lines(caplog) == [
            f"Routing {_VOYAGE}: '{_VOYAGE_APP}' is already live as an App → "
            "redeploying the vLLM App (stop it first to re-route)"
        ]

    def test_a_live_endpoint_refuses_the_deploy(self, cli_module, run, caplog) -> None:
        """An endpoint cannot be updated by ``create`` — the guard says so
        before Modal charges for finding out."""

        run.state.endpoints = [_endpoint_row(_QWEN_ENDPOINT)]

        result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert result.exit_code == 3
        assert _acting_argvs(run) == []
        assert (
            f"Refusing to deploy {_QWEN} via endpoint: '{_QWEN_ENDPOINT}' "
            "already exists on Modal as a Dedicated endpoint. Stop it first "
            f"(make memory-deploy-model-stop MODEL={_QWEN}) or pass FORCE=yes "
            "to deploy over it." in _output(result, caplog)
        )

    def test_serving_app_skips_the_endpoint_attempt(
        self, cli_module, run, caplog
    ) -> None:
        """Story 6: the operator wants to pin vLLM themselves. The YAML is
        unchanged and the client keeps working — same app name."""

        result = _invoke(
            cli_module, ["deploy", "--model", _QWEN, "--serving", "app"], caplog
        )

        assert result.exit_code == 0
        assert _acting_argvs(run) == [["modal", "deploy", _VLLM_SCRIPT]]
        assert _routing_lines(caplog) == [
            f"Routing {_QWEN}: SERVING=app → vLLM App (no endpoint attempt)"
        ]

    def test_serving_endpoint_turns_a_refusal_into_a_failure(
        self, cli_module, run, caplog
    ) -> None:
        """Pinning one path means there is no other to fall back to — and the
        pinned route IS the decision, so it is the one Routing line."""

        run.state.results = [_failed(_VOYAGE, _NOT_IN_CATALOG)]

        result = _invoke(
            cli_module, ["deploy", "--model", _VOYAGE, "--serving", "endpoint"], caplog
        )

        assert result.exit_code == 1
        assert _acting_argvs(run) == [_create_argv(_VOYAGE_ENDPOINT, _VOYAGE)]
        output = _output(result, caplog)
        assert "is not available for dedicated Endpoints" in output
        assert "modal deploy" not in output
        assert _routing_lines(caplog) == [
            f"Routing {_VOYAGE}: SERVING=endpoint → Dedicated endpoint only "
            "(no App fallback)"
        ]

    def test_serving_endpoint_logs_one_line_on_success_too(
        self, cli_module, run, caplog
    ) -> None:
        """ONE line carries the decision: the operator's pin replaces the
        reason the router would have derived, it does not join it."""

        result = _invoke(
            cli_module, ["deploy", "--model", _QWEN, "--serving", "endpoint"], caplog
        )

        assert result.exit_code == 0
        assert _acting_argvs(run) == [_create_argv(_QWEN_ENDPOINT, _QWEN)]
        assert _routing_lines(caplog) == [
            f"Routing {_QWEN}: SERVING=endpoint → Dedicated endpoint only "
            "(no App fallback)"
        ]

    @pytest.mark.parametrize("serving", ["sglang", "vllm", "app "])
    def test_a_retired_or_unknown_serving_value_exits_two(
        self, cli_module, run, caplog, serving: str
    ) -> None:
        """``sglang`` / ``vllm`` named an ENGINE, never a path — and they are
        exactly what an operator's shell history still holds."""

        result = _invoke(
            cli_module, ["deploy", "--model", _QWEN, "--serving", serving], caplog
        )

        assert result.exit_code == 2
        run.assert_not_called()
        assert "Use one of: endpoint, app." in _output(result, caplog)

    def test_an_unknown_model_exits_two_and_lists_both_groups(
        self, cli_module, run, caplog
    ) -> None:
        """Story 7: a mistyped MODEL names every id it could have meant,
        before anything is spent."""

        result = _invoke(
            cli_module, ["deploy", "--model", "LiquidAI/LFM2.5-350m"], caplog
        )

        assert result.exit_code == 2
        run.assert_not_called()
        output = _output(result, caplog)
        assert (
            "Unknown Modal model 'LiquidAI/LFM2.5-350m'. Modal catalog ids — "
            f"embeddings: {_QWEN}, {_VOYAGE}; llms: {_LFM}, {_QWEN_LLM}." in output
        )

    def test_a_missing_app_script_exits_two_and_deploys_nothing(
        self, cli_module, run, caplog, tmp_path, monkeypatch
    ) -> None:
        """Run from the wrong directory (here: an empty ``tmp_path``), the
        driver says WHICH file is missing instead of letting Modal fail on a
        path a minute into a deploy."""

        monkeypatch.chdir(tmp_path)

        result = _invoke(
            cli_module, ["deploy", "--model", _LFM, "--serving", "app"], caplog
        )

        assert result.exit_code == 2
        assert _acting_argvs(run) == []
        assert (
            f"Deploying {_LFM} needs the App script {_SGLANG_SCRIPT}, which "
            "does not exist." in _output(result, caplog)
        )

    def test_a_failing_app_deploy_propagates_modals_exit_code(
        self, cli_module, run, caplog
    ) -> None:
        """Modal's own exit code passes through — 7 here, deliberately not 3:
        3 is reserved for a guard refusal, which is a decision of OURS."""

        run.state.result = subprocess.CompletedProcess(
            args=[], returncode=7, stdout="", stderr=""
        )
        run.state.results = [_failed(_VOYAGE, _NOT_IN_CATALOG)]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 7
        # `modal deploy` streams its own errors past the operator, so the one
        # redacted, re-runnable summary line is what is left on screen.
        assert f"modal command failed (exit 7): modal deploy {_VLLM_SCRIPT}" in _output(
            result, caplog
        )


@pytest.mark.usefixtures("no_token", "in_app_root")
class TestDeployGuard:
    """Look before you write (ADR-009 §3): the matrix through the router.

    The operator's hand-made ``ep-qwen3-embedding-0-6b`` is not a ``tree-``
    name, so it is invisible here and untouched.
    """

    @staticmethod
    def _arrange(run, existing: str) -> None:
        if existing == "endpoint":
            run.state.endpoints = [_endpoint_row(_QWEN_ENDPOINT)]
        elif existing == "app":
            run.state.apps = [_app_row(_QWEN_APP)]

    @pytest.mark.parametrize("serving", ["", "app", "endpoint"])
    @pytest.mark.parametrize("existing", ["none", "endpoint", "app"])
    def test_the_matrix_without_force(
        self, cli_module, run, caplog, serving: str, existing: str
    ) -> None:
        self._arrange(run, existing)
        args = ["deploy", "--model", _QWEN]
        if serving:
            args += ["--serving", serving]
        # A live Dedicated endpoint refuses everything (`create` cannot update
        # it, and an App deploy would shadow it). A live App of OURS is a
        # normal update — and, when the router is in charge, the reason it
        # never attempts an endpoint at all.
        refuses = existing == "endpoint" or (
            serving == "endpoint" and existing == "app"
        )

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
        name = _QWEN_ENDPOINT if existing == "endpoint" else _QWEN_APP
        path = serving or "endpoint"
        assert f"Refusing to deploy {_QWEN} via {path}: {name!r}" in output

    @pytest.mark.parametrize("serving", ["", "app", "endpoint"])
    @pytest.mark.parametrize("existing", ["none", "endpoint", "app"])
    def test_the_matrix_with_force(
        self, cli_module, run, caplog, serving: str, existing: str
    ) -> None:
        """``FORCE=yes`` turns every refusal into ONE warning."""

        self._arrange(run, existing)
        args = ["deploy", "--model", _QWEN, "--force"]
        if serving:
            args += ["--serving", serving]
        warns = existing == "endpoint" or (serving == "endpoint" and existing == "app")

        result = _invoke(cli_module, args, caplog)
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
        self, cli_module, run, caplog
    ) -> None:
        run.state.apps = [_app_row(_VOYAGE_APP, state="stopped")]
        run.state.results = [_failed(_VOYAGE, _NOT_IN_CATALOG), _ok()]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert "Refusing to deploy" not in _output(result, caplog)

    def test_the_operators_hand_made_endpoint_is_invisible(
        self, cli_module, run, caplog
    ) -> None:
        """Modal named it ``ep-qwen3-embedding-0-6b`` and our deploy neither
        sees it nor writes to it."""

        run.state.endpoints = [_endpoint_row("qwen3-embedding-0-6b")]
        run.state.apps = [_app_row("ep-qwen3-embedding-0-6b")]

        result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert result.exit_code == 0
        assert _acting_argvs(run)[0][3:5] == ["--name", _QWEN_ENDPOINT]


@pytest.mark.usefixtures("no_token", "in_app_root")
class TestGuardFailsClosed:
    """Modal unreachable is not evidence that nothing is there."""

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

    def test_the_workspace_is_listed_once_per_deploy(
        self, cli_module, run, caplog
    ) -> None:
        """The router reads the workspace to ROUTE and hands the same reading
        to the guard — two read-only calls per deploy, not four."""

        run.state.results = [_failed(_VOYAGE, _NOT_IN_CATALOG), _ok()]

        _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        list_calls = [argv for argv in _argvs(run) if argv[-2:] == ["list", "--json"]]
        assert len(list_calls) == 2


@pytest.mark.usefixtures("with_token", "in_app_root")
class TestGuardLinesCarryNoToken:
    """The refusal and the ``FORCE=yes`` warning are structurally token-free —
    pinned here because they are the two lines a deploy can end on, and a
    custom-weights deploy is exactly when a token is in the argv (ADR-009 §9).
    """

    @pytest.fixture(autouse=True)
    def _a_live_endpoint(self, run, hub) -> None:
        run.state.endpoints = [_endpoint_row(_VOYAGE_ENDPOINT)]
        hub.cards[_VOYAGE] = {"cardData": {"base_model": _QWEN}}

    def test_a_refusal_never_carries_the_token(self, cli_module, run, caplog) -> None:
        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 3
        output = _output(result, caplog)
        assert "Refusing to deploy" in output
        assert _FAKE_TOKEN not in output

    def test_the_force_warning_never_carries_the_token(
        self, cli_module, run, caplog
    ) -> None:
        run.state.results = [_failed(_VOYAGE, _NOT_IN_CATALOG), _ok()]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE, "--force"], caplog)

        assert result.exit_code == 0
        output = _output(result, caplog)
        assert "FORCE=yes: deploying over the existing Dedicated endpoint" in output
        assert "--custom-hf-token ***" in output
        assert _FAKE_TOKEN not in output


@pytest.mark.usefixtures("no_token")
class TestStop:
    """Story 5: an operator stops a model without remembering how it was
    served — the endpoint stop, then the App stop."""

    def test_an_endpoint_stop_that_works_is_the_whole_command(
        self, cli_module, run, caplog
    ) -> None:
        result = _invoke(cli_module, ["stop", "--model", _QWEN], caplog)

        assert result.exit_code == 0
        assert _acting_argvs(run) == [
            ["modal", "endpoint", "stop", "-y", _QWEN_ENDPOINT]
        ]

    def test_a_failing_endpoint_stop_falls_through_to_the_app(
        self, cli_module, run, caplog
    ) -> None:
        run.state.results = [
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="Error: Endpoint 'tree-voyage-4-nano' not found\n",
                stderr="",
            ),
            _ok(),
        ]

        result = _invoke(cli_module, ["stop", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert _acting_argvs(run) == [
            ["modal", "endpoint", "stop", "-y", _VOYAGE_ENDPOINT],
            ["modal", "app", "stop", "-y", _VOYAGE_APP],
        ]
        assert (
            "Not a Dedicated endpoint (Error: Endpoint 'tree-voyage-4-nano' "
            "not found) — stopping the App instead." in _output(result, caplog)
        )

    def test_the_app_stops_exit_code_is_the_commands(
        self, cli_module, run, caplog
    ) -> None:
        """Both failing means the model is not live either way — the operator
        sees the App stop's code, the last thing that was tried."""

        run.state.result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="nope", stderr=""
        )

        result = _invoke(cli_module, ["stop", "--model", _VOYAGE], caplog)

        output = _output(result, caplog)
        assert result.exit_code == 1
        assert len(_acting_argvs(run)) == 2
        # Only the LAST command failed the stop: the endpoint stop failing is
        # how a path-blind stop learns the route, so it gets the INFO line and
        # no error summary of its own.
        assert (
            output.count("modal command failed (exit 1)") == 1
            and f"modal app stop -y {_VOYAGE_APP}" in output
        )

    def test_serving_app_stops_only_the_app(self, cli_module, run, caplog) -> None:
        result = _invoke(
            cli_module, ["stop", "--model", _QWEN, "--serving", "app"], caplog
        )

        assert result.exit_code == 0
        assert _acting_argvs(run) == [["modal", "app", "stop", "-y", _QWEN_APP]]

    def test_serving_endpoint_stops_only_the_endpoint(
        self, cli_module, run, caplog
    ) -> None:
        run.state.result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="nope", stderr=""
        )

        result = _invoke(
            cli_module, ["stop", "--model", _QWEN, "--serving", "endpoint"], caplog
        )

        assert result.exit_code == 1
        assert _acting_argvs(run) == [
            ["modal", "endpoint", "stop", "-y", _QWEN_ENDPOINT]
        ]

    def test_a_stop_lists_nothing(self, cli_module, run, caplog) -> None:
        """The prefix IS the guard of a stop: it can only ever name something
        of ours, so there is nothing to look up first."""

        _invoke(cli_module, ["stop", "--model", _QWEN], caplog)

        assert [argv for argv in _argvs(run) if argv[-2:] == ["list", "--json"]] == []

    def test_a_stop_never_carries_a_token(
        self, cli_module, run, caplog, mocker
    ) -> None:
        """Only a deploy downloads weights, so only a deploy may carry one."""

        mocker.patch.object(cli_module.settings, "hf_token", SecretStr(_FAKE_TOKEN))

        _invoke(cli_module, ["stop", "--model", _VOYAGE], caplog)

        assert "--custom-hf-token" not in run.call_args.args[0]


@pytest.mark.usefixtures("no_token", "in_app_root")
class TestOwnership:
    """The rail the incident bought: we only ever act on a ``tree-`` name.

    The derivation is patched back to what it was BEFORE the namespace, which
    is the only way this can fire today — and exactly the regression it must
    catch.
    """

    @pytest.fixture
    def unprefixed_endpoint(self, mocker) -> None:
        mocker.patch.object(
            ModalModelConfig,
            "endpoint_name",
            property(lambda self: "qwen3-embedding-0-6b"),
        )

    @pytest.fixture
    def unprefixed_app(self, mocker) -> None:
        mocker.patch.object(
            ModalModelConfig,
            "app_name",
            property(lambda self: "ep-qwen3-embedding-0-6b"),
        )

    @pytest.mark.parametrize(
        "action,extra",
        [("deploy", []), ("deploy", ["--force"]), ("stop", [])],
        ids=["deploy", "deploy-forced", "stop"],
    )
    def test_a_foreign_endpoint_name_exits_two_and_starts_nothing(
        self,
        cli_module,
        run,
        caplog,
        unprefixed_endpoint,
        action: str,
        extra: list[str],
    ) -> None:
        result = _invoke(cli_module, [action, "--model", _QWEN, *extra], caplog)

        assert result.exit_code == 2
        run.assert_not_called()
        assert (
            f"Refusing to {action} 'qwen3-embedding-0-6b': it lacks the 'tree-' "
            "prefix, so this project did not create it." in _output(result, caplog)
        )

    @pytest.mark.parametrize("action", ["deploy", "stop"])
    def test_a_foreign_app_name_is_refused_too(
        self, cli_module, run, caplog, unprefixed_app, action: str
    ) -> None:
        """Both names are checked before either path is chosen: a deploy may
        end on the App, and a stop tries it."""

        result = _invoke(cli_module, [action, "--model", _QWEN], caplog)

        assert result.exit_code == 2
        run.assert_not_called()
        assert f"Refusing to {action} 'ep-qwen3-embedding-0-6b'" in _output(
            result, caplog
        )


class TestDryRun:
    """The ONLY safe way to exercise the driver — and, since Modal is the
    oracle, it must say what each verdict WOULD lead to."""

    @pytest.mark.usefixtures("no_token", "in_app_root")
    def test_a_deploy_logs_the_create_and_the_undecided_routing(
        self, cli_module, run, caplog
    ) -> None:
        result = _invoke(
            cli_module, ["deploy", "--model", _VOYAGE, "--dry-run"], caplog
        )

        assert result.exit_code == 0
        run.assert_not_called()
        output = _output(result, caplog)
        assert (
            "DRY RUN — would run: modal endpoint create --name "
            f"{_VOYAGE_ENDPOINT} --model {_VOYAGE} --routing-region eu-west"
        ) in output
        assert (
            'DRY RUN — routing is undecided without Modal: on "not available '
            'for dedicated Endpoints" the next command would be: modal deploy '
            f"{_VLLM_SCRIPT}" in output
        )
        assert (
            "DRY RUN — skipped the Modal existence check (no modal process is "
            "started)." in output
        )

    @pytest.mark.usefixtures("no_token", "in_app_root")
    def test_an_llm_dry_run_names_the_sglang_script(
        self, cli_module, run, caplog
    ) -> None:
        result = _invoke(cli_module, ["deploy", "--model", _LFM, "--dry-run"], caplog)

        assert result.exit_code == 0
        run.assert_not_called()
        assert f"the next command would be: modal deploy {_SGLANG_SCRIPT}" in _output(
            result, caplog
        )

    @pytest.mark.usefixtures("no_token", "in_app_root")
    def test_serving_app_dry_runs_only_the_app_deploy(
        self, cli_module, run, caplog
    ) -> None:
        result = _invoke(
            cli_module,
            ["deploy", "--model", _QWEN, "--serving", "app", "--dry-run"],
            caplog,
        )

        assert result.exit_code == 0
        output = _output(result, caplog)
        assert f"DRY RUN — would run: modal deploy {_VLLM_SCRIPT}" in output
        assert "routing is undecided" not in output

    @pytest.mark.usefixtures("no_token")
    def test_a_stop_logs_both_commands(self, cli_module, run, caplog) -> None:
        result = _invoke(cli_module, ["stop", "--model", _VOYAGE, "--dry-run"], caplog)

        assert result.exit_code == 0
        run.assert_not_called()
        output = _output(result, caplog)
        assert (
            f"DRY RUN — would run: modal endpoint stop -y {_VOYAGE_ENDPOINT}" in output
        )
        assert f"DRY RUN — would run: modal app stop -y {_VOYAGE_APP}" in output

    @pytest.mark.usefixtures("no_token", "in_app_root")
    def test_a_dry_run_reads_neither_modal_nor_the_hub(
        self, cli_module, run, caplog, mocker
    ) -> None:
        """A dry run consults NOTHING: no `modal` process and no Hugging Face
        request — which is what makes it safe to run against the real targets.
        """

        mocker.patch.object(modal_router, "httpx", SimpleNamespace(Client=_no_hub))

        result = _invoke(
            cli_module, ["deploy", "--model", _VOYAGE, "--dry-run"], caplog
        )

        assert result.exit_code == 0
        run.assert_not_called()

    @pytest.mark.usefixtures("no_token", "in_app_root")
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

    @pytest.mark.usefixtures("with_token", "in_app_root")
    def test_a_dry_run_argv_is_redacted(self, cli_module, run, caplog, hub) -> None:
        """A dry run cannot know it would be a custom-weights create, so it
        shows the plain create — and nothing it shows may ever carry a token."""

        hub.cards[_VOYAGE] = {"cardData": {"base_model": _QWEN}}

        result = _invoke(
            cli_module, ["deploy", "--model", _VOYAGE, "--dry-run"], caplog
        )

        assert result.exit_code == 0
        run.assert_not_called()
        assert _FAKE_TOKEN not in _output(result, caplog)


@pytest.mark.usefixtures("no_token", "in_app_root")
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
class TestTestCommandDispatch:
    """ONE target family, two kinds (ADR-009 §10): the KIND picks the smoke
    test exactly as it picks the App script, so an operator types the same
    command for an embedding model and for an LLM."""

    def test_an_embedding_entry_awaits_the_embedding_smoke_test(
        self, cli_module, smoke, chat_smoke, caplog
    ) -> None:
        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        smoke.assert_awaited_once_with(_VOYAGE)
        chat_smoke.assert_not_awaited()

    def test_an_llm_entry_awaits_the_chat_smoke_test(
        self, cli_module, smoke, chat_smoke, caplog
    ) -> None:
        """Before this it exited 2 with "No smoke test for LLM entries yet" —
        running the EMBEDDING test on a chat model was never the alternative.
        """

        result = _invoke(cli_module, ["test", "--model", _LFM], caplog)

        assert result.exit_code == 0
        chat_smoke.assert_awaited_once_with(_LFM)
        smoke.assert_not_awaited()

    def test_an_endpoint_routed_llm_takes_the_same_path(
        self, cli_module, chat_smoke, caplog
    ) -> None:
        """Path-blind: ``Qwen/Qwen3.5-0.8B`` is a Dedicated endpoint and gets
        the very same test."""

        result = _invoke(cli_module, ["test", "--model", _QWEN_LLM], caplog)

        assert result.exit_code == 0
        chat_smoke.assert_awaited_once_with(_QWEN_LLM)

    def test_a_failing_chat_smoke_test_exits_one(
        self, cli_module, chat_smoke, caplog
    ) -> None:
        """Story 4: the served model ignored ``response_format``."""

        chat_smoke.side_effect = ModelError(
            "chat completion is not valid JSON: 'Sure! Tokyo is…'"
        )

        result = _invoke(cli_module, ["test", "--model", _LFM], caplog)

        assert result.exit_code == 1
        assert "chat completion is not valid JSON" in _output(result, caplog)

    def test_a_server_side_chat_failure_exits_one(
        self, cli_module, chat_smoke, caplog
    ) -> None:
        chat_smoke.side_effect = ExtractionError(
            "POST https://acme--x.modal.run/v1/chat/completions answered 400, "
            "expected 200.",
            status_code=400,
        )

        result = _invoke(cli_module, ["test", "--model", _LFM], caplog)

        assert result.exit_code == 1
        assert "answered 400" in _output(result, caplog)


@pytest.mark.usefixtures("no_token")
class TestTestCommandWaitsOutProvisioning:
    """#141 round 1 bridged this BY HAND: ``create`` returns while the endpoint
    is still ``provisioning``, so the smoke test waits for the row to read
    ``live`` FIRST — before it resolves a URL there is no server behind
    (ADR-009 §11)."""

    @pytest.fixture
    def wait(self, mocker, cli_module):
        """The wait itself is unit-tested in ``tests/unit/models/
        test_modal_cli.py``; here only its PLACE in the command is."""

        return mocker.patch.object(cli_module, "wait_until_live")

    def test_the_wait_runs_before_the_smoke_test(
        self, cli_module, wait, smoke, caplog, mocker
    ) -> None:
        manager = mocker.MagicMock()
        manager.attach_mock(wait, "wait")
        manager.attach_mock(smoke, "smoke")

        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert [name for name, *_ in manager.mock_calls] == ["wait", "smoke"]
        assert wait.call_args.args[0].repo_id == _VOYAGE

    def test_a_live_endpoint_costs_one_list_call_and_no_wait(
        self, cli_module, run, smoke, caplog
    ) -> None:
        """The chain UNPATCHED, through the CLI: one read-only list call, no
        ``Provisioning`` line, and the smoke test runs as it always did."""

        run.state.endpoints = [_endpoint_row(_VOYAGE_ENDPOINT, status="live")]

        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert _argvs(run) == [["modal", "endpoint", "list", "--json"]]
        smoke.assert_awaited_once_with(_VOYAGE)
        assert "Provisioning" not in _output(result, caplog)

    def test_a_spent_provisioning_budget_exits_one_and_never_smoke_tests(
        self, cli_module, wait, smoke, caplog
    ) -> None:
        """Story 4: Modal never brings the endpoint up. The driver's existing
        ``ModelError`` branch is what turns it into exit 1."""

        wait.side_effect = ModelError(
            "tree-qwen3-5-0-8b is still provisioning after 1800s — check "
            "`modal endpoint list` and the Modal dashboard"
        )

        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        assert result.exit_code == 1
        smoke.assert_not_awaited()
        assert "still provisioning after 1800s" in _output(result, caplog)


@pytest.mark.usefixtures("no_token")
class TestSmokeTestCommand:
    def test_an_unknown_model_exits_two(self, cli_module, smoke, caplog) -> None:
        result = _invoke(cli_module, ["test", "--model", "BAAI/bge-m3"], caplog)

        assert result.exit_code == 2
        smoke.assert_not_awaited()
        assert "Unknown Modal model 'BAAI/bge-m3'" in _output(result, caplog)

    def test_a_failing_smoke_test_exits_one(self, cli_module, smoke, caplog) -> None:
        """The message carries the numbers, the exit code carries the
        verdict."""

        smoke.side_effect = ModelError("expected 2048 dims, got 1024 — ...")

        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        assert result.exit_code == 1
        assert "expected 2048 dims, got 1024" in _output(result, caplog)

    def test_a_wrong_proxy_token_exits_one_with_the_polls_message(
        self, cli_module, smoke, caplog
    ) -> None:
        """#144: the health poll gives up after ONE attempt on a 401, and the
        operator reads WHICH variables to check — within seconds, not after
        the whole 600 s budget."""

        smoke.side_effect = ModelError(
            "Health poll of https://acme--x.modal.run/health returned HTTP 401 "
            "— not a cold start, giving up after 1 attempt. Check "
            "MODAL_PROXY_TOKEN_ID and MODAL_PROXY_TOKEN_SECRET in .env."
        )

        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        output = _output(result, caplog)
        assert result.exit_code == 1
        assert "giving up after 1 attempt" in output
        assert "MODAL_PROXY_TOKEN_ID" in output
        # ADR-009 §9: the HF_TOKEN hint never fires under a wrong Proxy token,
        # which is exactly why a fail-fast poll raises ModelError, not
        # ExtractionError.
        assert "HF_TOKEN" not in output

    def test_a_spent_warmup_budget_exits_one_with_the_polls_message(
        self, cli_module, smoke, caplog
    ) -> None:
        """#144: a server that never comes up is reported as the transient
        failure it is, naming the budget it spent."""

        smoke.side_effect = ExtractionError(
            "Health poll of https://acme--x.modal.run/health gave up after 600s "
            "(deadline 600s); last result: HTTP 503",
            status_code=503,
        )

        result = _invoke(cli_module, ["test", "--model", _VOYAGE], caplog)

        output = _output(result, caplog)
        assert result.exit_code == 1
        assert "gave up after 600s (deadline 600s)" in output
        assert "last result: HTTP 503" in output


@pytest.mark.usefixtures("with_token", "in_app_root")
class TestHfToken:
    """Story 3: a private fine-tune deploys, and the token appears nowhere."""

    @pytest.fixture(autouse=True)
    def _a_fine_tune(self, run, hub) -> None:
        """voyage-4-nano, faked into a fine-tune of a catalog model, is the
        only shape that carries a token at all."""

        hub.cards[_VOYAGE] = {"cardData": {"base_model": _QWEN}}
        run.state.results = [_failed(_VOYAGE, _NOT_IN_CATALOG), _ok()]

    def test_custom_weights_get_the_token_appended(
        self, cli_module, run, caplog
    ) -> None:
        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert run.call_args.args[0][-2:] == ["--custom-hf-token", _FAKE_TOKEN]

    def test_the_logged_argv_is_redacted(self, cli_module, run, caplog) -> None:
        """The positive half is the canary: it proves the capture works, so
        the absence assertion below it means something."""

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        output = _output(result, caplog)
        assert "--custom-hf-token ***" in output
        assert _FAKE_TOKEN not in output

    def test_a_plain_create_gets_no_token(self, cli_module, run, caplog) -> None:
        """Modal documents the flag as the token "for private
        --custom-hf-repo", so a create without custom weights never gets it."""

        _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

        assert "--custom-hf-token" not in run.call_args.args[0]

    def test_a_failing_command_never_leaks_the_token(
        self, cli_module, run, caplog
    ) -> None:
        """``check=True`` would raise ``CalledProcessError``, whose message is
        the full argv — the token. The failure path is the leak path."""

        run.state.results = [
            _failed(_VOYAGE, _NOT_IN_CATALOG),
            _failed(_VOYAGE, "Token missing."),
        ]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        output = _output(result, caplog)
        assert "modal command failed (exit 1)" in output
        assert "--custom-hf-token ***" in output
        assert _FAKE_TOKEN not in output

    def test_a_missing_modal_cli_says_how_to_install_it_without_the_argv(
        self, cli_module, run, caplog
    ) -> None:
        """Found by QA on #139: without the extra installed,
        ``subprocess.run`` raised a bare ``FileNotFoundError`` whose traceback
        printed the full argv — which on a custom-weights deploy is the
        Hugging Face token."""

        run.state.error = FileNotFoundError(2, "No such file or directory: 'modal'")

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 2
        output = _output(result, caplog)
        assert "The `modal` CLI is not installed" in output
        assert "uv --directory apps/memory sync --extra local-models" in output
        assert _FAKE_TOKEN not in output

    def test_a_failure_with_a_token_set_logs_no_hint(
        self, cli_module, run, caplog
    ) -> None:
        """The hint is about a MISSING token; with one set it is noise."""

        run.state.results = [
            _failed(_VOYAGE, _NOT_IN_CATALOG),
            _failed(_VOYAGE, "401 Client Error"),
        ]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert "private or gated Hugging Face repo" not in _output(result, caplog)


@pytest.mark.usefixtures("no_token", "in_app_root")
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
        """It is not even a failure any more — it is a route to the App."""

        run.state.results = [_failed(_VOYAGE, _NOT_IN_CATALOG), _ok()]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 0
        assert self._hints(caplog) == []

    def test_a_gated_download_gets_exactly_one_hint(
        self, cli_module, run, caplog
    ) -> None:
        run.state.results = [
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="GatedRepoError: access to model is restricted",
            )
        ]

        result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

        assert result.exit_code == 1
        assert len(self._hints(caplog)) == 1
        assert caplog.records[-1].levelname == "WARNING"

    def test_a_failed_app_deploy_gets_no_hint(self, cli_module, run, caplog) -> None:
        """``modal deploy`` streams to the terminal and is not captured, so
        there is no text to judge — and a gated download fails at container
        START, which is the smoke test's business."""

        run.state.results = [
            _failed(_VOYAGE, _NOT_IN_CATALOG),
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout=None, stderr=None
            ),
        ]

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


@pytest.mark.usefixtures("with_token", "in_app_root")
def test_endpoint_create_output_is_captured_and_the_app_deploy_streams(
    cli_module, run, caplog
) -> None:
    """Only the create is captured — the router must READ its verdict.
    ``modal deploy`` builds an image and must keep streaming; what IS captured
    is logged, redacted, line by line."""

    run.state.results = [
        subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"Endpoint created\ntoken was {_FAKE_TOKEN}\n",
            stderr="",
        )
    ]

    result = _invoke(cli_module, ["deploy", "--model", _QWEN], caplog)

    assert result.exit_code == 0
    create_call = run.call_args
    assert create_call.kwargs["capture_output"] is True
    assert create_call.kwargs["text"] is True
    assert create_call.kwargs["check"] is False
    output = _output(result, caplog)
    assert "modal: Endpoint created" in output
    assert "modal: token was ***" in output
    assert _FAKE_TOKEN not in output

    caplog.clear()
    run.state.results = [_failed(_VOYAGE, _NOT_IN_CATALOG), _ok()]
    _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

    assert run.call_args.args[0] == ["modal", "deploy", _VLLM_SCRIPT]
    assert run.call_args.kwargs["capture_output"] is False


@pytest.mark.usefixtures("no_token", "in_app_root")
def test_a_token_changes_no_app_argv_and_reaches_no_log(
    cli_module, run, caplog, mocker
) -> None:
    """On the App path the token travels as a Modal Secret built inside the
    deploy script, never as ``--custom-hf-token`` — so a set token leaves the
    argv byte-identical and appears in no log line."""

    mocker.patch.object(cli_module.settings, "hf_token", SecretStr(_FAKE_TOKEN))
    run.state.results = [_failed(_VOYAGE, _NOT_IN_CATALOG), _ok()]

    result = _invoke(cli_module, ["deploy", "--model", _VOYAGE], caplog)

    assert run.call_args.args[0] == ["modal", "deploy", _VLLM_SCRIPT]
    assert _FAKE_TOKEN not in str(run.call_args.args[0])
    assert _FAKE_TOKEN not in _output(result, caplog)


def test_no_module_on_the_deploy_path_uses_check_true(cli_module) -> None:
    """``subprocess.run(..., check=True)`` raises ``CalledProcessError``, which
    prints its full argv — the token. Banned by ADR-009 §9, on every side of
    the door."""

    for module in (cli_module, modal_cli, modal_router):
        assert "check=True" not in Path(module.__file__).read_text()
