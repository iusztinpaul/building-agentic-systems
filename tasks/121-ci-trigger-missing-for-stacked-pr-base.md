---
id: 121-ci-trigger-missing-for-stacked-pr-base
feature: ci-infra
status: in-progress
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

- [ ] `pull_request` trigger in `ci.yml` no longer restricted to `branches: [main]`.
- [ ] Push a trivial commit to `feat/embedding-clusters-viz` (or re-push HEAD) and confirm
      `gh run list --branch feat/embedding-clusters-viz` shows a `CI` run tied to PR #42.
- [ ] `gh pr checks 42` reports the `memory (python)` check instead of "no checks reported".
- [ ] `cd.yml` behavior unchanged: still only deploys after a `push`-event CI run on `main`.

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
