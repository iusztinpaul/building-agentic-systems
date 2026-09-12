---
id: 132-prefect-run-log-for-memory-module-loggers
status: pending
feature: mcp-tool-contracts-followups
---

# Forward `tree.*` module logs into the Prefect run log so readiness and vector-leg lines reach the operator

Tags: `infra`, `memory`, `rag`, `observability`
Depends on: None
Blocks: —
Implements: ADR-008 — Consequence "degraded is visible, not broken" (the operator must be able to SEE it)

## Scope

`tree/memory/rag/indexing.py` (vector-index readiness: `Waiting for vector search index …`,
`… treating it as ready`, `… not queryable after 300 s …`), `tree/memory/rag/search.py`
(`vector leg: N candidate(s), M kept at min_vector_score=…`, `Vector search leg unavailable …`)
and `tree/offline.py::_resolve_source_uris` log through plain module loggers. Prefect only
forwards `prefect.*` loggers (and `get_run_logger()`) to the API, so none of those lines
appear in `make memory-run-*-pipeline`'s streamed output or the Prefect UI — an operator
retrying from a receipt or waiting on an index sees nothing (task 129's SWE hit this and
captured the readiness line from the MCP bootstrap path instead).

Bias-to-least: Prefect's own switch, not a logger refactor.

- Set `PREFECT_LOGGING_EXTRA_LOGGERS=tree` wherever a flow run executes: the local
  `docker-compose.yml` `prefect-worker` `environment:` block (beside `PREFECT_API_URL`), the
  `apps/memory/Makefile` `serve-workflows` target (`PREFECT_LOGGING_EXTRA_LOGGERS=tree uv run
  python -m tree.orchestrator`), and the managed-run env in
  `apps/memory/deploy/prefect_pipelines.py` (`managed_env_templates`) — one value, three places,
  each with a one-line comment naming this task.
- Do NOT touch `init_logger()` or any module logger. Root-handler console output in the serve
  terminal is unchanged (Prefect adds its API handler to `tree`; it does not remove ours).
- `.agents/skills/run-pipelines-e2e/SKILL.md` step 3: the readiness line is now read from the
  streamed `make memory-run-indexing-pipeline` output (update the sentence task 130 rewrote).
- `apps/memory/README.md` "Memory indexing": one sentence that the run log carries the
  readiness line.

## Out of scope
- Replacing module loggers with `get_run_logger()` (a refactor the switch makes unnecessary).
- Log-level tuning; the `httpx` silencing in `init_logger` stays.

## Acceptance Criteria

- [ ] `grep -rn "PREFECT_LOGGING_EXTRA_LOGGERS" docker-compose.yml apps/memory/Makefile apps/memory/deploy/prefect_pipelines.py` → three hits, all `tree`.
- [ ] `tests/unit/deploy/…` (or the existing deploy test module) asserts `managed_env_templates()` carries `PREFECT_LOGGING_EXTRA_LOGGERS == "tree"`.
- [ ] Live (local, served from the branch): `make memory-run-indexing-pipeline` streamed output contains `Vector search index 'vector_index' reports neither 'status' nor 'queryable'` (after a `dropSearchIndex`) — recorded verbatim in `## Log`.
- [ ] Live: `TREE_MEMORY__MODE=rag make memory-run-memory-pipeline MODE=online SOURCE_URIS=<uri>` streamed output contains `offline-pipeline: resolved 1 source_uris to 1 document_ids` (already a run-logger line — regression guard) AND, once indexing runs, the `vector leg: … kept at min_vector_score=…` line does NOT appear (indexing does not search) — i.e. no line duplication or foreign lines; recorded in `## Log`.
- [ ] Live: the serve terminal still prints the same lines once (no double console output) — recorded in `## Log`.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator waits on a fresh index
1. Operator drops `vector_index` and runs `make memory-run-indexing-pipeline`.
2. The streamed run log shows `Waiting for vector search index 'vector_index' to be ready (up to 300 s)...` then the local `treating it as ready` line (Atlas: `ready (status=READY)`); the same lines are in the Prefect UI for that run.

### Story: Operator retries a receipt on the cloud worker
1. `make memory-run-memory-pipeline MODE=online SOURCE_URIS=<uri>` against the managed deployment.
2. The Prefect UI run log carries the `resolved 1 source_uris …` line and every `tree.*` WARNING from the run — no more "check the worker's stdout".

---

Blocked by: (none)

## Log

### [PA] 2026-09-12 18:20 — Grooming

**Summary**
One Prefect setting in the three places runs execute, so the module-logger lines the feature added become visible where the operator is looking.

**Key decisions**
- `PREFECT_LOGGING_EXTRA_LOGGERS=tree` over a `get_run_logger()` refactor — the platform feature is the least mechanism.
- Three places, one value: local worker, local serve, managed deploy.

**Dependencies**
- None.

**User stories**
- 2 stories covering: fresh index locally, receipt retry on the cloud worker.

Ready for implementation.
