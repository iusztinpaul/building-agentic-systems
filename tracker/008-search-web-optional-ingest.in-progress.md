# Optional ingestion path for `search_web`

Status: pending
Tags: `mcp`, `agentic-tools`, `web`, `search`, `prefect`
Depends on: #006, #007
Blocks: #009

## Scope

Extend the `search_web` MCP tool from #007 with an opt-in path that pushes selected SERP URLs into the existing web ingestion Prefect deployment (`ingest-web-url-batch-etl`). The default behavior remains "search and read, do NOT touch memory" — ingestion only fires when the caller explicitly opts in.

This task does **not** introduce a new ingestion pipeline. It composes with the existing `ingest_web_url_batch` flow served by `tree.orchestrator` (see `apps/memory/src/tree/data/web/web_pipeline.py`). The trigger pattern matches `apps/memory/scripts/run_url_data_pipeline.py` — `prefect.client.orchestration.get_client()` + `create_flow_run_from_deployment`.

### Behavioral contract

Add three new parameters to the `search_web` tool:

```python
@mcp.tool
async def search_web(
    query: str,
    ctx: Context,
    engine: Literal["google", "bing", "yandex"] = "google",
    num_results: int = 10,
    country: str | None = None,
    language: str | None = None,
    ingest: bool = False,
    ingest_top_k: int | None = None,
    ingest_urls: list[str] | None = None,
) -> str:
    ...
```

Decision matrix:

| `ingest` | `ingest_urls` | `ingest_top_k` | Behavior |
|---|---|---|---|
| `False` (default) | — | — | Pure search. Return SERP results only. No ingestion. |
| `True` | `None` | `None` | Ingest **all** returned URLs. |
| `True` | `None` | `K` (>=1) | Ingest the **top K** URLs from the SERP results (after the search). |
| `True` | `[urls...]` | — | Ingest **only** the explicitly listed URLs. They do not need to be a subset of the SERP results — the caller might pass them straight from a previous turn. `ingest_top_k` is ignored if `ingest_urls` is provided. |
| `False` | non-`None` | any | Treat as user error → return `{"error": "invalid_input", "detail": "ingest_urls/ingest_top_k passed but ingest=false"}`. |

### Ingestion trigger

- Use Prefect's async client to fire the existing `ingest-web-url-batch-etl/ingest-web-url-batch-etl` deployment with the chosen URL list.
- **Fire-and-forget** (non-blocking): create the flow run and return immediately. The tool's response payload includes the Prefect flow-run ID and a tracking URL the caller can poll if they want — but `search_web` does not block on flow completion. (Rationale: SERP is sub-5-second; the ingest pipeline can take minutes for many URLs. Blocking would defeat "on-demand exploratory search".)
- The trigger code lives in a small helper — propose `apps/memory/src/tree/data/web/web_search_ingest.py` with a single function:

  ```python
  async def trigger_url_batch_ingest(urls: list[str]) -> dict[str, str]:
      """Fire the ingest-web-url-batch-etl deployment with the given URLs.

      Returns a dict with `flow_run_id` (str) and `tracking_url` (str).
      Raises if the deployment is not registered (i.e. workflows aren't
      being served).
      """
  ```

  Mirrors the body of `apps/memory/scripts/run_url_data_pipeline.py` lines 39–48 but without the polling/log-streaming loop.

### Tool response shape

When `ingest=False` (default): unchanged from #007 — `{"query": ..., "engine": ..., "results": [...]}`.

When `ingest=True`: append an `ingest` field:

```json
{
  "query": "...",
  "engine": "google",
  "results": [...],
  "ingest": {
    "triggered": true,
    "urls": ["https://...", "..."],
    "flow_run_id": "abcd-1234-...",
    "tracking_url": "http://127.0.0.1:4200/runs/flow-run/abcd-1234-..."
  }
}
```

If the trigger fails (e.g. workflows not served), the tool still returns the SERP results but the `ingest` block has `triggered=false`, `error="<message>"`, no `flow_run_id`. Search results are independent of the ingest outcome — never make a successful search look like a failure.

### CLI script flags

Add `--ingest`, `--ingest-top-k`, `--ingest-urls` to `apps/memory/scripts/search_web.py` so the CLI is at parity with the MCP tool. `--ingest-urls` accepts a comma-separated list. Wire matching `INGEST=true`, `INGEST_TOP_K=N`, `INGEST_URLS="a,b,c"` env-var-style overrides into the `make memory-search-web` Makefile target.

## Acceptance Criteria

- [x] Default call `search_web(query="...")` (without `ingest`) returns `{"query", "engine", "results"}` only — **no** `ingest` field. Verified by unit test on the tool function.
- [x] `search_web(query="...", ingest=True)` triggers the `ingest-web-url-batch-etl/ingest-web-url-batch-etl` deployment with the full list of returned URLs. Verified by unit test mocking `prefect.client.orchestration.get_client` and asserting on the captured `parameters` dict.
- [x] `search_web(query="...", ingest=True, ingest_top_k=2)` triggers the deployment with **only** the first 2 URLs from the SERP results. Verified by unit test.
- [x] `search_web(query="...", ingest=True, ingest_urls=["https://a", "https://b"])` triggers the deployment with **exactly** those 2 URLs (ignoring SERP). Verified by unit test.
- [x] `search_web(query="...", ingest=False, ingest_top_k=3)` returns `{"error": "invalid_input", "detail": "..."}` and does NOT trigger the deployment. Verified by unit test.
- [x] `search_web(query="...", ingest=True, ingest_urls=[])` (explicitly empty list) returns `{"error": "invalid_input", "detail": "ingest_urls is empty"}` and does NOT trigger the deployment. Verified by unit test.
- [x] When the trigger succeeds, the response's `ingest` block contains `triggered=true`, `flow_run_id` (str), `tracking_url` (str starting with `http`), and `urls` (the list actually sent). Verified by unit test.
- [x] When the Prefect deployment lookup raises (deployment not found / no server), the tool returns the SERP results with `ingest.triggered=false` and `ingest.error="<message>"` — does NOT propagate the exception. Verified by unit test mocking the Prefect client to raise.
- [x] The tool is **non-blocking on flow completion**: after `create_flow_run_from_deployment` returns the flow-run object, `search_web` returns immediately without polling. Verified by unit test asserting `read_flow_run` is **not** called.
- [x] CLI parity: `uv run python scripts/search_web.py --query "..." --ingest --ingest-top-k 2` triggers the deployment (or reports trigger failure) and prints the ingest block. `--ingest-urls "a,b"` works equivalently.
- [ ] [HUMAN] **Real e2e check** (single-run with workflows served and `BRIGHTDATA_SERP_ZONE` set): SWE runs `make memory-serve-workflows &`, then `make memory-search-web QUERY="prefect orchestration" INGEST=true INGEST_TOP_K=1`. Captures: (a) the SERP result list, (b) the `flow_run_id` printed, (c) the eventual flow run state via `uv --directory apps/memory run prefect flow-run inspect <id>` showing `Completed` (or marks `NOT RUN — reason: live SERP zone unavailable` and #009 covers this). NOT RUN — live BRIGHTDATA_SERP_ZONE access blocked in worktree (sandbox denies reading .env credentials); deferred to Tester / #009.
- [ ] [HUMAN] `mongosh tree --eval 'db.documents.find({source_type:"web"}).count()'` increases by **at most** the number of new (non-duplicate) URLs ingested after the e2e step. Captured before/after counts in the SWE log. NOT RUN — depends on the e2e check above.
- [ ] [HUMAN] Calling `search_web` with `ingest=False` (default) does **not** increase the `documents` count. Verified by the same before/after method. NOT RUN — depends on live e2e; logically guaranteed by the unit test asserting the trigger is NOT awaited on the default path.
- [x] All new code (helper, tool changes, CLI flags) has full type annotations.
- [x] `make memory-format-check && make memory-lint-check && make memory-unit-tests && make pre-commit` pass. Output captured.
- [x] Unit tests at `apps/memory/tests/unit/data/web/test_web_search_ingest.py` (helper) and `apps/memory/tests/unit/mcp/test_search_web.py` (extended). No real Prefect / Bright Data calls.

## User Stories

### Story: Agent searches first, then chooses what to keep
1. The agent invokes `search_web(query="latest agent-tool-use papers", num_results=10)`.
2. The agent reads the 10 returned results, picks the 3 most relevant URLs.
3. The agent invokes `search_web(query="latest agent-tool-use papers", num_results=10, ingest=True, ingest_urls=[chosen_3])`.
4. The tool returns the SERP results again (cheap second hop) plus an `ingest` block with `triggered=true` and a `flow_run_id`.
5. A few minutes later the agent runs `query_memory(query="agent tool use")` and sees the ingested content surfacing in the knowledge graph.

### Story: Operator does fire-and-forget bulk ingest from the CLI
1. Operator runs `make memory-serve-workflows &` to ensure the Prefect worker picks up runs.
2. Operator runs `make memory-search-web QUERY="MongoDB Atlas vector search" INGEST=true INGEST_TOP_K=5`.
3. Operator immediately sees the SERP list logged + an `ingest.flow_run_id` and a tracking URL.
4. The CLI exits 0 within ~10 seconds — does NOT hang waiting for the batch ingest to finish.
5. Operator opens the tracking URL in a browser and watches the flow finish in the Prefect UI.

### Story: Default path leaves memory untouched
1. Operator runs `mongosh tree --eval 'db.documents.countDocuments({source_type:"web"})'` and notes the count `N`.
2. Operator runs `make memory-search-web QUERY="anything"` (no `INGEST` flag).
3. Operator re-runs the count — still `N`.
4. The output JSON has no `ingest` field at all (key absent).

### Story: Caller misuses the ingest flags
1. The agent calls `search_web(query="x", ingest=False, ingest_top_k=5)`.
2. The tool returns `{"error": "invalid_input", "detail": "ingest_urls/ingest_top_k passed but ingest=false"}`.
3. The Prefect deployment is NOT triggered — verified by mocking the Prefect client and asserting it was never called.

### Story: Workflows aren't running
1. Operator forgets to start `make memory-serve-workflows`.
2. Operator runs `make memory-search-web QUERY="x" INGEST=true`.
3. The CLI prints SERP results normally, but the `ingest` block reads `triggered=false` with an error message naming the missing deployment / connection.
4. Exit code is still 0 — the search itself succeeded; ingestion was best-effort.

---

Blocked by: #006, #007

## Log

### [SWE] 2026-05-01 14:20 — Implementation

**Files modified**
- `apps/memory/src/tree/data/web/web_search_ingest.py` — new helper: fire-and-forget trigger of `ingest-web-url-batch-etl/ingest-web-url-batch-etl` via `prefect.client.orchestration.get_client`. Returns `{flow_run_id, tracking_url}`. No polling.
- `apps/memory/src/tree/mcp/tools.py` — `search_web` MCP tool extended with `ingest`, `ingest_top_k`, `ingest_urls` parameters. Added `_build_ingest_block` helper. Restored docstring sentence "...or call `search_web` with `ingest=true` for ingestion".
- `apps/memory/scripts/search_web.py` — added `--ingest`, `--ingest-top-k`, `--ingest-urls` Click options + parsing for comma-separated URL list. Trigger failures degrade to exit 0 (search succeeded). Validation errors exit 1.
- `apps/memory/Makefile` — `search-web` target now passes `INGEST=true`, `INGEST_TOP_K=N`, `INGEST_URLS="a,b,c"` through to the CLI.
- `apps/memory/tests/unit/data/web/test_web_search_ingest.py` — new file: 7 tests covering happy path, deployment-name lookup, parameters dict, no polling, error propagation, tracking-URL construction, and empty-input rejection.
- `apps/memory/tests/unit/mcp/test_search_web.py` — extended with `TestSearchWebIngestPath` (9 tests for the MCP tool's ingest path) and 4 new CLI tests.

**Tests**
- Unit (memory): **431 passing, 0 failing** — `make memory-unit-tests` ran in 19.91s.
- Targeted file run: 37 tests in `test_web_search_ingest.py` + `test_search_web.py` all green.
- Integration: N/A — no infra changes; behavior is mocked end-to-end.

**Acceptance criteria**
- 13 of 16 marked `[x]` and verified by unit tests in the two files above.
- 3 marked `[HUMAN] NOT RUN` — they require live `BRIGHTDATA_SERP_ZONE` and a running `make memory-serve-workflows`; sandbox prevented reading `.env` to confirm zone availability. Logically covered by mocked unit tests; live-flow validation deferred per spec hint that #009 covers this.

**Evidence**

```
$ make memory-format-fix
3 files reformatted, 139 files left unchanged

$ make memory-lint-fix
All checks passed!

$ make memory-format-check
142 files already formatted

$ make memory-lint-check
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... (full suite) ...
============================= 431 passed in 19.91s =============================

$ uv run python scripts/search_web.py --help
Options:
  -q, --query TEXT                Search query.  [required]
  -e, --engine [google|bing|yandex]
                                  Search engine to query.  [default: google]
  -n, --num-results INTEGER       Maximum number of organic results to return.
                                  [default: 10]
  -c, --country TEXT              Optional 2-letter ISO country code...
  -l, --language TEXT             Optional 2-letter language code (e.g. 'en').
  --ingest                        Fire-and-forget the ingest-web-url-batch-etl
                                  Prefect deployment with the selected URLs.
  --ingest-top-k INTEGER          When --ingest is set, ingest only the first
                                  K SERP URLs.
  --ingest-urls TEXT              When --ingest is set, ingest exactly these
                                  URLs (comma-separated). Overrides --ingest-
                                  top-k.
  --help                          Show this message and exit.

$ uv run python scripts/search_web.py --query "x" --ingest-top-k 3
Invalid input: --ingest-urls/--ingest-top-k requires --ingest
(SystemExit: 1 — validation runs before the SERP call as designed.)

$ make -n memory-search-web QUERY="test query" INGEST=true INGEST_TOP_K=2
uv run python scripts/search_web.py --query "test query" \
    --ingest \
    --ingest-top-k 2
```

**Notes**
- `_build_ingest_block` swallows broad `Exception` from the trigger helper (with `noqa: BLE001`) so the search payload is never lost to a Prefect outage. Same pattern in the CLI's `_maybe_ingest`.
- `ingest_urls is not None` is the override signal — an explicit empty list is rejected with `invalid_input` (separate AC); a `None` value falls through to `ingest_top_k` then to "all SERP URLs".
- Empty SERP results + `ingest=True` returns `triggered=false` with `detail="no urls to ingest (empty SERP results)"` rather than firing the deployment with `[]` (the batch flow's `init_mongodb`/`asyncio.gather` would no-op anyway, but skipping saves a flow run).
- Validation is fail-fast: misuse (`ingest_top_k` without `ingest`) skips the SERP call entirely, saving a Bright Data credit on a request we'd reject anyway. Unit test confirms `mock_search.assert_not_awaited()`.
- Tracker file renamed from `.groomed.md` to `.in-progress.md` per `docs/PROCESS.md` "Tracker Modes" section. Note for the orchestrator: #006 stayed `.groomed.md` even after completion in `done/` — that's an inconsistency in earlier task hand-offs, not in mine.

### [Tester] 2026-05-01 11:55 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green)
- Unit tests: 431 passed / 0 failed / 0 warnings (`make memory-unit-tests` in 19.96s)
- Targeted ingest tests: 37 passed (7 helper + 13 ingest-path + 7 base + 10 CLI) in 5.23s
- Integration tests: NOT RUN — per task scope (live BRIGHTDATA_SERP_ZONE + Prefect deferred to #009)

**E2E adversarial pass**
1. **Default doesn't pollute memory** — `import tree.mcp.tools` succeeds without a Prefect server (verified live: `uv run python -c "import tree.mcp.tools"` → `OK`). Module-level import of `web_search_ingest` is safe because it only imports `prefect.client.orchestration.get_client` (already a project dependency); it does not connect at import time. Default-path test `test_default_does_not_emit_ingest_field_or_call_trigger` asserts `_trigger_url_batch_ingest` is never awaited and `"ingest"` is absent from the payload — PASS.
2. **CLI validation fail-fast (no SERP credit burned)** — live exercise `uv run python scripts/search_web.py --query "x" --ingest-top-k 3` → "Invalid input: --ingest-urls/--ingest-top-k requires --ingest" + EXIT=1. No SERP call attempted (would have surfaced a configuration_error if it did, since BRIGHTDATA_SERP_ZONE isn't set in the worktree). PASS.
3. **CLI empty `--ingest-urls`** — `uv run python scripts/search_web.py --query "x" --ingest --ingest-urls ""` → "Invalid input: --ingest-urls is empty" + EXIT=1. PASS.
4. **Tool-level adversarial inputs** (live exercise with mocked SERP + trigger):
   - `ingest_top_k=0` → `triggered=false`, `urls=[]`, trigger NOT awaited. Graceful no-op (cosmetic: `detail` says "empty SERP results" though SERP wasn't empty — see Other issues).
   - `ingest_top_k=-1` → Python slicing `results[:-1]` returns all-but-last; trigger awaited with `['https://a']`. Spec only defines K>=1; not validated. See Other issues.
   - `ingest_top_k=999` (oversized) → safely slices to all results. PASS.
   - `ingest_urls=['not-a-url', '\x00\x00']` → passed straight through; spec explicitly delegates URL validation to the deployment (helper docstring: "this helper does not"). PASS by design.
5. **Trigger raises ConnectionError live** — SERP results preserved (1 result), `ingest.triggered=false`, `ingest.error="Prefect server unreachable"`, no exception escapes. Logged as WARNING (not print). PASS.
6. **Empty SERP + ingest=True** — `test_ingest_true_with_empty_serp_results_does_not_fire` asserts `mock_trigger.assert_not_awaited()`. PASS.
7. **Fire-and-forget invariant (headline)** — re-read `web_search_ingest.py`: no `while`, no `asyncio.sleep`, no `read_flow_run` call. `test_does_not_poll_run_state` (test_web_search_ingest.py:105-116) asserts `client.read_flow_run.assert_not_awaited()`. PASS.
8. **Makefile flag forwarding** — `make -n search-web QUERY="test query" INGEST=true INGEST_TOP_K=2` dry-run emits `--ingest --ingest-top-k 2` correctly. PASS.
9. **Tool returns JSON string (FastMCP convention from #007)** — `json.dumps(payload, indent=2)` at tools.py:358; ingest block nests cleanly inside the top-level dict. PASS.

**Acceptance criteria**
- [x] PASS — Default call returns `{query,engine,results}` only — `test_default_does_not_emit_ingest_field_or_call_trigger` (test_search_web.py:257-274).
- [x] PASS — `ingest=True` fires deployment with all SERP URLs — `test_ingest_true_fires_deployment_with_all_urls` (test_search_web.py:276-303); asserts `mock_trigger.assert_awaited_once_with([url1,url2])`.
- [x] PASS — `ingest_top_k=2` truncates to first 2 — `test_ingest_top_k_truncates_to_first_k_urls` (test_search_web.py:305-324).
- [x] PASS — Explicit `ingest_urls` overrides SERP and `ingest_top_k` — `test_ingest_urls_overrides_ingest_top_k_and_serp` (test_search_web.py:326-352, ingest_top_k=99 ignored, custom_urls win).
- [x] PASS — `ingest=False, ingest_top_k=3` returns `invalid_input`, no trigger, no SERP — `test_ingest_false_with_top_k_returns_invalid_input` (test_search_web.py:354-372). Asserts BOTH `mock_trigger.assert_not_awaited()` AND `mock_search.assert_not_awaited()`. Live CLI exit=1 confirmed.
- [x] PASS — `ingest=True, ingest_urls=[]` returns `invalid_input` — `test_ingest_true_with_explicit_empty_urls_returns_invalid_input` (test_search_web.py:391-407). Live CLI confirmed.
- [x] PASS — Successful trigger payload shape — `test_ingest_true_fires_deployment_with_all_urls` asserts `triggered=True`, `flow_run_id="fr-1"`, `tracking_url.startswith("http")`, `urls=[...]`.
- [x] PASS — Trigger failure degrades — `test_trigger_failure_degrades_to_search_only_payload` (test_search_web.py:432-454). Live ConnectionError exercise also confirmed: SERP preserved, structured error block, no exception propagated.
- [x] PASS — Non-blocking on flow completion — `test_does_not_poll_run_state` (test_web_search_ingest.py:105-116) asserts `client.read_flow_run.assert_not_awaited()`. Code review of `web_search_ingest.py` shows no polling loop.
- [x] PASS — CLI parity (`--ingest`, `--ingest-top-k`, `--ingest-urls`) — `test_ingest_flag_fires_trigger_with_top_k`, `test_ingest_urls_overrides_top_k`, `test_ingest_top_k_without_ingest_flag_exits_one`, `test_ingest_trigger_failure_still_exits_zero` all PASS. Live `--help` lists all three flags. Makefile dry-run confirms env-var forwarding.
- [ ] [HUMAN] **Real e2e check** — NOT RUN. Per task scope (live `BRIGHTDATA_SERP_ZONE` + serve-workflows deferred to #009).
- [ ] [HUMAN] mongosh count delta — NOT RUN. Per task scope.
- [ ] [HUMAN] Default leaves count untouched — NOT RUN live; logically guaranteed by mocked `test_default_does_not_emit_ingest_field_or_call_trigger` (trigger never awaited on the default path).
- [x] PASS — Full type annotations — read all four modified files; every function/method has typed parameters and return types (including `_run`, `_maybe_ingest`, `_parse_ingest_urls`, `_build_ingest_block`, `trigger_url_batch_ingest`).
- [x] PASS — `make memory-format-check`, `make memory-lint-check`, `make memory-unit-tests`, `make pre-commit` all green (output captured below).
- [x] PASS — Test files exist at the expected paths; 7 helper tests + 13 new MCP-tool ingest-path tests + 4 new CLI ingest tests.

**Evidence**

```
$ make memory-format-check
142 files already formatted

$ make memory-lint-check
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... tests/unit/mcp/test_search_web.py .............................. [ 63%]
... tests/unit/data/web/ ... (passed) ...
============================= 431 passed in 19.96s =============================

$ uv run pytest tests/unit/data/web/test_web_search_ingest.py tests/unit/mcp/test_search_web.py -v
============================== 37 passed in 5.23s ==============================

$ uv run python scripts/search_web.py --query "x" --ingest-top-k 3; echo EXIT=$?
Invalid input: --ingest-urls/--ingest-top-k requires --ingest
EXIT=1

$ uv run python scripts/search_web.py --query "x" --ingest --ingest-urls ""; echo EXIT=$?
Invalid input: --ingest-urls is empty
EXIT=1

$ make -n search-web QUERY="test query" INGEST=true INGEST_TOP_K=2
uv run python scripts/search_web.py --query "test query" \
    --ingest \
    --ingest-top-k 2

$ uv run python -c "import tree.mcp.tools; print('OK import without prefect server')"
OK import without prefect server
```

**Other issues found** (non-blocking — do NOT fail the task; consider for #009 / a follow-up nit):
- `ingest_top_k=0` returns `detail="no urls to ingest (empty SERP results)"` even when SERP returned results — wording is slightly misleading. Cosmetic; the user-visible state (`triggered=false`, no flow run) is correct.
- `ingest_top_k=-1` (and other negatives) are silently treated as Python slice indices (`results[:-1]` etc.) and produce a non-obvious selection. The spec defines `K>=1`; out-of-range values aren't validated. Recommend adding an `if ingest_top_k is not None and ingest_top_k < 1: return invalid_input` guard. Not in the AC list, so PASS-with-note rather than FAIL. Worth picking up in #009 alongside the live e2e checks.
- `web_search_ingest.py` reads `client.api_url` and strips `/api` to construct the tracking URL. Prefect Cloud / behind-proxy deployments may use a different URL shape (`/api/accounts/.../workspaces/...`); the `removesuffix("/api")` only chops a literal trailing `/api`. Acceptable for the local dev setup this task targets; flag for #009 if the project ever moves to Prefect Cloud.

**VERDICT: PASS**
