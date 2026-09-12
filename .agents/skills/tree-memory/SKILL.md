---
name: tree-memory
description: "Query, explore, and write to Tree's memory. Use when the user asks to recall, search, visualize, or ingest information (people, tasks, episodes, preferences, documents, URLs, files, conversations)."
argument-hint: <natural language query or instruction>
allowed-tools: mcp__tree-memory__query_memory, mcp__tree-memory__search_memory, mcp__tree-memory__deep_search_memory, mcp__tree-memory__visualize_memory_embeddings, mcp__tree-memory__ingest_url, mcp__tree-memory__ingest_file, mcp__tree-memory__ingest_conversation, Read
disable-model-invocation: true
---

# Tree Memory

Query, explore, and write to Tree's (Your Rooted Personal Assistant) memory through the MCP server.

## Instruction

The query or instruction to run is: $ARGUMENTS

If no arguments are provided, ask the user what they want to know or do with Tree's memory.

---

## Memory modes — which tools exist

The server registers its tools ONCE at boot from `memory.mode` (ADR-006), so the tool set tells you the mode:

| Tool | `rag` | `graphrag` | What it does |
|---|---|---|---|
| `search_memory` | ✅ | ✅ | Semantic + text search. **Different signature per mode** (see below). |
| `ingest_url` | ✅ | ✅ | Ingest a web page. |
| `ingest_file` | ✅ | ✅ | Ingest a local file's text. |
| `ingest_conversation` | ✅ | ✅ | Ingest conversation text. |
| `search_web` | ✅ | ✅ | Live web search (Bright Data SERP). |
| `scrape_web` | ✅ | ✅ | Scrape URLs to markdown. |
| `visualize_memory_embeddings` | ✅ | ✅ | 2-D map of the memory's topics (clusters of chunk embeddings). |
| `query_memory` | — | ✅ | NL → MongoDB aggregation for exact/structured answers. |
| `deep_search_memory` | — | ✅ | Wide search, results written to disk + a YAML index. |
| `visualize_memory_graph` | — | ✅ | Render the graph as interactive HTML. |
| `memory_dashboard` | — | ✅ | Graph dashboard app. |
| `review_list_pending` / `review_confirm` / `review_reject` | — | ✅ | Human review of flagged duplicate entities. |

**Do not work around a missing tool.** In `rag` there are no edges, so the seven graph tools are not registered at all and calling one returns the standard unknown-tool error. If `query_memory` is absent, the answer is `search_memory` — not a retry. `visualize_memory_embeddings` is the exception that proves the rule: the map has no edges, so it is registered in BOTH modes.

---

## Reading Strategy

### `search_memory` — Default for most queries
- Open-ended or semantic queries (find related things, explore a topic). **Start here when unsure** — the most forgiving tool, and the only reader present in both modes
- Parameters and result differ by mode:

| | `rag` | `graphrag` |
|---|---|---|
| Parameters | `query`, `top_k` (default 10) | `query`, `top_k` (default 10), `max_hops` (default 1), `max_results` (default 10), `visualize` |
| How | hybrid vector + text search (RRF) over **child chunks**, grouped back to their **parent chunks** | the same seed search, then graph expansion over edges |
| Returns | `{"parents": [...], "outcome": "found" \| "nothing_found", "search_mode": "hybrid" \| "text_only" \| "vector_only"}` — each parent has `content`, `heading_path`, `parent_id`, `score`, `matched_children` and the `document` it belongs to (`title`, `source_uri`, `date`) | nodes + edges of the matched subgraph |
| Present it as | passages grouped by document title, quoting the matched children | entities and their relationships |

### `query_memory` — For structured/precise questions (graphrag only)
- Counts, filters, aggregations, specific lookups ("how many tasks does Paul have?") — translates natural language to MongoDB aggregation pipelines via LLM
- Use when `search_memory` is too broad or you need exact answers

### `deep_search_memory` — For broad exploration (graphrag only, progressive disclosure)
- Runs a wider search and saves **all** results to disk. Parameters: `query`, `top_k` (default 50), `max_hops` (default 3), `session_id` (optional)
- Returns a **YAML index** with one-line summaries — NOT the full results. Scan each entry's `context`, then `Read` only the file paths (`file` field, under `directory`) you actually need, and summarize for the user

### Visualization

**`visualize_memory_embeddings` — both modes.** Use it for "what topics are in my memory", "show me a map", "how is my memory organised". It draws the **embedding map**: every child chunk as a point at its stored coordinates, coloured by its cluster, each cluster carrying an LLM-written label. Pass `hulls=true` when the user asks to outline the clusters. Two answers you must relay verbatim rather than paper over:

- `No clustering run found for this user — run make memory-run-clustering-pipeline to build the embedding map.` — say exactly that and offer to run the command. Do NOT fall back to `search_memory` and summarize topics yourself; the user asked for the map.
- A first line reading `N of M chunks have no cluster assignment (or a stale one) — run make memory-run-clustering-pipeline` — repeat that line, then the rest of the answer: the map is real but under-reports the corpus, and `make memory-run-clustering-pipeline` is the fix.

When the answer carries a file path plus a `graphs://` resource link, the client could not render the map inline: share the path if it is on the user's machine, otherwise read the linked resource and save its text as a local `.html` file. Never re-author the HTML.

**Graph visualization — `graphrag` only.** Use `visualize=true` on `search_memory` or `query_memory` when the user asks to see a graph or map out connections between entities. In `rag` there is no graph to draw — present the retrieved parents as text, or draw the embedding map instead.

---

## Search loop

1. **At most 3 `search_memory` calls per user question, and every retry must change the query materially** — different entities or a different angle, never a synonym. Why: a synonym lands in the same embedding neighbourhood and returns the same parents, so the retry spends a call and buys nothing.
   - Good: `"Prefect deployment slots"` → `"free-tier limit five deployments"`.
   - Bad: `"Prefect deployment slots"` → `"Prefect deployment slot"`.
2. **`outcome: "nothing_found"` → at most ONE materially different retry, then answer "Not in memory" naming what you searched.** Never fall through to `search_web` unless the user asked for the web. Why: `nothing_found` means the search ran and matched nothing (a dead leg reports itself in `search_mode`, not as an empty result), so a third phrasing is guessing — and answering from the web passes web text off as the user's own memory.
   - Good: two `nothing_found`s → "Not in memory — I searched the sourdough starter decision and bread baking notes."
   - Bad: two `nothing_found`s → `search_web("sourdough starter")`, presented as what memory holds.
3. **Stop as soon as the answer is covered — 3 is a budget, not a target.** Why: every extra call adds latency and near-duplicate passages the user has to re-read, and a specific question is usually answered by the first call.
   - Good: the first call returns the decision the user asked about → answer and stop.
   - Bad: the first call already answers, and two more run "to be thorough".
4. **`search_mode` other than `"hybrid"` → prefix ONE caveat line naming the leg that was unavailable, offer to retry later, then answer anyway.** For `text_only` use exactly: `Search ran text-only — vector search was unavailable; results may miss semantic matches` (`vector_only` is the mirror case: the text leg was down). Why: those parents are real but partial, and an unflagged partial answer reads as a complete one.
   - Good: caveat line, then the passages, then "I can rerun this once vector search is back."
   - Bad: present the `text_only` passages as all of memory, or refuse to answer at all.
5. **Budget the loop in calls, never in tokens or context size.** Why: you cannot measure your own context spend, so a size cap is unenforceable guesswork, while "at most 3 calls" is checkable in the transcript.
   - Good: "that was my third `search_memory` call — I answer with what I have."
   - Bad: "keep searching until the retrieved passages start to feel too long."

---

## Writing Strategy

Use these tools when the user wants to add content to memory. All three exist in both modes; what gets written differs (`rag`: document + chunk rows; `graphrag`: those plus entities and edges).

**All three answer the same receipt** — `{"source_uri", "duplicate", "document_id", "flow_run_id", "status"}`, plus an echo of the `url` / `file_path` you passed:
- `duplicate: true` — already in memory, NOTHING was submitted: `document_id` names the existing document, `flow_run_id` is null, `status` is `"duplicate"`. Say it is already known, name its `source_uri`, and do not resubmit.
- `duplicate: false` — ONE pipeline run was submitted: report the `source_uri` and the `flow_run_id`, and say memory is being written out-of-band. No counts of any kind come back — never report one. Confirm later with `search_memory` for the new title; a `nothing_found` right after a submit means "not written yet", not "nothing is there".

### `ingest_url` — Ingest a web page
- Pass the URL; the router handles plain web pages, Substack articles and YouTube videos, and the tool returns as soon as the run is submitted
- `source_uri` may differ from the URL you passed (a YouTube link canonicalises) — quote the `source_uri`
- Use when: user shares a URL and wants it added to memory

### `ingest_file` — Ingest a local file
- The server never opens the path: read the file YOURSELF and pass its text as `content` (convert non-text formats to text/markdown first)
- Pass the absolute file path — it is the dedup key (`source_uri` is `file://<path>`) — and an optional title
- Use when: user wants to add a local file to memory

### `ingest_conversation` — Ingest conversation text
- Pass the raw conversation text and optional title
- In `graphrag` it also extracts people, tasks, episodes, preferences and their relationships
- Use when: the user asks to remember a conversation — whole sessions are persisted by the SessionEnd hook, not by you

---

## Presenting Results

- Summarize results in a human-readable way — don't dump raw JSON unless the user asks for it.
- Group by type (people, tasks, episodes, documents) when presenting mixed results; in `rag`, group the parents by their document title, and in `graphrag` highlight the relationships between entities.
- For deep search: present the index summary first, then offer to dive into specific entries.
- On `outcome: "nothing_found"`, say so and name what was searched (see Search loop).

### Errors

Any tool may answer `{"error_type", "retryable", "message"}` instead of its normal result. Act on `retryable`, never on the code string: `true` (transient infrastructure — `search_unavailable`, `pipeline_unavailable`, `storage_unavailable`, `network_error`, …) means retry the SAME call ONCE, then relay `message` and stop; `false` (`invalid_input`, `unsupported_url`, `configuration_error`, …) means change the input or stop — retrying it unchanged fails identically. Always relay `message` when you stop.

---

## Memory Reference

### Node Types (both modes write `document` and `chunk`; the rest are graphrag-only)
- **document** — Source documents (articles, papers, files, conversations)
- **chunk** — Two subtypes: **`parent`** (~4096 tokens, what retrieval returns, never embedded) and **`child`** (~256 tokens, the embedded search unit). Every chunk row carries `parent_id` (its parent chunk's id, or the document's id for a parent) and `chunk_index`.
- **person**, **organization**, **location**, **event**, **object** — POLE+O entities (`object` subtypes include `task`, `project`, `topic`, `software`, …)
- **preference** — User preferences; **fact** — free-form propositions that fit no typed relation (island nodes, no edges)

### Edge Types (graphrag only)
- **part_of** / **next** — structure: child → parent → document, plus sequential order between sibling chunks at both levels
- **mentions** (document → person), **referenced** (document ↔ document), **has** (person → preference)
- **related_to** — the umbrella for LLM-extracted domain relations, discriminated by `semantic_type` (`has_task`, `experienced_by`, `knows`, `employed_by`, `located_at`, `owns`, `uses`, …)
- **same_as** (confirmed duplicate entities), **superseded_by** (bi-temporal supersession between contradictory preferences/facts)

**Source types:** `substack` (articles/RSS), `web` (generic pages), `youtube` (videos), `huggingface` (ArXiv datasets), `file` (via `ingest_file`), `conversation` (via `ingest_conversation`), `latent` (referenced but not yet fully ingested).
