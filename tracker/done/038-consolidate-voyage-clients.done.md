# Consolidate Voyage embedding clients into the multimodal one

Status: in-progress
Tags: `cleanup`, `refactor`, `voyage`, `embeddings`
Depends on: #037
Blocks: —

## Scope

Operator decision (verbatim): "We already have a voyage embedding model
implementation available at
`apps/memory/src/tree/models/voyage_multimodal_embedding.py` — I want to
use only this one. Use this multimodal version while adding any useful
logic to it."

Resolution path picked at clarification: **switch the project default
model to `voyage-multimodal-3`** (the multimodal endpoint can't serve
`voyage-3` — Voyage returns HTTP 400). The text client added in #037 is
removed; its only useful piece (the 429 exponential-backoff retry loop)
is folded into the multimodal client. Both `voyage-multimodal-3` and
`voyage-3` are 1024-d, so the YAML `dimensions: 1024` does not change
and the mongot vector index shape is preserved. **But** the vector
*semantic space* changes — see the operator-warning section in
CLAUDE.md for the silent-corruption risk.

## Acceptance Criteria

- [x] `apps/memory/src/tree/models/voyage_multimodal_embedding.py` carries the 429 exponential-backoff retry loop (constructor param, schedule constant, retry loop, exhaustion → `ExtractionError`).
- [x] Multimodal payload shape preserved (`inputs: [{content: [{type: text, text: ...}]}]` — NOT the text-endpoint `input: [str]` shape).
- [x] `apps/memory/configs/default.yaml` sets `models.embedding.model: voyage-multimodal-3` (provider/dimensions unchanged).
- [x] `apps/memory/src/tree/models/voyage_embedding.py` and its test file are removed via `git rm`.
- [x] `apps/memory/src/tree/models/get_model.py`'s `voyage` branch no longer routes on `model_name.startswith("voyage-multimodal")` — always returns `VoyageMultimodalEmbeddingModel`.
- [x] `apps/memory/tests/unit/models/test_voyage_multimodal_embedding.py` covers: 429 retry succeeds eventually, 429 exhausted → `ExtractionError("rate-limit retries exhausted")`, fail-fast on non-429 non-200, mocked-time backoff.
- [x] `apps/memory/tests/unit/models/test_get_model.py` collapses the two voyage subtests into one (no more text-vs-multimodal routing branch).
- [x] CLAUDE.md's voyage-3 rebuild runbook references the new default and surfaces the **vector-space-change** warning.
- [x] `make memory-format-fix && make memory-lint-fix && make pre-commit && make memory-unit-tests` clean.
- [x] No `VoyageEmbeddingModel` or `voyage_embedding` references remain in `apps/memory/src`, `apps/memory/scripts`, or `apps/memory/tests` (tracker / CLAUDE.md historical references are OK).

## Log

### [SWE] 2026-05-19 17:00 — Implementation

**Files modified**
- `apps/memory/src/tree/models/voyage_multimodal_embedding.py` — folded in the 429 exponential-backoff loop from the deleted text client; added `_DEFAULT_RATE_LIMIT_BACKOFF_SECONDS` constant + `rate_limit_backoff_seconds` constructor param; module/class docstrings now document the retry behavior; preserved the multimodal payload shape (`inputs: [{content: [{type: text, text: ...}]}]`).
- `apps/memory/src/tree/models/get_model.py` — removed the `model_name.startswith("voyage-multimodal")` routing branch; the `voyage` provider now always returns `VoyageMultimodalEmbeddingModel`; comment block rewritten to explain the project pinned the multimodal model family.
- `apps/memory/configs/default.yaml` — `models.embedding.model: voyage-3` → `voyage-multimodal-3` (provider/dimensions unchanged).
- `apps/memory/src/tree/config/app_config.py` — Pydantic `EmbeddingConfig.model` default flipped to `"voyage-multimodal-3"`.
- `apps/memory/tests/unit/models/test_voyage_multimodal_embedding.py` — new `TestVoyageMultimodalRateLimitRetry` class covering: 429-then-success, 429-exhaustion → `ExtractionError("rate-limit retries exhausted")`, fail-fast on 500, fail-fast on 401. Patterns ported verbatim from the deleted `test_voyage_embedding.py`.
- `apps/memory/tests/unit/models/test_get_model.py` — dropped the `VoyageEmbeddingModel` import; collapsed the two voyage subtests into a single `test_returns_voyage_multimodal_embedding`.
- `apps/memory/tests/unit/config/test_app_config.py` — assertion updated to expect `voyage-multimodal-3`.
- `CLAUDE.md` — renamed the rebuild-runbook heading to drop the model-specific name; added a `[!CAUTION]` block warning that `voyage-3 → voyage-multimodal-3` is a silent-corruption risk because dimensions match but vector space does not.

**Files deleted (via `git rm`)**
- `apps/memory/src/tree/models/voyage_embedding.py`
- `apps/memory/tests/unit/models/test_voyage_embedding.py`

**Tests**
- Unit: 1201 passing, 0 failing (`make memory-unit-tests`) — full memory-app suite green.
- Targeted: `tests/unit/models/test_voyage_multimodal_embedding.py` ran 15/15 passing (including 4 new rate-limit-retry tests). `test_get_model.py` 10/10. `test_app_config.py` collapsed-voyage assertions green.
- Integration: N/A — orchestrator runs Tester next.

**Acceptance criteria**
- [x] 429 retry loop folded into multimodal client — verified by `test_voyage_multimodal_embedding.py::TestVoyageMultimodalRateLimitRetry::*`.
- [x] Payload shape unchanged (`inputs: [{content: [...]}]`) — verified by the pre-existing `test_embed_sends_correct_payload`.
- [x] YAML default flipped to `voyage-multimodal-3` — verified by `test_app_config.py::test_loads_default_yaml_top_level` and end-to-end smoke output below.
- [x] Text client + its test deleted via `git rm` — verified by `git status` (D entries) and `python -c "import tree.models.voyage_embedding"` raising `ModuleNotFoundError`.
- [x] `get_model.py` routing branch removed — verified by `test_get_model.py::test_returns_voyage_multimodal_embedding` and by reading the simplified branch.
- [x] CLAUDE.md sweep — runbook heading + body refer to both `voyage-3` and `voyage-multimodal-3` correctly; new `[!CAUTION]` warning surfaces the vector-space-change silent-corruption risk. The dim-mismatch anchor `Embedding dimension mismatch:` in `core.py` (line 503) is unchanged and still matches the runbook quote verbatim.
- [x] `make memory-format-fix && make memory-lint-fix && make pre-commit && make memory-unit-tests` clean — output below.
- [x] No `VoyageEmbeddingModel` or `voyage_embedding` import references remain in `src/scripts/tests`. The lone remaining `voyage_embedding` token is a historical-context mention in a test docstring; no live imports.

**Evidence**

End-to-end smoke (`get_embedding_model()` with a stub API key under the live YAML):
```
$ VOYAGE_API_KEY=stub-for-smoke uv run python -c "..."
Instance type: VoyageMultimodalEmbeddingModel
Is multimodal client: True
Reported dimensions: 1024
Has rate_limit_backoff_seconds attr: True
Backoff schedule length: 8
```

Deleted module no longer importable:
```
$ uv run python -c "from tree.models.voyage_embedding import VoyageEmbeddingModel"
ModuleNotFoundError: No module named 'tree.models.voyage_embedding'
```

Grep evidence (clean):
```
$ grep -rn "VoyageEmbeddingModel" apps/memory/src apps/memory/scripts apps/memory/tests --include="*.py"
(no output)

$ grep -rn "voyage_embedding" apps/memory/src apps/memory/scripts apps/memory/tests --include="*.py"
apps/memory/tests/unit/models/test_voyage_multimodal_embedding.py:199:    coverage that used to live in ``test_voyage_embedding.py``.
```

Format / lint / pre-commit:
```
$ make memory-format-fix
239 files left unchanged
$ make memory-lint-fix
All checks passed!
$ make pre-commit
prettier..............Passed
ruff check............Passed
ruff format...........Passed
biome check (harness).Passed
KGQuery discipline (memory).Passed
```

Unit-test tail:
```
$ make memory-unit-tests
...
tests/unit/models/test_voyage_multimodal_embedding.py ...............    [ 98%]
tests/unit/test_check_kgquery_discipline.py ........                     [ 98%]
tests/unit/test_migrate_multi_tenancy.py ..............                  [100%]
============================ 1201 passed in 40.63s =============================
```

**New YAML default snippet** (`apps/memory/configs/default.yaml`):
```yaml
  embedding:
    provider: voyage
    model: voyage-multimodal-3
    dimensions: 1024
```

**Notes / caveats for the Tester**
- **mongot vector-space risk (read first).** Dimensions are unchanged (1024 → 1024) so `assert_settings_match_live_vector_index` will **not** raise on boot. But any pre-existing `knowledge_graph` row whose `embedding` was written under `voyage-3` is now in the wrong semantic space; `$vectorSearch` will return wrong-but-superficially-plausible results with zero error signals. To validate the acceptance flow end-to-end, the cleanest path is the `RESET_ONTOLOGY=1` migration (drops `knowledge_graph`, re-extracts under voyage-multimodal-3). The CLAUDE.md `[!CAUTION]` block under the rebuild runbook documents this for operators.
- Integration tests intentionally NOT run per the orchestrator's instructions — Tester gate will exercise them with the full stack up.
- No production code paths besides the Voyage client family were touched; gemini / sentence-transformers / modal / mock branches are byte-identical.
- `voyage-3` and `voyage_embedding` still appear in tracker history files (`tracker/done/034-voyage-3-yaml-default.done.md`, etc.) and in two src-side `tracker/...` reference comments (`settings.py:45`, `indexing/core.py:472`) — left intentionally untouched; they are historical pointers, not live identifiers. CLAUDE.md still references `voyage-3` in the rebuild-runbook examples to explain the upgrade path; that is intended.

### [Tester] 2026-05-19 18:15 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`prettier`, `ruff check`, `ruff format`, `biome check (harness)`, `KGQuery discipline (memory)` all Passed).
- Unit tests: 1201 passed / 0 failed in 40.26s (matches SWE's reported number).
- Integration tests (`make memory-integration-tests-all`): 211 passed / 1 failed / 1 skipped in 593.49s. Single failure is `tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_empty_query_returns_empty_list` with `httpx.ReadTimeout` against the Bright Data SERP API — **known pre-existing flake** flagged in the prior run, unrelated to the Voyage-client consolidation (no Voyage path touched).
- Warnings: 0.

**E2E adversarial pass**
- **A. Live API call (happy path).** `cd apps/memory && set -a && source ../../.env && set +a && uv run python -c "..."` with the new YAML default. Output: `Model class: VoyageMultimodalEmbeddingModel`, `num vecs: 1 dim: 1024`. PASS — confirms `get_embedding_model()` returns the multimodal client and a real network call to `/v1/multimodalembeddings` against `voyage-multimodal-3` returns the expected 1024-d vector. This is the load-bearing regression that proves the YAML flip + routing-branch removal hold together.
- **B. No-references contract.** `grep -rn "VoyageEmbeddingModel\b" apps/memory/` → no hits; `grep -rn "from tree.models.voyage_embedding " apps/memory/` → no hits; `find apps/memory -name voyage_embedding.py -o -name test_voyage_embedding.py` → no hits. Both files are staged for deletion in `git status`. PASS.
- **C. Fail-fast contract on voyage-3.** Patched `app_config.models.embedding.model = "voyage-3"`, instantiated via `get_embedding_model()`, called `embed(["hello world"])`. Got `ExtractionError: Voyage multimodal API error 400: Model voyage-3 is not supported. Supported models are ['voyage-multimodal-3', 'voyage-multimodal-3-l4', 'voyage-multimodal-3-5', 'voyage-multimodal-3.5'].` — operator-actionable: names the bad model AND enumerates the valid alternatives. PASS.
- **D. 429 retry coverage.** Read `TestVoyageMultimodalRateLimitRetry` at `apps/memory/tests/unit/models/test_voyage_multimodal_embedding.py:196-348`. All four contracts present: (a) `test_embed_retries_on_429_then_succeeds` asserts `mock_sleep.await_count == 2` AND the result returned correctly after two 429s, with backoff values 0.1 and 0.2 verified via `mock_sleep.await_args_list`; (b) `test_embed_fails_fast_on_non_429_4xx` (401) and `test_embed_fails_fast_on_non_429_5xx` (500) both assert `mock_sleep.await_count == 0` — fail-fast confirmed on both 4xx and 5xx; (c) `test_embed_raises_when_429_backoff_exhausted` matches the literal anchor `"rate-limit retries exhausted"`; (d) `asyncio.sleep` is patched in every test via `mocker.patch("tree.models.voyage_multimodal_embedding.asyncio.sleep", new_callable=AsyncMock)` so the suite stays sub-second. PASS.
- **E. Vector-space-change warning quality.** CLAUDE.md `[!CAUTION]` block at lines 414-432 explains: (a) silent-corruption mechanism — "dimension is identical, so the `assert_settings_match_live_vector_index` boot check **will NOT catch this** — but the two models produce embeddings in different semantic spaces"; (b) failure mode — "returns wrong-but-superficially-plausible results"; (c) remediation — "drop the live `vector_index`, run the indexing pipeline to recreate it... then **re-trigger extraction**" and "the cleanest path is the Phase-2-5 `RESET_ONTOLOGY=1` migration"; (d) placement — sits **inside** the existing rebuild runbook (between the runbook intro and the `[!WARNING]` block about mongot convergence), so any operator reading the runbook hits the silent-corruption caveat before running the drop. PASS.
- **F. Dim-mismatch anchor preservation.** `grep -n "Embedding dimension mismatch" apps/memory/src/tree/memory/indexing/core.py` → line 503 still raises with that exact literal; `grep -n "Embedding dimension mismatch" CLAUDE.md` → three hits (lines 398, 401, 411) intact. The runbook's grep-from-error-text-alone discoverability is preserved. PASS.
- **G. app_config + .env.example consistency.** `EmbeddingConfig.model` default in `apps/memory/src/tree/config/app_config.py:48` is now `Field(default="voyage-multimodal-3")`; `apps/memory/tests/unit/config/test_app_config.py:33` asserts the same default. `.env.example` reviewed end-to-end — credentials-only (Mongo, Google, Voyage, Modal, Bright Data) plus the existing optional `APP_CONFIG_PATH` override; no new behavior knob env vars added. PASS.
- **H. macOS TMPDIR shim.** `make memory-print-tmpdir` → `TMPDIR=/var/folders/77/qsf7lbgs6mv7q4tn90mqgpl00000gn/T/` (49 bytes, well under the 104-byte `sun_path` ceiling). PASS.

**Acceptance criteria**
- [x] PASS — 429 retry loop folded into multimodal client — `voyage_multimodal_embedding.py:38-51` (schedule constant), `82-105` (constructor param), `162-215` (retry loop with exhaustion → `ExtractionError`).
- [x] PASS — Multimodal payload shape preserved — `voyage_multimodal_embedding.py:145` builds `inputs = [{"content": [{"type": "text", "text": t}]}]`; verified by `test_embed_sends_correct_payload`.
- [x] PASS — YAML default flipped — `apps/memory/configs/default.yaml:63-66` (`provider: voyage`, `model: voyage-multimodal-3`, `dimensions: 1024`); verified by Break Path A live call.
- [x] PASS — Text client + its test deleted — `git status` shows `deleted: apps/memory/src/tree/models/voyage_embedding.py` and `deleted: apps/memory/tests/unit/models/test_voyage_embedding.py` staged for commit; `find` confirms neither file exists on disk.
- [x] PASS — `get_model.py` routing branch removed — `apps/memory/src/tree/models/get_model.py:54-69` now unconditionally returns `VoyageMultimodalEmbeddingModel` for `provider == "voyage"`; the explanatory comment names #038 and documents the voyage-3 → HTTP 400 expected behavior.
- [x] PASS — 429 test coverage — see Break Path D evidence.
- [x] PASS — `test_get_model.py` collapsed — single `test_returns_voyage_multimodal_embedding` (line 81-99) replaces the prior two text-vs-multimodal subtests; `VoyageEmbeddingModel` import removed.
- [x] PASS — CLAUDE.md rebuild runbook references new default AND surfaces vector-space-change warning — see Break Path E evidence.
- [x] PASS — Format / lint / pre-commit / unit-tests clean — outputs above.
- [x] PASS — No `VoyageEmbeddingModel\b` or live `from tree.models.voyage_embedding ` references — see Break Path B evidence. The one remaining mention is a docstring inside `test_voyage_multimodal_embedding.py:199` describing the test-coverage migration history; not a live identifier.

**Evidence**

Pre-commit:
```
$ make pre-commit
prettier..............Passed
ruff check............Passed
ruff format...........Passed
biome check (harness).Passed
KGQuery discipline (memory).Passed
```

Unit tests:
```
$ make memory-unit-tests
...
tests/unit/models/test_voyage_multimodal_embedding.py ...............    [ 98%]
============================ 1201 passed in 40.26s =============================
```

Integration tests (acceptance gate):
```
$ make memory-integration-tests-all
...
FAILED tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_empty_query_returns_empty_list  (httpx.ReadTimeout — pre-existing Bright Data SERP flake, unrelated to Voyage)
============= 1 failed, 211 passed, 1 skipped in 593.49s (0:09:53) =============
```

Live API smoke (Break Path A):
```
$ cd apps/memory && set -a && source ../../.env && set +a && uv run python -c "..."
Model class: VoyageMultimodalEmbeddingModel
num vecs: 1 dim: 1024
```

Fail-fast contract on voyage-3 (Break Path C):
```
Model class: VoyageMultimodalEmbeddingModel model: voyage-3
Got ExtractionError: Voyage multimodal API error 400: Model voyage-3 is not supported. Supported models are ['voyage-multimodal-3', 'voyage-multimodal-3-l4', 'voyage-multimodal-3-5', 'voyage-multimodal-3.5'].
```

**Other issues found**
- The known pre-existing flake `tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_empty_query_returns_empty_list` (HTTP `ReadTimeout` against Bright Data SERP) reproduces on this branch. Not a regression — same test, same external-network instability flagged previously. No Voyage path touched by this PR could have caused it. Recommend a separate task to mark that test `@pytest.mark.slow` plus add a httpx-level retry, or stub the live API behind a recorded fixture. Out of scope for #038.
- Minor docs nit (not blocking): the CLAUDE.md `[!CAUTION]` block speaks specifically to the voyage-3 → voyage-multimodal-3 upgrade case. If the project ever adopts a third voyage variant (e.g., voyage-3.5 text), the warning will read as historical rather than general. Consider on the next vector-model change parameterizing the wording. Pure polish; not a Blocker.
- The `test_get_model.py::test_returns_voyage_multimodal_embedding` patches `app_config.models.embedding.dimensions = 1024`, but the assertion only checks `isinstance(...)`. The dim patch is therefore inert — happy to leave as defensive context, but it's currently dead test setup. Nit.

**VERDICT: PASS**
