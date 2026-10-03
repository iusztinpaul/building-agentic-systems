---
id: 179-embedding-map-payload-shrink
status: pending
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

- [ ] Map nodes carry exactly `id, type, name, x, y, cluster_id, size, meta`; `meta` has `cluster` and only
      non-empty `heading_path` / `snippet`; `x`/`y` have ≤ 3 decimals.
- [ ] Legend rows carry `cluster_id` (noise `-1`); the template colours nodes and hulls from it; a node WITH
      `color` (graph payload) still uses its own.
- [ ] The hover card of a map point lists `type`, `document` (= the title), `cluster`, `heading path`,
      `snippet` — identical rows to before the change.
- [ ] `to_graph_payload` output is byte-identical to before (existing tests untouched and green).
- [ ] Serialized payload size on the local corpus measured before/after and recorded in the Log (target
      ≥ 15 % smaller).
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [ ] Live (local): CLI file and MCP `as_html_file=true` render with colours, hulls and tooltips; evidence in
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
