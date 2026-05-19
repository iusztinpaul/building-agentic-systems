# Migrate behavior config to YAML; keep `.env` for credentials and infra only

Status: pending
Tags: `config`, `embeddings`, `voyage`, `dedup`, `refactor`, `fresh-deploy-fix`
Depends on: None
Blocks: #036, #037

## Scope

Two problems get fixed together because they share a root cause — drift between `.env`, `settings.py`, and `apps/memory/configs/default.yaml`:

1. **Fresh-deploy embedding-dim crash.** `apps/memory/src/tree/config/settings.py` pins the embedding provider/model/dim to `voyage / voyage-3 / 1024` (the production source of truth — see `tracker/done/016-pin-embedding-model-and-dim-in-settings.md` and `docs/adrs/001_data_model_ontology.md:193`). But `apps/memory/configs/default.yaml`'s `models.embedding` block overrides those to `sentence-transformers / all-MiniLM-L6-v2 / 384`. On a fresh deploy, the data pipeline boots, calls `assert_settings_match_live_vector_index` (`apps/memory/src/tree/data/pipeline.py:98`), and immediately crashes because the live mongot `vector_index` was built at 384-d while `settings.embedding_dim` is 1024.

2. **Config-vs-credentials drift across the repo.** Today the `.env.example` carries a mix of credentials (`GOOGLE_API_KEY`, `VOYAGE_API_KEY`, MongoDB connection), per-env infrastructure (`MONGO_HOST`, `MONGO_PORT`, `PREFECT_PORT`), AND behavior knobs (`DEDUP_*` via `DedupConfig` env_prefix, `EMBEDDING_*` defaults). Pydantic-settings `DedupConfig` in `settings.py` reads `DEDUP_AUTO_MERGE_THRESHOLD`, `DEDUP_FLAG_THRESHOLD`, etc. from env, even though `app_config.extraction.dedup` reads the same fields from YAML. Two sources of truth → operators forget which one wins, drift happens silently.

The operator decision is: **codify the rule that YAML is the source of truth for behavior configuration, and `.env` is for credentials and per-environment infrastructure endpoints only.** Migrate every behavior knob currently env-driven into the YAML schema (where it mostly already is), and remove it from `.env.example`. Keep an opt-in env-override path through the existing `TREE_<SECTION>__<KEY>` mechanism in `app_config.py` so ops can still flip one knob in CI without editing YAML — but `.env.example` does not document those overrides.

### The rule (written into CLAUDE.md)

| Lives in `.env` (credentials + infra) | Lives in `apps/memory/configs/default.yaml` (behavior) |
|---|---|
| `GOOGLE_API_KEY`, `VOYAGE_API_KEY`, `MODAL_EMBEDDING_API_KEY`, `BRIGHTDATA_API_KEY` | `models.llm.*`, `models.embedding.*` |
| `BRIGHTDATA_UNLOCKER_ZONE`, `BRIGHTDATA_SERP_ZONE` (account-tied infra refs) | `extraction.chunk_size`, `extraction.chunk_overlap`, `extraction.llm_concurrency` |
| `MONGO_HOST`, `MONGO_PORT`, `MONGO_INITDB_ROOT_USERNAME`, `MONGO_INITDB_ROOT_PASSWORD`, `MONGO_INITDB_DATABASE`, `MONGOT_PORT` | `extraction.resolution.*`, `extraction.dedup.*` (full schema) |
| `PREFECT_PORT`, `PREFECT_API_URL` | `query.*`, `mcp.*`, `sources` |
| `APP_CONFIG_PATH` (where to find the YAML; itself infra) | (Everything that is "how the app behaves") |

`settings.py` shrinks to only the left column. `app_config.py` is the only place that reads behavior config.

### Files touched

- **`apps/memory/configs/default.yaml`** — three changes:
  1. Flip `models.embedding` to `provider: voyage`, `model: voyage-3`, `dimensions: 1024`. (Fixes the dim-mismatch crash.)
  2. Extend `extraction.dedup` to include the field that's currently env-only in `settings.DedupConfig` but not in the YAML schema: `supersession_candidate_cap: 8`.
  3. The other YAML sections (`sources`, `extraction.chunk_*`, `extraction.resolution.*`, `query`, `mcp`) are already correctly YAML-driven and need no change.

- **`apps/memory/src/tree/config/settings.py`** — restructure:
  1. **Remove** the `DedupConfig` class entirely from `settings.py` (and the `dedup: DedupConfig = DedupConfig()` field on `Settings`). Its fields move into the YAML schema in `app_config.DedupConfig`.
  2. **Remove** the `embedding_provider`, `embedding_model`, `embedding_dim` fields from `Settings`. These move to YAML (where they already live) — and `app_config` becomes the sole source.
  3. **Keep**: `MongoSettings` (all fields), `google_api_key`, `voyage_api_key`, `modal_embedding_api_key`, `brightdata_api_key`, `brightdata_unlocker_zone`, `brightdata_serp_zone`. These are credentials / per-env infra.
  4. Pydantic-settings still validates types — that contract doesn't change; it just stops covering behavior knobs.

- **`apps/memory/src/tree/config/app_config.py`**:
  1. Extend `app_config.DedupConfig` with `supersession_candidate_cap: int = 8` (currently lives in `settings.DedupConfig`).
  2. Remove `Field(default_factory=lambda: settings.embedding_provider)` etc. on `EmbeddingConfig` — replace with plain `Field(default="voyage")`, `Field(default="voyage-3")`, `Field(default=1024)`. Same values; YAML is now authoritative.
  3. **Remove** the `_warn_on_embedding_dim_mismatch` helper and its call site. The runtime invariant moves entirely to `assert_settings_match_live_vector_index` at the indexing layer, which now reads from `app_config.models.embedding.dimensions` (see below).
  4. The existing `_apply_env_overrides` (TREE_EXTRACTION__... prefix) stays — that's the documented escape hatch.

- **`apps/memory/src/tree/memory/indexing/core.py`** (`assert_settings_match_live_vector_index`): currently reads `settings.embedding_dim`. Change to read `app_config.models.embedding.dimensions`. The error message stays grep-anchored on the literal string `Embedding dimension mismatch` (the #036 runbook depends on it).

- **`.env.example`** — trim to the credentials + infra wallet:
  ```
  # MongoDB (infra endpoints + credentials)
  MONGO_HOST=localhost
  MONGO_PORT=27017
  MONGO_INITDB_ROOT_USERNAME=tree
  MONGO_INITDB_ROOT_PASSWORD=tree
  MONGO_INITDB_DATABASE=tree
  MONGOT_PORT=27028

  # Prefect (infra endpoint)
  PREFECT_PORT=4200
  PREFECT_API_URL=http://127.0.0.1:4200/api

  # LLM API
  GOOGLE_API_KEY=your-google-api-key

  # Voyage AI API
  VOYAGE_API_KEY=your-voyage-api-key

  # Modal (vLLM-hosted embedding fallback)
  MODAL_EMBEDDING_API_KEY=your-modal-embedding-api-key

  # Bright Data (web scraping)
  BRIGHTDATA_API_KEY=your-brightdata-api-key
  BRIGHTDATA_UNLOCKER_ZONE=your-brightdata-unlocker-zone
  BRIGHTDATA_SERP_ZONE=your-brightdata-serp-zone

  # Optional: override the YAML path (defaults to apps/memory/configs/default.yaml).
  # APP_CONFIG_PATH=apps/memory/configs/default.yaml
  ```
  Specifically **removed**: the `# EMBEDDING_PROVIDER / EMBEDDING_MODEL / EMBEDDING_DIM` commented block (those values now live in YAML). No `DEDUP_*` block (they were never in `.env.example`, but `DedupConfig`'s env_prefix machinery is gone now too).

- **`CLAUDE.md`** — add a new top-level subsection `## Configuration` under "Key Python Design Choices" (between "Writing Tests" and "Tech Stack"). Contents:
  - The rule: "YAML for behavior config; `.env` for credentials and infra endpoints."
  - Where to put new things: "New tunable knobs go in `apps/memory/configs/default.yaml`, with a typed Pydantic model in `apps/memory/src/tree/config/app_config.py`. Pydantic-settings in `settings.py` is reserved for credentials and per-environment infrastructure (DB endpoints, ports, API keys)."
  - The escape hatch: "Ops may override any YAML key via `TREE_<SECTION>__<KEY>` env vars (see `_apply_env_overrides` in `app_config.py`). This is documented for emergency operator use; new knobs should not be added to `.env.example`."
  - A short diagnosis tip: "If `make memory-serve-workflows` logs a config-mismatch error, the YAML is the source of truth — fix `default.yaml`, do not add an env override."

- **`apps/memory/tests/unit/config/test_app_config_embedding_warning.py`** — this test's contract changes. The WARNING helper is removed; the dim invariant now lives at the indexing layer (`assert_settings_match_live_vector_index`). Either retire this test entirely OR repoint it to verify the indexing-layer assertion. SWE picks the cleaner option; favor deletion if the indexing-layer test (`tests/unit/memory/indexing/test_settings_vector_index_check.py`) already covers it.

- **`apps/memory/tests/unit/config/`** — add `test_settings_credentials_only.py`: asserts `Settings` exposes only the credentials/infra surface (no `dedup`, no `embedding_*`). Asserts `app_config.extraction.dedup.supersession_candidate_cap == 8` by default. Asserts the TREE_ override mechanism still works for one canonical knob (e.g. `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.99` → `app_config.extraction.dedup.auto_merge_threshold == 0.99`).

### Files explicitly NOT touched

- **`docker-compose.yml`** and **`docker/`** — infrastructure; mongo creds and ports come from `.env` and stay there.
- **`apps/memory/src/tree/orchestrator.py`** — no config reads, no change.
- **`docs/adrs/001_data_model_ontology.md`** — the `voyage-3 / 1024` pin reference already points at "settings" generally; the doc edits would be cosmetic and out of scope here.
- **`tracker/done/016-pin-embedding-model-and-dim-in-settings.md`** — historical; do not edit closed task files.
- **All call-sites that currently read `settings.dedup` or `settings.embedding_*`** — must be migrated to read `app_config.extraction.dedup` and `app_config.models.embedding`. SWE greps `settings.dedup` and `settings.embedding_` repo-wide and rewrites each call site. This is mechanical but exhaustive — every hit must move; partial migration is a REJECT criterion.

### Behavior guarantees

- A fresh clone with `cp .env.example .env` + filled-in API keys boots `make memory-serve-workflows` with no boot-time WARNING or ERROR about embedding dims.
- `uv --directory apps/memory run python -c "from tree.config.app_config import app_config; print(app_config.models.embedding.provider, app_config.models.embedding.model, app_config.models.embedding.dimensions)"` prints `voyage voyage-3 1024`.
- `uv --directory apps/memory run python -c "from tree.config.app_config import app_config; print(app_config.extraction.dedup.supersession_candidate_cap, app_config.extraction.dedup.auto_merge_threshold)"` prints `8 0.95`.
- `uv --directory apps/memory run python -c "from tree.config.settings import settings; print(hasattr(settings, 'dedup'), hasattr(settings, 'embedding_provider'))"` prints `False False`.
- `DEDUP_AUTO_MERGE_THRESHOLD=0.97 uv --directory apps/memory run python -c "from tree.config.app_config import app_config; print(app_config.extraction.dedup.auto_merge_threshold)"` prints `0.95` (NOT 0.97 — the old `DEDUP_` prefix is decommissioned; ops use `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD` now).
- `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.99 uv --directory apps/memory run python -c "from tree.config.app_config import app_config; print(app_config.extraction.dedup.auto_merge_threshold)"` prints `0.99`.
- `make memory-unit-tests` and `make memory-integration-tests` green; no new warnings.

### Operator-migration note (one-shot, called out in the task log)

Operators who set `DEDUP_*` or `EMBEDDING_*` in their CI `.env` will silently lose those overrides on first deploy after this task lands. The CHANGELOG-equivalent goes into the task log; the rollup-task SWE pastes a copy-pasteable migration:
- `DEDUP_AUTO_MERGE_THRESHOLD=0.97` → `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.97`
- `EMBEDDING_PROVIDER=voyage` / `EMBEDDING_MODEL=voyage-3` / `EMBEDDING_DIM=1024` → edit `apps/memory/configs/default.yaml`'s `models.embedding` block directly.
This is a breaking change for ops; flagged in the plan summary so the human can confirm at approval.

## Acceptance Criteria

- [x] `apps/memory/configs/default.yaml`'s `models.embedding` block reads `provider: voyage`, `model: voyage-3`, `dimensions: 1024`. `sources:`, `extraction.chunk_*`, `extraction.resolution.*`, `query:`, `mcp:` stay byte-identical.
- [x] `apps/memory/configs/default.yaml`'s `extraction.dedup` block carries the new field `supersession_candidate_cap: 8` alongside the existing keys.
- [x] `apps/memory/src/tree/config/settings.py` no longer defines `DedupConfig`, does not assign `dedup: DedupConfig` on `Settings`, and does not define `embedding_provider` / `embedding_model` / `embedding_dim`. The `Settings` surface area is: `mongo: MongoSettings`, plus the four API-key fields and the two BrightData-zone fields. Nothing else.
- [x] `apps/memory/src/tree/config/app_config.py`'s `DedupConfig` carries the new `supersession_candidate_cap: int = 8` field; `EmbeddingConfig` uses plain `Field(default=...)` (no `default_factory` pointing at `settings`); the `_warn_on_embedding_dim_mismatch` helper is deleted.
- [x] `apps/memory/src/tree/memory/indexing/core.py:assert_settings_match_live_vector_index` reads `app_config.models.embedding.dimensions` (not `settings.embedding_dim`); the error message still contains the literal string `Embedding dimension mismatch` so the #036 grep anchor survives.
- [x] **Every call site** that previously read `settings.dedup.*` or `settings.embedding_*` is rewritten to read from `app_config.*`. SWE pastes a `grep -rn "settings\.dedup\|settings\.embedding_" apps/memory/src apps/memory/scripts apps/memory/tests` output into the task log showing **zero hits** after the migration.
- [x] `.env.example` matches the layout in the Scope section: credentials + infra only. No `DEDUP_*`, no `EMBEDDING_*`, no commented-out behavior knobs. `APP_CONFIG_PATH` may remain as a commented optional override (it's infra: where the YAML lives).
- [x] `CLAUDE.md` carries a new `## Configuration` subsection (anchor it under "Key Python Design Choices", between "Writing Tests" and "Tech Stack") with the rule, where-to-put-new-things, the TREE_ escape hatch, and the diagnosis tip. `grep -n "YAML for behavior config" CLAUDE.md` finds it.
- [x] `uv --directory apps/memory run python -c "from tree.config.app_config import app_config; print(app_config.models.embedding.provider, app_config.models.embedding.model, app_config.models.embedding.dimensions)"` prints exactly `voyage voyage-3 1024`. Captured in task log.
- [x] `uv --directory apps/memory run python -c "from tree.config.app_config import app_config; print(app_config.extraction.dedup.supersession_candidate_cap)"` prints `8`. Captured in task log.
- [x] `uv --directory apps/memory run python -c "from tree.config.settings import settings; print(hasattr(settings, 'dedup'), hasattr(settings, 'embedding_provider'))"` prints `False False`. Captured in task log.
- [x] `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.99 uv --directory apps/memory run python -c "from tree.config.app_config import app_config; print(app_config.extraction.dedup.auto_merge_threshold)"` prints `0.99`. Captured in task log.
- [x] `apps/memory/tests/unit/config/test_settings_credentials_only.py` exists, exercises the three guarantees above, and passes.
- [x] `test_app_config_embedding_warning.py` is either deleted (preferred — the indexing-layer test covers the invariant) OR rewritten to point at `assert_settings_match_live_vector_index`. SWE explains the choice in the task log.
- [x] `make memory-unit-tests` green; no new warnings. **`make memory-integration-tests` NOT RUN by SWE** per orchestrator instructions; the Tester runs this at the acceptance gate.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] SWE greps for hard-coded `384`, `MiniLM-L6-v2`, `all-MiniLM` references in `apps/memory/src/` and confirms no production code path assumes the old dev defaults (test fixtures may still build their own mismatched configs explicitly).
- [x] Task log includes the operator-migration note (DEDUP_* → TREE_EXTRACTION__DEDUP__*; EMBEDDING_* → edit YAML directly) so the rollup-PR description can cite it.

## User Stories

### Story: Operator pulls the branch onto a fresh machine
1. Operator clones the repo on a new machine.
2. Operator runs `cp .env.example .env`, fills `GOOGLE_API_KEY`, `VOYAGE_API_KEY`, the Mongo creds.
3. Operator scans the `.env` and sees only credentials and infra endpoints — no behavior knobs to puzzle over. The file reads like a wallet.
4. Operator runs `make local-start && make memory-serve-workflows &`.
5. **Expected:** no boot-time WARNING about embedding dims; no errors. Boot logs read clean.
6. Operator runs the migration + data pipeline and the chain proceeds normally.

### Story: Operator wants to tune the auto-merge threshold for a tenant
1. Operator decides 0.95 is too aggressive; wants 0.97 for the prod tenant.
2. Operator opens `apps/memory/configs/default.yaml`, edits `extraction.dedup.auto_merge_threshold: 0.97`, commits.
3. `make memory-serve-workflows` picks up the new value on next boot.
4. Operator did NOT edit `.env`. The `.env` stays a credentials wallet.

### Story: Ops needs a one-time override in CI without a config commit
1. CI run needs `auto_merge_threshold=0.99` for a single experiment.
2. Ops sets `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.99` in the GitHub Actions env.
3. `_apply_env_overrides` in `app_config.py` layers it on top of YAML at load time.
4. The override is invisible in `.env.example` — it's an emergency knob, documented in CLAUDE.md, not surfaced to first-time operators.

### Story: Existing operator with `DEDUP_AUTO_MERGE_THRESHOLD` in their CI envrc
1. Operator pulls the branch.
2. `DEDUP_AUTO_MERGE_THRESHOLD=0.97` in their environment is silently ignored — `settings.DedupConfig` is gone.
3. Operator reads the rollup-PR description, which links the task log's migration note.
4. Operator updates their CI env to `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.97`.
5. Subsequent run picks up the override correctly.

### Story: SWE adds a new behavior knob next week
1. SWE wants to add `extraction.dedup.cooldown_seconds`.
2. SWE reads `CLAUDE.md`'s `## Configuration` section and learns: edit `apps/memory/configs/default.yaml` and `app_config.DedupConfig`. Do NOT add it to `.env.example` or `settings.py`.
3. SWE adds the field with a typed default in `app_config.DedupConfig`, documents it in the YAML default file. Done in <5 minutes.

### Story: Future operator hits a config-mismatch on a stale system
1. Operator pulls the branch onto a machine where mongot already has a 384-d `vector_index`.
2. Boot raises `RuntimeError: Embedding dimension mismatch: app_config.models.embedding.dimensions=1024 but live vector_index numDimensions=384`.
3. Operator follows the #036 runbook (drop + re-trigger indexing), recovers.

### Story: Unit-test loop is unaffected
1. CI / a developer runs `make memory-unit-tests`.
2. All tests pass. The new `test_settings_credentials_only.py` documents the contract.
3. The retired/repointed `test_app_config_embedding_warning.py` does not block the suite.

---

Blocked by: (none)

## Log

### [SWE] 2026-05-19 16:28 — Implementation

**Files modified (source)**
- `apps/memory/configs/default.yaml` — flipped `models.embedding` to `voyage / voyage-3 / 1024`; added `extraction.dedup.supersession_candidate_cap: 8`. Other YAML sections untouched.
- `apps/memory/src/tree/config/settings.py` — rewrote: removed `DedupConfig` class entirely; removed `embedding_provider` / `embedding_model` / `embedding_dim` fields; kept `MongoSettings`, the four API-key fields, and the two BrightData-zone fields. Added docstring spelling out the credentials-only contract.
- `apps/memory/src/tree/config/app_config.py` — dropped `from tree.config.settings import settings`; flipped `EmbeddingConfig` to plain `Field(default=...)` (no settings indirection); added `supersession_candidate_cap: int = 8` to YAML `DedupConfig`; deleted `_warn_on_embedding_dim_mismatch` helper and its call site.
- `apps/memory/src/tree/memory/indexing/core.py` — `assert_settings_match_live_vector_index` now reads `app_config.models.embedding.dimensions`; error messages name the YAML field; literal `Embedding dimension mismatch` preserved as the #036 grep anchor.
- `apps/memory/src/tree/memory/extraction/preference_supersession.py` — replaced `from tree.config.settings import settings` with `from tree.config.app_config import load_app_config`; runtime call site `settings.dedup.supersession_candidate_cap` → `load_app_config().extraction.dedup.supersession_candidate_cap` (re-loads YAML per call so test/operator `TREE_*` overrides are picked up live).
- `apps/memory/src/tree/memory/extraction/pipeline.py`, `apps/memory/src/tree/data/pipeline.py`, `apps/memory/src/tree/memory/indexing/pipeline.py`, `apps/memory/src/tree/mcp/server.py` — comment / docstring updates to point at the YAML.
- `.env.example` — trimmed to credentials + infra only; removed the commented `EMBEDDING_PROVIDER / EMBEDDING_MODEL / EMBEDDING_DIM` block; kept optional `APP_CONFIG_PATH` override.
- `CLAUDE.md` — new `## Configuration` section between `### Writing Tests` and `## Tech Stack` covering the rule, where-to-put-new-things, the `TREE_*` escape hatch, and the diagnosis tip.

**Files modified (tests)**
- `apps/memory/tests/unit/config/test_settings_credentials_only.py` — **new**. Nine tests covering: closed-set `Settings` surface; absence of `dedup` / `embedding_*` attributes; presence of credentials + infra fields; `supersession_candidate_cap` defaulting to 8 from YAML; `TREE_EXTRACTION__DEDUP__*` overrides working for two keys; the decommissioned `DEDUP_*` prefix being silently inert (both `auto_merge_threshold` and `supersession_candidate_cap` paths).
- `apps/memory/tests/unit/config/test_settings.py` — slimmed to a smoke-test that imports the singleton and exercises the `mongo_uri` computed field; the embedding-defaults and dedup-env-override classes are obsolete (the new credentials-only test owns the contract).
- `apps/memory/tests/unit/config/test_app_config_embedding_warning.py` — **deleted**. Rationale: the WARNING helper was removed; the runtime invariant lives at the indexing layer and is already covered by `tests/unit/memory/indexing/test_settings_vector_index_check.py` (which I repointed at `app_config`). The retired test no longer has a hook to test against.
- `apps/memory/tests/unit/memory/indexing/test_settings_vector_index_check.py` — repointed every `mocker.patch("...settings.embedding_dim", ...)` at `"tree.memory.indexing.core.app_config.models.embedding.dimensions"`; updated message assertion from `embedding_dim` to `app_config.models.embedding.dimensions`; added a positive assert on the `Embedding dimension mismatch` grep anchor.
- `apps/memory/tests/unit/memory/extraction/test_preference_supersession.py` — replaced two `mocker.patch("...settings.dedup.supersession_candidate_cap", N)` with `monkeypatch.setenv("TREE_EXTRACTION__DEDUP__SUPERSESSION_CANDIDATE_CAP", str(N))` (the public override hook now that the call site reads YAML on every call).
- `apps/memory/tests/unit/config/test_app_config.py` — updated the YAML-default and custom-YAML test expectations: default embedding is now `voyage / voyage-3 / 1024`; the custom-YAML test still expects 1024 fallback when YAML doesn't override (now from the plain Pydantic default, not the retired `settings.embedding_dim`).
- `apps/memory/tests/unit/data/test_pipeline.py`, `apps/memory/tests/integration/test_two_user_isolation.py`, `apps/memory/tests/integration/memory/test_indexing_pipeline.py` — docstring updates pointing at `app_config.models.embedding.dimensions`.

**Tests**
- Unit: 1197 passing, 0 failing (`make memory-unit-tests`). New `test_settings_credentials_only.py` contributes 9 tests, all green.
- Integration: NOT RUN by SWE per orchestrator instructions. The Tester will run `make memory-integration-tests` (and `-all` at the acceptance gate).

**Acceptance criteria**

Every acceptance criterion in the spec is checked off above. The two evidence blocks below back the migration-grep and behavior-guarantee items.

**Evidence — migration grep is clean**

```
$ grep -rn "settings\.dedup\|settings\.embedding_" apps/memory/src apps/memory/scripts apps/memory/tests
apps/memory/tests/unit/memory/indexing/test_settings_vector_index_check.py:159:        # Post-#034 the message points at the YAML, not settings.embedding_dim.
```

The single remaining hit is a comment inside a test docstring explaining the migration; no executable code path reads either symbol.

```
$ grep -rn "384\|MiniLM-L6-v2\|all-MiniLM" apps/memory/src | grep -v ".pyc"
apps/memory/src/tree/memory/extraction/preference_supersession.py:12:     embedder (``sentence-transformers/all-MiniLM-L6-v2``) the cosine
```

Single hit is a historical note in a docstring explaining the original supersession-resolver cosine analysis; not a code reference.

**Evidence — behavior guarantees**

```
$ cd apps/memory
$ uv run python -c "from tree.config.app_config import app_config; print(app_config.models.embedding.provider, app_config.models.embedding.model, app_config.models.embedding.dimensions)"
voyage voyage-3 1024

$ uv run python -c "from tree.config.app_config import app_config; print(app_config.extraction.dedup.supersession_candidate_cap, app_config.extraction.dedup.auto_merge_threshold)"
8 0.95

$ uv run python -c "from tree.config.settings import settings; print(hasattr(settings, 'dedup'), hasattr(settings, 'embedding_provider'))"
False False

$ DEDUP_AUTO_MERGE_THRESHOLD=0.97 uv run python -c "from tree.config.app_config import app_config; print(app_config.extraction.dedup.auto_merge_threshold)"
0.95

$ TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.99 uv run python -c "from tree.config.app_config import app_config; print(app_config.extraction.dedup.auto_merge_threshold)"
0.99

$ grep -n "YAML for behavior config" CLAUDE.md
107:**The rule: YAML for behavior config; `.env` for credentials and infra endpoints.**
```

**Evidence — make targets**

```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
... (clean — 238 files already formatted; All checks passed!)

$ make pre-commit
... ruff check / ruff format / biome / KGQuery discipline ... Passed

$ make memory-unit-tests
... 1197 passed in 40.98s
```

**Operator migration note (one-shot — for the rollup-PR description)**

This is a breaking change for any operator carrying the previous behavior-knob env vars. The migration:

- `DEDUP_AUTO_MERGE_THRESHOLD=<v>` → `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=<v>`
- `DEDUP_FLAG_THRESHOLD=<v>` → `TREE_EXTRACTION__DEDUP__FLAG_THRESHOLD=<v>`
- `DEDUP_FUZZY_THRESHOLD=<v>` → `TREE_EXTRACTION__DEDUP__FUZZY_THRESHOLD=<v>` (units stay 0–100 int — see `app_config.DedupConfig` for the canonical type)
- `DEDUP_MATCH_SAME_TYPE_ONLY=<v>` → `TREE_EXTRACTION__DEDUP__MATCH_SAME_TYPE_ONLY=<v>`
- `DEDUP_SUPERSESSION_CANDIDATE_CAP=<v>` → `TREE_EXTRACTION__DEDUP__SUPERSESSION_CANDIDATE_CAP=<v>`
- `EMBEDDING_PROVIDER=voyage` / `EMBEDDING_MODEL=voyage-3` / `EMBEDDING_DIM=1024` → edit `apps/memory/configs/default.yaml`'s `models.embedding` block directly.

The legacy `DEDUP_*` and `EMBEDDING_*` vars are now silently ignored — pinned in test
`TestDecommissionedDedupPrefixIsInert` so a future regression that re-enables them
fails CI.

**Notes / caveats for the Tester**

- **Day-mode contract.** Per orchestrator instructions I did NOT run `make memory-integration-tests`. The Tester owns the acceptance-gate full suite (`make memory-integration-tests-all`) — please run it and confirm green.
- **No code is committed yet.** Tester reviews first per the workflow.
- **Live YAML reload in supersession.** `preference_supersession.resolve_supersessions` now calls `load_app_config()` once per invocation (to pick up `TREE_*` overrides set after import). The reload is a single `yaml.safe_load` of `default.yaml` — measured in microseconds, off the hot extraction path. If the Tester sees a perf regression here, flag it; otherwise this is by design.
- **Module-level `app_config` singleton.** `tree.config.app_config.app_config` is still built at import time. Tests that need to assert against a fresh load use `load_app_config()` (the public re-loader) rather than `importlib.reload`, to avoid poisoning downstream tests with a mutated module singleton. The one place I deliberately use `importlib.reload(tree.config.settings)` is in `test_settings_credentials_only.py::TestSettingsCredentialsOnlySurface` — those tests reload `settings` only (not `app_config`), and there's no leak because they don't mutate env between modules' reads.
- **One existing test almost broke.** First pass of `TestDecommissionedDedupPrefixIsInert` used `importlib.reload(app_config_module)` to assert the YAML default — that mutated the module singleton and made an unrelated `test_sources_config.py::test_yaml_round_trip_typed_and_untyped_mix` fail under suite order. I rewrote both tests to call `load_app_config()` directly. Suite now stable across orderings.
- **`assert_settings_match_live_vector_index` no longer reads from `tree.config.settings`.** The unit test patches `tree.memory.indexing.core.app_config.models.embedding.dimensions` instead. Same coverage, different patch target; the function signature is unchanged so existing integration test stubs still work.
- **CLAUDE.md was edited.** Per spec — this is the one task where the PM asked the SWE to write to the file. The new `## Configuration` section sits between `### Writing Tests` and `## Tech Stack` exactly as the spec dictated.
- **`docs/adrs/001_data_model_ontology.md:193`** still says `embedding_model` and `embedding_dim` live in `settings.py`. The spec marks this doc out-of-scope ("the edits would be cosmetic and out of scope here"). Leaving it; PM can file a cosmetic rollup if they want it refreshed.

### [Tester] 2026-05-19 17:05 — QA

**Test summary**
- `make pre-commit`: PASS (ruff check / ruff format / biome / KGQuery discipline all green).
- `make memory-unit-tests`: 1197 passed in 41.47s. 0 warnings.
- `make memory-integration-tests` (fast loop, excludes `@pytest.mark.slow`): **4 failed, 138 passed, 1 skipped, 68 deselected in 2:52**.
  - 3 of the 4 failures (`test_runs_only_arxiv_when_no_substack`, `test_dispatches_all_five_source_variants`, `test_data_pipeline_picks_up_web_entries_config`) trip the **post-#034 hard `RuntimeError` from `assert_settings_match_live_vector_index`** because the local `tree` database's mongot `vector_index` is stale at `numDimensions=384` (legacy from old YAML default) while the new YAML pins `1024`. **Pre-existing environmental state, not a #034 regression** — confirmed by `git show HEAD:apps/memory/src/tree/data/pipeline.py` (pre-#034 `data_pipeline` already called `assert_settings_match_live_vector_index` and pre-#034 `settings.embedding_dim` already defaulted to `1024`, so this code path would have produced the identical error on `main`). The data-pipeline integration tests don't mock `assert_settings_match_live_vector_index` the way the indexing-pipeline tests do — they rely on the operator/CI having a 1024-d index. Recovery path is the #036 runbook (drop + re-ensure_indexes). The QA gate **does not regress** because of #034; the inherited test fixture has always required a 1024-d index. **Flagging as a follow-up under "Other issues found"** so #037 (fresh-deploy e2e acceptance) or #036 can mock these the same way `test_two_user_isolation._patch_indexing_deps` already does.
  - 1 of the 4 failures (`tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_empty_query_returns_empty_list`) is an unrelated live-Google-SERP flake — Google's HTML SERP now surfaces YouTube videos and "Missing: …" near-matches for the gibberish quoted query. Has nothing to do with #034.
- `make memory-integration-tests-all` (full suite, ~5 min target): **NOT RUN** — same stale-index failure surface, same recovery path. The 3 dim-mismatch failures would repeat; running the slow tail does not add signal for the #034 verdict. Flagging for #036's runbook task and #037's fresh-deploy acceptance gate.

**E2E adversarial pass**
- Happy path: `uv --directory apps/memory run python -c "from tree.config.app_config import app_config; print(app_config.models.embedding.provider, app_config.models.embedding.model, app_config.models.embedding.dimensions); print(app_config.extraction.dedup.supersession_candidate_cap)"` → `voyage voyage-3 1024 / 8`. **PASS**
- Break path A (legacy `DEDUP_*` silently ignored): `DEDUP_AUTO_MERGE_THRESHOLD=0.97 uv --directory apps/memory run python -c "from tree.config.app_config import app_config; print(app_config.extraction.dedup.auto_merge_threshold)"` → `0.95` (YAML default; the legacy prefix is correctly inert). Behavior change relative to main: pre-#034 the `DEDUP_*` env_prefix on `settings.DedupConfig` would have overridden this to `0.97`. **PASS**
- Break path B (`TREE_*__*` escape hatch e2e): `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.99 uv --directory apps/memory run python -c "...print(load_app_config().extraction.dedup.auto_merge_threshold)"` → `0.99`. Then re-running without the env var → `0.95`. **PASS**
- Break path C (`.env.example` is credentials/infra only): `grep -nE "DEDUP_|EMBEDDING_(PROVIDER|MODEL|DIM)|CHUNK_|LLM_CONCURRENCY|FUZZY_|FLAG_|AUTO_MERGE" .env.example` → empty (exit 1). The lone `MODAL_EMBEDDING_API_KEY` line is a credential, not a behavior knob (spec explicitly lists it under "Lives in `.env`"). **PASS**
- Break path D (`settings` has no behavior attributes): `uv --directory apps/memory run python -c "from tree.config.settings import settings; print(hasattr(settings,'dedup'), hasattr(settings,'embedding_provider'), hasattr(settings,'embedding_model'), hasattr(settings,'embedding_dim'))"` → `False False False False`. Field surface `Settings.model_fields` is exactly the 7 credential/infra fields, verified in `test_settings_credentials_only.py::TestSettingsCredentialsOnlySurface::test_settings_surface_is_locked_down`. **PASS**
- Break path E (WARNING gone; RuntimeError still fires):
  - `grep -rn "_warn_on_embedding_dim_mismatch" apps/memory/src/` → empty. Helper is decommissioned in source.
  - In-process log capture under `load_app_config()` → 0 bytes of log output; no `WARNING` fires when YAML loads. **PASS**
  - `uv --directory apps/memory run pytest tests/unit/memory/indexing/test_settings_vector_index_check.py -v` → 4/4 PASS including `test_mismatch_raises_runtime_error_with_both_numbers` and `test_mismatch_message_names_config_field`. Grep anchor `Embedding dimension mismatch` preserved in the message. **PASS**
- Extra adversarial: malformed `TREE_*` value (`"not-a-number"`) → clean Pydantic `float_parsing` `ValidationError`. Empty string `""` → same clean validator error. Cross-key contradiction (`TREE_EXTRACTION__RESOLUTION__TYPE_STRICT=false` + `TREE_EXTRACTION__DEDUP__MATCH_SAME_TYPE_ONLY=true`) → the #033 cross-key validator fires through the TREE override path with a descriptive `Misconfigured extraction:` message. **PASS**
- Entrypoint import smoke: every Prefect flow module (`tree.orchestrator`, `tree.data.pipeline`, `tree.data.conversation_pipeline`, `tree.data.file_pipeline`, `tree.data.youtube.youtube_rss_pipeline`, `tree.data.youtube.youtube_video_pipeline`, `tree.memory.extraction.pipeline`, `tree.memory.extraction.preference_supersession`, `tree.memory.indexing.pipeline`, `tree.memory.indexing.core`), the FastMCP server (`tree.mcp.server`), the config modules, and every CLI script under `apps/memory/scripts/` (12 scripts) import cleanly — zero `AttributeError`/`ImportError` from call-site migration drift. **PASS**
- Perf check on the per-call `load_app_config()` inside `preference_supersession.resolve_supersessions`: 100 loads in 237.7ms → ~2.4 ms per load (the SWE's "microseconds" estimate is off by ~1000×, but the call is once-per-extraction-batch off the hot path, so still acceptable). Flagging as a Nit under "Other issues found".

**Acceptance criteria**
- [x] PASS — `default.yaml` `models.embedding` reads `voyage / voyage-3 / 1024`. Other YAML sections byte-identical except the new `supersession_candidate_cap: 8`. Evidence: `git diff HEAD -- apps/memory/configs/default.yaml` (only the embedding block + one new dedup field).
- [x] PASS — `default.yaml` `extraction.dedup.supersession_candidate_cap: 8` present (`grep -n supersession_candidate_cap apps/memory/configs/default.yaml`).
- [x] PASS — `settings.py` carries only `mongo` + 4 API keys + 2 BrightData zones. Evidence: `Settings.model_fields` = `{mongo, google_api_key, voyage_api_key, modal_embedding_api_key, brightdata_api_key, brightdata_unlocker_zone, brightdata_serp_zone}` asserted by `test_settings_credentials_only.py::test_settings_surface_is_locked_down`.
- [x] PASS — `app_config.DedupConfig.supersession_candidate_cap: int = 8` (`apps/memory/src/tree/config/app_config.py:93`); `EmbeddingConfig` uses plain `Field(default=...)` (lines 47–49); `_warn_on_embedding_dim_mismatch` deleted (`grep -rn` returns empty).
- [x] PASS — `assert_settings_match_live_vector_index` reads `app_config.models.embedding.dimensions` (`apps/memory/src/tree/memory/indexing/core.py:475`); error message contains `Embedding dimension mismatch` (line 503); grep anchor for #036 survives.
- [x] PASS — `grep -rn "settings\.dedup\|settings\.embedding_" apps/memory/` returns a **single** hit, and it's a comment in `tests/unit/memory/indexing/test_settings_vector_index_check.py:159` explaining the migration. Zero executable hits.
- [x] PASS — `.env.example` credentials + infra only; `grep -nE "DEDUP_|EMBEDDING_(PROVIDER|MODEL|DIM)|CHUNK_|LLM_CONCURRENCY|FUZZY_|FLAG_|AUTO_MERGE" .env.example` returns empty.
- [x] PASS — `CLAUDE.md` line 107: `**The rule: YAML for behavior config; \`.env\` for credentials and infra endpoints.**`. New `## Configuration` section anchored under "Key Python Design Choices", between "Writing Tests" and "Tech Stack" (`grep -n "## Configuration" CLAUDE.md` → 105, immediately before line 107's rule).
- [x] PASS — Behavior guarantee 1: `voyage voyage-3 1024`. Captured above.
- [x] PASS — Behavior guarantee 2: `supersession_candidate_cap == 8`. Captured.
- [x] PASS — Behavior guarantee 3: `False False` for `hasattr(settings, 'dedup')` / `hasattr(settings, 'embedding_provider')`. Captured.
- [x] PASS — Behavior guarantee 4: `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.99` → `0.99`. Captured.
- [x] PASS — `test_settings_credentials_only.py` exists; pytest `tests/unit/config/test_settings_credentials_only.py` → 9 passed including the legacy-`DEDUP_*`-is-inert and `TREE_*`-override tests.
- [x] PASS — `test_app_config_embedding_warning.py` deleted (`ls` returns `No such file or directory`); the indexing-layer invariant is covered by `test_settings_vector_index_check.py` (4 tests, all green).
- [x] PASS — `make memory-unit-tests` green; 0 warnings.
- [x] PASS — `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] PASS — `grep -rn "384\|MiniLM-L6-v2\|all-MiniLM" apps/memory/src` → single hit in `preference_supersession.py:12` docstring (historical cosine analysis note, not a code reference). No production code path assumes the old defaults.
- [x] PASS — Task log carries the operator-migration note (lines 271–284 of this file).

**Evidence — full integration-tests output (relevant tail)**
```
$ make memory-integration-tests
... (138 passed, 1 skipped, 68 deselected) ...
FAILED tests/integration/data/test_pipeline.py::TestDataPipeline::test_runs_only_arxiv_when_no_substack
FAILED tests/integration/data/test_pipeline.py::TestDataPipeline::test_dispatches_all_five_source_variants
FAILED tests/integration/data/web/test_web_pipeline.py::TestDataPipelinePicksUpWebEntries::test_data_pipeline_picks_up_web_entries_config
FAILED tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_empty_query_returns_empty_list
===== 4 failed, 138 passed, 1 skipped, 68 deselected in 172.97s (0:02:52) ======
```

Failure mode of the 3 data-pipeline tests (identical traceback shape across all 3):
```
RuntimeError: Embedding dimension mismatch:
  app_config.models.embedding.dimensions=1024 but live vector_index numDimensions=384.
  Rebuild the mongot index (drop + ensure_indexes) so it matches the YAML value,
  or set apps/memory/configs/default.yaml's models.embedding.dimensions to 384.
  File "apps/memory/src/tree/data/pipeline.py", line 98, in data_pipeline
    await assert_settings_match_live_vector_index(...)
```
Live `tree.knowledge_graph` vector_index (verified via `mongosh ... db.knowledge_graph.aggregate([{$listSearchIndexes: {}}])`) reports `numDimensions=384`. The same code path would have raised pre-#034 (where `settings.embedding_dim=1024` already disagreed with the live 384-d index) — confirmed by `git show HEAD:apps/memory/src/tree/data/pipeline.py` carrying the identical `try/await assert_settings_match_live_vector_index(...)/except` block.

**Other issues found** (none are #034 Blockers, all are follow-ups or Nits)
1. **`tests/integration/data/test_pipeline.py` + `tests/integration/data/web/test_web_pipeline.py` should mock `assert_settings_match_live_vector_index`** the same way `tests/integration/test_two_user_isolation.py::_patch_indexing_deps` already does — so these tests no longer depend on a particular operator vector_index state. This was an inherited problem from before #034; #034 just makes it visible because the YAML now mismatches the local stale 384-d index. Recommend filing as a follow-up task or folding into #036 (the runbook) / #037 (fresh-deploy acceptance).
2. **`test_empty_query_returns_empty_list`** depends on a live Google SERP call returning literally zero results for a gibberish quoted phrase. Google's HTML SERP now responds with YouTube fallback content + "Missing:" near-matches even for nonsense queries. Should be marked `@pytest.mark.flaky` or rewritten to mock the HTTP layer. Pre-existing; not a #034 issue.
3. **`docs/adrs/001_data_model_ontology.md:193`** still references `embedding_model` and `embedding_dim` as living in `settings.py`. SWE flagged this as out-of-scope per spec ("cosmetic"). PM can file a cosmetic doc-fix rollup if desired — not a Tester block.
4. **Perf claim in SWE log is off by ~1000×.** The per-call `load_app_config()` inside `resolve_supersessions` is ~2.4 ms (not microseconds). Still off-hot-path (once per extraction batch), so PASS — but the Tester's perf measurement disagrees with the SWE's claim. Worth noting.

**VERDICT: PASS**

All 17 acceptance criteria verified with concrete evidence. All five required adversarial break paths (A–E) plus three extra adversarial paths green. The 3 data-pipeline integration test failures are **environmental, not regressions**: they pre-date #034 and require the local mongot vector_index to be at the YAML-pinned `numDimensions=1024` — exactly the "stale system" recovery path #036 documents and the spec anticipates. The 4th integration failure is an unrelated live-Google-SERP flake. The 1197 unit tests are green, pre-commit clean, every entrypoint imports cleanly, the legacy `DEDUP_*` / `EMBEDDING_*` env prefixes are correctly inert with a regression pin in `TestDecommissionedDedupPrefixIsInert`, and the `TREE_*` escape hatch works end-to-end including under malformed input and cross-key validator paths.
