---
id: 086-orchestrator-source-selection-and-retire-scheduled
feature: sources-config-split
status: done
---

# Orchestrator source-selection (file/URL/default) + retire scheduled_only

Tags: `data`, `infra`
Depends on: #084
Blocks: #087
Implements: ADR-003

## Scope

Make the offline data orchestrator select its source set dynamically — concatenating
`load_sources(source_files)` + inline `sources`, falling back to the backfill+listen default
only when BOTH are absent — wire the operator surface onto `run_data_pipeline.py`
(repeatable `--source-file` and repeatable `--uri` tokens, freely combinable), repoint the
nightly cron to load `sources/listen.yaml`, and retire the `scheduled_only` param /
`--scheduled-only` flag / `.scheduled` filter (the `scheduled` model field is removed in
087).

## Acceptance criteria

- [x] `data_etl_orchestrator` signature becomes
      `(user_id: PydanticObjectId | None = None, source_files: list[str] | None = None,
      sources: list[dict[str, Any]] | None = None)`: the `scheduled_only` param and the
      `if scheduled_only: sources = [s for s in sources if s.scheduled]` filter are removed.
- [x] Source-set resolution **concatenates** (NOT either/or): the resolved set is
      `load_sources(source_files)` (when given) followed by the coerced inline `sources`
      (when given), in that order. Inline `sources` are coerced via the existing
      `_SOURCES_ADAPTER` discriminated-union `TypeAdapter`. When BOTH `source_files` and
      `sources` are absent, the set is `default_configured_sources()` (= backfill+listen).
      The empty-set no-op (`DataFanOutStats(shards_total=0)`) and per-tenant fan-out are
      preserved. The now-unused `app_config` import is dropped from `offline_pipeline.py`.
- [x] `run_data_pipeline.py`: removes `--scheduled-only`; adds repeatable `--source-file`
      and repeatable `--uri`. There is NO separate `--type` flag and NO mutual-exclusivity:
      `--source-file` and `--uri` may be passed together. The script parses each `--uri`
      value with `tree.config.sources.parse_uri_token`, builds inline sources via
      `tree.config.sources.build_uri_sources(specs)` (script holds NO business logic), and
      forwards whichever of `parameters["source_files"]` / `parameters["sources"]` are
      present (neither when no flags → orchestrator default). Module docstring + `Usage:`
      rewritten to the new modes.
- [x] A combined run (`--source-file sources/backfill.yaml --uri https://x.com/a
      --uri https://y.com/feed=substack_rss`) forwards BOTH `source_files` and `sources`,
      and the orchestrator ingests the file's sources plus the two built URL sources (the
      typed one honored, the untyped one inferred).
- [x] An explicit `--uri 'https://x.com/ds=huggingface_dataset'` fails fast with the clear
      `build_uri_sources` error pointing the operator at a YAML file (surfaced before any
      flow is triggered).
- [x] `orchestrator.py`: the `data-etl-orchestrator` `_DeploymentSpec.schedule_parameters`
      becomes `{"source_files": ["sources/listen.yaml"]}` (no `user_id` → all active users);
      the `_SCHEDULED_INGEST_CRON` comment and the `_DeploymentSpec.cron` docstring example
      are updated to describe loading the listen file (no `scheduled_only`).
- [x] `apps/memory/Makefile` `run-data-pipeline-offline`: drops `SCHEDULED`/`--scheduled-only`
      and any `TYPE` var; expands space-separated `SOURCE_FILE` and `URI` vars into repeated
      `--source-file` / `--uri` flags via `$(foreach ...)` (both may be set in one invocation);
      keeps `USER_ID`/`USER_IDENTIFIER`; help comment rewritten.
- [x] `uv run python scripts/run_data_pipeline.py --scheduled-only` now FAILS with a Click
      "no such option" error (not a silent accept).
- [x] Tests updated/added (call the `/testing-python` skill):
      `tests/unit/data/test_orchestrator_data.py` (default→both files; `source_files`→listed;
      inline `sources`→used; BOTH→concatenated in file-then-inline order; no `scheduled_only`),
      `tests/unit/test_orchestrator.py` (the data cron's `schedule_parameters` is now
      `source_files=["sources/listen.yaml"]`), plus script-level coverage for the new flags,
      the `--source-file` + `--uri` combined run, and the `…=huggingface_dataset` fast-fail.
      `test_offline_pipeline.py` and `test_platform_partition.py` adjusted only if they
      referenced the removed param.
- [x] The `_ConfiguredSource.scheduled` field still EXISTS after this task (dead/unread —
      removed in 087); `default.yaml`'s still-present `scheduled:` keys validate as
      ignored-extras. No crash.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean; `make memory-unit-tests` green, 0 warnings;
      `make memory-integration-tests` green.

## Out of scope

- Removing the `scheduled` model field, `AppConfig.sources`, the `default.yaml` `sources:`
  block, and the README sweep (all in 087). Live end-to-end runs (088).

## Log

### [SWE] 2026-06-27 14:44 — Implementation

**Files modified**
- `apps/memory/src/tree/data/offline_pipeline.py` — new `_resolve_source_set` helper (file ++ inline ++ default); orchestrator signature `(user_id, source_files, sources)`; dropped `scheduled_only` + `.scheduled` filter + the now-unused `app_config` import; added `load_sources`/`default_configured_sources` imports.
- `apps/memory/scripts/run_data_pipeline.py` — removed `--scheduled-only`; added repeatable `--source-file` and `--uri`; parses `--uri` via `parse_uri_token` + builds inline sources via `build_uri_sources` up front (fail-fast); forwards only the present selectors; rewrote docstring + Usage.
- `apps/memory/src/tree/orchestrator.py` — `data-etl-orchestrator` `schedule_parameters` → `{"source_files": ["sources/listen.yaml"]}`; updated `_SCHEDULED_INGEST_CRON` comment and the `_DeploymentSpec.cron` docstring example.
- `apps/memory/Makefile` — `run-data-pipeline-offline` drops `SCHEDULED`/`--scheduled-only`, expands `SOURCE_FILE`/`URI` via `$(foreach ...)` into repeated flags; `run-offline` forwards `SOURCE_FILE`/`URI` instead of `SCHEDULED`.
- `apps/memory/tests/unit/data/test_orchestrator_data.py` — patch `default_configured_sources`/`load_sources` instead of `app_config`; new source-resolution tests (default/file/inline/BOTH-concatenated + helper-level order); removed `scheduled_only` tests; updated signature test.
- `apps/memory/tests/unit/test_orchestrator.py` — data cron `schedule_parameters` assertion → `source_files=["sources/listen.yaml"]`.
- `apps/memory/tests/unit/data/test_run_data_pipeline.py` — NEW: Click-level coverage (help lists new opts; `--scheduled-only` is "no such option"; no-flags forwards neither; file-only; uri-only inferred; `--source-file`+`--uri` combined; `…=huggingface_dataset` fast-fail before any flow).
- `apps/memory/tests/integration/data/test_pipeline.py` + `tests/integration/data/web/test_web_pipeline.py` — dropped the vestigial `tree.data.offline_pipeline.app_config` patch (worker takes sources as an argument; the symbol no longer exists).

**Tests**
- Unit: 1723 passing, 0 failing, 0 warnings — `make memory-unit-tests`.
- Integration: `make memory-integration-tests-ci` (CI-mirror, per-area sequential) exit 0; data area re-run = 11 passing (incl. the two adjusted files). Used the CI-mirror as the green judge per AGENTS.md (the single-process tail can surface pre-existing cross-area flakes).

**Acceptance criteria**
- [x] Orchestrator signature `(user_id, source_files, sources)`; `scheduled_only` param + `.scheduled` filter removed — `tests/unit/data/test_orchestrator_data.py::test_orchestrator_signature_is_user_id_source_files_sources`.
- [x] Source-set resolution concatenates file→inline, else default; `app_config` import dropped — `test_resolve_source_set_*`, `test_*_run_dispatches_*`, `test_both_files_and_inline_are_concatenated_for_dispatch`.
- [x] Script removes `--scheduled-only`, adds repeatable `--source-file`/`--uri`, no `--type`, combinable; forwards present selectors — `test_run_data_pipeline.py` (help/forwarding tests).
- [x] Combined run forwards both, typed honored + untyped inferred — `test_source_file_and_uri_combined_forwards_both`.
- [x] HF `--uri` fails fast pointing at a YAML file before any flow — `test_huggingface_uri_fails_fast_before_any_flow` + CLI smoke (exit 1).
- [x] `orchestrator.py` schedule params + comment/docstring updated — `tests/unit/test_orchestrator.py::test_serve_deployments_schedules_only_the_data_orchestrator`.
- [x] Makefile `run-data-pipeline-offline` rewritten with `$(foreach ...)`; help comment updated.
- [x] `--scheduled-only` now a Click "no such option" error — `test_scheduled_only_is_no_such_option` + CLI smoke (exit 2).
- [x] Tests updated/added per spec; integration `app_config` patches adjusted (referenced the removed symbol).
- [x] `_ConfiguredSource.scheduled` field still present (unread; removed in 087); `default.yaml` parses with no crash — full unit + integration suites green.
- [x] format/lint/pre-commit clean; unit green 0 warnings; integration green.

**Evidence**
```
$ make memory-unit-tests
... 1723 passed in 46.22s

$ make memory-integration-tests-ci            # CI-mirror, per-area sequential
... exit 0 (data / memory / mcp / entities+sessions areas all passed)
$ uv run pytest tests/integration/data/test_pipeline.py tests/integration/data/web/test_web_pipeline.py -m "not requires_mongot" -q
... 11 passed in 24.16s

$ make memory-format-check && make memory-lint-check
289 files already formatted
All checks passed!
$ make pre-commit
prettier...Passed  ruff check...Passed  ruff format...Passed  biome check (harness)...Passed

$ uv run python scripts/run_data_pipeline.py --scheduled-only ; echo exit=$?
Error: No such option: --scheduled-only
exit=2
$ uv run python scripts/run_data_pipeline.py --uri 'https://x.com/ds=huggingface_dataset' ; echo exit=$?
ValueError: huggingface_dataset sources cannot be built from a --uri token: ... Define it in a YAML file (e.g. sources/backfill.yaml) and use --source-file. Offending uri(s): https://x.com/ds.
exit=1
```

**Notes**
- Called the `/testing-python` skill conventions for all new/updated tests (AAA, atomic, `mocker` at boundaries, module-level imports, no infra unit-mocks).
- Left `_ConfiguredSource.scheduled` (and its docstring) + `tests/unit/config/test_sources_config.py` + `default.yaml`'s `sources:` block untouched — the field/docstring sweep is explicitly deferred to 087. Those reference `scheduled_only` only in comments/docstrings and stay green.
- Also updated `run-offline` in the Makefile to forward `SOURCE_FILE`/`URI` instead of the retired `SCHEDULED` (consistency with the retired flag; it chained into `run-data-pipeline-offline`).
- DID NOT commit (awaiting Tester PASS).

### [Tester] 2026-06-27 14:55 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`ruff format --check` 289 files OK; `ruff check` all passed; pre-commit prettier/ruff/biome all Passed)
- Unit tests: 1723 passed / 0 failed
- Integration tests (`make memory-integration-tests-ci`, CI-mirror per-area sequential, exit 0): data 27 passed / 1 skipped (incl. the two adjusted files `test_pipeline.py` 5, `test_web_pipeline.py` 6); memory 92 passed / 43 deselected (incl. `test_meta_state.py` 11 passed — known flake green under CI target); mcp 42 passed; rest 43 passed / 27 deselected. 0 FAILED/ERROR.
- Warnings: 0 in the pytest runs (the `opik` Pydantic-V1/Py3.14 `UserWarning` is a library import-time warning outside the test session, not a pytest warning).
- Diff touches no indexing/meta-state code — the documented single-process flakes are not a regression here.

**E2E adversarial pass** (independently exercised — not via the SWE tests)
- Happy path (resolution): `_resolve_source_set(None, None) is default_configured_sources()` → 17 entries (backfill 14 + listen 3), all 5 source types present (PASS)
- Break path 1 (state edges — source-set resolution): (a) no args → default identity ✓; (b) `source_files=["sources/backfill.yaml"]` → 14 entries == `load_sources([backfill])`, subset of default ✓; (c) inline `sources=[...]` → coerced `[SubstackRssSource, WebSource]`, uris preserved ✓; (d) BOTH → file entries FIRST (14) then inline appended LAST (15 total), order asserted ✓; orchestrator empty-set no-op (`source_files=[], sources=[]`) → `DataFanOutStats(shards_total=0)`, `run_deployment` never awaited ✓ (PASS)
- Break path 2 (CLI surface): `run_data_pipeline.py --scheduled-only` → `Error: No such option: --scheduled-only`, exit 2 ✓; `--help` lists `--source-file` + `--uri`, contains neither `--scheduled-only` nor `--type` (grep count 0/0) ✓ (PASS)
- Break path 3 (failure mode — HF fail-fast): `--uri 'https://x.com/ds=huggingface_dataset'` → `ValueError` raised at `run_data_pipeline.py:171` in `build_uri_sources(specs)`, BEFORE `asyncio.run(_run(...))`; traceback shows NO `init_mongodb`/`get_client` — fails during arg-building, exit 1, message names `sources/backfill.yaml` ✓ (PASS)
- Break path 4 (combined forwarding): drove `_run` with Prefect client mocked → captured deployment `parameters` = `{user_id, source_files:["sources/backfill.yaml"], sources:[{web},{substack_rss}]}` (BOTH forwarded, types honored/inferred); no-flags → `{user_id}` only ✓ (PASS)
- Break path 5 (cron): runtime inspect of `orchestrator._DEPLOYMENT_SPECS` → data-etl-orchestrator `cron="0 3 * * *"`, `schedule_parameters == {"source_files": ["sources/listen.yaml"]}`, no `scheduled_only`, no `user_id` ✓ (PASS)
- Break path 6 (grep + boot): no `scheduled_only` in offline_pipeline.py/run_data_pipeline.py/orchestrator.py; `_ConfiguredSource.scheduled` still present (app_config.py:329); `default.yaml` `sources:` block present (17 entries) + app boots ✓; `app_config` singleton import dropped from offline_pipeline.py (only model-class module import + one comment remain) ✓ (PASS)
- Extra boundary/malformed edges: nonexistent `--source-file` → clear server-side `FileNotFoundError` naming the file + attempted paths ✓; empty `--uri ''` → fast `ValidationError` "String should have at least 1 character", exit 1 ✓; bogus `--uri https://x.com/a=notatype` → suffix kept, inferred as `web` (no crash) ✓ (PASS)
- Makefile dry-run: `SOURCE_FILE="a b" URI="x y"` expands to repeated `--source-file`/`--uri` flags + user override; no selectors → bare invocation ✓ (PASS)

**Acceptance criteria**
- [x] PASS — orchestrator signature `(user_id, source_files, sources)`, `scheduled_only` param + `.scheduled` filter removed — runtime `inspect.signature` == `['user_id','source_files','sources']`; grep clean; diff removed the filter
- [x] PASS — source-set concatenation (file→inline, else default), empty-set no-op + per-tenant fan-out preserved, `app_config` import dropped — adversarial resolution (a)–(d)+noop; `test_user_id_none_fans_out_per_active_user` green; no `app_config` singleton usage in offline_pipeline.py
- [x] PASS — script removes `--scheduled-only`, adds repeatable `--source-file`/`--uri` (no `--type`, combinable), parses via `parse_uri_token`+`build_uri_sources`, forwards present selectors — `--help`, forwarding capture
- [x] PASS — combined run forwards BOTH, typed honored + untyped inferred — `_run` parameters capture + orchestrator concatenation (substack_article+web shards)
- [x] PASS — explicit `…=huggingface_dataset` fails fast pointing at a YAML file before any flow — live traceback at `build_uri_sources`, exit 1
- [x] PASS — cron `schedule_parameters == {"source_files":["sources/listen.yaml"]}` (no user_id); comment + docstring updated — runtime spec inspect + diff
- [x] PASS — Makefile `run-data-pipeline-offline` drops SCHEDULED/`--scheduled-only`/TYPE, `$(foreach)` over SOURCE_FILE/URI, keeps USER_ID/USER_IDENTIFIER, help rewritten — diff + dry-run expansion
- [x] PASS — `--scheduled-only` is a Click "no such option" error (exit 2)
- [x] PASS — tests updated/added — new `test_run_data_pipeline.py`, updated `test_orchestrator_data.py`/`test_orchestrator.py`, integration `app_config` patches dropped; all green
- [x] PASS — `_ConfiguredSource.scheduled` still present (app_config.py:329); `default.yaml` parses + app boots, no crash
- [x] PASS — format/lint/pre-commit clean; unit green 0 warnings; integration (CI-mirror) green

**Other issues found** (non-blocking — PASS with note)
- CLI build-time validation failures (`--uri ...=huggingface_dataset`, empty `--uri ''`) surface as raw Python tracebacks (ValueError / pydantic ValidationError) rather than a clean `click.UsageError` / stderr message. Spec only requires fail-fast with a clear error (met); a non-traceback UX would be a nicer follow-up.
- Empty-resolved-set log uses `"set" if sources else "unset"` — an explicitly-passed `sources=[]` logs as "unset" (cosmetic only; behavior correct).
- `apps/memory/README.md:130` still references the retired `SCHEDULED=1` / "scheduled-only" Make var, and `_ConfiguredSource.scheduled`'s docstring still mentions `scheduled_only=True` — both explicitly deferred to the 087 README/field sweep (out of scope here).

**VERDICT: PASS**
