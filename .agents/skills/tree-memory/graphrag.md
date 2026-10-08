# Tree Memory — `graphrag` mode

The memory is a knowledge graph: documents and chunks plus extracted entities (people, organizations, events, tasks, preferences, …) and the edges between them. The graph readers answer rows tagged `kind: "node"` or `kind: "edge"`.

**Empty = `[]`** from `search_memory` / `query_memory`, or `No results found.` from `deep_search_memory` — the trigger for read chain 5 in `SKILL.md`. No graph reader sends `outcome` or `search_mode`.

## Read chains in graphrag

1. **Pick the first reader by question shape, then escalate.**

   | Question | Start with | Escalate to |
   |---|---|---|
   | Open-ended, semantic, unsure | `search_memory` | `deep_search_memory` when the answer clearly spans more than the 5 returned rows |
   | Count, filter, aggregate, exact lookup | `query_memory` | `search_memory` when the aggregate comes back empty — the generated pipeline may have filtered on the wrong field |
   | "Summarise what's in my memory" | `memory_dashboard` | `query_memory` / `search_memory` for the details a row or bar points at |
   | "Show me how X connects" / "show me the graph" | `visualize_memory_structure` (with `query` for X, none for the whole graph) | `search_memory` on the nodes the picture surfaces |

   - Good: "how many open tasks?" → `query_memory` → an aggregate row (e.g. a count of 4) → answer.
   - Bad: `search_memory("tasks")`, then counting the 10 returned rows.
2. **Deep search → selective reads.** `deep_search_memory` answers a YAML index; scan each entry's `context`, then fetch only what you need:
   - `tree-memory-local` — `Read` `<directory>/<file>` for the chosen entries.
   - `tree-memory` (cloud) — the files live on the remote host: answer from the `context` lines, or run `search_memory` on the entity names they mention.
   - Bad: reading every file in the index.
3. **Answer + picture → `visualize=true` on the same reader call.** An aggregate from `query_memory` cannot be drawn and ends with `Visualization skipped: returned documents lack 'kind' field.` — relay the rows and drop `visualize` for counting questions.
4. **A structure view with no matches draws an empty picture** (`0 nodes, 0 edges` in its summary line) — tell the user nothing matched.
5. **Conflicting preferences or facts → follow `superseded_by` (it points new → old) and present the newer node**, mentioning the older one only as history.

## Write → Read in graphrag

**After ingests, entity dedup may flag near-duplicate pairs** (pending `same_as` edges). When the user ingested several sources, or the answers show two nodes for one person or thing ("Paul" and "Paul Iusztin"), offer the review loop:

1. `review_list_pending` → show each pair with both names, `entity_type` and `similarity_score`.
2. For each pair, act ONLY on the user's explicit decision, passing the user's identifier as `reviewed_by`: `review_confirm` merges (older node wins, edges move to it; `merge_strategy` `keep_primary` / `merge_properties` / `keep_aliases`), `review_reject` marks a false positive that is never flagged again.
3. `review_list_pending` again to confirm the queue shrank. An `invalid_state` error means the pair was already decided — re-list.

- Good: list 3 pairs → user confirms 2, rejects 1 → re-list shows 0.
- Bad: confirming every pair above 0.9 similarity without asking.

## Provenance

Group rows by node type and lead with the relationship the user asked about — `related_to` edges carry the specific relation in `semantic_type` (e.g. "Paul —[has_task]→ ship the demo"). Cite each claim `[n]` to the document its rows came from — `title` and `source_uri` (format in `SKILL.md` → Answer format).
