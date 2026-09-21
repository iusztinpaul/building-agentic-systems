---
id: 157-endpoint-provisioning-wait-and-guard-visibility-pin
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# What the live run taught us about `modal endpoint list`: `-test` waits while OUR endpoint is `provisioning`, `deploy` says the create is asynchronous, and the guard's endpoint-first short-circuit is pinned

Tags: `memory`, `modal`, `deploy`, `docs`, `tests`
Depends on: None (by NNN order it runs after #156; it touches none of its files)
Blocks: #141
Implements: ADR-009 — Decision 11, revision 6 ("A Dedicated endpoint is `provisioning` before it is `live`") and Consequences ("What the existence guard can and cannot see"). Found live by #141 round 1, cycles 4.0, 4a and 4d.

## Scope

**EXECUTION ORDER of the fix round: 153 -> 154 -> 155 -> 156 -> 157 -> 141 round 2.**

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
Run test commands one at a time — two concurrent pytest runs contend for the local MongoDB/Prefect and stall.
Do not run the real memory pipelines against the local database (shared with the human's `main` checkout).
Every test here mocks `run_modal` (the suite already sets `TREE_MODAL_DRY_RUN=1`); no test sleeps for real.

**Facts from the live run (`tasks/141` Log).**
- `modal endpoint create` is ASYNCHRONOUS: it returned in ~4 s with `status: provisioning`; `live` came after 2m15s (`Qwen/Qwen3-Embedding-0.6B`) and 9m25s (`Qwen/Qwen3.5-0.8B` — 565 s, 94 % of the 600 s warm-up budget). The SWE bridged it BY HAND, polling `modal endpoint list --json`, and only then ran `-test`; whether `-test` on a provisioning endpoint is sat out by the health poller (503) or fails at once in `resolve_server_url` was never observed. This task makes the answer irrelevant.
- `modal endpoint list --json` keys: `name, endpoint_id, status, created_at, created_by`; statuses seen: `provisioning`, `live`.
- `modal app list` shows NEITHER the `ep-*` app behind a Dedicated endpoint NOR long-stopped apps; `modal endpoint list` hides stopped endpoints. `existing_kind` (`modal_cli.py:181-209`) already reads the ENDPOINT list first and returns on a hit, and `TestExistingKind::test_both_lists_are_read_only_json_calls` already pins the argv order. PA analysis: **no collision class is open** — a hand-made name cannot carry `tree-`, a live or provisioning endpoint is always listed, and whatever is invisible is STOPPED (a deploy replaces it, never overwrites something serving). So this is a pin + a docstring, not a fix.
- Modal's refusals arrive on STDERR; `_log_modal_output` joins both streams since #147. Nothing to build — state it in the docstring of `_log_modal_output` if it is not already there.

**What to build**
1. `tree.models.modal_cli.endpoint_status(entry: ModalModelConfig) -> str | None` — ONE `run_modal(["modal", "endpoint", "list", "--json"], capture_output=True)`; the `status` of the row whose `name == entry.endpoint_name`, else `None`. A dry run (`None` result), a non-zero exit, or unparsable output -> `None` plus ONE WARNING `could not read the endpoint list (<detail>) — not waiting for provisioning`. This is a WAIT, not a guard: it fails OPEN (the smoke test gives the verdict), unlike `_list_rows`.
2. `tree.models.modal_cli.wait_until_live(entry, *, deadline_s: float = PROVISIONING_DEADLINE_S, sleep=time.sleep, clock=time.monotonic) -> None` with `PROVISIONING_DEADLINE_S = 1800.0` (a code constant with a comment: ~3x the slowest measured 565 s; NOT a YAML knob). Rules, closed:
   - status `None` (an App, a stopped endpoint, a dry run, an unreadable list) or `live` -> return at once, no log line for `None`.
   - `provisioning` -> INFO `Provisioning: <endpoint_name> is not live yet — <N>s/1800s`, sleep on the poller's schedule (5 s x 1.5 capped at 15 s — reuse `modal_warmup`'s constants, do not copy the numbers), read again; on `live` INFO `Live: <endpoint_name> after <N>s`.
   - any OTHER status -> ONE WARNING `<endpoint_name> has status '<x>' — not waiting` and return.
   - deadline spent -> `ModelError("<endpoint_name> is still provisioning after 1800s — check `modal endpoint list` and the Modal dashboard")` (the driver's existing `ModelError` branch turns it into exit 1).
3. `scripts/modal_model.py::test_command`: call `wait_until_live(entry)` before `asyncio.run(test(model))`, inside the existing `try` (glue only). Update its docstring: the sentence "No dry run either — it starts no CLI" is now wrong — REPLACE it with "one read-only `modal endpoint list --json` first (skipped under `DRY_RUN=yes`)".
4. `modal_router.run_deploy`: after a SUCCESSFUL endpoint create, ONE INFO line: `Endpoint <endpoint_name> is provisioning — Modal returns before it is live (minutes). make memory-deploy-model-test MODEL=<repo_id> waits for it.` Not after an App deploy, not in a dry run, not after a refusal.
5. `existing_kind` docstring: replace the sentence claiming the app list "catches everything else" with the live fact (it lists neither endpoint-backed `ep-*` apps nor long-stopped apps; the endpoint leg is what protects, hence first).
6. `apps/memory/README.md`: the paragraph #141 round 1 added ("A **Dedicated endpoint** has one wait the poller cannot cover … Watch that column, then run `-test`.") — REWRITE it: `create` returns while `provisioning` (keep the two measured times); `-test` waits for `live` by itself (up to 30 minutes) and then polls `/health`. Remove "Watch that column, then run `-test`" and the unobserved claim about `resolve_server_url`.
7. `.agents/skills/run-pipelines-e2e/SKILL.md`, "Models on Modal" step: one clause — `-test` waits out `provisioning`. Do not touch the verbatim `DRY_RUN=yes` closing sentence.

**Out of scope:** making `deploy` block until live; a YAML knob for the provisioning budget; waiting inside the clients (`ModalLLM` / `ModalEmbeddingModel` — a pipeline never runs seconds after a create); probing what Modal does on `endpoint create` over a STOPPED endpoint's name (recorded live by #141 round 2; any refusal already aborts as verdict `other`).

## Acceptance Criteria

- [ ] `tests/unit/models/test_modal_cli.py::TestEndpointStatus`: returns `"provisioning"` / `"live"` for our row using a fixture that is the REAL key set (`name, endpoint_id, status, created_at, created_by`); `None` when only the operator's un-prefixed `qwen3-embedding-0-6b` is listed; `None` + exactly ONE WARNING containing `not waiting for provisioning` for a non-zero exit, for non-JSON output and for a dry run; the argv is exactly `["modal", "endpoint", "list", "--json"]` with `check=False`.
- [ ] `::TestWaitUntilLive::test_no_row_returns_at_once` — one list call, `sleep` never called, no `Provisioning` record.
- [ ] `::test_provisioning_then_live` — statuses `provisioning, provisioning, live` with a fake clock: `sleep` called with `5.0` then `7.5`; records `Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s`, a second `Provisioning:` line, then `Live: tree-qwen3-5-0-8b after <N>s`.
- [ ] `::test_the_deadline_is_a_model_error` — always `provisioning`, fake clock past 1800 -> `ModelError` containing `still provisioning after 1800s`.
- [ ] `::test_an_unknown_status_is_not_waited_on` — status `"failed"` -> returns, ONE WARNING containing `has status 'failed'`, `sleep` never called.
- [ ] `::test_the_schedule_is_the_pollers` — `wait_until_live` imports `INITIAL_INTERVAL_S`, `BACKOFF_FACTOR`, `MAX_INTERVAL_S` from `tree.models.modal_warmup` (assert by patching `MAX_INTERVAL_S` to `6.0` and seeing a `6.0` sleep).
- [ ] `TestExistingKind::test_a_live_endpoint_short_circuits_the_app_list` — with our endpoint row present AND an app-list fixture that would say `deployed` for `entry.app_name`, the result is `"endpoint"` and `run` was called exactly ONCE (the app list was never read). `test_both_lists_are_read_only_json_calls` stays green unchanged.
- [ ] `::test_a_provisioning_endpoint_is_already_an_endpoint` — a row with `status: "provisioning"` makes `existing_kind` return `"endpoint"` (the guard refuses a second create during the 2-10 minutes before `live`).
- [ ] `tests/unit/models/test_modal_router.py`: after a mocked successful endpoint create the log contains `is provisioning — Modal returns before it is live`; it is absent after an App deploy, a dry run and a refusal.
- [ ] `tests/unit/scripts/` (wherever the driver's tests live): `test_command` calls `wait_until_live` BEFORE the smoke test (order asserted with a shared mock manager); a `ModelError` from it exits 1 and the smoke test never runs.
- [ ] `grep -c "Watch that column" apps/memory/README.md` -> 0; `grep -c "it starts no CLI" apps/memory/scripts/modal_model.py` -> 0.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator tests an endpoint seconds after creating it
1. `make memory-deploy-model MODEL=Qwen/Qwen3.5-0.8B` ends with `Endpoint tree-qwen3-5-0-8b is provisioning — Modal returns before it is live (minutes). make memory-deploy-model-test MODEL=Qwen/Qwen3.5-0.8B waits for it.`
2. Operator runs `make memory-deploy-model-test MODEL=Qwen/Qwen3.5-0.8B` immediately.
3. Sees `Provisioning: tree-qwen3-5-0-8b is not live yet — 0s/1800s` … `Live: tree-qwen3-5-0-8b after 548s`, then the usual `Warming …` / `Warm: …` lines and the smoke lines. They never opened `modal endpoint list`.

### Story: Operator tests an App
1. `make memory-deploy-model-test MODEL=voyageai/voyage-4-nano` (served as our vLLM App).
2. No `Provisioning` line at all — the endpoint list has no `tree-voyage-4-nano` row — and the health poller starts at once, as before.

### Story: Modal's list is down during a test
1. `modal endpoint list --json` exits 1.
2. ONE warning `could not read the endpoint list (exit 1) — not waiting for provisioning`, and the smoke test runs and gives the verdict. (A `deploy` in the same situation still REFUSES, exit 3 — the guard fails closed; this wait fails open.)

### Story: An endpoint never becomes live
1. Modal leaves `tree-qwen3-5-0-8b` on `provisioning`.
2. After 30 minutes: `tree-qwen3-5-0-8b is still provisioning after 1800s — check `modal endpoint list` and the Modal dashboard`, exit 1.

### Story: Second deploy while the first is still provisioning
1. Operator repeats `make memory-deploy-model MODEL=Qwen/Qwen3.5-0.8B` one minute after the first.
2. `Refusing to deploy Qwen/Qwen3.5-0.8B via endpoint: 'tree-qwen3-5-0-8b' already exists on Modal as a Dedicated endpoint. …`, exit 3 — a `provisioning` row counts.

### Story: The next engineer wonders why the guard reads two lists
1. Reads `existing_kind`'s docstring: the app list cannot see an endpoint's app; the endpoint list cannot see a stopped endpoint; the endpoint leg is first and short-circuits.
2. Finds the same paragraph in ADR-009 Consequences.

---

Blocked by: (none)

## Log

### [PA] 2026-09-21 16:10 — Grooming

**Summary**
`-test` waits while our endpoint row says `provisioning` (fail-open, 1800 s constant), `deploy` announces the asynchronous create, and the guard's endpoint-first short-circuit and its treatment of a `provisioning` row are pinned by tests.

**Key decisions**
- F4 is NOT a hole (analysis in Scope and ADR-009 Consequences): pin + docstring only.
- The wait lives in the DRIVER's `test`, not in the clients and not in `deploy`: it is the only place a human (or #141) acts seconds after a create; it is robust whichever way a provisioning endpoint fails (503 or unresolvable), which round 1 never observed.
- Fail OPEN, unlike the guard: a wait that cannot look must not block a smoke test.
- A code constant, not a knob: 565 s measured against a 600 s warm-up budget is too tight to share `warmup_deadline_s`, and nobody has asked to tune it.

**Dependencies**
- None.

**User stories**
- 6: test right after create, App untouched, list down, never live, double deploy during provisioning, the two-list rationale.

Ready for implementation.
