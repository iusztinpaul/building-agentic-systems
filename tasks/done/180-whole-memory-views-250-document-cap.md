---
id: 180-whole-memory-views-250-document-cap
status: done
feature: horizon-mcp-fixes
---

# Whole-memory views cap at the 250 most-recent documents: `full_graph_max_docs` 500 → 250, and the embedding map plots only those documents' chunks ("N of M chunks")

Tags: `viz`, `memory`, `config`, `mcp`, `docs`
Depends on: —
Blocks: 181 (the live Horizon size test needs this cap in)
Implements: ADR-013 §3 (amends ADR-011 §7's cap value; ADR-007 §8's "every child chunk is a point")

## Problem

The structure view embeds up to 500 documents and the embedding map plots EVERY clustered chunk (17,306
locally → 11.4 MB). Horizon cannot serve that; structure views of 0.06–0.7 MB download fine. Config:
`apps/memory/src/tree/config/default.yaml` `query.full_graph_max_docs: 500`, `full_graph_shown_docs: 100`;
readers: `fetch_rag_structure` / `fetch_full_graph` (structure) and `load_embedding_map`
(`apps/memory/src/tree/memory/clustering/store.py`, the map — no cap today).

## Scope

**Human decision (final):** lower `full_graph_max_docs` 500 → 250 for the structure view AND make the
embedding map plot only the chunks of the 250 most-recent documents; clusters/labels still come from the
full clustering run; the header/summary says "N of M chunks".

1. **Config:** `default.yaml` `full_graph_max_docs: 250`, its comment rewritten: "the no-query cap on EVERY
   whole-memory view — Full graph / rag Memory structure (documents embedded) and the Embedding map
   (documents whose chunks are plotted). 500 → 250 on 2026-10-03: Horizon's resource read failed
   (JSON-RPC -32603) on the 17k-chunk / 11 MB map while 0.06–0.7 MB structure views downloaded fine; the
   per-view Documents slider still shows `full_graph_shown_docs`." `QueryConfig.full_graph_max_docs`
   default 250 + docstring; `full_graph_shown_docs` stays 100.
2. **`load_embedding_map(client, database, user_id, *, max_docs: int | None = None)`** (default from
   `app_config.query.full_graph_max_docs`):
   - read the user's `document` rows (`{"user_id", "kind": "node", "type": "document"}`, projection
     `_id, sources, properties.date, created_at`) → `rank_recent_documents(rows, max_docs)`
     (`tree.memory.rag.structure` — import allowed: `clustering/` may import `rag/`) → `kept_sources =
     {row["sources"][0] for row in kept if row.get("sources")}`;
   - the points query adds `"sources": {"$in": list(kept_sources)}` and projects `sources`;
   - counts become three `count_documents` over `_embedded_children_filter(user_id)`: `total_children`
     (as today), `clustered` (`+ "viz.run_id": run_id`), `noise` (`+ "viz.run_id": run_id, "cluster_id":
     -1`) — `unclustered = total_children - clustered`, NEVER `total_children - len(points)` (the cap would
     otherwise read as "unclustered" in the warning banner).
   - `EmbeddingMap` gains `clustered: int`, `noise: int`, `plotted_documents: int` (= `len(kept)`),
     `total_documents: int`; validator: `len(points) <= clustered <= total_children` and
     `plotted_documents <= total_documents`. `unclustered_warning` is unchanged in wording.
3. **`to_embedding_map_payload`:** `summary` = `"Embedding map: {len(points)} of {clustered} chunks (the
   {plotted_documents} most-recent of {total_documents} documents) in {len(clusters)} clusters (+{noise}
   noise)"` — always this form; the template already strips the `Embedding map: ` prefix for the header.
   Legend: cluster rows keep the run's `size`; the noise row uses the full-run `noise`; i.e. the legend
   counts the RUN, the header counts the PLOT, by design (document it in the builder's docstring).
   `hulls`/`warning` unchanged.
4. **Every "500" / "every chunk" sentence:** `tools.py` `visualize_memory_structure` docstring (`max_docs`
   default 250) and `visualize_memory_embeddings` docstring ("the chunks of the 250 most-recent documents
   are plotted; the legend counts the whole run"), `apps/memory/Makefile` `visualize-structure` and
   `visualize-embeddings` help, `apps/memory/README.md`, `.agents/skills/tree-memory/rag.md` /
   `run-pipelines-e2e/SKILL.md` if they state 500 or "every chunk", `docs/glossary.md` rows (Part 2),
   ADR-011 §7 / ADR-007 §8 Status-line notes (ADR-013 "Amendments").
5. **Tests:** `tests/unit/memory/clustering/test_store.py` (real-Mongo fixture pattern of
   `TestLoadChildEmbeddings`): 3 documents with distinct `properties.date`, children with `viz.run_id` of
   the run; `max_docs=2` → points only from the 2 newest, `plotted_documents == 2`, `total_documents == 3`,
   `clustered` counts all 3 documents' children, `unclustered == 0`, `noise` counts `-1` rows run-wide;
   default `max_docs` reads the config; `test_embeddings.py` summary string; `test_app_config.py` default
   250; existing `to_graph_payload` clamp test at the new value; `fetch_rag_structure` default 250.
6. **Live verification** (LOCAL env, the local corpus of ~12k documents): `make memory-visualize-embeddings`
   → header `N of M chunks (the 250 most-recent of D documents) in K clusters (+X noise)` with `N < M`,
   file size recorded; `make memory-visualize-structure` → log line "embedded 250 of D documents";
   `make memory-visualize-structure MAX_DOCS=10` still overrides. MCP: `visualize_memory_embeddings` via
   the local server carries the same summary. Paste into the Log.

## Acceptance criteria

- [x] `query.full_graph_max_docs` defaults to 250 in YAML and `QueryConfig`; `TREE_QUERY__FULL_GRAPH_MAX_DOCS`
      still overrides; `fetch_rag_structure` / `fetch_full_graph` embed at most 250 by default.
- [x] `load_embedding_map` returns points only for children whose `sources[0]` is among the `max_docs`
      most-recent documents (ranked by `rank_recent_documents`); `plotted_documents` / `total_documents` /
      `clustered` / `noise` are set as specified; `unclustered` is unaffected by the cap.
- [x] The map summary reads `N of M chunks (the P most-recent of D documents) in K clusters (+X noise)`;
      legend cluster sizes and the noise row are run-wide.
- [x] The stale warning still fires only for children lacking this run's coordinates.
- [x] No remaining "500" for the cap and no "every child chunk is a point" claim in code docstrings,
      Makefile help, README, skills, glossary (grep).
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [x] Live (local): header shows `N of M` with the 250-document parenthetical; structure embeds 250;
      evidence (header text, file size) in the Log. (Local corpus = 14 documents, so the default cap
      reads `the 14 most-recent of 14`; `N < M` shown with `TREE_QUERY__FULL_GRAPH_MAX_DOCS=5` — see Log.)

## User Stories

### Story: The user sees the recent memory, told how much is cut
1. The user asks "show me a map of my memory"; the model calls `visualize_memory_embeddings()`.
2. The header reads `4,812 of 17,306 chunks (the 250 most-recent of 12,124 documents) in 37 clusters (+63 noise)`; the legend lists 37 clusters with their full-run sizes.
3. The model relays "the map shows the chunks of your 250 most recent documents; clusters are labelled from the whole memory".

### Story: Nothing is cut on a small memory
1. A user with 40 documents calls the tool.
2. The header reads `612 of 612 chunks (the 40 most-recent of 40 documents) in 5 clusters (+8 noise)`.

### Story: The stale warning still means stale
1. The user ingests one page after the last clustering run and calls the tool.
2. The first line is `38 of 17,344 chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline` — 38, not thousands.

### Story: The operator narrows the structure view
1. `make memory-visualize-structure` → log `embedded 250 of 12124 documents`, slider shows 100.
2. `make memory-visualize-structure MAX_DOCS=20` → `embedded 20 of 12124 documents`.

---

Blocked by: (none)

## Log

### [PA] 2026-10-03 18:00 — Grooming

**Summary**
One knob caps every whole-memory view at the 250 most-recent documents: the structure view embeds 250 and
the embedding map plots only those documents' chunks, while clusters, labels, sizes and the stale warning
keep describing the whole run; the header says `N of M chunks`.

**Key decisions**
- Reuse `query.full_graph_max_docs` (comment widened) instead of a second knob — bias-to-least; recorded
  in ADR-013 §3 with the upgrade trigger.
- `unclustered` is computed from counts, not `len(points)`: the cap must not masquerade as staleness.
- The legend counts the run, the header counts the plot — stated in the docstring so nobody "fixes" it.
- The documents' recency comes from the memory `document` rows via `rank_recent_documents`, the same
  ranking the structure view uses.

**Dependencies**
- None; task 181's live size test consumes this.

**User stories**
- 4 stories: the capped map, a small memory, the stale warning, the operator's structure override.

Ready for implementation.

### [SWE] 2026-10-03 18:11 — Implementation

**Files modified**
- `apps/memory/src/tree/config/default.yaml` — `query.full_graph_max_docs: 250` + the "no-query cap on EVERY whole-memory view" comment (500 → 250, dated, Horizon -32603 reason).
- `apps/memory/src/tree/config/app_config.py` — `QueryConfig.full_graph_max_docs` `Field(250, ge=1)` + comment.
- `apps/memory/src/tree/memory/clustering/types.py` — `EmbeddingMap` gains `clustered`, `noise`, `plotted_documents`, `total_documents`; validator now also checks `len(points) <= clustered <= total_children`, `0 <= noise <= clustered`, `0 <= plotted_documents <= total_documents`; docstring states the PLOT vs RUN scopes.
- `apps/memory/src/tree/memory/clustering/store.py` — `load_embedding_map(..., *, max_docs=None)`: reads the user's `document` rows (projection `_id, sources, properties.date, created_at`) → `rank_recent_documents` → `kept_sources`; points query adds `"sources": {"$in": kept_sources}` (+ projects `sources`); three `count_documents` (`total_children`, `clustered` = this run, `noise` = this run + `cluster_id: NOISE_LABEL`); `unclustered = total_children - clustered`; one INFO log line "plotting N of M clustered chunks (the P most-recent of D documents)".
- `apps/memory/src/tree/memory/visualize/embeddings.py` — summary `Embedding map: {N} of {clustered} chunks (the {P} most-recent of {D} documents) in {K} clusters (+{noise} noise)`; the legend noise row uses the run-wide `noise`; docstring: "the legend counts the RUN, the header counts the PLOT — by design".
- `apps/memory/src/tree/mcp/tools.py` — `visualize_memory_structure` (250 most-recent; `max_docs` default 250) and `visualize_memory_embeddings` ("the child chunks of the 250 most-recent documents are plotted … the legend counts the whole run …") docstrings.
- `apps/memory/src/tree/mcp/graph_tools.py` — graphrag `visualize_memory_structure` `max_docs` "default from config, 250" (not named in Scope; the AC's grep covers code docstrings).
- `apps/memory/Makefile` — `visualize-structure` help 500 → 250; `visualize-embeddings` help states the 250-document plot / "N of M chunks" / legend counts the whole run.
- `apps/memory/README.md` — structure examples 500 → 250; Embedding map section + tool-table row describe the cap and the header/legend split.
- `apps/memory/scripts/visualize_structure.py`, `apps/memory/scripts/visualize_embeddings.py` — module docstrings (500 → 250; the map's cap).
- `docs/glossary.md` — **Embedding map** (Definition opener + the `N of M chunks …` / WHOLE-run sentence; 179's lean-node sentence kept as is; 181's `graphs://…html.gz` Notes swap NOT applied), **Full graph** (250, one cap incl. the Embedding map), **Memory structure** (100 of the 250).
- `docs/adrs/007_…md` — Status: 180's §8 clause appended to 179's note (§7's gzip clause left for 181). `docs/adrs/011_…md` — Status: the drafted §7 cap sentence appended.
- Tests: `tests/unit/memory/clustering/test_store.py` (real Mongo: `_insert_child(source=)`, `_insert_document(source=, date=)`; new `three_documents` fixture; cap → 2 newest only; run-wide `clustered`/`noise`/`unclustered == 0` under the cap; a stale chunk of a CUT document still counts as unclustered; default reads the config; cap above corpus plots all; existing fixtures given a document + `sources`), `test_types.py` (new fields + 4 nesting-violation cases), `visualize/test_embeddings.py` (new summary string; capped summary `47 of 67 … (the 250 most-recent of 1200 documents) … (+9 noise)`; legend counts the run when no noise is plotted; HTML carries the new summary), `visualize/test_graph.py` (shipped caps show 100 of 250), `rag/test_structure.py` + `graph/test_retrieval.py` (unpatched config embeds 250 of 251 documents), `config/test_app_config.py` (shipped/typed/absent-key default 250), `mcp/test_tools.py` + `scripts/test_visualize_embeddings.py` (builders + docstring/summary strings).

**Tests**
- Unit: 4836 passing, 0 failing — `make memory-tests` (env-status: local).
- Integration: N/A — no integration suite in this repo (AGENTS.md).
- `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` clean; `make pre-commit` all hooks Passed.

**Acceptance criteria**
- [x] 250 default in YAML + `QueryConfig`; env override still wins — `test_app_config.py::TestFullGraphCaps::*` (shipped/typed/absent = 250; `test_the_escape_hatch_lowers_the_cap_alone`), live `TREE_QUERY__FULL_GRAPH_MAX_DOCS=5` below; `fetch_rag_structure` / `fetch_full_graph` embed 250 of 251 by default — `test_structure.py::…::test_the_shipped_cap_embeds_at_most_250_documents`, `test_retrieval.py::…::test_the_shipped_cap_embeds_at_most_250_documents`.
- [x] Points only from the `max_docs` most recent; counts as specified; `unclustered` unaffected — `test_store.py::TestLoadEmbeddingMap::test_plots_only_the_chunks_of_the_most_recent_documents`, `::test_the_run_wide_counts_ignore_the_document_cap`, `::test_the_cap_defaults_to_the_config`, `::test_a_cap_above_the_corpus_plots_every_document`.
- [x] Summary form + run-wide legend — `test_embeddings.py::test_payload_summary_counts_chunks_clusters_and_noise`, `::test_payload_summary_says_how_much_the_document_cap_cut`, `::test_payload_legend_counts_the_whole_run_not_the_plot`.
- [x] Stale warning only for children lacking this run's coordinates — `test_store.py::…::test_counts_stale_and_unassigned_chunks_as_unclustered`, `::test_a_stale_chunk_of_a_cut_document_is_still_unclustered`, `test_embeddings.py::test_payload_legend_counts_the_whole_run_not_the_plot` (`warning is None` under the cap).
- [x] No "500" cap / "every child chunk is a point" left — grep below.
- [x] format / lint / pre-commit / tests green.
- [x] Live (local) — see Evidence; the local corpus has 14 documents / 372 children (NOT the ~12k documents Scope 6 assumed), so the default run is the "nothing is cut" story and `N < M` is shown with the env override.

**Evidence**
```
$ make env-status            -> Env target: local (.env)
$ make memory-tests          -> ============================ 4836 passed in 55.21s =============================

# Embedding map, default cap (CLI = scripts/visualize_embeddings.py --no-open, .env exported as the Makefile does)
Embedding map 0d3d5fea-… for user_id=6ac0f976…: plotting 372 of 372 clustered chunks (the 14 most-recent of 14 documents)
Embedding map: 372 of 372 chunks (the 14 most-recent of 14 documents) in 15 clusters (+1 noise)
  file: raw 239,861 B, gzip -9 48,847 B
# Embedding map, TREE_QUERY__FULL_GRAPH_MAX_DOCS=5
Embedding map: 113 of 372 chunks (the 5 most-recent of 14 documents) in 15 clusters (+1 noise)
  file: raw 107,770 B, gzip -9 27,207 B
  headless Chrome --dump-dom header: id="counts">113 of 372 chunks (the 5 most-recent of 14 documents) in 15 clusters (+1 noise)<

# Structure view
default:                       Memory structure (rag): embedded 14 of 14 documents (most recent first) → 413 nodes, 399 edges
--max-docs 10 (MAX_DOCS=10):   Memory structure (rag): embedded 10 of 14 documents (most recent first) → 307 nodes, 297 edges
TREE_QUERY__FULL_GRAPH_MAX_DOCS=5: Memory structure (rag): embedded 5 of 14 documents (most recent first) → 127 nodes, 122 edges
app_config: cap 250 shown 100

# MCP (local server: scripts/serve_mcp.py --transport streamable-http, FASTMCP_PORT=8765 because :8000 is Docker's; stopped by PID)
$ uv run fastmcp call http://127.0.0.1:8765/mcp visualize_memory_embeddings as_html_file=true --auth none
Embedding map: 372 of 372 chunks (the 14 most-recent of 14 documents) in 15 clusters (+1 noise). Since you asked for an HTML file, …
# same server with TREE_QUERY__FULL_GRAPH_MAX_DOCS=5
Embedding map: 113 of 372 chunks (the 5 most-recent of 14 documents) in 15 clusters (+1 noise). Since you asked for an HTML file, …

# Production-size estimate (throwaway scratch script; the read-only 17,306-chunk file
# /tmp/.tree/graphs/embedding-map-20261003-094514.html → MapPoints → the REAL builder + file writer)
documents with plotted chunks: 10081; chunks/doc mean=1.72 median=1 p90=2 max=263
          uncapped-all: 17306 points | payload raw=8.310 MB gzip=1.963 MB | HTML file raw=8.362 MB gzip=1.979 MB
         random-250-s0:   427 points | payload raw=0.222 MB gzip=0.055 MB | HTML file raw=0.273 MB gzip=0.070 MB
         random-250-s1:   401 points | payload raw=0.209 MB gzip=0.052 MB | HTML file raw=0.260 MB gzip=0.068 MB
         random-250-s2:   399 points | payload raw=0.209 MB gzip=0.052 MB | HTML file raw=0.260 MB gzip=0.068 MB
     worst-250-largest:  3078 points | payload raw=1.587 MB gzip=0.283 MB | HTML file raw=1.638 MB gzip=0.299 MB
  summary e.g.: Embedding map: 427 of 17306 chunks (the 250 most-recent of 10081 documents) in 184 clusters (+6625 noise)

# Stale-claim grep (apps, .agents, docs minus adrs/notes, tutorials)
$ grep -rnI "500 most\|default 500\|config, 500\|(500)\|every child chunk is a\|every chunk is a point\|full_graph_max_docs: 500" …
apps/memory/tests/unit/config/fixtures/frozen_config.yaml:98:  full_graph_max_docs: 500   (intentional, see Notes)
```

**Notes**
- Production estimate: recency is not in the reference file, so "250 most-recent" is bracketed by 250 RANDOM documents (~400 points, ~0.26 MB HTML / ~0.07 MB gzip) and the 250 LARGEST documents as the worst case (3,078 points, 1.64 MB HTML / 0.30 MB gzip) — both under the 0.06–0.7 MB structure views that download on Horizon in the typical case, and ~5x below the 8.4 MB post-179 uncapped file in the worst case. Documents were grouped by the chunk id's document key (`<user>:chunk:<doc>:p<i>:c<j>`); 10,081 documents have plotted chunks (the production `document` row count may be higher — documents without clustered children don't appear in the file). Payload = `json.dumps(payload)`; HTML = the rendered file; gzip = Python `gzip.compress` (level 9).
- `tests/unit/config/fixtures/frozen_config.yaml` keeps `full_graph_max_docs: 500` ON PURPOSE: its conftest says it is "decoupled from the human-tuned default.yaml so operator edits to the real config never break the loader value-assertions"; `test_both_caps_load_from_the_frozen_yaml` asserts the loader reads 500 from it.
- `EmbeddingMap`'s validator also checks `0 <= noise <= clustered` (one line beyond the Scope's two) — a legend "noise · 7" above the run's clustered count is the same "two queries disagree" bug the existing check guards.
- ADR-007 §7's "import `rag/` types" wording: `clustering/store.py` now imports the FUNCTION `rank_recent_documents` from `tree.memory.rag.structure`, as the task's Scope (ADR-013 §3) directs; the layout tests are green.
- The points query projects `sources` as Scope 2 specifies; `_to_map_point` does not read it (negligible bytes at ≤ 250 documents).
- Legend clusters / the noise row can name clusters with no plotted point under the cap — by design (the legend counts the RUN), documented in the builder docstring.
- The two `fastmcp call`s opened the map in the local browser (the server's file branch does `webbrowser.open`); the server was stopped by PID (34400/34392, 34494/34490), port 8765 free afterwards.
- Red/green: the store and builder changes were written before their tests in this session (contract fixed by the Scope); the existing map tests would fail against the new store without the `sources`/document fixtures, which is what the fixture rewrite covers.

### [Tester] 2026-10-03 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit`)
- Unit tests: 4836 passed / 0 failed (`make memory-tests`, env-status local)
- Integration tests: N/A (none in this repo)
- Warnings: 0 test failures; only the third-party Pydantic-V1/py3.14 import warning

**E2E adversarial pass** (real local Mongo; a temporary pytest file in `tests/unit/memory/clustering/` reusing `test_store` helpers, DELETED afterwards; CLI with `--no-open`)
- Undated doc (created_at fallback) + dated doc + doc with 0 chunks, `max_docs=2` -> points only from the newest-by-fallback chunked doc; `plotted_documents=2/total_documents=3`, `clustered=2`, `unclustered=0` (PASS)
- `max_docs=1` landing on the 0-chunk doc -> 0 points, summary `0 of 2 chunks (the 1 most-recent of 3 documents) in 1 clusters (+0 noise)`, no warning, legend keeps run size (PASS); `max_docs=0` -> 0 points, no crash (PASS); `max_docs=10000` -> all 3 documents (PASS)
- 4 documents with identical `created_at`, cap 2, two reads -> same 2 chunks both times (ties deterministic) (PASS)
- Stale: 2 chunks ingested after the run (one in the kept doc, one in a CUT doc), cap 1 -> `total_children=8, clustered=6, unclustered=2`, banner `2 of 8 chunks have no cluster assignment (or a stale one) ...` (PASS)
- Orphan child (document row missing), child with no `sources`, another tenant's doc/child -> never plotted, no leak, `unclustered=0` (PASS)
- CLI on local corpus (14 docs / 372 chunks): `TREE_QUERY__FULL_GRAPH_MAX_DOCS=1` -> `1 of 372 chunks (the 1 most-recent of 14 documents) in 15 clusters (+1 noise)` and the same string is in the written HTML; `=14` and `=1000` -> `372 of 372`; `=abc` / `=0` -> clean pydantic validation error (PASS)
- Structure view: default `embedded 14 of 14`; `--max-docs 1` -> `embedded 1 of 14`; `--max-docs 0` -> click range error (PASS)
- Smell-check: store.py/types.py/embeddings.py read clean; `sources` projected but unused by `_to_map_point` (negligible, noted by SWE).

**Acceptance criteria**
- [x] PASS - 250 default in YAML + QueryConfig; fetch paths cap at 250 (`test_app_config.py::TestFullGraphCaps`, `test_structure.py` / `test_retrieval.py` shipped-cap tests)
- [x] PASS - load_embedding_map cap / counts / unclustered (my adversarial pass + `test_store.py::TestLoadEmbeddingMap`)
- [x] PASS - summary form + run-wide legend (`test_embeddings.py`; CLI output above)
- [x] PASS - stale warning only for chunks lacking this run's coordinates (adversarial stale case)
- [ ] FAIL - No remaining stale/advertising claims: `apps/memory/README.md:399` still reads "Lower the cap per shell with `TREE_QUERY__FULL_GRAPH_MAX_DOCS=...`", which the orchestrator ruling (a) says must be dropped (AGENTS.md: env overrides are emergency-only, not documented as knobs). The "500"/"every chunk" greps are otherwise clean (only the accepted frozen_config.yaml remains).
      Fix: delete that sentence from the README Embedding-map paragraph (the YAML key `query.full_graph_max_docs` mention is fine).
- [x] PASS - format / lint / pre-commit / memory-tests green
- [x] PASS - live local evidence reproduced (see E2E)

**Other issues found**
- None blocking. Note: `TREE_QUERY__FULL_GRAPH_MAX_DOCS=0` is coerced to `False` by the env-override mechanism before validation (pre-existing); the error is still clean.

**VERDICT: FAIL** (one-line README fix)

### [SWE] 2026-10-03 18:19 — Fix: dropped the README sentence advertising `TREE_QUERY__FULL_GRAPH_MAX_DOCS=…` (L399); grep shows no other TREE_* override added by this feature outside tasks/ and tests/; format/lint/pre-commit clean, `make memory-tests` 4836 passed (local).

### [Orchestrator] 2026-10-03 18:20 — README re-check clean — Tester's sole FAIL item resolved

### [PA] 2026-10-04 00:35 — Acceptance Review

**VERDICT: ACCEPT**

Reviewed from the user's POV in the feature-level acceptance of PR #45 (first pass REJECTed on
documentation discipline only — ADR-013 §1/§5/§7 did not record the mid-pipeline decisions; resolved by
rollup `tasks/done/184-pa-rejection-horizon-mcp-fixes.md`, commit 7fbe460). Evidence from the Tester
log entries above; every automated acceptance criterion verified against the user-visible surface
(tool text, docstrings, README, skill, tutorial, glossary). The `[HUMAN]` post-merge Horizon checks, where
present, stay open and are listed in the PR body. Hand off to the PR Reviewer.
