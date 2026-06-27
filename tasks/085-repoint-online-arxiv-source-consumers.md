---
id: 085-repoint-online-arxiv-source-consumers
feature: sources-config-split
status: done
---

# Repoint online + arxiv source consumers to the shared loader

Tags: `data`, `config`
Depends on: #084
Blocks: #087
Implements: ADR-003

## Scope

Switch the two non-orchestrator readers of `app_config.sources.sources` —
`_get_configured_substack_domains` (`data/online_pipeline.py`) and
`_get_huggingface_arxiv_defaults` (`data/huggingface/arxiv_dataset_pipeline.py`) — to read
from `default_configured_sources()` instead. Behavior is identical because the committed
files (083) currently carry the same set as `default.yaml`.

## Acceptance criteria

- [x] `_get_configured_substack_domains()` derives its domain set from
      `default_configured_sources()` (the substack RSS/article entries across
      backfill+listen) instead of `app_config.sources.sources`; the set is unchanged for the
      current source data.
- [x] `_get_huggingface_arxiv_defaults()` selects the arxiv entry from
      `default_configured_sources()` and returns the same
      `(max_samples, fetch_content, batch_size, concurrency)` tuple (falling back to
      `HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID)` defaults when absent).
- [x] Any now-unused `app_config` import is removed from these two modules; the variant
      classes they still need (`SubstackArticleSource`/`SubstackRssSource`,
      `HuggingFaceDatasetSource`) keep importing from `tree.config.app_config`.
- [x] Caching is preserved (the existing `@functools.cache` on the substack-domains helper
      and/or the loader cache); no per-call file re-read.
- [x] Tests updated to patch `default_configured_sources()` (or the source files) instead of
      `app_config.sources`: `tests/unit/data/test_online_pipeline.py`,
      `tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py`, and the integration
      tests that drove the substack-domain routing
      (`tests/integration/mcp/test_ingest_url_after_dispatcher_migration.py`,
      `tests/integration/data/web/test_web_pipeline.py`). Call the `/testing-python` skill.
- [x] The orchestrator still reads `app_config.sources.sources` (unchanged here);
      `default.yaml` + `AppConfig.sources` still present. No behavior divergence between the
      file-backed consumers and the still-config-backed orchestrator (same data).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean; `make memory-unit-tests` green, 0 warnings;
      `make memory-integration-tests` (fast tail) green for the touched online/mcp paths.

## Out of scope

- The orchestrator/script/cron/Makefile changes (086). Removing `AppConfig.sources`,
  the `scheduled` field, or the `default.yaml` block (087).

## Log

### [SWE] 2026-06-27 14:20 — Implementation

**Files modified**
- `apps/memory/src/tree/data/online_pipeline.py` — `_get_configured_substack_domains()` now reads `default_configured_sources()` (read-only iteration); dropped the unused `app_config` import, added `from tree.config.sources import default_configured_sources`; docstrings repointed. `@functools.cache` preserved.
- `apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py` — `_get_huggingface_arxiv_defaults()` now reads `default_configured_sources()` (read-only iteration), same tuple + same `HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID)` fallback; dropped the unused `app_config` import, added the loader import; docstring repointed.
- `apps/memory/tests/unit/data/test_online_pipeline.py` — `_patch_sources` now patches `default_configured_sources`; dropped unused `SourcesConfig` import; added `test_does_not_mutate_loader_cached_list` (guards the cached-list-mutation regression from 084's QA).
- `apps/memory/tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py` — both defaults tests patch `default_configured_sources`; dropped unused `SourcesConfig` import; added `test_does_not_mutate_loader_cached_list`.
- `apps/memory/tests/integration/mcp/test_ingest_url_after_dispatcher_migration.py` — asserts against `default_configured_sources()` instead of `app_config.sources.sources`; module docstring + comments repointed to the shared loader / source files.
- `apps/memory/tests/integration/data/web/test_web_pipeline.py` — `TestDataPipelinePicksUpWebEntries` patches `arxiv_dataset_pipeline.default_configured_sources` (was `app_config`); `_SUBSTACK_URL` comment repointed to `sources/backfill.yaml`.
- `apps/memory/tests/integration/data/test_pipeline.py` — `_make_full_config` patches `arxiv_dataset_pipeline.default_configured_sources` (was `app_config`); docstring repointed. (Not named in the spec but it drove the arxiv defaults via the now-removed `app_config` and broke on the import removal.)

**Tests**
- Unit: 1710 passing, 0 failing, 0 warnings — `make memory-unit-tests`. Touched modules (online + arxiv + sources): 88 passing.
- Integration (per-area, the canonical CI split): data `13 passed, 7 skipped`; mcp `40 passed, 2 skipped`; memory `70 passed`. The Bright-Data-gated web tests skip (no real creds in this env).

**Acceptance criteria**
- [x] `_get_configured_substack_domains()` reads `default_configured_sources()` — `tests/unit/data/test_online_pipeline.py::TestGetConfiguredSubstackDomains` + integration migration test.
- [x] `_get_huggingface_arxiv_defaults()` reads `default_configured_sources()`, same tuple + fallback — `tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py::TestGetHuggingfaceArxivDefaults`.
- [x] Unused `app_config` import removed from both modules; variant classes still imported from `tree.config.app_config`.
- [x] Caching preserved (`@functools.cache` on the substack helper + the loader cache); read-only iteration, no in-place mutation (regression tests added).
- [x] Tests repointed to `default_configured_sources()` (named files + `test_pipeline.py`). `/testing-python` skill consulted.
- [x] Orchestrator still reads `app_config.sources.sources`; `default.yaml` + `AppConfig.sources` untouched. Verified zero divergence at runtime (loader-path == app_config-path for both domains and arxiv defaults).
- [x] format/lint/pre-commit clean; unit-tests green 0 warnings; fast integration green for the touched online/mcp paths (per-area).

**Evidence**
```
$ make memory-unit-tests
============================ 1710 passed in 47.29s =============================

$ # End-to-end invocation against the real source files
loaded source entries: 17
substack domains: ['decodingai.com', 'latent.space', 'maximelabonne.substack.com']
arxiv defaults (max_samples, fetch_content, batch_size, concurrency): (1000, False, 50, 25)

$ # No-divergence check vs the still-config-backed orchestrator path
config-path substack domains == loader-path: True ['decodingai.com', 'latent.space', 'maximelabonne.substack.com']
config-path arxiv defaults == loader-path: True (1000, False, 50, 25)

$ # Touched integration areas, run one-process-per-area (CI split)
data: 13 passed, 7 skipped, 8 deselected
mcp:  40 passed, 2 skipped
memory: 70 passed
```

**Notes**
- The all-in-one-process `make memory-integration-tests` target flagged 3 failures: 1 was mine (`test_pipeline.py` patched the removed `app_config`) and is now fixed; the other 2 (`test_indexing_pipeline.py::test_embeds_nodes`, `test_meta_state.py::test_updated_at_is_recent`) are PRE-EXISTING cross-area, single-process flakiness — they touch embeddings / dream meta-state (nothing I changed), pass in isolation, and pass when their `memory` area runs as its own process (70/70). The suite is normally split one-process-per-area (`integration-tests-all`/`-ci`) precisely because shared-DB cleanup fixtures collide across areas (per CLAUDE.md). Recommend the Tester run the touched areas per-area (or `make memory-integration-tests-ci`) rather than the single-process fast tail.
- No commit/push per role — handing off to the Tester.

### [Tester] 2026-06-27 14:21 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`288 files already formatted`; `All checks passed!`; pre-commit all hooks Passed)
- Unit tests: 1710 passed / 0 failed (`make memory-unit-tests`)
- Integration tests (CI-mirroring `make memory-integration-tests-ci`, per-area sequential): data 27 passed/1 skipped · memory 92 passed/43 deselected · mcp 42 passed · misc 43 passed/27 deselected — 0 failed
- Warnings: 0 pytest warnings under the canonical make targets. (A single `UserWarning: Core Pydantic V1 … Python 3.14` is an import-time stderr line from the vendored `opik` dep, surfaced only on bare `uv run pytest`; it is a pre-existing env artifact, not a pytest-counted warning and not introduced by this task.)

**E2E adversarial pass** (behavioral equivalence is the core repoint risk — verified independently)
- Happy path: loader materialises 17 source entries (== `app_config.sources.sources` count of 17). PASS
- Break path 1 (behavioral equivalence — substack domains): computed both ways and diffed → loader-path `{decodingai.com, latent.space, maximelabonne.substack.com}` == config-path EXACTLY. PASS
- Break path 2 (behavioral equivalence — arxiv defaults): loader-path `(1000, False, 50, 25)` == config-path `(1000, False, 50, 25)` EXACTLY. PASS
- Break path 3 (state edge — cached-list mutation): called both helpers repeatedly; `default_configured_sources()` returns the SAME object identity before/after and contents are byte-for-byte unchanged; helpers are idempotent across re-calls. PASS
- Break path 4 (residual-read grep): no `app_config.sources` read remains in either module; the unused `app_config` import is gone (online keeps `SubstackArticleSource`/`SubstackRssSource`, arxiv keeps `HuggingFaceDatasetSource` from `tree.config.app_config`). PASS
- Break path 5 (orchestrator untouched): `offline_pipeline.py:556` still reads `app_config.sources.sources`; `AppConfig.sources` + `configs/default.yaml` block still present → zero divergence between file-backed consumers and config-backed orchestrator (same 17 entries, same domains, same arxiv tuple). PASS

**Pre-existing-flake claim verified**
- (a) Both green: `test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent` passes under the CI target and in isolation; `test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes` is `requires_mongot`-gated (deselected by the CI target) and passes in isolation with mongot up (`2 passed in 5.44s`).
- (b) `git diff --name-only` confirms this task touched NO indexing / meta-state / embedding source — only the two repointed modules + 5 test files + `docs/glossary.md`. The single-process-tail failures are shared-DB cleanup-fixture collisions, not a regression from this change.

**Acceptance criteria**
- [x] PASS — `_get_configured_substack_domains()` derives domains from `default_configured_sources()`, set unchanged — `online_pipeline.py:102-125`; equiv script EQUAL=True; `tests/unit/data/test_online_pipeline.py::TestGetConfiguredSubstackDomains` (incl. new no-mutation guard) + integration migration test pass.
- [x] PASS — `_get_huggingface_arxiv_defaults()` selects arxiv from the loader, same `(max_samples, fetch_content, batch_size, concurrency)` tuple + `HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID)` fallback — `arxiv_dataset_pipeline.py:170-198`; equiv script EQUAL=True; `tests/unit/.../test_arxiv_dataset_pipeline.py::TestGetHuggingfaceArxivDefaults`.
- [x] PASS — unused `app_config` import removed from both modules; variant classes still imported from `tree.config.app_config` — grep confirms.
- [x] PASS — caching preserved (`@functools.cache` on the substack helper `online_pipeline.py:101` + loader `sources.py:103`); no per-call re-read (same cached-object identity across calls in the equiv probe).
- [x] PASS — tests repointed to patch `default_configured_sources()`: `test_online_pipeline.py`, `test_arxiv_dataset_pipeline.py`, `test_ingest_url_after_dispatcher_migration.py`, `test_web_pipeline.py` (+ `test_pipeline.py` which patched the now-removed `app_config`); two new `test_does_not_mutate_loader_cached_list` guards pass.
- [x] PASS — orchestrator still reads `app_config.sources.sources` (`offline_pipeline.py:556`); `default.yaml` + `AppConfig.sources` intact; no data divergence (verified at runtime, both paths == 17 entries / same domains / same arxiv tuple).
- [x] PASS — format/lint/pre-commit clean; unit-tests green 0 warnings; touched online/mcp/data integration areas green under the CI-mirroring target.

**Evidence**
```
$ make memory-unit-tests
============================ 1710 passed in 46.77s =============================

$ make memory-integration-tests-ci
data:   27 passed, 1 skipped in 44.22s
memory: 92 passed, 43 deselected in 37.22s
mcp:    42 passed in 11.91s
misc:   43 passed, 27 deselected in 2.74s

$ # behavioral-equivalence e2e probe (loader-path vs old app_config-path)
loaded source entries (loader): 17   config entries (app_config): 17
[1] substack domains EQUAL: True   ['decodingai.com', 'latent.space', 'maximelabonne.substack.com']
[2] arxiv defaults  EQUAL: True   (1000, False, 50, 25)
[3] same cached object identity: True   cached contents unchanged: True
OVERALL EQUIVALENCE: PASS

$ # pre-existing flakes, run in isolation
tests/integration/memory/test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes PASSED
tests/integration/memory/test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent PASSED
```

**Other issues found** (non-blocking)
- The working tree carries an uncommitted `docs/glossary.md` edit (Backfill/Listen/Source-file terms) that is NOT part of 085's stated diff and belongs to the feature/loader work (#084). Not a 085 defect; flagging so the orchestrator/PA confirm it lands with the right task and isn't dropped.
- The `code-review` plugin is enabled in `.claude/settings.json` but is a `/review` slash-command not exposed as a tool in this sub-agent context, so it could not be invoked here. The manual checklist + adversarial pass were completed in full on a small, surgical diff.

**VERDICT: PASS**
