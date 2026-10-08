# Tree Memory — `rag` mode

The memory holds **documents** and their chunks only — no entities, no edges. `search_memory` answers whole **parent** passages, each with its `document` and the `matched_children` that made it rank. Every question, including "who" and "how many", is answered by reading those passages.

## Reading the answer

- **Empty = `outcome: "nothing_found"`** — the trigger for read chain 5 in `SKILL.md`. A down search leg never looks empty; it shows in `search_mode`.
- **`search_mode` other than `"hybrid"` → ONE caveat line first, then answer, then offer to rerun later.** For `text_only` use exactly: `Search ran text-only — vector search was unavailable; results may miss semantic matches` (`vector_only` mirrors it: the text leg was down). Why: the passages are real but partial, and an unflagged partial answer reads as complete.
  - Good: caveat line, the passages, "I can rerun this once vector search is back."
  - Bad: the `text_only` passages presented as all of memory.
- **Citations:** one `[n]` per document — its `title`, `source_uri` and `date` ride on every parent (format in `SKILL.md` → Answer format); the `matched_children` show which lines ranked.

## Chains in rag

1. **Count / filter / "list every…" → `search_memory` with `top_k` raised to 20–30 — the one exception to the `top_k=5` default — then count from the passages yourself.** State that the count covers the retrieved passages, not a database aggregate. Why: there is no `query_memory` here, and the `top_k=5` default silently caps the answer.
   - Good: "I found 4 tasks across the 18 passages that mention them."
   - Bad: "You have exactly 4 tasks" from a `top_k=5` search.
2. **"What's in my memory?" → `visualize_memory_embeddings` for topics, or `visualize_memory_structure()` with no query for documents and their chunks.**

Answer + picture is `visualize_memory_structure(query=<same query>)`. It answers two plain sentences to relay as the answer itself: `Memory is empty for this user — run make memory-run-pipeline first.` and `No results for "<query>" — nothing to draw.` The first means nothing was ever ingested — offer the chains in [`write.md`](write.md).
