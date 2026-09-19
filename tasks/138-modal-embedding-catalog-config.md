---
id: 138-modal-embedding-catalog-config
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Embedding catalog: `modal.embedding_models` in YAML with a per-model Serving path (`endpoint` | `sglang` | `vllm`), Pydantic validation, and the lookup / name / server-arg / deploy-spec helpers

Tags: `config`, `models`, `modal`
Depends on: None
Blocks: #139, #140, #142
Implements: ADR-009 — Decision 2 (three Serving paths, Dedicated endpoint first) and Decision 3 (YAML catalog, one app per model)

## Scope

Pure config + pure functions. No Modal import, no network, no behaviour change for any provider.

**1. YAML** — new top-level `modal:` section in `apps/memory/configs/default.yaml`:

    modal:
      autoinference_utils_version: "0.2.6"   # fallback scripts only
      engines:                               # fallback scripts only
        vllm: { version: "0.17.1" }          # SWE must verify the newest vLLM that serves voyage-4-nano
        sglang: { version: "0.5.20" }
      embedding_models:
        # Serving path ladder (ADR-009 §2): endpoint first -> sglang -> vllm.
        - repo_id: Qwen/Qwen3-Embedding-0.6B
          revision: 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3
          serving: endpoint                  # Modal Dedicated Endpoint — zero code of ours
          base_model: Qwen/Qwen3-Embedding-0.6B   # == repo_id -> no custom weights
          native_dimensions: 1024
          matryoshka_dimensions: []          # SWE must verify on the model card (MRL 32-1024) AND that the served engine honours `dimensions`; leave [] unless both hold
          query_prompt: "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
          document_prompt: ""
        - repo_id: voyageai/voyage-4-nano
          revision: main                     # SWE: pin the commit sha from the HF repo
          serving: vllm                      # known-good. tasks/141 first tries SERVING=endpoint (custom weights over base_model) and flips this ONLY on recorded evidence
          base_model: Qwen/Qwen3-Embedding-0.6B   # closest base model in Modal's endpoint catalog
          gpu: A10                           # SWE must verify the Modal 1.5.x GPU string (A10 vs A10G)
          cpu: 4
          memory_mb: 16384
          native_dimensions: 1024
          matryoshka_dimensions: [256, 512, 1024, 2048]
          max_model_len: 32768
          query_prompt: "Represent the query for retrieving supporting documents: "
          document_prompt: "Represent the document for retrieval: "
          extra_server_args:                 # model-specific extras for the FALLBACK scripts; a Dedicated endpoint never sees them
            "--convert": embed
            "--pooler-config": '{"pooling_type":"MEAN"}'
            "--hf-overrides": '{"architectures":["VoyageQwen3BidirectionalEmbedModel"]}'
            "--dtype": bfloat16
            "--enforce-eager": ""
            "--trust-remote-code": ""

  SWE must verify the Qwen3 `query_prompt` BYTES against the HF model card (whether `Query:` is
  followed by a space) and record the card's line in `## Log`.

**2. Pydantic** — `src/tree/config/app_config.py`: `ServingPath = Literal["endpoint", "sglang", "vllm"]`,
`ModalEngineConfig(version: str)`, `ModalEmbeddingModelConfig` and `ModalConfig`, wired as
`AppConfig.modal: ModalConfig = ModalConfig()` (code default = empty catalog; the YAML seeds it).
All three `extra="forbid"`. `ModalEmbeddingModelConfig` fields:
- Every Serving path: `repo_id: str` (must match `^[\w.-]+/[\w.-]+$`), `revision: str = "main"`,
  `serving: ServingPath = "endpoint"`, `native_dimensions: int = Field(gt=0)`,
  `matryoshka_dimensions: list[int] = []`, `query_prompt: str = ""`, `document_prompt: str = ""`.
- Dedicated endpoint: `base_model: str | None = None` (same regex as `repo_id`) — the model id from
  Modal's endpoint catalog passed as `--model`. REQUIRED when `serving == "endpoint"`; allowed on any
  entry so an operator can try `SERVING=endpoint` without editing YAML.
- Fallback scripts only, all OPTIONAL so a minimal `endpoint` entry stays 6 lines and
  `SERVING=sglang|vllm` still works on it: `gpu: str = "A10"`, `cpu: float = Field(4, gt=0)`,
  `memory_mb: int = Field(16384, gt=0)`, `max_model_len: int | None = Field(None, gt=0)`,
  `extra_server_args: dict[str, str] = {}`.
- Computed `endpoint_name: str` = the part of `repo_id` after `/`, lower-cased, every run of
  characters outside `[a-z0-9]` -> `-`, trimmed of `-`; computed `app_name: str` = `"ep-" + endpoint_name`.
  `voyageai/voyage-4-nano` -> `voyage-4-nano` / `ep-voyage-4-nano`;
  `Qwen/Qwen3-Embedding-0.6B` -> `qwen3-embedding-0-6b` / `ep-qwen3-embedding-0-6b`.
  (ASSUMPTION H1, proven in #141: `modal endpoint create --name N` yields the Modal app `ep-N` —
  the app name of the `serve.py` Modal generated for this very model. Keep the derivation in this
  ONE place so #141 can correct it with a one-line change.)
- Validators: `serving == "endpoint"` without `base_model` -> error
  `serving: endpoint requires base_model (a model id from Modal's endpoint catalog, e.g. Qwen/Qwen3-Embedding-0.6B)`;
  every `extra_server_args` key starts with `--`; NO value contains whitespace (`autoinference-utils`
  splits values on whitespace — the message says "use compact JSON"); keys `--model`, `--model-path`,
  `--host`, `--port`, `--revision`, `--served-model-name`, `--max-model-len`, `--context-length`,
  `--runner`, `--is-embedding` are rejected (the builder owns them); `ModalConfig` rejects duplicate
  `repo_id` and duplicate `app_name`.

**3. Helpers** — new `src/tree/models/modal_catalog.py` (imports `app_config` only; NO `modal`):
- `EMBEDDING_SERVER_NAME = "Server"` — the server class name on ALL three Serving paths: it is the
  class name in Modal's generated `serve.py`, and the fallback scripts (#142) reuse it so one URL
  lookup serves every path.
- `get_catalog_entry(model: str) -> ModalEmbeddingModelConfig` — exact `repo_id` match; unknown ->
  `ModelError("Unknown Modal embedding model 'x'. Embedding catalog ids: Qwen/Qwen3-Embedding-0.6B, voyageai/voyage-4-nano. Add an entry under modal.embedding_models in configs/default.yaml.")`
  (ids sorted).
- `resolve_serving(entry, override: str | None) -> ServingPath` — `None` / `""` -> `entry.serving`;
  a value outside the three -> `ModelError("Unknown Serving path 'tgi'. Use one of: endpoint, sglang, vllm.")`;
  `"endpoint"` on an entry without `base_model` ->
  `ModelError("voyageai/x has no base_model — a Dedicated endpoint needs one. Add base_model to its Embedding catalog entry.")`.
- `build_server_args(entry, engine: Literal["sglang", "vllm"]) -> dict[str, str]` —
  `entry.extra_server_args` plus the builder-owned keys: vllm -> `--runner` = `pooling`, `--revision`,
  `--served-model-name` = `repo_id`, and `--max-model-len` only when `max_model_len` is set;
  sglang -> `--is-embedding` = `""`, `--revision`, `--served-model-name` = `repo_id`, and
  `--context-length` only when `max_model_len` is set.
- `EmbeddingDeploySpec` (Pydantic): `repo_id, revision, engine, app_name, server_name, gpu, cpu,
  memory_mb, native_dimensions, engine_version, autoinference_utils_version, server_args`;
  `build_deploy_spec(model: str, engine: Literal["sglang", "vllm"]) -> EmbeddingDeploySpec`
  (the `engine` comes from the SCRIPT that calls it, not from `entry.serving`, so an override works).
  `DEPLOY_SPEC_ENV = "EMBEDDING_DEPLOY_SPEC"` names the env var #142 bakes `spec.model_dump_json()` into.
- `prompt_for(entry, input_type) -> str` — `query_prompt` / `document_prompt` / `""` for `None`.

README "`default.yaml` sections" gains one `modal` bullet naming the three Serving paths.
Write tests with `/squid-testing-python`.

## Out of scope
- Any `import modal`, the CLI argv / driver / Make targets (#139), the fallback scripts (#142), the client (#140).
- An `HF_TOKEN` / token catalog field: the Hugging Face token is ONE optional workspace credential (`Settings.hf_token`, #139), never YAML — ADR-009 §9.
- Forbidding the script-only fields on `endpoint` entries: `SERVING=sglang|vllm` needs them (defaults
  or explicit); #139's driver LOGS that a Dedicated endpoint does not use them.
- A `PoolingStrategy`/engine plugin interface; per-model engine versions; env-var overrides for
  list entries (the `TREE_<SECTION>__<KEY>` hatch does not address list items — documented, not built).

## Acceptance Criteria

- [ ] `app_config.modal.embedding_models` holds exactly 2 entries: `Qwen/Qwen3-Embedding-0.6B` (`serving == "endpoint"`, `base_model == "Qwen/Qwen3-Embedding-0.6B"`, revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, `gpu == "A10"`, `cpu == 4`, `memory_mb == 16384`, `max_model_len is None` from defaults) and `voyageai/voyage-4-nano` (`serving == "vllm"`, `base_model == "Qwen/Qwen3-Embedding-0.6B"`, `max_model_len == 32768`); both `native_dimensions == 1024` — `tests/unit/config/test_app_config.py::TestModalCatalog::test_seed_entries`.
- [ ] An entry with only `repo_id`, `base_model`, `native_dimensions` validates with `serving == "endpoint"` — `::test_serving_defaults_to_endpoint`.
- [ ] `endpoint_name` / `app_name` are `voyage-4-nano` / `ep-voyage-4-nano` and `qwen3-embedding-0-6b` / `ep-qwen3-embedding-0-6b`; `Org/My_Model..v2` -> `my-model-v2` / `ep-my-model-v2` — `::test_name_derivation`.
- [ ] Validation errors — `::test_rejects_invalid_entries` (parametrised, 11 cases): `serving: tgi`; an unknown field; `repo_id: "no-slash"`; `serving: endpoint` with no `base_model` (message contains `base_model`); `base_model: "no-slash"`; an `extra_server_args` key `"runner"`; a value `'{"pooling_type": "MEAN"}'` (contains a space; message contains `compact JSON`); a reserved key `--port`; a builder-owned key `--is-embedding`; two entries with the same `repo_id`; two entries deriving the same `app_name`.
- [ ] `AppConfig()` with no `modal:` section validates with an empty catalog — `::test_modal_section_is_optional`; `tests/unit/config/fixtures/frozen_config.yaml` gains the `modal:` block and the frozen-config test stays green.
- [ ] `get_catalog_entry("voyageai/voyage-4-nano").serving == "vllm"`; `get_catalog_entry("BAAI/bge-m3")` raises `ModelError` whose message contains both catalog ids in sorted order and `modal.embedding_models` — `tests/unit/models/test_modal_catalog.py::TestLookup`.
- [ ] `resolve_serving(qwen_entry, None) == "endpoint"`; `(qwen_entry, "") == "endpoint"`; `(qwen_entry, "sglang") == "sglang"`; `(voyage_entry, "endpoint") == "endpoint"`; `(qwen_entry, "tgi")` raises `ModelError` containing `endpoint, sglang, vllm`; `"endpoint"` on an entry built without `base_model` (serving `vllm`) raises `ModelError` containing `base_model` — `::TestResolveServing`.
- [ ] `build_server_args(voyage_entry, "vllm")` equals the YAML extras plus `{"--runner": "pooling", "--revision": "main", "--served-model-name": "voyageai/voyage-4-nano", "--max-model-len": "32768"}`; `build_server_args(qwen_entry, "sglang") == {"--is-embedding": "", "--revision": "97b0…65b3", "--served-model-name": "Qwen/Qwen3-Embedding-0.6B"}` (no `--context-length`); `build_server_args(qwen_entry, "vllm") == {"--runner": "pooling", "--revision": "97b0…65b3", "--served-model-name": "Qwen/Qwen3-Embedding-0.6B"}` — `::TestServerArgs`.
- [ ] `EmbeddingDeploySpec.model_validate_json(build_deploy_spec(m, e).model_dump_json())` round-trips for (`voyageai/voyage-4-nano`, `vllm`) and (`Qwen/Qwen3-Embedding-0.6B`, `sglang`), with `engine_version` `"0.17.1"` / `"0.5.20"` (or the verified vLLM pin), `engine == e` and `server_name == "Server"` — `::TestDeploySpec::test_json_round_trip`.
- [ ] `prompt_for(voyage_entry, "query")` is `"Represent the query for retrieving supporting documents: "`, `"document"` -> `"Represent the document for retrieval: "`, `None` -> `""`; `prompt_for(qwen_entry, "document") == ""` — `::TestPrompts`.
- [ ] `python -c "import sys, tree.models.modal_catalog; assert 'modal' not in sys.modules"` exits 0 — asserted in `::test_catalog_does_not_import_modal` (subprocess).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator adds BGE-M3 as a Dedicated endpoint
1. Appends `{repo_id: BAAI/bge-m3, revision: <sha>, base_model: BAAI/bge-m3, native_dimensions: 1024}` to `modal.embedding_models` (no `serving:` line — `endpoint` is the default).
2. `uv --directory apps/memory run python -c "from tree.models.modal_catalog import get_catalog_entry as g; e = g('BAAI/bge-m3'); print(e.serving, e.endpoint_name, e.app_name)"` -> `endpoint bge-m3 ep-bge-m3`.
3. No Python file changed.

### Story: Operator forgets the base model
1. Adds `{repo_id: acme/my-embedder, native_dimensions: 768}`.
2. Any `make memory-*` command fails at config load: `modal.embedding_models.2: serving: endpoint requires base_model (a model id from Modal's endpoint catalog, e.g. Qwen/Qwen3-Embedding-0.6B)`.

### Story: Operator pastes pretty-printed JSON into a server arg
1. Writes `"--pooler-config": '{"pooling_type": "MEAN"}'`.
2. Config load fails: `modal.embedding_models.1.extra_server_args: value for --pooler-config contains whitespace — use compact JSON, e.g. {"pooling_type":"MEAN"}`.

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

### [PA] 2026-09-19 18:23 — Re-grooming (plan edit: Dedicated endpoints first)

**What changed and why**
- The human added a third, preferred way to serve a model: a Modal Dedicated Endpoint with optional custom weights; our scripts become fallbacks. `engine: vllm | sglang` is replaced by `serving: endpoint | sglang | vllm` (default `endpoint`) plus `base_model`.
- The field is `serving`, not `deployment`: the glossary already owns **Deployment** (a registered Prefect flow). New glossary terms **Serving path** and **Dedicated endpoint**.
- Script-only fields (`gpu`, `cpu`, `memory_mb`, `max_model_len`, `extra_server_args`) became OPTIONAL with defaults instead of required: a minimal endpoint entry needs none of them, yet `SERVING=sglang|vllm` on that same entry (the fallback ladder, and #141's SGLang proof on Qwen3) must still work — so they cannot be forbidden on `endpoint` entries. What IS validated: `base_model` required for `endpoint`, at load time and at override time.
- `--runner pooling` (vLLM) and `--is-embedding` (SGLang) moved from YAML into `build_server_args` and became reserved keys: every embedding model needs them on that engine, and owning them in the builder is what lets one entry run under either script.
- `build_server_args` / `build_deploy_spec` take the engine as an argument (the calling script's), not from the entry. `deploy_script_for` is dropped — the mapping lives in #139's `modal_cli_command`.
- `EMBEDDING_SERVER_NAME` is `"Server"` (Modal's generated class name) and `endpoint_name` is a new computed field (`app_name = "ep-" + endpoint_name`) so all three paths resolve through one lookup (H1, proven in #141).
- Seeds: Qwen3 -> `endpoint`; voyage-4-nano stays `vllm` with a `base_model` so #141 can attempt the endpoint path first. `HF_TOKEN` stays out (public seeds).

**User stories**
- 5 stories: endpoint entry with defaults, missing base model, malformed JSON arg, unknown id, app-name collision.

Ready for implementation.

### [PA] 2026-09-19 18:44 — Re-grooming (HF token)

**What changed and why**
- Scope, AC and stories are UNCHANGED. One out-of-scope sentence was rewritten: the human brought the optional `HF_TOKEN` into the feature, and it is a per-workspace credential in `Settings` (#139), not a catalog field — so this task still adds no token field to `ModalEmbeddingModelConfig`. The "`HF_TOKEN` stays out" line in the entry above is superseded by ADR-009 §9.

Ready for implementation.
