# Feature Plan: Data dispatch uniformity + `data/` package tidy-up

## Summary
The `apps/memory/src/tree/data` module dispatches its source platforms ALMOST
uniformly, but with two warts. (1) The worker's `_ingest_sources` treats four
**Source variant**s as symmetric "batched variants" (Substack RSS/article, YouTube
RSS/video via `_BATCHED_VARIANTS`) yet special-cases `WebSource` through a bespoke
`asyncio.gather(*[ingest_url(...)])` block that re-uses the MCP **URL router**. (2)
The package layout is inconsistent: `core/` is a one-file package wrapping
`ingest.py`, and `conversation` / `file` sit as loose `*.py` + `*_pipeline.py` files
at the `data/` root while every other source (substack, youtube, web, huggingface) is
a package. This feature makes web the LAST batched variant (dropping the special-case),
flattens `core/` → `data/ingest.py`, and module-izes `conversation` + `file` to match
the `web/` convention. One behavior delta (explicit `web` config entries no longer get
per-domain `ingest_url` re-routing in the data path); everything else is pure
mechanical refactor. Three atomic, independently-shippable, separately-committed tasks.

## Locked design (decisions are final — do not relitigate)

1. **Web = the last batched variant.** Append
   `_BatchedVariant(WebSource, "ingest_web_url_batch", "Web", "URLs", "web")` as the
   FIFTH/last entry of `_BATCHED_VARIANTS`, wired to the already-existing
   `ingest_web_url_batch` batch sub-flow. Delete the bespoke web `gather(ingest_url ...)`
   block + the now-dead `ingest_url` import from `pipeline.py`. Ingestion + log order
   becomes Substack RSS → Substack article → YouTube RSS → YouTube video → Web.

2. **The `ingest_url` URL router stays UNCHANGED and is NOT unified with the data
   path.** It remains the MCP single-URL dispatcher only. Unifying the two routing
   tables (router vs `isinstance` batched-variant dispatch) was considered and REJECTED
   as out of scope. Three distinct dispatch mechanisms coexist by design: config-load
   type inference (`_normalize_untyped_entry`), the data pipeline's `isinstance`
   batched-variant dispatch, and the runtime per-URL **URL router**.

3. **Behavior delta (the only one): explicit `web` entries skip per-domain
   re-routing.** Routing the `custom`-Platform shard through `ingest_web_url_batch`
   (instead of per-URL `ingest_url`) means an explicitly `web`-typed config entry goes
   STRAIGHT to the generic web (Bright Data) pipeline. End-to-end platform ORDERING is
   preserved because untyped raw URLs are already mapped to the right variant at config
   load by `SourcesConfig._normalize_untyped_entry` (with `web` as the `else`
   catch-all). This delta is captured in #075's Scope + the glossary — NO ADR (see
   Documentation updates).

4. **`core/` flattens to `data/ingest.py`.** `git mv` `core/ingest.py` → `ingest.py`,
   delete `core/__init__.py` + the empty dir. Filename stays `ingest.py`; module path
   becomes `tree.data.ingest`. Zero behavior change.

5. **`conversation` + `file` become packages, mirroring `web/`.** The convention is
   `<module>/<module>.py` + `<module>/<module>_pipeline.py` + `__init__.py` (exactly
   `web/web.py` + `web/web_pipeline.py`). Modules become
   `tree.data.conversation.conversation`, `tree.data.file.file`, etc.

6. **No deployment-topology change anywhere.** `conversation_pipeline` /
   `file_pipeline` are commented out in `orchestrator.py` under the Prefect free-tier
   5-deployment cap, so `_DEPLOYMENT_SPECS` is untouched by #077 (their commented-out
   imports are still updated so a future re-enable is clean). #075/#076 don't touch
   deployments either.

7. **Dependency order 075 → 076 → 077.** #075 removes `pipeline.py`'s
   `core.ingest` import, shrinking #076's repoint blast radius; #076 and #077 are
   otherwise independent mechanical moves. Each task is independently shippable and
   committed separately.

8. **Acceptance gate per task.** `make memory-format-fix && make memory-lint-fix &&
   make memory-format-check && make memory-lint-check`, `make pre-commit`, and `make
   memory-unit-tests` all clean; plus `make memory-integration-tests` (fast tail)
   because all three tasks touch the data layer and/or MCP imports. #077 additionally
   carries a deferred `[HUMAN]` live smoke (mirroring #074), with the automated gate as
   the real bar. Tests run ONLY via `make memory-*` targets on the LOCAL env (the
   Makefile loads `.env`).

## Caveats (state them — they are NOT blockers)

- **The web special-case removal touches more unit tests than the brief enumerated.**
  `test_pipeline.py` has four web-via-`ingest_url` tests
  (`test_dispatches_each_variant`, `test_dispatches_each_web_entry_via_ingest_url`,
  `test_filters_none_results_from_web_dispatcher`, `test_skips_web_when_no_web_entries`)
  that must be reworked to `ingest_web_url_batch` (batched, last). #075's Test guidance
  enumerates each.
- **`core/` flatten has a FOURTH importer the brief didn't list.** The URL-router unit
  suite `tests/unit/data/core/test_ingest.py` references `tree.data.core.ingest` in
  ~20 patch strings + its import block + a caplog logger name. #076 adds it to the
  repoint set (preferred: move the test up to `tests/unit/data/test_ingest.py` so the
  test layout mirrors the source). The decisive AC is `grep -rn "tree\.data\.core"
  apps/memory` → empty.
- **The `file` rename has a false-positive trap.** `tree.data.file.file` (the new
  module) vs `file_data.file_uri` / `Part.from_uri(file_uri=...)` (a Gemini-SDK
  attribute/kwarg in `test_gemini_transcript_fetcher.py`, NOT an import). A blind
  `tree.data.file` find-replace is UNSAFE. #077 calls this out with two precise grep
  ACs.
- **`ingest_url` is NOT fully removed — only its data-path use.** The MCP router and
  its dispatcher tests keep it; #075 removes only the worker's coupling, #076 only
  moves its module.

## Tasks (in order)

1. **075** — Web becomes the LAST batched variant — append
   `_BatchedVariant(WebSource, "ingest_web_url_batch", "Web", "URLs", "web")` (last) to
   `_BATCHED_VARIANTS`, import `ingest_web_url_batch` with `# noqa: F401`, delete the
   bespoke web `gather(ingest_url ...)` block + the `ingest_url` import from
   `pipeline.py`, fix the two docstrings, rework the web unit tests.
   (file: `tracker/075-web-as-last-batched-variant.groomed.md`)
2. **076** — Flatten `data/core/` → `data/ingest.py` — `git mv` `core/ingest.py` →
   `ingest.py`, delete `core/__init__.py` + the empty dir, repoint `mcp/tools.py`, the
   URL-router unit suite, and the two integration tests; `grep "tree.data.core"` →
   empty. Depends on #075.
   (file: `tracker/076-flatten-data-core-to-ingest.groomed.md`)
3. **077** — Module-ize `conversation` + `file` — `git mv` into
   `<module>/<module>.py` + `<module>/<module>_pipeline.py` packages with `__init__.py`,
   repoint internal imports, `mcp/tools.py`, the two commented-out `orchestrator.py`
   imports, a `web/web.py` docstring path, and the two unit-test patch-path sets. No
   deployment change. Depends on #076.
   (file: `tracker/077-modularize-conversation-and-file.groomed.md`)

## Out of scope (intentional)

- **Unifying the two routing tables.** The MCP `ingest_url` URL router and the data
  pipeline's `isinstance` batched-variant dispatch stay separate — owner's explicit
  rejection. Only the data path's web handling is touched.
- **Renaming `ingest.py`.** The owner wants the filename kept (`data/ingest.py`), not
  `url_router.py` or similar.
- **Changing `ingest_url` behavior.** The router's match order (static registry →
  custom Substack domain → generic-web fallback) is byte-for-byte preserved; #076 only
  moves the module.
- **Deployment topology / the free-tier cap.** `_DEPLOYMENT_SPECS` is unchanged;
  conversation/file remain un-deployed.
- **Per-platform `num_workers` / config-shape changes.** No `SourceEntry` model
  changes; the `_normalize_untyped_entry` config-load inference is unchanged (only
  referenced to explain why ordering is preserved).
- **Touching `file_uri` / `Part.from_uri` SDK usage.** Explicitly excluded from the
  `file` rename rewrite.

## Documentation updates (this grooming round)

- **Glossary** (`docs/glossary.md`): ADD one row, **URL router** — defining
  `ingest_url` as the MCP single-URL dispatcher (static registry → custom-Substack-
  domain → generic-web fallback), and noting it is DISTINCT from (a) config-load type
  inference (`_normalize_untyped_entry`) and (b) the data pipeline's `isinstance`
  batched-variant dispatch — three separate mechanisms, only the router is
  runtime-per-URL. Committed in the grooming commit; no other glossary terms restated
  or changed.
- **ADRs**: NO new ADR and NO ADR amendment. This is a pure refactor with a single,
  small, localized behavior delta (explicit `web` entries no longer per-domain
  re-routed in the data path), which is fully captured in #075's Scope + the new
  glossary row. ADR-001 and ADR-002 are unaffected — ADR-002 §3 already documents the
  group-by-Platform data fan-out, which is UNCHANGED here (this only alters the
  worker's intra-shard dispatch, not the orchestrator's partition). If a reviewer later
  judges the behavior delta ADR-worthy, hand it back as a PROPOSAL — but the
  recommendation is "no ADR".

## Open questions
- None blocking. Every decision (web-as-last-batched-variant + the dropped
  special-case; `ingest_url` router stays + tables not unified; the single behavior
  delta + why ordering is preserved; `core/` → `data/ingest.py` keeping the filename;
  the `<module>/<module>.py` convention for conversation/file; no deployment change;
  075→076→077 order) is pinned in the locked design above. The only SWE-discretion item
  is whether #076 moves `test_ingest.py` up or rewrites it in place — both satisfy the
  "no `tree.data.core` survives" AC.
