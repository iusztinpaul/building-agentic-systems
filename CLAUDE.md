# The Why

**Tree: Your Rooted Personal Assistant.** Build a personal assistant rooted in a knowledge-graph memory, powered by ontologies, LLMs, and agents.

# The What 

## Key Components

- **Data Pipeline:** ETL pipelines gathering data from multiple sourcing and normalizing everything into the `documents` collection. One ETL pipeline per source. Sources:
    - Substack RSS feeds (e.g., https://www.decodingai.com/feed)
    - Substack articles
    - YouTube RSS feeds
    - YouTube videos
    - Custom sites
    - Markdown files
    - HuggingFace Datasets (e.g., https://huggingface.co/datasets/arxiv-community/arxiv_dataset)
- **Memory Pipeline:** Pipeline that maps `documents` to `knowledge graph objects` within the `knowledge_graph` collection by cleaning, chunking, graph extracting, normalizing and upserting nodes and edges directly.
- **The Unified Memory:** The agent's unified memory powered by MongoDB that leverages text, semantic and graph search. The data is stored in a single mutable `knowledge_graph` collection with upsert semantics. Nodes use `_id = "type:name"` and edges use `_id = "source|type|target"` string identifiers.
- **Agentic Tools:** Tools used to query or write to the unified memory. 
- **Configuration:** 
  - For the memory app, most of the configuration is done via the `apps/memory/src/tree/config/app_config.py` which loads YAML files from `apps/memory/src/tree/config`, while we intentionally keep the `.env.example` file thin scoped only to credentials or other env vars coupled with the credentials (e.g., URIs, availability zones, etc.)

## Project Structure

This is a monorepo. Each app lives under `apps/` and owns its own build files (`pyproject.toml`, `Makefile`, etc.). Only cross-app concerns live at the repo root.

```
project-root/
├── apps/
│   ├── memory/                          # Python app: ETL + knowledge graph + MCP server
│   │   ├── src/
│   │   │   └── tree/                    # Core Python module
│   │   │       ├── config/              # Configuration
│   │   │       ├── entities/            # Key data structures as ODMs
│   │   │       ├── db.py                # Database connection helpers
│   │   │       ├── orchestrator.py      # Orchestrator integration
│   │   │       ├── data/                # Data ETLs
│   │   │       │   ├── core/            # Core module business logic
│   │   │       │   ├── types.py         # Types used across the data layer
│   │   │       │   └── ...              # One .py file per ETL served via Prefect
│   │   │       └── memory/              # Unified memory module
│   │   │           ├── types.py
│   │   │           ├── extraction/      # Chunking, graph extraction, embedding
│   │   │           ├── indexing/        # Post-extraction indexing (reverse edges, embeddings, search indexes)
│   │   │           └── query/           # Query interfaces over unified memory
│   │   ├── deploy/                      # Cloud deployment scripts (Modal)
│   │   ├── configs/                     # App YAML configs (default.yaml)
│   │   ├── scripts/                     # Entrypoints (serve_mcp.py, run_*.py, ...)
│   │   ├── tests/                       # unit/ + integration/
│   │   ├── docker/Dockerfile            # Memory-app image (Prefect worker)
│   │   ├── pyproject.toml
│   │   ├── uv.lock
│   │   ├── .python-version
│   │   └── Makefile                     # Memory-app targets
│   └── harness/                         # Future TS/Ink/Bun coding-agent harness — see docs/harness-plan.md
├── docker/                              # SHARED infra (MongoDB + mongot config files)
├── docs/                                # Architecture & design docs (incl. harness-plan.md)
├── models/                              # Shared model interfaces (LLM/embedding abstractions)
├── .env / .env.example                  # Shared secrets (Mongo creds, API keys)
├── .mcp.json                            # MCP servers the agents/harness spawn
├── docker-compose.yml                   # Shared infra orchestration
└── Makefile                             # Thin root: delegates to apps/*/Makefile; shared infra targets
```

### Make command convention

- `make memory-<target>` — run `<target>` inside `apps/memory/` (e.g. `make memory-unit-tests`, `make memory-serve-mcp`).
- `make harness-<target>` — reserved for the future TS harness at `apps/harness/`.
- `make local-start` / `make local-stop` / `make local-restart` — shared Docker infra.
- `make tests` — aggregate: runs all apps' tests.
- `make pre-commit` — pre-commit across the repo.
- `make help` — list all root targets.

## Key Python Design Choices

- We are using Python with async patterns.
- Loose clean architecture design decoupling infrastructure, serving, app and domain logic:
    - The `entities` folder defines shared ODM, enums or other data structures data are used all over the project. While we have local `types.py` files per app module to define data types that will be used only within that current module or layers upwards. 
    - Infrastructure exceptions we don't plan to change: MongoDB, Prefect, Opik. Thus, it doesn't make sense to make them modular. 
    - Flat structure and naming based on actionability rather than dogmatic clean architecture.
- Properties of pipelines:
    - Idempotency
    - Retries
    - Checkpointing
- All the dates are timezone aware (UTC by default). We don't accept any naive datetime objects.
- Always add types to function or method parameters and return types. Even if they return `None`.

### Writing Scripts

- Memory-app scripts (entry points in `apps/memory/scripts/`) must call `init_logger()` from `tree.logging` at module level to configure logging.

### Writing Tests

- Structure the tests following a one-on-one relationship with the core python module.
- When writing tests respect:
    - **Naming**: Files must be `test_*.py`; functions must be `test_*`.
    - **Pattern**: Use AAA (Arrange, Act, Assert).
    - **Fixtures**: Use `conftest.py` for shared logic; avoid manual setup/teardown methods.
    - **Mocking**: Use `pytest-mock` (the `mocker` fixture) to isolate unit tests from your MongoDB.
    - **Parametrize**: Use `@pytest.mark.parametrize` to test multiple inputs (e.g., different sensor values) in a single function.
- Call the `testing-python` SKILL for step-by-step details
- Fix any `warnings`. Rerun the tests until we have 0 warnings.
- What to **AVOID**:
  - Writing unit tests for Prefect, Modal, Opik or other infra components. They represent our infrastructure layer, 
which is tested only via integration tests.

## Configuration

**The rule: YAML for behavior config; `.env` for credentials and infra endpoints.**

- **`apps/memory/configs/default.yaml`** is the single source of truth for behavior knobs: model names + dimensions, chunk sizes, LLM concurrency, resolution/dedup thresholds, query/MCP tuning, and the `sources:` list. Every value here has a typed Pydantic model in `apps/memory/src/tree/config/app_config.py`.
- **`.env` (driven by `apps/memory/src/tree/config/settings.py`)** is reserved for credentials (API keys) and per-environment infrastructure endpoints (Mongo host/port, Prefect URL, BrightData zones). It reads like a wallet — no behavior knobs, no commented-out tuning parameters.

**Where to put new things.** A new tunable behavior knob goes in `default.yaml` and `app_config.py`. Do NOT add it to `.env.example` or `settings.py`. A new credential or infra endpoint goes in `.env.example` and `settings.py`.

**Escape hatch.** Operators may override any YAML key via `TREE_<SECTION>__<KEY>` env vars — for example `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.99`. The mechanism is `_apply_env_overrides` in `app_config.py`. This is for emergency one-shot ops use; new knobs should not be documented in `.env.example`.

**Diagnosis tip.** If `make memory-serve-workflows` logs an embedding-dimension-mismatch error, the YAML is the source of truth — fix `apps/memory/configs/default.yaml`'s `models.search_embedding.dimensions` (and rebuild the mongot vector index if needed), do not add an env override. The persisted vectors come from the **search** embedding (#039); the transient `models.resolution_embedding` is not index-coupled.

### macOS torch / TMPDIR shim

`apps/memory/Makefile` exports `TMPDIR := $(shell getconf DARWIN_USER_TEMP_DIR)` on Darwin so every `make memory-*` target inherits a sub-104-byte tmpdir. Background: macOS `sockaddr_un.sun_path` is only 104 bytes, and torch's `torch_shm_manager` constructs `<TMPDIR>/torch_<pid>_<rand>/manager.sock`; long inherited `TMPDIR`s (some agent shells inherit an ~81-char `com.apple.shortcuts.mac-helper` path) overflow that buffer and SIGABRT the helper, surfacing as `RuntimeError: no response from torch_shm_manager`. If you run memory-app scripts **outside** `make` (e.g. directly via `uv run ...`), set `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)` manually. The regression sentinel is `apps/memory/tests/integration/test_torch_shared_memory.py`; see `tracker/done/035-pin-torch-version-py314-arm64.md` for the full diagnostic.

## Tech Stack

### Core
- **Data validation and structuring:** Pydantic
- **ODM:** Beanie + PyMongo Async driver
- **MCP Server Framework:** FastMCP
- **Testing:** Pytest
- **CLI:** Click
- **Logging:** Native Python logger (never prints!)

- **Embedding Model Definition:** Sentence Transformers

### Services
- **LLM API:** Gemini
- **Embedding Models API:** Voyage AI
  - [Documentation Multimodal Embeddings](https://docs.voyageai.com/docs/multimodal-embeddings)
  - [API Reference Multimodal Embeddings](https://docs.voyageai.com/reference/multimodal-embeddings-api)
- **Searching, crawling, scraping:** Bright Data

### Infrastructure
- **Unified memory and database:** MongoDB
- **Serving AI Models**: Modal
- **Observability and evals:** Opik
- **Containerization:** Docker
- **CI/CD:** GitHub Actions

### Orchestrator and Durable Workflows

- Tool: Prefect
- Sitemap: https://docs.prefect.io/llms.txt
- You can access deployments via `uv run prefect deployment ...` CLI commands. For example, to run a deployment served 
in @apps/memory/src/tree/orchestrator.py you can run `prefect deployment run [DEPLOYMENT_NAME]` (invoked from within `apps/memory/`).

### Access Documentation 

Use the `context7` MCP server to find out more about the tech stack usage and good practices.

# The How 

We manage all the core commands through GNU Make as our command center. File available at @Makefile. Run all the commands with `make ...`

We use `uv` to manage our Python project such as the virtual environment(s), dependencies, and overall package the project.

## Populating the Users Collection

By default, you will use the "Paul Iusztin" user when testing.

## Developing New Features and Bug Fixes Workflow

Use Squid's `/night` and `/day` skills to do any changes to the codebase.

## Step-by-Step Verification Steps

During development, run these steps after every atomic change or before commiting anything to git:

 1. Format and lint: `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check`
 2. Pre-commit: `make pre-commit`
 3. Unit tests: `make memory-unit-tests`
 3b. Fast integration tests (optional but recommended; <2 min): `make memory-integration-tests` — excludes `@pytest.mark.slow`.

When the feature is considered done and ready for PR, ALWAYS run:

 4. Full integration tests: `make memory-integration-tests-all` (~5 min; includes `@pytest.mark.slow`). CI runs this same target.
 5. Run and verify the code end-to-end. For example, when testing the memory run: `make memory-serve-workflows & `→ `make memory-run-data-pipeline` → `make memory-run-memory-pipeline-extraction` → `make memory-run-memory-pipeline-indexing` → `make memory-query-graph QUERY="test query"` → verify results. Always adapt this e2e example based on the modifications you've made. If necessary you should run multiple tests covering all the modifications you've made in the feature PR you are working on.

Slow tests are marked `@pytest.mark.slow`; `grep -rn "pytest.mark.slow" apps/memory/tests/` shows what's excluded from the fast loop. Use `make memory-integration-tests-slow` to run only the slow tail (useful when iterating on a vector-index or full-Prefect-e2e change).

## Build

```
make memory-build
```

## Running QA and Tests

We use `ruff` as our formatter and linter.

First always fix the formatting and linting errors with the fix commands:
```
make memory-format-fix
make memory-lint-fix
```

Then, check if there are any errors that couldn't be fixed automatically and fix them:
```
make memory-format-check
make memory-lint-check
make pre-commit
```

Run unit tests frequently during development:
```
make memory-unit-tests
```

Run the **fast integration loop** (excludes `@pytest.mark.slow`, target <2 min) between iterations:
```
make memory-integration-tests
```

When the feature is done and ready for PR, run the **full integration suite** (~5 min; CI runs this same target):
```
make memory-integration-tests-all
```

Run only the slow integration tail (useful when iterating on a vector-index or full-Prefect-e2e change):
```
make memory-integration-tests-slow
```

Mirror the CI integration command locally (skip mongot-dependent tests; runs sequentially because the shared-DB cleanup fixture makes parallel `-n auto` workers collide):
```
make memory-integration-tests-ci
```

Or run all tests together (aggregate across apps):
```
make tests
```

### Test-marker hierarchy

Two **orthogonal** pytest markers gate test selection:

- `@pytest.mark.slow` — tests that take >3s or require vector-index convergence / full Prefect e2e. Excluded from the fast inner loop (`make memory-integration-tests`); included in `make memory-integration-tests-all` and the local CI mirror.
- `@pytest.mark.requires_mongot` — tests that need a working Atlas Search / mongot service (live `$vectorSearch`, `create_search_index`, or the `_skip_without_mongot` fixture). **Excluded from CI** because mongot's Search Index Management gRPC channel is unreliable on GitHub runners (CI run 25989844295: 16 connectivity errors + 7 five-minute hangs). Included in every local target, where the full `docker-compose.yml` stack brings mongot up. A test can be `slow` without needing mongot, or vice versa.

CI runs `pytest tests/integration -m "not requires_mongot" --timeout=300`. The Tester's acceptance-gate target `make memory-integration-tests-all` runs everything, including mongot — so before signing off on a feature, run it locally with the full stack up.

`pytest-xdist` is installed in dev deps but **not enabled in CI**: the autouse `_clean_collections` fixture in `tests/integration/conftest.py` wipes every collection between tests, so parallel workers race against each other. If we ever need parallelization we'd need per-worker test DB names (`PYTEST_XDIST_WORKER` suffix).

## Running Pipelines

To test a pipeline after making changes:

1. **Serve the workflows** in a background process to pick up the latest code:
```
make memory-serve-workflows &
```
If a serve process is already running, kill it first and re-serve to pick up the latest code changes.

2. **Run the pipeline** via the corresponding Make command (which streams logs to the terminal), such as:
```
make memory-run-data-pipeline
make memory-run-memory-pipeline-extraction
make memory-run-memory-pipeline-indexing
```

The `make memory-serve-workflows` process must be running for pipeline triggers to be picked up, as it acts as the in-process Prefect worker. Without it, deployments are registered but no worker will execute them.

Always use these Make commands instead of `prefect deployment run` directly, as the scripts stream all logs (including errors) back to the current process so you can debug without checking the Prefect UI.

### Phase 1 migration (one-shot)

When upgrading an existing pre-multi-tenancy deployment to the Phase-1
schema, run the one-shot migration script before any other pipeline. It
seeds a `User`, backfills `user_id` onto every existing `Document`, drops
the `knowledge_graph` collection (which extraction will rebuild), and
triggers the extraction + indexing pipelines for the seed user.

1. **Always dry-run first** to inspect the plan with counts. No writes:
```
make memory-migrate-multi-tenancy USER_IDENTIFIER=dev@example.com DRY_RUN=1
```
This prints "would create user X; would backfill N documents; would drop M KG rows".

2. **Apply** (writes; idempotent — safe to re-run):
```
make memory-migrate-multi-tenancy USER_IDENTIFIER=dev@example.com NAME="Dev User"
```

3. **Verify** in `mongosh`:
```
mongosh "mongodb://tree:tree@localhost:27017/tree?authSource=admin&directConnection=true"
> db.users.find({identifier: "dev@example.com"})
> db.documents.countDocuments({user_id: ObjectId("<seed_user_id>")})
> db.knowledge_graph.find({_id: /:person:self$/})
```

Notes:
- The script needs `make memory-serve-workflows &` running to trigger
  the Prefect deployments at step 5. If the worker isn't up, pass
  `NO_TRIGGER_PIPELINES=1` and re-run the pipelines manually via
  `make memory-run-memory-pipeline-extraction USER_ID=...` and
  `make memory-run-memory-pipeline-indexing USER_ID=...`.
- **Step 4.5 (post-#023):** the migration now calls `ensure_indexes`
  inline immediately after re-creating the `person:self` node. The
  freshly dropped `knowledge_graph` collection therefore ships with its
  text / vector / `user_id`-prefixed compound indexes already in place
  when the script returns, rather than waiting on the fire-and-forget
  indexing deployment in step 5. Idempotent: step 5's indexing run
  re-issues the same `ensure_indexes` call.
- The script ABORTS if `documents` already carries `user_id` values
  from a different tenant (this script is a one-shot bootstrap, not a
  multi-tenant rebalance).
- Re-running with the same `--identifier` is a no-op (the seed user
  is re-used, the `update_many` writes no new values, and the
  self-person `$setOnInsert` upsert leaves the existing row alone).

### Phase 2-5 reset-ontology migration (POLE+O)

When upgrading an already-Phase-1 deployment to the Phase-2-through-5
POLE+O ontology (registry + collapsed `related_to` + `fact` island +
preference typed slots + bi-temporal supersession + audit
collections), run the migration with the `RESET_ONTOLOGY=1` knob. It
wipes `knowledge_graph` and the two Phase-3 audit collections, re-runs
the User `after_insert` hook to recreate `person:self` under the new
shape (`subtype="individual"`), ensures the new compound indexes (the
`(user_id, type, semantic_type)` partial index from #029) inline, and
triggers extraction + indexing under the new ontology.

1. **Always dry-run first** to inspect the plan with counts. No writes:
```
make memory-migrate-multi-tenancy USER_IDENTIFIER=dev@example.com RESET_ONTOLOGY=1 DRY_RUN=1
```
This prints "would drop knowledge_graph (N rows); would drop
extraction_rejections (M rows); would drop extraction_dropped_fields
(P rows); would re-create person:self for dev@example.com; would
trigger extraction + indexing."

2. **Apply** (writes; idempotent — safe to re-run):
```
make memory-migrate-multi-tenancy USER_IDENTIFIER=dev@example.com RESET_ONTOLOGY=1
```

3. **Verify** in `mongosh`:
```
mongosh "mongodb://tree:tree@localhost:27017/tree?authSource=admin&directConnection=true"
> db.knowledge_graph.find({_id: /:person:self$/})
> db.knowledge_graph.find({type: "organization"}).limit(3)
> db.knowledge_graph.find({type: "related_to", semantic_type: "employed_by"}).limit(3)
> db.knowledge_graph.find({type: "fact"}).limit(3)
> db.extraction_rejections.countDocuments({})
> db.extraction_dropped_fields.countDocuments({})
```

Notes:
- **Aborts** if the seed `User(identifier=...)` does not exist. This
  path assumes the Phase-1 bootstrap migration has already run. If you
  are bootstrapping a brand-new deployment, run the migration WITHOUT
  `RESET_ONTOLOGY=1` first, then re-run it with `RESET_ONTOLOGY=1` if
  you need to wipe-and-rebuild.
- The `documents` collection is **not** touched. Re-extraction reads
  the same per-tenant documents and rebuilds the graph against the new
  POLE+O ontology.
- Same Prefect-worker contract as Phase 1: requires
  `make memory-serve-workflows &` running, or pass
  `NO_TRIGGER_PIPELINES=1` to skip the deployment trigger.
- **Idempotent.** Re-running with the same `--identifier` is safe: the
  second run drops an already-empty `knowledge_graph`, re-upserts the
  same `person:self` (`$setOnInsert`), and re-triggers the pipelines
  (themselves idempotent — same chunk hashes → same emissions).
- **Multi-tenant note:** this drops every tenant's KG rows. Trigger per-tenant extraction afterwards for any other tenant whose data you want rebuilt.

### Voyage vector-index rebuild (one-shot, when adopting the voyage YAML default)

Use this runbook when the data or memory pipeline boots and immediately
raises an **Embedding dimension mismatch** error such as:

```
RuntimeError: Embedding dimension mismatch: app_config.models.search_embedding.dimensions=1024 but live vector_index numDimensions=384. Rebuild the mongot index (drop + ensure_indexes) so it matches the YAML value, or set apps/memory/configs/default.yaml's models.search_embedding.dimensions to 384.
```

This happens on any deployment that ran the pipeline at least once under
the previous `sentence-transformers` / `MiniLM-L6-v2` / 384-d YAML defaults
and is now pulling the post-#034 / post-#038 voyage / 1024-d defaults.
The check is deliberate — `assert_settings_match_live_vector_index` in
`apps/memory/src/tree/memory/indexing/core.py` exists precisely to prevent
silent dim-drift corruption (a `$vectorSearch` against a stale-dim index
silently returns garbage instead of raising). The literal anchor string
`Embedding dimension mismatch` is preserved verbatim across releases so
this runbook is grep-discoverable from the error text alone.

> [!CAUTION]
> **Vector-space change: voyage-3 → voyage-multimodal-3 is a SILENT
> CORRUPTION RISK.** #038 switched the project default from `voyage-3`
> (text endpoint, 1024-d) to `voyage-multimodal-3` (multimodal endpoint,
> 1024-d). The dimension is identical, so the
> `assert_settings_match_live_vector_index` boot check **will NOT
> catch this** — but the two models produce embeddings in different
> semantic spaces. If your live `knowledge_graph` carries voyage-3
> vectors and you adopt this branch, every `$vectorSearch` query
> compares a voyage-multimodal-3 query vector against voyage-3
> document vectors and returns wrong-but-superficially-plausible
> results. **You must re-extract:** drop the live `vector_index`, run
> the indexing pipeline to recreate it at the new (still 1024-d)
> shape, then **re-trigger extraction** so every node's `embedding`
> field is re-computed under `voyage-multimodal-3`. The cleanest path
> is the Phase-2-5 `RESET_ONTOLOGY=1` migration above, which drops
> `knowledge_graph` entirely and re-extracts. Skipping this step
> leaves the database in a state where text + graph search work and
> semantic search returns garbage with zero error signals.

> [!WARNING]
> The rebuild **wipes** the live `vector_index` on
> `db.knowledge_graph`. Until mongot re-converges (~30-90s after the
> indexing pipeline recreates the index), every `$vectorSearch` query
> returns empty — the agent's memory queries degrade to text + graph
> only during that window. Schedule the rebuild accordingly.

**Recipe (two commands; idempotent on a healthy 1024-d index — the drop
is the only destructive step, `ensure_indexes` no-ops when the live shape
already matches):**

1. **Drop the stale vector index** via `mongosh`:
```bash
mongosh "mongodb://tree:tree@localhost:27017/tree?authSource=admin&directConnection=true" \
  --eval 'db.knowledge_graph.dropSearchIndex("vector_index")'
```

2. **Re-trigger the indexing pipeline**, which calls `ensure_indexes` and
   recreates the index at the YAML-declared `models.search_embedding.dimensions`
   (1024 for `voyage-3` / `voyage-multimodal-3`):
```bash
make memory-serve-workflows &
make memory-run-memory-pipeline-indexing USER_ID=<oid>
```

3. **Verify** in `mongosh` that the new index is `READY` at the new dim:
```bash
mongosh "mongodb://tree:tree@localhost:27017/tree?authSource=admin&directConnection=true" \
  --eval 'db.knowledge_graph.aggregate([{$listSearchIndexes: {name: "vector_index"}}])'
```
Expected output shows `numDimensions: 1024` and `status: READY`. The
**convergence window** is typically 30-90s after `ensure_indexes` returns
(same window documented for fresh-deploy index creation); until then
`$vectorSearch` is empty. Re-running the data pipeline at this point
should no longer raise the dim-mismatch error — the boot check returns
`None`.

Notes:
- **Row-level embeddings are still stale.** `ensure_indexes` only
  replaces the mongot index definition; it does not touch the
  `embedding` field on existing `knowledge_graph` rows. Those vectors
  were produced by the old 384-d model and are now both wrong-shape AND
  wrong-semantic-space. The indexing pipeline's `embed_unembedded_nodes`
  step backfills only nodes whose `embedding` field is missing — it
  will NOT re-embed rows that already carry a (stale 384-d) vector. For
  a full refresh, run the reset-ontology migration above (which drops
  `knowledge_graph` entirely and triggers re-extraction), OR re-run
  extraction explicitly:
```bash
make memory-run-memory-pipeline-extraction USER_ID=<oid>
make memory-run-memory-pipeline-indexing USER_ID=<oid>
```
- **Idempotent.** Re-running the recipe on an already-1024-d index
  drops + recreates the same shape; `ensure_indexes` is a no-op when
  configuration matches.
- **Fresh deploys never hit this path.** On a clean machine,
  `make memory-run-memory-pipeline-indexing` is the first thing that
  creates `vector_index` and it creates it at the current YAML dim
  directly. This runbook is only relevant for upgrades over an existing
  mongot dataset.
- **Do not edit `default.yaml` back to 384.** The error message offers
  that as a fallback for emergencies; the supported path is to rebuild
  the index at the current YAML value. See the `## Configuration`
  section above — the YAML is the source of truth for embedding dim.

## Running Custom Commands for Project Level Dependencies

Use `uv` to run any custom command that is not present in the @Makefile or @apps/memory/Makefile, but uses Python or other dependency installed through uv, usually available in @apps/memory/pyproject.toml.

Run them from the repo root with `uv --directory apps/memory run ...`, or from `apps/memory/` with `uv run ...`. Examples:
- `uv --directory apps/memory run python ...`
- `uv --directory apps/memory run prefect ...`
- `uv --directory apps/memory run modal ...`

## Running Custom Commands for Accessing Infrastructure and External Services 

Always use the following CLIs installed directly on the system:

- MongoDB: `mongosh` CLI for CRUD operations and monitoring on the local MongoDB instance.
- GitHub: `gh` CLI to interact with the remote GitHub repository this project is attached to (e.g., accessing PRs, issues or GitHub Actions)
- Git: `git` CLI for generic Git operations.

## Self Improve

Run the `self-improve` SKILL to analyze corrections from the session and persist lessons learned to CLAUDE.md files or memory.
