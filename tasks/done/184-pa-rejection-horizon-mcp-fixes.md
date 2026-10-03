---
id: 184-pa-rejection-horizon-mcp-fixes
status: done
feature: horizon-mcp-fixes
---

# [PA rejection] horizon-mcp-fixes — ADR-013 §1 and §5 do not record the decisions made while the human was away

Tags: `rollup`, `pa-rejection`, `docs`, `adr`
Refs: `tasks/done/176-search-web-bing-only-and-cooldown.md`, `tasks/done/178-prefect-rate-limiter-fail-open.md`, `tasks/done/183-min-text-score-gate.md`, `docs/adrs/013_horizon_scale_mcp_surface.md`, PR #45

## Scope

The feature PASSED automated QA on every task (176–183) and, from the user's perspective, serves all
four original intents: `search_web` returns real Bing URLs locally (20/20 live runs) with the Horizon
check honestly left open; the embedding map downloads as one gzip blob with one decode line after the
lean-node shrink and the 250-document cap; Prefect failures name the variable to rotate and the limiter
never takes retrieval down; `ingest_url` stores the article (Story 1 of task 182 holds live) and the
text-leg relevance was investigated with the finding recorded.

What fails the acceptance review is **documentation discipline**, and the cure is grooming, not
implementation — so this rollup routes to the **PA** (the ADR author), not the SWE. **Docs-only: no
code, no tests, no config change.** ADR-013 is the ONE design record of this feature and the place the
human reads what was decided mid-pipeline. It already carries the orchestrator's fork decisions for
§2 (explicit `ResourceResult`, stdin decode one-liner), §3 (orphan-cluster colour rule) and §6
(best-of-two extraction) — but §1 and §5 still describe the pre-decision design, while the shipped
code, the README `search_web` row, `default.yaml` and `app_config.py` describe the post-decision one.
Three of five recorded, two silently absent, is the inconsistency the review rule marks REJECT-able.

Edit `docs/adrs/013_horizon_scale_mcp_surface.md` only (Status stays `Accepted`; Date unchanged; the
Diagram needs no change). Use the same condensed "*Implementation decision (task N, 2026-10-03,
orchestrator under the owner's pre-authorisation)*" block style §6 already uses. After the edit the
Tester's re-run is a diff read plus `make pre-commit` (prettier) — no `make memory-tests` change is
expected.

## Acceptance Criteria

- [x] Issue 1: ADR-013 §1 records task 176's decisions A–C and the `msockid` strip (see detail) in a
      dated implementation-decision block; the README `search_web` row and the `search()` docstring in
      `apps/memory/src/tree/data/web/web_serp.py` read as consistent with it.
- [x] Issue 2: ADR-013 §5 records task 178's bounded acquire — the `asyncio.wait_for` bound, the new
      `concurrency.voyage_slot_acquire_timeout_seconds` (120) knob, its derivation and its trade-off —
      so the ADR knows the knob exists.
- [x] Issue 3: ADR-013 §7 states the eval's outcome (shipped `0.0`, why no 0.5-step separates) and the
      Consequences bullet "a learned text threshold → evals" names the recorded upgrade path (a
      per-query normalised text score) instead of only "evals".
- [x] `make pre-commit` green (prettier on the markdown); `make memory-tests` unchanged (no code touched).
- [x] Tester re-runs QA (diff read) and PASSES.
- [x] PA re-runs acceptance review on the feature and ACCEPTS.

## Issues (detail)

### 1. ADR-013 §1 still says a second cooldown always answers `fetch_failed` — `docs/adrs/013_horizon_scale_mcp_surface.md:48-56`
- **What the user experiences (wrong):** a reader of the design record believes `search_web` either
  returns a full SERP or an envelope. The shipped tool (task 176, SWE entries "Fixes (Tester FAIL 1–2 +
  orchestrator items 3–6)" and "Fixes (orchestrator decisions A–C)") does something different and
  user-visible: a page AFTER the first that stays in cooldown after its one retry, times out or cannot
  connect returns the results already collected (WARNING only — the answer carries no truncation
  marker; page 1 still raises); the per-request timeout is 60 s (`_DEFAULT_TIMEOUT_SECONDS`, the only
  place it lives — 6 of 23 live calls exceeded the old 30 s); pagination advances `first` by 10 and
  stops on Bing's no-results page, on the SECOND consecutive page that adds no new URL (Bing replays
  page 1 once in ~1 of 8 probes), or at `ceil(num_results / 10) + 2` pages; `msockid` is stripped
  before dedup. `apps/memory/README.md:441` and the `search()` docstring already say this; the ADR
  does not.
- **What the spec / good UX implies (right):** ADR-013 §1 carries a dated implementation-decision block
  with those four facts, and one sentence that the search has no overall deadline (worst case
  `max_pages × 60 s`; a per-search wall-clock budget is the follow-up — the PR body says the same).
- **Suggested fix:** append the block under §1, mirroring §6's style; no body text above it changes.

### 2. ADR-013 §5 does not know the acquire is bounded or that a knob exists — `docs/adrs/013_horizon_scale_mcp_surface.md:88-94`
- **What the user experiences (wrong):** §5 says the acquire fails open on four exception classes.
  The shipped helper (`apps/memory/src/tree/models/throttle.py`, task 178 entry "Orchestrator
  follow-ups") ALSO fails open when the limiter accepts the connection and never replies: the acquire
  is wrapped in `asyncio.wait_for(rate_limit(..., timeout_seconds=bound), timeout=bound)` with a new
  `concurrency.voyage_slot_acquire_timeout_seconds: 120` in `default.yaml` / `ConcurrencyConfig`.
  That bound also caps a LEGITIMATE throttling wait — a trade-off ADR-002 §1's design never had — and
  an operator who scales `runner_global_limit` or `voyage_rpm` must re-derive it. None of this is in
  the ADR; a reader cannot find out the knob exists from the design record.
- **What the spec / good UX implies (right):** §5 records: the bound; why BOTH `wait_for` and
  `timeout_seconds` (Prefect 3.6 serialises acquires per limit in one background service whose
  `timeout_seconds` only starts at the head of the queue, so it cannot bound the caller;
  `timeout_seconds` lets the service drop the hung request so the queue drains); the derivation
  `runner_global_limit (6) × 60 / voyage_rpm (3) = 120 s`; the trade-off (past the bound the POST
  proceeds and Voyage's 429 backoff guards the quota; a hung limiter stalls a POST 2 min instead of
  6–8 min); and Prefect's cosmetic `ConcurrencySlotAcquisitionService … state=cancelled` ERROR line.
- **Suggested fix:** append a dated implementation-decision block under §5; add the knob to the ADR's
  "Context references" file list only if that list names config files (it does not today — skip).

### 3. ADR-013 §7 and Consequences predate the eval — `docs/adrs/013_horizon_scale_mcp_surface.md:132-137, 209-214`
- **What the user experiences (wrong):** §7 says the bar "is pinned by a six-query live eval"; the eval
  (task 183 Log) found NO 0.5-step separates on-topic from off-topic because `textScore` grows with the
  number of matched query terms (an 8-word off-topic query outscores a one-word on-topic hit), so the
  gate shipped at `0.0` and the production dark-photon collision stays. The Consequences bullet names
  "a learned text threshold → evals" as the upgrade, while `default.yaml` and the task Log record a
  concrete one: a per-query normalised score (`textScore / query word count`, or a minimum fraction of
  matched terms).
- **What the spec / good UX implies (right):** §7 ends with the outcome in one sentence (shipped off,
  why, where the numbers live) and the Consequences upgrade trigger names the normalised-score path.
- **Suggested fix:** two sentences; nothing else in §7 changes.

## User Stories

(Inherit from tasks 176, 178 and 183 — no new stories. Nothing user-visible changes in this rollup;
re-verify the three tasks' stories still hold by reading, not by re-running.)

---

Refs: `tasks/done/176-search-web-bing-only-and-cooldown.md`, `tasks/done/178-prefect-rate-limiter-fail-open.md`, `tasks/done/183-min-text-score-gate.md`

## Log

### [PA] 2026-10-03 23:40 — Rollup filed from acceptance review of PR #45

**VERDICT on the feature: REJECT** (documentation discipline only; every user intent verified from the
user's POV — see the acceptance-review report). Routes to the PA per the documentation-discipline rule;
docs-only; not committed by the reviewer.

### [SWE] 2026-10-03 23:58 — Implementation (docs only)

**Files modified**
- `docs/adrs/013_horizon_scale_mcp_surface.md` — the only file touched (+49 / −2). Status, Date, Diagram, the Context references and all existing body text are unchanged.
  - §1: added an *Implementation decision (task 176, …)* block covering four facts. A later page that cools down, times out or cannot connect returns the results already collected (WARNING only, no truncation marker; with nothing collected the call still raises → `fetch_failed` / `network_error`). `_DEFAULT_TIMEOUT_SECONDS = 60.0` is the only place the timeout is set (6 of 23 live calls went over 30 s). `first` advances by 10 and paging stops on the no-results page, on the 2nd consecutive no-new-URL page, or after `ceil(n/10) + 2` pages. `msockid` is stripped before dedup. One closing sentence says there is no overall deadline: `max_pages × 60 s` without a cooldown (180 s at n=10), plus 15 s and a retry per cooled page; the follow-up is a per-search wall-clock budget.
  - §5: added an *Implementation decision (task 178, …)* block. It records the `asyncio.wait_for(rate_limit(..., timeout_seconds=bound), timeout=bound)` bound, the `concurrency.voyage_slot_acquire_timeout_seconds: 120` knob, why both bounds are needed (Prefect 3.6's serialised `ConcurrencySlotAcquisitionService`), the 6 × 20 s = 120 s derivation, the trade-off (caps a legitimate wait; the 429 backoff guards the quota; 2 min instead of ~6–8 min), and the cosmetic `state=cancelled` ERROR line as a known limitation.
  - §7: added an *Outcome (task 183, …)* paragraph. No 0.5 step separates on-topic from off-topic queries, because `textScore` grows with the number of matched terms (8-word off-topic 1.524 vs one-word on-topic ~1.0–1.1; 2.0 empties both). The gate shipped at `0.0`, so the dark-photon collision stays. The numbers are in `tasks/183` and the `default.yaml` comment.
  - Consequences: the "learned text threshold → evals" upgrade now names a per-query normalised text score (`textScore` / query word count, or a minimum fraction of matched terms), re-pinned by the evals.
- `tasks/184-…md` — status set to in-progress, ACs 1–4 ticked, this entry added.

**Sources**: `web_serp.py` (constants + `search()` docstring), `throttle.py`, `default.yaml` (`concurrency`, `query.min_text_score`), `app_config.py`, and the logs of tasks 176 ("Fixes (Tester FAIL 1–2 + orchestrator items 3–6)", "Fixes (orchestrator decisions A–C)"), 178 ("Orchestrator follow-ups") and 183 ("Implementation" eval, "Pre-commit follow-ups"). I added no new facts. The README `search_web` row (`apps/memory/README.md`) and the `search()` docstring both agree with the new §1 block.

**Tests**
- Unit: 4894 passed, 0 failing (`make memory-tests`, env-status local). This is the same count as task 183's last entry; no code was touched.
- Integration: N/A (no suite, per AGENTS.md).

**Acceptance criteria**
- [x] Issue 1 — ADR-013 §1 block (decisions A–C + `msockid` + no-deadline sentence)
- [x] Issue 2 — ADR-013 §5 block (bound, knob, why both, derivation, trade-off, cosmetic ERROR)
- [x] Issue 3 — ADR-013 §7 outcome + Consequences upgrade path
- [x] `make pre-commit` green; `make memory-tests` unchanged
- [ ] Tester re-runs QA — pending
- [ ] PA acceptance — pending

**Evidence**
```
$ make env-status          → Env target: local (.env)
$ make pre-commit          → prettier / ruff check / ruff format / biome check (harness): Passed
$ make memory-format-check → 333 files already formatted
$ make memory-lint-check   → All checks passed!
$ make memory-tests        → 4894 passed in 64.68s
$ git diff --stat          → docs/adrs/013_horizon_scale_mcp_surface.md | 51 ++++++++++++++++++++++++++++--
```

**Notes**
- `make pre-commit`'s prettier hook only checks `types_or: [yaml, json5]` (`.pre-commit-config.yaml:13`), so it does NOT lint markdown. I wrapped the new lines by hand to the file's existing width (no added line is over 108 chars) and used §6's 3-space continuation indent.
- Separate, pre-existing inconsistency (not changed — no code changes allowed): `ConcurrencyConfig.runner_global_limit` defaults to `4` in `app_config.py:271`, but `default.yaml` sets `6`, and the 120 s derivation uses the YAML value. That is why the ADR anchors the number to `default.yaml`. A follow-up task could align the Pydantic default.
- No commit — waiting for the Tester.

### [Tester] 2026-10-04 00:20 — QA (docs-only diff read)

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit`: prettier, ruff check, ruff format, biome all Passed). Prettier only covers yaml/json5, so markdown is checked by hand below.
- Unit / integration: not re-run — `git status` shows only the ADR modified (plus this task file); no code, test or config touched.
- Warnings: 0

**Diff scope**: `git diff --stat` = 1 file, +49 / -2. The 2 removed lines are the re-wrapped "a learned text threshold -> evals" Consequences bullet. Status line, Date, Deciders, Context references, Diagram and all other body text are untouched. `git diff --check` clean; no added line over 108 chars; code fences balanced.

**Claim-by-claim check against shipped code and task logs**
- Section 1 later page fails soft (cooldown after its retry / `httpx.TimeoutException` / `httpx.ConnectError`, results kept, WARNING only, no marker; page 1 raises): `web_serp.py:565-583` (`if not aggregated: raise`, else `logger.warning` + `break`). `fetch_failed` for cooldown, `network_error` for timeout/connect, both retryable: `mcp/tools.py:807-830`. PASS.
- `_DEFAULT_TIMEOUT_SECONDS = 60.0`, "6 of 23 live calls over 30 s": `web_serp.py:80`; the constant comment says 6 of 23; task 176 log line 351/359. PASS.
- `first` advances by 10 (1, 11, 21): `web_serp.py:152` + `offset = page_index * _PAGE_SIZE`; stops on the no-results page (`_looks_like_legitimate_empty_serp`), 2nd consecutive no-new page (`_MAX_CONSECUTIVE_NO_NEW_PAGES = 2`), or `ceil(n/10) + 2` pages (`_EXTRA_PAGES = 2`); "~1 of 8 probes": task 176 log line 350. PASS.
- `msockid` stripped before dedup, for direct and decoded `/ck/a` URLs: `_TRACKING_QUERY_PARAMS`, `_strip_tracking_params`, `_resolve_bing_href` docstring (`web_serp.py:105,185,209`). PASS.
- No overall deadline; worst case `max_pages x 60 s` = 180 s at n=10, plus 15 s and one retry per cooled page: task 176 log lines 368-371 (n=10: 3 pages x 60 = 180 s). PASS.
- Section 5 bound: `asyncio.wait_for(rate_limit(..., timeout_seconds=bound), timeout=bound)`, same WARNING "no reply within <bound> s": `throttle.py:69-90`. Knob `concurrency.voyage_slot_acquire_timeout_seconds: 120` in `default.yaml:185` and `ConcurrencyConfig` (`app_config.py:272`, 120.0). Why-both (Prefect 3.6 serialised `ConcurrencySlotAcquisitionService`), derivation 6 x (60/3 = 20 s) = 120 s, trade-off, "2 min instead of ~6-8 min", cosmetic `state=cancelled` ERROR and the late-slot caveat: `throttle.py` docstring, `default.yaml:180-185`, task 178 log lines 207, 256-258, 277. PASS.
- Section 7 outcome: 8-word off-topic query 1.524, one-word on-topic ~1.0-1.1 (1.072 / 1.062 / 1.028), 2.0 is the lowest step emptying the off-topic legs and empties every one-word on-topic leg, shipped `min_text_score: 0.0` (`default.yaml:140`), numbers in task 183 Log (lines 157-171) and the `default.yaml` comment (lines 125-139). PASS.
- Consequences upgrade trigger names the per-query normalised score (`textScore` / query word count): matches `default.yaml:136-138` and task 183. The "or a minimum fraction of matched query terms" variant is in task 184's issue text rather than `default.yaml`; harmless. PASS.
- README `search_web` row (`apps/memory/README.md:441`) and the `search()` docstring (`web_serp.py:480-506`) read consistently with the new section 1 block. PASS.

**Acceptance criteria**
- [x] PASS — Issue 1 (section 1 block with decisions A-C + msockid + no-deadline sentence; README / docstring consistent) — ADR lines 57-75, evidence above
- [x] PASS — Issue 2 (section 5 bound, knob, derivation, trade-off, cosmetic ERROR) — ADR lines 114-134, evidence above
- [x] PASS — Issue 3 (section 7 outcome; Consequences names the normalised score) — ADR lines 178-183, 259-261
- [x] PASS — `make pre-commit` green; no code touched so `make memory-tests` unchanged (git status)
- [ ] PA acceptance — pending (not a Tester item)

**Other issues found (non-blocking, no action needed)**
- Pre-existing and out of scope: `ConcurrencyConfig.runner_global_limit` defaults to 4 (`app_config.py:271`) while `default.yaml` says 6; the ADR derivation correctly anchors to the YAML value. The SWE already flagged it; candidate follow-up task.
- The section 1 body sentence "a second cooldown answers `fetch_failed`" is left as-is and is now qualified by the block below it (page 1 only); the section 5 body "fails open on four exception classes" likewise, with the block adding the timeout bound. Acceptable per the task's "no body text above changes".

**VERDICT: PASS**


### [PA] 2026-10-04 00:35 — Acceptance Review (re-review after the rollup)

**VERDICT: ACCEPT**

Re-read `git show 7fbe460` (ADR-013 only, +49/−2) and the Tester's claim-by-claim entry above. All three
issues resolved: §1 carries the task-176 block (later page fails soft with no truncation marker, 60 s per
request, pagination stop rules, `msockid` strip, no overall deadline); §5 carries the task-178 bounded
acquire (the `wait_for` bound, the `voyage_slot_acquire_timeout_seconds: 120` knob, why both, the
6 × 20 s derivation, the trade-off, the cosmetic `state=cancelled` line); §7 states the shipped-`0.0`
outcome and Consequences names the per-query normalised text score as the upgrade path. Each claim
matches the code (`web_serp.py`, `throttle.py`, `default.yaml`, `app_config.py`) and the task logs; the
README `search_web` row and the `search()` docstring read consistently with §1. No code touched, so the
previously-PASS criteria of tasks 176–183 stand. Hand off to the PR Reviewer.

Follow-up (not blocking, no new task filed): `ConcurrencyConfig.runner_global_limit` defaults to 4 in
`app_config.py` while `default.yaml` sets 6 — the ADR anchors the 120 s derivation to the YAML value.
