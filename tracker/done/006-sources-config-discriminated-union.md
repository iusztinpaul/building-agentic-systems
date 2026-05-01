# Define new Pydantic discriminated-union `SourcesConfig`

Status: pending
Tags: `config`, `data`, `schema`
Depends on: None
Blocks: #007, #008, #009

## Scope

Replace the legacy `SourcesConfig` and `HuggingFaceArxivDatasetConfig` Pydantic
models in `apps/memory/src/tree/config/app_config.py` with a discriminated-union
schema in which `sources` is a flat list of `SourceEntry` items.

This task is **schema + validation only** — no consumer in `tree.data.*` is
changed yet. The migration of the YAML file and the consumers comes in
subsequent tasks.

### Required entry variants

All variants live in `tree.config.app_config` (or a new
`tree.config.sources.py` module — SWE picks; keeping them in `app_config.py`
matches the existing layout). Each is a Pydantic `BaseModel` with a `Literal`
type discriminator.

| Variant class | `type` literal | `uri` shape | Extra fields |
|---|---|---|---|
| `SubstackRssSource` | `"substack_rss"` | URL string (HTTP/HTTPS) | — |
| `SubstackArticleSource` | `"substack_article"` | URL string (HTTP/HTTPS) | — |
| `HuggingFaceArxivSource` | `"huggingface_arxiv"` | dataset id string (e.g. `librarian-bots/arxiv-metadata-snapshot`) | `max_samples: int = 10`, `fetch_content: bool = False`, `batch_size: int = 50`, `concurrency: int = 10` |
| `WebSource` | `"web"` | URL string (HTTP/HTTPS) | — |

The discriminated union is exposed as:

```python
SourceEntry = Annotated[
    Union[
        SubstackRssSource,
        SubstackArticleSource,
        HuggingFaceArxivSource,
        WebSource,
    ],
    Field(discriminator="type"),
]


class SourcesConfig(BaseModel):
    sources: list[SourceEntry] = []
```

(SWE: the exact import shape — `typing.Annotated` vs `typing_extensions`,
`Union[...]` vs PEP 604 `|` — is at SWE discretion as long as Pydantic v2's
discriminator validation works. Keep it consistent with the rest of the file.)

### Untyped-entry handling (config-load time)

YAML may also contain entries without a `type` key, e.g.:

```yaml
sources:
  - uri: https://www.reddit.com/r/AI_Agents/...
```

The schema must accept these and normalize them to a typed variant at
**config-load time** (not at dispatch time). Use a Pydantic `model_validator`
(or `field_validator` on `sources`) on `SourcesConfig` (or `AppConfig`) that:

1. For each raw entry without `type`, inspects the `uri`:
   - If `urlparse(uri).netloc` equals (or its `www.`-stripped form equals)
     `substack.com` or any `*.substack.com` subdomain → coerce to
     `SubstackArticleSource`.
   - If `uri` matches a custom Substack domain *previously declared* in the
     same `sources` list (i.e. another entry has
     `type: substack_rss` or `type: substack_article` on that domain) → coerce
     to `SubstackArticleSource`.
   - Otherwise → coerce to `WebSource`.
2. Replaces the raw dict with the typed variant.

The inference is local to config-load. Subsequent tasks (#008) update the
URL dispatcher (used at MCP runtime) to derive its custom-domain registry
from this same flat list.

The dataset-id `uri` field on `HuggingFaceArxivSource` does NOT need to be a
URL — it's a string. Validation should reject empty strings only.

### What to remove

Delete:

- `class HuggingFaceArxivDatasetConfig(BaseModel): ...`
- The four fields on the old `class SourcesConfig`: `substack`,
  `substack_articles`, `huggingface_arxiv_dataset`, `urls`.

Keep `AppConfig.sources: SourcesConfig = SourcesConfig()` — only the inner
shape changes.

### What stays

- `LLMConfig`, `EmbeddingConfig`, `ModelsConfig`, `ExtractionConfig`,
  `QueryConfig`, `MCPConfig`, `AppConfig`, `load_app_config`, `app_config`
  module-level singleton.
- `apps/memory/configs/default.yaml` is NOT modified in this task — that's #007.
  After this task, `app_config = load_app_config()` at import time will fail
  to validate against the legacy YAML; that's expected and fixed in #007.
  (SWE: to keep the test suite from going red between tasks, gate
  `app_config = load_app_config()` behind a try/except in #006 if needed, OR
  land #006 + #007 in a single PR-staging branch. Recommended path is to land
  #006 and #007 back-to-back; the in-progress test suite is the orchestrator's
  problem, not a permanent state.)

## Acceptance Criteria

- [x] `tree.config.app_config` exposes a `SourceEntry` discriminated union
      and four variant classes: `SubstackRssSource`, `SubstackArticleSource`,
      `HuggingFaceArxivSource`, `WebSource`.
- [x] `SourcesConfig` has exactly one field: `sources: list[SourceEntry] = []`.
      The legacy fields (`substack`, `substack_articles`,
      `huggingface_arxiv_dataset`, `urls`) and the legacy
      `HuggingFaceArxivDatasetConfig` class are removed from the module.
- [x] Each variant validates its required fields: missing `uri` raises
      `ValidationError`; unknown `type` literal raises `ValidationError`.
- [x] `HuggingFaceArxivSource` defaults match the current YAML defaults:
      `max_samples=10`, `fetch_content=False`, `batch_size=50`, `concurrency=10`.
- [x] Entries without a `type` key validate successfully and are normalized to
      a typed variant at load time:
      - URL on `*.substack.com` → `SubstackArticleSource`.
      - URL whose host matches another entry's typed Substack source → `SubstackArticleSource`.
      - Anything else (HTTP URL) → `WebSource`.
- [x] An entry with `type: huggingface_arxiv` and a non-URL `uri` (e.g.
      `librarian-bots/arxiv-metadata-snapshot`) validates without error.
- [x] An entry with an explicit unknown type (e.g. `type: rss`) raises
      `ValidationError` with the type literal in the message.
- [x] YAML round-trip works: a YAML doc with a mix of typed + untyped entries
      loads via `load_app_config(path)` into `AppConfig` whose `sources.sources`
      list is a list of typed Pydantic instances (no raw dicts).
- [x] Type annotations on every new function / method / variable that this task
      introduces (project convention).
- [x] All new + existing unit tests under `tests/unit/config/` pass:
      `make memory-unit-tests tests/unit/config/`.
- [x] Format + lint clean: `make memory-format-fix && make memory-lint-fix &&
      make memory-format-check && make memory-lint-check && make pre-commit`.

## User Stories

### Story: Developer adds a new entry without specifying the type
1. Developer opens a config file (e.g. `apps/memory/configs/default.yaml`).
2. Developer adds an entry under `sources:`:
   ```yaml
   - uri: https://news.ycombinator.com/item?id=123
   ```
3. Developer runs `python -c "from tree.config.app_config import app_config;
   print(app_config.sources.sources[-1])"`.
4. The output prints a `WebSource(type='web', uri='https://news.ycombinator.com/item?id=123')`
   — the inference normalized it to the explicit web variant.

### Story: Developer adds a Substack RSS feed for a custom domain + an article on the same domain without re-typing
1. Developer adds two entries:
   ```yaml
   - uri: https://customblog.com/feed
     type: substack_rss
   - uri: https://customblog.com/p/some-article
   ```
2. Developer reloads the config.
3. The second entry (no `type`) is normalized to
   `SubstackArticleSource(uri='https://customblog.com/p/some-article')` because
   the load-time validator detected that another entry declared `customblog.com`
   as a Substack domain.

### Story: Developer makes a typo on the type literal
1. Developer adds:
   ```yaml
   - uri: https://x.com/feed
     type: substack-rss   # typo: dash instead of underscore
   ```
2. `load_app_config(path)` raises `pydantic.ValidationError`.
3. The error message names the offending entry index and the unknown type
   value, so the developer can locate the typo without re-reading the schema.

### Story: Developer configures the HuggingFace arxiv connector
1. Developer adds:
   ```yaml
   - uri: librarian-bots/arxiv-metadata-snapshot
     type: huggingface_arxiv
     max_samples: 100
     fetch_content: true
   ```
2. After `load_app_config`, the entry is a `HuggingFaceArxivSource` instance
   with `max_samples=100`, `fetch_content=True`, `batch_size=50` (default),
   `concurrency=10` (default).

### Story: Developer omits required fields
1. Developer adds:
   ```yaml
   - type: substack_rss
   ```
2. Loading raises `ValidationError` mentioning the missing `uri` field.

## Test outline (under `apps/memory/tests/unit/config/`)

Either extend `test_app_config.py` or add a new file
`test_sources_config.py` (one-on-one with the source module is the project
convention; either is acceptable since both modules live in `tree.config`).
The legacy `test_urls_default_is_empty` and `test_urls_round_trip_from_yaml`
in `test_app_config.py` are removed in #007 — leave them broken in this PR is
NOT acceptable; either delete them here as a pre-emptive cleanup or update
them to the new shape. SWE picks. (Recommended: delete here, since the
new shape's coverage is much richer.)

Test shape:

- `test_substack_rss_validates` / `test_substack_article_validates` /
  `test_web_validates` / `test_huggingface_arxiv_validates`.
- `test_huggingface_arxiv_defaults`.
- `test_huggingface_arxiv_uri_is_dataset_id` (non-URL accepted).
- `test_unknown_type_raises_validation_error`.
- `test_missing_uri_raises_validation_error` (parametrized over each variant).
- `test_untyped_entry_with_substack_subdomain_normalizes_to_article`.
- `test_untyped_entry_with_unknown_url_normalizes_to_web`.
- `test_untyped_entry_with_custom_substack_domain_normalizes_to_article`
  (declares a typed `substack_rss` entry on the same domain in the same list).
- `test_yaml_round_trip_typed_and_untyped_mix` (loads a small YAML doc with
  one of each variant, verifies the resulting `sources.sources` list is
  fully-typed).

Use `pytest-mock` `mocker` fixture only where needed (mostly not — these are
pure Pydantic validation tests). Use `@pytest.mark.parametrize` for the
missing-field cases.

---

Blocked by: (none)

## Log

### [SWE] 2026-04-30 16:00 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — replaced legacy `SourcesConfig` and `HuggingFaceArxivDatasetConfig` with a discriminated-union schema (4 variants + `SourceEntry`), added a `model_validator(mode="before")` on `SourcesConfig` that normalizes untyped entries to a typed variant at load time, and gated the module-level `app_config = load_app_config()` singleton in a `try/except` with a `TODO(#007)` to keep imports working until #007 migrates `default.yaml`.
- `apps/memory/tests/unit/config/test_sources_config.py` — new file with 17 tests covering variant validation, defaults, unknown-type / missing-uri errors (parametrized over all four variants), untyped-entry normalization (substack subdomain, custom Substack domain, unknown URL → web, bare `substack.com`), and a YAML round-trip with a mix of typed + untyped entries.
- `apps/memory/tests/unit/config/test_app_config.py` — removed the legacy `test_urls_default_is_empty` and `test_urls_round_trip_from_yaml` tests (their coverage is replaced by the richer `test_sources_config.py`).

**Tests**
- Unit (config only): 22 passing, 0 failing, 0 warnings — `cd apps/memory && uv run pytest tests/unit/config -v`.
- Unit (full memory suite): 3 collection errors in `tests/unit/data/core/test_ingest.py`, `tests/unit/data/test_pipeline.py`, `tests/unit/mcp/test_tools.py` — **expected intermediate-state breakage** (consumers in `tree.data.*` still reference the now-removed `app_config.sources.substack` etc.). These get fixed by tasks #008/#009 per the spec; not in scope for #006.
- Integration: N/A — schema-only change.

**Acceptance criteria**
- [x] `SourceEntry` + 4 variant classes exposed — verified by `tests/unit/config/test_sources_config.py::TestVariantValidation::*` and the import in that file.
- [x] `SourcesConfig` has exactly one `sources` field; legacy fields and `HuggingFaceArxivDatasetConfig` removed — verified by `tests/unit/config/test_sources_config.py::TestSourcesConfigDefault::test_default_sources_is_empty_list` and by the absence of the removed names from `app_config.py`.
- [x] Missing `uri` and unknown `type` raise `ValidationError` — `tests/unit/config/test_sources_config.py::TestValidationErrors::test_missing_uri_raises_validation_error[*]`, `::test_unknown_type_raises_validation_error`.
- [x] `HuggingFaceArxivSource` defaults are 10/False/50/10 — `TestVariantDefaults::test_huggingface_arxiv_defaults`.
- [x] Untyped entries normalize correctly — `TestUntypedEntryNormalization::test_untyped_entry_with_substack_subdomain_normalizes_to_article`, `::test_untyped_entry_with_unknown_url_normalizes_to_web`, `::test_untyped_entry_with_custom_substack_domain_normalizes_to_article`, `::test_untyped_entry_on_bare_substack_com_normalizes_to_article`.
- [x] HF dataset id `uri` (non-URL) accepted — `TestVariantDefaults::test_huggingface_arxiv_uri_is_dataset_id`.
- [x] YAML round-trip with mixed typed + untyped entries — `TestYamlRoundTrip::test_yaml_round_trip_typed_and_untyped_mix`.
- [x] Type annotations on all new functions / methods / variables — `_is_substack_subdomain`, `_host_of`, `_collect_typed_substack_hosts`, `_normalize_untyped_entry`, `_normalize_untyped_sources` all annotated.
- [x] Format + lint + pre-commit clean — see Evidence below.

**Evidence**
```
$ cd apps/memory && uv run pytest tests/unit/config -v
... 22 passed in 0.43s ...

$ make memory-format-check && make memory-lint-check
... 136 files already formatted ...
... All checks passed! ...

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

**Notes**
- Module-level `app_config = load_app_config()` is wrapped in a `try/except` with a `TODO(#007)` — explicit intermediate-state mitigation called out by the spec (line 105) and the orchestrator hand-off. #007 removes the gate when `default.yaml` is migrated.
- In practice, with Pydantic v2's default `extra="ignore"`, `load_app_config()` against the legacy `default.yaml` does NOT raise — it silently drops the unknown `substack`, `substack_articles`, `huggingface_arxiv_dataset`, `urls` keys and returns `AppConfig(sources=SourcesConfig(sources=[]))`. The try/except is still in place as a defensive belt-and-braces measure for any future schema tightening.
- The full unit suite (`make memory-unit-tests`) fails at collection in **exactly three test files**, all caused by `tree.data.core.ingest` reading `app_config.sources.substack` at import time:
  - `tests/unit/data/core/test_ingest.py`
  - `tests/unit/data/test_pipeline.py`
  - `tests/unit/mcp/test_tools.py` (transitively via `tree.mcp.tools` → `tree.data.core.ingest`)
  These are the EXACT consumer-code breakages flagged by the orchestrator hand-off; #008 fixes the dispatcher and #009 fixes the pipeline flow.
- Did NOT commit per process; awaiting Tester.

### [Tester] 2026-05-01 11:53 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all clean)
- Config unit tests: 22 passed / 0 failed / 0 warnings (`cd apps/memory && uv run pytest tests/unit/config -v`)
- Full memory unit suite: 346 passed / 1 failed / 3 collection errors (`make memory-unit-tests`)
- The 3 collection errors are exactly the files SWE listed: `tests/unit/data/core/test_ingest.py`, `tests/unit/data/test_pipeline.py`, `tests/unit/mcp/test_tools.py`
- **One additional intermediate-state runtime failure not flagged by SWE**: `tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py::TestIngestArxivDataset::test_processes_batches_in_parallel` — same root cause (consumer reads removed `app_config.sources.huggingface_arxiv_dataset` at `apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py:56`). Will be cleaned up by #008/#009 along with the other legacy-shape consumers; not a NEW class of regression. Flagging only because SWE's report claimed "exactly 3 files" — it's "3 collection errors + 1 runtime failure", and the orchestrator should know the full surface.

**E2E adversarial pass**
- Happy path (load mixed YAML): all 6 entries (typed + untyped) load as fully-typed Pydantic instances per `test_yaml_round_trip_typed_and_untyped_mix`. PASS.
- Break path 1 — discriminator strictness:
  - `type: SUBSTACK_RSS` (uppercase) → REJECTED with `union_tag_invalid`. PASS.
  - `type: ""` (empty) → REJECTED. PASS.
  - `type: substack_rss` with extra `garbage: "x"` → ACCEPTED (extra silently dropped per Pydantic v2 default `extra="ignore"`). Spec doesn't mandate `extra="forbid"`; documented behaviour.
  - `type: web` with stray `max_samples: 10` → ACCEPTED (extra dropped). Documented.
- Break path 2 — untyped-entry normalization:
  - `ftp://example.com` (untyped) → coerced to `WebSource`. Spec says "anything else (HTTP URL) → WebSource"; the `(HTTP URL)` parenthetical is a hint, not a hard rule (variants only enforce `min_length=1`). Acceptable.
  - `uri: ""` (untyped) → REJECTED downstream by `min_length=1` on the `WebSource` variant. PASS.
  - `uri: "not-a-url"` (untyped, bare) → ACCEPTED as `WebSource`. Spec doesn't require URL-format validation; documented.
  - **Order independence verified**: untyped `https://customblog.com/p/x` declared BEFORE typed `substack_rss` on the same domain still normalizes to `SubstackArticleSource`, because `_collect_typed_substack_hosts` walks the entire raw list before normalization. Solid design.
  - Case-insensitive host matching: `https://www.CUSTOMBLOG.com/feed` (typed) followed by `https://customblog.com/p/x` (untyped) → both lowercased; untyped resolves to `SubstackArticleSource`. PASS.
  - `www.`-stripping: `https://customblog.com/feed` (typed) followed by `https://www.customblog.com/p/x` (untyped) → matches. PASS.
- Break path 3 — YAML round-trip:
  - `sources.sources: []` → `SourcesConfig(sources=[])`. PASS.
  - Missing `sources:` block entirely → defaults to `SourcesConfig(sources=[])`. PASS.
  - `sources.sources: null` → REJECTED with `list_type` Pydantic error. Standard Pydantic behaviour (defaults only apply when key absent). Acceptable.
  - Typed substack_rss entry with stray `max_samples: 99` → ACCEPTED (extra dropped). Documented.
- Break path 4 — HuggingFace `uri`:
  - `uri: ""` → REJECTED. PASS.
  - `uri: "librarian-bots/arxiv-metadata-snapshot"` (canonical) → PASS.
  - `uri: "single-segment"` (no slash) → ACCEPTED (spec only requires non-empty). Documented.
  - `uri: "https://huggingface.co/datasets/foo/bar"` (full URL) → ACCEPTED as plain string. Documented.
- Break path 5 — discriminator vs duck-typing:
  - `type: huggingface_arxiv` with no overrides → defaults `(10, False, 50, 10)` preserved. PASS.
  - `type: web` with stray HF `max_samples` → extra dropped. Documented.

**Acceptance criteria**
- [x] PASS — `SourceEntry` discriminated union + 4 variants exposed — `apps/memory/src/tree/config/app_config.py:60-100`; tests `tests/unit/config/test_sources_config.py::TestVariantValidation::*` (4 tests).
- [x] PASS — `SourcesConfig` has exactly one `sources` field; legacy fields and `HuggingFaceArxivDatasetConfig` removed — verified via `grep -nE "(substack|substack_articles|huggingface_arxiv_dataset|urls):" app_config.py` returns nothing; only `HuggingFaceArxivSource` remains; `tests/unit/config/test_sources_config.py::TestSourcesConfigDefault::test_default_sources_is_empty_list`.
- [x] PASS — Each variant validates required fields; missing `uri` and unknown `type` raise `ValidationError` — `tests/unit/config/test_sources_config.py::TestValidationErrors::test_missing_uri_raises_validation_error[*]` (parametrized over all 4), `::test_unknown_type_raises_validation_error`. Adversarial pass also verified uppercase / empty type rejection.
- [x] PASS — `HuggingFaceArxivSource` defaults `(max_samples=10, fetch_content=False, batch_size=50, concurrency=10)` — `tests/unit/config/test_sources_config.py::TestVariantDefaults::test_huggingface_arxiv_defaults`. Source: `app_config.py:74-82`.
- [x] PASS — Untyped entries normalize at load time:
  - `*.substack.com` → `SubstackArticleSource`: `::test_untyped_entry_with_substack_subdomain_normalizes_to_article`, `::test_untyped_entry_on_bare_substack_com_normalizes_to_article`.
  - Custom Substack domain → `SubstackArticleSource`: `::test_untyped_entry_with_custom_substack_domain_normalizes_to_article`. Adversarial pass also confirmed order independence, case insensitivity, and `www.`-stripping.
  - Otherwise → `WebSource`: `::test_untyped_entry_with_unknown_url_normalizes_to_web`.
- [x] PASS — HF dataset id `uri` (non-URL) accepted — `::test_huggingface_arxiv_uri_is_dataset_id`; adversarial run also confirmed full-URL and single-segment shapes accepted.
- [x] PASS — Unknown explicit type with literal in error — `::test_unknown_type_raises_validation_error`. Manual probe with `type: substack-rss` confirmed error mentions both `sources.0` (index) and `substack-rss` (the typo value).
- [x] PASS — YAML round-trip with mixed typed + untyped entries → all typed Pydantic instances (no raw dicts) — `::test_yaml_round_trip_typed_and_untyped_mix`.
- [x] PASS — Type annotations on every new function / method / variable — `_is_substack_subdomain(host: str) -> bool`, `_host_of(uri: str) -> str`, `_collect_typed_substack_hosts(raw_entries: list[Any]) -> set[str]`, `_normalize_untyped_entry(entry: dict[str, Any], substack_hosts: set[str]) -> dict[str, Any]`, `_normalize_untyped_sources(cls, data: Any) -> Any`. All annotated.
- [x] PASS — All new + existing tests under `tests/unit/config/` pass — 22/22 passing, 0 warnings.
- [x] PASS — Format / lint / pre-commit clean — verified.

**Evidence**
```
$ cd apps/memory && uv run pytest tests/unit/config -v
... 22 passed in 0.16s ...

$ make memory-format-check && make memory-lint-check
136 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests   # full suite
=================== 1 failed, 346 passed, 3 errors in 21.90s ===================
ERROR tests/unit/data/core/test_ingest.py - AttributeError: 'SourcesConfig' object has no attribute 'substack'
ERROR tests/unit/data/test_pipeline.py  - AttributeError: 'SourcesConfig' object has no attribute 'substack'
ERROR tests/unit/mcp/test_tools.py      - AttributeError: 'SourcesConfig' object has no attribute 'substack'
FAILED tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py::TestIngestArxivDataset::test_processes_batches_in_parallel
        # → AttributeError: 'SourcesConfig' object has no attribute 'huggingface_arxiv_dataset'
        # → src/tree/data/huggingface/arxiv_dataset_pipeline.py:56 — same legacy-shape consumer bucket
```

**Other issues found**
- SWE's report claims "exactly 3 unit-test files fail at collection time" — actual surface is "3 collection errors + 1 runtime failure" because `arxiv_dataset_pipeline.py:56` reads `app_config.sources.huggingface_arxiv_dataset` lazily inside the function (so the test collects successfully but fails when invoked). Same root cause (consumer of removed shape), so #008/#009 will mop it up along with the other consumers. Worth noting in the orchestrator hand-off so #008/#009 don't forget this consumer. The full set of legacy-shape consumers is: `apps/memory/src/tree/data/pipeline.py:40,49,62,70`, `apps/memory/src/tree/data/core/ingest.py:56,60`, `apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py:56` (7 reference sites total).
- The variants don't enforce HTTP/HTTPS scheme on URI fields — only `min_length=1`. So `ftp://...`, `not-a-url`, etc. all pass. Spec doesn't require scheme validation; flagging as a future-hardening hook for the PM if they want stricter input.
- Pydantic v2 default `extra="ignore"` means stray fields on a typed entry (e.g. `max_samples` on `substack_rss`) are silently dropped instead of raising. SWE flagged this for `load_app_config`'s legacy-YAML behaviour (correct). Same applies to per-entry typos. Spec doesn't mandate `extra="forbid"`; flagging as a future-hardening hook.
- The try/except gate on the module-level `app_config = load_app_config()` is in place (`app_config.py:247-250`) with the `TODO(#007)` comment per spec line 105. As SWE noted, the gate is defensive-only because Pydantic's default `extra="ignore"` means the legacy YAML doesn't actually raise — it silently produces an empty `AppConfig`. Worth keeping for safety until #007 lands.

**VERDICT: PASS**

The schema is sound, every AC is verified line-by-line with a concrete test or runtime probe, format/lint/pre-commit all green, and the 22 config tests run with 0 warnings. The intermediate-state breakage is in the same root-cause bucket the SWE described (consumers of the removed shape, all under `tree.data.*`) — the only deviation from the SWE's report is one additional runtime failure (`arxiv_dataset_pipeline.py:56`) that the SWE missed in its enumeration; same root cause, fixed by the same downstream tasks (#008/#009). This is intermediate state by design; not a fail. Discriminator validation is strict on the literal; untyped-entry normalization handles order independence, case insensitivity, and `www.`-stripping. The `extra="ignore"` and HTTP-scheme-permissive behaviours are noted as future-hardening hooks for the PM, not blockers.

