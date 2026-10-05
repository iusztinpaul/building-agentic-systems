---
name: tree-memory
description: "Query, explore, and write to Tree's memory through its MCP server (rag or graphrag mode)."
argument-hint: <natural language query or instruction>
disable-model-invocation: true
---

# Tree Memory

A facade over Tree's MCP server — `tree-memory` (cloud) or `tree-memory-local` (this repo). Each tool's description says what it takes and returns; this skill says which tools to **chain**, in what order, and why.

The query or instruction to run is: $ARGUMENTS

If no arguments are provided, ask the user what they want to know or do with Tree's memory.

**Provenance** is the thread through every chain: each claim you make carries where it came from — a memory document's `title` and `source_uri`, a web URL, or the searches that came up empty.

---

## Steps

1. **Find the mode.** The server registers its tools once at boot from `memory.mode` (default `rag`), so the tool list IS the mode: `query_memory` absent → `rag`, read [`rag.md`](rag.md); present → `graphrag`, read [`graphrag.md`](graphrag.md). Done when you have named the mode and read its file.
2. **Pick the chain.** Reading or seeing memory → the read chains below plus the mode file. Adding to memory → read [`write.md`](write.md). Done when you can name the chain you will run.
3. **Run it to a checkable end.** A read is done when every claim carries its provenance, or you answered "Not in memory" naming what you searched. A write is done when every source has its **receipt** reported.

| Tool | `rag` | `graphrag` | Purpose |
|---|---|---|---|
| `search_memory` | ✅ | ✅ | First reader for any question. |
| `visualize_memory_structure` | ✅ | ✅ | Picture of an answer, or of the whole memory. |
| `visualize_memory_embeddings` | ✅ | ✅ | Topic map. |
| `ingest_url` / `ingest_file` / `ingest_conversation` | ✅ | ✅ | Writes. |
| `search_web` / `scrape_web` | ✅ | ✅ | Find and read web pages. |
| `query_memory` | — | ✅ | Exact counts and filters. |
| `deep_search_memory` | — | ✅ | Wide sweep → index → selective reads. |
| `memory_dashboard` | — | ✅ | Summary view. |
| `review_list_pending` / `review_confirm` / `review_reject` | — | ✅ | Dedup review loop. |

A tool marked `—` is absent in that mode; the mode file names the chain to use instead.

---

## Read chains

1. **Budget: at most 3 memory-reader calls per question** — `search_memory`, plus `query_memory` and `deep_search_memory` in graphrag, counted together — **each retry a new angle**: different entities or a different framing. Why: a synonym lands in the same embedding neighbourhood and returns the same results.
   - Good: `"Prefect deployment slots"` → `"free-tier limit five deployments"`.
   - Bad: `"Prefect deployment slots"` → `"Prefect deployment slot"`.
2. **Empty answer (the mode file says what "empty" looks like) → ONE new-angle retry, then "Not in memory", naming what you searched.** Why: a third phrasing is guessing.
   - Good: two empty answers → "Not in memory — I searched the sourdough starter decision and bread baking notes."
   - Bad: a third and fourth rephrasing of "sourdough".
3. **Stop once the answer is covered — 3 is a budget, not a target.**
   - Good: the first call returns the decision asked about → answer and stop.
   - Bad: two more calls "to be thorough".
4. **Answer + picture → reuse the SAME `query` in the visual call.** Why: the picture then shows exactly the passages you quoted.
   - Good: `search_memory("Modal cold starts")`, then the mode's picture call with `"Modal cold starts"`.
   - Bad: a picture of the whole memory next to an answer about one topic.
5. **Map → drill-down: a cluster label from `visualize_memory_embeddings` is a ready `search_memory` query.** Why: the label names a topic the memory actually holds.
   - Good: cluster "Prefect deployment limits" → "what's in that one?" → `search_memory("Prefect deployment limits")`.
   - Bad: summarising the cluster from its label alone.
6. **Memory miss → offer the web, and run it only on a yes.** Chain `search_web` → `scrape_web` → answer with the URLs as provenance → offer to keep the useful pages (see [`write.md`](write.md)). Why: web text without its URL reads as something the user stored.
   - Good: "Not in memory. Want me to search the web?" → yes → answer citing the URLs.
   - Bad: two empty searches → web results presented as what memory holds.

**Relay the map's two fixed answers verbatim**, as the answer itself:

- `No clustering run found for this user — run make memory-run-clustering-pipeline to build the embedding map.` — offer to run the command.
- A first line `N of M chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline` — repeat it, then the rest: the map is real but under-reports the corpus.

**A visual answer carrying a `graphs://…html.gz` link** could not render inline: on `tree-memory-local` share the local path it also gives; on `tree-memory` (cloud) read the linked blob within its expiry (about 5 minutes), base64-decode + gunzip it to a local `.html` (`base64 -d < blob.b64 | gunzip > map.html`) and open it; an expired link → re-run the visual tool once.

---

## Errors

Any tool may answer `{"error_type", "retryable", "message"}`. Act on `retryable`: `true` → retry the SAME call ONCE, then relay `message` and end the chain; `false` → change the input or end the chain. Why end it: the next link would work from missing data.

`configuration_error` naming `horizon-actor-email` or `TREE_USER_IDENTIFIER` means the request carries no user (Horizon auth disabled, a service-account key, or an empty local env); `configuration_error` naming `make memory-signup` means the email has no user yet — the operator runs `make memory-signup USER_IDENTIFIER=<their Horizon account email>`. Either way, relay the message, do not retry.
