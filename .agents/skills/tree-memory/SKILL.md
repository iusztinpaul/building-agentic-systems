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

**Provenance** is the thread through every chain: each claim you make carries an inline `[n]` citation to where it came from — a memory document or a web URL (see [Answer format](#answer-format)).

---

## Steps

1. **Find the mode.** The server registers its tools once at boot from `memory.mode` (default `rag`), so the tool list IS the mode: `query_memory` absent → `rag`, read [`rag.md`](rag.md); present → `graphrag`, read [`graphrag.md`](graphrag.md). Done when you have named the mode and read its file.
2. **Pick the chain.** Reading or seeing memory → the read chains below plus the mode file. Adding to memory → read [`write.md`](write.md). Done when you can name the chain you will run.
3. **Run it to a checkable end.** A read is done when every claim carries an `[n]` that resolves in the References list, or you gave the no-hits answer of read chain 5. A write is done when every source has its **receipt** reported.

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

1. **Memory first: answering a question ALWAYS starts with `search_memory`, never `search_web`.** Why: the user asked their memory; a web answer given first reads as something they stored. An explicit "pull this page into memory" is a write, not a question — [`write.md`](write.md) chain 3.
   - Good: "what did I decide about Modal?" → `search_memory("Modal decision")`.
   - Bad: the same question → `search_web("Modal")`.
2. **Multi-topic question → decompose it into single-topic queries, one `search_memory` call each, in parallel.** Why: one embedding of two topics lands between both and ranks neither well.
   - Good: "what do I know about Prefect limits and Voyage pricing?" → `search_memory("Prefect deployment limits")` + `search_memory("Voyage AI embedding pricing")`.
   - Bad: `search_memory("Prefect limits and Voyage pricing")`.

   **Then merge the results, dropping duplicates by `parent_id` (graphrag: the row's `_id`) before you read or cite.** Why: two topics can share a passage, and keeping both copies weights it twice and splits one source across two numbers.
   - Good: both queries return parent `66f1…a2` → read it once, cite it as `[1]` under both topics.
   - Bad: the same passage summarised twice, cited as `[1]` and `[3]`.
3. **`top_k=5` per query (graphrag: also `max_results=5`), then cite only the passages that answer the question.** Relevance is gated on the server — the vector search keeps hits ≥ `query.min_vector_score` (0.70 similarity), the text search hits ≥ `query.min_text_score` — so `top_k` only caps how many survivors reach you. Never threshold the returned `score`: it is a rank-fusion score (max ≈ 0.033), not a similarity. The one exception is count/list questions, which raise the cap (see the mode file).
   - Good: `search_memory("Modal cold starts", top_k=5)` → 2 of the 5 passages answer → cite those 2.
   - Bad: dropping every passage with `score < 0.85` — that drops all of them.
4. **Budget: at most 3 memory-reader calls per topic** — `search_memory`, plus `query_memory` and `deep_search_memory` in graphrag, counted together — **each retry a new angle**: different entities or a different framing. Why: a synonym lands in the same embedding neighbourhood and returns the same results.
   - Good: `"Prefect deployment slots"` → `"free-tier limit five deployments"`.
   - Bad: `"Prefect deployment slots"` → `"Prefect deployment slot"`.
5. **No hits (an empty answer — the mode file says what it looks like — or no passage that answers) → ONE new-angle retry, then answer exactly `I don't know. I couldn't find anything.`, name the queries you ran, and suggest `search_web`. Run the web only on a yes.** Why: an answer stitched from unrelated passages or general knowledge is a hallucination; web text without its URL reads as something the user stored. In a multi-topic question, answer the topics that hit and give this line for the ones that did not.
   - Good: two empty searches → "I don't know. I couldn't find anything. I searched "sourdough starter decision" and "bread baking notes" — want me to search the web with `search_web`?" → yes → `search_web` → `scrape_web` → answer citing the URLs → offer to keep the useful pages ([`write.md`](write.md)).
   - Bad: answering from general knowledge, or calling `search_web` before the user says yes.
6. **Stop once the answer is covered — 3 is a budget, not a target.**
   - Good: the first call returns the decision asked about → answer and stop.
   - Bad: two more calls "to be thorough".
7. **Answer + picture → reuse the SAME `query` in the visual call.** Why: the picture then shows exactly the passages you quoted.
   - Good: `search_memory("Modal cold starts")`, then the mode's picture call with `"Modal cold starts"`.
   - Bad: a picture of the whole memory next to an answer about one topic.
8. **Map → drill-down: a cluster label from `visualize_memory_embeddings` is a ready `search_memory` query.** Why: the label names a topic the memory actually holds.
   - Good: cluster "Prefect deployment limits" → "what's in that one?" → `search_memory("Prefect deployment limits")`.
   - Bad: summarising the cluster from its label alone.

**Relay the map's two fixed answers verbatim**, as the answer itself:

- `No clustering run found for this user — run make memory-run-clustering-pipeline to build the embedding map.` — offer to run the command.
- A first line `N of M chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline` — repeat it, then the rest: the map is real but under-reports the corpus.

**A visual answer carrying a `graphs://…html.gz` link** could not render inline: on `tree-memory-local` share the local path it also gives; on `tree-memory` (cloud) read the linked blob within its expiry (about 5 minutes), base64-decode + gunzip it to a local `.html` (`base64 -d < blob.b64 | gunzip > map.html`) and open it; an expired link → re-run the visual tool once.

---

## Answer format

**Compile one answer in your own words, cite every claim inline with `[n]`, and end with a References list.** Number sources in order of first citation, one number per memory document or web URL, reused for every claim from it; each entry is `[n] <title> — <source_uri> (<date>)`, dropping `(<date>)` when unknown. Why: the inline number shows which claim rests on which source, and the list makes each one checkable.

```
Modal cold starts dropped to about 2 s once memory snapshots were enabled [1]; the Prefect worker relies on that [2].

References
[1] Modal deployment notes — https://example.com/modal-notes (2026-09-12)
[2] Prefect worker setup — file:///notes/prefect.md (2026-08-30)
```

- Good: the answer above — claims in prose, each with its `[n]`.
- Bad: a dump of the passages grouped by document, or a claim with no `[n]`.

---

## Errors

Any tool may answer `{"error_type", "retryable", "message"}`. Act on `retryable`: `true` → retry the SAME call ONCE, then relay `message` and end the chain; `false` → change the input or end the chain. Why end it: the next link would work from missing data.

`configuration_error` naming `horizon-actor-email` or `TREE_USER_IDENTIFIER` means the request carries no user (Horizon auth disabled, a service-account key, or an empty local env); `configuration_error` naming `make memory-signup` means the email has no user yet — the operator runs `make memory-signup USER_IDENTIFIER=<their Horizon account email>`. Either way, relay the message, do not retry.
