---
id: 138-modal-embedding-catalog-config
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Embedding catalog: `modal.embedding_models` in YAML, Pydantic validation, and the lookup / app-name / server-arg / deploy-spec helpers

Tags: `config`, `models`, `modal`
Depends on: None
Blocks: #139, #140
Implements: ADR-009 — Decision 2 (two engines, per-model `engine`) and Decision 3 (YAML catalog, one app per model)

## Scope

Pure config + pure functions. No Modal import, no network, no behaviour change for any provider.

**1. YAML** — new top-level `modal:` section in `apps/memory/configs/default.yaml`:

    modal:
      autoinference_utils_version: "0.2.6"
      engines:
        vllm: { version: "0.17.1" }     # SWE must verify the newest vLLM that serves voyage-4-nano
        sglang: { version: "0.5.20" }
      embedding_models:
        - repo_id: voyageai/voyage-4-nano
          revision: main               # SWE: pin the commit sha from the HF repo
          engine: vllm                 # SGLang 0.5.20 cannot serve it (sgl-project/sglang#18436 unmerged)
          gpu: A10                     # SWE must verify the Modal 1.5.x GPU string (A10 vs A10G)
          cpu: 4
          memory_mb: 16384
          native_dimensions: 1024
          matryoshka_dimensions: [256, 512, 1024, 2048]
          max_model_len: 32768
          query_prompt: "Represent the query for retrieving supporting documents: "
          document_prompt: "Represent the document for retrieval: "
          extra_server_args:
            "--runner": pooling
            "--convert": embed
            "--pooler-config": '{"pooling_type":"MEAN"}'
            "--hf-overrides": '{"architectures":["VoyageQwen3BidirectionalEmbedModel"]}'
            "--dtype": bfloat16
            "--enforce-eager": ""
            "--trust-remote-code": ""
        - repo_id: Qwen/Qwen3-Embedding-0.6B
          revision: 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3
          engine: sglang
          gpu: A10
          cpu: 4
          memory_mb: 16384
          native_dimensions: 1024
          matryoshka_dimensions: []      # SWE must verify on the model card (MRL 32-1024) AND that SGLang honours `dimensions`; leave [] unless both hold
          max_model_len: 32768
          query_prompt: "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
          document_prompt: ""
          extra_server_args:
            "--is-embedding": ""

  SWE must verify the Qwen3 `query_prompt` BYTES against the HF model card (whether `Query:` is
  followed by a space) and record the card's line in `## Log`.

**2. Pydantic** — `src/tree/config/app_config.py`: `ModalEngineConfig(version: str)`,
`ModalEmbeddingModelConfig` and `ModalConfig`, wired as `AppConfig.modal: ModalConfig = ModalConfig()`
(code default = empty catalog; the YAML seeds it). All three `extra="forbid"`.
`ModalEmbeddingModelConfig` fields: `repo_id: str` (must match `^[\w.-]+/[\w.-]+$`),
`revision: str = "main"`, `engine: Literal["vllm", "sglang"]`, `gpu: str`, `cpu: float = Field(gt=0)`,
`memory_mb: int = Field(gt=0)`, `native_dimensions: int = Field(gt=0)`,
`matryoshka_dimensions: list[int] = []`, `max_model_len: int = Field(gt=0)`,
`extra_server_args: dict[str, str] = {}`, `query_prompt: str = ""`, `document_prompt: str = ""`,
and a computed `app_name: str`.
- `app_name` = `"ep-"` + the part of `repo_id` after `/`, lower-cased, every run of characters
  outside `[a-z0-9]` → `-`, trimmed of `-`. `voyageai/voyage-4-nano` → `ep-voyage-4-nano`;
  `Qwen/Qwen3-Embedding-0.6B` → `ep-qwen3-embedding-0-6b`.
- Validators: every `extra_server_args` key starts with `--`; NO value contains whitespace
  (`autoinference-utils` splits values on whitespace, so `{"pooling_type": "MEAN"}` with a space
  would become two argv tokens — the error message says "use compact JSON"); keys `--model`,
  `--model-path`, `--host`, `--port`, `--revision`, `--served-model-name`, `--max-model-len`,
  `--context-length` are rejected (the builder owns them); `ModalConfig` rejects duplicate
  `repo_id` and duplicate `app_name`.

**3. Helpers** — new `src/tree/models/modal_catalog.py` (imports `app_config` only; NO `modal`):
- `EMBEDDING_SERVER_NAME = "EmbeddingServer"` — the `@app.server` class/server name both deploy
  scripts and the client use.
- `get_catalog_entry(model: str) -> ModalEmbeddingModelConfig` — exact `repo_id` match; unknown →
  `ModelError("Unknown Modal embedding model 'x'. Embedding catalog ids: Qwen/Qwen3-Embedding-0.6B, voyageai/voyage-4-nano. Add an entry under modal.embedding_models in configs/default.yaml.")`
  (ids sorted).
- `build_server_args(entry) -> dict[str, str]` — `entry.extra_server_args` plus, per engine:
  vllm → `--revision`, `--served-model-name` = `repo_id`, `--max-model-len`;
  sglang → `--revision`, `--served-model-name` = `repo_id`, `--context-length`.
- `EmbeddingDeploySpec` (Pydantic): `repo_id, revision, engine, app_name, server_name, gpu, cpu,
  memory_mb, native_dimensions, engine_version, autoinference_utils_version, server_args`;
  `build_deploy_spec(model: str) -> EmbeddingDeploySpec`. `DEPLOY_SPEC_ENV = "EMBEDDING_DEPLOY_SPEC"`
  names the env var #139 bakes `spec.model_dump_json()` into.
- `prompt_for(entry, input_type) -> str` — `query_prompt` / `document_prompt` / `""` for `None`.
- `deploy_script_for(entry) -> str` — `"deploy/modal_vllm_embedding.py"` | `"deploy/modal_sglang_embedding.py"`.

README "`default.yaml` sections" gains one `modal` bullet. Write tests with `/squid-testing-python`.

## Out of scope
- Any `import modal`, deploy script, Make target (#139) or client change (#140).
- A `PoolingStrategy`/engine plugin interface; per-model engine versions; env-var overrides for
  list entries (the `TREE_<SECTION>__<KEY>` hatch does not address list items — documented, not built).

## Acceptance Criteria

- [ ] `app_config.modal.embedding_models` holds exactly 2 entries with `repo_id`s `voyageai/voyage-4-nano` (engine `vllm`) and `Qwen/Qwen3-Embedding-0.6B` (engine `sglang`, revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`), both `native_dimensions == 1024` — `tests/unit/config/test_app_config.py::TestModalCatalog::test_seed_entries`.
- [ ] `app_name` is `ep-voyage-4-nano` and `ep-qwen3-embedding-0-6b`; `Org/My_Model..v2` → `ep-my-model-v2` — `::test_app_name_derivation`.
- [ ] Validation errors: `engine: tgi`; an unknown field; `repo_id: "no-slash"`; an `extra_server_args` key `"runner"`; a value `'{"pooling_type": "MEAN"}'` (contains a space; message contains `compact JSON`); a reserved key `--port`; two entries with the same `repo_id`; two entries deriving the same `app_name` — `::test_rejects_invalid_entries` (parametrised, 8 cases).
- [ ] `AppConfig()` with no `modal:` section validates with an empty catalog — `::test_modal_section_is_optional`; `tests/unit/config/fixtures/frozen_config.yaml` gains the `modal:` block and the frozen-config test stays green.
- [ ] `get_catalog_entry("voyageai/voyage-4-nano").engine == "vllm"`; `get_catalog_entry("BAAI/bge-m3")` raises `ModelError` whose message contains both catalog ids in sorted order and `modal.embedding_models` — `tests/unit/models/test_modal_catalog.py::TestLookup`.
- [ ] `build_server_args` for voyage-4-nano equals the YAML extras plus `{"--revision": "main", "--served-model-name": "voyageai/voyage-4-nano", "--max-model-len": "32768"}`; for Qwen3 it equals `{"--is-embedding": "", "--revision": "97b0…65b3", "--served-model-name": "Qwen/Qwen3-Embedding-0.6B", "--context-length": "32768"}` — `::TestServerArgs`.
- [ ] `EmbeddingDeploySpec.model_validate_json(build_deploy_spec(m).model_dump_json())` round-trips for both models, with `engine_version` `"0.17.1"` / `"0.5.20"` (or the verified vLLM pin) and `server_name == "EmbeddingServer"` — `::TestDeploySpec::test_json_round_trip`.
- [ ] `prompt_for(voyage_entry, "query")` is `"Represent the query for retrieving supporting documents: "`, `"document"` → `"Represent the document for retrieval: "`, `None` → `""`; `prompt_for(qwen_entry, "document") == ""` — `::TestPrompts`.
- [ ] `deploy_script_for` returns the vllm / sglang script path by engine — `::TestDeployScript`.
- [ ] `python -c "import sys, tree.models.modal_catalog; assert 'modal' not in sys.modules"` exits 0 — asserted in `::test_catalog_does_not_import_modal` (subprocess).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator adds BGE-M3 to the catalog
1. Appends `{repo_id: BAAI/bge-m3, revision: <sha>, engine: sglang, gpu: A10, cpu: 4, memory_mb: 16384, native_dimensions: 1024, max_model_len: 8192, extra_server_args: {"--is-embedding": ""}}` to `modal.embedding_models`.
2. `uv --directory apps/memory run python -c "from tree.models.modal_catalog import get_catalog_entry as g; print(g('BAAI/bge-m3').app_name)"` → `ep-bge-m3`.
3. No Python file changed.

### Story: Operator pastes pretty-printed JSON into a server arg
1. Writes `"--pooler-config": '{"pooling_type": "MEAN"}'`.
2. Any `make memory-*` command fails at config load: `modal.embedding_models.0.extra_server_args: value for --pooler-config contains whitespace — use compact JSON, e.g. {"pooling_type":"MEAN"}`.

### Story: Operator mistypes a model id
1. Code asks for `get_catalog_entry("voyageai/voyage-4-nan")`.
2. `ModelError: Unknown Modal embedding model 'voyageai/voyage-4-nan'. Embedding catalog ids: Qwen/Qwen3-Embedding-0.6B, voyageai/voyage-4-nano. …`

### Story: Two orgs publish the same model name
1. Operator adds `acme/voyage-4-nano`.
2. Config load fails: both entries derive `ep-voyage-4-nano`; the message names both `repo_id`s.

---

Blocked by: (none)

## Log

### [PA] 2026-09-19 17:43 — Grooming

**Summary**
The Embedding catalog as validated YAML plus the pure helpers the deploy scripts and the client will share.

**Key decisions**
- `matryoshka_dimensions` added to the settled field list: the client's "fail if the model cannot truncate" rule needs the catalog to say what truncation a model supports.
- Whitespace-free server-arg values enforced at config load — verified in the `autoinference-utils` 0.2.6 source that values are `.split()` into argv tokens.
- Engine versions are per engine, not per model (least mechanism); promote to a per-entry override only when two models need different engine versions.
- Helpers import no `modal`, so the cloud MCP boot path and the unit suite never pay for it.

**Dependencies**
- None.

**User stories**
- 4 stories: add a model, malformed JSON arg, unknown id, app-name collision.

Ready for implementation.
