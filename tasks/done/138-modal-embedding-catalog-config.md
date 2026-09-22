---
id: 138-modal-embedding-catalog-config
status: done
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

- [x] `app_config.modal.embedding_models` holds exactly 2 entries: `Qwen/Qwen3-Embedding-0.6B` (`serving == "endpoint"`, `base_model == "Qwen/Qwen3-Embedding-0.6B"`, revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, `gpu == "A10"`, `cpu == 4`, `memory_mb == 16384`, `max_model_len is None` from defaults) and `voyageai/voyage-4-nano` (`serving == "vllm"`, `base_model == "Qwen/Qwen3-Embedding-0.6B"`, `max_model_len == 32768`); Qwen `native_dimensions == 1024`, voyage-4-nano `native_dimensions == 2048` — `tests/unit/config/test_app_config.py::TestModalCatalog::test_seed_entries`. (Literal corrected from "both `== 1024`" on the Tester's primary-source finding — the voyage head is a learned 1024→2048 projection; 1024 is a Matryoshka truncation.)
- [x] An entry with only `repo_id`, `base_model`, `native_dimensions` validates with `serving == "endpoint"` — `::test_serving_defaults_to_endpoint`.
- [x] `endpoint_name` / `app_name` are `voyage-4-nano` / `ep-voyage-4-nano` and `qwen3-embedding-0-6b` / `ep-qwen3-embedding-0-6b`; `Org/My_Model..v2` -> `my-model-v2` / `ep-my-model-v2` — `::test_name_derivation`.
- [x] Validation errors — `::test_rejects_invalid_entries` (parametrised, 12 cases): `serving: tgi`; an unknown field; `repo_id: "no-slash"`; `serving: endpoint` with no `base_model` (message contains `base_model`); `base_model: "no-slash"`; an `extra_server_args` key `"runner"`; a value `'{"pooling_type": "MEAN"}'` (contains a space; message contains `compact JSON`); a reserved key `--port`; a builder-owned key `--is-embedding`; two entries with the same `repo_id`; two entries deriving the same `app_name`.
- [x] `AppConfig()` with no `modal:` section validates with an empty catalog — `::test_modal_section_is_optional`; `tests/unit/config/fixtures/frozen_config.yaml` gains the `modal:` block and the frozen-config test stays green.
- [x] `get_catalog_entry("voyageai/voyage-4-nano").serving == "vllm"`; `get_catalog_entry("BAAI/bge-m3")` raises `ModelError` whose message contains both catalog ids in sorted order and `modal.embedding_models` — `tests/unit/models/test_modal_catalog.py::TestLookup`.
- [x] `resolve_serving(qwen_entry, None) == "endpoint"`; `(qwen_entry, "") == "endpoint"`; `(qwen_entry, "sglang") == "sglang"`; `(voyage_entry, "endpoint") == "endpoint"`; `(qwen_entry, "tgi")` raises `ModelError` containing `endpoint, sglang, vllm`; `"endpoint"` on an entry built without `base_model` (serving `vllm`) raises `ModelError` containing `base_model` — `::TestResolveServing`.
- [x] `build_server_args(voyage_entry, "vllm")` equals the YAML extras plus `{"--runner": "pooling", "--revision": "main", "--served-model-name": "voyageai/voyage-4-nano", "--max-model-len": "32768"}`; `build_server_args(qwen_entry, "sglang") == {"--is-embedding": "", "--revision": "97b0…65b3", "--served-model-name": "Qwen/Qwen3-Embedding-0.6B"}` (no `--context-length`); `build_server_args(qwen_entry, "vllm") == {"--runner": "pooling", "--revision": "97b0…65b3", "--served-model-name": "Qwen/Qwen3-Embedding-0.6B"}` — `::TestServerArgs`.
- [x] `EmbeddingDeploySpec.model_validate_json(build_deploy_spec(m, e).model_dump_json())` round-trips for (`voyageai/voyage-4-nano`, `vllm`) and (`Qwen/Qwen3-Embedding-0.6B`, `sglang`), with `engine_version` `"0.17.1"` / `"0.5.20"` (or the verified vLLM pin), `engine == e` and `server_name == "Server"` — `::TestDeploySpec::test_json_round_trip`.
- [x] `prompt_for(voyage_entry, "query")` is `"Represent the query for retrieving supporting documents: "`, `"document"` -> `"Represent the document for retrieval: "`, `None` -> `""`; `prompt_for(qwen_entry, "document") == ""` — `::TestPrompts`.
- [x] `python -c "import sys, tree.models.modal_catalog; assert 'modal' not in sys.modules"` exits 0 — asserted in `::test_catalog_does_not_import_modal` (subprocess).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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

### [SWE] 2026-09-19 19:38 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — `ServingPath`, `ModalEngineConfig`, `ModalEmbeddingModelConfig`, `ModalConfig` (all `extra="forbid"`), wired as `AppConfig.modal` with an EMPTY code-default catalog.
- `apps/memory/src/tree/models/modal_catalog.py` — new: `EMBEDDING_SERVER_NAME`, `DEPLOY_SPEC_ENV`, `get_catalog_entry`, `resolve_serving`, `build_server_args`, `EmbeddingDeploySpec`, `build_deploy_spec`, `prompt_for`. Imports `app_config` + `tree.models.base`/`exceptions` only — no `modal`.
- `apps/memory/configs/default.yaml` — new top-level `modal:` block seeding the two catalog entries + the engine pins.
- `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — same block in the frozen fixture (shape, not seed values).
- `apps/memory/tests/unit/config/test_app_config.py` — `TestModalCatalog` (seeds, prompt bytes, engine pins, endpoint default, name derivation, 11 rejection cases, optional section, frozen fixture).
- `apps/memory/tests/unit/models/test_modal_catalog.py` — new: `TestLookup`, `TestResolveServing`, `TestServerArgs`, `TestDeploySpec`, `TestPrompts`, `test_catalog_does_not_import_modal` (subprocess).
- `apps/memory/README.md` — one `modal` bullet under "`default.yaml` sections" naming the three Serving paths.

**Tests**
- Unit: 3255 passing, 0 failing (`make memory-tests`; 23 new in `test_modal_catalog.py`, 19 new in `test_app_config.py::TestModalCatalog`). Suite run with `make env-status` = local.
- Integration: N/A — this repo has no integration suite (deleted deliberately); e2e is the story run below.

**Verified against primary sources (all read 2026-09-19)**
- `Qwen/Qwen3-Embedding-0.6B` revision — `GET https://huggingface.co/api/models/Qwen/Qwen3-Embedding-0.6B` → `"sha":"97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"`. Matches the groomed value.
- Qwen3 `query_prompt` BYTES — model card (`/raw/main/README.md`) line 129: `return f'Instruct: {task_description}\nQuery:{query}'` → **no space after `Query:`**, so the YAML ends `…answer the query\nQuery:`. (The card's TEI `curl` example at line 227 DOES have a space; the canonical Python/ST helper does not, and that is what the vectors were trained through.) Double-quoted in YAML so `\n` is a real newline — asserted byte-for-byte in `test_qwen_query_prompt_is_byte_exact`.
- Qwen3 Matryoshka — card line 36 "supports user-defined output dimensions ranging from 32 to 1024" and the model table (line 44) `MRL Support: Yes`. But nothing offline proves the recipe **Modal** picks for a Dedicated endpoint honours an OpenAI `dimensions` argument, and the task's rule is "leave `[]` unless BOTH hold" → shipped as `[]`. **Flagged for #141** (prove live, then fill).
- `voyageai/voyage-4-nano` revision — HF API `"sha":"67fabc9bef010dabc5f6024aa1b1b6b93410426f"`. Pinned per the Scope's "SWE: pin the commit sha from the HF repo".
- voyage-4-nano prompts — `config_sentence_transformers.json`: `"query": "Represent the query for retrieving supporting documents: "`, `"document": "Represent the document for retrieval: "` (trailing space in both). Byte-identical to the groomed values.
- voyage-4-nano `native_dimensions: 1024` — `config.json` `"hidden_size": 1024` and `modules.json` = Transformer → Pooling → Normalize with **no Dense projection**, so the served vector is 1024-d. The card's prose ("The default embedding dimension is 2048", line 141) and its MRL list (2048/1024/512/256, line 45) are kept in `matryoshka_dimensions` as groomed, but 2048 looks unachievable from a 1024-d hidden state. **Flagged for #141/#140:** measure the actual wire length before anyone asks for 2048.
- Modal GPU string — `https://modal.com/docs/guide/gpu.md` "Specifying GPU type" lists `T4, L4, A10, L40S, A100, A100-40GB, A100-80GB, RTX-PRO-6000, H100/H100!, H200, B200/B200+, B300`. **`A10` is correct; there is no `A10G`** in Modal's current list. (The legacy `deploy/modal_vllm_embedding.py` still says `gpu="A10G"` — that file is #142's, untouched here.)
- Engine pins — vLLM held at `0.17.1`, the version the repo's existing script already serves this model with. Evidence for/against a bump: the voyage-4-nano card's own vLLM example says `pip install vllm==0.16.0`; PyPI latest is `0.29.0` (unverified for `VoyageQwen3BidirectionalEmbedModel`). "Newest that serves it" is only provable live → **flagged for #141**. SGLang `0.5.20` = PyPI latest, matching the groomed value.

**Acceptance criteria**
- [x] 2 seed entries with the pinned fields — `tests/unit/config/test_app_config.py::TestModalCatalog::test_seed_entries` (+ `::test_engine_versions_and_utils_pin`, `::test_qwen_query_prompt_is_byte_exact`).
- [x] Minimal entry defaults to `serving: endpoint` — `::test_serving_defaults_to_endpoint`.
- [x] `endpoint_name` / `app_name` for both seeds and `Org/My_Model..v2` — `::test_name_derivation` (one `re.sub(r"[^a-z0-9]+", "-", …).strip("-")`, so the derivation is one line to correct if H1 is wrong).
- [x] 11 parametrised validation errors — `::test_rejects_invalid_entries`.
- [x] `AppConfig()` / a YAML with no `modal:` gives an empty catalog; frozen fixture carries the block — `::test_modal_section_is_optional`, `::test_frozen_config_carries_the_catalog`, whole config suite green.
- [x] Lookup + unknown-id message — `tests/unit/models/test_modal_catalog.py::TestLookup`.
- [x] `resolve_serving` for all six cases — `::TestResolveServing`.
- [x] `build_server_args` for both engines — `::TestServerArgs` (see Notes: voyage's `--revision` is the pinned sha, not `main`).
- [x] `EmbeddingDeploySpec` JSON round-trip, `engine_version` `0.17.1` / `0.5.20`, `server_name == "Server"` — `::TestDeploySpec::test_json_round_trip`.
- [x] `prompt_for` — `::TestPrompts`.
- [x] Importing the catalog does not import `modal` — `::test_catalog_does_not_import_modal` (subprocess) and run by hand, exit 0.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

**Evidence**

```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
1 file reformatted, 296 files left unchanged
All checks passed!
297 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests
tests/unit/models/test_modal_catalog.py .......................          [ 89%]
============================ 3255 passed in 45.44s =============================
```

End-to-end, the five User Stories run against the real loader (`APP_CONFIG_PATH=<scratch copy of default.yaml>`):

```
=== Story 1: add BGE-M3 as a Dedicated endpoint (no serving: line) ===
$ uv --directory apps/memory run python -c "from tree.models.modal_catalog import get_catalog_entry as g; e = g('BAAI/bge-m3'); print(e.serving, e.endpoint_name, e.app_name)"
endpoint bge-m3 ep-bge-m3

=== Story 2: operator forgets base_model ===
modal.embedding_models.2
  Value error, serving: endpoint requires base_model (a model id from Modal's endpoint catalog, e.g. Qwen/Qwen3-Embedding-0.6B)

=== Story 3: pretty-printed JSON in a server arg ===
modal.embedding_models.1.extra_server_args
  Value error, value for --pooler-config contains whitespace — use compact JSON, e.g. {"pooling_type":"MEAN"}

=== Story 4: mistyped model id ===
ModelError: Unknown Modal embedding model 'voyageai/voyage-4-nan'. Embedding catalog ids: Qwen/Qwen3-Embedding-0.6B, voyageai/voyage-4-nano. Add an entry under modal.embedding_models in configs/default.yaml.

=== Story 5: two orgs publish the same model name ===
modal
  Value error, 'voyageai/voyage-4-nano' and 'acme/voyage-4-nano' both derive the Modal app name 'ep-voyage-4-nano' in modal.embedding_models — one app per model, so rename one of them.
```

Shipped catalog through the helpers:

```
qwen  : endpoint ep-qwen3-embedding-0-6b SERVING=sglang -> sglang
voyage: vllm ep-voyage-4-nano SERVING=endpoint -> endpoint
qwen query_prompt bytes: 'Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:'
qwen prompt_for(document): ''
voyage prompt_for(query): 'Represent the query for retrieving supporting documents: '
sglang args (qwen): {'--revision': '97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3', '--served-model-name': 'Qwen/Qwen3-Embedding-0.6B', '--is-embedding': ''}

$ build_deploy_spec("voyageai/voyage-4-nano", "vllm").model_dump_json()
{"repo_id": "voyageai/voyage-4-nano", "revision": "67fabc9b…426f", "engine": "vllm",
 "app_name": "ep-voyage-4-nano", "server_name": "Server", "gpu": "A10", "cpu": 4.0,
 "memory_mb": 16384, "native_dimensions": 1024, "engine_version": "0.17.1",
 "autoinference_utils_version": "0.2.6",
 "server_args": {"--convert": "embed", "--pooler-config": "{\"pooling_type\":\"MEAN\"}",
   "--hf-overrides": "{\"architectures\":[\"VoyageQwen3BidirectionalEmbedModel\"]}",
   "--dtype": "bfloat16", "--enforce-eager": "", "--trust-remote-code": "",
   "--revision": "67fabc9b…426f", "--served-model-name": "voyageai/voyage-4-nano",
   "--runner": "pooling", "--max-model-len": "32768"}}
```

**Notes**
- **One AC literal was superseded by the Scope, deliberately.** AC "build_server_args" writes `"--revision": "main"` for voyage-4-nano, but the Scope's YAML says `revision: main  # SWE: pin the commit sha from the HF repo`. The sha is now pinned (`67fabc9b…426f`), so the test asserts the sha instead of `main` (the same AC already writes the REAL sha for Qwen). The `main` default is still covered by `TestServerArgs::test_default_revision_is_main`, which builds an entry that pins nothing.
- `ModalConfig.engines` is `dict[Literal["sglang", "vllm"], ModalEngineConfig]` — keeps the model count at the three the task names while still rejecting an unknown engine key at load time.
- `endpoint_name` / `app_name` are plain `@property`, not `computed_field`: they must NOT be accepted (or required) as YAML input under `extra="forbid"`.
- `build_server_args` copies `extra_server_args` before adding the builder-owned keys — asserted by `::test_the_entry_is_not_mutated`, since the entry is a process-wide singleton read by both the driver and the client.
- Bare flags (`"--enforce-eager": ""`) survive both the whitespace validator and the builder — an empty value is a valid bare flag, not a falsy value to drop.
- NOT RUN — anything live: no Modal call, no deploy, no network at import time. The endpoint→`ep-N` naming (H1), whether a Dedicated endpoint honours `dimensions`, whether 2048-d output is real, and whether a vLLM newer than 0.17.1 serves voyage-4-nano all stay for #141.
- Untouched on purpose: `deploy/modal_vllm_embedding.py` (#142 owns it, still `gpu="A10G"` and its own app name), `tree/models/modal_embedding.py` (#140), `docs/adrs/009*`, `docs/glossary.md`, tasks 139-142.

### [SWE] 2026-09-19 20:05 — Mutation check on the 11 rejection cases

Eleven parametrised assertions on a `ValidationError` *message* can pass on
Pydantic's own `input_value={...}` dump instead of on our guard, so they were
tightened to distinctive fragments of OUR messages (`must start with '--'`,
`owned by the deploy builder`, `serving: endpoint requires base_model`,
`duplicate repo_id …`, `… both derive`, `Input should be 'endpoint', 'sglang'
or 'vllm'`, `should match pattern`, `Extra inputs are not permitted`) and then
mutation-checked: with every catalog guard neutered in `app_config.py`
(`ServingPath` → `str`, both `pattern=` removed, `extra="forbid"` → `"allow"`,
the three validator bodies short-circuited) the suite reports

```
11 failed, 3244 passed in 45.46s
```

— exactly the 11 cases fail and nothing else, so none of them was passing on
boilerplate. Source restored from a byte-copy and re-verified: `make
memory-format-check`, `make memory-lint-check` clean, `3255 passed`.

`test_a_near_miss_is_not_resolved_by_prefix` gained the same treatment (it now
asserts the unknown-id message, not just the exception type).

**Seam for #139/#140, not a defect:** `modal_catalog` binds the module-level
`app_config` singleton at import, matching how the rest of the codebase reads
config. So `APP_CONFIG_PATH` only takes effect before first import (which is
why the story runs above each used a fresh process), and a test wanting a
custom catalog cannot `monkeypatch.setattr` through that binding — it must pass
an entry in directly (as `TestResolveServing` and `TestServerArgs` do) or spawn
a process.

### [Tester] 2026-09-19 19:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all clean, 297 files formatted, ruff/prettier/biome all "Passed")
- Unit tests: 3255 passed / 0 failed (`make memory-tests`, `make env-status` → local)
- Integration tests: N/A — no integration suite in this repo (deleted deliberately per AGENTS.md); e2e verification done via the real loader instead (below)
- Warnings: 0

**E2E adversarial pass**
- Happy path: `uv run python -c "from tree.models.modal_catalog import get_catalog_entry as g; e = g('voyageai/voyage-4-nano'); print(e.serving, e.app_name)"` → `vllm ep-voyage-4-nano` (PASS)
- Break path 1 (malformed YAML: `serving: vllm` entry with none of gpu/cpu/memory/base_model set, loaded through the real `load_app_config` against a scratch YAML): defaults kick in (`gpu=A10, cpu=4, memory_mb=16384, base_model=None`); `resolve_serving(entry, "endpoint")` on it raises `ModelError: acme/foo has no base_model — a Dedicated endpoint needs one...` — matches spec exactly (PASS)
- Break path 2 (state edge: `build_server_args` called twice on the same singleton catalog entry, once per engine): `entry.extra_server_args` unchanged after both calls — confirmed no mutation of the process-wide singleton (PASS)
- Break path 3 (malformed input: `TREE_MODAL__EMBEDDING_MODELS='[{"repo_id":"x/y"}]'` env var — the documented-unsupported list-override escape hatch): fails LOUDLY at import time with a clear pydantic `ValidationError: Input should be a valid list, input_type=str` rather than silently corrupting the catalog (PASS — fails safely as documented)
- Break path 4 (boundary/hostile input: odd `repo_id` suffixes through `endpoint_name`/`app_name` derivation) — `Org/My_Model..v2` → `my-model-v2`/`ep-my-model-v2` (matches spec, PASS); `a/日本語model` → `model`/`ep-model` (unicode stripped, no crash, PASS); **found but not blocking:** `a/---` → `endpoint_name == ""`, `app_name == "ep-"` — a repo_id whose suffix is entirely symbols produces a degenerate empty app name with no validator catching it. Not in any AC, requires an absurd repo_id, not filed as a FAIL — see "Other issues found."
- Break path 5 (hostile input via reserved/builder-owned keys + whitespace values, run through the real loader against 5 separate scratch YAMLs): `--runner` in `extra_server_args` → rejected "owned by the deploy builder" (PASS); bare key `foo` (no `--`) → rejected "must start with '--'" (PASS); value `"a b"` → rejected "contains whitespace — use compact JSON" (PASS); case-differing repo_ids `Acme/Foo` / `acme/foo` → correctly NOT flagged as duplicate `repo_id` (case differs) but correctly flagged as duplicate `app_name` `ep-foo` (PASS); `OrgA/Foo-Bar` / `OrgB/foo_bar` → both correctly collide on `ep-foo-bar` (PASS)
- Break path 6 (mutation-adversarial pass on the guards themselves, run by Tester independently of the SWE's own mutation check): neutered `ServingPath = Literal[...]` → `str` and `_REPO_ID_PATTERN` → `r".*"` in a scratch copy, re-ran `TestModalCatalog` + `test_modal_catalog.py` → exactly the 3 rejection cases tied to those two guards failed (`unknown-serving-path`, `repo-id-without-org`, `base-model-without-org`), nothing else broke; restored the file byte-for-byte (`git diff --stat` unchanged, `make memory-tests` re-confirmed 3255 passed) (PASS — guards are load-bearing, not decorative)

**Deep-dive investigation: is `native_dimensions: 1024` correct for voyage-4-nano?**

The orchestrator's doubt is confirmed with primary-source evidence, not inference. Fetched directly from Hugging Face and vLLM's own GitHub source (no live model call, no network at import time — this was all done outside the test suite with `curl`/`urllib`, consistent with "pure config, no Modal import" scope):

- `https://huggingface.co/voyageai/voyage-4-nano/raw/main/config.json` → `"hidden_size": 1024`, **`"num_labels": 2048`**.
- `https://huggingface.co/voyageai/voyage-4-nano/raw/main/1_Pooling/config.json` → **`"word_embedding_dimension": 2048`** — the sentence-transformers Pooling module itself declares it pools 2048-wide vectors, not 1024.
- `https://huggingface.co/voyageai/voyage-4-nano/raw/main/modeling_qwen3_bidirectional.py` (the `auto_map.AutoModel` custom class HF loads, referenced from `config.json`) — the decisive artifact:
  ```python
  self.linear = nn.Linear(config.hidden_size, config.num_labels, bias=False)  # 1024 -> 2048
  ...
  outputs.last_hidden_state = self.linear(outputs.last_hidden_state)
  ```
  This is exactly the "learned 1024→2048 projection head living in the custom modeling code" the orchestrator predicted. It is NOT a sentence-transformers `Dense` module (hence invisible in `modules.json`, which only lists `Transformer → Pooling → Normalize`) — it lives inside the custom `AutoModel` forward pass, so `modules.json` was never going to show it. This is the SWE's exact blind spot.
- Safetensors header of `model.safetensors` (read via HTTP Range request, no full download): **`linear.weight: {"shape": [2048, 1024]}`** — confirms `nn.Linear(1024, 2048)` at the weight level, independent of the Python source.
- vLLM's own `vllm/model_executor/models/voyage.py` (`VoyageQwen3BidirectionalEmbedModel`, fetched from `github.com/vllm-project/vllm@main`) reimplements the identical head and returns `self.linear(out)` from `forward()` with **no truncation logic** — so a vLLM server for this model returns 2048-d vectors when NO `dimensions` parameter is sent. The catalog's own `extra_server_args["--hf-overrides"]` only overrides `architectures`, never `num_labels`, so the shipped config does not suppress this.
- SGLang's unmerged onboarding PR (`sgl-project/sglang#18436`) launches the model with `--json-model-override-args '{"architectures": ["VoyageQwen3BidirectionalEmbedModel"], "num_labels": 2048}'` — independent third-party confirmation of 2048.
- Comparison model Qwen/Qwen3-Embedding-0.6B: `config.json` has no `num_labels` field, `hidden_size: 1024`, `modules.json` is a plain `Transformer → Pooling → Normalize` with no custom `auto_map`/projection — its `native_dimensions: 1024` is correct as shipped. MRL 32-1024 confirmed on the model card (line 36 and the model table, line 44).

**Conclusion: `native_dimensions: 1024` for `voyageai/voyage-4-nano` is WRONG. The real native/default served width is 2048.** This is exactly the failure mode the task called out as a FAIL condition: "if the true native is 2048 and the catalog says 1024, the client would silently receive 2048-d vectors" against a 1024-d mongot index.

**Required fix (concrete, so the SWE does not need to re-investigate):**
1. `apps/memory/configs/default.yaml:228` — change `native_dimensions: 1024` → `native_dimensions: 2048` for the `voyageai/voyage-4-nano` entry. Its trailing comment `# config.json hidden_size, no Dense projection` is itself wrong and must be replaced, e.g. `# linear head num_labels: a learned 1024->2048 projection lives in modeling_qwen3_bidirectional.py's AutoModel class, invisible in modules.json`.
2. `apps/memory/tests/unit/config/test_app_config.py::TestModalCatalog::test_seed_entries` — `assert voyage.native_dimensions == 1024` → `== 2048`; the line `assert qwen.native_dimensions == 1024` stays; the comment `# Both land in the 1024-d mongot vector_index untruncated.` is now false for voyage and must be reworded (only Qwen lands there untruncated; voyage must be asked to truncate).
3. `apps/memory/tests/unit/models/test_modal_catalog.py:214` (`TestDeploySpec::test_carries_the_hardware_and_the_app_name`) — `assert spec.native_dimensions == 1024` → `== 2048`.
4. `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — the voyage entry's `native_dimensions: 1024` (line 147) → `2048`, for consistency even though no current test asserts the fixture's seed values (only shape/repo_ids).
5. `matryoshka_dimensions: [256, 512, 1024, 2048]` needs **no change** — it already lists 2048 as the top (native) rung, so it was already internally inconsistent with `native_dimensions: 1024` before this fix (nothing enforces `native_dimensions` be a member of `matryoshka_dimensions`, but the seed data itself was self-contradictory — 2048 appearing in the truncation ladder of a model declared 1024-native).
6. AC bullet 1 in this task file (unticked above) asserts "`both native_dimensions == 1024`" — that literal must become "`Qwen native_dimensions == 1024`, `voyage native_dimensions == 2048`" (PA's/SWE's lane to reword, not Tester's).

**What #140/#141 must do as a result:** with the catalog corrected to `native_dimensions: 2048`, #140's stated rule ("send `dimensions=` only when it differs from `native_dimensions`") automatically makes the client send `dimensions=1024` against voyage-4-nano — which is exactly what the 1024-d mongot index needs. That is the entire point of the fix: today, with the catalog wrong, the client would compute "no override needed" and silently receive 2048-d vectors. **Open question left for #141, not resolved by this review (docs/source alone cannot settle it):** whether vLLM's `/v1/embeddings` for this model actually honours a `dimensions` parameter and performs a correct MRL truncation + renormalization server-side, or whether the client must truncate+renormalize itself client-side after receiving the full 2048-d vector. `#141 must measure the wire length with and without `dimensions` before #140 ships anything that assumes server-side truncation works.`

**Sha deviation verdict: ACCEPT.** Both pinned shas verified directly against the HF API:
- `curl https://huggingface.co/api/models/voyageai/voyage-4-nano` → `"sha": "67fabc9bef010dabc5f6024aa1b1b6b93410426f"` — matches the catalog and `_VOYAGE_SHA` in tests exactly.
- `curl https://huggingface.co/api/models/Qwen/Qwen3-Embedding-0.6B` → `"sha": "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"` — matches.
The deviation (asserting the pinned sha instead of the AC's literal `"main"`) is legitimate: the Scope explicitly instructs "SWE: pin the commit sha from the HF repo", the sha is correct, and the `main`-default behavior is still covered separately by `TestServerArgs::test_default_revision_is_main`. Accepted as implemented.

**Qwen3 `query_prompt` byte-exactness: CONFIRMED.** Model card's canonical helper (`README.md`, Transformers Usage section) is `f'Instruct: {task_description}\nQuery:{query}'` — no space after `Query:`. Byte-compared programmatically: `e.query_prompt == f'Instruct: {task}' + chr(10) + 'Query:'` → `True`. The catalog's YAML value is byte-identical.

**Acceptance criteria**
- [ ] FAIL — 2 seed entries with the pinned fields, both `native_dimensions == 1024`
      Expected: per AC literal, both entries `native_dimensions == 1024`
      Actual: `voyageai/voyage-4-nano`'s true native output is 2048-d (see investigation above); `Qwen/Qwen3-Embedding-0.6B` is correctly 1024
      Fix: see "Required fix" list above (5 concrete edits)
- [x] PASS — minimal entry defaults to `serving: endpoint` — `TestModalCatalog::test_serving_defaults_to_endpoint`, re-run green
- [x] PASS — `endpoint_name`/`app_name` derivation incl. `Org/My_Model..v2` — `TestModalCatalog::test_name_derivation` (3 params), re-verified live with additional odd ids (unicode, all-symbol suffix — see "Other issues found")
- [x] PASS — 11 parametrised validation errors — `TestModalCatalog::test_rejects_invalid_entries` (20 collected sub-tests incl. name derivation/duplicate params), all pass; independently mutation-checked by Tester (see Break path 6)
- [x] PASS — `AppConfig()`/no `modal:` section → empty catalog; frozen fixture carries the block — `test_modal_section_is_optional`, `test_frozen_config_carries_the_catalog`
- [x] PASS — `get_catalog_entry` lookup + sorted unknown-id message — `tests/unit/models/test_modal_catalog.py::TestLookup`, re-run green; confirmed exact-match (no fuzzy/prefix resolution) live
- [x] PASS — `resolve_serving` all six cases — `TestResolveServing`, re-run green; confirmed live with a hand-built entry lacking `base_model`
- [x] PASS — `build_server_args` for both engines incl. voyage's pinned-sha `--revision` — `TestServerArgs`, re-run green; confirmed live, and confirmed non-mutating (Break path 2)
- [x] PASS — `EmbeddingDeploySpec` JSON round-trip incl. `engine_version`/`server_name` — `TestDeploySpec::test_json_round_trip`, re-run green; confirmed live incl. empty-string bare-flag values (`--enforce-eager`, `--trust-remote-code`) surviving the round trip byte-for-byte
- [x] PASS — `prompt_for` — `TestPrompts`, re-run green
- [x] PASS — `import tree.models.modal_catalog` does not import `modal` — `test_catalog_does_not_import_modal` (subprocess), re-run green and confirmed by hand, exit 0
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green — reproduced independently, 3255 passed, 0 warnings

**Evidence**
```
$ make memory-format-check
uv run ruff format --check src/ tests/ scripts/ deploy/
297 files already formatted

$ make memory-lint-check
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests
tests/unit/models/test_modal_catalog.py .......................          [ 89%]
============================ 3255 passed in 45.99s =============================

$ curl -s https://huggingface.co/api/models/voyageai/voyage-4-nano | python3 -c "import json,sys;print(json.load(sys.stdin)['sha'])"
67fabc9bef010dabc5f6024aa1b1b6b93410426f

$ curl -s https://huggingface.co/voyageai/voyage-4-nano/raw/main/1_Pooling/config.json
{"word_embedding_dimension": 2048, "pooling_mode_cls_token": false, "pooling_mode_mean_tokens": true, ...}

$ curl -s https://huggingface.co/voyageai/voyage-4-nano/raw/main/modeling_qwen3_bidirectional.py | grep -n "linear ="
self.linear = nn.Linear(config.hidden_size, config.num_labels, bias=False)

$ python3 -c "<HTTP Range read of model.safetensors header>"
linear.weight {'dtype': 'BF16', 'shape': [2048, 1024], 'data_offsets': [0, 4194304]}
```

**Other issues found**
- `ModalEmbeddingModelConfig`'s `endpoint_name`/`app_name` derivation has no floor: a `repo_id` whose suffix is entirely non-alphanumeric symbols (e.g. `a/---`, which passes the `_REPO_ID_PATTERN` regex since `-`/`.` are permitted chars) collapses to an empty `endpoint_name` and `app_name == "ep-"`. Not in any AC, requires an absurd id, and two such entries would still be caught by the existing duplicate-`app_name` validator — flagged for a follow-up, not blocking this task.
- YAML comment at `apps/memory/configs/default.yaml:228` (`# config.json hidden_size, no Dense projection`) is factually wrong per the investigation above and must be corrected alongside the `native_dimensions` fix.
- Minor test-count discrepancy: SWE's report says "19 new in `test_app_config.py::TestModalCatalog`"; pytest collects 20 (`test_rejects_invalid_entries` has 11 params + `test_name_derivation` has 3 params, all counted individually). Cosmetic, not a defect.

**VERDICT: FAIL**

One blocking issue: `native_dimensions: 1024` for `voyageai/voyage-4-nano` is factually wrong (true native output is 2048-d, per primary-source evidence: HF safetensors weight shape, HF config.json/1_Pooling config.json, and vLLM's own model source, all independently agreeing). Everything else exercised — format/lint/pre-commit, all 3255 unit tests, all 11 rejection paths (independently mutation-checked), the env-override safe-failure, import isolation, non-mutation of the singleton, JSON round-trip, name derivation (incl. unicode/odd ids), both HF sha pins, and Qwen3 prompt byte-exactness — is genuinely green. The fix is narrow (5 files/lines) and fully specified above; no re-investigation needed by the SWE.

### [SWE] 2026-09-19 20:48 — Fixes (QA round 1)

**The blocking issue: `voyageai/voyage-4-nano` is natively 2048-d, not 1024-d.**
The Tester is right and the evidence is primary-source and unanimous: `config.json`
`num_labels: 2048` (not `hidden_size: 1024`), `1_Pooling/config.json`
`word_embedding_dimension: 2048`, the custom `modeling_qwen3_bidirectional.py`
`nn.Linear(config.hidden_size, config.num_labels, bias=False)` applied to
`last_hidden_state`, the safetensors header `linear.weight` shape `[2048, 1024]`
(PyTorch `[out, in]` → out 2048), and vLLM's `voyage.py` returning that head
untruncated. My original reading trusted `modules.json` (`Transformer → Pooling →
Normalize`), which can only list sentence-transformers modules — this projection
lives inside the custom `AutoModel` forward pass, so `modules.json` was never going
to show it. `native_dimensions` means "width the server returns when nothing is
asked for", so 2048 is the correct value and 1024 is the Matryoshka truncation that
`models.search_embedding.dimensions: 1024` (and the mongot `vector_index`) requires.

**Files modified**
- `apps/memory/configs/default.yaml` — voyage-4-nano `native_dimensions: 1024` → `2048`; the wrong `# config.json hidden_size, no Dense projection` comment replaced with the real mechanism (`num_labels` / the learned 1024→2048 `linear` head in the custom modeling code, invisible in `modules.json`) and why 1024 is a truncation. `matryoshka_dimensions: [256, 512, 1024, 2048]` unchanged — it is now consistent with the native rung instead of contradicting it.
- `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — same value for consistency.
- `apps/memory/tests/unit/config/test_app_config.py` — `TestModalCatalog::test_seed_entries`: `voyage.native_dimensions == 2048`, `qwen.native_dimensions == 1024`; the false comment "Both land in the 1024-d mongot vector_index untruncated" replaced with the asymmetry and the silent-failure mode it guards. Plus a 12th rejection case (below).
- `apps/memory/tests/unit/models/test_modal_catalog.py` — `TestDeploySpec::test_carries_the_hardware_and_the_app_name`: `spec.native_dimensions == 2048`, with the reason in a comment.
- `apps/memory/src/tree/config/app_config.py` — new `ModalEmbeddingModelConfig._check_the_derived_names_are_not_empty` (the Tester's optional finding).

**The Tester's optional finding — empty derived endpoint name — fixed**
`a/---` passes `_REPO_ID_PATTERN` but the derivation collapses every character
away, yielding `endpoint_name == ""` and `app_name == "ep-"`. Now an entry-level
`model_validator(mode="after")` rejects it at config load:

```
modal.embedding_models.2
  Value error, repo_id 'acme/---' derives an empty endpoint name: the part after '/' must contain at least one letter or digit (everything else is collapsed away).
```

Guarding the derived name (not the raw `repo_id`) means the check cannot drift from
the derivation it protects. Added as parametrised case
`repo-id-deriving-an-empty-endpoint-name` in `::test_rejects_invalid_entries`, so
that test now has **12** cases — the AC literal says 11 and all 11 of those remain;
the 12th is the QA finding, and the AC text was left alone (only bullet 1 was in my
lane to reword). Mutation-checked like the other eleven: with the guard body
short-circuited (`if False:`) the suite reports `1 failed, 3255 passed` — exactly
this case and nothing else, so it asserts OUR message, not pydantic boilerplate.
Source restored from a byte-copy and re-verified green.

**Tests**
- Unit: 3256 passing, 0 failing (`make memory-tests`, `make env-status` → local). 3255 before + the 12th rejection case.
- Integration: N/A — no integration suite in this repo.

**Acceptance criteria**
- [x] Seed entries — AC bullet 1's literal corrected per the Tester's primary-source finding ("both `native_dimensions == 1024`" → Qwen `== 1024`, voyage-4-nano `== 2048`) and re-ticked only after `::test_seed_entries` proved it. Every other field in that bullet is unchanged and still asserted.
- [x] All other bullets unchanged and still green (nothing else in this round touched their code paths).

**Evidence**

```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
297 files left unchanged
All checks passed!
297 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make env-status
Env target: local (.env)

$ make memory-tests
============================ 3256 passed in 46.07s =============================
```

The corrected width through the real loader and helper — this SUPERSEDES the
`"native_dimensions": 1024` in the `build_deploy_spec` JSON block of my
2026-09-19 19:38 entry:

```
$ uv run python -c "<catalog + build_deploy_spec>"
voyage native/matryoshka: 2048 [256, 512, 1024, 2048]
qwen   native/matryoshka: 1024 []
spec.native_dimensions   : 2048
index dim 1024 != native -> client must send dimensions=: True
```

**Superseding note on my 2026-09-19 19:38 entry** (history kept, not rewritten):
its "Verified against primary sources" bullet `voyage-4-nano native_dimensions: 1024
— config.json hidden_size 1024 and modules.json … no Dense projection, so the served
vector is 1024-d … 2048 looks unachievable from a 1024-d hidden state` is **WRONG and
superseded by this entry**. 2048 is not only achievable, it is the default: the
1024→2048 `linear` head is in the custom modeling code. The same entry's `NOT RUN`
bullet ("whether 2048-d output is real … stays for #141") and the `build_deploy_spec`
Evidence JSON (`"native_dimensions": 1024`) are superseded on the same grounds.

**Notes**
- `src/tree/models/modal_embedding.py` still carries a stale `_MODEL_NATIVE_DIMENSIONS = {"voyageai/voyage-4-nano": 1024}` table. Left untouched on purpose — **#140 deletes that table** and reads the catalog instead. Flagged here so it is not mistaken for a second source of truth that survived this fix; if #140 ever slips, that dict is the remaining place claiming 1024.
- Still open for #141, unchanged by this fix and NOT resolved by documents: whether vLLM's `/v1/embeddings` for this model honours a `dimensions` argument and truncates + renormalizes server-side, or whether #140's client must truncate client-side after receiving 2048 floats. Measure the wire length with and without `dimensions` before #140 ships either assumption. The catalog is now correct for BOTH outcomes: `native_dimensions: 2048` is what the server returns, `matryoshka_dimensions` says 1024 is a legal target.
- Untouched on purpose, per the orchestrator: `tasks/139-142`, `docs/adrs/009`, `docs/glossary.md` (PA is correcting those in parallel), `deploy/modal_vllm_embedding.py` (#142). `apps/memory/README.md` line 549 mentions voyage-4-nano on "an A10G" but makes no dimension claim — pre-existing text in #142's lane, left alone.
- `grep -rn` over the full diff plus `apps/memory/src` and `apps/memory/README.md` for `1024` near `nano` found no remaining straggler claiming voyage-4-nano is 1024-d, other than the two deliberate exclusions above. Two test-file `1024`s DID share a line-neighbourhood with the nano id, and both were dealt with: `_VALID_ENTRY` (the rejection-case base fixture, which borrows the nano `repo_id`) now carries `native_dimensions: 2048` so no fixture contradicts the catalog — no rejection case asserts on that key, each mutates exactly one OTHER key; `::test_name_derivation` passes `native_dimensions=1024` as filler to three unrelated ids (nano, Qwen3, `Org/My_Model..v2`) and asserts only on `endpoint_name`/`app_name`, never on the width, so it makes no dimension claim. `::test_serving_defaults_to_endpoint` is BGE-M3 and unrelated.
- NOT RUN — anything live: no Modal call, no deploy, no DB write, no `.env`/`.env.prod` read. Local env throughout.

### [Tester] 2026-09-19 21:20 — QA (round 2)

**Scope of this pass:** re-QA after the round-1 FAIL (`native_dimensions: 1024` for `voyageai/voyage-4-nano` was wrong). `make env-status` = local throughout; no `.env`/`.env.prod` opened; no Modal call, no deploy, no DB write. `tasks/139-142`, `docs/adrs/009`, `docs/glossary.md` untouched (confirmed via `git status --short` — not in the diff).

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all clean)
- Unit tests: 3256 passed / 0 failed (`make memory-tests`)
- Integration tests: N/A — no integration suite in this repo (deleted deliberately per AGENTS.md)
- Warnings: 0

**E2E adversarial pass**
- Happy path: real loader in a subprocess — `get_catalog_entry('voyageai/voyage-4-nano').native_dimensions == 2048`, `.matryoshka_dimensions == [256, 512, 1024, 2048]`; Qwen `native_dimensions == 1024`, `matryoshka_dimensions == []`; `build_deploy_spec('voyageai/voyage-4-nano', 'vllm').native_dimensions == 2048`; `EmbeddingDeploySpec.model_validate_json(spec.model_dump_json()) == spec` — all PASS.
- Break path 1 (mutation-adversarial, independent of the SWE's own check): neutered `ModalEmbeddingModelConfig._check_the_derived_names_are_not_empty` (`if not self.endpoint_name` → `if False`) in the live source, re-ran `make memory-tests` → exactly `1 failed, 3255 passed`, and the ONE failure was `TestModalCatalog::test_rejects_invalid_entries[repo-id-deriving-an-empty-endpoint-name]` (`DID NOT RAISE ValidationError`). Restored the file byte-for-byte (`git diff` shows only the original 272-insertion/1-deletion diff, nothing else) and re-confirmed 3256 passed. PASS — the guard is load-bearing.
- Break path 2 (boundary: legitimate odd repo_ids through the new validator): `Org/My_Model..v2` → `my-model-v2`/`ep-my-model-v2` (PASS); `org/a` (single char) → `a`/`ep-a` (PASS); `org/12345` (digits-only) → `12345`/`ep-12345` (PASS) — none of these false-positive against the new empty-name guard.
- Break path 3 (hostile/degenerate input): `acme/---` → rejected with `repo_id 'acme/---' derives an empty endpoint name: the part after '/' must contain at least one letter or digit (everything else is collapsed away).` (PASS, matches SWE's claimed message exactly). `a/日本語` (all-CJK suffix, `\w` is Unicode-aware so it passes `_REPO_ID_PATTERN` but `[^a-z0-9]+` strips it entirely) → also correctly rejected by the same guard (PASS on behavior). Note: the message says "must contain at least one letter or digit" — for an all-CJK suffix this reads as slightly misleading (it IS letters, just not ASCII a-z); not blocking, wording nit only, see "Other issues found."
- Break path 4 (duplicate-app-name validator unaffected by the new guard): two entries `orgA/foo` / `orgB/foo` still correctly collide on `ep-foo` with the original message intact (`both derive` / `one app per model`) — confirmed live. PASS.

**Verification of the round-1 fix (blocking issue)**
- `apps/memory/configs/default.yaml`: voyage-4-nano `native_dimensions: 2048`, comment now correctly describes the `num_labels` / learned `nn.Linear(1024, 2048)` head in the custom modeling code, invisible in `modules.json` — confirmed via `git diff`.
- `apps/memory/tests/unit/config/test_app_config.py::TestModalCatalog::test_seed_entries` — asserts `qwen.native_dimensions == 1024`, `voyage.native_dimensions == 2048`, comment reworded to state the asymmetry — confirmed.
- `apps/memory/tests/unit/models/test_modal_catalog.py::TestDeploySpec::test_carries_the_hardware_and_the_app_name` — `spec.native_dimensions == 2048` — confirmed.
- `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — voyage entry `native_dimensions: 2048` — confirmed via `git diff`.
- `_VALID_ENTRY["native_dimensions"]` in `test_app_config.py` → `2048` (the rejection-case base fixture borrows the nano `repo_id`, so it no longer contradicts the catalog) — confirmed.
- Grep sweep for a remaining "voyage-4-nano is 1024-d" claim, across `apps/memory/src`, `apps/memory/configs`, `apps/memory/tests`, `apps/memory/README.md`, and the full diff (`git diff | grep -n "1024" | grep -i "nano\|voyage"`): the only hits are (a) the round-1 QA/SWE Log prose recounting the WRONG original value for the historical record (expected — Log entries are appended, not rewritten) and (b) `TestModalCatalog::test_name_derivation`'s `native_dimensions=1024` filler value passed to three unrelated ids (nano, Qwen3, `Org/My_Model..v2`) — that test asserts only on `endpoint_name`/`app_name`, never on width, so it makes no dimension claim; judged not a straggler. No other file makes a "voyage-4-nano == 1024" claim.
- `src/tree/models/modal_embedding.py`'s `_MODEL_NATIVE_DIMENSIONS = {"voyageai/voyage-4-nano": 1024}` — confirmed genuinely untouched (`git status --short` shows no `M` on this file) and correctly flagged in the SWE's Notes as the one remaining place claiming 1024, deliberately out of this task's scope (#140 deletes it).
- Log history: `git diff tasks/138-modal-embedding-catalog-config.md | grep "^-" | grep -v "^---"` shows only the `status:` frontmatter flip and the 12 `- [ ]` → `- [x]` checkbox lines (one of which — bullet 1 — has its literal text changed alongside the tick, as authorised). No text inside any prior `## Log` entry was removed or rewritten — the "appended, not rewritten" claim holds.

**Acceptance criteria**
- [x] PASS — 2 seed entries with pinned fields; Qwen `native_dimensions == 1024`, voyage-4-nano `native_dimensions == 2048` — `TestModalCatalog::test_seed_entries`, re-run green; confirmed live through the real loader in a subprocess (see Happy path above)
- [x] PASS — minimal entry defaults to `serving: endpoint` — `TestModalCatalog::test_serving_defaults_to_endpoint`
- [x] PASS — `endpoint_name`/`app_name` derivation incl. `Org/My_Model..v2` — `TestModalCatalog::test_name_derivation`; re-verified live incl. single-char (`org/a`) and digits-only (`org/12345`) ids
- [x] PASS — validation errors, now 12 parametrised cases (11 original + the new empty-derived-name guard) — `TestModalCatalog::test_rejects_invalid_entries`, `--collect-only` confirms 12 collected, all pass; independently mutation-checked (Break path 1). **Note for the literal:** the bullet's text (this file, the `Validation errors` bullet) still says "11 cases" — recommend it be updated to 12 for accuracy (11 required + the QA-sourced 12th), since a future reader comparing count-to-AC can't otherwise tell a case was added rather than one of the original 11 lost. Not blocking — behaviorally all 11 original cases are intact and independently verified.
- [x] PASS — `AppConfig()`/no `modal:` section → empty catalog; frozen fixture carries the block — `test_modal_section_is_optional`, `test_frozen_config_carries_the_catalog`
- [x] PASS — `get_catalog_entry` lookup + sorted unknown-id message — `TestLookup`, re-confirmed live
- [x] PASS — `resolve_serving` all six cases — `TestResolveServing`, re-confirmed live
- [x] PASS — `build_server_args` for both engines incl. voyage's pinned-sha `--revision` — `TestServerArgs`
- [x] PASS — `EmbeddingDeploySpec` JSON round-trip incl. `engine_version`/`server_name`, now with `native_dimensions == 2048` for voyage — `TestDeploySpec::test_json_round_trip`, re-confirmed live via `model_validate_json(spec.model_dump_json()) == spec`
- [x] PASS — `prompt_for` — `TestPrompts`
- [x] PASS — `import tree.models.modal_catalog` does not import `modal` — `test_catalog_does_not_import_modal`, re-confirmed live, exit 0
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green — reproduced independently, 3256 passed, 0 warnings

**Evidence**
```
$ make memory-format-check && make memory-lint-check
297 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests
============================ 3256 passed in 45.46s =============================

$ uv run python -c "<real loader + get_catalog_entry + build_deploy_spec>"
voyage native/matryoshka: 2048 [256, 512, 1024, 2048]
qwen native/matryoshka: 1024 []
spec.native_dimensions: 2048
round trip OK

$ uv --directory apps/memory run pytest tests/unit/config/test_app_config.py::TestModalCatalog::test_rejects_invalid_entries --collect-only -q
12 tests collected

# Mutation check on the new validator, run independently of the SWE:
$ sed -i '' 's/if not self.endpoint_name:/if False:/' src/tree/config/app_config.py
$ make memory-tests
FAILED tests/unit/config/test_app_config.py::TestModalCatalog::test_rejects_invalid_entries[repo-id-deriving-an-empty-endpoint-name]
1 failed, 3255 passed
# restored, re-ran: 3256 passed, git diff confirmed byte-identical to pre-mutation
```

**Other issues found**
- `Validation errors` AC bullet's literal still says "(parametrised, 11 cases)" though the test now collects 12 (11 original + the QA-sourced empty-derived-name case). Recommend the PA update the literal to 12 in the same pass as the bullet-1 rewording, since this bullet's edit is also PA/orchestrator lane per the SWE's log. Non-blocking.
- The new validator's error message ("the part after '/' must contain at least one letter or digit") is slightly imprecise for an all-non-ASCII-letter suffix (e.g. `a/日本語`, which IS letters, just not `[a-z0-9]`) — the rejection itself is correct (confirmed live), but a CJK-named repo would read the message as contradicting what it did. Cosmetic wording nit, not blocking, no AC covers Unicode repo_ids.
- (Carried from round 1, still true, still non-blocking) `endpoint_name`/`app_name` derivation still has no floor for repo_ids in `matryoshka_dimensions`-adjacent territory beyond what's now guarded — the new validator closes the gap the round-1 Tester found; no further gap found this round.

**VERDICT: PASS**

All acceptance criteria verified with evidence, full suite green at 3256/3256 with 0 warnings, format/lint/pre-commit clean, the round-1 blocking issue (`native_dimensions` for voyage-4-nano) is fixed everywhere it needed to be and nowhere else contradicts it (except the deliberately-excluded, task-out-of-scope `_MODEL_NATIVE_DIMENSIONS` dict slated for #140's deletion), the new empty-derived-name validator is genuinely load-bearing (independent mutation check isolated exactly the one test it should), and the e2e adversarial pass (mutation attack, boundary ids, hostile/degenerate ids, duplicate-app-name interaction) is green. Two non-blocking wording nits recorded above for the PA/SWE's discretion.
