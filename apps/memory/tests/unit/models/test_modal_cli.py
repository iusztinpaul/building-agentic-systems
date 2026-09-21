"""Unit tests for ``tree.models.modal_cli`` — the rails around the CLI.

This module is the reason task #143 exists: on 2026-09-20 a deploy of ours
overwrote a **Dedicated endpoint** an operator had built by hand, because the
driver wrote without looking and stopped without asking whose name it was.
So the assertions here are about what does NOT happen — no process started
under a dry run, no ``modal`` command for a foreign name, no deploy over
something live, and no "nothing is there" conclusion drawn from a list call
that failed.

``subprocess.run`` is replaced by the shared ``run`` fixture
(``tests/unit/conftest.py``), which is also the only place in the test tree
that leaves ``TREE_MODAL_DRY_RUN``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import pytest

from tree.config.app_config import ModalEmbeddingModelConfig, ModalLLMModelConfig
from tree.models import modal_warmup
from tree.models.exceptions import ModelError
from tree.models.modal_cli import (
    DRY_RUN_ENV,
    PROVISIONING_DEADLINE_S,
    ModalGuardError,
    assert_owned_name,
    endpoint_status,
    existing_kind,
    guard_deploy,
    is_dry_run,
    run_modal,
    wait_until_live,
)

_REPO_ID = "voyageai/voyage-4-nano"
_ENDPOINT_NAME = "tree-voyage-4-nano"
_APP_NAME = "ep-tree-voyage-4-nano"
_FAKE_TOKEN = "hf_secret123"

# The LLM whose live create sat on `provisioning` for 9m25s (`tasks/141`) —
# the model every wait message below is written about.
_LLM_REPO_ID = "Qwen/Qwen3.5-0.8B"
_LLM_ENDPOINT_NAME = "tree-qwen3-5-0-8b"

_ENDPOINT_LIST = ["modal", "endpoint", "list", "--json"]
_APP_LIST = ["modal", "app", "list", "--json"]


@pytest.fixture
def entry() -> ModalEmbeddingModelConfig:
    """A catalog entry built here, not read from YAML: these tests are about
    the guard, not about the seeds."""

    return ModalEmbeddingModelConfig(repo_id=_REPO_ID, native_dimensions=2048)


@pytest.fixture
def llm_entry() -> ModalLLMModelConfig:
    """``Qwen/Qwen3.5-0.8B`` -> ``tree-qwen3-5-0-8b``, the endpoint the wait
    was measured on."""

    return ModalLLMModelConfig(repo_id=_LLM_REPO_ID)


def _endpoint_row(name: str, status: str = "live") -> dict[str, str]:
    """A ``modal endpoint list --json`` row (modal 1.5.5 columns).

    ``live`` and ``provisioning`` are the two statuses the live run saw
    (``tasks/141``, 2026-09-21); the id is fake, as every id here is.
    """

    return {
        "name": name,
        "endpoint_id": "ep-FAKE0000000000000000",
        "status": status,
        "created_at": "2026-08-24T10:00:00Z",
        "created_by": "someone",
    }


def _clock(*readings: float) -> Callable[[], float]:
    """A monotonic clock handing out ``readings`` in order, then repeating the
    last one — so a test SAYS when each elapsed line is measured instead of
    depending on an auto-increment nobody can read off the assertion."""

    remaining = list(readings)

    def _read() -> float:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return _read


def _wait_lines(caplog) -> list[str]:
    """What the WAIT logged: the CLI door's own ``Running: …`` line, one per
    list call, belongs to :func:`run_modal` and is asserted on there."""

    return [
        record.getMessage()
        for record in caplog.records
        if not record.getMessage().startswith("Running: ")
    ]


def _app_row(description: str, state: str = "deployed") -> dict[str, str]:
    """A ``modal app list --json`` row (modal 1.5.5 columns)."""

    return {
        "app_id": "ap-abcdefghijklmnopqrstuv",
        "description": description,
        "state": state,
        "tasks": "0",
        "created_at": "2026-09-20T10:00:00Z",
        "stopped_at": "",
    }


class TestAssertOwnedName:
    """The prefix IS the ownership mark (ADR-009 §3): no tags, no registry."""

    @pytest.mark.parametrize("name", ["tree-x", "ep-tree-x"])
    def test_a_prefixed_name_is_ours(self, name: str) -> None:
        assert assert_owned_name(name, "deploy") is None

    @pytest.mark.parametrize(
        "name",
        [
            # The hand-made endpoint the incident overwrote, and its app.
            "qwen3-embedding-0-6b",
            "ep-qwen3-embedding-0-6b",
            # `startswith`, not `in`: a name merely CONTAINING the letters is
            # not ours.
            "ep-treex",
            "",
        ],
    )
    @pytest.mark.parametrize("action", ["deploy", "stop"])
    def test_a_foreign_name_is_refused(self, name: str, action: str) -> None:
        with pytest.raises(ModelError) as excinfo:
            assert_owned_name(name, action)

        message = str(excinfo.value)
        assert f"Refusing to {action} {name!r}" in message
        assert "'tree-' prefix" in message
        assert "this project did not create it" in message


class TestExistingKind:
    """Two read-only calls, and a closed door when either cannot be read."""

    def test_a_live_endpoint_of_ours_is_found_by_name(self, entry, run) -> None:
        run.state.endpoints = [_endpoint_row(_ENDPOINT_NAME)]

        assert existing_kind(entry) == "endpoint"

    def test_a_live_endpoint_short_circuits_the_app_list(self, entry, run) -> None:
        """The ENDPOINT list is the leg that protects (ADR-009 Consequences),
        so it is read first and a hit ends the lookup — the app list, which
        cannot even see the ``ep-*`` app behind a Dedicated endpoint, is never
        asked."""

        run.state.endpoints = [_endpoint_row(_ENDPOINT_NAME)]
        run.state.apps = [_app_row(_APP_NAME)]

        assert existing_kind(entry) == "endpoint"

        assert [call.args[0] for call in run.call_args_list] == [_ENDPOINT_LIST]

    def test_a_provisioning_endpoint_is_already_an_endpoint(self, entry, run) -> None:
        """The 2-10 minutes between ``create`` and ``live`` are exactly when an
        operator repeats the deploy — and a second create over a provisioning
        endpoint is the overwrite the guard exists for."""

        run.state.endpoints = [_endpoint_row(_ENDPOINT_NAME, status="provisioning")]

        assert existing_kind(entry) == "endpoint"

    def test_a_live_app_of_ours_is_found_by_description(self, entry, run) -> None:
        run.state.apps = [_app_row(_APP_NAME)]

        assert existing_kind(entry) == "app"

    def test_a_stopped_app_is_not_live(self, entry, run) -> None:
        """``modal app list`` keeps recently stopped apps, so the state is
        what says whether a deploy would overwrite anything."""

        run.state.apps = [_app_row(_APP_NAME, state="stopped")]

        assert existing_kind(entry) == "none"

    def test_empty_lists_are_a_clean_slate(self, entry, run) -> None:
        assert existing_kind(entry) == "none"

    def test_the_operators_own_unprefixed_endpoint_is_ignored(self, entry, run) -> None:
        """The whole point of the ``tree-`` namespace: the hand-made
        ``ep-qwen3-embedding-0-6b`` is not our name, so it neither blocks a
        deploy nor is ever touched by one."""

        run.state.endpoints = [_endpoint_row("qwen3-embedding-0-6b")]
        run.state.apps = [_app_row("ep-qwen3-embedding-0-6b")]

        assert existing_kind(entry) == "none"

    def test_both_lists_are_read_only_json_calls(self, entry, run) -> None:
        existing_kind(entry)

        argvs = [call.args[0] for call in run.call_args_list]
        assert argvs == [_ENDPOINT_LIST, _APP_LIST]
        for call in run.call_args_list:
            assert call.kwargs["check"] is False
            assert call.kwargs["capture_output"] is True

    def test_a_failed_list_cannot_rule_out_an_overwrite(self, entry, run) -> None:
        run.state.list_returncode = 1

        with pytest.raises(ModalGuardError) as excinfo:
            existing_kind(entry)

        assert excinfo.value.reason == "could not list Modal endpoints (exit 1)"
        assert (
            f"Refusing to deploy {_REPO_ID}: could not list Modal endpoints "
            "(exit 1), so an overwrite cannot be ruled out. Pass FORCE=yes to "
            "skip the check." == str(excinfo.value)
        )

    @pytest.mark.parametrize(
        "stdout", ["not json", '{"name": "tree-voyage-4-nano"}', ""]
    )
    def test_output_that_is_not_a_json_list_is_a_closed_door(
        self, entry, run, stdout: str
    ) -> None:
        """The human-readable table is never parsed as a fallback — and a JSON
        object is not the list the columns promise."""

        run.state.list_stdout = stdout

        with pytest.raises(ModalGuardError) as excinfo:
            existing_kind(entry)

        assert "invalid JSON" in str(excinfo.value)

    def test_a_dry_run_is_never_mistaken_for_an_empty_workspace(
        self, entry, monkeypatch
    ) -> None:
        """No ``run`` fixture, so the suite-wide dry run is still on: the door
        starts no process, and a check that did not run is not a pass."""

        monkeypatch.setenv(DRY_RUN_ENV, "1")

        with pytest.raises(ModalGuardError) as excinfo:
            existing_kind(entry)

        assert "dry run" in str(excinfo.value)


class TestGuardDeploy:
    """The matrix of ADR-009 §3, keyed on what the deploy would CREATE."""

    @staticmethod
    def _arrange(run, existing: str) -> None:
        if existing == "endpoint":
            run.state.endpoints = [_endpoint_row(_ENDPOINT_NAME)]
        elif existing == "app":
            run.state.apps = [_app_row(_APP_NAME)]

    @pytest.mark.parametrize("target", ["endpoint", "app"])
    @pytest.mark.parametrize("existing", ["none", "endpoint", "app"])
    def test_the_matrix_without_force(
        self, entry, run, caplog, target: str, existing: str
    ) -> None:
        self._arrange(run, existing)
        refuses = existing == "endpoint" or (target == "endpoint" and existing == "app")

        with caplog.at_level(logging.WARNING):
            if refuses:
                with pytest.raises(ModalGuardError) as excinfo:
                    guard_deploy(entry, target, False, path="vllm")
                assert f"Refusing to deploy {_REPO_ID} via vllm" in str(excinfo.value)
            else:
                assert guard_deploy(entry, target, False, path="vllm") is None

        assert caplog.records == []

    @pytest.mark.parametrize("target", ["endpoint", "app"])
    @pytest.mark.parametrize("existing", ["none", "endpoint", "app"])
    def test_the_matrix_with_force_never_refuses(
        self, entry, run, caplog, target: str, existing: str
    ) -> None:
        self._arrange(run, existing)
        warns = existing == "endpoint" or (target == "endpoint" and existing == "app")

        with caplog.at_level(logging.WARNING):
            assert guard_deploy(entry, target, True, path="vllm") is None

        messages = [record.getMessage() for record in caplog.records]
        if warns:
            name = _ENDPOINT_NAME if existing == "endpoint" else _APP_NAME
            label = "Dedicated endpoint" if existing == "endpoint" else "app"
            assert messages == [
                f"FORCE=yes: deploying over the existing {label} {name!r}."
            ]
        else:
            assert messages == []

    def test_a_refusal_names_the_endpoint_and_how_to_stop_it(self, entry, run) -> None:
        """The operator forgot the ``-stop``, which needs no SERVING= at all
        now that a stop is path-blind."""

        run.state.endpoints = [_endpoint_row(_ENDPOINT_NAME)]

        with pytest.raises(ModalGuardError) as excinfo:
            guard_deploy(entry, "app", False, path="app")

        assert str(excinfo.value) == (
            f"Refusing to deploy {_REPO_ID} via app: {_ENDPOINT_NAME!r} "
            "already exists on Modal as a Dedicated endpoint. Stop it first "
            f"(make memory-deploy-model-stop MODEL={_REPO_ID}) or pass "
            "FORCE=yes to deploy over it."
        )

    def test_a_refusal_on_an_app_names_the_app(self, entry, run) -> None:
        run.state.apps = [_app_row(_APP_NAME)]

        with pytest.raises(ModalGuardError) as excinfo:
            guard_deploy(entry, "endpoint", False, path="endpoint")

        message = str(excinfo.value)
        assert f"{_APP_NAME!r} already exists on Modal as an app" in message
        assert f"make memory-deploy-model-stop MODEL={_REPO_ID}" in message

    def test_a_pre_read_kind_costs_no_second_lookup(self, entry, run) -> None:
        """The router reads the workspace once, to ROUTE, and hands the same
        reading to the guard — so the guard lists nothing of its own."""

        run.state.endpoints = [_endpoint_row(_ENDPOINT_NAME)]

        with pytest.raises(ModalGuardError):
            guard_deploy(entry, "endpoint", False, path="endpoint", kind="endpoint")

        run.assert_not_called()

    def test_a_redeploy_of_our_own_app_is_a_normal_update(self, entry, run) -> None:
        """Story 3: the operator bumped ``revision`` and deploys again."""

        run.state.apps = [_app_row(_APP_NAME)]

        assert guard_deploy(entry, "app", False, path="app") is None

    def test_a_failed_list_under_force_is_only_a_warning(
        self, entry, run, caplog
    ) -> None:
        run.state.list_returncode = 1

        with caplog.at_level(logging.WARNING):
            assert guard_deploy(entry, "app", True, path="vllm") is None

        assert [record.getMessage() for record in caplog.records] == [
            "FORCE=yes: could not list Modal endpoints (exit 1) — deploying anyway."
        ]


class TestEndpointStatus:
    """The wait's ONE read. Unlike the guard's ``_list_rows`` it fails OPEN:
    a list nobody can read must not stop a smoke test, which gives the verdict
    either way."""

    @pytest.mark.parametrize("status", ["provisioning", "live"])
    def test_it_returns_our_rows_status(self, entry, run, caplog, status: str) -> None:
        run.state.endpoints = [
            _endpoint_row("qwen3-embedding-0-6b", status="live"),
            _endpoint_row(_ENDPOINT_NAME, status=status),
        ]

        with caplog.at_level(logging.WARNING):
            assert endpoint_status(entry) == status

        assert caplog.records == []

    def test_a_workspace_without_our_row_has_no_status(
        self, entry, run, caplog
    ) -> None:
        """Two ways to have no row and one answer: the operator's hand-made
        ``qwen3-embedding-0-6b`` is not our name, and an App is not listed here
        at all."""

        run.state.endpoints = [_endpoint_row("qwen3-embedding-0-6b")]

        with caplog.at_level(logging.WARNING):
            assert endpoint_status(entry) is None

        assert caplog.records == []

    @pytest.mark.parametrize(
        "state,detail",
        [
            ({"list_returncode": 1}, "exit 1"),
            ({"list_stdout": "not json"}, "invalid JSON"),
        ],
        ids=["a failed list", "output that is not JSON"],
    )
    def test_a_list_it_cannot_read_is_one_warning(
        self, entry, run, caplog, state: dict[str, object], detail: str
    ) -> None:
        """Story 3: Modal's list is down during a test."""

        for key, value in state.items():
            setattr(run.state, key, value)

        with caplog.at_level(logging.WARNING):
            assert endpoint_status(entry) is None

        assert [record.getMessage() for record in caplog.records] == [
            f"could not read the endpoint list ({detail}) — not waiting for "
            "provisioning"
        ]

    def test_a_dry_run_cannot_look_either(self, entry, caplog, monkeypatch) -> None:
        """No ``run`` fixture, so the suite-wide dry run is still on: under
        ``DRY_RUN=yes`` the door starts no process, and a look we did not take
        is not a status."""

        monkeypatch.setenv(DRY_RUN_ENV, "1")

        with caplog.at_level(logging.WARNING):
            assert endpoint_status(entry) is None

        assert [record.getMessage() for record in caplog.records] == [
            "could not read the endpoint list (dry run) — not waiting for provisioning"
        ]

    def test_it_is_one_read_only_list_call(self, entry, run) -> None:
        endpoint_status(entry)

        assert [call.args[0] for call in run.call_args_list] == [_ENDPOINT_LIST]
        assert run.call_args.kwargs["check"] is False
        assert run.call_args.kwargs["capture_output"] is True


class TestWaitUntilLive:
    """``modal endpoint create`` is ASYNCHRONOUS (measured 2026-09-21: ~4 s to
    return, `live` after 2m15s / 9m25s), so ``-test`` bridges the gap itself."""

    def test_the_deadline_is_three_times_the_slowest_create(self) -> None:
        """565 s measured against a 600 s warm-up budget is too tight to share
        ``modal.warmup_deadline_s`` — hence a constant of its own."""

        assert PROVISIONING_DEADLINE_S == 1800.0

    def test_no_row_returns_at_once(self, llm_entry, run, caplog) -> None:
        """Story 2: an App has no endpoint row, so the health poller starts
        immediately, as it did before this wait existed."""

        sleeps: list[float] = []

        with caplog.at_level(logging.INFO):
            assert wait_until_live(llm_entry, sleep=sleeps.append) is None

        assert [call.args[0] for call in run.call_args_list] == [_ENDPOINT_LIST]
        assert sleeps == []
        assert "Provisioning" not in caplog.text

    def test_provisioning_then_live(self, llm_entry, run, caplog) -> None:
        """Story 1: the operator tests seconds after the create and never opens
        ``modal endpoint list`` themselves."""

        sleeps: list[float] = []
        statuses = iter(["provisioning", "live"])

        def _sleep(seconds: float) -> None:
            sleeps.append(seconds)
            run.state.endpoints = [
                _endpoint_row(_LLM_ENDPOINT_NAME, status=next(statuses))
            ]

        run.state.endpoints = [_endpoint_row(_LLM_ENDPOINT_NAME, status="provisioning")]

        with caplog.at_level(logging.INFO):
            wait_until_live(llm_entry, sleep=_sleep, clock=_clock(0.0, 0.0, 5.0, 548.0))

        assert sleeps == [5.0, 7.5]
        assert _wait_lines(caplog) == [
            f"Provisioning: {_LLM_ENDPOINT_NAME} is not live yet — 0s/1800s",
            f"Provisioning: {_LLM_ENDPOINT_NAME} is not live yet — 5s/1800s",
            f"Live: {_LLM_ENDPOINT_NAME} after 548s",
        ]

    @pytest.mark.parametrize(
        "constants,expected",
        [
            ({"MAX_INTERVAL_S": 6.0}, [5.0, 6.0]),
            ({"INITIAL_INTERVAL_S": 2.0, "BACKOFF_FACTOR": 2.0}, [2.0, 4.0]),
        ],
        ids=["the cap", "the first interval and the factor"],
    )
    def test_the_schedule_is_the_pollers(
        self, llm_entry, run, mocker, constants: dict[str, float], expected: list[float]
    ) -> None:
        """5 s x 1.5 capped at 15 s — the health poller's own three constants,
        read from ITS module at call time rather than copied: re-tune the
        poller and this wait follows, which is the whole point of not owning a
        second schedule."""

        for name, value in constants.items():
            mocker.patch.object(modal_warmup, name, value)
        sleeps: list[float] = []
        statuses = iter(["provisioning", "live"])

        def _sleep(seconds: float) -> None:
            sleeps.append(seconds)
            run.state.endpoints = [
                _endpoint_row(_LLM_ENDPOINT_NAME, status=next(statuses))
            ]

        run.state.endpoints = [_endpoint_row(_LLM_ENDPOINT_NAME, status="provisioning")]

        wait_until_live(llm_entry, sleep=_sleep, clock=_clock(0.0))

        assert sleeps == expected

    def test_the_deadline_is_a_model_error(self, llm_entry, run, caplog) -> None:
        """Story 4: Modal leaves the endpoint provisioning forever. The
        driver's existing ``ModelError`` branch turns this into exit 1."""

        run.state.endpoints = [_endpoint_row(_LLM_ENDPOINT_NAME, status="provisioning")]
        sleeps: list[float] = []

        with pytest.raises(ModelError) as excinfo:
            wait_until_live(
                llm_entry, sleep=sleeps.append, clock=_clock(0.0, 0.0, 1801.0)
            )

        assert str(excinfo.value) == (
            f"{_LLM_ENDPOINT_NAME} is still provisioning after 1800s — check "
            "`modal endpoint list` and the Modal dashboard"
        )
        assert sleeps == [5.0]

    def test_an_unknown_status_is_not_waited_on(self, llm_entry, run, caplog) -> None:
        """Waiting out a state we have never seen is guesswork — the smoke
        test that follows says what it means."""

        run.state.endpoints = [_endpoint_row(_LLM_ENDPOINT_NAME, status="failed")]
        sleeps: list[float] = []

        with caplog.at_level(logging.INFO):
            assert wait_until_live(llm_entry, sleep=sleeps.append) is None

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelname == "WARNING"
        ]
        assert warnings == [f"{_LLM_ENDPOINT_NAME} has status 'failed' — not waiting"]
        assert _wait_lines(caplog) == warnings
        assert sleeps == []

    @pytest.mark.parametrize(
        "state,detail",
        [
            ({"list_returncode": 1}, "exit 1"),
            ({"list_stdout": "not json"}, "invalid JSON"),
        ],
        ids=["a failed list", "output that is not JSON"],
    )
    def test_a_list_that_goes_unreadable_mid_wait_claims_nothing(
        self, llm_entry, run, caplog, state: dict[str, object], detail: str
    ) -> None:
        """Story 3, hit one poll INTO the wait: a look we could not take is not
        a ``live`` reading. The wait ends on ``endpoint_status``'s single
        warning and the smoke test gives the verdict — saying ``Live:`` right
        after admitting the list was unreadable would be an invention."""

        sleeps: list[float] = []

        def _sleep(seconds: float) -> None:
            sleeps.append(seconds)
            for key, value in state.items():
                setattr(run.state, key, value)

        run.state.endpoints = [_endpoint_row(_LLM_ENDPOINT_NAME, status="provisioning")]

        with caplog.at_level(logging.INFO):
            assert wait_until_live(llm_entry, sleep=_sleep, clock=_clock(0.0)) is None

        assert _wait_lines(caplog) == [
            f"Provisioning: {_LLM_ENDPOINT_NAME} is not live yet — 0s/1800s",
            f"could not read the endpoint list ({detail}) — not waiting for "
            "provisioning",
        ]
        assert sleeps == [5.0]

    def test_a_row_that_vanishes_mid_wait_claims_nothing(
        self, llm_entry, run, caplog
    ) -> None:
        """A ``-stop`` racing the wait takes our row away mid-poll. Nothing
        read ``live``, so nothing says ``Live:``.

        Nothing says the row is gone either, and that is deliberate:
        :func:`endpoint_status` answers ``None`` for a vanished row, an
        unreadable list and a dry run alike, so a "no longer listed" line here
        would fire in the case above too — where the row may well still be
        provisioning and we merely could not look. The exact-list assertion
        pins that silence, so adding such a line has to be a decision, not a
        drift.
        """

        sleeps: list[float] = []

        def _sleep(seconds: float) -> None:
            sleeps.append(seconds)
            run.state.endpoints = []

        run.state.endpoints = [_endpoint_row(_LLM_ENDPOINT_NAME, status="provisioning")]

        with caplog.at_level(logging.INFO):
            assert wait_until_live(llm_entry, sleep=_sleep, clock=_clock(0.0)) is None

        assert _wait_lines(caplog) == [
            f"Provisioning: {_LLM_ENDPOINT_NAME} is not live yet — 0s/1800s"
        ]
        assert sleeps == [5.0]

    def test_a_status_that_turns_unknown_mid_wait_is_one_warning_and_no_claim(
        self, llm_entry, run, caplog
    ) -> None:
        """``provisioning`` -> ``failed``: the same ONE warning as a first-read
        ``failed``, with no ``Live:`` line following it to contradict it."""

        sleeps: list[float] = []

        def _sleep(seconds: float) -> None:
            sleeps.append(seconds)
            run.state.endpoints = [_endpoint_row(_LLM_ENDPOINT_NAME, status="failed")]

        run.state.endpoints = [_endpoint_row(_LLM_ENDPOINT_NAME, status="provisioning")]

        with caplog.at_level(logging.INFO):
            assert wait_until_live(llm_entry, sleep=_sleep, clock=_clock(0.0)) is None

        assert _wait_lines(caplog) == [
            f"Provisioning: {_LLM_ENDPOINT_NAME} is not live yet — 0s/1800s",
            f"{_LLM_ENDPOINT_NAME} has status 'failed' — not waiting",
        ]
        assert sleeps == [5.0]


class TestRunModal:
    """The ONE door every ``modal`` process goes through."""

    def test_a_dry_run_starts_no_process(self, run, caplog) -> None:
        with caplog.at_level(logging.INFO):
            result = run_modal(["modal", "app", "list"], dry_run=True)

        assert result is None
        run.assert_not_called()
        assert "DRY RUN — would run: modal app list" in caplog.text

    def test_the_env_var_turns_it_on_without_the_flag(
        self, run, caplog, monkeypatch
    ) -> None:
        """``DRY_RUN=yes`` reaches a deploy script's grandchild process as an
        env var, so the door reads it at CALL time."""

        monkeypatch.setenv(DRY_RUN_ENV, "1")

        with caplog.at_level(logging.INFO):
            assert run_modal(["modal", "app", "list"]) is None

        run.assert_not_called()

    @pytest.mark.parametrize("value", ["", "0"])
    def test_an_empty_or_zero_env_var_is_not_a_dry_run(
        self, run, monkeypatch, value: str
    ) -> None:
        monkeypatch.setenv(DRY_RUN_ENV, value)

        assert is_dry_run() is False
        assert run_modal(["modal", "app", "list"]) is not None

    def test_a_dry_run_argv_is_redacted(self, run, caplog) -> None:
        with caplog.at_level(logging.INFO):
            run_modal(
                ["modal", "endpoint", "create", "--custom-hf-token", _FAKE_TOKEN],
                dry_run=True,
            )

        assert "--custom-hf-token ***" in caplog.text
        assert _FAKE_TOKEN not in caplog.text

    def test_it_never_runs_with_check_true(self, run) -> None:
        run_modal(["modal", "app", "list"])

        assert run.call_args.kwargs["check"] is False

    def test_a_missing_cli_says_how_to_install_it_without_the_argv(
        self, run, caplog
    ) -> None:
        run.state.error = FileNotFoundError(2, "No such file or directory: 'modal'")

        with pytest.raises(ModelError) as excinfo:
            run_modal(["modal", "endpoint", "create", "--custom-hf-token", _FAKE_TOKEN])

        assert "The `modal` CLI is not installed" in str(excinfo.value)
        assert _FAKE_TOKEN not in str(excinfo.value)
        assert _FAKE_TOKEN not in caplog.text


def test_the_guard_rows_match_the_pinned_clients_columns() -> None:
    """The guard reads ``name`` / ``description`` / ``state``; these are the
    snake_cased column titles of modal 1.5.5 (``cli/endpoint.py:307``,
    ``cli/app.py:113-119``, ``cli/utils.py:132``). A key that drifts makes the
    guard blind, so the fixtures above are asserted to BE those columns."""

    assert set(_endpoint_row("x")) == {
        "name",
        "endpoint_id",
        "status",
        "created_at",
        "created_by",
    }
    assert set(_app_row("x")) == {
        "app_id",
        "description",
        "state",
        "tasks",
        "created_at",
        "stopped_at",
    }
    # And the rows survive the JSON round trip the guard actually parses.
    assert json.loads(json.dumps([_app_row("x")]))[0]["description"] == "x"
