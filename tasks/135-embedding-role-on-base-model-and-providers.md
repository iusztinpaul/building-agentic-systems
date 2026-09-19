---
id: 135-embedding-role-on-base-model-and-providers
status: pending
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

- [ ] `inspect.signature(BaseEmbeddingModel.embed)` has `input_type` with default `None`; every concrete subclass in `src/` has the same parameter name and default — `tests/unit/models/test_dimensions.py::TestEmbedSignature::test_every_provider_accepts_input_type` (parametrised over the 8 classes).
- [ ] Voyage text: `embed(["q"], input_type="query")` POSTs `"input_type": "query"`; `"document"` → `"document"`; `None` → key absent; `VoyageTextEmbeddingModel(api_key="k", input_type="query")` raises `TypeError` — `tests/unit/models/test_voyage_embedding.py::TestInputType`.
- [ ] Voyage multimodal: same three payload cases and the same `TypeError` — `tests/unit/models/test_voyage_multimodal_embedding.py::TestInputType`.
- [ ] Gemini: `query` → `task_type == "RETRIEVAL_QUERY"`, `document` → `"RETRIEVAL_DOCUMENT"`, `None` → no `task_type` key, `output_dimensionality` present in all three — `tests/unit/models/test_gemini.py::TestTaskType`.
- [ ] sentence-transformers: with `model.prompts == {"query": "Q: "}`, `input_type="query"` calls `encode(..., prompt_name="query")`; `input_type="document"` (not defined) and `None` call `encode` WITHOUT `prompt_name` — `tests/unit/models/test_sentence_transformer.py::TestPromptName`.
- [ ] Modal, Fake, Mock and `_CachedSingleEmbedding` return identical output for `input_type` in `{None, "query", "document"}` (Mock compared on shape) — `tests/unit/models/test_fake_model.py::TestInputTypeIgnored`, `tests/unit/models/test_modal_embedding.py::TestInputTypeAccepted`, `tests/unit/memory/test_pipeline.py::TestCachedSingleEmbedding::test_ignores_input_type`.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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
