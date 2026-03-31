---
name: twin-memory
description: Query the twin's knowledge graph memory. Use when the user asks to recall, search, or visualize information from their digital twin (people, tasks, episodes, preferences, documents).
argument-hint: <natural language query>
allowed-tools: mcp__twin-memory__query_memory, mcp__twin-memory__search_memory
---

# Twin Memory Query

Query the user's digital twin knowledge graph through the MCP server.

## Strategy

1. **Pick the right tool** based on the query:
   - `query_memory` — for structured questions (counts, filters, aggregations, specific lookups). Translates natural language to MongoDB aggregation pipelines via LLM.
   - `search_memory` — for open-ended or semantic queries (find related things, explore a topic). Uses vector + text search with graph expansion.

2. **Start with `search_memory`** when unsure — it is more forgiving and always returns relevant context. Fall back to `query_memory` for precise or aggregate queries (e.g., "how many tasks does Paul have?").

3. **Use `visualize=true`** when the user asks to visualize, render, show a graph, or map out connections. This generates an interactive HTML file and opens it in the browser.

## Query

The query to run is: $ARGUMENTS

If no arguments are provided, ask the user what they want to know from their twin memory.

## Presenting Results

- Summarize results in a human-readable way — don't dump raw JSON unless the user asks for it.
- Group by type (people, tasks, episodes, documents) when presenting mixed results.
- Highlight relationships and connections between entities.
- If results are empty, suggest rephrasing the query or trying the other tool.

## Node Types in the Knowledge Graph

- **person** — People mentioned in documents
- **document** — Source documents (articles, papers)
- **chunk** — Text chunks from documents
- **task** — Tasks and action items
- **episode** — Events and experiences
- **preference** — User preferences

## Edge Types

- **part_of** — Chunk belongs to document
- **next** — Sequential chunk ordering
- **mentions** — Document mentions a person/entity
- **referenced** — Cross-references between entities
- **related_to** — General relationship
- **todo** — Person has a task
- **experienced** — Person experienced an episode
- **has** — Person has a preference
