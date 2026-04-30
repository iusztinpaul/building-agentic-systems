# Wire URL config + ingest_all_data + Make targets + orchestrator deployment

Status: pending
Tags: `data-pipeline`, `web`, `config`, `prefect`, `infrastructure`
Depends on: #002, #003
Blocks: #005

## Scope

Surface the new web pipeline through the same operator interfaces as every other data pipeline: YAML config, the unified `ingest_all_data` flow, the Make target convention, and the Prefect deployment registry.

### 1. App config: `app_config.sources.urls: list[str]`

Edit `apps/memory/src/tree/config/app_config.py`:

```python
class SourcesConfig(BaseModel):
    substack: list[str] = []
    substack_articles: list[str] = []
    huggingface_arxiv_dataset: HuggingFaceArxivDatasetConfig = (
        HuggingFaceArxivDatasetConfig()
    )
    urls: list[str] = []  # NEW — arbitrary URLs ingested via Bright Data fallback
```

Edit `apps/memory/configs/default.yaml`:

```yaml
sources:
  substack:
    - ...
  substack_articles:
    - ...
  huggingface_arxiv_dataset: {...}
  urls: []   # NEW — populate with any blog/news/repo/profile URL
```

Add a corresponding test in `apps/memory/tests/unit/config/test_app_config.py` asserting:
- The default `urls` list is `[]`.
- A YAML with `sources: {urls: [https://x.com, https://y.com]}` round-trips to `app_config.sources.urls == ["https://x.com", "https://y.com"]`.

### 2. Wire `ingest_all_data` to dispatch URLs through the dispatcher

Edit `apps/memory/src/tree/data/pipeline.py` to add a new step that delegates each URL in `app_config.sources.urls` to **the dispatcher** (`tree.data.core.ingest.ingest_url`), not directly to `ingest_web_url_batch`. Rationale: a config URL might be a Substack URL the user added by hand — in that case, the specialized pipeline should win, exactly as it does today through the dispatcher.

```python
urls = app_config.sources.urls
if urls:
    logger.info("Starting URL pipeline (dispatcher) with %d URLs", len(urls))
    url_docs = await asyncio.gather(*[ingest_url(u) for u in urls])
    url_docs = [d for d in url_docs if d is not None]
    all_ingested.extend(url_docs)
    logger.info("URL pipeline ingested %d documents", len(url_docs))
else:
    logger.info("URL pipeline skipped: no URLs configured")
```

Update the existing `tests/unit/data/test_pipeline.py` to cover:
- `urls=[]` skips the new step.
- `urls=[<one substack url>, <one arbitrary url>]` results in two `ingest_url` calls, both via the dispatcher.

### 3. Register Prefect deployments

Edit `apps/memory/src/tree/orchestrator.py` to add:

```python
ingest_web_url.to_deployment(
    name="ingest-web-url-etl",
    tags=["data-pipeline", "web"],
),
ingest_web_url_batch.to_deployment(
    name="ingest-web-url-batch-etl",
    tags=["data-pipeline", "web"],
),
```

(Imports as `from tree.data.web.web_pipeline import ingest_web_url, ingest_web_url_batch`.)

### 4. Make targets

Edit `apps/memory/Makefile` under `# --- Data Pipelines ---`:

```makefile
URL ?=

run-url-data-pipeline: # Trigger web URL ingestion via Bright Data Web Unlocker. Pass URL="https://...".
	@if [ -z "$(URL)" ]; then echo "USAGE: make run-url-data-pipeline URL=https://..."; exit 1; fi
	uv run python scripts/run_url_data_pipeline.py "$(URL)"
```

(Root `Makefile` already delegates `memory-%` → `apps/memory/Makefile`, so `make memory-run-url-data-pipeline URL=...` works automatically — no root edit needed.)

Add a new entry script at `apps/memory/scripts/run_url_data_pipeline.py` mirroring `run_substack_article_data_pipeline.py`: it triggers the `ingest-web-url-etl` deployment with `parameters={"url": <URL from argv>}` and streams logs. Calls `init_logger()` at module level.

### 5. Update `run_all_data_pipelines.py`

Already triggers `ingest-all-data-etl/ingest-all-data-etl`, which now picks up `sources.urls`. No script change needed, but the help text/docstring at the top of the script should mention the new step.

### What this task does NOT do

- Does NOT touch the dispatcher logic itself (#003 owns that).
- Does NOT add integration tests (#005 owns that).

## Acceptance Criteria

- [x] `apps/memory/src/tree/config/app_config.py` defines `SourcesConfig.urls: list[str] = []`.
- [x] `apps/memory/configs/default.yaml` includes a `sources.urls: []` line with a comment.
- [x] `app_config.sources.urls` round-trips correctly from a YAML override (verified by unit test).
- [x] `tree.data.pipeline.ingest_all_data` calls `tree.data.core.ingest.ingest_url` for each URL in `app_config.sources.urls`, in parallel via `asyncio.gather`, and includes successful results in its return value (verified by unit test).
- [x] `ingest_all_data` skips the URL step (no calls, single INFO log) when `urls=[]` (verified by unit test).
- [x] `tree.orchestrator` registers `ingest-web-url-etl` and `ingest-web-url-batch-etl` deployments, both tagged `data-pipeline` and `web`.
- [x] `apps/memory/Makefile` defines `run-url-data-pipeline` requiring a `URL=` arg; missing arg prints a usage line and exits non-zero.
- [x] `apps/memory/scripts/run_url_data_pipeline.py` exists, calls `init_logger()` at module level, triggers the `ingest-web-url-etl` deployment with the URL passed via `sys.argv[1]`, and streams logs (mirror `run_substack_article_data_pipeline.py`).
- [x] `make memory-help` lists the new `run-url-data-pipeline` target.
- [x] All datetimes UTC-aware. All public functions fully type-annotated.
- [x] `make memory-format-check && make memory-lint-check && make memory-unit-tests` pass.

## User Stories

### Story: Operator declares a blog URL in YAML config
1. Operator edits `apps/memory/configs/default.yaml`, adds under `sources.urls`:
   ```yaml
   urls:
     - https://martinfowler.com/articles/microservices.html
   ```
2. Operator runs `make local-start` (MongoDB + Prefect).
3. Operator runs `make memory-serve-workflows &`.
4. Operator runs `make memory-run-all-data-pipelines`.
5. Operator sees in the streamed logs: `Starting URL pipeline (dispatcher) with 1 URLs`, then `URL pipeline ingested 1 documents`.
6. Operator runs `mongosh` and `db.documents.findOne({source_uri: "https://martinfowler.com/articles/microservices.html"})` and sees the new document with `source_type: "web"`.

### Story: Operator ingests an ad-hoc URL via Make target
1. Operator runs `make memory-run-url-data-pipeline URL="https://lethain.com/staff-engineer/"`.
2. Operator sees the streamed Prefect logs from the `ingest-web-url-etl` deployment, ending with `Done.` or an explicit failure.
3. The document lands in MongoDB with `source_type: "web"`, `source_uri: "https://lethain.com/staff-engineer/"`.

### Story: Operator forgets to pass URL
1. Operator runs `make memory-run-url-data-pipeline`.
2. Operator sees `USAGE: make run-url-data-pipeline URL=https://...`.
3. Make exits with non-zero status (does not silently no-op).

### Story: Operator mixes a Substack URL and an arbitrary URL in `sources.urls`
1. Operator's YAML contains:
   ```yaml
   urls:
     - https://www.decodingai.com/p/ai-agents-foundations-course
     - https://martinfowler.com/articles/microservices.html
   ```
2. Operator runs `make memory-run-all-data-pipelines`.
3. The Substack URL is routed via the substack pipeline (the dispatcher's specialized match wins) — `source_type: "substack"`.
4. The Martin Fowler URL falls through to Bright Data — `source_type: "web"`.
5. Both documents land in MongoDB.

### Story: Operator inspects the new Make help
1. Operator runs `make memory-help`.
2. Operator sees a colored line for `run-url-data-pipeline` with the description `Trigger web URL ingestion via Bright Data Web Unlocker. Pass URL="https://...".`.

---

Blocked by: #002, #003

## Log

### [SWE] 2026-04-30 14:00 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — added `SourcesConfig.urls: list[str] = []`.
- `apps/memory/configs/default.yaml` — added `sources.urls: []` with operator comment.
- `apps/memory/src/tree/data/pipeline.py` — added URL dispatcher step using `tree.data.core.ingest.ingest_url` with `asyncio.gather`; logs skip when empty.
- `apps/memory/src/tree/orchestrator.py` — registered `ingest-web-url-etl` and `ingest-web-url-batch-etl` deployments tagged `data-pipeline` + `web`.
- `apps/memory/Makefile` — added `URL ?=` and `run-url-data-pipeline` target with usage guard.
- `apps/memory/scripts/run_url_data_pipeline.py` — new entry script mirroring `run_substack_article_data_pipeline.py`; calls `init_logger()` at module level, triggers `ingest-web-url-etl` with `parameters={"url": sys.argv[1]}`, streams logs.
- `apps/memory/tests/unit/config/test_app_config.py` — added round-trip + default tests for `sources.urls`.
- `apps/memory/tests/unit/data/test_pipeline.py` — extended config helper with `urls`; added 3 new tests covering skip/dispatch/None-filtering behavior.

**Tests**
- Unit: 362 passing, 0 failing — `make memory-unit-tests` (full suite). Targeted: 16 tests in `test_pipeline.py` + `test_app_config.py` all pass.
- Integration: N/A — task explicitly punts integration tests to #005.

**Acceptance criteria**
- [x] `SourcesConfig.urls: list[str] = []` — `app_config.py:68`.
- [x] `default.yaml` includes `sources.urls: []` with comment — `default.yaml:28-31`.
- [x] YAML round-trip verified — `test_app_config.py::TestLoadAppConfig::test_urls_round_trip_from_yaml`.
- [x] `ingest_all_data` dispatches each URL via `ingest_url` in parallel via `asyncio.gather` — verified by `test_pipeline.py::TestIngestAllData::test_dispatches_each_url_via_dispatcher`. Successful results included in return — same test asserts `substack_doc in result and web_doc in result`. None values filtered — `test_filters_none_results_from_url_dispatcher`.
- [x] Empty URLs skip step (no calls, INFO log) — `test_skips_urls_when_empty`.
- [x] Two new deployments registered with correct tags — `orchestrator.py:71-78`.
- [x] Makefile `run-url-data-pipeline` requires URL, exits non-zero on missing — verified live: `make run-url-data-pipeline` → `USAGE: ...` + Make error code 2.
- [x] Script exists, calls `init_logger()` at module level (line 24), triggers `ingest-web-url-etl` with `{"url": sys.argv[1]}`, streams logs — mirrors `run_substack_article_data_pipeline.py`.
- [x] `make memory-help` lists the new target with the exact AC description string — verified live.
- [x] All datetimes UTC (no datetime work in this task; existing patterns preserved). All public functions type-annotated.
- [x] Format check + lint check + unit tests all pass.

**Evidence**

`make memory-unit-tests` (tail):
```
tests/unit/models/test_voyage_multimodal_embedding.py ...........        [100%]

============================= 362 passed in 20.16s =============================
```

`make help | grep run-url-data-pipeline`:
```
[1;32mrun-url-data-pipeline[00m: Trigger web URL ingestion via Bright Data Web Unlocker. Pass URL="https://...".
```

`make run-url-data-pipeline` (no URL):
```
USAGE: make run-url-data-pipeline URL=https://...
make: *** [run-url-data-pipeline] Error 1
```

**Notes**
- E2E live run against Prefect (firing the real deployment) NOT RUN — requires `make local-start` + `make memory-serve-workflows` + Bright Data credentials, and is the explicit territory of #005 (integration tests + e2e).
- No commits made (per process — Tester runs next).

### [Tester] 2026-04-30 18:47 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all clean).
- Unit tests: 362 passed / 0 failed in 19.24s.
- Integration tests: NOT RUN — task spec explicitly punts to #005; `git status apps/memory/tests/integration/` is clean (no new or modified integration tests in this task).
- Warnings: 0 (no `warning` / `deprecat` markers in pytest output).

**E2E adversarial pass**
- Happy path #1: `make memory-help | grep run-url-data-pipeline` → exact ANSI-colored line `run-url-data-pipeline: Trigger web URL ingestion via Bright Data Web Unlocker. Pass URL="https://...".` (PASS).
- Happy path #2: default config loads via `load_app_config()` — `sources.urls == []`, other sources untouched (PASS).
- Break path 1 (missing URL arg): `make memory-run-url-data-pipeline` → prints `USAGE: make run-url-data-pipeline URL=https://...`, exits with status 2 from outer Make, status 1 from inner recipe (PASS).
- Break path 2 (empty URL string): `make memory-run-url-data-pipeline URL=""` → guard `[ -z "$(URL)" ]` triggers, same usage message + non-zero exit (PASS).
- Break path 3 (URL with spaces): `make -n memory-run-url-data-pipeline URL='https://example.com/with spaces'` → recipe expands to `uv run python scripts/run_url_data_pipeline.py "https://example.com/with spaces"`. URL stays quoted, single argv (PASS).
- Break path 4 (shell-injection attempt): `make -n memory-run-url-data-pipeline URL='https://example.com; echo INJECTED'` → expands to `uv run python scripts/run_url_data_pipeline.py "https://example.com; echo INJECTED"`. Double quotes neutralize the `;`; no command splitting (PASS).
- Break path 5 (script with no argv): `python -c "sys.argv=['x']; asyncio.run(main())"` → logs `USAGE: run_url_data_pipeline.py <url>`, `SystemExit code: 1` (PASS — fails loudly, not silently).
- Break path 6 (pydantic edge — null urls): `AppConfig.model_validate({'sources': {'urls': None}})` → `ValidationError: Input should be a valid list` (PASS — strict, no silent `None` propagation that would break `len(urls)` / `for u in urls`).
- Break path 7 (pydantic edge — non-string in list): `AppConfig.model_validate({'sources': {'urls': [42, 'https://x.com']}})` → `ValidationError: Input should be a valid string` (PASS — strict).
- Break path 8 (YAML parses cleanly): `yaml.safe_load(open('configs/default.yaml'))` → `urls` key present and equals `[]`, sibling keys intact (`substack`, `substack_articles`, `huggingface_arxiv_dataset`) (PASS).
- Break path 9 (out-of-scope sanity): `git diff HEAD -- apps/memory/src/tree/data/web/ apps/memory/src/tree/data/core/` and `git diff HEAD -- apps/memory/tests/integration/` both empty — task did not touch #002/#003-owned modules or #005 integration tests (PASS).
- Break path 10 (pipeline ordering): URL step runs after Substack RSS, Substack articles, and Arxiv (`pipeline.py:70`). Order avoids the duplicate-key race scenario where a Substack URL could be matched by the dispatcher's substack handler before the substack-articles batch ran (PASS).

**Acceptance criteria**
- [x] PASS — `SourcesConfig.urls: list[str] = []` defined — `app_config.py:68`.
- [x] PASS — `default.yaml` includes `sources.urls: []` with explanatory comment — `default.yaml:27-30`.
- [x] PASS — YAML round-trip verified — `tests/unit/config/test_app_config.py::TestLoadAppConfig::test_urls_round_trip_from_yaml` and `::test_urls_default_is_empty` (both pass).
- [x] PASS — `ingest_all_data` calls `ingest_url` (the dispatcher) per URL via `asyncio.gather`, includes results — `pipeline.py:19, 73`; `tests/unit/data/test_pipeline.py::test_dispatches_each_url_via_dispatcher` and `::test_filters_none_results_from_url_dispatcher` (both pass). Confirmed import is `from tree.data.core.ingest import ingest_url`, NOT a direct `ingest_web_url_batch` short-circuit.
- [x] PASS — Empty URLs skipped with INFO log — `pipeline.py:77-78`; `tests/unit/data/test_pipeline.py::test_skips_urls_when_empty` (passes).
- [x] PASS — Both deployments registered with `["data-pipeline", "web"]` tags — `orchestrator.py:72-79`. Deployment names match `ingest-web-url-etl` (used by the new entry script) and `ingest-web-url-batch-etl`.
- [x] PASS — Makefile `run-url-data-pipeline` guards on `URL=` — `Makefile:102-104`; missing/empty URL prints usage and exits non-zero (verified live).
- [x] PASS — `scripts/run_url_data_pipeline.py` exists, `init_logger()` at module level (line 25), targets `ingest-web-url-etl/ingest-web-url-etl` deployment, passes `parameters={"url": sys.argv[1]}`, streams logs via `read_logs` polling. Mirror of `run_substack_article_data_pipeline.py` confirmed by side-by-side diff — same imports, same Prefect client idiom, same final-state polling.
- [x] PASS — `make memory-help` lists target with the exact AC description.
- [x] PASS — All public functions type-annotated (`async def main() -> None:` in script; `async def ingest_all_data() -> list[Document]:` in pipeline). No new datetimes introduced in this task.
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-unit-tests` all green.

**Evidence**
```
$ make memory-format-check && make memory-lint-check
133 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
============================= 362 passed in 19.24s =============================

$ make memory-run-url-data-pipeline
USAGE: make run-url-data-pipeline URL=https://...
make[1]: *** [run-url-data-pipeline] Error 1
make: *** [memory-run-url-data-pipeline] Error 2
EXIT=2

$ make memory-help | grep run-url-data-pipeline
[1;32mrun-url-data-pipeline[00m: Trigger web URL ingestion via Bright Data Web Unlocker. Pass URL="https://...".
```

**Other issues found**
- None. The dispatcher-vs-batch routing is correct (uses `ingest_url`, not `ingest_web_url_batch`), so a Substack URL accidentally placed in `sources.urls` will still be routed to the substack pipeline by the #003 dispatcher (covered by the User Story #4 mix scenario, asserted in `test_dispatches_each_url_via_dispatcher`).
- The SWE-flagged ruff one-line collapse of a 3-line logger call is cosmetic — no behavior or readability concern worth a Nit.
- Pre-existing Prefect/rich teardown noise (`ValueError: I/O operation on closed file`) does not appear in unit-test output; would be relevant only to integration runs which this task explicitly punts.

**VERDICT: PASS**
