---
id: 143-modal-name-namespace-existence-guard-and-dry-run
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# Never touch Modal infrastructure we did not create: `tree-` name namespace, an existence guard before deploy, an ownership check before stop, a `DRY_RUN=yes` safety rail — and an `HF_TOKEN` hint that only fires on gated-looking failures

Tags: `modal`, `deploy`, `infra`, `safety`, `rollup`
Depends on: None (#138, #139, #140 and #142 — everything this task edits — are done)
Blocks: #144, #145, #146, #147, #148, #149, #141
Implements: ADR-009 — Decision 3 ("Names": the `tree-` namespace, the existence guard, "we only ever stop what carries our prefix", the dry run) and Decision 9 (the `HF_TOKEN` hint only for gated-looking failures)

## Scope

**EXECUTION ORDER of the rework: 143 -> 144 -> 145 -> 146 -> 147 -> 148 -> 149 -> 150 -> 141.** This task is FIRST
because every later task's SWE and Tester run the driver's tests: the dry-run rail must exist before anyone
touches the driver again. #138/#139/#140/#142 are done (`702c08a`) and provide everything this task edits.

**HARD SAFETY RULE (verbatim in every task that touches the driver or the scripts):** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
(In THIS task `DRY_RUN=yes` does not exist until section C is built — until then, mocked unit tests only. The
three `DRY_RUN=yes` commands of the "are run for real" acceptance criterion are the ONLY `make memory-deploy-*`
invocations allowed here, and only after `TestDryRun` is green.)

**What this task rewrites (committed code):** `ModalEmbeddingModelConfig.endpoint_name` + a new validator
(`app_config.py`); `scripts/modal_embedding_model.py::_run_modal` and `_hint_if_no_token`; `HF_TOKEN_HINT`'s
call sites; a NEW `src/tree/models/modal_cli.py`; `apps/memory/Makefile` (2 targets); `tests/unit/conftest.py`
(one autouse fixture); the hard-coded names listed in section D; README's Modal section. `serving` /
`base_model` / `ServingPath` are NOT touched here — #145 removes them. The guard is therefore keyed on WHAT
the command is about to create (`target: Literal["endpoint", "app"]`), not on the three path names, so #145
reuses it unchanged: today `target = "endpoint" if path == "endpoint" else "app"`.

**Why (the incident, 2026-09-20).** The catalog derived `endpoint_name` = slug of the repo name and
`app_name` = `ep-<endpoint_name>` — by design the SAME name Modal gives a Dedicated Endpoint an operator
creates by hand for that model. An accidental real `modal deploy` of the SGLang fallback script for
`Qwen/Qwen3-Embedding-0.6B` therefore landed on app `ep-qwen3-embedding-0-6b`, which the human had created in
the dashboard on 2026-08-24, and silently overwrote it (v3 over v1/v2; no rollback on the plan). Nothing in the
driver looked before it wrote, and `-stop` would have stopped the same app just as blindly.

**A. Namespace (the fix by construction).**
- ONE constant next to the ONE derivation, in `tree/config/app_config.py` (`modal_catalog.py` imports from it,
  not the reverse): `MODAL_NAME_PREFIX = "tree"`.
- `ModalEmbeddingModelConfig.endpoint_name` = `f"{MODAL_NAME_PREFIX}-{slug}"`; `app_name` stays
  `f"ep-{endpoint_name}"`. Examples: `voyageai/voyage-4-nano` -> `tree-voyage-4-nano` / `ep-tree-voyage-4-nano`;
  `Qwen/Qwen3-Embedding-0.6B` -> `tree-qwen3-embedding-0-6b` / `ep-tree-qwen3-embedding-0-6b`;
  `BAAI/bge-m3` -> `tree-bge-m3` / `ep-tree-bge-m3`. H1 (endpoint `N` = app `ep-N`, class `Server`) is untouched:
  a prefix is part of `N`. The empty-slug validator keeps checking the SLUG (not the prefixed name).
- NEW load-time validator: `len(app_name) <= 63`. Source: the pinned client's
  `modal/_utils/name_utils.py::is_valid_object_name` (modal 1.5.5) — charset `[a-zA-Z0-9-_.]`, code says
  `len <= 64`, its error text says "shorter than 64"; 63 satisfies both. The derived charset `[a-z0-9-]` is
  already inside Modal's. With `ep-tree-` = 8 characters the slug may be at most 55. Message:
  `repo_id '<id>' derives the Modal app name '<name>' (<n> characters); Modal allows at most 63.`
  **SWE must verify** whether `modal endpoint create --name` has a STRICTER limit — by READING the pinned
  client's source (`.venv/lib/python*/site-packages/modal/cli/endpoint.py`) and
  https://modal.com/docs/cli/latest/endpoint.md, NOT by running `modal` (not even `--help`: the hard safety
  rule has no read-only exception); if so, lower the constant and say so in `## Log`. The URL label
  `<workspace>--<app>-server` may exceed DNS's 63 and Modal then shortens it — irrelevant, the client reads the
  URL from `get_url`, never builds it.
- NOT a YAML knob, NOT per entry: a second project in the same workspace is what would justify one.

**B. Existence guard + ownership check** — logic in a NEW `tree/models/modal_cli.py` (the only module under
`src/tree/` that runs the `modal` CLI; `modal_catalog.py` stays pure and subprocess-free); the script keeps glue only.
- `assert_owned_name(name: str) -> None`: raises `ModelError` unless `name` starts with `tree-` (an endpoint
  name) or `ep-tree-` (an app name). Called for EVERY `deploy` and `stop` on the name about to be passed to
  `modal`, AFTER the argv is built. `FORCE` never overrides it. Message:
  `Refusing to <deploy|stop> '<name>': it lacks the 'tree-' prefix, so this project did not create it.`
  Today it can only fire if the derivation regresses — that is the point.
- `existing_kind(entry) -> Literal["none", "endpoint", "app"]`, read-only, two calls, `check=False`, output
  captured: `modal endpoint list --json` and `modal app list --json`. `endpoint` = a row whose `name` equals
  `entry.endpoint_name`; else `app` = a row whose `description` equals `entry.app_name` and whose `state` is
  neither `stopped` nor `stopping...`; else `none`. Field names read from the pinned client's source on
  2026-09-20 (`modal/cli/endpoint.py::list_` -> `name, endpoint_id, status, created_at, created_by`, ACTIVE
  endpoints only; `modal/cli/app.py::list_` -> `app_id, description, state, tasks, created_at, stopped_at`;
  keys are the snake_cased column titles, `modal/cli/utils.py::_col_name_to_json_key`). **SWE must verify** on
  modal 1.5.5 by re-reading those two source files in `.venv` (both commands define `--json`) and paste the key
  set into `## Log`; the LIVE confirmation of both key sets is #141's step 4.0 (its baseline IS these two calls). Because `endpoint list` hides stopped
  endpoints, the app list is what catches everything else. **Fallback if a call exits non-zero, `--json` is
  rejected, or the output is not a JSON list:** fail CLOSED — treat as "cannot verify" and refuse (exit 3)
  unless forced; never parse the human-readable table.
- `guard_deploy(entry, target, force) -> None` — the guard matrix for `deploy` (stop runs NO list call — the
  ownership check is its guard). `target` is `endpoint` for the `endpoint` path, `app` for `sglang` / `vllm`:

  | `target` | existing `none` | existing `endpoint` | existing `app` (live, not an endpoint) |
  |---|---|---|---|
  | `endpoint` | run | REFUSE | REFUSE |
  | `app` | run | REFUSE | run (a redeploy of our own script app = a normal update) |

  REFUSE = one ERROR line + exit code **3**, nothing else is run:
  `Refusing to deploy <repo_id> via <path>: '<name>' already exists on Modal as <a Dedicated endpoint|an app>. Stop it first (make memory-deploy-embedding-model-stop MODEL=<repo_id> SERVING=<endpoint|the path it was deployed with>) or pass FORCE=yes to deploy over it.`
  Cannot-verify line: `Refusing to deploy <repo_id>: could not list Modal <apps|endpoints> (<exit N|invalid JSON>), so an overwrite cannot be ruled out. Pass FORCE=yes to skip the check.`
  `<name>` is the endpoint name for kind `endpoint`, the app name for kind `app`.
  With `--force` / `FORCE=yes` a REFUSE becomes ONE WARNING (`FORCE=yes: deploying over the existing <kind> '<name>'.`)
  and the deploy runs; a failed list under force is a WARNING too.
- Exit codes after this task: `0` ok (or dry run), `1` smoke test failed, `2` usage/config (unknown `MODEL`,
  bad `SERVING`, missing script, `modal` not installed, ownership check), `3` guard refusal, otherwise
  `modal`'s own exit code. #139's rules hold everywhere: every logged argv through `redact_argv`, every
  `subprocess.run` with `check=False`, the token never in a message. The list calls carry no token.
- Make: `deploy-embedding-model` gains `$(if $(filter yes,$(FORCE)),--force,)`; `-stop` does NOT take `FORCE`.

**C. Dry run — the ONLY safe way to exercise the driver without deploying.**
- `--dry-run` on `deploy` and `stop`, `DRY_RUN=yes` on both Make targets, and the env var
  `TREE_MODAL_DRY_RUN=1` (read at CALL time, inside `modal_cli.py`'s ONE function that every `modal` subprocess
  call goes through — flag OR env var turns it on). Dry run: build the argv, run the ownership check, log
  `DRY RUN — would run: <redacted argv>` and `DRY RUN — skipped the Modal existence check (no modal process is started).`,
  exit 0. NO `modal` process is started — not even the two list calls. `test` has no dry run (it starts no CLI).
- Test-safety rail: an autouse fixture in `tests/unit/conftest.py` sets `TREE_MODAL_DRY_RUN=1` for the whole
  unit suite; the driver tests' existing `run` fixture (the `subprocess.run` spy) is the ONLY place that
  deletes it — so leaving dry run and patching `subprocess.run` are one act, and an un-mocked test can never
  reach the real CLI.
- README (`## Modal embedding deployment (optional)`), add exactly these facts (wording free, values fixed):
  1. `Every name we create on Modal starts with tree- (endpoint tree-<model>, app ep-tree-<model>), so a Dedicated Endpoint you made by hand in the dashboard (ep-<model>) can never be overwritten or stopped by these targets.`
  2. `deploy refuses (exit 3) when the name already exists as something the requested Serving path did not create; FORCE=yes overrides. -stop only ever stops tree- names.`
  3. `DRY_RUN=yes prints the (redacted) modal command and exits 0 without starting modal — the ONLY way to try these targets without deploying. A fake modal on PATH does NOT work: make and uv run put .venv/bin first, so the real CLI wins.`
  and fix line ~590 (`modal app logs ep-<endpoint_name>` stays correct; add the example `ep-tree-voyage-4-nano`).

**C2. The `HF_TOKEN` hint only for gated-looking failures (bug found live 2026-09-20).** Today
`_hint_if_no_token` fires on EVERY failure with an empty token — it was printed under an architecture mismatch,
under "is not available for dedicated Endpoints" and under a cold-start 503, none of which a token fixes.
- NEW pure function `looks_gated(text: str) -> bool` in `modal_catalog.py` (next to `HF_TOKEN_HINT`): true iff
  `text` contains, case-insensitively, one of `401`, `403`, `gated`, `GatedRepoError`, `RepositoryNotFoundError`,
  `access to model` — the strings the Hub and `huggingface_hub` use for a repo you may not read.
- The hint is logged iff the token is EMPTY **and** `looks_gated(<what failed>)`: for `test`, the text is
  `str(exc)`; for `deploy`, the text is Modal's captured output. `modal deploy` (an image build) must keep
  streaming to the terminal, so ONLY `modal endpoint create` is run with `capture_output=True, text=True`; its
  stdout + stderr are then logged line by line through a NEW `redact_text(text, token)` (replaces a non-empty
  token value with `***` — Modal is not known to echo argv, this is belt and braces) and handed to
  `looks_gated`. A failed `modal deploy` logs NO hint (a gated download fails at container START, i.e. in `test`).
- The proxy-token 401 of the smoke test (`Modal answered 401 for …/health — the Proxy token is wrong`) must NOT
  trigger the HF hint: the driver skips the hint when the exception is a plain `ModelError` whose message names
  the Proxy token. Simplest correct rule: hint only for `ExtractionError` (server-side failures), never for a
  bare `ModelError` (configuration). Say in `## Log` which rule was implemented.

**D. Every hard-coded name that must move to the prefix** (line numbers as of 2026-09-20; re-grep, they drift):
- `src/tree/config/app_config.py` ~795-817 (both docstrings + the derivation), ~853-865 (validator docstring).
- `src/tree/models/modal_catalog.py` ~258-303 (`modal_cli_command` docstring); `src/tree/models/modal_server.py:7`.
- `deploy/modal_sglang_embedding.py:21`, `deploy/modal_vllm_embedding.py:17` — "the same name a Dedicated
  Endpoint…" becomes "the same SHAPE (`ep-<name>`, class `Server`) inside our `tree-` namespace".
- `apps/memory/Makefile:89` (`ep-<model>` -> `ep-tree-<model>`; document `FORCE=yes`, `DRY_RUN=yes`).
- `tests/unit/config/test_app_config.py`: 1127 (`ep-bge-m3`), 1150-1151 + 1157 + 1164 (`test_name_derivation` table),
  1196, 1261 (duplicate-name message `'ep-voyage-4-nano'`).
- `tests/unit/models/test_modal_catalog.py`: 221, 316, 332, 380, 391, 477.
- `tests/unit/models/test_modal_embedding.py`: 45, 313, 644. `tests/unit/models/test_modal_server.py`: 35, 186, 194, 224
  (NOT 171/330/359: `modal-recipe/qwen3-embedding-0-6b` is a served model id, not a name).
- `tests/unit/scripts/test_modal_embedding_model_script.py`: 77, 120, 227, 246.
- `tests/unit/deploy/test_modal_embedding_scripts.py:169` (docstring).
- `configs/default.yaml` and `tests/unit/config/fixtures/frozen_config.yaml`: no hit today — names are derived.
- NOT edited here: `docs/glossary.md`, `docs/adrs/009_…` (PA, already in the grooming commit);
  `.agents/skills/run-pipelines-e2e/SKILL.md` (#141 adds the dry-run sentence); everything under `tasks/done/`,
  #142 included (true when written, and their Logs are history).

## Out of scope
- ANY `modal` process — deploy, create, stop, `list`, `--help`. Facts about the CLI come from the pinned
  client's source in `.venv`; live proof of the prefixed names and of the `list --json` keys is #141's.
- Touching, renaming, stopping or deleting what already exists in the workspace (`ep-qwen3-embedding-0-6b` —
  the human's; `ep-voyage-4-nano` — accidental, stopped).
- Auto-routing, removing `serving` / `base_model`, LLMs, the warm-up poller (#144-#148).
- Modal app tags / a marker file as an ownership signal; a configurable prefix; a per-environment prefix;
  telling a `vllm` app from an `sglang` app of the same model (both are ours, same name by design).
- A guard on `test` (it only reads) and list calls on `stop`.

## Acceptance Criteria

- [x] `test_name_derivation` (parametrised) asserts `voyageai/voyage-4-nano` -> `tree-voyage-4-nano` / `ep-tree-voyage-4-nano`, `Qwen/Qwen3-Embedding-0.6B` -> `tree-qwen3-embedding-0-6b` / `ep-tree-qwen3-embedding-0-6b`, `BAAI/bge-m3` -> `tree-bge-m3` / `ep-tree-bge-m3`, and `MODAL_NAME_PREFIX == "tree"`.
- [x] `test_app_name_length_limit`: a `repo_id` whose slug is 55 characters loads (`len(app_name) == 63`); 56 characters raises a `ValidationError` containing `at most 63`. `test_empty_slug_is_still_rejected`: `a/---` still fails with the existing message (not masked by the prefix).
- [x] `test_modal_cli_command_uses_prefixed_names`: endpoint create argv contains `--name tree-qwen3-embedding-0-6b`; endpoint stop is `modal endpoint stop -y tree-qwen3-embedding-0-6b`; fallback stop is `modal app stop -y ep-tree-voyage-4-nano`; `build_deploy_spec("voyageai/voyage-4-nano", "vllm").app_name == "ep-tree-voyage-4-nano"`.
- [x] `TestAssertOwnedName`: `tree-x` and `ep-tree-x` pass; `qwen3-embedding-0-6b`, `ep-qwen3-embedding-0-6b`, `ep-treex`, `` (empty) raise `ModelError` naming the prefix.
- [x] `TestExistingKind` (all `subprocess.run` mocked, fixtures = JSON lists with the keys above): endpoint row with the name -> `endpoint`; no endpoint row + app row `state: deployed` -> `app`; app row `state: stopped` only -> `none`; both lists empty -> `none`; a row named `ep-qwen3-embedding-0-6b` (un-prefixed) is ignored -> `none`; list exit 1 -> the cannot-verify error; stdout `not json` -> the same. Both calls are asserted to be `[..., "list", "--json"]` with `check=False`.
- [x] `TestGuardDeploy` (on `modal_cli.guard_deploy`, keyed on `target`): `endpoint` x {`none`,`endpoint`,`app`} and `app` x the same, x force {no, yes} = 12 cases with the matrix above.
- [x] `TestDeployGuard` (through the driver) covers the full matrix — path {`endpoint`, `sglang`, `vllm`} x existing {`none`, `endpoint`, `app`} x force {no, yes} = 18 cases: the 4 REFUSE cells without force exit 3, log the one-line refusal naming the repo id, the path and the name, and the deploy argv is NEVER passed to `subprocess.run`; with `--force` the same 4 cells log one WARNING and run; the other cells run with no warning.
- [x] `test_guard_failure_is_closed`: a failing list call -> exit 3 without force, WARNING + deploy with `--force`.
- [x] `TestStopOwnership`: `stop` starts NO list call; with `assert_owned_name` fed an un-prefixed name (derivation patched to `qwen3-embedding-0-6b`) `stop` AND `deploy` exit 2 with the ownership message and `subprocess.run` is not called — also with `--force`.
- [x] `TestDryRun`: `deploy --dry-run` and `stop --dry-run` (and, separately, `TREE_MODAL_DRY_RUN=1` with no flag) exit 0, log `DRY RUN — would run: modal …` and the skipped-check line, and `subprocess.run` has 0 calls; with a FAKE token on a custom-weights endpoint entry the line shows `--custom-hf-token ***` and the token value appears nowhere in the captured output.
- [x] `test_unit_suite_defaults_to_modal_dry_run`: with NO `run` fixture, `os.environ["TREE_MODAL_DRY_RUN"] == "1"` and invoking `deploy --model Qwen/Qwen3-Embedding-0.6B` with `subprocess.run` patched to raise `AssertionError` exits 0. `grep -n "TREE_MODAL_DRY_RUN" apps/memory/tests/unit/conftest.py` >= 1; the ONLY `delenv("TREE_MODAL_DRY_RUN"…)` in the test tree is inside the `run` fixture.
- [x] `TestLooksGated`: `"401 Client Error"`, `"403 Forbidden"`, `"GatedRepoError: …"`, `"Cannot access gated repo"` -> true; the three live texts `"'voyageai/voyage-4-nano' is not available for dedicated Endpoints."`, `"The custom model is not a servable checkpoint of base model 'Qwen/Qwen3-Embedding-0.6B': hidden_size …"`, `"Health check on https://x/health answered 503."` -> false.
- [x] `TestHfHint`: empty token + endpoint create failing with the not-available text -> NO `HF_TOKEN` line in the captured log; empty token + output containing `GatedRepoError` -> exactly one hint line; a SET token -> never a hint; `test` failing with the Proxy-token 401 `ModelError` -> no hint; `test` failing with `ExtractionError("… 403 …")` and an empty token -> one hint.
- [x] `test_endpoint_create_output_is_captured_and_redacted`: `subprocess.run` for `modal endpoint create` is called with `capture_output=True, text=True, check=False`; with `_FAKE_TOKEN` planted in the fake stdout the logged lines show `***` and never the token; `modal deploy <script>` is NOT captured.
- [x] Token rules regress-tested on the new paths: no test passes `check=True`; the refusal, warning and dry-run lines never contain `_FAKE_TOKEN`.
- [x] `grep -rnE "ep-(voyage-4-nano|qwen3-embedding-0-6b)|\"(voyage-4-nano|qwen3-embedding-0-6b)\"" apps/memory/src apps/memory/tests apps/memory/deploy apps/memory/scripts apps/memory/Makefile apps/memory/README.md` -> only served-model-id / repo-id strings remain (list any survivor with its reason in `## Log`); `grep -c "ep-tree-" apps/memory/README.md` >= 1.
- [x] `grep -c "DRY_RUN=yes" apps/memory/README.md` >= 1, `grep -c "FORCE=yes" apps/memory/README.md` >= 1, `grep -c "PATH" apps/memory/README.md` >= 1 inside the Modal section; `make -n memory-deploy-embedding-model MODEL=x DRY_RUN=yes FORCE=yes` shows `--dry-run --force`; `make -n memory-deploy-embedding-model-stop MODEL=x FORCE=yes` shows NO `--force`.
- [x] `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B DRY_RUN=yes` and `… MODEL=voyageai/voyage-4-nano SERVING=vllm DRY_RUN=yes` and `make memory-deploy-embedding-model-stop MODEL=Qwen/Qwen3-Embedding-0.6B DRY_RUN=yes` are run for real (they start no `modal`), exit 0, and their `DRY RUN — would run: …` lines with the `tree-` names are pasted in `## Log`.
- [x] `## Log` answers both "SWE must verify" items (endpoint-name limit; the two `--json` key sets on modal 1.5.5) with the SOURCE FILE and line they were read from — no `modal` process was started to answer them.
- [x] `test_catalog_does_not_import_modal` stays green and `modal_catalog.py` still imports no `subprocess`.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

## User Stories

### Story: Operator has a hand-made Dedicated Endpoint of the same model
1. In the dashboard the operator once created an endpoint for `Qwen/Qwen3-Embedding-0.6B`; Modal named its app `ep-qwen3-embedding-0-6b`.
2. They run `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B SERVING=sglang`.
3. The log shows `Running: modal deploy deploy/modal_sglang_embedding.py`; Modal reports the app `ep-tree-qwen3-embedding-0-6b`.
4. `modal app list` shows BOTH apps; `modal app history ep-qwen3-embedding-0-6b` has no new version.
5. `make memory-deploy-embedding-model-stop MODEL=Qwen/Qwen3-Embedding-0.6B SERVING=sglang` runs `modal app stop -y ep-tree-qwen3-embedding-0-6b`; the hand-made endpoint keeps serving.

### Story: Operator walks the ladder and forgets to stop the endpoint first
1. `tree-voyage-4-nano` is live as a Dedicated endpoint (`SERVING=endpoint`).
2. `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano SERVING=vllm`.
3. One ERROR line: `Refusing to deploy voyageai/voyage-4-nano via vllm: 'tree-voyage-4-nano' already exists on Modal as a Dedicated endpoint. Stop it first (…) or pass FORCE=yes to deploy over it.`; exit code 3; no image is built.
4. They run `…-stop MODEL=voyageai/voyage-4-nano SERVING=endpoint`, then the deploy again -> it runs.

### Story: Operator updates their own fallback app
1. `ep-tree-voyage-4-nano` is live from `SERVING=vllm`; the operator bumps `revision` in the YAML.
2. `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano` -> no refusal, no warning, `modal deploy` runs (a normal update).

### Story: Operator really wants to deploy over it
1. Same state as story 2, but the operator adds `FORCE=yes`.
2. One WARNING `FORCE=yes: deploying over the existing Dedicated endpoint 'tree-voyage-4-nano'.`, then `Running: modal deploy …`.

### Story: A tester checks the driver's command without deploying anything
1. `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano SERVING=endpoint DRY_RUN=yes` with an `HF_TOKEN` in `.env`.
2. Output: `DRY RUN — would run: modal endpoint create --name tree-voyage-4-nano --model Qwen/Qwen3-Embedding-0.6B --custom-hf-repo voyageai/voyage-4-nano --custom-hf-revision <sha> --routing-region eu-west --custom-hf-token ***`, then the skipped-check line; exit 0.
3. `modal app list` is unchanged — no `modal` process ever started. They did NOT put a fake `modal` on `PATH`: the README says why that fails.

### Story: A contributor writes a new driver test and forgets to mock `subprocess.run`
1. Their test calls `CliRunner().invoke(main, ["deploy", "--model", "Qwen/Qwen3-Embedding-0.6B"])` with no `run` fixture.
2. `make memory-tests`: the command dry-runs (autouse `TREE_MODAL_DRY_RUN=1`), exit 0, nothing reaches Modal; their argv assertion fails, pointing them at the `run` fixture.

### Story: Modal is unreachable when the operator deploys
1. `modal app list --json` exits 1 (no network).
2. One ERROR line `Refusing to deploy …: could not list Modal apps (exit 1), so an overwrite cannot be ruled out. Pass FORCE=yes to skip the check.`; exit code 3.

---

Blocked by: (none)

## Log

### [PA] 2026-09-20 11:20 — Grooming (new rollup task: name collision incident) — drafted, never applied; merged below

**Summary**
Our three Serving paths used the exact app name Modal gives a hand-made Dedicated Endpoint of the same model, and the driver wrote and stopped without looking. This task namespaces every name we create (`tree-`), makes `deploy` look first, makes `stop` refuse foreign names, and adds a dry run so nobody needs a real `modal` to check the driver.

**The incident, factually (2026-09-20)**
- During #142's QA two real `modal deploy` calls ran in the human's workspace: a `PATH` shim meant to fake `modal` was bypassed because `make` -> `uv run` puts `.venv/bin` first.
- `SERVING=sglang` for `Qwen/Qwen3-Embedding-0.6B` deployed to app `ep-qwen3-embedding-0-6b` — the app of a Dedicated Endpoint the human created by hand on 2026-08-24 (`modal app history`: v1/v2 August, client 1.5.1.dev13; v3 ours). It was overwritten silently. `modal app rollback` is not available on the plan; the human is restoring it in the dashboard.
- The voyage-4-nano deploy created `ep-voyage-4-nano` (no collision); it has been stopped and is left alone.
- No secret leaked (`HF_TOKEN` was unset). #141 as then written would have hit the same app deliberately: deploy Qwen3 as endpoint `qwen3-embedding-0-6b`, then STOP it.

**Key decisions**
- Prefix over detection: a `tree-` namespace makes the collision impossible by construction; the guard is the second line, not the first. Name parity with Modal's own endpoint names bought nothing — H1 needs the `ep-<N>` SHAPE, not the same `N`.
- Ownership = the prefix. No tags, no marker: least mechanism, and it is checkable offline. `FORCE` never overrides it.
- Guard kinds come from two read-only list calls; fails closed. Field names were read from the pinned client's SOURCE (modal 1.5.5), not from a live call — hence the SWE-must-verify.
- `stop` does no listing: the prefix is its guard, and a wrong `SERVING=` on stop fails harmlessly on Modal's side.
- Dry run is suite-wide by default; the `subprocess.run` spy fixture is the only exit from it.
- New module `tree/models/modal_cli.py` so `modal_catalog.py` keeps its "pure, no subprocess" contract.
- No open dependency: #138/#139/#140/#142 are all done (#142 landed as `702c08a` while this was groomed).

**Dependencies**
- None open.

**User stories**
- 7 stories: hand-made endpoint untouched, ladder refusal, own-app update, force, dry run, forgotten mock, Modal unreachable.

**Open questions**
- None blocking. (A configurable prefix was considered and rejected until a second project shares the workspace.)

Ready for implementation.

### [PA] 2026-09-20 13:40 — Re-grooming (third plan edit: auto-routing, LLMs, warm-up) — THIS is the version that ships

**What changed against the 11:20 draft**
- Same namespace, guard, ownership check, dry run and autouse fixture — nothing dropped.
- The guard is now specified on `target: endpoint | app` (`modal_cli.guard_deploy`) so #145's auto-router calls it unchanged when `serving` disappears; the driver-level 18-case test stays while the three paths still exist and is re-parametrised by #145.
- NEW C2: the `HF_TOKEN` hint fires only for gated-looking failures (`looks_gated`), found live today (it was printed under a 503, an architecture mismatch and a not-in-catalog refusal). This forces `modal endpoint create` to run with captured output — which #145's router needs anyway to read Modal's verdict.
- The hard safety sentence is now verbatim in the Scope. Blocks every later task of the rework.
- The two static-guard blind spots on the deploy scripts moved to #146 (the task that rewrites the script they guard).

**Dependencies**
- None open.

**User stories**
- 7 stories, unchanged.

Ready for implementation.

### [SWE] 2026-09-20 16:05 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — `MODAL_NAME_PREFIX = "tree"` + `MODAL_APP_NAME_MAX_LENGTH = 63`; `endpoint_name` = `tree-<slug>` (new `name_slug` property holds the raw derivation); empty-slug validator now reads the SLUG; new `_check_the_app_name_fits_modal` validator.
- `apps/memory/src/tree/models/modal_cli.py` — **NEW**. The only module that spawns `modal`: `run_modal` (the ONE `subprocess.run` door, honours the dry run, maps `FileNotFoundError`), `is_dry_run`, `assert_owned_name`, `existing_kind`, `guard_deploy`, `ModalGuardError`.
- `apps/memory/src/tree/models/modal_catalog.py` — new `redact_text` and `looks_gated` (+ `_GATED_MARKERS`); `modal_cli_command` docstring. Still imports no `subprocess`, no `modal`.
- `apps/memory/scripts/modal_embedding_model.py` — glue only: rails in order (resolve → argv → ownership → script check → guard → run), `--force` / `--dry-run`, exit-code map (2 config / 3 guard / modal's own), captured+redacted `endpoint create` output, `_hint_if_gated` replacing `_hint_if_no_token`.
- `apps/memory/Makefile` — `DRY_RUN=yes` on both targets, `FORCE=yes` on deploy only, `ep-tree-<model>` in the help text.
- `apps/memory/README.md` — new "1b." block (namespace, guard + `FORCE=yes`, `DRY_RUN=yes`, why a `PATH` shim fails), `ep-tree-voyage-4-nano` example, gated-only hint note.
- `apps/memory/src/tree/models/modal_server.py`, `deploy/modal_sglang_embedding.py`, `deploy/modal_vllm_embedding.py` — docstrings: the same SHAPE inside our `tree-` namespace, not the same name.
- `apps/memory/tests/unit/conftest.py` — autouse `TREE_MODAL_DRY_RUN=1` for the whole suite + the shared `run` fixture (patches `modal_cli.subprocess.run`, the ONE `delenv`).
- `apps/memory/tests/unit/models/test_modal_cli.py` — **NEW** (`TestAssertOwnedName`, `TestExistingKind`, `TestGuardDeploy`, `TestRunModal`).
- `apps/memory/tests/unit/scripts/test_modal_embedding_model_script.py` — `TestDeployGuard` (18 cells), `TestGuardFailsClosed`, `TestStopOwnership`, `TestDryRun`, `TestHfHint`, capture/redaction test, rail tests; `run.return_value` → `run.state.*`.
- `apps/memory/tests/unit/{config/test_app_config.py,models/test_modal_catalog.py,models/test_modal_embedding.py,models/test_modal_server.py,deploy/test_modal_embedding_scripts.py}` — prefixed names, `test_app_name_length_limit`, `test_empty_slug_is_still_rejected`, `TestLooksGated`, `TestRedactText`.

**Tests**
- Unit: 3497 passing, 0 failing, 0 skipped — `make memory-tests` (LOCAL env).
- Integration: N/A — this repo has no integration suite (AGENTS.md).

**Acceptance criteria** (`tests/unit/` paths; no `[HUMAN]` criteria in this task — the LIVE proof of the
prefixed names and of the two `list --json` key sets stays #141's)

- [x] prefixed derivation + `MODAL_NAME_PREFIX` — `config/test_app_config.py::TestModalCatalog::test_name_derivation` (4 cases: voyage, Qwen, bge-m3, `Org/My_Model..v2`)
- [x] 63-character limit / empty slug — `config/test_app_config.py::TestModalCatalog::test_app_name_length_limit` (55 loads, 56 raises "at most 63"), `::test_empty_slug_is_still_rejected`
- [x] prefixed argv + deploy spec — `models/test_modal_catalog.py::TestModalCliCommand::test_modal_cli_command_uses_prefixed_names`
- [x] ownership check — `models/test_modal_cli.py::TestAssertOwnedName` (`tree-x`, `ep-tree-x` pass; `qwen3-embedding-0-6b`, `ep-qwen3-embedding-0-6b`, `ep-treex`, `""` raise, x both actions)
- [x] `existing_kind` — `models/test_modal_cli.py::TestExistingKind` (8 tests: endpoint row, live app, stopped app, empty lists, un-prefixed rows ignored, both argvs `[..., "list", "--json"]` with `check=False`, exit 1, non-list JSON)
- [x] `guard_deploy` matrix, 12 cells — `models/test_modal_cli.py::TestGuardDeploy::test_the_matrix_without_force` + `::test_the_matrix_with_force_never_refuses` (2 targets x 3 kinds x force), plus the three message tests
- [x] driver matrix, 18 cells — `scripts/test_modal_embedding_model_script.py::TestDeployGuard::test_the_matrix_without_force` + `::test_the_matrix_with_force` (3 paths x 3 kinds x force); refusals exit 3, name the repo id / path / name, and `_acting_argvs(run) == []`
- [x] fail closed — `scripts/...::TestGuardFailsClosed::test_guard_failure_is_closed` (exit 3) + `::test_force_downgrades_it_to_a_warning`
- [x] stop ownership — `scripts/...::TestStopOwnership` (3 cases incl. `--force`, + `::test_a_fallback_deploy_checks_the_app_name`); `::TestStop::test_a_stop_lists_nothing`
- [x] dry run — `scripts/...::TestDryRun` (flag on deploy + stop, env var alone, redacted custom-weights argv); `models/test_modal_cli.py::TestRunModal`
- [x] suite-wide rail — `scripts/...::test_unit_suite_defaults_to_modal_dry_run` (`subprocess.run` patched to raise, exit 0) + `::test_the_rail_is_wired_once_in_the_unit_conftest` (one `setenv`, the ONE `delenv`)
- [x] `looks_gated` — `models/test_modal_catalog.py::TestLooksGated` (6 true, the 3 live texts + `""` false, case-insensitive)
- [x] the hint — `scripts/...::TestHfHint` (catalog refusal, `GatedRepoError`, failed fallback deploy, gated smoke test, 503, Proxy-token 401, token set)
- [x] capture + redaction — `scripts/...::test_endpoint_create_output_is_captured_and_redacted` (`capture_output`/`text`/`check`, `***`, and `modal deploy` NOT captured)
- [x] token rules on the new paths — `scripts/...::TestGuardLinesCarryNoToken` (refusal + FORCE warning), `::TestDryRun::test_a_dry_run_argv_is_redacted`, `::test_neither_the_driver_nor_the_cli_module_uses_check_true`
- [x] name greps / README greps / `make -n` — see Evidence and the survivor list in Notes
- [x] the three `DRY_RUN=yes` commands — see Evidence (all exit 0, no `modal` process)
- [x] both "SWE must verify" items — answered below with file:line + the curled doc URL
- [x] `models/test_modal_catalog.py::test_catalog_does_not_import_modal` green; `modal_catalog.py` imports no `subprocess` (only docstring mentions)
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green on LOCAL

**Evidence**

```
$ make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests
305 files already formatted
All checks passed!
ruff check...............................................................Passed
ruff format..............................................................Passed
prettier.................................................................Passed
biome check (harness)....................................................Passed
============================ 3497 passed in 45.48s =============================

$ make -n memory-deploy-embedding-model MODEL=x DRY_RUN=yes FORCE=yes
uv run python scripts/modal_embedding_model.py deploy --model "x"  --dry-run --force
$ make -n memory-deploy-embedding-model-stop MODEL=x FORCE=yes
uv run python scripts/modal_embedding_model.py stop --model "x"

$ make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B DRY_RUN=yes   # exit 0
Dedicated endpoint: Modal picks the GPU, engine and flags — this entry's gpu/cpu/memory_mb/max_model_len/extra_server_args are used only by SERVING=sglang|vllm.
DRY RUN — would run: modal endpoint create --name tree-qwen3-embedding-0-6b --model Qwen/Qwen3-Embedding-0.6B --routing-region eu-west
DRY RUN — skipped the Modal existence check (no modal process is started).

$ make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano SERVING=vllm DRY_RUN=yes   # exit 0
DRY RUN — would run: modal deploy deploy/modal_vllm_embedding.py
DRY RUN — skipped the Modal existence check (no modal process is started).

$ make memory-deploy-embedding-model-stop MODEL=Qwen/Qwen3-Embedding-0.6B DRY_RUN=yes   # exit 0
DRY RUN — would run: modal endpoint stop -y tree-qwen3-embedding-0-6b
DRY RUN — skipped the Modal existence check (no modal process is started).
```

**NO `modal` process was started at any point.** The three `DRY_RUN=yes` commands were run only after
`TestDryRun` proved `run_modal` returns BEFORE `subprocess.run` (`run.assert_not_called()`), and
`test_unit_suite_defaults_to_modal_dry_run` proves the same with `subprocess.run` patched to raise. No
`modal …` / `uv run modal …` command was issued — every CLI fact below comes from READING the pinned client.

**SWE must verify #1 — is `modal endpoint create --name` stricter than 63?**
No. `create` does not validate the name at all: it assigns `req.name = name` and sends it
(`apps/memory/.venv/lib/python3.14/site-packages/modal/cli/endpoint.py:255`), never calling
`check_object_name`. The only rule in the client is `name_utils.is_valid_object_name`
(`modal/_utils/name_utils.py:19-27`: `len(name) <= 64`, charset `^[a-zA-Z0-9-_.]+$`, not an `ap-…` id) whose
error text says "shorter than 64 characters" (`:64`). `https://modal.com/docs/cli/latest/endpoint.md`
(curled 2026-09-20) documents `--name TEXT` with NO limit. **63 stands**; the constant is unchanged.
One related find, harmless for us: `endpoint stop`/`create` treat an identifier matching `^ep-[a-zA-Z0-9]{22}$`
as an endpoint ID (`cli/endpoint.py:34`) — `ep-tree-…` contains `-`, so it can never collide.

**SWE must verify #2 — the two `--json` key sets on modal 1.5.5** (installed: `modal-1.5.5.dist-info`).
Keys are the snake_cased column titles (`modal/cli/utils.py:132` `_col_name_to_json_key`, used at `:156`).
- `modal endpoint list --json` (`cli/endpoint.py:296-311`): **`name`, `endpoint_id`, `status`, `created_at`, `created_by`** — and only `active_items`, i.e. rows whose `app_state` is not `APP_STATE_STOPPING`/`APP_STATE_STOPPED` (`:45-46`, `:305`). Hence the app list is what catches a stopped-or-other name.
- `modal app list --json` (`cli/app.py:103-131`): **`app_id`, `description`, `state`, `tasks`, `created_at`, `stopped_at`**. The `state` strings come from `APP_STATE_TO_MESSAGE` (`cli/app.py:41-50`): `deployed`, `ephemeral (detached)`, `disabled`, `ephemeral`, `initializing...`, `stopped`, `stopping...` — so `_DEAD_APP_STATES = {"stopped", "stopping..."}` and everything else counts as live.
Both key sets are pinned by `test_the_guard_rows_match_the_pinned_clients_columns`. LIVE confirmation stays #141 step 4.0.

**Notes**
- Three spec signatures gained a parameter, because the spec's own message text needs it — no behaviour change,
  no architectural fork: `assert_owned_name(name, action)` (the message names `<deploy|stop>`);
  `guard_deploy(entry, target, force, *, path)` (`path` is a plain `str`, only quoted back to the operator —
  #145's router passes its own route name unchanged); `ModalGuardError(message, *, reason)` so `FORCE=yes` can
  warn with the cause without echoing a refusal that did not happen.
- The hint rule implemented for `test` is the type split the spec called "simplest correct": a hint only for
  `ExtractionError` (server-side), never for a bare `ModelError` (configuration) — the Proxy-token message
  contains `401`, so a text-only rule would fire on it. For `deploy` the rule is empty token AND
  `looks_gated(<captured output>)`; a failed `modal deploy` is not captured and so never hints.
- `existing_kind` short-circuits: it returns `endpoint` without the app-list call when the endpoint row is
  there (the spec's "else"). Every outcome is identical — a live endpoint REFUSES both targets — and it saves
  one CLI round trip. Both calls are asserted in the `none` case.
- `_GATED_MARKERS` keeps `gatedrepoerror` even though `gated` subsumes it: the tuple is read as the
  documentation of what we recognise.
- The captured `endpoint create` output is logged (redacted) on SUCCESS too — capturing it otherwise swallows
  the endpoint URL Modal prints.
- `test_a_failing_modal_command_propagates_its_exit_code` now uses exit 7, not 3: 3 became OUR guard refusal.
- Known, accepted risk (noted, not worked around): `modal … list --json` prints through `rich`'s
  `print_json`, so a pathological console width could in principle wrap a line and break the parse. It fails
  CLOSED (exit 3, "invalid JSON") rather than silently, and #141 confirms the real output live.
- Grep survivors of `ep-(voyage-4-nano|qwen3-embedding-0-6b)` are all deliberate, and all are the OPERATOR's
  foreign name used as a negative case or as narrative: `src/tree/config/app_config.py:665` (the incident in
  the `MODAL_NAME_PREFIX` docstring), `tests/unit/models/test_modal_cli.py:90-91,137,140-141` and
  `tests/unit/scripts/test_modal_embedding_model_script.py:272,361,364-365,469,485,501` (the un-prefixed names
  that must be refused / ignored). `configs/default.yaml` and the frozen fixture still have no hit — names are
  derived.
- Not pre-empted, per the execution order: `serving` / `base_model` / `ServingPath` are untouched (#145),
  `wait_until_healthy` / `_init_lock` untouched (#144), `.agents/skills/run-pipelines-e2e/SKILL.md` untouched
  (#141).

### [Tester] 2026-09-20 18:10 — QA

**Safety-rail pre-check (before any command was allowed)**
Read `src/tree/models/modal_cli.py` in full and grepped the whole tree for
`subprocess`, `os.system`, `os.exec`, `Popen`, `shutil.which("modal")`:
`modal_cli.py:141` (`subprocess.run` inside `run_modal`) is the ONLY spawn
point for `modal` in `src/` + `scripts/`; every other `subprocess.run` hit
(`test_get_model.py`, `test_clustering_dependencies.py`, etc.) launches
`sys.executable -c ...`, unrelated to `modal`. Confirmed `run_modal`'s
`is_dry_run(dry_run)` check (`modal_cli.py:135`) returns `None` BEFORE the
`subprocess.run` call. Confirmed in `scripts/modal_embedding_model.py:173-174`
that `guard_deploy` (which drives `existing_kind`'s two `list --json` calls)
only runs `if action == "deploy" and not is_dry_run(dry_run)` — skipped
under dry run. All three conditions (a)(b)(c) held, so the `DRY_RUN=yes`
Make invocations and the mocked/read-only experiments below were in scope.
No `modal …` / `uv run modal …` command, no `scripts/modal_embedding_model.py`
as a process, and no `make memory-deploy-embedding-model-test` were run at
any point.

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check` -> `All checks passed!`; `make pre-commit` -> ruff/format/prettier/biome all `Passed`)
- Unit tests: 3497 passed / 0 failed (`make memory-tests`, LOCAL env, `make env-status` confirmed `local`)
- Integration tests: N/A — no integration suite in this repo (AGENTS.md)
- Warnings: 0 (pytest summary line reports 0 warnings)

**E2E adversarial pass**
- Happy path: `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B DRY_RUN=yes` -> logs `DRY RUN — would run: modal endpoint create --name tree-qwen3-embedding-0-6b --model Qwen/Qwen3-Embedding-0.6B --routing-region eu-west` + the skipped-check line, exit 0 (PASS)
- Break path 1 (state edge — rail bypassability): scratch pytest file (`tests/unit/scripts/test_zzz_qa_scratch_rail.py`, written to the real tree, run via `uv run pytest`, then deleted — never committed) patched `subprocess.run` to RAISE and drove `deploy`/`stop` for both seeds (`Qwen/Qwen3-Embedding-0.6B`, `voyageai/voyage-4-nano`) x every `SERVING` (`""`, `endpoint`, `sglang`, `vllm`) x force/no-force = 24 combinations, plus a stop -> ALL exit 0, subprocess.run never reached (PASS). Then, with `TREE_MODAL_DRY_RUN` explicitly `monkeypatch.delenv`'d and no `run` fixture, the same drive DID reach the patched-to-raise `subprocess.run` (`result.exception` was the `AssertionError`) — proving the rail, not luck, is what protects (PASS). Also confirmed `is_dry_run()`: `""`/`"0"` -> `False` (live), any other string (`"false"`, `"False"`, `"no"`) -> `True` (dry run) — an unset/explicit-off value is the only way to go live, and unrecognized values fail SAFE toward dry-run. Confirmed `_apply_env_overrides` does not choke on the section-less `TREE_MODAL_DRY_RUN` var (it is read directly via `os.environ.get` in `modal_cli.py`, never through the YAML escape hatch) — `load_app_config()` still succeeds with it set.
- Break path 2 (ownership escape / collision): crafted `repo_id`s (`org/Tree-Something`, `org/treeXfoo`, `org/xtree-foo`, `org/qwen3-embedding-0-6b`, a 100-char slug) via direct `ModalEmbeddingModelConfig` construction — none can escape the `tree-`/`ep-tree-` prefix (it is prepended in code, never derived from `repo_id`) and `org/qwen3-embedding-0-6b` derives `tree-qwen3-embedding-0-6b`, which does NOT collide with the foreign, unprefixed `qwen3-embedding-0-6b` (PASS). 63-char boundary verified directly: slug 54/55 chars load (`app_name` len 62/63), 56/63/64 raise `ValidationError` containing "at most 63" (PASS, matches AC exactly).
- Break path 3 (guard matrix robustness against malformed list output): direct calls to `existing_kind` with extra unknown JSON keys on an app row (tolerated, matched by `description`/`state`), a MISSING `state` key (`.get` returns `None`, not in `_DEAD_APP_STATES`, counts as live — fails toward REFUSE, the safe direction), `state: "stopping..."` (-> `none`, correct), empty stdout (fails CLOSED, "invalid JSON"), and pretty/indented JSON (`json.dumps(..., indent=2)`, still parses fine since `json.loads` tolerates whitespace) — all behaved as specified, none produced an incorrect "none" verdict (PASS).
- Break path 4 (redaction on captured output): `redact_text` directly exercised with the fake token on its own line, inside a URL, repeated twice, and a "split across two lines" case (confirmed harmless because redaction runs on the FULL concatenated `stdout+stderr` string BEFORE `.splitlines()` in `_log_modal_output`, so a real un-split token is always redacted) — token never survives, empty token is a correct no-op (PASS).
- Break path 5 (`looks_gated` false-positive risk): independently verified in Python that none of the three live refusal/health texts ("... is not available for dedicated Endpoints.", "... is not a servable checkpoint of base model ...", "Health check ... answered 503.") match any of the 6 gated markers — in particular confirmed "dedicated" does NOT contain "gated" as a substring (no `g` in "dedicated" at all), so there is no accidental false-positive path (PASS).

**Acceptance criteria**
- [x] PASS — `test_name_derivation` prefixed names + `MODAL_NAME_PREFIX` — `tests/unit/config/test_app_config.py::TestModalCatalog::test_name_derivation` (in `make memory-tests` run)
- [x] PASS — 63-char limit / empty slug — `::test_app_name_length_limit`, `::test_empty_slug_is_still_rejected`; independently re-derived the 55/56/62/63/64 boundary via direct `ModalEmbeddingModelConfig` construction, exact match
- [x] PASS — prefixed argv + deploy spec — `tests/unit/models/test_modal_catalog.py::TestModalCliCommand::test_modal_cli_command_uses_prefixed_names`
- [x] PASS — ownership check — `tests/unit/models/test_modal_cli.py::TestAssertOwnedName` (10 cases); independently re-verified `qwen3-embedding-0-6b` / `ep-qwen3-embedding-0-6b` / `ep-treex` / `""` all raise
- [x] PASS — `existing_kind` — `test_modal_cli.py::TestExistingKind` (9 tests incl. dry-run-as-closed-door); independently re-verified extra/missing keys, `stopping...`, empty stdout, pretty JSON
- [x] PASS — `guard_deploy` matrix, 12 cells — `test_modal_cli.py::TestGuardDeploy` (both matrix tests + 3 message tests)
- [x] PASS — driver matrix, 18 cells — `tests/unit/scripts/test_modal_embedding_model_script.py::TestDeployGuard` (both matrix tests, `_acting_argvs(run) == []` on refusal)
- [x] PASS — fail closed — `TestGuardFailsClosed` (both tests); confirmed exit 3 without force / warning+run with force live via `make memory-tests`
- [x] PASS — stop ownership — `TestStopOwnership` (3 cases) + `TestStop::test_a_stop_lists_nothing`
- [x] PASS — dry run — `TestDryRun` (3 tests) + `test_modal_cli.py::TestRunModal`; the three real `DRY_RUN=yes` Make invocations run for real (see Evidence), all exit 0, no `modal` process started
- [x] PASS — suite-wide rail — `test_unit_suite_defaults_to_modal_dry_run` + `test_the_rail_is_wired_once_in_the_unit_conftest`; independently reproduced with a 24-case scratch drive (see E2E break path 1) and confirmed the rail — not luck — is what protects
- [x] PASS — `looks_gated` — `tests/unit/models/test_modal_catalog.py::TestLooksGated`; independently re-verified the 3 live texts are all `False`
- [x] PASS — the hint — `TestHfHint` (7 tests, catalog refusal / GatedRepoError / failed fallback / gated smoke test / 503 / Proxy-401 / token-set)
- [x] PASS — capture + redaction — `test_endpoint_create_output_is_captured_and_redacted`; independently re-verified `redact_text` on token-on-own-line / in-URL / repeated / split-across-lines
- [x] PASS — token rules on the new paths — `TestGuardLinesCarryNoToken`, `TestDryRun::test_a_dry_run_argv_is_redacted`, `test_neither_the_driver_nor_the_cli_module_uses_check_true`; secret-shaped scan on the full diff (`hf_[A-Za-z0-9]{20,}` etc.) -> 0 matches
- [x] PASS — name greps / README greps / `make -n` — `grep -rnE "ep-(voyage-4-nano|qwen3-embedding-0-6b)|..."` survivors are exactly the SWE's disclosed list (deliberate negatives in `app_config.py:665` docstring + `test_modal_cli.py` + `test_modal_embedding_model_script.py`); `grep -c "ep-tree-" README.md` = 2, `DRY_RUN=yes` = 2, `FORCE=yes` = 1, `PATH` inside the Modal section = 1; `make -n` outputs reproduced byte-for-byte against the SWE's Evidence
- [x] PASS — the three `DRY_RUN=yes` commands run for real — reproduced independently (see Evidence), all exit 0, output matches the SWE's Log verbatim
- [x] PASS — both "SWE must verify" items — independently re-read `modal/_utils/name_utils.py:19-27,60-66` (64-char limit, "shorter than 64" text) and `modal/cli/endpoint.py:245-255` (`create` never calls `check_object_name`, sends `name` unvalidated) confirming item 1; re-read `modal/cli/endpoint.py:290-311` and `modal/cli/app.py:95-131` + `APP_STATE_TO_MESSAGE` (`:41-49`) and `modal/cli/utils.py:132` (`_col_name_to_json_key`) confirming item 2's exact key sets and dead-state strings — both match the SWE's Log claims verbatim, on the installed `modal-1.5.5.dist-info`
- [x] PASS — `test_catalog_does_not_import_modal` green; `modal_catalog.py` imports no `subprocess`/`modal` (grepped imports directly: `math`, `typing`, `pydantic`, `tree.config.app_config`, `tree.config.settings`, `tree.models.base`, `tree.models.exceptions` only)
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green on LOCAL (`make env-status` -> `local`)

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-format-check && make memory-lint-check
305 files already formatted
All checks passed!

$ make pre-commit
ruff check...............................................................Passed
ruff format..............................................................Passed
prettier.................................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests
============================ 3497 passed in 45.45s =============================

$ make -n memory-deploy-embedding-model MODEL=x DRY_RUN=yes FORCE=yes
uv run python scripts/modal_embedding_model.py deploy --model "x"  --dry-run --force
$ make -n memory-deploy-embedding-model-stop MODEL=x FORCE=yes
uv run python scripts/modal_embedding_model.py stop --model "x"

$ make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B DRY_RUN=yes   # exit 0
Dedicated endpoint: Modal picks the GPU, engine and flags — this entry's gpu/cpu/memory_mb/max_model_len/extra_server_args are used only by SERVING=sglang|vllm.
DRY RUN — would run: modal endpoint create --name tree-qwen3-embedding-0-6b --model Qwen/Qwen3-Embedding-0.6B --routing-region eu-west
DRY RUN — skipped the Modal existence check (no modal process is started).

$ make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano SERVING=vllm DRY_RUN=yes   # exit 0
DRY RUN — would run: modal deploy deploy/modal_vllm_embedding.py
DRY RUN — skipped the Modal existence check (no modal process is started).

$ make memory-deploy-embedding-model-stop MODEL=Qwen/Qwen3-Embedding-0.6B DRY_RUN=yes   # exit 0
DRY RUN — would run: modal endpoint stop -y tree-qwen3-embedding-0-6b
DRY RUN — skipped the Modal existence check (no modal process is started).
```
No `modal` process was started at any point during this QA pass.

**Commands run, verbatim (for the record — nothing else touched Modal)**
`make env-status`; `git status`; `git diff --stat`; `git diff`; `grep -rn "subprocess\|os\.system\|os\.exec\|Popen\|shutil.which" apps/memory/src apps/memory/scripts apps/memory/deploy`; `make memory-format-check`; `make memory-lint-check`; `make pre-commit`; `make memory-tests`; `make -n memory-deploy-embedding-model MODEL=x DRY_RUN=yes FORCE=yes`; `make -n memory-deploy-embedding-model-stop MODEL=x FORCE=yes`; `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B DRY_RUN=yes`; `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano SERVING=vllm DRY_RUN=yes`; `make memory-deploy-embedding-model-stop MODEL=Qwen/Qwen3-Embedding-0.6B DRY_RUN=yes`; `uv run pytest tests/unit/models/test_modal_cli.py -q` (from `apps/memory`, sanity check, no `.env`-only deps); `uv run pytest tests/unit/scripts/test_zzz_qa_scratch_rail.py -q -s` (from `apps/memory`, a scratch file I wrote and then `rm`'d — never committed, never part of the SWE's diff); assorted read-only `uv run python -c "..."` one-liners exercising `ModalEmbeddingModelConfig`, `assert_owned_name`, `existing_kind`, `redact_text`, `looks_gated`, `is_dry_run`, `_apply_env_overrides` — all pure-Python/mocked, none touching `modal`, `subprocess.run` (real), or any file under `.env*`.

**Other issues found**
- None. The `code-review` plugin's `/code-review` command is PR-shaped (`gh pr diff`/`gh pr comment` on a live GitHub PR); this work is uncommitted and not yet pushed, so the command has no PR to operate on. I performed its five review angles manually instead (CLAUDE.md/AGENTS.md compliance, a bug-focused read of the diff, `git log`/incident-history context, code-comment compliance) and found nothing beyond what is already covered above. The plugin proper will run in `/squid-review` once this is committed and pushed, per the workflow this repo documents.
- Minor (not a blocker): the `print_json` line-wrap risk the SWE flagged in their Log is real in principle (an extremely narrow terminal width could in theory wrap a `rich`-rendered JSON line) but fails CLOSED (exit 3, "invalid JSON") as claimed — confirmed by direct testing of malformed/empty/non-list JSON inputs above. No action needed; #141's live run is the real-world check.

**VERDICT: PASS**
