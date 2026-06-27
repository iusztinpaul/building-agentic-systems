---
id: 087-remove-appconfig-sources-and-defaultyaml-block
feature: sources-config-split
status: done
---

# Remove AppConfig.sources + default.yaml sources block + scheduled field; docs sweep

Tags: `data`, `config`, `docs`
Depends on: #085, #086
Implements: ADR-003

## Scope

Finish the split: drop the now-dead `_ConfiguredSource.scheduled` field, remove the
`AppConfig.sources` field and the `sources:` block from `configs/default.yaml` (leaving it
static-config-only), and sweep the READMEs/docs to describe the new source-file model. By
this point nothing reads `app_config.sources` (repointed in 085/086) or `.scheduled`
(filter removed in 086).

## Acceptance criteria

- [x] `_ConfiguredSource.scheduled` is removed (and its docstring trimmed); the variant
      classes still validate. (Keep `_ConfiguredSource` as the documented shared base or
      collapse it — SWE's call — as long as every variant + the union are unchanged in
      behavior.)
- [x] `AppConfig.sources` field is removed; `app_config = load_app_config()` no longer
      eagerly validates a `sources:` block. `SourcesConfig`, the variants, the discriminated
      union, and the `_normalize_untyped_entry`/host helpers STAY (the loader + URL builder
      depend on them).
- [x] `apps/memory/configs/default.yaml` no longer has a `sources:` block (or its
      explanatory comments); only static memory config remains and `load_app_config()` is
      green.
- [x] READMEs swept — `README.md` (root, lines ~166/178/180/207) and
      `apps/memory/README.md` (the `configs/default.yaml` table row, the `sources` section
      ~46-48, the "scheduled only" mode ~152-164, line ~130): replace the old
      "all / `SCHEDULED=1` / online" three-mode story with the new modes (default = both
      files / `--source-file` / `--uri[=type]` / online), point the source docs at
      `sources/backfill.yaml` + `sources/listen.yaml`, and remove every `scheduled:` /
      `SCHEDULED=1` / `--scheduled-only` reference.
- [x] `git grep` is clean of live references to the `scheduled` source flag,
      `scheduled_only`, `--scheduled-only`, `SCHEDULED`, `app_config.sources`, and any
      `sources:` block in `default.yaml` (outside `tasks/`, `tracker/` and the ADR's
      historical note).
- [x] Tests updated (call the `/testing-python` skill): `tests/unit/config/test_app_config.py`
      and `tests/unit/config/test_sources_config.py` no longer assert `AppConfig.sources` or
      the `scheduled` field; the inference/normalization tests (now exercising
      `SourcesConfig`/`build_uri_sources` directly) stay green. Integration tests that
      monkeypatched `app_config.sources` (`tests/integration/data/test_pipeline.py`, …)
      patch the loader / source files instead.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean; `make memory-unit-tests` green, 0 warnings;
      `make memory-integration-tests-all` green.

## Out of scope

- Live E2E runs + flipping the ADR to Accepted (088).

## Log

### [SWE] 2026-06-27 16:40 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — removed the `_ConfiguredSource` base + its `scheduled` field (collapsed the 6 variants to inherit `BaseModel` directly); removed `AppConfig.sources`; rewrote the `SourcesConfig` validator doc/comment that referenced the removed `AppConfig.sources`. Kept `SourcesConfig`, all variants, the `SourceEntry` union, and the `_normalize_untyped_entry`/host helpers.
- `apps/memory/configs/default.yaml` — removed the `sources:` block + its comments; now static memory config only, with a header note pointing at the `sources/` files + ADR-003.
- `README.md` (root) — replaced the old "all / `SCHEDULED=1` / online" three-mode story (lines ~166/174/178/180/207) with the new default/`SOURCE_FILE`/`URI`/online modes; pointed source docs at `sources/backfill.yaml` + `sources/listen.yaml`.
- `apps/memory/README.md` — config table now lists `sources/`; added a "Source files (`sources/`)" subsection; dropped the `sources` bullet + "scheduled only" section; rewrote "Data pipelines" to the new selection modes; removed every `scheduled:` / `SCHEDULED=1` reference; fixed `num_workers` doc to `sources/backfill.yaml`.
- `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — dropped the now-dead `sources:` block (tests no longer read `config.sources`).
- `apps/memory/tests/unit/config/test_app_config.py` — removed the 4 tests asserting `config.sources.sources`; added `test_app_config_has_no_sources_field` + `test_load_app_config_ignores_a_legacy_sources_block`; pruned now-unused imports.
- `apps/memory/tests/unit/config/test_sources_config.py` — rewrote the YAML round-trip to exercise `SourcesConfig` directly (flat top-level list, no `load_app_config`); replaced `TestScheduledFlag` with `TestScheduledFieldRetired` (no variant has the field; a legacy `scheduled:` key is dropped, not persisted).
- `apps/memory/tests/unit/data/test_run_data_pipeline.py` — `inline_sources` dump no longer carries `scheduled: False`.
- `apps/memory/tests/integration/data/test_pipeline.py` — `_make_full_config` → `_make_sources` returns a `list[SourceEntry]` (no `MagicMock().sources.sources`); rewrote `test_dispatches_all_five_source_variants` to load via `tree.config.sources.load_sources` over a flat-list source-file fixture; moved variant imports to module level.
- `apps/memory/tests/integration/data/web/test_web_pipeline.py` — pass a plain `sources` list to `data_etl_worker` (dropped the `MagicMock().sources.sources` shape + unused `MagicMock` import).

**Tests**
- Unit: 1724 passing, 0 failing, 0 warnings — `make memory-unit-tests`.
- Integration: FULL `make memory-integration-tests-all` green — data 27 passed/1 skipped (Bright Data-gated), memory 135, mcp 42, other 70. (Skip is pre-existing, creds-gated.)

**Acceptance criteria**
- [x] `_ConfiguredSource.scheduled` removed — variants collapsed to `BaseModel`, union unchanged; verified by `tests/unit/config/test_sources_config.py::TestScheduledFieldRetired` + `TestVariantValidation`.
- [x] `AppConfig.sources` removed; loader no longer validates a `sources:` block — `tests/unit/config/test_app_config.py::TestLoadAppConfig::test_app_config_has_no_sources_field` + `::test_load_app_config_ignores_a_legacy_sources_block`.
- [x] `configs/default.yaml` has no `sources:` block; `load_app_config()` green — smoke: `app_config` imports + `git grep "^sources:" default.yaml` clean.
- [x] READMEs swept (both files) — new modes, source-file docs, no `scheduled`/`SCHEDULED=1`/`--scheduled-only`.
- [x] `git grep` clean of live references (see Evidence).
- [x] Tests updated (`/testing-python` skill conventions followed).
- [x] format/lint/pre-commit clean; unit-tests green/0 warnings; integration-tests-all green.

**Evidence**
```
$ make memory-unit-tests
... 1724 passed in 45.92s ...

$ make memory-integration-tests-all
... data: 27 passed, 1 skipped in 58.76s ...
... memory: 135 passed in 242.11s ...
... mcp: 42 passed in 10.61s ...
... other: 70 passed in 125.08s ...

$ git grep -n "app_config\.sources\|\.sources\.sources" -- ':!tasks/' ':!tracker/' ':!docs/adrs/'
(clean)
$ git grep -n "SCHEDULED=1" / "scheduled:" in default.yaml / "^sources:" in default.yaml
(clean)
# remaining `scheduled_only` / `--scheduled-only` hits are negative-assertion
# guards in test_orchestrator_data.py + test_run_data_pipeline.py (they assert the
# retirement holds); remaining `scheduled` mentions are dream-consolidation /
# nightly-cron wording + the sources/*.yaml notes documenting the flag's absence.

$ uv run python -c "from tree.config.app_config import app_config, AppConfig; \
  from tree.config.sources import default_configured_sources; ..."
AppConfig has sources field: False
default_configured_sources count = 17  # 14 backfill + 3 listen
sample variant fields (no scheduled): ['type', 'uri']

$ uv run python scripts/run_data_pipeline.py --scheduled-only
Error: No such option: --scheduled-only
```

**Notes**
- `_ConfiguredSource` would have become an empty base after dropping `scheduled`, so I collapsed it (the task left this to SWE) — all 6 variants now inherit `BaseModel`, union/behavior unchanged.
- Did NOT touch `docs/glossary.md` or `docs/adrs/003_*` (pre-written, committed by the orchestrator). README wording aligned to them (Backfill/Listen/Source file terms).
- Code is local + uncommitted, awaiting Tester.

### [Tester] 2026-06-27 17:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`format-check` 289 files formatted; `lint-check` all passed; `pre-commit` all hooks passed)
- Unit tests: 1724 passed / 0 failed
- Integration tests (FULL `make memory-integration-tests-all`, exit 0): data 27 passed / 1 skipped, memory 135 passed, mcp 42 passed, rest 70 passed = 274 passed / 1 skipped / 0 failed
- Warnings: 0 (the single live Bright Data SERP test skip is creds-gated `BRIGHTDATA_API_KEY`/`BRIGHTDATA_SERP_ZONE`, pre-existing, unrelated to this diff)

**E2E adversarial pass** (`uv run python` from `apps/memory`)
- Happy path: `default_configured_sources()` → 17 entries (10 substack_article + 1 hf + 1 youtube_video + 2 web + 3 substack_rss = 14 backfill + 3 listen) (PASS); `run_data_pipeline.py --help` shows `--source-file`/`--uri`, no `--scheduled-only` (PASS)
- Break path 1 (back-compat: legacy `sources:` block + `scheduled: true`): `load_app_config(yaml_with_sources_block)` → loads green, `query.top_k` honored, `hasattr(cfg,'sources')` False — legacy block silently ignored (Pydantic `extra=ignore`) (PASS)
- Break path 2 (boundary: empty source file): `load_sources([empty.yaml])` → `[]` (yaml→None coerced) (PASS)
- Break path 3 (failure mode: missing file): `load_sources(["sources/does_not_exist.yaml"])` → `FileNotFoundError` naming both attempted locations, clean (no stack-trace leak) (PASS)
- Break path 4 (malformed: missing `uri` / empty `uri` / bogus `type`): each → `ValidationError`, no crash (PASS)
- Break path 5 (hostile-ish: `--uri ns/name=huggingface_dataset`): `build_uri_sources` → `ValueError` with actionable message (HF must be defined in a file) (PASS)
- Break path 6 (parser edge: query-string `=` not mistaken for a type): `parse_uri_token(".../videos.xml?channel_id=UCabc")` → `(url, None)`; `.../feed=substack_rss` → `(.../feed, 'substack_rss')` (PASS)
- Variant validation: all 6 variants build via `SourcesConfig`; none has a `scheduled` model field; a legacy `scheduled: true` key is dropped from `model_dump()` not persisted (PASS)
- `run_data_pipeline.py --scheduled-only` → `Error: No such option: --scheduled-only`, exit 2 (PASS)

**Acceptance criteria**
- [x] PASS — `_ConfiguredSource.scheduled` removed, variants validate — base collapsed; 6 variants inherit `BaseModel` (`app_config.py:317-385`); `SourceEntry` union + `_normalize_untyped_entry`/`SourcesConfig` intact (`:392/:463/:502`); no live `_ConfiguredSource` ref; `test_sources_config.py::TestScheduledFieldRetired` green
- [x] PASS — `AppConfig.sources` removed, no eager `sources:` validation, kept models stay — `'sources' not in AppConfig.model_fields`; legacy block loads green; `test_app_config.py::{test_app_config_has_no_sources_field,test_load_app_config_ignores_a_legacy_sources_block}` green
- [x] PASS — `configs/default.yaml` has no `sources:` block, only static config, `load_app_config()` green — `grep "sources:" default.yaml` none; 8 static sections present (models/extraction/query/dream/concurrency/prefect/mcp/observability); load parses `models.llm`
- [x] PASS — READMEs swept — root `README.md` + `apps/memory/README.md` describe default/`SOURCE_FILE`/`URI`/online modes, point at `sources/backfill.yaml`+`sources/listen.yaml`; `git grep "scheduled: true|all configured sources|SCHEDULED" -- README.md apps/memory/README.md` clean
- [x] PASS — `git grep` clean of live refs — `app_config.sources`/`.sources.sources` clean; `SCHEDULED=1` clean; remaining `--scheduled-only`/`scheduled_only` hits are negative-assertion test guards; other `scheduled` hits are dream/nightly-cron wording + docs documenting the flag's absence
- [x] PASS — tests updated — `test_app_config.py`/`test_sources_config.py` no longer assert `AppConfig.sources`/`scheduled`; integration tests (`test_pipeline.py`, `web/test_web_pipeline.py`) use `load_sources`/plain lists instead of `MagicMock().sources.sources`; Arrange/Act/Assert + parametrize conventions followed
- [x] PASS — format/lint/pre-commit clean; unit green/0 warnings; `integration-tests-all` green (see Test summary)

**Evidence**
```
$ make memory-unit-tests
============================ 1724 passed in 44.20s =============================

$ make memory-integration-tests-all   # MAKE_EXIT=0
data:   27 passed, 1 skipped in 35.68s
memory: 135 passed in 246.78s
mcp:    42 passed in 10.55s
rest:   70 passed in 171.92s

$ uv run python scripts/run_data_pipeline.py --scheduled-only
Error: No such option: --scheduled-only   (exit 2)

$ adv_check.py
hasattr(app_config,'sources'): False | 'sources' in AppConfig.model_fields: False
legacy sources: block loads OK (query.top_k=7, no sources attr)
default_configured_sources() total: 17
build_uri_sources HF rejected with ValueError: OK
```

**Other issues found**
- `docs/glossary.md` (3 rows: Backfill/Listen sources, Source file) is in the uncommitted working tree though the SWE noted it as "left untouched / pre-written by the orchestrator". Content is topical and accurate (matches the cron wiring at `orchestrator.py:155`). Not an AC for this task; flagging only so the orchestrator confirms it's intended to land with #087. Non-blocking.

**VERDICT: PASS**
