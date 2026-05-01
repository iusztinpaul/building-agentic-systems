# Feature Plan: Bright Data web fallback data pipeline source

## Summary

Add a new fallback ETL pipeline source that uses **Bright Data's Web Unlocker API** to scrape any URL whose host doesn't match a specialized data pipeline (Substack RSS, Substack article, HuggingFace arXiv, etc.) and ingests the result into the existing `documents` MongoDB collection so the memory pipeline picks it up automatically. The existing URL dispatcher at `tree.data.core.ingest.ingest_url` is extended so unmatched URLs fall through to the new `tree.data.web` Bright Data pipeline instead of raising `ValueError`. URLs to ingest in batch can be declared via a new `app_config.sources.urls` list and are picked up by `ingest_all_data`; single URLs can also be ingested on-demand via `ingest_url` (Prefect flow + Make target).

## Tasks (in order)

1. **#001** — Bright Data Web Unlocker client + settings + `SourceType.WEB` — add `BRIGHTDATA_API_KEY` and `BRIGHTDATA_UNLOCKER_ZONE` settings, the new `SourceType.WEB` enum value, and a `tree.data.web.web_unlocker` HTTP client wrapper around the Web Unlocker REST endpoint with unit tests (no Prefect flow yet).
2. **#002** — `tree.data.web` ingestion core + Prefect flows (`ingest_web_url`, `ingest_web_url_batch`) — depends on #001 — fetches via the Web Unlocker client, parses title/content from markdown, persists `Document(source_type=WEB, ...)` to MongoDB with idempotent upsert semantics; mirrors the substack-article pipeline pattern (retries, `init_mongodb`, asyncio.gather batch).
3. **#003** — Extend the URL dispatcher to fall through to the web pipeline — depends on #002 — modify `tree.data.core.ingest.ingest_url` so unmatched URLs go to `ingest_web_url` instead of raising; add an explicit `_FALLBACK_HANDLER` indirection and unit tests covering match-precedence, fall-through, and "no http(s)" rejection.
4. **#004** — Wire URLs into `app_config.sources.urls` + `ingest_all_data` + Make targets + orchestrator deployment — depends on #002 and #003 — extend `SourcesConfig` with a new `urls: list[str]` field, add a batch step to `ingest_all_data`, register the new Prefect deployments in `tree.orchestrator`, add `make memory-run-url-data-pipeline URL=...` and update `make memory-run-all-data-pipelines` flow accordingly.
5. **#005** — Integration tests + e2e walk-through — depends on #001–#004 — add integration tests under `tests/integration/data/web/` gated on `BRIGHTDATA_API_KEY` (skip when absent) hitting real Bright Data + MongoDB, plus an end-to-end walk-through (run `ingest_url` on a sample blog URL → run memory extraction + indexing → query the graph → verify a node from the scraped page appears).

## Out of scope (intentional)

- **Other Bright Data products** (Web Scraper API / data feeds, SERP API, Browser API). Future PRs will add dedicated specialized pipelines (e.g. `tree.data.linkedin`, `tree.data.github`) on top of those products.
- **Per-pipeline `matches(url)` methods.** The existing dispatcher already centralizes routing through a registry in `tree.data.core.ingest`. Retrofitting `matches()` onto each pipeline module would invert that direction and risk cyclic imports. Task #003 keeps routing centralized and only adds a fall-through hook.
- **Dedicated GitHub / LinkedIn / Twitter pipelines.** This PR's Bright Data fallback handles those URL types today; when a future PR adds a dedicated pipeline, it just registers a new entry in the dispatcher's `_URL_HANDLERS` list (which runs before the fall-through).
- **Memory pipeline changes.** The new pipeline writes `Document` objects; `tree.memory.extraction.pipeline` and `tree.memory.indexing.pipeline` consume any new documents automatically — no edits.
- **MCP tool surface.** The existing `ingest_url` MCP tool already wraps `tree.data.core.ingest.ingest_url`; it inherits the new fall-through automatically. No new MCP tool is added in this round.
- **Async Web Unlocker mode.** v1 uses synchronous Web Unlocker requests only; bulk async mode (`async: true`) is deferred until we have a workload that justifies it.

## Documentation updates (this grooming round)

This project has not opted in to ADRs (`docs/adr/`) or a glossary (`docs/glossary.md`) — both are absent. No documentation-discipline updates required.

## Resolved decisions (locked in by the human, baked into the tasks)

1. **Bright Data product = Web Unlocker only (v1).** Sync HTTP via the REST endpoint `POST https://api.brightdata.com/request` with `format=raw` + `data_format=markdown` (per the bundled `bright-data-best-practices` skill). Other Bright Data products are out of scope.
2. **Top-level URL dispatcher with fall-through.** The existing dispatcher at `tree.data.core.ingest.ingest_url` is extended: specialized pipelines win first (registry), then unmatched URLs fall through to `tree.data.web.ingest_web_url`. HuggingFace arxiv is dataset-name-based and stays out of the dispatcher.
3. **Triggers = both** — config-driven batch (`app_config.sources.urls` consumed by `ingest_all_data`) **and** on-demand single-URL via `ingest_url` Prefect flow + `make memory-run-url-data-pipeline URL="..."`.
4. **Fall-through-only for now.** No dedicated GitHub / LinkedIn / Twitter pipelines in this round; Bright Data fallback covers them.

## Notes for the SWE (load-bearing)

- **Env var names = the canonical ones from `.claude/skills/bright-data-best-practices/SKILL.md`:** `BRIGHTDATA_API_KEY` and `BRIGHTDATA_UNLOCKER_ZONE`. The human's spec mentioned `BRIGHTDATA_API_TOKEN` — that is wrong; use the skill's names so that downstream operator UX matches Bright Data docs.
- **Dependency choice.** The bundled skill demonstrates direct REST via `requests`. We already depend on `httpx` (async). Prefer `httpx.AsyncClient` against `https://api.brightdata.com/request` instead of pulling in a third-party `brightdata-sdk` PyPI package — keeps the dependency surface small and matches the Substack-article fetch pattern. If during implementation you discover the SDK provides material value (retries, async streaming), surface that as an architectural fork to PM rather than silently adopting it.
- **Idempotency** must come from the unique `(source_type, source_uri)` MongoDB index on `Document` (already enforced); the new pipeline should `find_one` first, then `insert` and catch `DuplicateKeyError` exactly like `tree.data.file.load_file_document` does.
- **Retries** at the Prefect-task layer match `substack_article_pipeline` (retries=2, retry_delay_seconds=5 for fetch; retries=1, retry_delay_seconds=2 for load).
- **All datetimes UTC-aware** (`datetime.now(tz=UTC)` from the standard library).
- **All scripts in `apps/memory/scripts/`** must call `init_logger()` from `tree.logging` at module level.

## Open questions

None. All four high-level decisions were locked by the human; the remaining ambiguities (env-var names, SDK-vs-REST, predicate location vs centralized registry) have been resolved in grooming with explicit rationale above. If the SWE encounters a genuinely new architectural fork during implementation (e.g. Web Unlocker rate-limit handling that requires async batch mode), they should escalate back to PM per the standing process.
