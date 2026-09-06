---
id: 120-pa-rejection-embedding-clusters-viz
status: done
feature: embedding-clusters-viz
---

# [PA rejection] Embedding clusters + embedding map visualization (PR #42)

Tags: `rollup`, `pa-rejection`
Refs: `tasks/done/114-explicit-offline-indexing-phase.md`, `tasks/done/117-memory-clustering-subflow-summaries-store.md`, `tasks/done/118-visualize-package-and-embedding-map-renderer.md`, `tasks/done/119-embedding-map-mcp-tool-cli-docs-e2e.md`

## Scope

The feature (tasks #114–#119, PR #42) PASSED automated QA but failed the user-perspective
acceptance review on three small, operator-facing points. The clustering itself, the stored data,
the map, the warning contract, the MCP tool and the docs are otherwise right — this rollup is a
short coordinated pass, not a redesign. The SWE fixes every issue below in ONE pass, then hands
back to the Tester (full pipeline re-runs from QA).

Nothing here changes the ADR-007 design, the payload shape, the warning text, the no-run message,
the tool signature or the tool counts (7 / 14). Do not touch those.

## Acceptance Criteria

- [x] Issue 1: `apps/memory/README.md` "Small corpora" bullet no longer tells the operator to prefix `make memory-run-clustering-pipeline` with `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5`; it says WHERE the override must live for the flow to see it — the shell that runs `make memory-serve-workflows` (local) / the deployment's environment (Prefect Managed) — consistent with `.agents/skills/run-pipelines-e2e/SKILL.md` step 2 ("in the SERVING shell"). `grep -n "MIN_CLUSTER_SIZE=5 make" apps/memory/README.md` returns nothing.
- [x] Issue 2: every operator-/model-visible string produced while delivering an **Embedding map** calls it a map, not a graph, and never reports edge counts for it: the `_graph_tool_result` file-branch sentence, the UI-branch suffix, the `graphs://` resource-link `description`, and the `Wrote self-contained graph HTML (N nodes, 0 edges) to …` log line. The Graph payload path keeps its current wording byte-for-byte (`test_graph_tools.py` / `test_viz_app.py` / `test_graph.py` graph assertions unchanged). `tests/unit/mcp/test_tools.py::TestVisualizeMemoryEmbeddings` gains an assertion that the non-UI answer contains `embedding map` and not `interactive graph`; `test_visualize_embeddings.py` asserts the CLI's stdout/log carries no `0 edges`.
- [x] Issue 3: after `make memory-run-clustering-pipeline USER_IDENTIFIER=paul` the CLI stream (the PARENT `offline-pipeline` run's logs, which `tree.cli.wait_for_flow_run` already streams) shows the clustering outcome for each target user — the `N clusters, M chunks, K noise, F fallback summaries` counts on success, or the `skipped_reason` text on a skip — and after `make memory-run-indexing-pipeline …` it shows the embedded-node count (`Embedded N nodes` or equivalent). Unit tests in `tests/unit/test_offline.py` assert the parent-level log records (caplog) for both phases, including the skip case.
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green; test count ≥ 2876.
- [x] Tester re-runs full QA suite and PASSES (live re-smoke of `make memory-run-clustering-pipeline`, `make memory-visualize-embeddings HULLS=true` and one rag-mode `fastmcp call … visualize_memory_embeddings` is enough — the map/warning/no-run contracts were verified live in #119 and are untouched).
- [ ] PA re-runs acceptance review on the original tasks and ACCEPTS.

## Issues (detail)

### 1. README tells the small-corpus operator to set the knob in the wrong process — `apps/memory/README.md:286`
- **What the user experiences (wrong):** A book reader with a small corpus (the common first run) runs
  clustering → the phase is skipped (`N child embeddings < min_cluster_size 15`) → the map says
  `No clustering run found for this user — run make memory-run-clustering-pipeline` → they follow the
  README's line `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5 make memory-run-clustering-pipeline`
  → nothing changes. The script only dispatches (`dispatch_offline_pipeline` forwards no env); the
  flow reads config via `_live_app_config()` inside the `serve-workflows` process, so the variable
  in the dispatching shell is a silent no-op. The reader is now in a loop with no hint why.
- **What the spec / good UX implies (right):** #117 scope §5 asked the README to explain "how to
  loosen `min_cluster_size`". `.agents/skills/run-pipelines-e2e/SKILL.md` already has it right:
  "export `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5` in the SERVING shell (the flow
  reads it inside the serve process, not in yours)". The README must say the same thing (and, in
  one clause, what that means on Prefect Managed — the deployment's env, not the CLI).
- **Suggested fix:** Rewrite the bullet's last sentence; no code change. Optionally make
  `configs/default.yaml`'s comment (line 52) say "set where the flow runs" too.

### 2. The map is delivered as a "graph … 0 edges" — `apps/memory/src/tree/mcp/viz_app.py:124,172-173,179`, `apps/memory/src/tree/memory/visualize/graph.py:238`
- **What the user experiences (wrong):** In `rag` mode — where no graph exists at all — the
  assistant answers "Embedding map: 1448 chunks in 37 clusters (+142 noise). Since this client does
  not render inline MCP App UIs, I saved a self-contained interactive **graph** to: …" with a
  resource link described as "Self-contained interactive **graph** (download me)"; UI-capable
  clients get "… (interactive **graph** view)." On the CLI the terminal prints
  `Wrote self-contained graph HTML (1623 nodes, 0 edges) to <path>` and then `Wrote <path>` again —
  the path twice, and "0 edges" for a picture that has none by design (the same "reads as a broken
  graph" reasoning that made #119 fix the `#counts` header).
- **What the spec / good UX implies (right):** The glossary keeps **Embedding map** and **Graph
  payload** distinct ("the map has no edges"); user-facing copy should follow. The delivery helper
  and the file writer already know which they are drawing (`payload["layout"] == "fixed"`).
- **Suggested fix:** Derive one noun from the payload (`"embedding map"` when `layout == "fixed"`,
  else `"graph"`) inside `_graph_tool_result` / `_render_graph_file` and use it in the three
  strings + the log line; for the map, log the payload's `summary` (or node count only) instead of
  `N nodes, 0 edges`. Keep the graph wording identical so no graph test moves.

### 3. The operator's terminal cannot tell a skipped clustering run from a successful one — `apps/memory/src/tree/offline.py` (phase 3 / phase 4 loops)
- **What the user experiences (wrong):** `make memory-run-clustering-pipeline` ends with
  `Finished in state Completed()` / `Done. Flow completed successfully.` whether it wrote 37
  clusters or skipped the corpus as too small. The `clustering run …: 37 clusters, …` summary and
  the `clustering skipped: …` WARNING are logged inside the SUBFLOW, which `wait_for_flow_run`
  does not stream; they land only in the `serve-workflows` terminal / Prefect UI. Same for
  `make memory-run-indexing-pipeline`: #114 User Story 1 step 3 promised `Embedded N nodes` in the
  CLI output and it is not there (SWE + Tester both flagged this and deferred it).
- **What the spec / good UX implies (right):** #114 Story 1 ("the CLI streams … `Embedded N
  nodes`"), #117 Story 3 ("the log tells the operator to lower …") — from the terminal the
  operator is actually looking at. The parent flow's own log lines DO reach the CLI (that is how
  the `Beginning flow run` / all-phases-disabled lines get there).
- **Suggested fix:** The least mechanism: in `offline_pipeline`, after each per-user phase-3 /
  phase-4 call, emit ONE parent-level `logger.info` (or `logger.warning` for a skip) with the
  phase's outcome — `stats.model_dump()` already carries `clusters / clustered / noise /
  summaries_failed / skipped_reason`, and phase 3 has `{"embedded": n}`. No change to `tree.cli`,
  no subflow-log streaming. (If the SWE prefers teaching `wait_for_flow_run` to include subflow
  logs across all scripts, that is acceptable too but is a wider change — pick one.)

## User Stories

(Inherit from the original tasks — no new stories. Re-verify after the fix, from the operator's
terminal: #114 "Operator indexes without touching extraction" step 3; #117 "Operator clusters the
memory for the first time" step 3 and "Corpus too small to cluster" step 2; #119 "Reader asks the
assistant what their memory is about" step 2 and "Operator renders from the terminal with hulls"
step 2.)

## Out of scope (follow-ups, not part of this rollup)

- Streaming SUBFLOW logs to every dispatched script's CLI (`tree.cli.wait_for_flow_run`) — a
  cross-cutting change; Issue 3's parent-level lines are the scoped fix.
- Tooltip polish on the map: the `Type: chunk` row is redundant on a chunks-only map, `Document`
  repeats the tooltip title, and meta keys render as `Heading_path` (raw key + CSS capitalize) —
  all inherited from the shared template's graph convention; a separate polish task if wanted.
- Legend rows without hover interaction; `numpy` imported directly but not declared in
  `[project.dependencies]` (for the PR Reviewer); palette indexed by cluster id (equivalent to rank
  under HDBSCAN's dense labels — no action).

---

Refs: `tasks/done/114-explicit-offline-indexing-phase.md`, `tasks/done/117-memory-clustering-subflow-summaries-store.md`, `tasks/done/118-visualize-package-and-embedding-map-renderer.md`, `tasks/done/119-embedding-map-mcp-tool-cli-docs-e2e.md`

## Log

### [SWE] 2026-09-06 19:55 — Implementation

**Files modified**

Issue 1 (the knob's process):
- `apps/memory/README.md` — "Small corpora" bullet now says WHERE the override lives (the
  `make memory-serve-workflows` shell locally / the deployment's env on Prefect Managed); the
  `TREE_…=5 make memory-run-clustering-pipeline` prefix is gone.
- `apps/memory/configs/default.yaml` — same clause on the `min_cluster_size` comment (line 52).
- `apps/memory/src/tree/cli.py` — new `warn_ignored_config_overrides(prefix)`: one WARNING when a
  `TREE_…` override sits in the DISPATCHING shell, where the flow never reads it.
- `apps/memory/scripts/run_clustering_pipeline.py` — calls it with `TREE_MEMORY__CLUSTERING__`.

Issue 2 (map ≠ graph in delivered copy):
- `apps/memory/src/tree/memory/visualize/graph.py` — new `_payload_noun(payload)` (the ONE
  mechanism: `layout == "fixed"` → `"embedding map"`, else `"graph"`); the writer's log line uses
  it and drops edge counts for a map (`… embedding map HTML (N points) to …`).
- `apps/memory/src/tree/mcp/viz_app.py` — the three delivered strings (UI suffix, file sentence,
  `graphs://` link description) take the same noun. Graph payloads keep byte-identical wording.

Issue 3 (parent-level phase lines):
- `apps/memory/src/tree/offline.py` — `_get_run_logger()` (house convention from
  `tree.memory.pipeline` / `graph.sharding`); one PARENT-level line per user per phase:
  `extraction: … shards=… succeeded=… failed=…`, `indexing: … embedded=N`,
  `clustering: … run_id=… clusters=… clustered=… noise=… fallbacks=…` or
  `clustering SKIPPED: … reason=…` (WARNING). The isolated-failure `logger.exception` calls now go
  through the same run logger, so a blown phase reaches the terminal too.

Tests: `tests/unit/memory/visualize/test_graph.py`, `tests/unit/mcp/test_viz_app.py`,
`tests/unit/mcp/test_tools.py`, `tests/unit/scripts/test_visualize_embeddings.py`,
`tests/unit/scripts/test_run_clustering_pipeline.py`, `tests/unit/test_cli.py`,
`tests/unit/test_offline.py`, `tests/unit/test_embedding_map_docs.py` — additions only, no existing
assertion weakened or removed.

**Tests**
- Unit: 2894 passing, 0 failing (`make memory-tests`; baseline 2876, +18).
- Integration: N/A — this repo has no integration suite; e2e is the live run below.

**Acceptance criteria**
- [x] Issue 1 — `tests/unit/test_embedding_map_docs.py::TestMemoryReadme::test_the_small_corpus_knob_names_the_process_that_reads_it`
  (`grep -n "MIN_CLUSTER_SIZE=5 make" apps/memory/README.md` → no hits);
  `tests/unit/test_cli.py::TestWarnIgnoredConfigOverrides` (3 tests) +
  `tests/unit/scripts/test_run_clustering_pipeline.py::TestRunClusteringPipeline::test_it_warns_when_a_clustering_knob_is_set_in_this_shell`.
- [x] Issue 2 — `tests/unit/mcp/test_viz_app.py::test_graph_tool_result_calls_an_embedding_map_a_map_in_both_branches`,
  `::test_graph_tool_result_calls_a_graph_a_graph_in_both_branches` (byte-identical graph wording),
  `::test_the_delivered_noun_comes_from_the_payloads_layout_key`;
  `tests/unit/mcp/test_tools.py::TestVisualizeMemoryEmbeddings::test_the_delivered_copy_calls_the_picture_a_map_not_a_graph`;
  `tests/unit/memory/visualize/test_graph.py::test_render_graph_file_logs_a_fixed_layout_payload_as_a_map_and_no_edges`
  + `::test_render_graph_file_logs_a_graph_with_its_node_and_edge_counts`;
  `tests/unit/scripts/test_visualize_embeddings.py::TestRenderedMap::test_the_render_log_never_reports_edges_for_a_map`.
- [x] Issue 3 — `tests/unit/test_offline.py::TestOfflinePhaseLogLines` (5 tests: indexing,
  extraction, clustering summary, skip WARNING, isolated failure).
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green; 2894 ≥ 2876.
- [ ] Tester re-runs full QA suite and PASSES.
- [ ] PA re-runs acceptance review and ACCEPTS.

**Evidence**

```
$ make memory-tests
2894 passed in 47.63s

$ make memory-format-check && make memory-lint-check
288 files already formatted / All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) — Passed

# LIVE (this worktree served; dockerized tree-prefect-worker stopped for the run)
$ TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=2000 make memory-run-clustering-pipeline USER_IDENTIFIER=paul@example.com
TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE set in THIS shell, but the flow reads its config
where it RUNS — the shell running `make memory-serve-workflows` locally, the deployment's
environment on Prefect Managed. Dispatch forwards no environment, so the override is IGNORED …
2026-09-06 19:47:02 | INFO | clustering: user_id=6a8ea9579a7aeb13175955c8 run_id=49e3871d-… clusters=37 clustered=1524 noise=99 fallbacks=0
Done. Flow completed successfully.
(37 clusters despite min_cluster_size=2000 in the dispatching shell — the README's premise, live.)

$ # same knob EXPORTED in the serving shell instead
$ make memory-run-clustering-pipeline USER_IDENTIFIER=paul@example.com
2026-09-06 19:47:52 | WARNING | clustering SKIPPED: user_id=6a8ea9579a7aeb13175955c8 reason=1623 child embeddings < min_cluster_size 2000

$ make memory-run-indexing-pipeline USER_IDENTIFIER=paul@example.com
2026-09-06 19:48:40 | INFO | indexing: user_id=6a8ea9579a7aeb13175955c8 embedded=0

$ make memory-visualize-embeddings HULLS=true USER_IDENTIFIER=paul@example.com OUTPUT=/tmp/e2e120/map.html
Wrote self-contained embedding map HTML (1623 points) to /tmp/e2e120/map.html
Embedding map: 1623 chunks in 37 clusters (+99 noise)

$ TREE_MEMORY__MODE=rag … fastmcp call … visualize_memory_embeddings hulls=true
Embedding map: 1623 chunks in 37 clusters (+99 noise). Since this client does not render inline MCP
App UIs, I saved a self-contained interactive embedding map to: …
description='Self-contained interactive embedding map (download me)'

$ make memory-query-graph QUERY="agent memory" USER_IDENTIFIER=paul@example.com   # graphrag
Wrote self-contained graph HTML (76 nodes, 70 edges) to …/agent-memory-20260906-194910.html
```

**Notes**
- Judgement call (Issue 1): added the dispatch-shell warning. It costs ~10 lines in `tree.cli` and
  closes the exact loop the PA described at the moment the operator makes the mistake — proven live
  above, where the warning fired and the knob was in fact ignored.
- Judgement call (Issue 3, extraction): included, same `phase: key=value` shape. A full offline run
  otherwise says nothing per user between "Beginning flow run" and the indexing line.
- Judgement call (Issue 3, mechanism): the lines MUST go through `get_run_logger()` — a module
  logger reaches the worker's stdout only, so `wait_for_flow_run` (which reads the parent run's
  API logs) would never show them. Verified live. Same helper shape as `tree.memory.pipeline` /
  `tree.memory.graph.sharding`; not shared, because each binds its own module logger as the
  no-flow-context fallback (which is what keeps `caplog` working in unit tests).
- The per-user `logger.exception` calls moved to the run logger too, so an isolated phase failure is
  visible in the same stream as the success lines (asserted in `test_a_failed_phase_is_reported_at_the_parent_level`).
- Not fixed, no AC covers it: the map CLI still prints the path twice (`Wrote self-contained …`
  log line, then `Wrote <path>` on stdout), and `graphs://` says "No rendered graph named …" for a
  missing map file. Both are candidates for the follow-up polish task.
- Docker `tree-prefect-worker` stopped for the live run and restarted afterwards; the serving and
  MCP processes are stopped; rendered artefacts removed. The stored clustering run is unchanged
  (37 clusters — the skip test writes nothing by contract).

### [Tester] 2026-09-06 23:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check` →
  "288 files already formatted" / "All checks passed!"; `make pre-commit` → prettier, ruff
  check, ruff format, biome check all Passed)
- Unit tests: 2894 passed / 0 failed (`make memory-tests`, run twice — once before the live
  smoke, once after — both 2894 passed; ≥ 2876 baseline)
- Integration tests: N/A — no integration suite in this repo (per AGENTS.md); e2e covered live below
- Warnings: 1 pre-existing, unrelated third-party warning (`opik/rest_api/.../pydantic_utilities.py:13:
  UserWarning: Core Pydantic V1 functionality isn't compatible with Python 3.14`) — not introduced
  by this diff, not part of any changed file. 0 warnings from changed code.
- code-review plugin: enabled in `.claude/settings.json` but not invocable as a standalone CLI/tool
  from this Tester session (no slash-command surface here) — folded in via manual diff review of
  every changed file instead (Step 2 of this review).

**E2E adversarial pass (live, this worktree, `tree-prefect-worker` stopped and `make
memory-serve-workflows` served from this worktree per AGENTS.md/`run-pipelines-e2e`)**
- Happy path (a): clean shell, `make memory-run-clustering-pipeline USER_IDENTIFIER=paul@example.com`
  → no override warning; stream ends:
  ```
  2026-09-06 19:57:00 | INFO    | Downloading flow code from storage at '.'
  2026-09-06 19:57:01 | INFO    | Beginning flow run 'steadfast-pillbug' for flow 'offline-pipeline'
  2026-09-06 19:57:31 | INFO    | clustering: user_id=6a8ea9579a7aeb13175955c8 run_id=252e3708-4c48-4b61-a234-5d20530c9c86 clusters=37 clustered=1524 noise=99 fallbacks=0
  2026-09-06 19:57:31 | INFO    | Finished in state Completed()
  Done. Flow completed successfully.
  ```
  (PASS)
- Break path 1 (skip contract — knob exported in the SERVING shell): restarted serve with
  `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=2000` exported → clean dispatch shell run
  → `WARNING | clustering SKIPPED: user_id=6a8ea9579a7aeb13175955c8 reason=1623 child embeddings
  < min_cluster_size 2000`. `db.memory_clusters.countDocuments()` before and after: 37 / 37,
  same `run_id` (`252e3708-…`) — the skip wrote nothing. Expected: skip warns, DB unchanged.
  Observed: matches. (PASS)
- Break path 2 (dispatch-shell-only knob — the actual PA-rejected bug): serving shell restarted
  clean (no override); dispatch shell: `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5 make
  memory-run-clustering-pipeline USER_IDENTIFIER=paul@example.com` → warning fired naming the
  variable and `make memory-serve-workflows`; run still produced `clusters=37` (proving the knob
  really is ignored, and the operator is told so instead of silently looping). Expected: warn +
  cluster normally. Observed: matches. (PASS)
- Break path 3 (missing-context robustness of `_get_run_logger()`): confirmed empirically that
  `offline_pipeline` is `@flow`-decorated, so every call in `tests/unit/test_offline.py` executes
  inside a genuine Prefect flow-run context (verified live: `get_run_logger()` inside a throwaway
  `@flow` returns a `PrefectLogAdapter` with `propagate=True`, so `caplog`'s root handler captures
  it regardless of which branch fires) — `PREFECT_API_URL` unset (no external server; Prefect's
  ephemeral-mode fallback, `PREFECT_SERVER_ALLOW_EPHEMERAL_MODE=True` in this env) →
  `env -u PREFECT_API_URL uv run pytest tests/unit/test_offline.py -q` → **33 passed** (confirms
  the suite is genuinely server-independent, matching the `_noop_voyage_rate_limit` fixture's own
  "unit boxes don't run [a Prefect server]" assumption). Separately tried the literal
  `PREFECT_API_URL=http://127.0.0.1:1 …` form suggested in the brief — that is a materially
  different failure mode (an explicit, actively-refusing address, not "no server"): Prefect's
  engine fails validating the flow-run's own API connection *before* the flow body (and hence
  `_get_run_logger()`) ever runs, so 24/33 tests in that file fail with
  `RuntimeError: Failed to reach API at http://127.0.0.1:1` — this is pre-existing behavior of
  every `@flow`-decorated entry point in this codebase (not introduced by Issue 3) and out of this
  rollout's scope; see "Other issues found". (PASS on the in-scope claim — "tests pass either way"
  — verified with the correct interpretation of "no server"; noted as a caveat, not a blocker)
- Break path 4 (direnv/env pollution check, per instruction (i)): `env | grep TREE_` in this
  worktree's shell → only `TREE_USER_IDENTIFIER=paul.iusztin@example.com` (unrelated). No
  `TREE_MEMORY__CLUSTERING__*` present in `.env`/`.env.prod`/`.env.example`
  (`grep -n "TREE_MEMORY__CLUSTERING" .env .env.prod .env.example` → no hits) or in the live
  shell before/after the run. The new dispatch-shell warning would NOT fire spuriously for an
  operator using this project's default `.envrc`. (PASS)

**Acceptance criteria**
- [x] PASS — Issue 1 (README says WHERE to set the knob): `grep -n "MIN_CLUSTER_SIZE=5 make"
      apps/memory/README.md` → no hits (confirmed live); README bullet now reads "…set it where the
      FLOW runs… `export TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5` in the shell that
      runs `make memory-serve-workflows`… on Prefect Managed put it in the deployment's
      environment"; `configs/default.yaml:52` comment updated to match;
      `tests/unit/test_embedding_map_docs.py::TestMemoryReadme::test_the_small_corpus_knob_names_the_process_that_reads_it`
      passes (part of the 2894); live-verified via break paths 1–2 above.
- [x] PASS — Issue 1 (dispatch-shell warning): `tree.cli.warn_ignored_config_overrides(prefix)`
      called from `scripts/run_clustering_pipeline.py` with `TREE_MEMORY__CLUSTERING__`; unit
      tests `tests/unit/test_cli.py::TestWarnIgnoredConfigOverrides` (3) +
      `tests/unit/scripts/test_run_clustering_pipeline.py::test_it_warns_when_a_clustering_knob_is_set_in_this_shell`
      / `test_a_clean_shell_dispatches_without_a_warning` pass; live break path 2 above reproduces
      the exact warning text and shows the run still dispatches (a hint, not a gate).
- [x] PASS — Issue 2 (map wording, no edge counts): `_payload_noun(payload)` in
      `apps/memory/src/tree/memory/visualize/graph.py:209-220` (`"embedding map"` iff
      `payload["layout"] == "fixed"`, else `"graph"`); consumed by `_render_graph_file`'s log line
      (drops edge counts for a map) and by all three delivered strings in
      `apps/memory/src/tree/mcp/viz_app.py:113-186` (UI suffix, file-branch sentence, `graphs://`
      `description`). Graph-branch strings verified byte-identical against
      `git show acbbd6e:apps/memory/src/tree/mcp/viz_app.py` (all three lines match verbatim) and
      confirmed live via a real `graphrag` MCP call (`visualize_memory_graph` →
      "interactive graph to:" / `description='Self-contained interactive graph (download me)'` /
      log `Wrote self-contained graph HTML (91 nodes, 145 edges) to …`). Map-branch strings
      confirmed live via a real `rag`-mode MCP call (`visualize_memory_embeddings hulls=true` →
      "interactive embedding map to:" / `description='Self-contained interactive embedding map
      (download me)'` / log `Wrote self-contained embedding map HTML (1623 points) to …`, no
      "edges" anywhere in the log). Tests: `test_graph.py::test_render_graph_file_logs_a_graph_…`
      + `…_logs_a_fixed_layout_payload_as_a_map_and_no_edges`, `test_viz_app.py::test_graph_tool_result_calls_a_graph_a_graph_in_both_branches`
      + `…calls_an_embedding_map_a_map_in_both_branches` + `…the_delivered_noun_comes_from_the_payloads_layout_key`,
      `test_tools.py::TestVisualizeMemoryEmbeddings::test_the_delivered_copy_calls_the_picture_a_map_not_a_graph`,
      `test_visualize_embeddings.py::TestRenderedMap::test_the_render_log_never_reports_edges_for_a_map`
      (this one runs the REAL renderer, not a stub) — all pass.
- [x] PASS — Issue 3 (parent-level phase log lines): `tree/offline.py` — `_get_run_logger()` +
      `_log_clustering_outcome()`; one line per user per phase for extraction/indexing/clustering
      success, one WARNING for a clustering skip, `logger.exception` routed through the same run
      logger for isolated per-user failures. `tests/unit/test_offline.py::TestOfflinePhaseLogLines`
      (5 tests: indexing, extraction, clustering summary, skip WARNING, isolated failure) all pass.
      Live-verified all four log-line shapes: `clustering: user_id=… run_id=… clusters=37
      clustered=1524 noise=99 fallbacks=0`, `clustering SKIPPED: user_id=… reason=1623 child
      embeddings < min_cluster_size 2000` (WARNING), `indexing: user_id=… embedded=0`, and
      `extraction: user_id=… shards=0 succeeded=0 failed=0` followed immediately by the indexing
      line on a no-pending-docs `make memory-run-memory-pipeline` run (order confirmed). Per-user
      isolated-failure path not cheaply reproducible live (both offline-phase functions swallow a
      bogus but well-formed doc id rather than raising) — relied on
      `test_a_failed_phase_is_reported_at_the_parent_level` (mocks `RuntimeError` side effects on
      indexing + clustering, asserts both `ERROR`-level parent lines and that no success line is
      logged for that user) as the orchestrator's fallback instruction (f) allows.
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green;
      2894 ≥ 2876 (re-run twice in this review, both 2894 passed, 0 failed).
- [x] PASS — Tester re-runs full QA suite and PASSES (this entry).
- [ ] Awaiting — PA re-runs acceptance review on the original tasks and ACCEPTS (not the Tester's
      box; PA runs in `/squid-review`).

**Evidence**
```
$ make memory-tests
2894 passed in 45.50s   (re-run after live smoke: 2894 passed in 48.44s)

$ grep -n "MIN_CLUSTER_SIZE=5 make" apps/memory/README.md
(no output)

$ mongosh … --eval 'db.memory_clusters.countDocuments()'   # before skip attempt
37
$ mongosh … --eval 'db.memory_clusters.countDocuments()'   # after skip attempt
37   (same run_id: 252e3708-4c48-4b61-a234-5d20530c9c86)

$ uv run fastmcp call http://127.0.0.1:8000/mcp --auth none visualize_memory_graph query="agent memory"
Knowledge graph for 'agent memory': 91 nodes, 145 edges. Since this client does
not render inline MCP App UIs, I saved a self-contained interactive graph to:
…/agent-memory-20260906-200213.html
description='Self-contained interactive graph (download me)'

$ uv run fastmcp call http://127.0.0.1:8000/mcp --auth none visualize_memory_embeddings hulls=true   # TREE_MEMORY__MODE=rag server
Embedding map: 1623 chunks in 37 clusters (+99 noise). Since this client does
not render inline MCP App UIs, I saved a self-contained interactive embedding
map to: …/embedding-map-20260906-200240.html
description='Self-contained interactive embedding map (download me)'

$ env -u PREFECT_API_URL uv run pytest tests/unit/test_offline.py -q
33 passed in 7.67s
```

**Other issues found**
- `_get_run_logger()`'s `except` fallback branch (the module-logger path) is not concretely
  exercised by any test in `tests/unit/test_offline.py` — every test calls the decorated
  `offline_pipeline` directly, which always establishes a genuine Prefect flow-run context (real
  server in this env, or ephemeral mode if `PREFECT_API_URL` is unset), so `get_run_logger()`
  always takes the `try` branch here. This is not a functional bug (caplog captures either branch
  because both loggers propagate to root, and the analogous helpers in `tree.memory.pipeline` /
  `tree.memory.graph.sharding` are presumably exercised more directly elsewhere), but the
  docstring's claim "unit tests calling the body directly … degrades to the module logger" does
  not describe what `tests/unit/test_offline.py` actually does. Not a blocker — informational only.
- Pre-existing, out-of-scope: if the configured `PREFECT_API_URL` points at an address that
  actively refuses connections (as opposed to being merely unset), every `@flow`-decorated entry
  point in this codebase — not just `offline_pipeline` — fails before the flow body runs at all,
  with a raw `RuntimeError: Failed to reach API at …`. This predates the rollout and is a
  structural property of Prefect-orchestrated flows here, not something Issue 3 promised to fix;
  flagging for awareness only.
- Two known nits already disclosed by the SWE and confirmed live, unchanged from the SWE's report:
  the map CLI still prints the destination path twice (`Wrote self-contained embedding map HTML
  (1623 points) to /tmp/…/map.html` then `Wrote /tmp/…/map.html`), and `graphs://` still says "No
  rendered graph named …" for a missing MAP file. Both explicitly out of AC scope — candidates for
  the follow-up polish task, not this rollout.

**VERDICT: PASS**
