---
name: tree-memory
description: "Query, explore, and write to Tree's knowledge graph memory. Use when the user asks to recall, search, visualize, or ingest information (people, tasks, episodes, preferences, documents, URLs, files, conversations). PROACTIVE USE: Also use this skill to extract the current conversation whenever something meaningful was discussed (technical decisions, debugging sessions, architecture changes, new learnings) or when the user switches to a different topic. When extracting conversations proactively, always run the ingestion in a background agent to avoid blocking the user."
argument-hint: <natural language query or instruction>
allowed-tools: mcp__tree-memory__query_memory, mcp__tree-memory__search_memory, mcp__tree-memory__deep_search_memory, mcp__tree-memory__ingest_url, mcp__tree-memory__ingest_file, mcp__tree-memory__ingest_conversation, Read
---

# Tree Memory

Query, explore, and write to Tree's (Your Rooted Personal Assistant) knowledge graph through the MCP server.

## Instruction

The query or instruction to run is: $ARGUMENTS

If no arguments are provided, ask the user what they want to know or do with Tree's memory.

---

## Reading Strategy

Pick the right tool based on what the user needs:

### `search_memory` — Default for most queries
- Open-ended or semantic queries (find related things, explore a topic)
- Uses vector + text search with RRF fusion, then graph expansion
- **Start here when unsure** — it is the most forgiving tool
- Parameters: `query`, `top_k` (default 10), `max_hops` (default 1), `max_results` (default 10)

### `query_memory` — For structured/precise questions
- Counts, filters, aggregations, specific lookups ("how many tasks does Paul have?")
- Translates natural language to MongoDB aggregation pipelines via LLM
- Use when `search_memory` is too broad or you need exact answers

### `deep_search_memory` — For broad exploration (progressive disclosure)
- Runs a wider search (`top_k=50`, `max_hops=3` by default) and saves **all** results to disk
- Returns a **YAML index** with one-line summaries — NOT the full results
- Use when the user wants to explore a broad topic, map out connections, or needs comprehensive context
- Parameters: `query`, `top_k` (default 50), `max_hops` (default 3), `session_id` (optional)

**Progressive disclosure workflow:**
1. Call `deep_search_memory("topic")` — get back the YAML index
2. Scan the `context` field in each entry to identify what's relevant
3. Use `Read` on individual file paths (from the `file` field, under the `directory` path) to get full details only for entries you need
4. Summarize findings for the user

### Visualization
Use `visualize=true` on `search_memory` or `query_memory` when the user asks to visualize, render, show a graph, or map out connections. This generates an interactive HTML file and opens it in the browser.

---

## Writing Strategy

Use these tools when the user wants to add content to the knowledge graph:

### `ingest_url` — Ingest a web page
- Currently supports Substack articles (including custom domains configured in the app)
- Pass the URL; the tool fetches, extracts text, creates a Document, then runs memory extraction + indexing
- Returns a JSON summary with node/edge counts
- Use when: user shares a URL and wants it added to memory

### `ingest_file` — Ingest a local file
- Supports `.txt`, `.md`, `.html` files
- Pass the absolute file path and optional title
- Returns a JSON summary with node/edge counts
- Use when: user wants to add a local file to memory

### `ingest_conversation` — Ingest conversation text
- Pass the raw conversation text and optional title
- Extracts people, tasks, episodes, preferences, and relationships from the text
- Returns a JSON summary with node/edge counts
- Use when: user wants to remember a conversation, or at the end of a session to persist learnings
- **Proactive use:** Also ingest when meaningful topics were discussed (technical decisions, debugging sessions, architecture changes, new learnings) or when the user switches to a different topic. Always run proactive ingestion in a **background agent** to avoid blocking the user.

### After ingestion
- Confirm what was extracted (node/edge counts) in a human-readable summary
- Optionally run a quick `search_memory` to verify the new content is queryable

---

## Presenting Results

- Summarize results in a human-readable way — don't dump raw JSON unless the user asks for it.
- Group by type (people, tasks, episodes, documents) when presenting mixed results.
- Highlight relationships and connections between entities.
- For deep search: present the index summary first, then offer to dive into specific entries.
- For ingestion: report what was created (document title, node count, edge count).
- If results are empty, suggest rephrasing the query or trying a different tool.

---

## Knowledge Graph Reference

### Node Types
- **person** — People mentioned in documents
- **document** — Source documents (articles, papers, files, conversations)
- **chunk** — Text chunks from documents
- **task** — Tasks and action items
- **episode** — Events and experiences
- **preference** — User preferences

### Edge Types
- **part_of** — Chunk belongs to document
- **next** — Sequential chunk ordering
- **mentions** — Document mentions a person
- **referenced** — Cross-references between documents
- **related_to** — Person-to-person relationship
- **todo** — Person has a task
- **experienced** — Person experienced an episode
- **has** — Person has a preference

### Source Types
- `substack` — Substack articles/RSS
- `huggingface` — HuggingFace datasets (ArXiv)
- `file` — Local files ingested via `ingest_file`
- `conversation` — Conversations ingested via `ingest_conversation`
- `latent` — Referenced but not yet fully ingested
