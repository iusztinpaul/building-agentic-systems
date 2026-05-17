# Development Process

This document defines the agent-team workflow for this project. It is the **single source of truth** for the pipeline. Every agent (PM, SWE, Tester, PR Reviewer, On-Call) reads this file before acting, and the `/night` and `/day` skills drive the loops described here.

## Modes

The agent team ships with two entry points:

| Mode | Entry | Scope | Gates active | Commits? | When to use |
|---|---|---|---|---|---|
| Night | `/night [feature-spec]` | One feature, end-to-end | PM groom + human-approve plan, Tester, PM accept, Push, On-Call (CI), PR Reviewer (diff), Squash, optional Self-Improve | Yes — agent commits + pushes; **human merges PR** | Unattended end-to-end delivery of a single feature |
| Day | `/day [task]` | One task, supervised | Tester only | No — human commits | On-demand single-task work, fast iteration, human in the loop |

**Night mode** runs the whole feature-level pipeline described below. It blocks on the human exactly twice by design: once after the PM produces the Tasks Plan (approve the plan), and once at the end (merge the squashed PR). Every other gate is automated; failures route back into the inner loop as new rollup tasks rather than stopping the pipeline.

**Day mode** is the lean inner loop: SWE implements, Tester verifies, human commits. No PM grooming, no PM acceptance, no PR Reviewer, no On-Call. Use it for the kinds of changes you'd otherwise type by hand — the Tester gate is the one automated check that's hard for a human to replicate (full suite + every AC + e2e adversarial pass).

## Lifecycle (night mode)

```
Feature raw spec
    │
    ▼
Orchestrator creates feature branch + worktree
    │
    ▼
PM grooms the feature → Tasks Plan (ordered list of groomed tasks)
    │
    ▼
HUMAN APPROVES the Tasks Plan         ← blocking gate #1
    │
    ▼
Inner implementation loop, per task in order:
    SWE implements → Tester verifies → (FAIL → SWE; loop, Max 5)
    │
    ▼
All tasks done in this round
    │
    ▼
PM acceptance review (whole feature, user POV)
    │
    ├── REJECT → PM writes ONE rollup task with all issues → back to inner loop (Max 3)
    │
    └── ACCEPT
        │
        ▼
    Push to git (open or update Feature PR via `create-pr`)
        │
        ├──► On-Call (CI/CD only)
        │       └── Fail → SWE fix → loop (Max 5)
        │
        └──► PR Reviewer (git diff only)
                └── Blockers → write ONE rollup task → back to inner loop (Max 3)
    │
    ▼
Both gates green
    │
    ▼
Orchestrator squashes feature-branch commits into one commit on the feature branch
    │
    ▼
Ask human: "Run self-improve to capture corrections into CLAUDE.md?"
    │
    ├── Yes → Self-Improve runs → produces a CLAUDE.md update proposal → human accepts/rejects
    │
    └── No → skip
    │
    ▼
Hand the squashed Feature PR to the human
    │
    ▼
HUMAN MERGES the Feature PR          ← blocking gate #2
```

Day mode collapses to: `SWE implements → Tester verifies → human reviews and commits`.

## Agents

Five sub-agents plus the orchestrator (the top-level Claude Code session).

| Agent | File | Role |
|---|---|---|
| Orchestrator | `CLAUDE.md` + `skills/night/SKILL.md` / `skills/day/SKILL.md` | Drives the pipeline; never writes code itself. |
| Product Manager | `agents/product-manager.md` | Grooms a feature into a Tasks Plan; user-perspective acceptance review at the end of the inner loop. |
| Software Engineer | `agents/software-engineer.md` | Implements code + tests; commits only after Tester PASS + PM ACCEPT. |
| Tester | `agents/tester.md` | Runs the full suite; verifies every AC with evidence; **headline duty: e2e adversarial QA — break the feature from multiple user perspectives**. |
| PR Reviewer | `agents/pr-reviewer.md` | Reads the git diff after push. Narrow performance review (hot path / asymptotic / material framework underuse only), clean code, untested code, standards. Produces a rollup task with Blockers + Nits. Never reads CI, never merges. |
| On-Call Engineer | `agents/oncall-engineer.md` | Watches CI/CD after push. On red, traces to a task, fixes, pushes (`Refs #N`), confirms green. CI/CD-only — does not read the diff. |

## Retry Caps

Hard caps. When a cap is hit, the orchestrator stops the pipeline and surfaces a `USER ACTION REQUIRED` summary.

| Loop | Cap | Counter resets when |
|---|---|---|
| Tester FAIL → SWE fix → re-test | **5** | The task being implemented changes (next task in Plan, or a new rollup task) |
| PM REJECT → SWE fix (rollup task) | **3** | Per feature |
| PR Reviewer Blockers → SWE fix (rollup task) | **3** | Per feature |
| On-Call CI Fail → SWE fix → re-push | **5** | Per push attempt |

## Documentation discipline

*This section applies only when `docs/adr/` and/or `docs/glossary.md` exist in the project. On projects that opted out during scaffold, ignore — agents skip the corresponding checks.*

A standing invariant across both `/day` and `/night`: when the project has chosen to maintain ADRs or a glossary, those files are part of every PR's contract.

| Role | Responsibility |
|---|---|
| **PM** | Authors `docs/adr/NNNN-...md` and updates `docs/glossary.md` during grooming — before implementation tasks ship. The Tasks Plan's "Documentation updates" section enumerates what was added. |
| **SWE** | Reads both. Uses canonical glossary terms verbatim. Respects existing Accepted ADRs as binding. **Stops and escalates** if implementation reveals an undocumented architectural fork or requires a domain term the glossary doesn't have — never silently authors. |
| **Tester** | When an AC names `docs/glossary.md` or a `docs/adr/NNNN` file as expected output, verifies presence and topical match in the diff. No content judgement. |
| **PR Reviewer** | Discipline backstop (review dimension E). Blocks on missing glossary update for new domain concepts, missing ADR for new architectural decisions, ADR contradictions without supersession. Doc-discipline Blockers are prefixed `[PM]` in the rollup so the orchestrator routes the rollup through PM grooming first. |
| **Orchestrator** | Routes `[PM]`-prefixed Blocker rollups to PM (re-engagement entry-point) before handing back to SWE. Standard SWE-fix rollups continue to route normally. |

Rollups for documentation discipline route to PM, not SWE — the cure for missing or wrong docs is grooming, not implementation.

## Severity Rule (PR Reviewer)

The PR Reviewer tags every finding as Blocker or Nit. Severity decides what the orchestrator does with it.

| Severity | Definition | Outcome |
|---|---|---|
| **Blocker** | Real defect: bug, security issue, dead/duplicate code being shipped, untested non-trivial logic, hot-path performance regression, material framework underuse, standards violation that would fail review at any team. | Goes into the rollup task body; the rollup task loops the SWE. Pipeline does not advance to Squash until zero Blockers. |
| **Nit** | Subjective preference, micro-optimization on a non-hot path, naming taste, doc polish, suggestion-not-requirement. | Goes into the same rollup task under a Nits section AND appended to the PR description (so the human merger sees them) — but **does NOT block the pipeline**. |

If the rollup contains zero Blockers (only Nits), the PR Reviewer reports `NO BLOCKERS` and the pipeline advances. The PR Reviewer never blocks on style preferences.

The performance review is **narrow**: hot path, concrete asymptotic problem, or material framework underuse. It does NOT over-engineer in advance, micro-optimize, or worry about one-off scripts.

## Branch Lifecycle

| Step | Who | What |
|---|---|---|
| Create feature branch + worktree | Orchestrator (`/night` Step 1) | New branch off `main`, isolated worktree |
| Commit per task | SWE (after Tester PASS + PM ACCEPT) | One commit per task, references the task |
| Squash before merge | Orchestrator (after On-Call green + PR Reviewer no-blockers) | Single squashed commit on the feature branch; PR description preserved |
| Merge to `main` | **Human** | The merge button is the human's, always |

**The orchestrator never merges.** It pushes, squashes, and asks the human to merge.

## CLI-Only Tooling Rule

All agents that touch git, datastores, cloud services, or CI **must** use the relevant CLI (`git`, `gh`, `psql`, `aws`, `docker`, etc.). No web UIs, no ad-hoc REST wrappers when a CLI exists.

Reasons:
1. CLIs are scriptable and reproducible — agents leave a trail the orchestrator can verify.
2. CLIs version with the project (locked deps); web UIs drift.
3. The orchestrator can spot-check by re-running the same command.

Applies to: SWE, On-Call, Tester (running suites), PR Reviewer (reading diff/tracker), PM (reading tracker).

## Responsibility Model

Every role owns their deliverable. Quality is distributed across the team.

| Role | Owns | Accountable For |
|---|---|---|
| PM | The whole feature — UX, scope, quality | The user's last line of defense. If PM ACCEPTS and the user finds it broken, that's a PM failure. |
| SWE | Code correctness, tests, regression coverage | If the Tester finds bugs, that's an SWE failure. |
| Tester | Verification, evidence, e2e QA-style break-it testing | If Tester says "PASS" without running the test or without trying realistic break paths, that's a Tester failure. |
| PR Reviewer | Diff quality | If a Blocker-grade defect lands and the PR Reviewer didn't flag it, that's a PR Reviewer failure. |
| On-Call | Pipeline health after push | If a red pipeline sits red, that's an On-Call failure. |
| Orchestrator | The team | Verifying each agent's report before forwarding it. If an agent cuts corners and the orchestrator accepts it, that's an orchestrator failure. |

**The PM owns the user experience.** When accepting a deliverable, the PM verifies the feature works from the user's perspective — actual output, UI, logs — not just that code exists and tests pass.

**The orchestrator is a MANAGER, not an implementer.** The orchestrator NEVER writes or modifies code. It launches agents, enforces gates, verifies reports.

## False Confidence Is the Worst Outcome

The single worst thing the orchestrator can do is tell the user "it works" when it doesn't.

1. **Never say "it works" without firsthand evidence.** An agent saying "PASS" is not firsthand evidence. Test output, logs, and produced artifacts are.
2. **If you're not sure, say so.** "Tester reports PASS but I haven't independently verified" beats false certainty.
3. **If something contradicts user experience, the user is right.** Investigate why the test passed when the feature is broken.
4. **Treat every "it works" claim as a promise.** Before making it, ask: "If the user checks right now, will it work?"

The orchestrator must verify each agent's report:

1. Re-read the AC line by line.
2. Check each criterion against the agent's report.
3. **REJECT and re-launch if any criterion was skipped** — with specific instructions about what was missed.

Common failures:
- AC says "run e2e" → agent wrote the test but never ran it. **REJECT.**
- AC says "verify CLI output" → agent only ran unit tests. **REJECT.**
- AC says "no regressions in module X" → agent only ran new tests. **REJECT.**

"Tests pass" is NEVER sufficient if the AC requires runtime or visual verification.

## Pipeline Always Moves Forward

Between its two human gates, `/night` runs autonomously:

- **NEVER wait for additional user input** between gates. Write blockers as `USER ACTION REQUIRED` in the task log and continue.
- **Never silently skip.** If a task can't be completed, file it as a follow-up and surface it in the final summary.
- **One agent per task.** When fanning out, launch N separate agents — never combine multiple tasks into one agent call.

## Orchestrator Commit Rules

- The SWE commits per task (specific files only, never `git add -A` / `git add .`); each commit references the task ID.
- The SWE pushes to the feature branch (or invokes `create-pr`) after PM ACCEPT.
- After On-Call green AND PR Reviewer no-blockers, the **orchestrator** squashes the per-task commits into one squashed commit on the feature branch (e.g. `git reset --soft <merge-base> && git commit`), preserving the PR description.
- The orchestrator never merges to `main`.

## Definition of Done

### SWE Done

- [ ] Code follows existing patterns (`CLAUDE.md`).
- [ ] Tests written and passing — actual output appended to the task log.
- [ ] Format + lint clean (`make format-fix && make lint-fix && make format-check && make lint-check && make pre-commit`) — output appended.
- [ ] For runtime behavior: actually exercised (CLI invoked, service started, endpoint called) — output appended.
- [ ] For bug fixes: regression test added FIRST, confirmed red, then fixed, then confirmed green.
- [ ] TDD red/green when the contract is **decidable** (new logic; regression test for a known bug). NOT required for refactors, glue code, migrations, or one-off scripts.
- [ ] Any verification that couldn't run: `NOT RUN — reason` (never silently skip).
- [ ] All git/datastore/cloud/CI access via CLI.
- [ ] Commit via the `commit-commands` plugin (required, not optional, when present).

"It compiles" is NOT done. "I ran it, here's the output" IS done.

### Tester Done

- [ ] Every acceptance criterion walked through and marked PASS / FAIL with evidence.
- [ ] Full suite run (`make pre-commit && make unit-tests && make integration-tests-all`) — output appended. **The `-all` target is required at the acceptance gate**; the bare `make integration-tests` target is the fast inner-loop subset and excludes `@pytest.mark.slow` tests.
- [ ] **E2E QA-style break-it pass**: ran the feature the way users will (CLI / HTTP / UI), tried at least 2–3 realistic break paths (empty input, malformed input, large input, concurrent invocation, etc.), recorded what happened. **This is the headline duty, not a formality.**
- [ ] Any SWE `NOT RUN` items: either run them now, or explain why not.
- [ ] Suspicious results investigated (a 3-second pass on a multi-step flow is a red flag).
- [ ] Verdict: PASS or FAIL, with reasons.

"I read the diff and it looks right" is NOT done. "I ran every test, walked every AC, tried to break it, here's the evidence" IS done.

### PM Done (acceptance)

- [ ] Reviewed the Tester's evidence.
- [ ] Verified each user story has a corresponding test that passed.
- [ ] Walked the feature from the user's perspective.
- [ ] Silently-dropped scope: filed as a new task and linked.
- [ ] On REJECT: produced ONE rollup task containing **all** issues (file, expected, actual, suggested fix) — not separate tickets per issue.
- [ ] Verdict: "If the user checks this right now, they will be satisfied" — yes or no.

"QA said PASS" is NOT done. "I reviewed the evidence, I guarantee user satisfaction" IS done.

### PR Reviewer Done

- [ ] Read the entire diff (every file in `git diff $(git merge-base HEAD origin/main)...HEAD`).
- [ ] Tagged every finding as Blocker or Nit (per Severity Rule).
- [ ] Performance review stayed within the narrow scope (hot path / asymptotic / material framework underuse).
- [ ] Produced ONE rollup task with all findings, OR reported `NO BLOCKERS`.
- [ ] Did NOT comment on the PR, did NOT merge, did NOT touch CI.

### On-Call Done

- [ ] Pulled the failing CI run and identified the responsible task from the commit message.
- [ ] Reproduced the failure locally (same command CI ran).
- [ ] Fixed the root cause, not the symptom.
- [ ] Pushed the fix with `Refs #N` (never `Closes #N`).
- [ ] Confirmed CI is green after the fix.
- [ ] All access (git, gh, etc.) via CLI.

### Orchestrator Done

- [ ] PM gave a concrete verdict with evidence references (not just "ACCEPTED").
- [ ] PM's evidence matches the AC (orchestrator spot-checks).
- [ ] Squash commit only after On-Call green AND PR Reviewer no-blockers.
- [ ] Asked the human about Self-Improve at the end (not before).
- [ ] Never merged to `main`.

## No Silent Descoping

The PM must NEVER silently drop acceptance criteria. If something is too large or out of scope:

1. Call out what is being descoped and why (in the Tasks Plan or in the acceptance review).
2. File a new task for each descoped item.
3. The descoped items must reference the original feature so the trail is preserved.

## Issue Log (single source of truth)

Every agent appends timestamped entries to the task's `## Log` section as they work.

```markdown
## Log

### [PM] 2026-04-27 12:30 — Grooming
...

### [SWE] 2026-04-27 14:00 — Implementation
...

### [Tester] 2026-04-27 14:45 — QA
...

### [PM] 2026-04-27 15:40 — Acceptance
...

### [PR Reviewer] 2026-04-27 16:00 — Review (rollup)
...

### [On-Call] 2026-04-27 16:30 — CI
...
```

Format: `### [ROLE] YYYY-MM-DD HH:MM — Short subject`. Append-only.

## How `/night` Picks the Feature

`/night` takes the feature spec from `$ARGUMENTS` (a free-form description, a path to a spec file, or a tracker reference). It does NOT pick from a backlog — `/night` is invoked per feature.

The PM, during grooming, produces the **Tasks Plan**: an ordered list of groomed tasks. The orchestrator processes those tasks in order during the inner loop. New rollup tasks (from PM REJECT or PR Reviewer Blockers) are inserted at the end of the queue.

## Self-Improve

Run **only** when the human says yes at the end of the run. Produces a proposed update to `CLAUDE.md` (or another project doc) capturing corrections from the run; the human reviews and accepts. See `skills/self-improve/SKILL.md`.

## Tracker Modes

Pick one per project; document the choice at the top of this file.

### Default: file-based tracker (`tracker/`)

Filename encodes the state:

```
tracker/
├── 001-add-feature.todo.md         # raw, awaiting grooming (uncommon in /night — PM creates groomed tasks directly)
├── 002-pagination.groomed.md       # PM-groomed, in Tasks Plan
├── 003-search.in-progress.md       # SWE/Tester actively working
└── done/
    └── 000-bootstrap.md            # accepted, committed
```

State transitions are file renames:
- New task → `NNN-slug.todo.md`
- After PM grooming → `NNN-slug.groomed.md`
- When SWE picks up → `NNN-slug.in-progress.md`
- After PM accepts and commit lands → `git mv` to `done/NNN-slug.md`

### Opt-in: GitHub Issues

Use `gh issue ...` for everything. Best when the project has visible coordination needs.

To switch, set `TRACKER_MODE: gh` at the top of this file.

**Active tracker for this project:** `TRACKER_MODE: file` *(file-based tracker under tracker/; PRs still go through GitHub at squash time)*

## Tech Stack Hooks

Stack-agnostic conventions. Each project's `CLAUDE.md` fills in the specifics.

- **Format / lint**: `make format-fix && make lint-fix && make format-check && make lint-check`
- **Pre-commit**: `make pre-commit`
- **Unit tests**: `make unit-tests`
- **Integration tests (fast inner loop, <2 min)**: `make integration-tests` — excludes `@pytest.mark.slow`. Use this between SWE iterations and inside the Night inner loop.
- **Integration tests (full, ~5 min)**: `make integration-tests-all` — runs every test including slow ones. Required at the Tester acceptance gate, in CI, and before any push.
- **Integration tests (slow only)**: `make integration-tests-slow` — handy when iterating on a vector-index or full-Prefect-e2e change.
- **All tests**: `make tests`
- **Build**: `make build`
- **Run any project-level Python**: `uv run python ...`

Mark a test `@pytest.mark.slow` when it takes >3s or requires full Prefect e2e. CI runs slow tests (so long as they're not also `requires_mongot`); the local fast loop excludes them. `grep -rn "pytest.mark.slow" apps/memory/tests/` shows what's currently excluded.

Mark a test `@pytest.mark.requires_mongot` when it issues a live Atlas `$vectorSearch`, calls `create_search_index`, or otherwise depends on the mongot Search Index Management service. **CI excludes these** (mongot's gRPC channel is unreliable on GitHub runners — see CI run 25989844295). The Tester MUST run them locally with the full `docker-compose.yml` stack (which brings mongot up) before the Phase-1-style acceptance gate. The acceptance command is `make memory-integration-tests-all`; the CI-mirror command (no mongot) is `make memory-integration-tests-ci`. The two markers are orthogonal — a test can be `slow` without needing mongot, or vice versa.

## Reuse Existing Skills

- **Test writing conventions** — `skills/testing-python/SKILL.md`.
- **PR creation** — `skills/create-pr/SKILL.md`.
- **Commit messages** — `commit-commands` plugin (required for SWE commits when present).
- **Code review** — Tester may invoke the `code-review` plugin as an extra signal.
- **Self-improvement** — `skills/self-improve/SKILL.md`, run only on human request at end of `/night`.

## When to Use Which Workflow

- **Direct chat / single-session**: trivial edits, one-shot questions, typo fixes.
- **`/day [task]`**: a single task you want to ship under active supervision with the Tester gate. You commit yourself.
- **`/night [feature]`**: end-to-end delivery of a feature with all gates active and exactly two human approvals (plan + merge).
