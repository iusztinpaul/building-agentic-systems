---
name: run-pipelines-e2e
description: "Run the memory pipelines end-to-end (serve workflows, then data → memory → indexing → query) for real-pipeline verification. Use when a feature is done and needs e2e verification before a PR, or when asked to run a pipeline."
---

# Running pipelines & E2E

By default, use the "Paul Iusztin" user when testing.

0. **Pick the memory mode.** `memory.mode` (ADR-006) decides how much of the pipeline runs: `rag` writes `document` + parent/child `chunk` rows only, `graphrag` (the YAML default) adds structural edges and LLM-extracted entities. Override per shell with `TREE_MEMORY__MODE=rag` and pass it to **every** command of the run — serve, pipeline, query, MCP — or the flow and the reader disagree about what is in the collection.

   **Switching modes means dropping the collection first** — both modes start from scratch and there is no migration, so rows left over from the other mode make every count meaningless:

   ```bash
   mongosh "mongodb://$MONGO_INITDB_ROOT_USERNAME:$MONGO_INITDB_ROOT_PASSWORD@localhost:$MONGO_PORT/?directConnection=true" \
     --quiet --eval 'db.getSiblingDB("tree").memory.drop()'
   ```

   Verifying a change in BOTH modes = run steps 1–3 twice, dropping `memory` in between.

1. **Serve the workflows** in the background to pick up the latest code: `make memory-serve-workflows &`. This process is the in-process Prefect worker — without it, deployments register but nothing executes. If a serve process is already running, kill it first and re-serve.

   **IMPORTANT — worktrees and feature branches.** The Dockerized `prefect-worker` (started by `make local-start`) executes the **MAIN checkout baked into its image**, not your working tree. Dispatching a run while only that worker is up silently runs OLD code — the pipeline "passes" and proves nothing about your branch. When verifying a feature branch or a git worktree, run `make memory-serve-workflows` **from that worktree** (and stop it when done) so the run executes the code you changed. Same rule for the mode: export `TREE_MEMORY__MODE` in the shell that serves, because the flow reads it at flow entry inside the serving process.

2. **Run a pipeline** via its Make command (which streams logs to the terminal — use these instead of `prefect deployment run` directly so errors surface here). Each pipeline target takes `MODE=offline` (default) or `MODE=online`: step-by-step is `make memory-run-data-pipeline` → `make memory-run-memory-pipeline` → `make memory-run-indexing-pipeline`; or end-to-end in one run with `make memory-run-pipeline` (offline batch) / `make memory-run-pipeline MODE=online SOURCE="<url|path>"` (one realtime source).

   Every offline target is the SAME `offline-pipeline` deployment with a different set of **Offline phase**s on (`run_data` / `run_extraction` / `run_indexing` / `run_clustering`) — including `make memory-run-indexing-pipeline`, which since ADR-007 dispatches instead of running the flow in-process. So step 1's serve process is required for all of them, and the Prefect UI shows one flow run whose children are exactly the phases you asked for.

   **Clustering (optional, after indexing).** `make memory-run-clustering-pipeline` runs the fourth phase alone — it needs embeddings, so run it after indexing, and it is off everywhere else including the nightly cron. Two gotchas that look like failures and are not: the first run on a fresh env spends ~40 s compiling umap's numba kernels before it clusters anything, and a corpus below `min_cluster_size` (default 15) is skipped with a log line naming the knob — for a small local corpus export `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5` in the SERVING shell (the flow reads it inside the serve process, not in yours).

3. **Verify the result.** Count what landed with `mongosh` over the `memory` collection, grouped by `kind` / `type` / `subtype` (in `rag`: `edge` count 0, parents with `embedding: []`, children with a 1024-length vector), then read it back:

   ```bash
   make memory-query-graph QUERY="test query"
   ```

   The output follows the mode: `graphrag` writes and opens an interactive HTML graph under `.tree/graphs/<slug>-<UTC-stamp>.html`; **`rag` prints the ranked parent chunks as TEXT** (score, document title, heading path, a 300-char excerpt, matched-children count) and writes no file — a missing HTML file in `rag` is the expected outcome, not a failure. `make memory-query-graph` with no `QUERY` is graphrag-only (in `rag` it exits 1: there are no edges to draw).

   If you clustered, check the **Embedding map** too — the one visual surface that works in BOTH modes:

   ```bash
   make memory-visualize-embeddings HULLS=true
   ```

   It writes `.tree/graphs/embedding-map-<UTC-stamp>.html` and opens it; the legend shows one row per cluster. Two expected outcomes, not failures: with no clustering run it prints `No clustering run found for this user …` and exits 1, and after ingesting anything since the last run the FIRST output line is `N of M chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline` (those points are off the map and counted in the legend). Ingest one document and re-run to see that stale warning appear — then re-cluster to clear it.

   For the MCP surface, serve it in the same mode (`TREE_MEMORY__MODE=rag make memory-serve-mcp TRANSPORT=streamable-http`) and call the tools with `uv run fastmcp call http://127.0.0.1:8000/mcp --auth none <tool> …`, e.g. `… visualize_memory_embeddings hulls=true` — it must carry the same file path, warning line and no-run message as the CLI. `rag` registers 7 tools, `graphrag` 14.

4. **Clean up.** Stop the serve process and any MCP server you started, and remove stray artefacts (`.tree/graphs/*.html`) so the worktree stays clean.
