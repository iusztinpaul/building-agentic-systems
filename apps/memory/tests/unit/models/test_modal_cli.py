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

import pytest

from tree.config.app_config import ModalEmbeddingModelConfig
from tree.models.exceptions import ModelError
from tree.models.modal_cli import (
    DRY_RUN_ENV,
    ModalGuardError,
    assert_owned_name,
    existing_kind,
    guard_deploy,
    is_dry_run,
    run_modal,
)

_REPO_ID = "voyageai/voyage-4-nano"
_ENDPOINT_NAME = "tree-voyage-4-nano"
_APP_NAME = "ep-tree-voyage-4-nano"
_FAKE_TOKEN = "hf_secret123"

_ENDPOINT_LIST = ["modal", "endpoint", "list", "--json"]
_APP_LIST = ["modal", "app", "list", "--json"]


@pytest.fixture
def entry() -> ModalEmbeddingModelConfig:
    """A catalog entry built here, not read from YAML: these tests are about
    the guard, not about the seeds."""

    return ModalEmbeddingModelConfig(
        repo_id=_REPO_ID, base_model=_REPO_ID, native_dimensions=2048
    )


def _endpoint_row(name: str) -> dict[str, str]:
    """A ``modal endpoint list --json`` row (modal 1.5.5 columns)."""

    return {
        "name": name,
        "endpoint_id": "ep-abcdefghijklmnopqrstuv",
        "status": "running",
        "created_at": "2026-08-24T10:00:00Z",
        "created_by": "someone",
    }


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
        """Story 2: the operator walked the ladder and forgot the ``-stop``."""

        run.state.endpoints = [_endpoint_row(_ENDPOINT_NAME)]

        with pytest.raises(ModalGuardError) as excinfo:
            guard_deploy(entry, "app", False, path="vllm")

        assert str(excinfo.value) == (
            f"Refusing to deploy {_REPO_ID} via vllm: {_ENDPOINT_NAME!r} "
            "already exists on Modal as a Dedicated endpoint. Stop it first "
            f"(make memory-deploy-embedding-model-stop MODEL={_REPO_ID} "
            "SERVING=endpoint) or pass FORCE=yes to deploy over it."
        )

    def test_a_refusal_on_an_app_names_the_app(self, entry, run) -> None:
        run.state.apps = [_app_row(_APP_NAME)]

        with pytest.raises(ModalGuardError) as excinfo:
            guard_deploy(entry, "endpoint", False, path="endpoint")

        message = str(excinfo.value)
        assert f"{_APP_NAME!r} already exists on Modal as an app" in message
        assert "SERVING=the path it was deployed with" in message

    def test_a_redeploy_of_our_own_app_is_a_normal_update(self, entry, run) -> None:
        """Story 3: the operator bumped ``revision`` and deploys again."""

        run.state.apps = [_app_row(_APP_NAME)]

        assert guard_deploy(entry, "app", False, path="vllm") is None

    def test_a_failed_list_under_force_is_only_a_warning(
        self, entry, run, caplog
    ) -> None:
        run.state.list_returncode = 1

        with caplog.at_level(logging.WARNING):
            assert guard_deploy(entry, "app", True, path="vllm") is None

        assert [record.getMessage() for record in caplog.records] == [
            "FORCE=yes: could not list Modal endpoints (exit 1) — deploying anyway."
        ]


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
