---
id: 135-embedding-role-on-base-model-and-providers
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# Embedding role on the model layer: `embed(texts, input_type=None)` on `BaseEmbeddingModel` and every provider

Tags: `models`
Depends on: None
Blocks: #136, #140
Implements: ADR-009 — Decision 5 (Embedding role, per-provider mapping)

## Scope

Add the **Embedding role** to the model contract. Models layer ONLY — no caller passes a role yet,
so persisted vectors do not change in this task.

- `src/tree/models/base.py`: `EmbeddingRole = Literal["query", "document"]` and
  `async def embed(self, texts: list[str], input_type: EmbeddingRole | None = None) -> list[list[float]]`.
  Docstring states the rule: `None` = symmetric (no provider-side prompt), and a provider that
  cannot honour a role IGNORES it — it never raises.
- `voyage_embedding.py` + `voyage_multimodal_embedding.py`: REMOVE the constructor-level
  `input_type` (grep: used only by their own tests) and send the per-call value as the API
  `input_type` field; `None` → key absent from the payload (as today). Cost/usage recording,
  the 429 loop, `status_code` contract: unchanged.
- `gemini.py`: `query` → `config["task_type"] = "RETRIEVAL_QUERY"`, `document` →
  `"RETRIEVAL_DOCUMENT"`, `None` → key absent. SWE must verify with the `tech-docs` skill that the
  configured Gemini embedding model accepts `task_type` in `EmbedContentConfig` on the pinned
  `google-genai`; if the model rejects it, ignore the role and say so in the docstring.
- `sentence_transformer.py`: pass `prompt_name=input_type` to `encode(...)` ONLY when
  `input_type in (self._model.prompts or {})`; otherwise call exactly as today.
  (`SentenceTransformer.prompts` is the model's `{name: prompt}` dict.)
- `modal_embedding.py`: accept `input_type` and ignore it for now, with a one-line comment that
  #140 maps it to the **Embedding catalog** prompts (OpenAI-compatible `/v1/embeddings` has no such
  parameter).
- `fake_model.py` (`FakeEmbeddingModel`, `MockEmbeddingModel`) and
  `pipeline.py::_CachedSingleEmbedding`: accept and ignore.
- Every `BaseEmbeddingModel` subclass under `tests/` (17 today: `_SpyEmbeddingModel`,
  `_RecordingEmbeddingModel`, `_ScriptedEmbeddingModel`, `_FakeEmbedding`, … — find with
  `grep -rn "BaseEmbeddingModel)" apps/memory/tests`) gains the parameter so #136 can pass it.

Write tests with `/squid-testing-python`; all network mocked.

## Out of scope
- Passing a role from any caller; the batching layer (#136). Modal prompts (#140).
- A per-provider prompt registry: each provider maps the two literals inline.

## Acceptance Criteria

- [x] `inspect.signature(BaseEmbeddingModel.embed)` has `input_type` with default `None`; every concrete subclass in `src/` has the same parameter name and default — `tests/unit/models/test_dimensions.py::TestEmbedSignature::test_every_provider_accepts_input_type` (parametrised over the 8 classes).
- [x] Voyage text: `embed(["q"], input_type="query")` POSTs `"input_type": "query"`; `"document"` → `"document"`; `None` → key absent; `VoyageTextEmbeddingModel(api_key="k", input_type="query")` raises `TypeError` — `tests/unit/models/test_voyage_embedding.py::TestInputType`.
- [x] Voyage multimodal: same three payload cases and the same `TypeError` — `tests/unit/models/test_voyage_multimodal_embedding.py::TestInputType`.
- [x] Gemini: `query` → `task_type == "RETRIEVAL_QUERY"`, `document` → `"RETRIEVAL_DOCUMENT"`, `None` → no `task_type` key, `output_dimensionality` present in all three — `tests/unit/models/test_gemini.py::TestTaskType`.
- [x] sentence-transformers: with `model.prompts == {"query": "Q: "}`, `input_type="query"` calls `encode(..., prompt_name="query")`; `input_type="document"` (not defined) and `None` call `encode` WITHOUT `prompt_name` — `tests/unit/models/test_sentence_transformer.py::TestPromptName`.
- [x] Modal, Fake, Mock and `_CachedSingleEmbedding` return identical output for `input_type` in `{None, "query", "document"}` (Mock compared on shape) — `tests/unit/models/test_fake_model.py::TestInputTypeIgnored`, `tests/unit/models/test_modal_embedding.py::TestInputTypeAccepted`, `tests/unit/memory/test_pipeline.py::TestCachedSingleEmbedding::test_ignores_input_type`.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Developer embeds a question with Voyage
1. `model = VoyageTextEmbeddingModel(api_key=...)`; `await model.embed(["how does sharding work?"], input_type="query")`.
2. The request body is `{"model": "voyage-4", "input": [...], "truncation": true, "input_type": "query"}`.
3. One 1024-float vector comes back.

### Story: Developer swaps to a local sentence-transformers model without prompts
1. YAML: `provider: sentence-transformers`, `model: all-MiniLM-L6-v2` (defines no prompts).
2. `embed(["text"], input_type="document")` encodes exactly as before — no `prompt_name`, no error.

### Story: Existing caller keeps working untouched
1. `await model.embed(["Ada Lovelace"])` — no `input_type`.
2. Every provider sends today's request byte-for-byte (no role key / prompt).

---

Blocked by: (none)

## Log

### [PA] 2026-09-19 17:43 — Grooming

**Summary**
One optional `input_type` argument on the embedding contract, mapped natively per provider and ignored where a provider has no notion of it.

**Key decisions**
- Per-call over constructor-level: one model instance serves both the indexing and the query path, so the role cannot live on the instance. The Voyage constructor param is deleted, not deprecated (no caller uses it).
- "Cannot honour → ignore, never raise": a role is a retrieval hint, not a correctness input.
- Modal ignores the role here; its prompts need the catalog (#138/#140).

**Dependencies**
- None.

**User stories**
- 3 stories: Voyage query role, prompt-less local model, untouched legacy caller.

Ready for implementation.

### [SWE] 2026-09-19 19:06 — Implementation

**Files modified**
- `apps/memory/src/tree/models/base.py` — `EmbeddingRole` literal + `embed(texts, input_type=None)` on the contract, docstring states "`None` = symmetric" and "cannot honour → ignore, never raise".
- `apps/memory/src/tree/models/voyage_embedding.py` — constructor `input_type` DELETED; the per-call role becomes the API `input_type` field.
- `apps/memory/src/tree/models/voyage_multimodal_embedding.py` — same, on the multimodal payload.
- `apps/memory/src/tree/models/gemini.py` — `_ROLE_TO_TASK_TYPE` → `config["task_type"]`; `None` omits the key.
- `apps/memory/src/tree/models/sentence_transformer.py` — `prompt_name=input_type` only when the model defines a NON-EMPTY prompt of that name.
- `apps/memory/src/tree/models/modal_embedding.py` — accepts + ignores the role (#140 maps it to the Embedding catalog prompts).
- `apps/memory/src/tree/models/fake_model.py` — `FakeEmbeddingModel` / `MockEmbeddingModel` accept + ignore.
- `apps/memory/src/tree/memory/pipeline.py` — `_CachedSingleEmbedding.embed` accepts + ignores.
- `apps/memory/tests/unit/models/test_dimensions.py` — `TestEmbedSignature` (base + 8 concrete classes).
- `apps/memory/tests/unit/models/test_{voyage_embedding,voyage_multimodal_embedding,gemini,sentence_transformer,fake_model,modal_embedding}.py` — per-provider role tests; the 4 tests that passed `input_type=` to a Voyage constructor now pass it to `embed`.
- `apps/memory/tests/unit/memory/**` (7 files) — the 16 `BaseEmbeddingModel` test doubles gain the parameter so #136 can pass it.

**Tests**
- Unit: 3178 passing, 0 failing (`make memory-tests`; 3177 before the last Gemini simplification added a parametrisation).
- Integration: N/A — this repo has no integration suite (AGENTS.md); e2e is the live run below.
- Red-first: the 42 new/changed tests were written first and failed on assertions, not import errors.

**Acceptance criteria**
- [x] `inspect.signature` — `tests/unit/models/test_dimensions.py::TestEmbedSignature` (base + 8 classes: 7 in `tree/models/` + `_CachedSingleEmbedding`).
- [x] Voyage text 3 payload cases + constructor `TypeError` — `test_voyage_embedding.py::TestInputType`.
- [x] Voyage multimodal, same — `test_voyage_multimodal_embedding.py::TestInputType`.
- [x] Gemini `RETRIEVAL_QUERY` / `RETRIEVAL_DOCUMENT` / no key, `output_dimensionality` in all three — `test_gemini.py::TestTaskType`.
- [x] sentence-transformers `prompt_name` — `test_sentence_transformer.py::TestPromptName`.
- [x] Modal / Fake / Mock / `_CachedSingleEmbedding` identical output per role — `test_modal_embedding.py::TestInputTypeAccepted`, `test_fake_model.py::TestInputTypeIgnored`, `test_pipeline.py::TestCachedSingleEmbedding::test_ignores_input_type`.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

**Doc verification (required by the Scope)**

No context7 MCP tool was connected in this session, so the `tech-docs` fallbacks were used — the pinned packages' own source (strongest available primary source) plus the doc URLs the skill lists.

1. **Gemini `task_type`** — https://ai.google.dev/gemini-api/docs/embeddings (`.md.txt`, read 2026-09-19): supported task types for `gemini-embedding-001` include `RETRIEVAL_QUERY` ("use for queries") and `RETRIEVAL_DOCUMENT` ("for documents to be retrieved"); the page also says *"You cannot use the `task_type` field for the `gemini-embedding-2` model."* Pinned `google-genai` 1.65.0 declares `EmbedContentConfig.task_type: Optional[str]` (free-form, `types.py:7409`) — the SDK cannot validate an id for us.
2. **Live negative control (2026-09-19)** — the doc's "cannot use" is NOT an error: `gemini-embedding-2` ACCEPTS `task_type` and ignores it (`cos(no-role, RETRIEVAL_QUERY) = cos(no-role, RETRIEVAL_DOCUMENT) = 1.000000`). `models.list()` also shows `gemini-embedding-2-preview` is live. So the role is sent for EVERY model id: the server already implements ADR-009 §5's "cannot honour → ignore, never raise", and a client-side allow-list of supporting ids would buy nothing while silently dropping the role on the next id Google ships. Recorded in the `embed` docstring and in `TestTaskType`'s docstring so it is not "fixed" back into a guard.
3. **sentence-transformers `prompts` / `prompt_name`** — pinned 5.3.0 source: `encode(prompt_name=X)` raises `ValueError` when `X` is absent from `self.prompts`, and an empty prompt string is a no-op (`if prompt is not None and len(prompt) > 0`). Crucially `__init__` seeds `self.prompts = {"query": "", "document": ""}` on EVERY model, so the Scope's membership test (`input_type in model.prompts`) is a tautology and would pass `prompt_name="document"` for `all-MiniLM-L6-v2` — contradicting user story 2. Implemented as a NON-EMPTY check instead ("only if the model defines that prompt", glossary); both AC cases still hold, and an extra test pins the seeded-empty case.

**Evidence**

```
$ make memory-tests
============================ 3178 passed in 51.36s =============================

$ make memory-format-check && make memory-lint-check && make pre-commit
293 files already formatted
All checks passed!
prettier/ruff check/ruff format/biome check (harness) ................ Passed
```

End-to-end, real APIs, local env (`make env-status` → local), one model instance serving both roles:

```
$ uv run python scratchpad/e2e_roles.py        # VoyageTextEmbeddingModel, voyage-4
query    dims=1024 first3=[-0.0035, 0.0386, 0.02]
document dims=1024 first3=[-0.0196, 0.0028, 0.0003]
no role  dims=1024 first3=[-0.001, 0.0297, 0.0187]
cos(query, document)       = 0.6043
cos(query, same-text-none) = 0.9737   (<1.0 => the role reached the API)

$ uv run python scratchpad/e2e_gemini.py       # GeminiEmbeddingModel
gemini-embedding-001 query/document/none dims=1024/1024/1024
  cos(query, none)     = 1.0000    (Gemini's own default task_type IS RETRIEVAL_QUERY)
  cos(query, document) = 0.8730    (the document role clearly moves the vector)
gemini-embedding-2 with input_type='query' -> OK, dims=1024 (ignored server-side)
```

**Notes**
- Two deliberate deviations from the Scope's letter, both evidence-backed above and both keeping every stated acceptance criterion: (a) sentence-transformers gates on a non-empty prompt rather than dict membership; (b) Gemini sends `task_type` for every model id rather than probing whether "the configured model accepts it" — because no model rejects it.
- No caller passes a role yet, so no persisted vector changes in this task — #136 wires the callers, #140 the Modal prompts. User story 3 ("existing caller untouched") is pinned per provider by a `test_default_call_omits_the_key`-style test.
- `_CachedSingleEmbedding` ignoring the role is correct by construction: it replays a vector task ④ already computed under its own role, and never reaches a provider.
- NOT RUN — the sentence-transformers and Modal providers were not exercised live (weights download / a deployed Modal app; both are ignored-or-unchanged paths here and #140/#141 own the live Modal proof).

### [Tester] 2026-09-19 21:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check`: "293 files already formatted" / "All checks passed!"; `make pre-commit`: prettier/ruff check/ruff format/biome all Passed)
- Unit tests: 3178 passed / 0 failed (`make memory-tests`). Baseline before this diff (`git stash`): 3134 passed — 44 tests added net, consistent with the claimed 42 new/changed tests plus extra parametrisation.
- Integration tests: N/A — no integration suite in this repo (AGENTS.md); e2e verified live below.
- Warnings: 1 pre-existing, unrelated `UserWarning` from `opik/rest_api/core/pydantic_utilities.py` ("Core Pydantic V1 functionality isn't compatible with Python 3.14") — reproduced identically on the pre-diff baseline (`git stash` + `make memory-tests`), so it is not a regression introduced by this task.
- `typecheck`: root `Makefile:78` documents `typecheck` as harness (TS)-only — "memory is dynamically typed" — so there is no Python type-check gate to run here; not a skipped step, it doesn't exist for this app.
- `code-review` plugin: enabled in `.claude/settings.json`, but its command (`.claude/plugins/.../code-review/commands/code-review.md`) is scoped to `gh pr diff`/`gh pr view` against an existing PR. No PR exists yet for this uncommitted worktree change, so it cannot run at this stage; it applies later at `/squid-review` on the pushed PR. Noted, not skipped.

**E2E adversarial pass**
- Happy path (live Voyage, `voyage-4`, real API key, local env): `VoyageTextEmbeddingModel(api_key=...).embed(["how does sharding work?"], input_type="query")` vs `.embed([...])` (no role) → `cos(query, no-role) = 0.9736603590159216`, matching the SWE's reported 0.9737 to 4 decimals (PASS).
- Happy path (live Gemini `gemini-embedding-001`): `cos(query, document) = 0.8871` — the role measurably moves the vector, role mapping is real, not a no-op (PASS).
- Break path 1 (negative control, live network — deviation #2's evidence): `GeminiEmbeddingModel(model="gemini-embedding-2").embed(["hello world"], input_type="query"/"document")` vs no-role → `cos(none, query) = 1.0`, `cos(none, document) = 1.0`. Confirms `gemini-embedding-2` ACCEPTS and IGNORES `task_type` server-side rather than rejecting it, independently reproduced (not just trusting the SWE's report) (PASS).
- Break path 2 (invalid/hostile role string): `GeminiEmbeddingModel.embed(["hi"], input_type="passage")` → raises a bare `KeyError: 'passage'` from `_ROLE_TO_TASK_TYPE[input_type]` in `apps/memory/src/tree/models/gemini.py`, which sits ABOVE the `try/except` that wraps the API call, so it is not converted to `ExtractionError`. Compare: Voyage forwards an unrecognised `input_type` straight into the payload (verified via a mocked session — `payload = {'model': 'voyage-4', ..., 'input_type': 'passage'}`) and would fail closed as an `ExtractionError` with `status_code` once the real API 400s (`voyage_embedding.py:287-293`); sentence-transformers silently ignores an unknown role (`prompts.get(input_type)` → falsy) and Modal/Fake/Mock/`_CachedSingleEmbedding` ignore the value outright. Gemini alone leaks a raw non-project exception on this path. NOT a FAIL: `"passage"` is outside the declared domain of `EmbeddingRole = Literal["query", "document"]`, no AC exercises an invalid role, and the only future caller (#136) selects from the two literals — this project relies on static typing (mypy/IDE), not runtime `Literal` enforcement, consistent with CLAUDE.md's typed-signature convention rather than defensive runtime validation. Recorded as a note for the SWE (see below), not a blocker.
- Break path 3 (stray constructor usage / missed subclasses — regression risk for #136): `grep -rn "input_type=" --include="*.py" apps/memory` → every hit is either a docstring or a call to `.embed(..., input_type=...)`; zero remaining `VoyageTextEmbeddingModel(...input_type=...)` / `VoyageMultimodalEmbeddingModel(...input_type=...)` constructor calls anywhere in `src/` or `tests/`. `grep -rn "input_type" --include="*.yaml" --include="*.yml" --include="*.toml" --include="*.example"` → no hits; read `apps/memory/src/tree/models/get_model.py` — Voyage/Gemini/sentence-transformers/Modal are all built with explicit named kwargs (`api_key=`, `model=`, `output_dimension=`), never a `**cfg`/`model_dump()` splat, so no YAML key could ever reach a deleted constructor param (PASS — no regression risk).
- Also checked: positional role call `await FakeEmbeddingModel().embed(["a"], "query")` works (role is the 2nd positional param, matches base signature) (PASS); empty `texts=[]` with a role short-circuits before any payload/API work on Voyage and returns `[]` on Fake (PASS); the Voyage 429-retry loop builds `payload` once before entering the `while True:` loop, so a retried request re-sends the identical `input_type` (verified by reading `voyage_embedding.py:229-238` — payload construction is above the retry loop) (PASS); `_CachedSingleEmbedding.embed` returns the seeded vector for `input_type` in `{None, "query", "document"}` (verified via `tests/unit/memory/test_pipeline.py::TestCachedSingleEmbedding::test_ignores_input_type`, and read `pipeline.py:1664-1671`) (PASS).

**Acceptance criteria**
- [x] PASS — `inspect.signature(BaseEmbeddingModel.embed)` has `input_type` default `None`; every concrete subclass in `src/` matches — `tests/unit/models/test_dimensions.py::TestEmbedSignature::test_base_declares_input_type_defaulting_to_none` + `::test_every_provider_accepts_input_type` (parametrised over `VoyageTextEmbeddingModel`, `VoyageMultimodalEmbeddingModel`, `GeminiEmbeddingModel`, `SentenceTransformerEmbeddingModel`, `ModalEmbeddingModel`, `FakeEmbeddingModel`, `MockEmbeddingModel`, `_CachedSingleEmbedding` — 8 classes); all 24 pass in `make memory-tests`.
- [x] PASS — Voyage text 3 payload cases + constructor `TypeError` — `tests/unit/models/test_voyage_embedding.py::TestInputType` (`test_role_is_sent_as_the_api_input_type[query/document]`, `test_no_role_omits_the_key`, `test_default_call_omits_the_key`, `test_constructor_rejects_input_type`); constructor deletion confirmed by reading `voyage_embedding.py:162-179` (no `input_type` param).
- [x] PASS — Voyage multimodal, same three cases + `TypeError` — `tests/unit/models/test_voyage_multimodal_embedding.py::TestInputType`, same 4 tests, same shape as text.
- [x] PASS — Gemini `RETRIEVAL_QUERY`/`RETRIEVAL_DOCUMENT`/no key, `output_dimensionality` present in all three — `tests/unit/models/test_gemini.py::TestTaskType` (parametrised over both `gemini-embedding-001` and `gemini-embedding-2`); live-verified independently (see E2E above).
- [x] PASS — sentence-transformers: `model.prompts == {"query": "Q: "}` → `input_type="query"` passes `prompt_name="query"`; `"document"` (undefined) and `None` send no `prompt_name` — `tests/unit/models/test_sentence_transformer.py::TestPromptName::test_defined_role_is_passed_as_prompt_name` / `test_undefined_role_sends_no_prompt_name` / `test_no_role_sends_no_prompt_name`, exactly the AC's literal case. Deviation (non-empty-prompt gate instead of dict-membership) verified against the installed package: `sentence_transformers/SentenceTransformer.py:189` seeds `self.prompts = {"query": "", "document": ""}` on every model construction — confirmed by reading the installed 5.3.0 source directly (not just trusting the SWE's claim). ACCEPTED: a membership-only gate would pass `prompt_name="document"` for a model that defines no such prompt (contradicting User Story 2), even though it wouldn't raise (line 1042 succeeds for `""`, line 1057's `len(prompt) > 0` check makes it a no-op) — the real breach is `prompt_name` being present in the `encode()` call at all, which the AC's "encode exactly as before" requires it not to be. An extra test (`test_empty_seeded_prompts_send_no_prompt_name`) pins the seeded-empty case.
- [x] PASS — Modal, Fake, Mock, `_CachedSingleEmbedding` identical output across `{None, "query", "document"}` — `tests/unit/models/test_fake_model.py::TestInputTypeIgnored`, `tests/unit/models/test_modal_embedding.py::TestInputTypeAccepted::test_role_does_not_change_the_request_or_the_result` (asserts the mocked HTTP call itself is unchanged, not just the output), `tests/unit/memory/test_pipeline.py::TestCachedSingleEmbedding::test_ignores_input_type`.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green — reproduced independently, see Test summary.

**Deviation verdicts**
1. sentence-transformers non-empty-prompt gate: ACCEPT. Verified against installed 5.3.0 source (`self.prompts = {"query": "", "document": ""}` seeded in `__init__`); AC's explicit `{"query": "Q: "}` case still holds exactly as written.
2. Gemini sends `task_type` unconditionally (no allow-list): ACCEPT. Independently reproduced the negative control live (`gemini-embedding-2` accepts + ignores, cosine 1.0 across roles). Failure-mode check: if a FUTURE Gemini model id ever REJECTS `task_type` outright, the call fails CLOSED — `embed_content` raises inside the `try/except` in `gemini.py`, so any SDK-level rejection becomes `ExtractionError(f"Gemini embedding call failed: {exc}")`, loud and caught, not silent corruption. The alternative (a client-side allow-list) has a strictly worse failure mode: silently dropping the role on a new/unlisted model id with no error at all, producing mismatched query/document vectors. Risk is documented in both the `embed` docstring and `TestTaskType`'s class docstring ("Do NOT 'fix' this back into a guard"). Acceptable for this task.

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-tests
============================ 3178 passed in 47.04s =============================

$ make memory-format-check && make memory-lint-check
293 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format...............................................................Passed
biome check (harness)....................................................Passed

# live, independently reproduced (not copy-pasted from the SWE report):
$ uv run python live_voyage_check.py
cos(query, none) = 0.9736603590159216

$ uv run python live_gemini_check.py   # gemini-embedding-2
gemini-embedding-2 ACCEPTED task_type
cos(none, query) = 1.0
cos(none, document) = 1.0

$ uv run python live_gemini_check2.py  # gemini-embedding-001
cos(query, document) = 0.8871471155287846

# adversarial: invalid role on Gemini
$ uv run python adv_test.py
RAISED: <class 'KeyError'> 'passage'
```

**Other issues found**
- `apps/memory/src/tree/models/gemini.py`: `_ROLE_TO_TASK_TYPE[input_type]` (dict-index lookup) sits above the `try/except ExtractionError` wrapper in `embed()`, so an `input_type` value outside `{"query", "document", None}` raises a bare `KeyError` instead of the project's `ExtractionError`. Every other provider on this surface either ignores an unrecognised role (sentence-transformers, Modal, Fake, Mock, `_CachedSingleEmbedding`) or fails closed with a well-formed `ExtractionError` (Voyage, via the real API's 400). Not a blocker for this task (out of the declared `EmbeddingRole` domain, no AC covers it, only literal-selecting callers exist), but worth a follow-up: `_ROLE_TO_TASK_TYPE.get(input_type)` + `if task_type: config["task_type"] = task_type` (or move the lookup inside the `try`) would make Gemini consistent with its siblings before #136 wires a real caller.
- No other issues found. Code smell check on the touched paths (payload/config construction, retry loop, prompt gating) found nothing else worth flagging.

**VERDICT: PASS**
