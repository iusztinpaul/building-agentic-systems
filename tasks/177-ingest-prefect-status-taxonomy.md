---
id: 177-ingest-prefect-status-taxonomy
status: pending
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

- [ ] A `PrefectHTTPStatusError` with 401 or 403 answers `{"error_type": "configuration_error", "retryable":
      false}` whose message contains `PREFECT_API_KEY`, `PREFECT_API_URL` and "redeploy".
- [ ] 404 (both as `ObjectNotFound` and as a 404 `PrefectHTTPStatusError`) answers the one
      `DEPLOYMENT_MISSING_MESSAGE` as `configuration_error`, not retryable.
- [ ] 429 and every 5xx answer `pipeline_unavailable`, retryable, message containing `HTTP <status>`.
- [ ] Any other 4xx answers `internal_error`, not retryable, message containing `HTTP <status>`.
- [ ] `httpx.ConnectError` / `httpx.TimeoutException` still answer `pipeline_unavailable` "Prefect API
      unreachable — try again".
- [ ] Every branch logs the status code with the traceback.
- [ ] `ERROR_CONTRACT` verbatim in the three ingest docstrings; `test_error_envelope.py` table extended and
      green; tutorial sentence and glossary row updated.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [ ] Live (local server, bogus cloud key): `ingest_url` answers `configuration_error`; evidence in the Log.

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
