# Migrate `default.yaml` to the flat `sources:` shape

Status: pending
Tags: `config`, `data`
Depends on: #006
Blocks: #008, #009, #010

## Scope

Rewrite the `sources:` block of `apps/memory/configs/default.yaml` from the
legacy typed-keys shape to the new flat-list shape introduced in #006. Carry
over **every** existing source verbatim (no URL is dropped, no parameter
changed). Verify that `app_config = load_app_config()` succeeds against the
new file and that downstream consumers that *currently* read the legacy fields
(`tree.data.huggingface.arxiv_dataset_pipeline`,
`tree.data.pipeline.ingest_all_data`, `tree.data.core.ingest`) either:

- Have been updated by their own task (#008, #009), OR
- Are touched here only to the minimum extent needed to keep the unit + module
  imports working until #008 / #009 land.

The pragmatic path is to land #006 + #007 together as a "config-layer-only" PR
that breaks `tree.data.pipeline.ingest_all_data` and the dispatcher's
`_get_configured_substack_domains` helper temporarily, then immediately follow
with #008 + #009. The Tasks Plan's task ordering reflects this — #008 and #009
are gated on this task.

### Required YAML output (target shape)

```yaml
sources:
  - uri: https://www.decodingai.com/feed
    type: substack_rss
  - uri: https://maximelabonne.substack.com/feed
    type: substack_rss
  - uri: https://modelcraft.substack.com/feed
    type: substack_rss
  - uri: https://www.latent.space/feed
    type: substack_rss
  - uri: https://alexeyondata.substack.com/feed
    type: substack_rss
  - uri: https://www.decodingai.com/p/ai-agents-foundations-course
    type: substack_article
  - uri: https://www.decodingai.com/p/ai-workflows-vs-agents-the-autonomy
    type: substack_article
  - uri: https://www.decodingai.com/p/context-engineering-2025s-1-skill
    type: substack_article
  - uri: https://www.decodingai.com/p/llm-structured-outputs-the-only-way
    type: substack_article
  - uri: https://www.decodingai.com/p/stop-building-ai-agents-use-these
    type: substack_article
  - uri: https://www.decodingai.com/p/tool-calling-from-scratch-to-production
    type: substack_article
  - uri: https://www.decodingai.com/p/ai-agents-planning
    type: substack_article
  - uri: https://www.decodingai.com/p/building-production-react-agents
    type: substack_article
  - uri: https://www.decodingai.com/p/how-does-memory-for-ai-agents-work
    type: substack_article
  - uri: https://www.decodingai.com/p/stop-converting-documents-to-text
    type: substack_article
  - uri: librarian-bots/arxiv-metadata-snapshot
    type: huggingface_arxiv
    max_samples: 10
    fetch_content: false
    batch_size: 50
    concurrency: 10
  # Arbitrary URLs (blogs, news, repos, profiles) ingested via the URL
  # dispatcher; specialized pipelines (e.g. Substack) win when they match,
  # otherwise the URL falls back to Bright Data Web Unlocker.
  - uri: https://www.reddit.com/r/AI_Agents/comments/1su8zwi/i_almost_built_rag_for_my_notes_then_realized_i/
  - uri: https://www.anthropic.com/engineering/harness-design-long-running-apps
```

The `models:`, `extraction:`, `query:`, `mcp:` sections are not modified.

### Consumer-side updates needed in this task

The legacy code paths read fields that no longer exist in `SourcesConfig`. The
**minimum** needed to keep imports clean during this task is:

- `tree.data.huggingface.arxiv_dataset_pipeline.ingest_arxiv_dataset` reads
  `app_config.sources.huggingface_arxiv_dataset.{max_samples, fetch_content,
  batch_size, concurrency}` at lines 56–63 of
  `apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py`. Update it
  to scan `app_config.sources.sources` for the first
  `HuggingFaceArxivSource` instance and read its fields, OR accept the
  parameters explicitly (recommended — caller passes them in from the new
  `data_pipeline` flow in #009). For this task, do the minimum: update the
  function to find the first `HuggingFaceArxivSource` in
  `app_config.sources.sources` and read its fields, defaulting to the
  `HuggingFaceArxivSource()` defaults if no such entry exists. SWE picks the
  helper shape.
- `tree.data.pipeline.ingest_all_data` and `tree.data.core.ingest` are
  intentionally NOT updated here — that's #008 and #009. Their unit tests
  WILL break in this PR — that's tolerated because the broader test suite is
  green-by-tasks-end (orchestrator's responsibility).

  **Alternative SWE path:** if the broken intermediate state is too noisy,
  fold #007 + #008 + #009 into a single commit. The grooming explicitly allows
  this — the per-task split is for review clarity, not commit granularity.

### Test updates

Update `apps/memory/tests/unit/config/test_app_config.py`:

- Remove `test_urls_default_is_empty` and `test_urls_round_trip_from_yaml`
  (their assertions reference the deleted `urls` field).
- Add `test_loads_default_yaml_sources` that asserts the loaded
  `app_config.sources.sources` contains the expected set of typed variants
  (count by type: 5 `SubstackRssSource`, 10 `SubstackArticleSource`,
  1 `HuggingFaceArxivSource`, 2 `WebSource`).
- Verify the `HuggingFaceArxivSource` entry's `uri` is
  `librarian-bots/arxiv-metadata-snapshot` and its `max_samples` is `10`.
- The other existing tests in `test_app_config.py` (models, extraction, env
  var override) keep their current shape — they don't reference `sources`.

The arxiv pipeline's own tests (`tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py`)
may need adjustment if they patch `app_config.sources.huggingface_arxiv_dataset`
directly; update those patches to match the new helper shape.

## Acceptance Criteria

- [x] `apps/memory/configs/default.yaml` is updated to the flat shape exactly
      as written in the target snippet above. No URL is dropped; no
      `huggingface_arxiv` parameter is changed.
- [x] `python -c "from tree.config.app_config import app_config;
      print(len(app_config.sources.sources))"` prints `18`
      (5 RSS + 10 articles + 1 HF + 2 web).
- [x] `app_config.sources.sources` contains:
      - 5 instances where `isinstance(e, SubstackRssSource)`.
      - 10 instances where `isinstance(e, SubstackArticleSource)`.
      - 1 instance where `isinstance(e, HuggingFaceArxivSource)` with
        `uri='librarian-bots/arxiv-metadata-snapshot'`, `max_samples=10`,
        `fetch_content=False`, `batch_size=50`, `concurrency=10`.
      - 2 instances where `isinstance(e, WebSource)`
        (the Reddit URL and the Anthropic engineering article).
- [x] The two untyped entries (Reddit + Anthropic) load as `WebSource` via
      the load-time inference (no manual `type: web` in the YAML).
- [x] `tree.data.huggingface.arxiv_dataset_pipeline.ingest_arxiv_dataset` no
      longer references `app_config.sources.huggingface_arxiv_dataset` (the
      legacy attribute is gone). Its existing flow signature
      `ingest_arxiv_dataset(max_samples=None, fetch_content=None)` is preserved
      so #009's caller can override.
- [x] Unit tests under `tests/unit/config/` and
      `tests/unit/data/huggingface/` pass:
      `make memory-unit-tests tests/unit/config/ tests/unit/data/huggingface/`.
- [x] Format + lint + pre-commit clean (project convention).

## User Stories

### Story: Developer reloads the default config after the migration
1. Developer runs `python -c "from tree.config.app_config import app_config;
   from collections import Counter;
   print(Counter(type(e).__name__ for e in app_config.sources.sources))"`.
2. Output: `Counter({'SubstackArticleSource': 10, 'SubstackRssSource': 5, 'WebSource': 2, 'HuggingFaceArxivSource': 1})`.

### Story: Developer triggers the arxiv pipeline alone after migration
1. Developer runs the existing arxiv flow function directly (still served as
   a Prefect deployment in this task — orchestrator cleanup happens in #010):
   `await ingest_arxiv_dataset()`.
2. The flow reads `max_samples=10` and `fetch_content=False` from the
   `HuggingFaceArxivSource` entry in `app_config.sources.sources` and runs
   exactly as before.

### Story: Developer adds a new untyped URL to default.yaml
1. Developer appends `  - uri: https://example.com/post` to the bottom of
   `sources:` in `default.yaml`.
2. `app_config.sources.sources[-1]` is a `WebSource`, not a raw dict.

## Test outline

Update existing files; do NOT create new test files for this task.

- `tests/unit/config/test_app_config.py`:
  - Delete `test_urls_default_is_empty`, `test_urls_round_trip_from_yaml`.
  - Add `test_loads_default_yaml_sources` (counts by variant type).
  - Add `test_loads_default_yaml_huggingface_arxiv_entry` (uri + defaults).
  - Add `test_loads_default_yaml_normalizes_untyped_to_web` (the Reddit and
    Anthropic entries are `WebSource` instances).
- `tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py`: adjust any
  patches that reference `app_config.sources.huggingface_arxiv_dataset` to the
  new helper shape (`app_config.sources.sources`).

---

Blocked by: #006

## Log

### [SWE] 2026-05-01 12:12 — Implementation

**Files modified**
- `apps/memory/configs/default.yaml` — flattened `sources:` block to a flat
  list of typed entries (5 RSS + 10 articles + 1 HF arxiv + 2 untyped web).
- `apps/memory/src/tree/config/app_config.py` — `SourcesConfig` model
  validator now coerces a bare list into `{"sources": <list>}` so the YAML
  can write the flat shape directly under `AppConfig.sources`. Removed the
  `try/except` fallback around `load_app_config()`; we now load eagerly.
- `apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py` —
  `_get_huggingface_arxiv_defaults` now walks
  `app_config.sources.sources` and picks the first `HuggingFaceArxivSource`,
  falling back to `HuggingFaceArxivSource()` defaults if absent. Legacy
  `app_config.sources.huggingface_arxiv_dataset` reference fully removed.
- `apps/memory/tests/unit/config/test_app_config.py` — added
  `test_loads_default_yaml_sources_flat_shape` (variant counts),
  `test_loads_default_yaml_huggingface_arxiv_entry` (HF entry uri + defaults),
  `test_loads_default_yaml_normalizes_untyped_to_web` (Reddit + Anthropic),
  `test_default_yaml_round_trip_preserves_typed_variants`.
- `apps/memory/tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py`
  — added `TestGetHuggingfaceArxivDefaults` with two cases:
  picks-first-HF-source and falls-back-to-defaults. No legacy patches
  existed in this file that referenced the old shim shape, so nothing to
  remove.

**Tests**
- Unit: 384 passing, 0 failing, 0 warnings — `make memory-unit-tests`.
- Integration: N/A — no infra changes (per task spec, integration tests are
  scoped to #011).

**Acceptance criteria**
- [x] `default.yaml` flat shape — verified by inspection + variant-count
  test in `test_app_config.py::test_loads_default_yaml_sources_flat_shape`.
- [x] `len(app_config.sources.sources) == 18` — verified by the same test.
- [x] Variant counts (5 RSS / 10 article / 1 HF / 2 web) — same test.
- [x] HF entry preserves `uri`, `max_samples`, `fetch_content`,
  `batch_size`, `concurrency` —
  `test_loads_default_yaml_huggingface_arxiv_entry`.
- [x] Untyped entries normalize to `WebSource` —
  `test_loads_default_yaml_normalizes_untyped_to_web`.
- [x] `ingest_arxiv_dataset` no longer references the legacy attribute —
  `_get_huggingface_arxiv_defaults` rewritten;
  `TestGetHuggingfaceArxivDefaults` covers the new path.
- [x] Existing arxiv pipeline tests still pass — verified by full unit
  suite (`tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py`
  10 passed).
- [x] Format + lint + pre-commit clean — `make memory-format-check`,
  `make memory-lint-check`, `make pre-commit` all green.

**Evidence**
```
$ uv --directory apps/memory run python -c "from tree.config.app_config import app_config; from collections import Counter; print(len(app_config.sources.sources)); print(Counter(type(e).__name__ for e in app_config.sources.sources))"
18
Counter({'SubstackArticleSource': 10, 'SubstackRssSource': 5, 'WebSource': 2, 'HuggingFaceArxivSource': 1})

$ uv --directory apps/memory run python -c "from tree.data.huggingface.arxiv_dataset_pipeline import _get_huggingface_arxiv_defaults; print(_get_huggingface_arxiv_defaults())"
(10, False, 50, 10)

$ make memory-format-check && make memory-lint-check
136 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
============================= 384 passed in 20.44s =============================
```

**Notes**
- Per task scope, `_get_configured_substack_domains` in
  `tree.data.core.ingest` is intentionally NOT touched — that's #008.
- `tree.data.pipeline.ingest_all_data` (legacy dispatch) is also untouched
  — that's #009. Their tests may temporarily fail if/when the broader
  integration tests are run; unit-test suite is fully green here.
- Two test methods in `test_arxiv_dataset_pipeline.py` patch
  `app_config.sources` rather than the helper directly, ensuring the new
  flat-list lookup path is exercised end-to-end through
  `_get_huggingface_arxiv_defaults`.
- Code is uncommitted and uncommitted-pushed per process; awaiting Tester
  review.

### [Tester] 2026-05-01 12:35 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — Validate pyproject (skipped), prettier, ruff check, ruff format, biome check all Passed).
- Unit tests: 384 passed / 0 failed / 0 warnings (`make memory-unit-tests` — 25.22s).
- Integration tests: NOT RUN — local Docker infra is down (`docker compose ps` empty), and per the groomed spec ("Consumer-side updates needed in this task" + SWE log) integration coverage for the flat-source migration is explicitly scoped to #011. Running integration here would test the deliberately-broken legacy paths in `tree.data.pipeline.ingest_all_data` / `tree.data.core.ingest._get_configured_substack_domains` that #008 / #009 will fix. Imports of those modules still work cleanly (verified below), and the unit suite is green for everything in this task's blast radius (`tests/unit/config/`, `tests/unit/data/huggingface/`).
- Warnings: 0.

**E2E adversarial pass**
- Happy path: `uv --directory apps/memory run python -c "from collections import Counter; from tree.config.app_config import app_config; print(len(app_config.sources.sources)); print(Counter(type(e).__name__ for e in app_config.sources.sources))"` → `18` and `Counter({'SubstackArticleSource': 10, 'SubstackRssSource': 5, 'WebSource': 2, 'HuggingFaceArxivSource': 1})` — PASS.
- Happy path (HF helper): `_get_huggingface_arxiv_defaults()` → `(10, False, 50, 10)` — matches HF entry in YAML — PASS.
- Happy path (Web URIs): Reddit and Anthropic URIs both load as `WebSource` — PASS.
- Break path A — empty sources (`sources: []`): loaded cleanly, `len == 0` — PASS.
- Break path B — bogus type (`type: bogus_type`): raised `pydantic_core._pydantic_core.ValidationError` with the message `Input tag 'bogus_type' found using 'type' does not match any of the expected tags: 'substack_rss', 'substack_article', 'huggingface_arxiv', 'web'` — clear, actionable, no silent crash — PASS.
- Break path C — HF entry absent: `_get_huggingface_arxiv_defaults()` falls back to `HuggingFaceArxivSource()` defaults `(10, False, 50, 10)` without raising; verified via `APP_CONFIG_PATH=/tmp/qa007/no_hf.yaml` reload — PASS.
- Break path D — duplicate `uri` entries: accepted as-is (4 distinct entries kept). The spec does not forbid duplicates; documenting only — not a failure.
- Extra edge — empty `uri: ""`: clean `ValidationError` "String should have at least 1 character" — PASS.
- Extra edge — missing `uri`: clean `ValidationError` "Field required" — PASS.
- Extra edge — legacy bare-string entry (`- "https://example.com"`): clean `ValidationError` "Input should be a valid dictionary or object" — PASS (not silently accepted).
- Module-import smoke (legacy modules untouched but must still import): `tree.data.pipeline`, `tree.data.core.ingest`, `tree.orchestrator` all import cleanly — PASS.

**Acceptance criteria**
- [x] PASS — `default.yaml` flat shape exactly per snippet — `git diff apps/memory/configs/default.yaml` matches the target spec; 18 entries; HF params preserved verbatim (`max_samples: 10`, `fetch_content: false`, `batch_size: 50`, `concurrency: 10`).
- [x] PASS — `len(app_config.sources.sources) == 18` — Evidence: happy-path command output.
- [x] PASS — Variant counts (5 RSS / 10 article / 1 HF / 2 web) and HF field values — Evidence: `tests/unit/config/test_app_config.py::TestLoadAppConfig::test_loads_default_yaml_sources_flat_shape` + `test_loads_default_yaml_huggingface_arxiv_entry`.
- [x] PASS — Reddit + Anthropic load as `WebSource` via inference — Evidence: `test_loads_default_yaml_normalizes_untyped_to_web` and live happy-path output.
- [x] PASS — `ingest_arxiv_dataset` no longer references `app_config.sources.huggingface_arxiv_dataset`; signature `(max_samples=None, fetch_content=None)` preserved — Evidence: `git diff apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py` shows the legacy attribute removed; `TestGetHuggingfaceArxivDefaults::test_picks_first_huggingface_arxiv_source` and `test_falls_back_to_huggingface_arxiv_source_defaults` cover both branches.
- [x] PASS — Scoped unit tests pass — Evidence: full `make memory-unit-tests` 384 passed including all of `tests/unit/config/` and `tests/unit/data/huggingface/`.
- [x] PASS — Format + lint + pre-commit clean — Evidence: `make pre-commit` output above.

**Evidence**
```
$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
============================= 384 passed in 25.22s =============================

$ uv --directory apps/memory run python -c "from collections import Counter; from tree.config.app_config import app_config; print(len(app_config.sources.sources)); print(Counter(type(e).__name__ for e in app_config.sources.sources))"
18
Counter({'SubstackArticleSource': 10, 'SubstackRssSource': 5, 'WebSource': 2, 'HuggingFaceArxivSource': 1})

$ uv --directory apps/memory run python -c "from tree.data.huggingface.arxiv_dataset_pipeline import _get_huggingface_arxiv_defaults; print(_get_huggingface_arxiv_defaults())"
(10, False, 50, 10)

$ # Break path B (bogus type)
ValidationError : 1 validation error for AppConfig
sources.sources.0
  Input tag 'bogus_type' found using 'type' does not match any of the expected tags: 'substack_rss', 'substack_article', 'huggingface_arxiv', 'web'

$ # Break path C (no HF entry)
helper: (10, False, 50, 10)
```

**Other issues found**
- Duplicate `uri` entries are accepted silently. Not a blocker (spec doesn't forbid it), but worth a follow-up: the dispatcher in #008 may want a `uri`-uniqueness validator (or at least a warning) so misconfiguration doesn't double-ingest. Flagging for orchestrator's awareness.
- Two unit test cases in `TestGetHuggingfaceArxivDefaults` patch `tree.data.huggingface.arxiv_dataset_pipeline.app_config.sources` (an attribute on the imported `app_config` instance). This works because the helper reads `app_config.sources.sources` at call time and the mock replaces `.sources`. Just a note — passes cleanly.
- `_get_configured_substack_domains` in `tree.data.core.ingest` and `ingest_all_data` in `tree.data.pipeline` still reference legacy attrs (`.substack`, `.substack_articles`, `.huggingface_arxiv_dataset`, `.urls`) that no longer exist on `SourcesConfig`. Confirmed broken-by-design; #008 / #009 cover the fix. Module imports still succeed because the references are inside function bodies, not module-level.

**VERDICT: PASS**
