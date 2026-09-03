---
name: run-pipelines-e2e
description: "Run the memory pipelines end-to-end (serve workflows, then data → memory → indexing → query) for real-pipeline verification. Use when a feature is done and needs e2e verification before a PR, or when asked to run a pipeline."
---

# Running pipelines & E2E

By default, use the "Paul Iusztin" user when testing.

1. **Serve the workflows** in the background to pick up the latest code: `make memory-serve-workflows &`. This process is the in-process Prefect worker — without it, deployments register but nothing executes. If a serve process is already running, kill it first and re-serve.
2. **Run a pipeline** via its Make command (which streams logs to the terminal — use these instead of `prefect deployment run` directly so errors surface here). Each pipeline target takes `MODE=offline` (default) or `MODE=online`: step-by-step is `make memory-run-data-pipeline` → `make memory-run-memory-pipeline` → `make memory-run-indexing-pipeline` → `make memory-query-graph QUERY="test query"` → verify results; or end-to-end in one run with `make memory-run-pipeline` (offline batch) / `make memory-run-pipeline MODE=online SOURCE="<url|path>"` (one realtime source).
