---
name: tree-memory
description: "Query, explore, and write to Tree's memory. Use when the user asks to recall, search, visualize, or ingest information (people, tasks, episodes, preferences, documents, URLs, files, conversations). PROACTIVE USE: Also use this skill to extract the current conversation whenever something meaningful was discussed (technical decisions, debugging sessions, architecture changes, new learnings) or when the user switches to a different topic. When extracting conversations proactively, always run the ingestion in a background agent to avoid blocking the user."
argument-hint: <natural language query or instruction>
allowed-tools: mcp__tree-memory__query_memory, mcp__tree-memory__search_memory, mcp__tree-memory__deep_search_memory, mcp__tree-memory__ingest_url, mcp__tree-memory__ingest_file, mcp__tree-memory__ingest_conversation, Read
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
| `query_memory` | — | ✅ | NL → MongoDB aggregation for exact/structured answers. |
| `deep_search_memory` | — | ✅ | Wide search, results written to disk + a YAML index. |
| `visualize_memory_graph` | — | ✅ | Render the graph as interactive HTML. |
| `memory_dashboard` | — | ✅ | Graph dashboard app. |
| `review_list_pending` / `review_confirm` / `review_reject` | — | ✅ | Human review of flagged duplicate entities. |

**Do not work around a missing tool.** In `rag` there are no edges, so the seven graph tools are not registered at all and calling one returns the standard unknown-tool error. If `query_memory` is absent, the answer is `search_memory` — not a retry.

---

## Reading Strategy

Pick the right tool based on what the user needs:

### `search_memory` — Default for most queries
- Open-ended or semantic queries (find related things, explore a topic)
- **Start here when unsure** — it is the most forgiving tool, and the only reader present in both modes
- Parameters and result differ by mode:

| | `rag` | `graphrag` |
|---|---|---|
| Parameters | `query`, `top_k` (default 10) | `query`, `top_k` (default 10), `max_hops` (default 1), `max_results` (default 10), `visualize` |
| How | hybrid vector + text search (RRF) over **child chunks**, grouped back to their **parent chunks** | the same seed search, then graph expansion over edges |
| Returns | `{"parents": [...]}` — each entry has `content`, `heading_path`, `parent_id`, `score`, `matched_children` and the `document` it belongs to (`title`, `source_uri`, `date`) | nodes + edges of the matched subgraph |
| Present it as | passages grouped by document title, quoting the matched children | entities and their relationships |

### `query_memory` — For structured/precise questions (graphrag only)
- Counts, filters, aggregations, specific lookups ("how many tasks does Paul have?")
- Translates natural language to MongoDB aggregation pipelines via LLM
- Use when `search_memory` is too broad or you need exact answers

### `deep_search_memory` — For broad exploration (graphrag only, progressive disclosure)
- Runs a wider search (`top_k=50`, `max_hops=3` by default) and saves **all** results to disk
- Returns a **YAML index** with one-line summaries — NOT the full results
- Use when the user wants to explore a broad topic, map out connections, or needs comprehensive context
- Parameters: `query`, `top_k` (default 50), `max_hops` (default 3), `session_id` (optional)

**Progressive disclosure workflow:**
1. Call `deep_search_memory("topic")` — get back the YAML index
2. Scan the `context` field in each entry to identify what's relevant
3. Use `Read` on individual file paths (from the `file` field, under the `directory` path) to get full details only for entries you need
4. Summarize findings for the user

### Visualization (graphrag only)
Use `visualize=true` on `search_memory` or `query_memory` when the user asks to visualize, render, show a graph, or map out connections. This generates an interactive HTML file and opens it in the browser. In `rag` there is nothing to draw — present the retrieved parents as text instead.

---

## Writing Strategy

Use these tools when the user wants to add content to memory. All three exist in both modes; what gets written differs (`rag`: document + chunk rows; `graphrag`: those plus entities and edges).

### `ingest_url` — Ingest a web page
- Currently supports Substack articles (including custom domains configured in the app)
- Pass the URL; the tool fetches, extracts text, creates a Document, then runs the memory pipeline + indexing
- Returns a JSON summary with node/edge counts
- Use when: user shares a URL and wants it added to memory

### `ingest_file` — Ingest a local file
- Supports `.txt`, `.md`, `.html` files
- Pass the absolute file path and optional title
- Returns a JSON summary with node/edge counts
- Use when: user wants to add a local file to memory

### `ingest_conversation` — Ingest conversation text
- Pass the raw conversation text and optional title
- In `graphrag` it also extracts people, tasks, episodes, preferences and their relationships
- Returns a JSON summary with node/edge counts
- Use when: user wants to remember a conversation, or at the end of a session to persist learnings
- **Proactive use:** Also ingest when meaningful topics were discussed (technical decisions, debugging sessions, architecture changes, new learnings) or when the user switches to a different topic. Always run proactive ingestion in a **background agent** to avoid blocking the user.

### After ingestion
- Confirm what was extracted (node/edge counts) in a human-readable summary
- Optionally run a quick `search_memory` to verify the new content is queryable

---

## Presenting Results

- Summarize results in a human-readable way — don't dump raw JSON unless the user asks for it.
- Group by type (people, tasks, episodes, documents) when presenting mixed results; in `rag`, group the parents by their document title.
- Highlight relationships and connections between entities (graphrag).
- For deep search: present the index summary first, then offer to dive into specific entries.
- For ingestion: report what was created (document title, node count, edge count).
- If results are empty, suggest rephrasing the query or trying a different tool.

---

## Memory Reference

### Node Types (both modes write `document` and `chunk`; the rest are graphrag-only)
- **document** — Source documents (articles, papers, files, conversations)
- **chunk** — Text chunks from documents. Two subtypes: **`parent`** (~4096 tokens, what retrieval returns, never embedded) and **`child`** (~256 tokens, the embedded search unit). Every chunk row carries `parent_id` (its parent chunk's id, or the document's id for a parent) and `chunk_index`.
- **person** — People mentioned in documents
- **organization**, **location**, **event**, **object** — POLE+O entities (`object` subtypes include `task`, `project`, `topic`, `software`, …)
- **preference** — User preferences
- **fact** — Free-form propositions that fit no typed relation (island nodes, no edges)

### Edge Types (graphrag only)
- **part_of** — Child chunk → parent chunk, and parent chunk → document
- **next** — Sequential ordering between sibling chunks (at both levels)
- **mentions** — Document mentions a person
- **referenced** — Cross-references between documents
- **related_to** — The umbrella for LLM-extracted domain relations, discriminated by `semantic_type` (`has_task`, `experienced_by`, `knows`, `employed_by`, `member_of`, `located_at`, `owns`, `uses`, …)
- **has** — Person has a preference
- **same_as** — Confirmed duplicate entities
- **superseded_by** — Bi-temporal supersession between contradictory preferences/facts

### Source Types
- `substack` — Substack articles/RSS
- `huggingface` — HuggingFace datasets (ArXiv)
- `file` — Local files ingested via `ingest_file`
- `conversation` — Conversations ingested via `ingest_conversation`
- `latent` — Referenced but not yet fully ingested
