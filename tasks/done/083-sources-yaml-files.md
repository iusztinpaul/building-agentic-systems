---
id: 083-sources-yaml-files
feature: sources-config-split
status: done
---

# Add sources/backfill.yaml + sources/listen.yaml

Tags: `data`, `config`
Depends on: None
Blocks: #084
Implements: ADR-003

## Scope

Create the two committed source-data files at the repo-root `sources/` directory by
splitting today's `apps/memory/configs/default.yaml` `sources:` block by cadence —
`listen.yaml` (RSS feeds only) and `backfill.yaml` (everything else) — with all
`scheduled:` keys dropped. This task is purely additive: `default.yaml` is NOT modified
and nothing reads the new files yet.

## Acceptance criteria

- [x] `sources/listen.yaml` exists at the repo root and contains ONLY the RSS feeds
      currently in `default.yaml`: the three `substack_rss` feeds (`decodingai.com/feed`,
      `maximelabonne.substack.com/feed`, `latent.space/feed`). The existing commented-out
      `youtube_rss` example is carried over (still commented). No `scheduled:` keys.
- [x] `sources/backfill.yaml` exists at the repo root and contains everything else from
      today's block: the 10 `substack_article` decodingai URLs, the `huggingface_dataset`
      arxiv entry with its tuning verbatim (`max_samples: 1000`, `fetch_content: false`,
      `num_workers: 2`, `batch_size: 50`, `concurrency: 25`), the one `youtube_video`, and
      the two untyped web URLs (the reddit + anthropic links). No `scheduled:` keys.
- [x] The union of the two files equals the current `default.yaml` `sources:` set
      (same uris, types and tuning), differing ONLY by the removed `scheduled` keys.
- [x] Each file is a top-level YAML list of source entries (the shape `SourcesConfig`
      accepts). `SourcesConfig.model_validate(yaml.safe_load(<file>))` parses each without
      error; the untyped web URLs infer to `web`, RSS entries validate as
      `substack_rss`/`youtube_rss`, etc.
- [x] A small unit test loads each committed file through `SourcesConfig` and asserts the
      expected variant counts/types (call the `/testing-python` skill).
- [x] No Python/Makefile/`default.yaml` changes in this task (diff is `sources/*.yaml` +
      the one test).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit` clean;
      `make memory-unit-tests` green, 0 warnings.

## Out of scope

- Touching `configs/default.yaml`, any flow/script/Makefile, or the loader (those land in
  084+). No code reads these files yet.

## Log

### [SWE] 2026-06-27 — Implementation

**Files modified**
- `sources/listen.yaml` (new) — repo-root Listen sources: the 3 `substack_rss`
  feeds + the commented-out `youtube_rss` example (carried over still commented);
  no `scheduled:` keys.
- `sources/backfill.yaml` (new) — repo-root Backfill sources: 10
  `substack_article` URLs, the `huggingface_dataset` arxiv entry with tuning
  verbatim, 1 `youtube_video`, 2 untyped web URLs; no `scheduled:` keys.
- `apps/memory/tests/unit/config/test_sources_files.py` (new) — loads each
  committed file via `SourcesConfig.model_validate(yaml.safe_load(...))` and
  asserts variant counts/types + absence of any `scheduled` key.

**Tests**
- Unit: 16 new tests passing (full suite 1672 passing, 0 failing) — output below.
- Integration: N/A — additive YAML data + one unit test, no infra changes.

**Acceptance criteria**
- [x] `sources/listen.yaml` = 3 `substack_rss` feeds + commented `youtube_rss` —
      `tests/.../test_sources_files.py::TestListenYaml`
- [x] `sources/backfill.yaml` = 10 article + 1 hf (tuning verbatim) + 1 video +
      2 web — `TestBackfillYaml`
- [x] Union equals the current `default.yaml` `sources:` set — verified by the
      end-to-end count comparison below (3 + 14 = 17, types match)
- [x] Each file is a top-level YAML list parsed by
      `SourcesConfig.model_validate(yaml.safe_load(<file>))`; untyped URLs infer
      to `web` — `TestFilesExistAsTopLevelLists`, `test_untyped_web_urls_infer_to_web`
- [x] Unit test loads each file through `SourcesConfig` (`/testing-python` skill
      followed) — `test_sources_files.py`
- [x] No Python/Makefile/`default.yaml` changes — `git diff --stat` on
      `default.yaml` is empty; diff is `sources/*.yaml` + the one test
- [x] format-check, lint-check, pre-commit clean; unit-tests green, 0 warnings

**Evidence**
```
$ make memory-unit-tests
... 1672 passed in 46.13s

$ cd apps/memory && uv run pytest tests/unit/config/test_sources_files.py -v -W error
... 16 passed in 0.64s   (0 warnings, warnings-as-errors)

$ make pre-commit
prettier ... Passed   ruff check ... Passed   ruff format ... Passed   biome ... Passed

$ uv run python  (load each file as a user would)
listen.yaml:   3 entries -> {'substack_rss': 3}
backfill.yaml: 14 entries -> {'substack_article': 10, 'huggingface_dataset': 1, 'youtube_video': 1, 'web': 2}
default.yaml active sources: {'substack_rss': 3, 'substack_article': 10, 'huggingface_dataset': 1, 'youtube_video': 1, 'web(untyped->infer): 2}  total: 17
```

**Notes**
- DO NOT COMMIT yet — handing to the Tester first (per role lifecycle).
- `docs/glossary.md` (modified) and `docs/adrs/003_*.md` (untracked) are
  pre-existing PA grooming artifacts already in the working tree when I started;
  I am read-only on them and left them untouched. The eventual commit must
  `git add` only `sources/listen.yaml`, `sources/backfill.yaml`,
  `apps/memory/tests/unit/config/test_sources_files.py`, and this task file.
- The repo-root `sources/` dir is resolved in the test via
  `Path(__file__).resolve().parents[5]` (NOT `tree.config.paths._PROJECT_ROOT`,
  which points at `apps/memory`); the shared loader with cwd/module-root
  resolution lands in 084+, so the test locates the files independently.

### [Tester] 2026-06-27 14:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` — prettier/ruff-check/ruff-format/biome all Passed)
- Unit tests: 1672 passed / 0 failed (`make memory-unit-tests`)
- Targeted new file: 16 passed / 0 failed under `-W error` (0 warnings)
- Integration tests: N/A per spec (additive YAML data + one unit test, no infra changes)
- Warnings: 0 (the opik Pydantic-V1 line is a pre-existing import-time artifact, emitted before the session, unrelated to this task)
- Env: local (`make env-status` → local)

**E2E adversarial pass** (independent script, not the SWE's test)
- Happy path: `SourcesConfig.model_validate(yaml.safe_load(...))` on each file → listen = 3 `{substack_rss: 3}`, backfill = 14 `{substack_article: 10, huggingface_dataset: 1, youtube_video: 1, web: 2}` (PASS)
- Break path 1 (data-integrity diff): union(listen, backfill) normalized through `SourcesConfig` and minus `scheduled` → EXACTLY equals `default.yaml` `sources:` set (17 == 17, order-independent model_dump comparison, 0 entries only-in-union / only-in-default) (PASS)
- Break path 2 (state edge: retired key): no raw entry in either new file carries a `scheduled` key; `default.yaml` still carries it on some entry (proves a real diff, not an empty parse) (PASS)
- Break path 3 (verbatim tuning): HF entry `max_samples=1000 fetch_content=False num_workers=2 batch_size=50 concurrency=25` preserved (PASS)
- Break path 4 (boundary: empty + commented): `SourcesConfig.model_validate([])` → `[]`; listen type set is `{substack_rss}` only — the commented `youtube_rss` stays commented and never leaks into the parsed set (PASS)
- Diff scope: `git diff -- apps/memory/configs/default.yaml` is empty (byte-for-byte unchanged); only tracked change is `docs/glossary.md` (out-of-scope PA artifact, ignored per task brief); only untracked `.py` is the one new test file; `sources/*.yaml` + task file the rest. No Python/Makefile/`default.yaml` change. (PASS)

**Acceptance criteria**
- [x] PASS — `sources/listen.yaml` = 3 `substack_rss` feeds (decodingai/feed, maximelabonne.substack.com/feed, latent.space/feed) + commented `youtube_rss`, no `scheduled` — `sources/listen.yaml:6-15`; `TestListenYaml`, break path 1/2
- [x] PASS — `sources/backfill.yaml` = 10 `substack_article` + 1 `huggingface_dataset` (tuning verbatim) + 1 `youtube_video` + 2 untyped web, no `scheduled` — `sources/backfill.yaml:7-43`; `TestBackfillYaml`, break path 3
- [x] PASS — union == current `default.yaml` `sources:` set, differing only by removed `scheduled` — break path 1 (17==17 exact match)
- [x] PASS — each file is a top-level YAML list parsed by `SourcesConfig.model_validate(yaml.safe_load(...))`; untyped web URLs infer to `web` — happy path + `test_untyped_web_urls_infer_to_web`; inference confirmed against `app_config.py:478-514`
- [x] PASS — unit test loads each committed file through `SourcesConfig` and asserts variant counts/types — `apps/memory/tests/unit/config/test_sources_files.py` (16 tests)
- [x] PASS — no Python/Makefile/`default.yaml` changes — `git diff -- apps/memory/configs/default.yaml` empty; only new `.py` is the test
- [x] PASS — format-check + lint-check + pre-commit clean; unit-tests green, 0 warnings — see Test summary

**Evidence**
```
$ make memory-unit-tests
============================ 1672 passed in 44.38s =============================

$ uv run pytest tests/unit/config/test_sources_files.py -v -W error
============================== 16 passed in 0.75s ==============================

$ make pre-commit
prettier ... Passed   ruff check ... Passed   ruff format ... Passed   biome ... Passed

$ git diff -- apps/memory/configs/default.yaml
(empty — byte-for-byte unchanged)

# independent e2e diff (union minus scheduled vs default.yaml sources)
union entries: 17  default entries: 17
MATCH: union (minus scheduled) == default.yaml sources EXACTLY
```

**Other issues found**
- None blocking. Minor note (not in AC, no action required this task): the test resolves the repo root via `Path(__file__).resolve().parents[5]`, which is positionally brittle if the test ever moves; the SWE flagged this as intentional until the shared loader lands in #084. Acceptable for an additive task.

**VERDICT: PASS**
