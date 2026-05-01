# `tree.data.web` ingestion core + Prefect flows

Status: pending
Tags: `data-pipeline`, `web`, `bright-data`, `prefect`
Depends on: #001
Blocks: #003, #004

## Scope

Build the actual data pipeline source on top of the Web Unlocker client from #001. Two layers, mirroring `tree.data.substack.substack_article` + `tree.data.substack.substack_article_pipeline`:

### Core (`apps/memory/src/tree/data/web/web.py`)

```python
async def fetch_and_extract_web(url: str) -> Document:
    """Fetch the URL via Bright Data Web Unlocker (markdown) and build a Document.

    Title heuristic (in order):
      1. The first markdown H1 line (`# ...`) if present.
      2. The Open Graph / <title> if the response is HTML (only used when we fall
         back to data_format="html"; v1 sticks to markdown).
      3. The URL's path tail (last non-empty path segment), title-cased.
    Summary heuristic: the first 300 characters of the markdown body, single-line.
    Content: the full markdown text returned by the Web Unlocker, verbatim.
    Date: datetime.now(tz=UTC) — the Web Unlocker does not return a publish date,
          and we do not parse the page for one in v1.
    Authors: ["Unknown"] — v1 does not extract author metadata.

    Returns a fully-populated Document with source_type=SourceType.WEB and
    source_uri=url, NOT yet persisted.
    """

async def load_web_document(doc: Document) -> Document | None:
    """Persist a single web Document with idempotent upsert semantics.

    - find_one by source_uri.
    - If found and source_type != LATENT, return None (duplicate, skip).
    - If found and source_type == LATENT, upgrade in place to WEB and return.
      (Mirrors tree.data.file.load_file_document — keeps LATENT promotion semantics.)
    - Otherwise insert; on DuplicateKeyError race, return None.
    """
```

### Prefect layer (`apps/memory/src/tree/data/web/web_pipeline.py`)

```python
@task(name="fetch-and-extract-web", retries=2, retry_delay_seconds=5)
async def fetch_and_extract_web_task(url: str) -> Document: ...

@task(name="load-web-document", retries=1, retry_delay_seconds=2)
async def load_web_document_task(doc: Document) -> Document | None: ...

@flow(name="ingest-web-url-etl", log_prints=True)
async def ingest_web_url(url: str) -> Document | None:
    """Single-URL ingestion. Assumes MongoDB is initialised by caller."""

@flow(name="ingest-web-url-batch-etl", log_prints=True)
async def ingest_web_url_batch(urls: list[str]) -> list[Document]:
    """Batch ingestion. Initialises MongoDB itself (top-level entry point).
       Uses asyncio.gather over per-URL ingest_web_url calls."""
```

The retry/concurrency/`init_mongodb` patterns must exactly match `tree.data.substack.substack_article_pipeline` (read it as the reference implementation).

### Idempotency rules (carry over from existing pipelines)

- The unique `(source_type, source_uri)` index on `Document` is the source of truth — re-running `ingest_web_url("https://x.com")` twice must produce one document and return `None` on the second call.
- `find_one` first, then `insert` with `DuplicateKeyError` caught — never use `replace_one(upsert=True)` because that would silently mutate documents already promoted by other pipelines.

### What this task does NOT do

- Does NOT touch the URL dispatcher (#003 does).
- Does NOT add `app_config.sources.urls`, `ingest_all_data` wiring, or Make targets (#004 does).
- Does NOT register the Prefect deployment in `tree.orchestrator` (#004 does).

## Acceptance Criteria

- [x] New module `tree.data.web.web` exposes `fetch_and_extract_web(url) -> Document` and `load_web_document(doc) -> Document | None`.
- [x] New module `tree.data.web.web_pipeline` exposes `ingest_web_url(url) -> Document | None` and `ingest_web_url_batch(urls) -> list[Document]` as Prefect flows.
- [x] `fetch_and_extract_web` returns a `Document` with `source_type == SourceType.WEB`, `source_uri == url`, non-empty `content`, non-empty `title`, `date.tzinfo is timezone.utc`.
- [x] Title falls back to the URL path tail when no H1 is present in the markdown — verified by unit test.
- [x] `load_web_document` returns `None` for a duplicate (existing non-LATENT document) — verified by unit test.
- [x] `load_web_document` upgrades a `LATENT` document in place — verified by unit test (mirror `tree.data.file` test pattern).
- [x] `load_web_document` catches `DuplicateKeyError` from a race condition and returns `None` — verified by unit test.
- [x] `ingest_web_url_batch` calls `init_mongodb` exactly once before processing URLs — verified by unit test (mock).
- [x] `ingest_web_url_batch` returns only successfully-ingested (non-None) documents — verified by unit test where some URLs are duplicates.
- [x] Both flow names are `ingest-web-url-etl` and `ingest-web-url-batch-etl` (kebab case, matches existing convention).
- [x] All datetimes are timezone-aware UTC (no `datetime.now()` without `tz=`).
- [x] All public functions have full type annotations including return types.
- [x] `make memory-format-check && make memory-lint-check && make memory-unit-tests` pass.
- [x] Unit tests at `apps/memory/tests/unit/data/web/test_web.py` and `apps/memory/tests/unit/data/web/test_web_pipeline.py` (mirroring the source layout); mock `httpx`/`Document.find_one`/`Document.insert` — no real network, no real MongoDB.

## User Stories

### Story: Developer ingests a single URL on demand
1. Developer ensures Bright Data credentials are set, MongoDB is running (`make local-start`), and `make memory-serve-workflows` is running.
2. Developer opens a Python REPL: `uv --directory apps/memory run python`.
3. Developer runs:
   ```python
   import asyncio
   from tree.config.settings import settings
   from tree.db import init_mongodb
   from tree.data.web.web_pipeline import ingest_web_url

   asyncio.run(init_mongodb(
       settings.mongo.mongo_uri.get_secret_value(),
       settings.mongo.mongo_initdb_database,
   ))
   doc = asyncio.run(ingest_web_url("https://martinfowler.com/articles/microservices.html"))
   print(doc.title, len(doc.content))
   ```
4. The first run prints the article title and a content length > 1000.
5. The second run prints `None None` (it does not raise; the duplicate is detected and returned as None).

### Story: Developer ingests several URLs at once via the batch flow
1. Developer runs:
   ```python
   from tree.data.web.web_pipeline import ingest_web_url_batch
   docs = asyncio.run(ingest_web_url_batch([
       "https://martinfowler.com/articles/microservices.html",
       "https://martinfowler.com/bliki/CQRS.html",
   ]))
   print(len(docs))
   ```
2. Developer sees `2` (both ingested) on first run.
3. Developer re-runs and sees `0` (both deduplicated).

### Story: Tester verifies idempotency at the MongoDB level
1. Tester runs the single-URL ingest twice on the same URL.
2. Tester runs `mongosh` and `db.documents.countDocuments({source_type: "web", source_uri: "<url>"})`.
3. Tester sees `1`.

### Story: Tester triggers a Bright Data outage at the HTTP layer
1. Tester points `BRIGHTDATA_API_KEY` at an invalid value.
2. Tester runs `ingest_web_url("https://example.com")`.
3. The Prefect task retries twice (per `retries=2`); each retry surfaces a `BrightDataRequestError` in the Prefect logs.
4. After exhausting retries, the flow finishes with `Failed` state. No partial Document is persisted (verifiable via `mongosh`).

### Story: SWE inspects the persisted Document
1. Developer runs `ingest_web_url("https://lethain.com/staff-engineer/")`.
2. Developer runs `mongosh` and `db.documents.findOne({source_uri: "https://lethain.com/staff-engineer/"})`.
3. The document has `source_type: "web"`, a non-empty `title`, a non-empty markdown `content`, `date` as a BSON `Date` (UTC), `authors: ["Unknown"]`.

---

Blocked by: #001

## Log

### [SWE] 2026-04-30 14:00 — Implementation

**Files modified**
- `apps/memory/src/tree/data/web/web.py` — new module: `fetch_and_extract_web` (markdown via Web Unlocker → Document) and `load_web_document` (idempotent upsert with LATENT promotion + DuplicateKeyError race handling); private helpers `_derive_title` (H1 → URL path tail) and `_derive_summary` (300-char single-line).
- `apps/memory/src/tree/data/web/web_pipeline.py` — new module: Prefect `@task` wrappers (`fetch-and-extract-web` retries=2/delay=5s, `load-web-document` retries=1/delay=2s) and the two flows `ingest-web-url-etl` and `ingest-web-url-batch-etl`. Batch flow calls `init_mongodb` once, then `asyncio.gather` over per-URL flows. Mirrors `tree.data.substack.substack_article_pipeline` exactly.
- `apps/memory/tests/unit/data/web/test_web.py` — unit tests for title heuristic, summary heuristic, fetch_and_extract_web (mocks `fetch_url`), and load_web_document (insert / non-LATENT skip / LATENT promote / DuplicateKeyError race; parametrised across all non-LATENT source types).
- `apps/memory/tests/unit/data/web/test_web_pipeline.py` — unit tests asserting task/flow names + retry config, task wrappers, single flow, and batch flow (init_mongodb-called-once, duplicate filtering, empty list).

**Tests**
- Unit: 350 passing, 0 failing, 0 warnings — `make memory-unit-tests` (full memory suite). 31 of those are new under `tests/unit/data/web/test_web.py` (20) and `tests/unit/data/web/test_web_pipeline.py` (11).
- Integration: N/A — no infra changes; per the task's "out of scope" list, orchestrator registration / Make targets / dispatcher wiring are #003 and #004.

**Acceptance criteria**
- [x] `tree.data.web.web` exposes `fetch_and_extract_web` + `load_web_document` — verified by `tests/unit/data/web/test_web.py::TestFetchAndExtractWeb::*` and `TestLoadWebDocument::*`.
- [x] `tree.data.web.web_pipeline` exposes `ingest_web_url` + `ingest_web_url_batch` as Prefect flows — verified by `test_web_pipeline.py::TestTaskAndFlowMetadata::test_flow_names`.
- [x] `fetch_and_extract_web` returns a Document with `source_type==WEB`, `source_uri==url`, non-empty content/title, UTC tzinfo — verified by `test_web.py::TestFetchAndExtractWeb::test_returns_document_with_web_source`.
- [x] Title falls back to URL path tail without H1 — verified by `test_web.py::TestDeriveTitle::test_url_path_tail_title_cased` + `TestFetchAndExtractWeb::test_falls_back_to_url_path_tail`.
- [x] `load_web_document` returns `None` for non-LATENT duplicate — verified by `test_web.py::TestLoadWebDocument::test_returns_none_for_non_latent_duplicate` + `TestLoadWebDocumentReturnType` parametrised.
- [x] `load_web_document` upgrades LATENT in place — verified by `test_web.py::TestLoadWebDocument::test_upgrades_latent_document_in_place`.
- [x] DuplicateKeyError race returns `None` — verified by `test_web.py::TestLoadWebDocument::test_returns_none_on_duplicate_key_race`.
- [x] `ingest_web_url_batch` calls `init_mongodb` exactly once — verified by `test_web_pipeline.py::TestIngestWebUrlBatch::test_initialises_mongodb_once` (asserts `mock_init.assert_awaited_once()`).
- [x] Batch returns only non-None docs — verified by `test_web_pipeline.py::TestIngestWebUrlBatch::test_filters_out_duplicates`.
- [x] Flow names `ingest-web-url-etl` / `ingest-web-url-batch-etl` — verified by `test_web_pipeline.py::TestTaskAndFlowMetadata::test_flow_names` and runtime smoke (`ingest_web_url.name == "ingest-web-url-etl"`).
- [x] All datetimes timezone-aware UTC (`datetime.now(tz=UTC)`) — only callsites are in `web.py`, both use `datetime.now(tz=UTC)`. Verified by tzinfo assertions in `test_web.py::TestFetchAndExtractWeb::test_returns_document_with_web_source` and `TestLoadWebDocument::test_upgrades_latent_document_in_place`.
- [x] All public functions fully type-annotated — `fetch_and_extract_web`, `load_web_document`, `ingest_web_url`, `ingest_web_url_batch`, plus tasks. Verified by ruff (`memory-lint-check` clean) and inspection.
- [x] `make memory-format-check && make memory-lint-check && make memory-unit-tests` pass — see Evidence.
- [x] Unit tests live at `apps/memory/tests/unit/data/web/test_web.py` and `.../test_web_pipeline.py`; only `tree.data.web.web.fetch_url`, `Document.find_one`, `Document.insert` mocked — no real network, no real MongoDB.

**Evidence**
```
$ make memory-format-check
uv run ruff format --check src/ tests/ scripts/ deploy/
132 files already formatted

$ make memory-lint-check
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make memory-unit-tests
... (350 passed in 19.10s, 0 warnings) ...
tests/unit/data/web/test_web.py ....................                     [ 40%]
tests/unit/data/web/test_web_pipeline.py ...........                     [ 43%]
tests/unit/data/web/test_web_unlocker.py ..................              [ 48%]
...
============================= 350 passed in 19.10s =============================

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ uv --directory apps/memory run python -c "from tree.data.web.web_pipeline import *; print(ingest_web_url.name, ingest_web_url_batch.name)"
ingest-web-url-etl ingest-web-url-batch-etl
```

**Notes**
- Task / flow retry metadata mirrors `substack_article_pipeline` exactly: fetch task `retries=2 delay=5`, load task `retries=1 delay=2`. Verified by `test_web_pipeline.py::TestTaskAndFlowMetadata`.
- `_derive_title` host fallback (`Example.Com`) is reached only when the URL has no path segments at all (e.g. `https://example.com/`); spec said "URL path tail title-cased" and is silent on rootless URLs, so falling through to the host keeps `title` non-empty (required by AC).
- Out of scope (untouched, per task spec): `tree.data.core.ingest`, `app_config.sources`, `tree.orchestrator`, root/app `Makefile`. These belong to #003 / #004.
- No new dependencies — uses existing `pymongo`, `prefect`, `httpx` (transitively via `web_unlocker`).
- `--- Logging error ---` lines that appear after the pytest summary are Prefect's known noise from shutting down its temporary in-memory server post-session; they do not affect test outcomes (350 passed, 0 warnings).
- DO NOT COMMIT — handing off to Tester.

### [Tester] 2026-04-30 21:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (all hooks green: prettier, ruff check, ruff format, biome check)
- Unit tests: 350 passed / 0 failed (`make memory-unit-tests`)
- Integration tests: 57 passed / 0 failed (`make memory-integration-tests`, 68.35s)
- Warnings: 0 (re-ran with `2>&1 | grep -i -E "warn|error|logging"` — no matches; the SWE's "Logging error" did not reproduce)

**E2E adversarial pass**
- Happy path: `uv --directory apps/memory run python -c "from tree.data.web.web_pipeline import ingest_web_url, ingest_web_url_batch, fetch_and_extract_web_task, load_web_document_task; print(ingest_web_url.name, ingest_web_url_batch.name, fetch_and_extract_web_task.name, load_web_document_task.name, fetch_and_extract_web_task.retries, fetch_and_extract_web_task.retry_delay_seconds, load_web_document_task.retries, load_web_document_task.retry_delay_seconds)"` → `ingest-web-url-etl ingest-web-url-batch-etl fetch-and-extract-web load-web-document 2 5 1 2` (PASS — names + retries match spec exactly, mirror substack_article_pipeline)
- Break path 1 (boundary: title heuristic edges):
  - H2 followed by H1 → `'Real H1 Title'` (regex correctly skips H2, picks first H1) — PASS
  - `# # Real Title # extra` → `'Real Title # extra'` (leading hashes stripped, trailing left alone — acceptable) — PASS
  - `https://example.com/` (no path) → `'Example.Com'` (host fallback) — PASS
  - `https://example.com/blog/post?foo=bar#section` → `'Post'` (urlparse correctly drops query/fragment) — PASS
  - `https://example.com/Foo%20Bar` → `'Foo%20Bar'` (NOT decoded — note below) — PASS with note
  - H1 inside fenced code block → still picked as title (no fence-awareness — note below) — PASS with note
- Break path 2 (boundary: summary heuristic edges):
  - 300-char body → 300 char summary; 301 → 300; 299 → 299 — PASS
  - 200 emojis (UTF-8) → 200 chars (Python char-based slice, not byte-based) — PASS
  - Bullet list → whitespace collapsed to single line — PASS
  - H1 line included in summary (e.g. `# Title body...`) — PASS with note (slightly redundant since H1 is also `title`, but spec doesn't require stripping)
- Break path 3 (state edge: idempotency / LATENT promotion):
  - Non-LATENT skip parametrized across `[SUBSTACK, FILE, HUGGINGFACE, WEB]` (`TestLoadWebDocumentReturnType`) — PASS
  - LATENT promotion test verifies `source_type` mutates to WEB in place AND `replace()` is awaited (real upgrade, not delete-and-insert) — PASS
  - DuplicateKeyError race test mocks `find_one→None` then `insert→DuplicateKeyError` (correct race simulation) — PASS
  - LATENT promotion path also refreshes `summary`, `authors`, `date` — slightly more aggressive than `tree.data.file.load_file_document` (which only refreshes `title`/`content`/`date`) but reasonable for richer web metadata
- Break path 4 (state edge: batch flow):
  - Empty list `[]` → `init_mongodb` called exactly once, returns `[]` (verified live via `ingest_web_url_batch.fn([])`) — PASS
  - Duplicate URLs in input `["https://x", "https://x"]` with one `None` and one doc returned → `len(result) == 1` (None correctly filtered after gather) — PASS
  - Implementation uses `asyncio.gather` (concurrent), matches spec — PASS
- Break path 5 (datetime hygiene): `grep -rn "datetime.now\|datetime\.utcnow" apps/memory/src/tree/data/web/` → only two callsites in `web.py:93` and `web.py:115`, both `datetime.now(tz=UTC)`. No naive datetimes. — PASS
- Break path 6 (Document construction): `authors=["Unknown"]` validates; `source_uri` is plain `str` (no URL validation in entity, by design); all required fields populated. — PASS

**Acceptance criteria**
- [x] PASS — `tree.data.web.web` exposes `fetch_and_extract_web` + `load_web_document` — `apps/memory/src/tree/data/web/web.py:72,97`; tests at `tests/unit/data/web/test_web.py::TestFetchAndExtractWeb::*` and `::TestLoadWebDocument::*`
- [x] PASS — `tree.data.web.web_pipeline` exposes `ingest_web_url` + `ingest_web_url_batch` as Prefect flows — `apps/memory/src/tree/data/web/web_pipeline.py:38,53`; `test_web_pipeline.py::TestTaskAndFlowMetadata::test_flow_names`; live verified `ingest_web_url.name == "ingest-web-url-etl"`
- [x] PASS — `fetch_and_extract_web` Document has `source_type==WEB`, `source_uri==url`, non-empty content/title, UTC tzinfo — `test_web.py::TestFetchAndExtractWeb::test_returns_document_with_web_source` (line 84-103)
- [x] PASS — Title falls back to URL path tail without H1 — `test_web.py::TestDeriveTitle::test_url_path_tail_title_cased` (line 33) + `TestFetchAndExtractWeb::test_falls_back_to_url_path_tail` (line 105)
- [x] PASS — `load_web_document` returns `None` for non-LATENT duplicate — `test_web.py::TestLoadWebDocument::test_returns_none_for_non_latent_duplicate` (line 169) + parametrized `TestLoadWebDocumentReturnType::test_skips_any_non_latent_existing` across 4 source types
- [x] PASS — LATENT upgrade in place — `test_web.py::TestLoadWebDocument::test_upgrades_latent_document_in_place` (line 185); verifies `source_type` mutates to WEB and `replace()` awaited
- [x] PASS — DuplicateKeyError race returns `None` — `test_web.py::TestLoadWebDocument::test_returns_none_on_duplicate_key_race` (line 213)
- [x] PASS — `ingest_web_url_batch` calls `init_mongodb` exactly once — `test_web_pipeline.py::TestIngestWebUrlBatch::test_initialises_mongodb_once` (line 125, `assert_awaited_once`); also reproduced live with empty URL list (still called once)
- [x] PASS — Batch returns only non-None docs — `test_web_pipeline.py::TestIngestWebUrlBatch::test_filters_out_duplicates` (line 151); reproduced live with duplicate URLs
- [x] PASS — Flow names `ingest-web-url-etl` / `ingest-web-url-batch-etl` — `test_web_pipeline.py::TestTaskAndFlowMetadata::test_flow_names` (line 43); live runtime check confirmed
- [x] PASS — All datetimes timezone-aware UTC — `grep` shows only two `datetime.now()` callsites in `web.py`, both `tz=UTC`; tzinfo asserted in two tests
- [x] PASS — All public functions fully type-annotated — verified by `make memory-lint-check` (clean) and inspection of `web.py` + `web_pipeline.py`
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-unit-tests` pass — see Evidence
- [x] PASS — Unit tests at correct paths, mocking `httpx`/`Document.find_one`/`Document.insert` — `tests/unit/data/web/test_web.py` + `tests/unit/data/web/test_web_pipeline.py`; only `tree.data.web.web.fetch_url`, `Document.find_one`, `Document.insert` mocked

**Evidence**
```
$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
============================= 350 passed in 20.76s =============================
(0 warnings; grep "warn|error|logging" → no matches)

$ make memory-integration-tests
======================== 57 passed in 68.35s (0:01:08) =========================

$ git diff feat/bright-data-fallback-source~1 -- apps/memory/src/tree/data/core/ingest.py apps/memory/src/tree/orchestrator.py apps/memory/src/tree/config/app_config.py
(empty — out-of-scope files genuinely untouched)
```

**Other issues found (not AC-blocking, follow-up candidates)**
- URL-encoded path tails are not decoded by `_derive_title` (e.g. `Foo%20Bar` stays as `Foo%20Bar`). Not in spec; minor cosmetic issue for titles derived from real-world URLs with encoded characters. Suggested follow-up: `from urllib.parse import unquote` then `unquote(tail)` before title-casing.
- `_H1_RE` is fence-unaware: a `# ...` line inside a fenced code block will be picked as the H1. Spec doesn't require fence handling and the fallback is still acceptable, but worth a follow-up if real-world articles trip it.
- The summary includes the H1 line itself (e.g. `# The Title This is the body.`); the title is already exposed separately as `Document.title`. Worth considering stripping the first H1 from the summary to avoid duplication.
- `__init__.py` re-exports only `web_unlocker` symbols, not `fetch_and_extract_web`/`ingest_web_url`. Consistent with `tree.data.substack` (whose `__init__.py` is empty), so callers import from leaf modules — fine.
- Note for #003/#004: LATENT promotion path also overwrites `summary` and `authors`, which is more aggressive than `tree.data.file.load_file_document` (which only refreshes `title`/`content`/`date`). Reasonable for richer web metadata; flagging only so reviewers know.

**VERDICT: PASS**

