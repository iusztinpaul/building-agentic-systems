"""Unit tests for the CD entrypoint ``deploy/prefect_pipelines.py`` and its wiring.

``.github/workflows/cd.yml`` is the only caller, and what makes CD safe lives in
that file, not in the script: the deploy pins the managed runs to the commit
that passed CI, and only the tip of ``main`` deploys. A workflow edit that drops
either one still deploys green, so these tests read the workflow itself.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

_CD_WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[5] / ".github" / "workflows" / "cd.yml"
)
_TESTED_SHA = "${{ github.event.workflow_run.head_sha }}"


@pytest.fixture
def deploy_step() -> dict:
    workflow = yaml.safe_load(_CD_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy-prefect"]["steps"]
    return next(
        step for step in steps if "deploy/prefect_pipelines.py" in step.get("run", "")
    )


class TestCdWorkflowWiring:
    def test_the_deploy_pins_runs_to_the_commit_that_passed_ci(
        self, deploy_step: dict
    ) -> None:
        # Without GIT_REF the script defaults to ``main``: every managed run then
        # clones whatever main is at run time, red CI included.
        assert deploy_step["env"]["GIT_REF"] == _TESTED_SHA

    def test_only_the_tip_of_main_deploys(self, deploy_step: dict) -> None:
        # A pinned deploy of an older commit (a re-run of its CI) is a rollback.
        run = deploy_step["run"]

        assert "git ls-remote origin refs/heads/main" in run
        assert run.index('[ "$tip" != "$GIT_REF" ]') < run.index(
            "deploy/prefect_pipelines.py"
        )

    def test_it_checks_out_the_commit_that_passed_ci(self) -> None:
        workflow = yaml.safe_load(_CD_WORKFLOW.read_text(encoding="utf-8"))
        checkout = workflow["jobs"]["deploy-prefect"]["steps"][0]

        assert checkout["uses"].startswith("actions/checkout@")
        assert checkout["with"]["ref"] == _TESTED_SHA
