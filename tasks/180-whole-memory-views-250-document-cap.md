---
id: 180-whole-memory-views-250-document-cap
status: pending
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

- [ ] `query.full_graph_max_docs` defaults to 250 in YAML and `QueryConfig`; `TREE_QUERY__FULL_GRAPH_MAX_DOCS`
      still overrides; `fetch_rag_structure` / `fetch_full_graph` embed at most 250 by default.
- [ ] `load_embedding_map` returns points only for children whose `sources[0]` is among the `max_docs`
      most-recent documents (ranked by `rank_recent_documents`); `plotted_documents` / `total_documents` /
      `clustered` / `noise` are set as specified; `unclustered` is unaffected by the cap.
- [ ] The map summary reads `N of M chunks (the P most-recent of D documents) in K clusters (+X noise)`;
      legend cluster sizes and the noise row are run-wide.
- [ ] The stale warning still fires only for children lacking this run's coordinates.
- [ ] No remaining "500" for the cap and no "every child chunk is a point" claim in code docstrings,
      Makefile help, README, skills, glossary (grep).
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [ ] Live (local): header shows `N of M` with the 250-document parenthetical; structure embeds 250;
      evidence (header text, file size) in the Log.

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
