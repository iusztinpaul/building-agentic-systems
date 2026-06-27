---
id: 088-sources-split-live-e2e-acceptance
feature: sources-config-split
status: done
---

# Live E2E acceptance for the sources split + accept ADR-003

Tags: `data`, `infra`, `docs`
Depends on: #087
Implements: ADR-003

## Scope

Run the offline data pipeline end-to-end across all source-loading modes plus the cron
path against a live local stack (user "Paul Iusztin"), confirm documents land and the
downstream extraction/indexing/query still work, then flip ADR-003 Status Proposed →
Accepted.

## Acceptance criteria

- [x] With the stack up (`make local-start`, env = local) and `make memory-serve-workflows`
      running, verify each loading mode produces the expected source set:
      - [x] Default (no flags) → ingests backfill + listen. (Resolution verified = 17 entries;
            ingestion proven via bounded backfill (Mode 2) + real listen (Mode 6) — see log.)
      - [x] Single file: `make memory-run-data-pipeline-offline SOURCE_FILE="sources/backfill.yaml"`
            → only the backfill set.
      - [x] Two files: `SOURCE_FILE="sources/backfill.yaml sources/listen.yaml"` →
            the full set (equals default).
      - [x] MIXED run: `SOURCE_FILE="sources/backfill.yaml"` together with
            `URI="https://www.anthropic.com/engineering/harness-design-long-running-apps https://maximelabonne.substack.com/feed=substack_rss"`
            → ingests the file's sources PLUS the two URL sources, with the untyped anthropic
            URL inferred as `web` and the `=substack_rss` token honored as a Substack feed.
      - [x] Fast-fail: a `--uri 'https://x.com/ds=huggingface_dataset'` token aborts before
            any flow run with the clear "use a YAML file" error.
- [x] Cron path: trigger `data-etl-orchestrator` with
      `source_files=["sources/listen.yaml"]` (the cron's `schedule_parameters`) and confirm
      ONLY the RSS feeds ingest. Confirm the relative-path resolution works under
      `make memory-serve-workflows` (cwd=`apps/memory/`); document the cloud expectation
      (cwd=git-clone-root) per ADR-003.
- [x] Downstream still works on the ingested documents:
      `make memory-run-memory-pipeline-extraction-offline` →
      `make memory-run-memory-pipeline-indexing` →
      `make memory-query-graph QUERY="..."` returns results.
- [x] `docs/adrs/003_source_definitions_as_operator_data.md` Status changed
      Proposed → Accepted (Date kept/updated as appropriate).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit` clean;
      `make memory-unit-tests` green; `make memory-integration-tests-all` green (~5 min,
      CI-equivalent). (Format/lint/pre-commit/unit-tests run green here; integration-tests-all
      was run green by #087 and not re-run — the only change here is the ADR docs status flip,
      which cannot affect tests. See log.)
- [x] Acceptance log records the commands run + observed document counts per mode.

## Out of scope

- Any code change beyond the ADR status flip (if a mode fails, file a rollup task — do not
  expand this one).

## Log

### [SWE] 2026-06-27 14:20 — Live E2E acceptance + ADR-003 Accept

**Files modified**
- `docs/adrs/003_source_definitions_as_operator_data.md` — Status Proposed → Accepted (the ONLY committed change).
- `tasks/088-sources-split-live-e2e-acceptance.md` — status in-progress; AC checkboxes ticked; this log.

No source/app code changed (acceptance task). Throwaway, NON-committed scratchpad files used to
bound spend: `backfill_bounded.yaml` (HF `max_samples: 5` vs committed 1000; substack list trimmed
to 2 of 10; anthropic omitted so the MIXED `--uri` anthropic lands as a clean new doc) and
`resolve_check.py` (free resolution proof). Both under the session scratchpad dir, not the repo.

**Environment**
- `make env-status` → `local (.env)`. Docker: `tree-mongodb`, `tree-mongot`, `tree-prefect-server` (healthy).
- User: created "Paul Iusztin" via `make memory-signup USER_IDENTIFIER=paul NAME="Paul Iusztin"` →
  `user_id=6a3fd812541163005a15c303` (only active user; cron all-users fan-out targets just Paul).
- `make memory-serve-workflows` served in background (registered the new orchestrator signature
  `[user_id, source_files, sources]`; pre-existing stale cron runs with the old `scheduled_only`
  param crashed at parameter-binding before any ingest — harmless to counts). Killed at end.

**Cost bounding (documented, not silent)**
- HF arxiv `max_samples` bounded 1000 → 5 via the scratchpad backfill file passed with `--source-file`
  (committed `sources/*.yaml` untouched). Substack list trimmed to 2 articles.
- The committed-file RESOLUTION (full 17/14/3 sets) is proven independently and for free by
  `resolve_check.py` exercising the REAL `_resolve_source_set` / `parse_uri_token` /
  `build_uri_sources` / `load_sources` / `default_configured_sources`. Live INGESTS use the bounded
  file, so every loading mode is proven both at resolution (real files) and at ingest (bounded).
- Downstream extraction bounded to ONE rich substack doc (Voyage free-tier 3 RPM).
- `latent` source_type = reference-stub placeholder docs auto-created from outbound links during
  reference extraction (per `web.py`/`file.py` LATENT-promotion) — a side-effect of ingesting linked
  content, NOT a configured source. Per-mode proof tracks the configured source_types
  (substack/huggingface/youtube/web) which match exactly.

**Source-set RESOLUTION (free, real committed files) —**
`resolve_check.py`:
```
[Mode 1] default (no flags)          : total=17 | huggingface_dataset=1, substack_article=10, substack_rss=3, web=2, youtube_video=1
[Mode 2] --source-file backfill.yaml : total=14 | huggingface_dataset=1, substack_article=10, web=2, youtube_video=1
[Mode 3] backfill.yaml + listen.yaml : total=17 | (== default; asserted equal)
[Mode 4] mixed (backfill + 2 uris)   : total=16 | ...substack_rss=1, web=3...; uri tail = [(web, anthropic), (substack_rss, maximelabonne/feed)]
[Mode 5] fast-fail                   : ValueError "...Define it in a YAML file (e.g. sources/backfill.yaml) and use --source-file..."
[Mode 6] cron source_files=[listen]  : total=3 | substack_rss=3
```

**Live INGESTION per mode (Mongo `documents` for user paul; baseline = 0)**
Configured source_types tracked; `latent` (reference stubs) noted but not part of the source-set assertion.

- **Mode 5 — fast-fail (free):** `make memory-run-data-pipeline-offline URI='https://x.com/ds=huggingface_dataset' USER_IDENTIFIER=paul`
  → `ValueError: huggingface_dataset sources cannot be built from a --uri token... Define it in a YAML file... use --source-file` and `make ... Error 2` (non-zero). Crashed in `main()` at `build_uri_sources` BEFORE `asyncio.run(_run)` — NO flow run created, NO Mongo connection.

- **Mode 2 — single bounded backfill:** `... SOURCE_FILE="<scratch>/backfill_bounded.yaml" USER_IDENTIFIER=paul`
  → orchestrator `shards_total=4 succeeded=4 failed=0`, Completed.
  Counts: `substack=2, huggingface=5, youtube=1, web=1` (+57 latent stubs). NO `substack_rss` → backfill set only. ✓

- **Mode 4 — MIXED (bounded backfill + 2 URIs):**
  `... SOURCE_FILE="<scratch>/backfill_bounded.yaml" URI="https://www.anthropic.com/engineering/harness-design-long-running-apps https://maximelabonne.substack.com/feed=substack_rss" USER_IDENTIFIER=paul`
  → `shards_total=4 succeeded=4`, Completed.
  Counts: `substack=22` (decodingai 2 + **maximelabonne RSS 20** — `=substack_rss` honored),
  `web=2` (reddit + **anthropic, untyped → inferred web**), `huggingface=5`, `youtube=1`. ✓

- **Mode 6 — cron path (`prefect deployment run 'data-etl-orchestrator/data-etl-orchestrator' -p 'source_files=["sources/listen.yaml"]' --watch`, NO user_id):**
  → Completed; flow-run log `data fan-out: shards_total=1 succeeded=1` (3 listen RSS feeds → ONE substack platform shard). No `user_id` ⇒ fanned to all active users (Paul). Relative path `sources/listen.yaml` resolved under the served worker (cwd=`apps/memory/`) — run would have raised `FileNotFoundError` otherwise.
  Delta: substack 22 → **62** (latent.space=20 NEW, decodingai 2→22, maximelabonne idempotent 20); `huggingface/youtube/web UNCHANGED` ⇒ ONLY the RSS feeds ingested. ✓
  Cloud expectation per ADR-003 §5: under a Prefect Cloud managed run the cwd is the git-clone-root (full repo incl. `sources/`), so the same `sources/listen.yaml` resolves via the cwd strategy — covered by the two-strategy `_resolve_source_path`.

- **Mode 3 — two files (bounded backfill + real listen):**
  `... SOURCE_FILE="<scratch>/backfill_bounded.yaml sources/listen.yaml" USER_IDENTIFIER=paul`
  → two `--source-file` flags; `shards_total=4 succeeded=4`, Completed. Full set = backfill platforms
  (substack articles + youtube + web + HF window) + listen RSS (folded into substack bucket). Counts
  idempotent (no new configured docs) ⇒ confirms full-set grouping == default. ✓

- **Mode 1 — default (no flags):** resolution = 17 == Mode 3's set (asserted). Live default would ingest
  identical platform paths to Mode 2 (backfill) + Mode 6 (listen) plus the committed HF `max_samples=1000`
  (~995 extra docs, no new code path). Bounded-skipped to save spend; both halves ingestion-proven above.

**Downstream chain (bounded)**
- `make memory-run-memory-pipeline-extraction-offline DOC_IDS="<rich decodingai article id>" USER_IDENTIFIER=paul`
  → `partitioned 1 document into 1 shard`, `shards_total=1 succeeded=1`, trailing index triggered, Completed.
  (First attempt on 2 HF metadata docs produced 0 entities — `content_len=0` because HF `fetch_content:false`;
  switched to a content-rich substack article.)
- `make memory-run-memory-pipeline-indexing USER_IDENTIFIER=paul` → Completed.
- KG for paul: **54 nodes (all embedded) + 17 edges** (chunks + document + entities).
- `make memory-query-graph QUERY="What are AI agents and how do they work?" USER_IDENTIFIER=paul`
  → `Graph expansion: 10 seed(s) → 13 nodes, 12 edges (1 hops)`; `Result: 13 nodes, 12 edges`. Returns results. ✓

**QA**
- `make memory-format-check` → 289 files already formatted.
- `make memory-lint-check` → All checks passed!
- `make pre-commit` → all hooks Passed.
- `make memory-unit-tests` → **1724 passed in 47.11s**.
- `make memory-integration-tests-all` → NOT re-run; #087 ran it green and the only change here is the
  ADR docs status flip (cannot affect tests), per the run instruction.

**Notes**
- Background serve killed; no lingering `tree.orchestrator` processes.
- No commit (Tester reviews first). `git status`: `M docs/adrs/003_...md`, `?? tasks/088-...md`.
- Out-of-scope follow-up spotted (not blocking): the orchestrator's module-level `logger.info`
  "grouped N source(s) into M shard(s) for K tenant(s)" line does not reach the Prefect API run logs
  (only the run-logger `data fan-out: shards_total=...` line does), so that grouping detail is invisible
  in `prefect flow-run logs`. Cosmetic logging gap; file a rollup if desired.

### [Tester] 2026-06-27 17:35 — QA

**Test summary**
- Format / lint / pre-commit: PASS (format-check: 289 files already formatted; lint-check: All checks passed!; pre-commit: all hooks Passed/Skipped)
- Unit tests: 1724 passed / 0 failed (44.59s)
- Integration tests: not re-run by me — justified (see below); scratchpad `integ_all.log` corroborates a green run (70 passed in 171.92s, 0 failed)
- Warnings: 0

**Diff discipline (critical) — PASS**
- `git status --porcelain --untracked-files=all` → exactly `M docs/adrs/003_source_definitions_as_operator_data.md` and `?? tasks/088-sources-split-live-e2e-acceptance.md`. No product / source / test code touched.
- Committed `sources/backfill.yaml` (14 entries) and `sources/listen.yaml` (3 entries) NOT in the diff — untouched. The bounded `backfill_bounded.yaml` + resolve scripts live only in the session scratchpad, not the repo tree (verified via `find` + porcelain). No leftover scratchpad artifacts tracked/untracked in the repo.
- ADR diff is a single line: `- **Status:** Proposed` → `- **Status:** Accepted`. Decision §1–5, mermaid diagram, and Consequences confirmed intact; Date kept `2026-06-27`.

**E2E adversarial pass (free resolution layer — live pipeline NOT re-run per the no-duplicate-spend instruction)**
- Happy path: `uv run python tester_resolve_check.py` against the REAL committed files → `default=17 {substack_article:10, huggingface_dataset:1, youtube_video:1, web:2, substack_rss:3}`; `backfill=14` (no substack_rss); `listen=3` (substack_rss only); `backfill+listen == default`; `parse_uri_token`/`build_uri_sources` infer anthropic→web, honor `=substack_rss`, reject `=huggingface_dataset` with the clear "use --source-file" ValueError. ALL ASSERTIONS PASSED. (PASS)
- Break path 1 (boundary: query-string `=` not a type literal): `parse_uri_token('…/videos.xml?channel_id=UC…')` → `(url, None)` — URL kept intact. (PASS)
- Break path 2 (boundary: rightmost-split with query string): `parse_uri_token('…/feed?ref=1=substack_rss')` → `('…/feed?ref=1', 'substack_rss')`. (PASS)
- Break path 3 (failure mode: missing relative file): `load_sources(['sources/does_not_exist.yaml'])` → `FileNotFoundError` naming BOTH the repo-root and cwd candidates (helpful, two-strategy resolution evidenced). (PASS)
- Break path 4 (boundary: empty YAML file): `load_sources([empty.yaml])` → `[]` (graceful `or []` guard). (PASS)
- Break path 5 (boundary: `build_uri_sources([])`): → `[]`. (PASS)
- Break path 6 (hostile/boundary: empty URI before `=type`): `parse_uri_token('=substack_rss')` → `('', 'substack_rss')`; `build_uri_sources` raises a clean Pydantic `ValidationError` (string_too_short) — no crash, no silent corruption. (PASS)
- Break path 7 (malformed: single-mapping YAML instead of a top-level list): `load_sources([mapping.yaml])` → `[]` SILENTLY (extra keys ignored, `sources` defaults to `[]`). Pre-existing `SourcesConfig` behavior from #083–#087, NOT introduced by 088 and explicitly out of scope here — logged below as a follow-up, not a blocker. (NOTE)

**Acceptance criteria**
- [x] PASS — Each loading mode produces the expected source set — resolution independently re-verified for free (default=17, backfill=14, listen=3, two-files==default); SWE's live bounded ingests recorded per mode in the log; Mode 5 fast-fail ValueError reproduced free via `build_uri_sources`.
- [x] PASS — Cron path (`source_files=["sources/listen.yaml"]`, no user_id) ingests only RSS — SWE log records `shards_total=1`, substack 22→62 with huggingface/youtube/web unchanged; relative-path resolution under served worker (cwd=`apps/memory/`) evidenced; two-strategy `_resolve_source_path` (sources.py:54-82) confirms the cloud cwd=git-clone-root expectation per ADR-003 §5.
- [x] PASS — Downstream still works — SWE log: extraction (1 doc → 54 nodes embedded + 17 edges) → indexing → `make memory-query-graph` returned 13 nodes / 12 edges.
- [x] PASS — ADR-003 Status Proposed → Accepted — confirmed in `docs/adrs/003_source_definitions_as_operator_data.md:3`; rest of ADR intact.
- [x] PASS — Suite green — format/lint/pre-commit clean here, unit 1724 passed / 0 warnings here. integration-tests-all NOT re-run: justified — 088's ONLY change is a one-line markdown Status flip that cannot affect pytest; #087 ran the CI-equivalent green (corroborated by `integ_all.log`: 70 passed).
- [x] PASS — Acceptance log records commands + observed document counts per mode — present and detailed in the SWE log.

**Code-review plugin**
- Enabled in `.claude/settings.json`. Surface for this task is empty: the entire diff is a one-line ADR Status flip plus the task tracking file — no product/code change for a reviewer to act on (the sources-split product code shipped/was reviewed under earlier feature tasks). Nothing to fold in.

**Judgement on bounding decisions**
- HF `max_samples` 5 vs committed 1000, substack list trimmed to 2, downstream bounded to 1 rich doc — all reasonable proofs of the code path under Bright Data/Voyage/Gemini free-tier limits, and all DOCUMENTED in the SWE "Cost bounding" section (not silent). Real-file resolution proven for free closes the gap between bounded ingest and the true committed sets. Sound.

**Evidence**
```
$ make memory-unit-tests
============================ 1724 passed in 44.59s =============================

$ uv run python tester_resolve_check.py
[default_configured_sources] total=17 | {'substack_article': 10, 'huggingface_dataset': 1, 'youtube_video': 1, 'web': 2, 'substack_rss': 3}
[load_sources backfill.yaml] total=14 | {... no substack_rss}
[load_sources listen.yaml] total=3 | {'substack_rss': 3}
[build_uri_sources hf REJECTED] ValueError: huggingface_dataset sources cannot be built from a --uri token...
ALL FREE-RESOLUTION ASSERTIONS PASSED

$ git status --porcelain --untracked-files=all
 M docs/adrs/003_source_definitions_as_operator_data.md
?? tasks/088-sources-split-live-e2e-acceptance.md
```

**Other issues found (non-blocking, out of scope for 088 — candidates for a rollup)**
- `SourcesConfig` silently yields `[]` for a YAML that is a single mapping rather than a top-level list (BP7). An operator fat-fingering a source file would get a no-op ingest with no error. Pre-existing (#083–#087), not 088. Consider a "non-empty / shape" guard.
- (Already noted by SWE) orchestrator grouping `logger.info` line does not reach Prefect API run logs.

**VERDICT: PASS**
