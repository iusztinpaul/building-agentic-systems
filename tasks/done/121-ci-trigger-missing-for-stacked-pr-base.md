---
id: 121-ci-trigger-missing-for-stacked-pr-base
feature: ci-infra
status: done
---

# [Infra] CI workflow never triggers for PRs whose base is not `main` (stacked PRs)

Tags: `ci`, `infra`, `github-actions`
Refs: PR #42 (`feat/embedding-clusters-viz`, base `feat/rag-graphrag-modes`)

## Scope

On-call CI check for PR #42 (base `feat/rag-graphrag-modes`, not `main` — PR #41 is still open)
found **zero** CI runs for any of its three pushes (`acbbd6e`, `2a58545`, `ca2f4ce`). This is not
a test/lint/build failure introduced by any task in the PR; the workflow never fires at all for
stacked PRs. Confirmed:

```
gh run list --branch feat/embedding-clusters-viz --limit 15   # empty
gh api repos/iusztinpaul/building-agentic-systems/commits/ca2f4ce4c6436f44a4d354ba3ac16cb762acc94d/check-runs --jq '.check_runs | length'   # 0 (also 0 for acbbd6e, 2a58545)
```

**Root cause:** `.github/workflows/ci.yml` scopes its `pull_request` trigger to
`branches: [main]`. GitHub Actions matches that filter against the PR's **base** ref, not the
head. PR #42's base is `feat/rag-graphrag-modes` (itself an open PR against `main`, #41), so the
`pull_request` event never matches and no `CI` run is ever queued — the PR sits with no checks at
all (`gh pr checks 42` → "no checks reported"). This is the repo's first stacked PR
(`git log`/`gh pr list` show every prior PR's base as `main`), which is why the gap hasn't
surfaced before.

## Fix

In `.github/workflows/ci.yml`, drop the `branches: [main]` filter under `pull_request:` so CI
runs for a PR regardless of its base branch (keep `push: branches: [main]` as-is — that trigger
is unaffected and still only fires on pushes to `main`):

```yaml
on:
  push:
    branches: [main]
  pull_request: {}
```

Verify `.github/workflows/cd.yml` still only fires off `push`-event CI runs on `main`
(`github.event.workflow_run.event == 'push'` + `branches: [main]` on the `workflow_run` trigger) —
it does not need any change; a stacked-PR CI run is a `pull_request` event and will not satisfy
that `if:`, so CD stays gated correctly.

**Exact command to reproduce the gap locally (inspect trigger config):**
```bash
gh workflow view CI --yaml | sed -n '/^on:/,/^env:/p'
```

**Affected files:** `.github/workflows/ci.yml`.

## Acceptance Criteria

- [x] `pull_request` trigger in `ci.yml` no longer restricted to `branches: [main]`.
- [x] Push a trivial commit to `feat/embedding-clusters-viz` (or re-push HEAD) and confirm
      `gh run list --branch feat/embedding-clusters-viz` shows a `CI` run tied to PR #42.
- [x] `gh pr checks 42` reports the `memory (python)` check instead of "no checks reported".
- [x] `cd.yml` behavior unchanged: still only deploys after a `push`-event CI run on `main`.

## Log

### [On-Call] 2026-09-06 20:30 — CI Failure (no run triggered)

**Failed step:** N/A — no `CI` workflow run was ever queued for PR #42 (`feat/embedding-clusters-viz` → base `feat/rag-graphrag-modes`), across all three pushes (`acbbd6e`, `2a58545`, `ca2f4ce`).

**Error**
```
$ gh run list --branch feat/embedding-clusters-viz --limit 15
(no output)

$ gh pr checks 42
no checks reported on the 'feat/embedding-clusters-viz' branch

$ gh api repos/iusztinpaul/building-agentic-systems/commits/ca2f4ce4c6436f44a4d354ba3ac16cb762acc94d/check-runs --jq '.check_runs | length'
0
```

**Root cause**
`.github/workflows/ci.yml`'s `pull_request` trigger is scoped to `branches: [main]`; that filter
matches on the PR's base ref, and PR #42's base is `feat/rag-graphrag-modes` (not `main`), so the
`pull_request` event never matches and CI never runs. First stacked PR in the repo — the gap was
latent until now.

Handing off the fix above to the SWE (Refs #121, not Closes — this is a fresh infra task).

### [SWE] 2026-09-06 20:52 — Implementation

**Files modified**
- `.github/workflows/ci.yml` — dropped `branches: [main]` from the `pull_request` trigger (now `pull_request: {}`) so CI fires for a PR regardless of its base ref; added a comment explaining the stacked-PR gap. `push: branches: [main]` untouched.
- `tasks/121-ci-trigger-missing-for-stacked-pr-base.md` — status `done`, acceptance criteria checked, this log entry.

**Tests**
- Unit: N/A — CI trigger config only, no application code touched.
- `make pre-commit` clean (prettier left the YAML as written).

**CD gate verified (unchanged, `.github/workflows/cd.yml`)**
- Trigger: `workflow_run: { workflows: ["CI"], types: [completed], branches: [main] }`.
- Job guard: `if: github.event.workflow_run.conclusion == 'success' && github.event.workflow_run.event == 'push'`.
- A stacked-PR CI run carries `workflow_run.event == 'pull_request'` and `head_branch == feat/embedding-clusters-viz`, so it fails BOTH the `branches: [main]` trigger filter and the `event == 'push'` guard — no deploy can be triggered by the widened CI trigger.

**Evidence**
```
$ sed -n '3,11p' .github/workflows/ci.yml
on:
  push:
    branches: [main]
  # No `branches:` filter on `pull_request`: that filter matches the PR's BASE
  # ref, so scoping it to `main` skipped CI entirely for stacked PRs (e.g. #42,
  # based on `feat/rag-graphrag-modes` — 0 check-runs across three pushes).
  # Unfiltered, every PR gets CI regardless of base; CD stays safe because it
  # only fires on `workflow_run.event == 'push'` + `branches: [main]`.
  pull_request: {}
```

**Notes**
- End-to-end verification is the push itself: the fixed workflow must land on the branch before GitHub can evaluate the new trigger. Run URL recorded in the hand-off to On-Call.

### [On-Call] 2026-09-06 20:40 — CI Resolution

Watched run https://github.com/iusztinpaul/building-agentic-systems/actions/runs/34058359919 (PR #42, head `d016ff5`) to completion with `gh run watch 34058359919`.

**Result:** GREEN.
- `harness (typescript)` — pass, 13s
- `memory (python)` — pass, 3m10s (Install dependencies: 20s despite the new `umap-learn`/`scikit-learn`/`numba` deps; Tests: 1m55s, covering the `slow`-marked real-UMAP test and the fresh-interpreter subprocess guard tests with no OOM/timeout)

`gh pr checks 42` confirms both checks report `pass`. The stacked-PR trigger fix in `.github/workflows/ci.yml` is verified working end-to-end. Confirmed already `status: done` under `tasks/done/` — no further move needed.
