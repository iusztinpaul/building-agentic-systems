# [PM rejection] Flatten sources config — stale "10 deployments" count contradicts the new orchestrator

Status: pending
Tags: `rollup`, `pm-rejection`, `docs`
Refs: `tracker/done/010-orchestrator-make-targets-cleanup.md`,
`tracker/done/012-pm-rejection-flatten-sources-config-doc-fixes.md`,
`tracker/feature-flatten-sources-config-plan.md`

## Scope

Cycle 2/3 of PM acceptance review on the `flatten-sources-config` feature.

The previously-named blockers from cycle 1 are all fixed and verified:

- `apps/memory/README.md:47-55` — `default.yaml sections` now describes
  the flat `sources` list with typed entries, the `type` field, valid
  type values, untyped inference rules, and per-entry HF-arxiv tuning.
  No stale typed-keys schema. ACK.
- `docs/agentic-graphrag-mcp-tools.md` lines 98, 124, 983 — all three now
  reference the unified `data-pipeline-etl` deployment instead of the
  removed per-source pipelines. ACK.
- `apps/memory/tests/integration/data/web/test_web_pipeline.py:42` —
  comment now reads "as a `type: substack_article` entry under the
  top-level `sources` list," matching the flat schema. ACK.

But the user-facing surfaces that enumerate Prefect deployments were
only **partially** updated. `orchestrator.py` registers exactly 5
deployments (verified by reading the file):

```
data-pipeline-etl
memory-extraction-etl
memory-indexing-etl
ingest-file-etl
ingest-conversation-etl
```

Two prose locations still claim **10**, including one that contradicts
itself two lines apart in the same paragraph. A user reading either doc
cannot tell whether the system has 5 or 10 batch deployments. The fix
is mechanical (s/10/5/) and must accompany the merge — leaving these in
ships a docs-vs-code contradiction in the public-facing architecture
report and the app README.

## Acceptance Criteria

- [x] `apps/memory/README.md:92` no longer says "serves all 10
      deployments". The number must match the enumeration immediately
      below it (currently 5 deployments, in `orchestrator.py`).
- [x] `docs/agentic-graphrag-architecture-report.md:985` Tech Stack
      Summary table no longer says "Prefect (10 deployments for batch
      pipelines)". The number must match `orchestrator.py`.
- [x] `grep -nE "10 deployments" apps/memory/README.md docs/ README.md`
      returns no results.
- [x] No new staleness introduced — the SWE re-greps the touched docs
      for any other count or enumeration of pipeline deployments and
      confirms consistency with `apps/memory/src/tree/orchestrator.py`.
- [ ] Tester re-runs full QA suite and PASSES.
- [ ] PM re-runs acceptance review (cycle 3/3) and ACCEPTS.

## Issues (detail)

### 1. Self-contradicting deployment count — `apps/memory/README.md:92`
- **What the user experiences (wrong):** Line 92 reads "...runs
  `python -m tree.orchestrator`, which serves all **10 deployments**."
  Line 100 (eight lines below, same section) reads "The **5
  deployments** registered by `src/tree/orchestrator.py`:" followed by
  the actual five names. The numbers are visibly inconsistent in a
  single screenful of text.
- **What the spec / good UX implies (right):** Both numbers say `5`
  (matching `orchestrator.py`), or the offending sentence is rephrased
  to avoid quoting a count at all (e.g. "...which serves the
  deployments listed below" — even more robust against future drift).
- **Suggested fix:** Edit line 92 to drop "all 10" — either hard-code
  "5" to match line 100, or remove the count entirely and let line 100
  carry it. SWE picks.

### 2. Stale deployment count in architecture report — `docs/agentic-graphrag-architecture-report.md:985`
- **What the user experiences (wrong):** The "Tech Stack Summary" table
  row reads `| **Orchestration** | Prefect (10 deployments for batch
  pipelines) |`. A reader scanning this summary table for at-a-glance
  facts about the system gets the wrong number.
- **What the spec / good UX implies (right):** "Prefect (5 deployments
  for batch pipelines)" or just "Prefect (batch pipelines)" — same
  defensive-against-drift rationale as #1.
- **Suggested fix:** Update the cell to `5` or drop the parenthetical
  count. SWE picks.

## User Stories

(Inherit from the original task — no new stories. The existing stories
about ingesting flat-sources entries via `make memory-run-data-pipeline`
and reading the README to author new entries still apply, and they all
implicitly depend on the deployment count being internally consistent.)

## Out-of-scope finding (NOT part of this rollup)

The Tester noted that
`AppConfig.model_validate({"sources": {"substack": [...]}})` (the OLD
typed-keys YAML shape) silently yields `sources=[]` instead of raising
`ValidationError` — `_normalize_untyped_sources` only acts on
list-shaped `sources`. PM agrees with the Tester's recommendation:
this is a separate post-merge follow-up, NOT a blocker on this
feature, because:

- The migrated `default.yaml` is already a list, so live config loads
  correctly.
- No live config file uses the dict shape anywhere in the repo.
- The behavior is "lenient on input we no longer document", not "live
  bug a user will hit". Tightening it is a strict-validation
  enhancement, not a regression fix.
- Adding a strict-mode validator is its own design decision (do we
  reject the dict shape outright, or emit a deprecation warning? do we
  also reject any unknown top-level keys under `sources`?), worth its
  own task spec rather than smuggled into a docs rollup.

PM will file this as `tracker/014-strict-validation-for-legacy-sources-shape.todo.md`
**after** this feature merges, so it doesn't gate the current PR.

---

Refs: `tracker/done/010-orchestrator-make-targets-cleanup.md`,
`tracker/done/012-pm-rejection-flatten-sources-config-doc-fixes.md`

## Log

### [PM] 2026-05-01 12:00 — Acceptance Review (cycle 2/3)

**VERDICT: REJECT**

Re-walked the three previously-named blockers by reading the actual
files — all three are fixed (evidence above in Scope).

Found 1 new class of defect (deployment-count staleness) that the
rollup #012 missed because cycle 1 only named the typed-keys-schema
sites, not the stale "10 deployments" count that `chore` task #010
left behind. Two user-facing docs are now internally inconsistent or
contradict the orchestrator code:

- `apps/memory/README.md:92` says "10 deployments" eight lines above
  saying "5 deployments" in the same section.
- `docs/agentic-graphrag-architecture-report.md:985` Tech Stack
  Summary table row says "Prefect (10 deployments for batch
  pipelines)".

Filed this rollup. Pipeline re-runs from inner loop with this rollup;
on green QA, re-run acceptance on the original feature (cycle 3/3 max,
per `docs/PROCESS.md` Retry Caps).

The Tester's strict-validation finding is acknowledged and will be
filed as a **post-merge** follow-up task (#014) — explicitly NOT
included in this rollup, to keep this rollup tightly scoped to
"docs match code".

### [SWE] 2026-05-01 12:30 — Implementation

**Files modified**
- `apps/memory/README.md` — line 92: "serves all 10 deployments" → "serves all 5 deployments" so the prose matches the enumeration eight lines below (and matches `orchestrator.py`).
- `docs/agentic-graphrag-architecture-report.md` — line 985 Tech Stack Summary table cell: "Prefect (10 deployments for batch pipelines)" → "Prefect (5 deployments for batch pipelines)".

**Tests**
- Unit: 388 passing, 0 failing, 0 warnings — `make memory-unit-tests` (output below).
- Integration: N/A — docs-only change, no infra / runtime touched.

**Acceptance criteria**
- [x] `apps/memory/README.md:92` no longer says "serves all 10 deployments" — verified by re-reading line 92 ("serves all 5 deployments") and consistency with line 100 ("The 5 deployments registered by `src/tree/orchestrator.py`").
- [x] `docs/agentic-graphrag-architecture-report.md:985` no longer says "Prefect (10 deployments for batch pipelines)" — verified by re-reading line 985 ("Prefect (5 deployments for batch pipelines)").
- [x] `grep -nE "10 deployments" apps/memory/README.md docs/ README.md` returns no results — verified (also extended the grep recursively across `apps/memory/`, `docs/`, `README.md`, `CLAUDE.md` and added `"ten deployments"` — all empty).
- [x] No new staleness introduced — re-greped the touched docs for `[0-9]+ deployments?` phrases. Three matches remain and all three say `5`, consistent with the 5 deployments registered in `apps/memory/src/tree/orchestrator.py` (`data-pipeline-etl`, `memory-extraction-etl`, `memory-indexing-etl`, `ingest-file-etl`, `ingest-conversation-etl`).
- [x] Tester re-runs full QA suite and PASSES — verified by Tester (see log below).
- [ ] PM re-runs acceptance review (cycle 3/3) and ACCEPTS — pending.

**Evidence**

Stale-count grep (post-edit):
```
$ grep -nE "10 deployments|ten deployments" apps/memory/ docs/ README.md CLAUDE.md -r
(no results)
```

Surviving deployment-count phrases (all `5`, all consistent with `orchestrator.py`):
```
$ grep -nE "deployments?" apps/memory/README.md docs/agentic-graphrag-architecture-report.md \
    | grep -iE "[0-9]+ deployments?|all deployments|the deployments"
apps/memory/README.md:92:The Dockerized `prefect-worker` container (started by `make local-start`) runs `python -m tree.orchestrator`, which serves all 5 deployments. ...
apps/memory/README.md:100:The 5 deployments registered by `src/tree/orchestrator.py`:
docs/agentic-graphrag-architecture-report.md:985:| **Orchestration** | Prefect (5 deployments for batch pipelines) |
```

QA loop:
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
... 132 files left unchanged / All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... 388 passed in 20.38s ...
```

End-to-end / runtime check: N/A. Pure markdown copy edit on two prose locations — no code, no config, no behavior changed. The "exercise the feature" check is "read the rendered docs and confirm the count matches the enumeration"; both files now read consistently with the orchestrator.

**Notes**
- Did not commit — handing off to the Tester per the workflow.
- Out-of-scope strict-validation finding mentioned at the bottom of this spec is intentionally untouched; PM will file it post-merge as `tracker/014-...todo.md`.
- Tracker file renamed `*.groomed.md` → `*.in-progress.md` via plain `mv` (file is untracked, so `git mv` rejected it — same handling as the prior in-progress rollups in this branch).

### [Tester] 2026-05-01 13:00 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — prettier / ruff check / ruff format / biome all Passed).
- Unit tests: 388 passed / 0 failed (`make memory-unit-tests` — `388 passed in 19.29s`).
- Integration tests: SKIPPED — docs-only change, no infra/runtime touched (per spec; matches SWE rationale and the project's standing convention that pure markdown copy edits don't require the 15-min integration suite).
- Warnings: 0.

**E2E adversarial pass** (docs-only diff — adversarial pass focuses on doc-truth consistency rather than runtime break paths)
- Happy path: read `apps/memory/README.md` lines 90-104 — line 92 now says "serves all 5 deployments", line 100 says "The 5 deployments registered by `src/tree/orchestrator.py`", followed by an enumeration of 5 names. Internally consistent. PASS.
- Happy path #2: read `docs/agentic-graphrag-architecture-report.md:985` — "Prefect (5 deployments for batch pipelines)". PASS.
- Break path 1 (residual stale count anywhere in the corpus): `grep -rnE "10 deployments|ten deployments" apps/memory/ docs/ README.md CLAUDE.md` → empty (exit 1). PASS.
- Break path 2 (other count mismatches in adjacent docs): `grep -rn "deployments" docs/ apps/memory/README.md` — every numeric count phrase visible says 5; no other integers; references to "deployments" in `docs/agentic-graphrag-mcp-tools.md`, `docs/scaling-with-prefect.md`, and `apps/memory/README.md:222`/`:224` are unrelated context (not counts). PASS.
- Break path 3 (truth-source vs docs): counted `to_deployment(...)` calls in `apps/memory/src/tree/orchestrator.py` — exactly 5: `data-pipeline-etl`, `memory-extraction-etl`, `memory-indexing-etl`, `ingest-file-etl`, `ingest-conversation-etl`. Both updated docs match the truth source. PASS.
- Break path 4 (scope creep / unrelated drift in this rollup): `git diff --stat` shows exactly 2 files modified (`apps/memory/README.md`, `docs/agentic-graphrag-architecture-report.md`), 2 insertions / 2 deletions. No source code, no test code, no config touched. PASS.

**Acceptance criteria**
- [x] PASS — `apps/memory/README.md:92` no longer says "serves all 10 deployments" — Evidence: re-read shows "serves all 5 deployments"; `git diff` confirms s/10/5/.
- [x] PASS — `docs/agentic-graphrag-architecture-report.md:985` no longer says "Prefect (10 deployments for batch pipelines)" — Evidence: re-read shows "Prefect (5 deployments for batch pipelines)"; `git diff` confirms s/10/5/.
- [x] PASS — `grep -nE "10 deployments" apps/memory/README.md docs/ README.md` returns no results — Evidence: independent recursive grep over `apps/memory/`, `docs/`, `README.md`, `CLAUDE.md` for both `"10 deployments"` and `"ten deployments"` returns empty (exit 1).
- [x] PASS — No new staleness introduced — Evidence: visual scan of `grep -rn "deployments" docs/ apps/memory/README.md`; the only numeric count phrases that remain are the three intended `5` references, consistent with the 5 `to_deployment(...)` calls in `apps/memory/src/tree/orchestrator.py`.
- [x] PASS — Tester re-runs full QA suite and PASSES — Evidence: this entry.

**Evidence**
```
$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... 388 passed in 19.29s

$ grep -rnE "10 deployments|ten deployments" apps/memory/ docs/ README.md CLAUDE.md
(no output, exit 1)

$ git diff --stat
 apps/memory/README.md                        | 2 +-
 docs/agentic-graphrag-architecture-report.md | 2 +-
 2 files changed, 2 insertions(+), 2 deletions(-)
```

**Other issues found**
- None within scope. The out-of-scope strict-validation finding (legacy dict-shape `sources` silently coerces to `[]`) is already acknowledged at the bottom of the spec and slated for post-merge task #014; not flagged again here.

**VERDICT: PASS**
