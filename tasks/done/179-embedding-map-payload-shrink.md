---
id: 179-embedding-map-payload-shrink
status: done
feature: horizon-mcp-fixes
---

# Embedding map payload: drop duplicated and empty node fields, derive colour from the cluster legend, round coordinates — the same picture, fewer bytes

Tags: `viz`, `memory`, `mcp`, `payload`
Depends on: —
Blocks: 181 (the live Horizon size test needs this shrink in)
Implements: ADR-013 §3 (payload contract of the **Embedding map**; ADR-007 §2 unchanged in substance)

## Problem

The map's DATA payload for 17,306 chunks is ~11.4 MB: node `meta` 5.8 MB (snippet + `meta.document`,
which duplicates `name`), `name` 1.5 MB, `id` 1.3 MB, `x`/`y` 0.6 MB (full double precision), `color`
0.16 MB; every node also ships an empty `label: ""`. Code: `to_embedding_map_payload`
(`apps/memory/src/tree/memory/visualize/embeddings.py`), the shared template `_RENDER_JS`
(`apps/memory/src/tree/memory/visualize/graph.py`), consumed by the inline MCP App view AND the file.

## Scope

**Human decision (final):** shrink with NO visible loss — drop `meta.document` (== `name`), the empty
`label`, the per-node `color` (derivable from the cluster legend); round `x`/`y` to 3 decimals; tooltips
unchanged. Applies to the SHARED payload (iframe + file).

1. **`to_embedding_map_payload`:** a node is `{"id", "type": "chunk", "name", "x", "y", "cluster_id",
   "size", "meta": {"cluster", "heading_path"?, "snippet"?}}` — no `label`, no `color`, no `meta.document`;
   `x`/`y` = `round(value, 3)`; `heading_path` / `snippet` omitted when empty (the `_curated_meta` rule).
   Legend rows gain `"cluster_id"` (noise row `-1`; the "unclustered / stale" row carries none) so the
   template can map `cluster_id → colour`. `summary`, `warning`, `hulls`, `layout`, `controls` unchanged.
   `to_graph_payload` (the graph) is NOT touched: its nodes keep `label` and `color`.
2. **Template (`_RENDER_JS`):** one resolver `colourOf(n)` = `n.color ?? colourByCluster.get(n.cluster_id)
   ?? "#d5d8de"` where `colourByCluster` is built once from `payload.legend` rows that carry a numeric
   `cluster_id`; used at `graph.addNode` (`color`), the hull `byCluster` entry, and the per-type legend
   fallback (`colorByType`). `label: n.label ?? ""`. Tooltip: on a `layout: "fixed"` payload the card is
   `{type, document: n.name, ...n.meta}` so the hover card reads EXACTLY as today (title heading, then
   `type`, `document`, `cluster`, `heading path`, `snippet`). The graph (live layout) card is unchanged.
3. **Measure, don't guess:** a throwaway script (scratch, not committed) loads the local map
   (`load_embedding_map`) and prints `len(json.dumps(payload).encode())` before and after, plus
   `len(gzip.compress(...))`; the numbers go in the Log (expected on 17.3k chunks: ≈ −1.5 MB
   `meta.document`, −0.2 MB `label`, −0.17 MB `color`, −0.3 MB coordinates).
4. **Docs:** `docs/glossary.md` **Embedding map** row (Part 2 draft) notes the lean node shape; ADR-007
   Status-line note (ADR-013 "Amendments").
5. **Tests** (`tests/unit/memory/visualize/test_embeddings.py`, `test_graph.py`): node key set is exactly
   the one above; `x`/`y` rounded (e.g. `1.23456 → 1.235`); empty `heading_path`/`snippet` absent, non-empty
   present; no `color` / `label` / `meta.document`; legend rows carry `cluster_id` (noise `-1`, unclustered
   row without); graph payload unchanged (existing tests); template anchors: `_RENDER_JS` contains
   `colourOf(` and builds `colourByCluster` from `payload.legend`, and the fixed-layout tooltip card
   inserts `document`. Headless DOM smoke is the e2e step below.
6. **Live verification** (LOCAL env): `make memory-visualize-embeddings HULLS=true` → the file opens, points
   are coloured per cluster, hulls fill with the cluster colour, hovering a point shows the `document` row;
   headless Chrome `--dump-dom` over the file shows `<body data-layout="fixed"`. Serve the MCP server
   (`make memory-serve-mcp TRANSPORT=streamable-http`) and call `visualize_memory_embeddings as_html_file=true`
   → same file shape. Paste the byte counts from step 3 into the Log.

## Acceptance criteria

- [x] Map nodes carry exactly `id, type, name, x, y, cluster_id, size, meta`; `meta` has `cluster` and only
      non-empty `heading_path` / `snippet`; `x`/`y` have ≤ 3 decimals.
- [x] Legend rows carry `cluster_id` (noise `-1`); the template colours nodes and hulls from it; a node WITH
      `color` (graph payload) still uses its own.
- [x] The hover card of a map point lists `type`, `document` (= the title), `cluster`, `heading path`,
      `snippet` — identical rows to before the change.
- [x] `to_graph_payload` output is byte-identical to before (existing tests untouched and green).
- [x] Serialized payload size on the local corpus measured before/after and recorded in the Log (target
      ≥ 15 % smaller).
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [x] Live (local): CLI file and MCP `as_html_file=true` render with colours, hulls and tooltips; evidence in
      the Log.

## User Stories

### Story: The user explores the map and sees nothing missing
1. The user asks "what topics does my memory hold?"; the model calls `visualize_memory_embeddings(hulls=true)` in a UI-capable client.
2. The inline map shows coloured clusters with hulls and a legend `label · size` per cluster.
3. Hovering a point shows `Beanie ODM docs` as the heading, then `type chunk`, `document Beanie ODM docs`, `cluster MongoDB async ODMs`, `heading path Getting started > Install`, `snippet …` — the same card as before.

### Story: The operator renders the file
1. `make memory-visualize-embeddings HULLS=true`.
2. `.tree/graphs/embedding-map-<stamp>.html` opens; the header reads the summary; points are coloured per cluster; the Controls panel still drags/pins points.

### Story: The structure graph is untouched
1. `make memory-visualize-structure` (rag).
2. Documents are green stars, chunks lighter green, labels show on documents — nothing changed.

---

Blocked by: (none)

## Log

### [PA] 2026-10-03 17:55 — Grooming

**Summary**
The map payload loses what the renderer can derive or already has — `meta.document` (= `name`), the empty
`label`, the per-node `color` (now looked up from the legend's `cluster_id`) — and ships 3-decimal
coordinates; the template reconstructs the tooltip's `document` row from `name`, so nothing visible changes.

**Key decisions**
- Legend rows gain `cluster_id` rather than the payload gaining a separate colour map: the legend already
  is the per-cluster list the reader sees, and the noise row is already there.
- `type`/`size` stay per node (constant, ≈0.4 MB): not in the decided list, 3 % gain, contract churn.
- The graph payload is explicitly NOT changed — the resolver falls through to `n.color` so one template
  serves both.

**Dependencies**
- None; task 181's live size test consumes this.

**User stories**
- 3 stories: the user's inline map, the operator's file, the untouched structure graph.

Ready for implementation.

### [SWE] 2026-10-03 20:45 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/visualize/embeddings.py` — lean map node `{id, type, name, x, y, cluster_id, size, meta}`; `x`/`y` = `round(v, 3)`; `meta` = `cluster` + non-empty `heading_path` / `snippet` only; legend cluster rows + noise row (`-1`) carry `cluster_id`, the "unclustered / stale" row carries none.
- `apps/memory/src/tree/memory/visualize/graph.py` — `_RENDER_JS`: one `colourOf(n)` = `n.color ?? colourByCluster.get(n.cluster_id) ?? "#d5d8de"` (`colourByCluster` built once from `payload.legend` rows with a numeric `cluster_id`), used at `graph.addNode`, the hull `byCluster` entry and the per-type legend fallback; `label: n.label ?? ""`; fixed-layout hover card rebuilds `document` from `name`. Module docstring notes the legend's `cluster_id`. `to_graph_payload` untouched.
- `apps/memory/tests/unit/memory/visualize/test_embeddings.py` — exact node key set, rounding (`1.23456 → 1.235`, `-7.65432 → -7.654`), empty `heading_path`/`snippet` absent / non-empty present, no `color`/`label`/`meta.document`, legend `cluster_id` (noise `-1`, unclustered row without), cluster → legend colour resolution.
- `apps/memory/tests/unit/memory/visualize/test_graph.py` — template anchors: `colourOf(` resolver + `colourByCluster` from `legendRows`, all three colour sites use it, `n.color` read only inside the resolver, `label: n.label ?? ""`, fixed-layout card inserts `document: n.name` after `cluster`, graph card unchanged; per-type legend anchor updated to `colourOf(n)`; hover-card child-count anchor follows `card = Object.assign(...)`; `_TEMPLATE_SHA256["_RENDER_JS"]` re-pinned (comment says: task 179 only).
- `apps/memory/tests/unit/mcp/test_viz_app.py` — hand-made `_map_payload` fixture trimmed to the lean shape (its docstring says "as `to_embedding_map_payload` builds it").
- `docs/glossary.md` — **Embedding map** row: the task-179 sentence from the Part 2 draft ("Map nodes are lean (task 179): …"). The task-180 rewordings in that draft are left for 180.
- `docs/adrs/007_embedding_clusters_and_explicit_offline_phases.md` — Status: a 179-only "amended by 013 §3: lean map nodes …" fragment, following ADR-008's per-task precedent. The PA's drafted ADR-007 note describes §7 gzip (181) and §8 cap (180), so 180/181 finish that wording.

**Tests**
- Unit: 4818 passing, 0 failing (`make memory-tests`, env target local). Before the implementation, the 13 new or rewritten tests failed on assertions, not import errors.
- Integration: N/A (the project has no integration suite).
- `make memory-format-fix / memory-lint-fix / memory-format-check / memory-lint-check / pre-commit`: all clean.

**Acceptance criteria**
- [x] Lean node key set, `meta` non-empty only, ≤ 3 decimals: `test_embeddings.py::test_payload_nodes_carry_exactly_the_lean_key_set`, `::test_payload_rounds_coordinates_to_three_decimals`, `::test_payload_omits_an_empty_heading_path_and_snippet`, `::test_payload_names_an_untitled_document`. Live: every node of the CLI and MCP files has exactly `(cluster_id, id, meta, name, size, type, x, y)` and at most 3 decimals.
- [x] Legend `cluster_id` (noise `-1`), template colours from it, a node WITH `color` keeps its own: `::test_payload_legend_lists_clusters_by_size_then_the_two_greys`, `::test_payload_colours_clusters_through_the_legend_and_noise_grey`, `test_graph.py::test_one_colour_resolver_prefers_the_node_then_the_legend_cluster`, `::test_every_node_colour_site_goes_through_the_resolver[*]`. Live DOM: 372/372 map nodes drawn in their legend colour and 413/413 structure-graph nodes drawn in their own payload `color`.
- [x] Hover card rows match the old card: `test_graph.py::test_the_fixed_layout_tooltip_rebuilds_the_document_row_from_the_name`, plus the full-population DOM diff below.
- [x] `to_graph_payload` byte-identical: no change to its code, existing graph-payload tests untouched and green. The only `test_graph.py` edits are template-string anchors and the template hash pin.
- [x] Size measured (below): raw −26.8 %, gzip −19.1 %.
- [x] format / lint / pre-commit / memory-tests green.
- [x] Live CLI + MCP `as_html_file=true` render with colours, hulls and tooltips (below).

**Evidence**

*Measurement.* A scratch script, not committed, rebuilt an `EmbeddingMap` from the existing 17,306-chunk production-sized file `/tmp/.tree/graphs/embedding-map-20261003-094514.html`, read-only. The local Mongo has no such corpus, so it does not call `load_embedding_map`. It then ran the real builder. On the old code the rebuild reproduces the shipped payload byte-for-byte (11,354,206 = 11,354,206).
```
nodes=17306 clusters=184
                      raw bytes     gzip bytes
before (HEAD builder) 11,354,206    2,427,130
after  (this change)   8,310,244    1,962,728
delta                 -3,043,962 (-26.8 %)   -464,402 (-19.1 %)
```
Local corpus, measured literally per Scope step 3: a scratch script calls `load_embedding_map` on local Mongo for the default user and runs the HEAD builder (`git show HEAD:…/embeddings.py`, imported as a module) and the new builder on the same map:
```
                      raw bytes  gzip bytes
before (HEAD, 372 pts)  238,250     39,964
after                   188,355     32,864
delta                   -20.9 %     -17.8 %
```

*Visual identity, 17,306-point map, headless Chrome.*
- Rendered old (HEAD) and new HTML, each with a probe that emits `enterNode` for EVERY node and dumps every node's drawn colour, label, size and tooltip `innerHTML`, plus all 184 hull colours, the legend HTML, the header text and `data-layout`.
- Header, legend HTML, warning, hull checkbox and all 184 hull colours and sizes are identical. All 17,306 node colours, labels and sizes are identical.
- Tooltips differ only by the removal of 16,909 empty `heading_path` rows (an empty value cell). With those blank rows stripped from the old dump, the old and new dumps are byte-identical. Row order stays `type, cluster, document, [heading_path], snippet`.
- Screenshots at 1400×900:
  - Same file rendered twice: 0 px differ, so the renderer is deterministic.
  - New template + lean payload with the ORIGINAL unrounded coordinates vs old: 0 px differ.
  - Shipped change (rounded coordinates) vs old: 9,415 px (0.75 %) differ inside the canvas, max channel delta 58. This is anti-aliasing from the rounding: the extent is 25.24 units, so the maximum shift of 0.0005 units is about 0.016 px at an 800 px view.

*Live (local env).*
- Local had no clustering run. I served workflows from this branch (`make memory-serve-workflows`, with `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5`) and ran `make memory-run-clustering-pipeline`: `clusters=15 clustered=371 noise=1`. Note: on startup the serve process also drained 4 `online-pipeline` runs that were already queued locally.
- `make memory-visualize-embeddings HULLS=true` → `Embedding map: 372 chunks in 15 clusters (+1 noise)`, file `apps/memory/.tree/graphs/embedding-map-20261003-173603.html`.
  - `--dump-dom` shows `<body data-layout="fixed">`.
  - The screenshot shows points coloured per cluster, filled hulls and a 16-row legend.
  - Probe: 372/372 nodes in their legend colour, 15 hulls. Every card has the rows `type, cluster, document, [heading_path], snippet`, and on 372/372 the `document` row equals the title.
- `make memory-visualize-structure` (rag): 413 nodes / 399 edges. The probe shows 413/413 drawn colours and labels equal to the payload, a per-type legend (chunk `#c2e373`, document `#80c21d`) and 0 graph cards with a `document` row. The graph is untouched.
- `make memory-serve-mcp TRANSPORT=streamable-http` (`FASTMCP_PORT=8765`, because :8000 is held by another local container), then `fastmcp call http://127.0.0.1:8765/mcp visualize_memory_embeddings hulls=true as_html_file=true`. It returned the summary and a `graphs://embedding-map-20261003-173802.html` resource link. That file's payload == the CLI file's payload, and its full DOM probe == the CLI probe; `<body data-layout="fixed">`.
- Both servers were stopped by PID.

**Notes**
- Tooltip row order: I kept the OLD order (`type, cluster, document, …`) by inserting `document` after `cluster`. The Scope's literal `{type, document: n.name, ...n.meta}` would have moved `document` above `cluster`, while the human decision says the hover card must read "EXACTLY as today". The AC/story list the rows as "type, document, cluster" and I read that as a set, not an order. Flagging this so the PA can overrule.
- The only visible tooltip change is intended by the Scope's "`heading_path` / `snippet` omitted when empty" rule: on 16,909 of 17,306 production points the old card showed a `heading_path` row with a blank value; it is gone now.
- `_RENDER_JS` sha256 pin in `test_graph.py` (guards the template against "for rag" edits) re-pinned to the new template.
- Visual identity was checked with headless Chrome over the generated files. The inline MCP App iframe is not driven, but it consumes the same `_RENDER_JS` and payload.

### [Tester] 2026-10-03 21:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`: 331 files formatted; `make memory-lint-check`: all passed; `make pre-commit`: prettier, ruff, ruff format, biome all Passed). Env target: local.
- Unit tests: 4818 passed / 0 failed (`make memory-tests`). Integration: N/A (project has none).
- Warnings: 0 test warnings.

**E2E adversarial pass** (method: old HEAD builder `t179/old_embeddings.py` vs the new builder on the SAME synthetic `EmbeddingMap`; both files rendered by the working-tree template, headless Chrome probe dumps every node's drawn colour/label/size/hover-card HTML + hull colours + legend + header; blank `heading_path`/`snippet` rows stripped from the old dump. Old payload carries per-node `color` + `meta.document`, so it also exercises "old-shape node renders in the new template".)
- Happy path, live local corpus: `scripts/visualize_embeddings.py --hulls --no-open` → 372 nodes, `layout=fixed`, 15 hulls checked=true, 372/372 drawn colours == legend colour, 0 label mismatches, hull colours == legend (PASS). Without `--hulls`: 372/372, checkbox unchecked (PASS). Node key set on file: `cluster_id,id,meta,name,size,type,x,y` (PASS).
- MCP file: `apps/memory/.tree/graphs/embedding-map-20261003-173802.html` (written by the SWE's `as_html_file=true` call) `const DATA` == my fresh CLI payload (`True`), `data-layout="fixed"` present in its DOM dump (PASS).
- Break 1 (boundary: >20 clusters + noise + stale warning + hostile text; scenario A: 25 clusters x 6, 8 noise, 5 unclustered, titles/labels/headings/snippets containing `</script><script>alert(1)</script>`, quotes, `&`, `<b>`, backslash+newline, CJK, emoji, U+2028; hulls=true): old vs new → header, legend, warning, all hull lines identical; 158/158 nodes identical (palette wraps correctly at 20; script did not break out; probe present) (PASS).
- Break 2 (single point / all-noise run / old-shape payload; scenarios C, D): 0 diffs on each; old payload (with `color` + `meta.document`) rendered by the new template is identical to the new payload render, so a node WITH `color` keeps its own and a stale `meta.document` does not duplicate the row (PASS).
- Break 3 (structure + query views): `visualize_structure.py` → 413/413 drawn colours == payload `color`, labels equal, `layout=live`, node keys still include `color,label`; `--query "MLOps"` → 171/171, same (PASS). Graph card path untouched in the diff.
- Break 4 (failure mode: a drawn cluster with NO `memory_clusters` row — partially written run; scenario B = `mk(4, 5, 3, missing_rows=(2,), neg=(-2,))`): FAIL.
  - Old: points of cluster 2 drawn `#ff7f0e` (palette), hull `#ff7f0e`.
  - New: the same 5 points drawn `#d5d8de`, hull `#d5d8de` — the colour of the legend's "unclustered / stale (not shown)" category — while the tooltip still says "Cluster 2". `_cluster_label` documents this state ("a stable stand-in if a cluster row is missing (an all-noise or partially-written run)").
  - Same run, point with `cluster_id = -2`: old `#9e9e9e` (noise grey, `cluster_colour` rule "any negative label"), new `#d5d8de`, though counted as noise and tooltip "noise". Lower risk: `clustering/core.py:100-102` normalises degenerate sklearn labels (-2/-3) to -1 before storing, so -2 is unreachable through the real pipeline; kept in as defence-in-depth.
  - Reachability of the orphan case: `store.py` writes cluster rows (`bulk_write(..., ordered=False)`) and chunk stamps in separate bulk writes; a partial failure leaves chunks pointing at a cluster id with no row.
- Break 5 (zero points + `unclustered=3`): old and new both render "No graph data returned." (pre-existing, identical; out of scope).

**Acceptance criteria**
- [x] PASS — Lean node key set, `meta` non-empty only, ≤ 3 decimals — `test_embeddings.py::test_payload_nodes_carry_exactly_the_lean_key_set`, `::test_payload_rounds_coordinates_to_three_decimals`, `::test_payload_omits_an_empty_heading_path_and_snippet`; live file key set above.
- [x] PASS (healthy maps) — Legend `cluster_id` (noise -1), template colours nodes and hulls from it, node WITH `color` keeps its own — `test_graph.py::test_one_colour_resolver_*`, `::test_every_node_colour_site_goes_through_the_resolver[*]`; live 372/372 + hull colours; old-shape payload scenario. Degenerate runs: see FAIL below.
- [x] PASS — Hover card rows identical to before — probe diff of 158 + 24 + 1 + 6 hostile nodes and 372 live nodes (only the blank rows are gone); `test_graph.py::test_the_fixed_layout_tooltip_rebuilds_the_document_row_from_the_name`. Order stays type, cluster, document, heading path, snippet (SWE note on the Scope's literal order is a PA call).
- [x] PASS — `to_graph_payload` unchanged: no diff in its code; structure 413 and query 171 nodes still carry `color`/`label`, drawn colours equal.
- [x] PASS — Size measured and logged by SWE (raw -26.8 %, gzip -19.1 % on 17,306 pts; local 372 pts -20.9 %); target ≥ 15 % met. I did not re-measure; consistent with my payload checks (no `label`/`color`/`meta.document` bytes).
- [x] PASS — format / lint / pre-commit / `make memory-tests` green (above).
- [x] PASS — Live CLI + MCP `as_html_file=true` render (above).
- [ ] FAIL (e2e rule, not an AC checkbox) — "NO visible loss" for a drawn cluster with no `memory_clusters` row (and cluster_id < -1): see Break 4.
      Expected: points/hull keep the colour they had (`cluster_colour(cid)`; negative → `NOISE_COLOUR`).
      Actual: `#d5d8de` (the "not shown" category colour).
      Fix: the spec prescribes `?? "#d5d8de"`, so this is a spec gap, not an SWE deviation. Smallest fix inside the spec's own `n.color ??` hatch: in `to_embedding_map_payload` (`embeddings.py`, node loop) set `node["color"] = cluster_colour(point.cluster_id)` ONLY when `point.cluster_id` has no legend row (not in `{c.cluster_id for c in embedding_map.clusters}` and not the noise row's -1). Zero extra bytes on a healthy map. Two regression tests: orphan cluster id → node `color == cluster_colour(cid)`; `cluster_id = -2` → `NOISE_COLOUR`. If the PA prefers to accept the literal fallback, at minimum make the template fall back to the noise colour for negative ids and record the orphan case as accepted.

**Evidence**
```
$ make memory-tests          -> 4818 passed in 56.22s
$ python3 qa/cmp2.py (old vs new probe, blanks stripped)
A_25clusters_noise_stale: nodes 158 158, header/legend/hull equal: True, nodes differing: 0
C_one_point:              nodes 1 1,     equal: True, nodes differing: 0
D_all_noise:              nodes 6 6,     equal: True, nodes differing: 0
B_missing_row_and_neg2:   OLD HULL 2 #ff7f0e 5 / NEW HULL 2 #d5d8de 5; nodes differing: 6
  old c10..c14 #ff7f0e, c23 #9e9e9e   new c10..c14 #d5d8de, c23 #d5d8de
$ qa/live_check.py
live_hulls   372/372 mismatch 0 layout fixed hulls 15 checked true
live_nohulls 372/372 mismatch 0 layout fixed checked false
live_struct  413/413 mismatch 0 layout live
live_query   171/171 mismatch 0 layout live
```
Scratch under the session scratchpad `qa/` (gen.py, cmp2.py, live_check.py). Headless Chrome processes stopped by PID.

**Other issues found**
- Zero-point map renders "No graph data returned." in both old and new (pre-existing).
- `scripts/visualize_*.py` import rewrites `~/.opik.config` on every run (pre-existing side effect, noticed only).

**VERDICT: FAIL** (1 issue: degenerate-run colour regression; everything else PASS)

### [SWE] 2026-10-03 22:00 — Fixes (Tester Break 4)

**Spec amendment (orchestrator decision, both parts).** Scope §2 had prescribed `colourOf(n) = n.color ?? colourByCluster.get(n.cluster_id) ?? "#d5d8de"`. That fallback painted degenerate runs in the "unclustered / stale (not shown)" grey. It is amended to:
1. **Payload.** `to_embedding_map_payload` sets `node["color"] = cluster_colour(cid)` ONLY for a point with `cid >= 0` and no `memory_clusters` row (no legend row). This is the partially-written-run case. A healthy map carries zero `color` keys and zero extra bytes.
2. **Template.** `colourOf(n) = n.color ?? colourByCluster.get(n.cluster_id) ?? (n.cluster_id < 0 ? NOISE_COLOUR : "#d5d8de")`, with `const NOISE_COLOUR = "#9e9e9e"` in `_RENDER_JS`. A test pins it equal to `embeddings.NOISE_COLOUR`; the template cannot import that constant because embeddings already imports graph. `n.color` is still preferred, so old-shape payloads and graph payloads are unchanged.

**Files modified**
- `apps/memory/src/tree/memory/visualize/embeddings.py`: orphan-cluster `color` (node loop).
- `apps/memory/src/tree/memory/visualize/graph.py`: `_RENDER_JS` negative-id → `NOISE_COLOUR` fallback.
- `apps/memory/tests/unit/memory/visualize/test_embeddings.py`, three new tests:
  - `test_payload_keeps_the_colour_of_a_cluster_with_no_legend_row`: orphan cluster → `color == cluster_colour(cid)`; every other node stays lean.
  - `test_payload_ships_no_colour_on_a_healthy_map`.
  - `test_payload_leaves_a_negative_cluster_id_to_the_template_noise_rule`: -2 → no `color`, tooltip shows "noise".
- `apps/memory/tests/unit/memory/visualize/test_graph.py`:
  - New `test_a_negative_cluster_id_without_a_legend_row_is_drawn_noise_grey` (resolver text + `NOISE_COLOUR` literal pinned to the Python constant).
  - The resolver anchor is loosened to `n.color ?? colourByCluster.get(n.cluster_id)`.
  - `_RENDER_JS` sha re-pinned (`055947dc…`), with the comment updated.

**Tests**
- Before the fix, the 2 new behaviour tests failed on assertions.
- After the fix: `make memory-tests` 4822 passed, 0 failed (env target local); format-fix / lint-fix / format-check / lint-check / pre-commit all clean.

**Evidence**
I re-ran the Tester's `qa/gen.py` (output redirected to `qa2/`) and the `qa/cmp2.py` comparison over scenarios A–D: old HEAD builder vs new builder, both rendered by the working-tree template, with a full-node headless-Chrome probe and blank rows stripped.
```
== A_25clusters_noise_stale nodes 158 158  header/legend/hull lines equal: True  nodes differing: 0
== B_missing_row_and_neg2   nodes 24 24    header/legend/hull lines equal: True  nodes differing: 0
== C_one_point              nodes 1 1      header/legend/hull lines equal: True  nodes differing: 0
== D_all_noise              nodes 6 6      header/legend/hull lines equal: True  nodes differing: 0
B old/new: c10..c14 #ff7f0e (orphan cluster 2), c23 #9e9e9e (cluster_id -2); HULL 2 #ff7f0e 5 in both
```
- **Size:** the 17,306-point production-sized map is unchanged at raw 8,310,244 bytes (a healthy run carries no `color`).
- **Live local CLI** (`make memory-visualize-embeddings HULLS=true OUTPUT=<scratch>`): 372 points, 0 nodes with a `color` key, 372/372 drawn in their legend colour, 15 hulls.
- Headless Chrome processes were stopped by PID.

### [Tester] 2026-10-03 22:40 — Re-QA (after SWE "Fixes (Tester Break 4)")

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check` 331 files formatted; `make memory-lint-check` all passed; `make pre-commit` prettier/ruff/ruff format/biome Passed). Env target: local.
- Unit tests: 4822 passed / 0 failed (`make memory-tests`, 56.30s). Integration: N/A (none in project).
- Warnings: 0.

**E2E adversarial pass** (independent re-run in a fresh dir `qa3/`; method unchanged: old HEAD builder vs new builder on the SAME synthetic `EmbeddingMap`, both rendered by the working-tree template, headless-Chrome probe of every node's drawn colour/label/size/hover card + hulls + legend + header, blank rows stripped from old)
- Break 4 repro (scenario B `mk(4,5,3,missing_rows=(2,),neg=(-2,))`): header/legend/hull equal, nodes differing 0 (was 6). Orphan cluster 2 drawn `#ff7f0e` in both, hull `#ff7f0e 5` in both; `cluster_id=-2` drawn `#9e9e9e` in both. PASS.
- New scenario F (extra, beyond the SWE's): `mk(26,3,2,missing_rows=(3,21,25),neg=(-2,-3),unclustered=2)` — orphans past the 20-colour palette wrap (21 -> `#aec7e8`, 25 -> `#98df8a`), -2 and -3 noise grey, stale row: 82/82 nodes identical, header/legend/hulls equal. Payload: exactly 9 nodes carry `color` (the 3 orphan clusters x 3), none elsewhere. PASS.
- Regression A (25 clusters + noise + stale + hostile text `</script>`, quotes, CJK, emoji, U+2028): 158/158 identical. C (one point): 1/1. D (all noise): 6/6. PASS.
- Live local CLI `visualize_embeddings.py --hulls --no-open` (`embedding-map-20261003-175558.html`): 372/372 drawn == legend colour, 15 hulls, hull colours == legend, `layout=fixed`, hulls checkbox checked, 0 nodes with a `color` key, node keys `cluster_id,id,meta,name,size,type,x,y`. PASS.
- Structure `visualize_structure.py`: 413/413 drawn == payload `color`, `layout=live`, node keys still include `color,label`; `--query MLOps`: 171/171. Graph path unchanged. PASS.

**Acceptance criteria**
- [x] PASS — Lean node keys / meta / <=3 decimals — `test_embeddings.py::test_payload_nodes_carry_exactly_the_lean_key_set`, `::test_payload_rounds_coordinates_to_three_decimals`, `::test_payload_omits_an_empty_heading_path_and_snippet`; live key set above. The orphan-only `color` is the amended exception (`embeddings.py` node loop), covered by `::test_payload_keeps_the_colour_of_a_cluster_with_no_legend_row` and `::test_payload_ships_no_colour_on_a_healthy_map`.
- [x] PASS — Legend `cluster_id` (noise -1), template colours nodes + hulls from it, node WITH `color` keeps own — `test_graph.py::test_one_colour_resolver_*`, `::test_every_node_colour_site_goes_through_the_resolver[*]`, `::test_a_negative_cluster_id_without_a_legend_row_is_drawn_noise_grey` (NOISE_COLOUR literal pinned to the Python constant); live 372/372 + hull colours; scenarios B/F including degenerate runs.
- [x] PASS — Hover card rows identical (order type, cluster, document, heading path, snippet; rulings accepted) — probe diffs: 0 across A–D, F, and 372 live nodes (only blank rows gone).
- [x] PASS — `to_graph_payload` unchanged (no diff in its code; 413/171 live nodes keep `color`/`label`).
- [x] PASS — Size: SWE measured raw -26.8 %, gzip -19.1 % on 17,306 pts (>=15 %); local 372 pts -20.9 %. Fix adds zero bytes on a healthy map (0 `color` keys in the live file; not re-measured on the 17k file, consistent by construction: `color` only for `cid>=0` with no legend row).
- [x] PASS — format / lint / pre-commit / `make memory-tests` green.
- [x] PASS — Live CLI + MCP file render (MCP file from the prior pass has the same payload shape; CLI re-verified now).
- [x] PASS — Break 4 (previously FAIL) resolved; "no visible loss" holds for orphan clusters and cluster_id < -1.

**Evidence**
```
$ make memory-tests -> 4822 passed in 56.30s
$ python3 qa3/cmp3.py (old vs new probe, blanks stripped)
F_orphans_beyond_palette nodes 82 82   equal: True  nodes differing: 0
A_25clusters_noise_stale nodes 158 158 equal: True  nodes differing: 0
B_missing_row_and_neg2   nodes 24 24   equal: True  nodes differing: 0
C_one_point              nodes 1 1     equal: True  nodes differing: 0
D_all_noise              nodes 6 6     equal: True  nodes differing: 0
$ qa3/live/lc.py
live_hulls  372/372 mismatch 0 layout fixed hulls 15 checked true; hull colours ok: True
live_struct 413/413 mismatch 0 layout live
live_query  171/171 mismatch 0 layout live
```
Scratch under the session scratchpad `qa3/`. Headless Chrome processes stopped by PID (none left running).

**Other issues found**
- Nit (not blocking): glossary Embedding map row does not mention the orphan-cluster `color` exception; ADR-013 §3 / task Scope §2 text still shows the pre-amendment `?? "#d5d8de"` resolver — the SWE's amendment note in this log is the record; PA may want the ADR to match.
- Pre-existing, unchanged: zero-point map renders "No graph data returned."; scripts rewrite `~/.opik.config` on import.

**VERDICT: PASS**
