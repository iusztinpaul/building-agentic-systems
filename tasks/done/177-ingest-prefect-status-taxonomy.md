---
id: 177-ingest-prefect-status-taxonomy
status: done
feature: horizon-mcp-fixes
---

# Ingest tools name the Prefect failure: 401/403 → `configuration_error` (rotate `PREFECT_API_KEY`), 429/5xx → `pipeline_unavailable` with the status, unreachable stays "unreachable"

Tags: `mcp`, `prefect`, `errors`, `docs`
Depends on: —
Blocks: —
Implements: ADR-013 §4 (amends ADR-008 §2's dispatch-boundary mapping)

## Problem

`_ingest` (`apps/memory/src/tree/mcp/tools.py` ~L550) catches `(httpx.ConnectError, httpx.TimeoutException,
PrefectHTTPStatusError)` as one case → `pipeline_unavailable`, retryable, "Prefect API unreachable — try
again". Live on Horizon a stale `PREFECT_API_KEY` produced HTTP 401 and the tool told the model to retry.
ADR-008 §2 already defines `configuration_error` (not retryable) for "an operator has to act".

## Scope

**Human decisions (final):** 401/403 → `configuration_error`, `retryable=false`, message names
`PREFECT_API_KEY` (and the `PREFECT_API_URL` workspace) and says to rotate + redeploy; 404 stays on the
`ObjectNotFound` path; 429/5xx → `pipeline_unavailable` retryable with the status in the message;
`ConnectError`/timeout → `pipeline_unavailable` retryable "unreachable"; the log line carries the status.

1. **`tools.py`:** replace the three-way `except` with `except ObjectNotFound` (unchanged) +
   `except PrefectHTTPStatusError as exc` → `_prefect_status_error(exc)` + `except (httpx.ConnectError,
   httpx.TimeoutException)` (unchanged message). `_prefect_status_error(exc) -> str` reads
   `exc.response.status_code` and answers:
   - 401 / 403 → `configuration_error`, `retryable=False`: `"Prefect API rejected the request (HTTP
     {status}): PREFECT_API_KEY is invalid or expired for the workspace at PREFECT_API_URL — rotate the key
     in Prefect Cloud, update this server's environment (Horizon: Settings → Environment, then redeploy)
     and retry."`
   - 404 → `configuration_error`, `retryable=False`, the SAME sentence `ObjectNotFound` answers (lift it to
     a module constant `DEPLOYMENT_MISSING_MESSAGE` so both paths share it).
   - 429 or ≥ 500 → `pipeline_unavailable`, `retryable=True`: `"Prefect API answered HTTP {status} — try
     again"`.
   - any other 4xx → `internal_error`, `retryable=False`: `"Prefect API answered HTTP {status} while
     dispatching the ingest: {exc}"` (a bug until the traceback is read).
   Every branch logs with `logger.exception("Prefect API answered HTTP %d while dispatching the ingest",
   status)` (traceback kept). Reuse `_http_retryable` for the 429/5xx test.
2. **Docstrings:** `_ingest`'s "catches exactly two Prefect failures" paragraph becomes the four-way table
   above; `ingest_url` / `ingest_file` / `ingest_conversation` keep `ERROR_CONTRACT` verbatim and the
   `ingest_url` paragraph gains "an invalid `PREFECT_API_KEY` is `configuration_error` (not retryable)".
3. **Docs:** `tutorials/4_x_x_deploying_mcp_server.md` "Environment Variables" bad example — the last clause
   becomes "…every ingest fails with a Prefect `401 Unauthorized`, and the tool answers
   `configuration_error` naming `PREFECT_API_KEY`"; `docs/glossary.md` **Tool error envelope** row (drafted
   in the grooming round, Part 2); ADR-008 §2 Status-line note (ADR-013 "Amendments").
4. **Tests** (`tests/unit/mcp/test_tools.py` + the table in `test_error_envelope.py`): build
   `PrefectHTTPStatusError.from_httpx_error(httpx.HTTPStatusError(..., request=httpx.Request("POST", url),
   response=httpx.Response(status)))` for 401, 403, 404, 422, 429, 500, 503; parametrise over `_ingest` with
   `dispatch_online_pipeline` patched; assert `error_type`, `retryable`, and that 401's message contains
   `PREFECT_API_KEY` and `PREFECT_API_URL`; one test per ingest tool for the 401 path; `ConnectError` and
   `ReadTimeout` still answer `pipeline_unavailable` "unreachable"; `ObjectNotFound` unchanged; caplog
   shows the status code.
5. **Live verification** (LOCAL env): serve the MCP server against Prefect Cloud with a bogus key —
   `PREFECT_API_URL=<the .env.prod workspace url> PREFECT_API_KEY=pnu_bogus make memory-serve-mcp
   TRANSPORT=streamable-http` — then `uv run fastmcp call http://127.0.0.1:8000/mcp --auth none ingest_url
   url=https://example.com/` → the `configuration_error` envelope; restore the local env and show the
   happy path still answers a receipt. Paste both answers into the Log.

## Acceptance criteria

- [x] A `PrefectHTTPStatusError` with 401 or 403 answers `{"error_type": "configuration_error", "retryable":
      false}` whose message contains `PREFECT_API_KEY`, `PREFECT_API_URL` and "redeploy".
- [x] 404 (both as `ObjectNotFound` and as a 404 `PrefectHTTPStatusError`) answers the one
      `DEPLOYMENT_MISSING_MESSAGE` as `configuration_error`, not retryable.
- [x] 429 and every 5xx answer `pipeline_unavailable`, retryable, message containing `HTTP <status>`.
- [x] Any other 4xx answers `internal_error`, not retryable, message containing `HTTP <status>`.
- [x] `httpx.ConnectError` / `httpx.TimeoutException` still answer `pipeline_unavailable` "Prefect API
      unreachable — try again".
- [x] Every branch logs the status code with the traceback.
- [x] `ERROR_CONTRACT` verbatim in the three ingest docstrings; `test_error_envelope.py` table extended and
      green; tutorial sentence and glossary row updated.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [x] Live (local server, bogus cloud key): `ingest_url` answers `configuration_error`; evidence in the Log.

## User Stories

### Story: The Horizon key expired and the model says so
1. The operator rotated `PREFECT_API_KEY` in Prefect Cloud but not in Horizon.
2. The user asks the assistant to "save this page", the model calls `ingest_url(url=…)` on `tree-memory`.
3. The answer is `{"error_type": "configuration_error", "retryable": false, "message": "Prefect API rejected the request (HTTP 401): PREFECT_API_KEY is invalid or expired … redeploy …"}`.
4. The model tells the user the server's Prefect key must be rotated and does NOT retry.

### Story: Prefect Cloud has a bad minute
1. `run_deployment` answers HTTP 503.
2. The tool answers `{"error_type": "pipeline_unavailable", "retryable": true, "message": "Prefect API answered HTTP 503 — try again"}`; the server log reads `Prefect API answered HTTP 503 while dispatching the ingest` with the traceback.
3. The model retries once later and gets a receipt.

### Story: The deployment was never served (local)
1. The operator runs the local MCP server without `make memory-serve-workflows`.
2. `ingest_file(...)` answers `configuration_error` with the existing "deployment is not registered — run `make memory-serve-workflows`" sentence (both for `ObjectNotFound` and for a raw 404).

### Story: The operator verifies the fix locally
1. `PREFECT_API_URL=<cloud workspace> PREFECT_API_KEY=pnu_bogus make memory-serve-mcp TRANSPORT=streamable-http`.
2. `uv run fastmcp call http://127.0.0.1:8000/mcp --auth none ingest_url url=https://example.com/` prints the `configuration_error` envelope naming `PREFECT_API_KEY`.

---

Blocked by: (none)

## Log

### [PA] 2026-10-03 17:45 — Grooming

**Summary**
The ingest dispatch boundary maps Prefect's HTTP status to the envelope the model can act on: auth
failures are configuration (stop, rotate the key), 429/5xx are transient (retry, status named), other 4xx
are bugs, unreachable stays unreachable.

**Key decisions**
- One helper `_prefect_status_error` beside `storage_error` / `internal_error` — same shape as the
  existing boundary helpers, no new module.
- Other 4xx → `internal_error` (not retryable): the spec did not list them and a 422 on dispatch is a bug.
- The 404 message is shared with the `ObjectNotFound` path via one constant so the two cannot drift.

**Dependencies**
- None.

**User stories**
- 4 stories: expired key on Horizon, a 503, an unserved local deployment, the operator's local check.

Ready for implementation.

### [SWE] 2026-10-03 19:33 — Implementation

**Files modified**
- `apps/memory/src/tree/mcp/tools.py`: new `DEPLOYMENT_MISSING_MESSAGE` constant, shared by the `ObjectNotFound` path and the raw-404 path. New `_prefect_status_error(exc) -> str` placed next to `storage_error` / `internal_error`. It logs one `logger.exception("Prefect API answered HTTP %d while dispatching the ingest", status)` and then maps the status: 401/403 → `configuration_error`, 404 → `configuration_error`, 429/5xx (via `_http_retryable`) → `pipeline_unavailable`, any other 4xx → `internal_error`. `_ingest` now has separate `except` clauses for `ObjectNotFound`, `PrefectHTTPStatusError` and `ConnectError`/`TimeoutException`; the unreachable message is unchanged. The `_ingest` docstring now carries the four-way table. The `ingest_url` docstring gains "an invalid `PREFECT_API_KEY` is `configuration_error` (not retryable)", with the `ERROR_CONTRACT` sentence left as it was. I also corrected the stale inline comment in `ingest_url`.
- `apps/memory/tests/unit/mcp/test_tools.py`: new `_prefect_http_error(status)` fixture (named apart from the production helper) built with `PrefectHTTPStatusError.from_httpx_error(httpx.HTTPStatusError(...))`. `TestDispatchErrors` gains:
  - the status table (401/403/404/422/429/500/502/503 through `_ingest`), asserting `error_type`, `retryable`, `HTTP <status>` or `DEPLOYMENT_MISSING_MESSAGE`, plus a caplog check of the exact status line with `exc_info`;
  - the exact 401/403 message;
  - the exact 429/503 messages;
  - one 401 test for each ingest tool.

  The old 502 "unreachable" case moved into the status table. `ReadTimeout` was added to the unreachable cases. The `ObjectNotFound` test now asserts `== DEPLOYMENT_MISSING_MESSAGE`.
- `apps/memory/tests/unit/mcp/test_error_envelope.py`: cross-tool table `test_the_ingest_dispatch_boundary_maps_prefect_failures` with 10 rows: 401/403/404, `ObjectNotFound`, 422, 429/500/503, `ConnectError`, `ReadTimeout`.
- `tutorials/4_x_x_deploying_mcp_server.md`: the "Environment Variables" bad example now ends "…the tool answers `configuration_error` naming `PREFECT_API_KEY`".
- `docs/glossary.md`: in the **Tool error envelope** row, the clause "an unreachable Prefect API is `pipeline_unavailable`" is replaced by the four-way dispatch mapping (ADR-013 §4). The Part 2 draft was not in the repo, so I wrote this wording myself.
- `docs/adrs/008_mcp_tool_contract.md`: Status-line note from ADR-013 "Amendments", **§2 clauses only**: task 176's `search_web`/cooldown clause plus this task's dispatch-boundary clause. Task 176 shipped without its clause, so I added it here. The §3 `min_text_score` clause is left for task 183, whose Scope owns it.

**Tests**
- Unit: 4789 passed, 0 failed (`make memory-tests`, env-status local).
- Integration: N/A (no integration suite in this repo).
- Red first: before the change, the envelope table had 4 failures (401/403/404 answered `pipeline_unavailable` where `configuration_error` was expected; 422 answered `pipeline_unavailable` where `internal_error` was expected).

**Acceptance criteria**
- [x] 401/403 → `configuration_error`, not retryable, message contains `PREFECT_API_KEY`, `PREFECT_API_URL` and "redeploy". Tests: `test_tools.py::TestDispatchErrors::test_a_rejected_key_names_the_variables_and_redeploy`, `::test_every_ingest_tool_reports_a_rejected_key_as_configuration`.
- [x] 404 → `DEPLOYMENT_MISSING_MESSAGE` as `configuration_error`, both as `ObjectNotFound` and as a raw 404. Tests: `::test_prefect_status_maps_to_what_the_model_can_act_on[404]`, `::test_unregistered_deployment_is_a_configuration_error`, envelope table `[404]` / `[object-not-found]`.
- [x] 429/5xx → `pipeline_unavailable`, retryable, `HTTP <status>`. Tests: status table `[429|500|502|503]`, `::test_a_transient_status_is_named_in_the_retry_message`.
- [x] Other 4xx → `internal_error`, not retryable, `HTTP <status>`. Test: status table `[422]`.
- [x] `ConnectError` / `TimeoutException` / `ReadTimeout` → "Prefect API unreachable — try again". Test: `::test_unreachable_prefect_is_pipeline_unavailable`.
- [x] Every status branch logs the status with the traceback. Test: the status table's caplog assertion (exact message, `exc_info` set).
- [x] `ERROR_CONTRACT` verbatim (existing anchor test green); envelope table extended; tutorial sentence and glossary row updated.
- [x] format-check, lint-check, pre-commit, memory-tests all green.
- [x] Live check with a bogus cloud key: see Evidence.

**Evidence**
```
$ make env-status
Env target: local (.env)
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
329 files already formatted
All checks passed!
$ make pre-commit
prettier....Passed  ruff check....Passed  ruff format....Passed  biome check (harness)....Passed
$ make memory-tests
============================ 4789 passed in 57.70s =============================

# Live 1: local server, Prefect Cloud workspace URL from .env.prod, bogus key
$ make memory-serve-mcp PREFECT_API_URL=<.env.prod PREFECT_API_URL> PREFECT_API_KEY=pnu_bogus \
    TRANSPORT=streamable-http FASTMCP_PORT=8765
$ uv run fastmcp call http://127.0.0.1:8765/mcp --auth none ingest_url 'url=https://example.com/?tree-task-177'
{
  "result": "{\"error_type\":\"configuration_error\",\"retryable\":false,\"message\":\"Prefect API rejected the request (HTTP 401): PREFECT_API_KEY is invalid or expired for the workspace at PREFECT_API_URL — rotate the key in Prefect Cloud, update this server's environment (Horizon: Settings → Environment, then redeploy) and retry.\"}"
}
# server log:
Prefect API answered HTTP 401 while dispatching the ingest
Traceback (most recent call last): ...
prefect.exceptions.PrefectHTTPStatusError: Client error '401 Unauthorized' for url 'https://api.prefect.cloud/api/accounts/<acct>/workspaces/<ws>/deployments/name/online-pipeline/online-pipeline'

# Live 2: restored local env (.env, local Prefect, online-pipeline deployment served)
$ make memory-serve-mcp TRANSPORT=streamable-http FASTMCP_PORT=8765
$ uv run fastmcp call http://127.0.0.1:8765/mcp --auth none ingest_url 'url=https://example.com/'
{
  "result": "{\"source_uri\": \"https://example.com/\", \"duplicate\": false, \"document_id\": null, \"flow_run_id\": \"346edf0e-1876-4038-901f-da9d7dcd0792\", \"status\": \"scheduled\", \"url\": \"https://example.com/\"}"
}
```

**Notes**
- **The Scope §5 / Story 4 command does not work as written.** The root `Makefile` does `include .env` + `export`, and variables from an included file override the process environment. So with the env-prefix form (`PREFECT_API_URL=… PREFECT_API_KEY=pnu_bogus make …`), the local `.env` values win and the server quietly dispatches to local Prefect. Pass the overrides as make arguments instead (`make memory-serve-mcp PREFECT_API_URL=… PREFECT_API_KEY=pnu_bogus …`). The log above confirms the override took effect: the traceback URL is `api.prefect.cloud`.
- I used `FASTMCP_PORT=8765` because port 8000 is held by another local Docker container (`kitaru-local-server`).
- Live 1 used a unique URL (`?tree-task-177`). The dispatcher's pre-flight duplicate lookup runs before Prefect, so a URL already in memory would answer `duplicate` and never reach the 401 path.
- Live 2 left one scheduled `online-pipeline` run for `https://example.com/` in local Prefect/Mongo.
- Deliberate choice: the `ObjectNotFound` path keeps its existing log line ("online-pipeline deployment is not registered") because it has no HTTP status object of its own. The raw-404 path logs the status line.

### [Tester] 2026-10-03 20:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (329 files formatted, ruff clean, prettier/ruff/biome hooks Passed)
- Unit tests: 4789 passed / 0 failed (`make memory-tests`, env-status local)
- Integration tests: N/A (no integration suite in this repo)
- Warnings: 0 new

**E2E adversarial pass** (real MCP server + real Prefect client, served with `make memory-serve-mcp PREFECT_API_URL=http://127.0.0.1:9101/api PREFECT_API_KEY=pnu_bogus TRANSPORT=streamable-http FASTMCP_PORT=8766` against a throwaway fake Prefect API that answers one fixed status; called with `uv run fastmcp call ... ingest_url url=https://example.com/?qa177-<n>`; servers stopped by PID)
- Happy path (local env, real Prefect): receipt `{"status": "scheduled", "flow_run_id": ..., "duplicate": false}`, no "Prefect API" log lines (PASS)
- HTTP 401 / 403: `configuration_error`, `retryable:false`, message names PREFECT_API_KEY, PREFECT_API_URL, "redeploy"; server log `Prefect API answered HTTP 401|403 while dispatching the ingest` (PASS)
- HTTP 404 (real client turns it into `ObjectNotFound`): `configuration_error`, `retryable:false`, DEPLOYMENT_MISSING_MESSAGE (PASS)
- HTTP 400 / 422 (other 4xx): `internal_error`, `retryable:false`, "Prefect API answered HTTP 4xx while dispatching the ingest: ..." + log line with status (PASS)
- HTTP 503 / 429: `pipeline_unavailable`, `retryable:true`, "Prefect API answered HTTP 503|429 — try again"; log line with status (PASS on mapping; see note on latency)
- Prefect API down (connection refused): `pipeline_unavailable`, `retryable:true`, "Prefect API unreachable — try again", log "unreachable ... All connection attempts failed" (PASS)
- All servers/fakes stopped; ports 8766/9101 verified free.

**Acceptance criteria**
- [x] PASS — 401/403 → configuration_error, not retryable, message has PREFECT_API_KEY/PREFECT_API_URL/redeploy — `test_tools.py::TestDispatchErrors::test_a_rejected_key_names_the_variables_and_redeploy`, live 401 and 403 above
- [x] PASS — 404 (ObjectNotFound and raw 404 PrefectHTTPStatusError) → DEPLOYMENT_MISSING_MESSAGE — status-table `[404]`, envelope table `[404]`/`[object-not-found]`, `tools.py` `_prefect_status_error`; live 404 above
- [x] PASS — 429/5xx → pipeline_unavailable, retryable, `HTTP <status>` — status table, live 503/429
- [x] PASS — other 4xx → internal_error, not retryable, `HTTP <status>` — status table `[422]`, live 400/422
- [x] PASS — ConnectError/TimeoutException → "Prefect API unreachable — try again" — `test_unreachable_prefect_is_pipeline_unavailable`, live refused-connection
- [x] PASS — every status branch logs status + traceback — caplog assertion (`exc_info` set) and live server logs
- [x] PASS — ERROR_CONTRACT verbatim, envelope table (10 rows) green, tutorial sentence and glossary row updated — `git diff` of tutorials/4_x_x_deploying_mcp_server.md and docs/glossary.md
- [x] PASS — format-check / lint-check / pre-commit / memory-tests green — outputs above
- [x] PASS — live bogus-key `ingest_url` answers configuration_error (reproduced independently with a fake Prefect endpoint returning 401; SWE's Prefect Cloud run is in their log)

**Other issues found (not blocking)**
- Latency: Prefect's own client retries 429/502/503/504 with backoff before raising, so a 503 took 124 s and a 429 took 141 s to reach the tool answer in my run (401/403/404/400/422 answer in about 1 s). Pre-existing, not introduced here, but a model/Horizon client with a shorter timeout will see a timeout rather than the new envelope for 429/5xx. Candidate follow-up.
- The "other 4xx" `internal_error` message embeds `str(exc)`, which includes the full Prefect URL (on Cloud: account and workspace ids) and a developer.mozilla.org link. Consistent with the existing `internal_error` helper (`{tool} failed: {exc}`) and mandated by the spec text, so left as is.
- Docs: the PA's Part 2 glossary draft for **Tool error envelope** also carries a final sentence about `search_web` answering Bright Data's cooldown as `fetch_failed` (true), never `search_unavailable` (task 176). Task 176 shipped without it and this task did not add it, although the ADR-008 Status line does carry the 176 clause. The 177 dispatch wording otherwise matches the draft. Add the one sentence (here or in a follow-up) so the glossary and ADR agree.
- The `ObjectNotFound` path keeps its old log line (no status object); acceptable and noted by the SWE.

**VERDICT: PASS**

### [SWE] 2026-10-03 — Tester follow-ups

- Other-4xx `internal_error` message now names only the status (`Prefect API answered HTTP <status> while dispatching the ingest`) — no `str(exc)`, so the Prefect Cloud account/workspace URL no longer reaches the model; the full exception stays in the logged traceback. Regression test `test_tools.py::TestDispatchErrors::test_another_4xx_names_only_the_status_not_the_request_url` (red before the fix, green after).
- `docs/glossary.md` **Tool error envelope** row: added the Part 2 sentence — `search_web` answers Bright Data's cooldown body as `fetch_failed` (true), never `search_unavailable` (task 176); other 4xx noted as "naming only the status".
- `make memory-format-check` / `memory-lint-check` / `pre-commit` green; `make memory-tests` 4790 passed (env-status: local).

### [PA] 2026-10-04 00:35 — Acceptance Review

**VERDICT: ACCEPT**

Reviewed from the user's POV in the feature-level acceptance of PR #45 (first pass REJECTed on
documentation discipline only — ADR-013 §1/§5/§7 did not record the mid-pipeline decisions; resolved by
rollup `tasks/done/184-pa-rejection-horizon-mcp-fixes.md`, commit 7fbe460). Evidence from the Tester
log entries above; every automated acceptance criterion verified against the user-visible surface
(tool text, docstrings, README, skill, tutorial, glossary). The `[HUMAN]` post-merge Horizon checks, where
present, stay open and are listed in the PR body. Hand off to the PR Reviewer.
