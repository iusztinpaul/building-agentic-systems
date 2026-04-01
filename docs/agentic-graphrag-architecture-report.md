# Agentic GraphRAG Architecture Report

> Comprehensive E2E technical report for blog/LinkedIn content about building a digital twin with knowledge graphs, FastMCP, and Claude Code.

---

## 1. Data Pipelines (ETL Layer)

**Purpose:** Gather content from multiple sources, normalize it into a unified `documents` MongoDB collection.

### Architecture

All pipelines inherit from `BaseETL` (`src/twin/data/core/base.py`), which defines the contract:
- `extract_one(raw_entry) -> Document` -- transform raw data
- `run(source_uri) -> list[Document]` -- fetch, extract, persist
- `run_batch(source_uris)` -- process multiple sources

Each pipeline is orchestrated via **Prefect flows** with retry logic and task-level checkpointing.

### Supported Sources (5 pipelines)

| Source | Pipeline File | Fetch Method | Key Detail |
|--------|--------------|-------------|------------|
| **Substack RSS** | `data/substack/substack_rss.py` | `httpx` + `feedparser` | Parses RSS XML, extracts HTML content, converts to plain text via BeautifulSoup |
| **Substack Articles** | `data/substack/substack_article.py` | Direct HTTP GET | Scrapes `og:title`, `og:description`, `article:published_time` from meta tags; extracts body from `div.body` or `<article>` |
| **ArXiv (HuggingFace)** | `data/huggingface/arxiv_dataset.py` | HuggingFace `datasets` streaming API | Streams `librarian-bots/arxiv-metadata-snapshot`; optional HTML content fetch from `arxiv.org/html/` |
| **Local Files** | `data/file.py` | Filesystem read | Supports `.txt`, `.md`, `.html`; HTML auto-converted to plain text |
| **Conversations** | `data/conversation.py` | Direct text input | UUID-based `source_uri`; always unique (no dedup) |

### Document Data Model (`src/twin/entities/documents.py`)

```python
class Document(BeanieDocument):
    source_type: SourceType        # SUBSTACK | HUGGINGFACE | FILE | CONVERSATION | LATENT
    source_uri: Indexed(str, unique=True)  # Deduplication key
    title: str | None
    summary: str | None
    content: str | None
    authors: list[str] = []
    date: datetime | None          # Always timezone-aware (UTC)
    references: list[Link["Document"]] = []  # Citation graph
```

### Key Design Patterns

- **Deduplication via `source_uri`**: Unique index prevents re-ingestion
- **LATENT documents**: When Article A references URL B that hasn't been ingested yet, a placeholder LATENT document is created for B. When B is later ingested, the LATENT is upgraded with real content
- **Reference graph**: `references` field tracks cross-document citations, forming an implicit citation network before graph extraction even runs
- **Idempotent upserts**: Re-running a pipeline on the same source is safe

### URL Ingestion Router (`data/core/ingest.py`)

Registry-based dispatcher that routes URLs to the correct pipeline automatically. Static patterns (e.g., `substack.com`) plus custom domains from config. This is what the MCP `ingest_url` tool uses under the hood.

---

## 2. Memory Extraction Pipeline

**Purpose:** Transform raw `documents` into a structured knowledge graph of nodes and edges in the `knowledge_graph` collection.

**File:** `src/twin/memory/extraction/core.py`  
**Prefect flow:** `src/twin/memory/extraction/pipeline.py`

### 5-Stage Pipeline

```
Document.content
    |
    v
[1] chunk_document()          -- Split into 512-token chunks (64-token overlap, tiktoken cl100k_base)
    |
    v
[2] extract_entities()        -- LLM (Gemini) extracts nodes & edges from each chunk (5 concurrent)
    |                            System prompt includes full ontology schema as JSON
    |                            Returns: person, task, episode, preference nodes
    |                            Returns: related_to, todo, experienced, has edges
    v
[3] build_structural_entries() -- Deterministic (no LLM):
    |                              DOCUMENT node for the source
    |                              CHUNK nodes for each text chunk (with full text in properties.content)
    |                              PART_OF edges: chunk -> document
    |                              NEXT edges: chunk[i] -> chunk[i+1] (sequential ordering)
    |                              MENTIONS edges: document -> person
    |                              REFERENCED edges: document -> document (from references list)
    v
[4] normalize_nodes()         -- Two-phase deduplication:
    |                            Phase A (in-memory): Fuzzy match via SequenceMatcher (threshold 0.85)
    |                            Phase B (cross-document): Query MongoDB for existing nodes, merge
    |                            Edge remapping: Update endpoints to canonical names
    v
[5] upsert_graph_entries()    -- Bulk upserts with MongoDB aggregation pipeline updates
                                 Merges properties (new wins), unions sources array, unions aliases
                                 Idempotent: running twice produces the same result
```

### Ontology Schema (`src/twin/entities/ontology.py`)

**Node types (6):**
| Type | Properties | LLM-Extractable? |
|------|-----------|-------------------|
| `document` | source_type, source_uri, date | No (structural) |
| `chunk` | source_type, source_uri, content, date | No (structural) |
| `person` | aliases, email | Yes |
| `task` | content, date | Yes |
| `episode` | content, date | Yes |
| `preference` | content | Yes |

**Edge types (8):**
| Type | Source -> Target | LLM-Extractable? |
|------|-----------------|-------------------|
| `part_of` | chunk -> document | No (structural) |
| `next` | chunk -> chunk | No (structural) |
| `mentions` | document -> person | No (structural) |
| `referenced` | document -> document | No (structural) |
| `related_to` | person -> person | Yes |
| `todo` | person -> task | Yes |
| `experienced` | person -> episode | Yes |
| `has` | person -> preference | Yes |

**Edge constraints** are enforced programmatically -- the LLM can't create an edge that violates the ontology (e.g., a `todo` edge from `episode` to `document`).

---

## 3. Knowledge Graph Data Model

**Collection:** Single `knowledge_graph` MongoDB collection  
**File:** `src/twin/entities/knowledge_graph.py`

### Single-Collection Design

Nodes and edges coexist in one collection, discriminated by the `kind` field:

**Node document:**
```json
{
  "_id": "person:paul iusztin",       // Composite string ID: "type:name"
  "kind": "node",
  "type": "person",
  "name": "paul iusztin",
  "properties": {"aliases": ["paul"], "email": null},
  "embedding": [0.12, -0.34, ...],    // 384-dim vector (empty until indexed)
  "sources": [ObjectId("...")],        // Which documents contributed this node
  "created_at": "2025-...",
  "updated_at": "2025-..."
}
```

**Edge document:**
```json
{
  "_id": "person:paul iusztin|todo|task:write blog post",  // "source|type|target"
  "kind": "edge",
  "type": "todo",
  "source_node_id": "person:paul iusztin",
  "source_type": "person",
  "target_node_id": "task:write blog post",
  "target_type": "task",
  "properties": {},
  "sources": [ObjectId("...")],
  "created_at": "2025-...",
  "updated_at": "2025-..."
}
```

### Why Single Collection?

This enables MongoDB's `$graphLookup` for multi-hop traversal within one collection, simplifies indexing (one text index, one vector index), and enables atomic upserts with composite string IDs that are human-readable and deterministic.

---

## 4. Indexing Pipeline

**Purpose:** Generate embeddings for all nodes and create search indexes.  
**File:** `src/twin/memory/indexing/core.py`  
**Prefect flow:** `src/twin/memory/indexing/pipeline.py`

### Step 1: Node Embedding (`embed_nodes()`)

- Finds all nodes where `embedding` is empty (`[]` or `None`)
- Builds a text representation per node:
  ```
  {type}: {_id}
  {property_key}: {property_value}
  {content}   // appended last if present
  ```
- Batch processing (default 64 nodes per batch)
- Embedding model: Sentence Transformers `all-MiniLM-L6-v2` (384 dimensions, runs locally)

### Step 2: Search Index Creation (`ensure_indexes()`)

Creates **two indexes** on the `knowledge_graph` collection:

1. **Text Index** (standard MongoDB):
   - Fields: `name`, `properties.content`, `properties.aliases`
   - Enables `$text` queries for keyword/word-level search

2. **Vector Search Index** (MongoDB Atlas / `mongot`):
   - Field: `embedding`
   - Similarity: `cosine`
   - Dimensions: 384
   - Waits up to 60s for `mongot` to sync

---

## 5. Query Logic (3 Strategies)

**Files:** `src/twin/memory/query/core.py`, `src/twin/memory/query/nl_query.py`

### Strategy 1: Hybrid Search + Graph Expansion (`search_memory`)

**Two-phase retrieval:**

**Phase 1 -- Seed Node Discovery (`search_nodes()`):**
- **Vector search**: `$vectorSearch` aggregation stage on `vector_index`, cosine similarity
- **Text search**: `$text` operator on the text index (word-level, stemmed)
- **RRF Fusion**: Reciprocal Rank Fusion combines both rankings:
  ```
  score(doc) = sum(1 / (k + rank + 1)) across all result lists
  ```
  Default `k=60`. No absolute weighting -- purely rank-based fusion.

**Phase 2 -- Graph Expansion (`expand_graph()`):**
- Bidirectional `$graphLookup` from seed nodes:
  - Outgoing: node._id -> edge.source_node_id -> follow edge.target_node_id
  - Incoming: node._id -> edge.target_node_id -> follow edge.source_node_id
- Configurable depth (default 1 hop)
- Deduplicates edges, hydrates all discovered nodes
- Returns `QueryResult(nodes, edges)`

### Strategy 2: Natural Language Query (`query_memory`)

- LLM (Gemini) translates natural language to a MongoDB aggregation pipeline
- System prompt is **dynamically generated** from the ontology registries -- includes all node types, edge types, constraints, index info, and vector search syntax
- Uses an `__EMBED__` placeholder that gets replaced with actual embedding vectors at execution time

**Safety validation (`validate_pipeline()`):**
- Whitelist of 19 allowed aggregation stages
- Blocks all write operations (`$out`, `$merge`, `$delete`, `$drop`, etc.)
- `$lookup`/`$graphLookup` must target `knowledge_graph` collection only
- Auto-injects `$limit` if missing
- Strips `embedding` field from output

**Self-correction:** On pipeline validation or execution error, feeds the error back to the LLM for a retry (default 1 retry).

### Strategy 3: Deep Search (`deep_search_memory`)

- Broad exploration with larger defaults (`top_k=50`, `max_hops=3`)
- **Progressive disclosure**: Writes individual markdown files per node/edge to `.twin/{session_id}/`
- Returns a lightweight **YAML index** with one-line summaries
- Claude Code reads individual files on-demand based on the index

---

## 6. MCP Server

**Framework:** FastMCP  
**File:** `src/twin/mcp/server.py`  
**Entry point:** `scripts/serve_mcp.py`

### Lifespan Management

```python
@lifespan
async def app_lifespan(server: FastMCP):
    # 1. Initialize MongoDB connection (async)
    # 2. Load LLM (Gemini) and embedding model (Sentence Transformers)
    # 3. Ensure MongoDB indexes exist
    # 4. Yield context dict to all tool handlers
    # 5. Cleanup: close MongoDB client on shutdown
```

The server name is `"Twin Memory"` with instructions that guide Claude on how to use the tools.

### Transport

Configured via `.mcp.json` at project root:
```json
{
  "mcpServers": {
    "twin-memory": {
      "command": "uv",
      "args": ["run", "python", "scripts/serve_mcp.py"],
      "env": { "ENV_FILE_PATH": ".env" }
    }
  }
}
```

Default transport: **stdio** (Claude Code spawns the server as a subprocess).

---

## 7. MCP Server Tools

**File:** `src/twin/mcp/tools.py`

### Search/Read Tools (3)

| Tool | Purpose | Key Parameters | Returns |
|------|---------|---------------|---------|
| `query_memory` | NL -> MongoDB pipeline | `query`, `visualize=False`, `max_results=10` | Serialized JSON (embeddings stripped) |
| `search_memory` | Hybrid search + graph expansion | `query`, `top_k=10`, `max_hops=1`, `max_results=10`, `visualize=False` | Serialized JSON (embeddings stripped) |
| `deep_search_memory` | Broad exploration, saves to disk | `query`, `top_k=50`, `max_hops=3`, `session_id=None` | YAML index with file paths |

### Write/Ingest Tools (3)

| Tool | Purpose | Key Parameters | Returns |
|------|---------|---------------|---------|
| `ingest_url` | Fetch + ingest web content | `url` | JSON summary (node/edge counts) |
| `ingest_file` | Ingest local files (.txt, .md, .html) | `file_path`, `title=None` | JSON summary (node/edge counts) |
| `ingest_conversation` | Extract knowledge from conversation text | `conversation_text`, `title=None` | JSON summary (node/edge counts) |

### Ingestion Pipeline (`src/twin/mcp/ingest.py`)

When an ingestion tool is called, it runs the **full pipeline inline** (not via Prefect):
1. Data pipeline: fetch + extract + load -> `Document`
2. Memory extraction: chunk + LLM extract + structural entries + normalize + upsert
3. Indexing: embed new nodes

This means a single `ingest_url` call goes from raw URL to queryable knowledge graph in one step.

---

## 8. Skills

**File:** `.claude/skills/twin-memory/SKILL.md`

The `twin-memory` skill is a Claude Code skill that provides structured guidance on **when and how to use each MCP tool**:

### Reading Strategy (decision tree)
- **`search_memory`** -- Default for most queries. Semantic + text search with graph expansion. "Start here when unsure."
- **`query_memory`** -- For structured/precise questions (counts, filters, aggregations). Uses LLM-to-pipeline translation.
- **`deep_search_memory`** -- For broad exploration. Progressive disclosure: returns YAML index, then read individual files.

### Writing Strategy
- **`ingest_url`** -- When user shares a URL
- **`ingest_file`** -- When user wants to add a local file
- **`ingest_conversation`** -- At end of session or when user wants to persist conversation knowledge

### Presentation Guidelines
- Summarize results in human-readable format (never dump raw JSON)
- Group by entity type (people, tasks, episodes, documents)
- Highlight relationships between entities
- For deep search: present index first, offer to dive deeper

---

## 9. Claude Code Connection

### How Claude Code connects to the MCP server

1. **`.mcp.json`** at project root declares the `twin-memory` MCP server
2. Claude Code reads this config and spawns the server as a subprocess via `uv run python scripts/serve_mcp.py`
3. Communication happens over **stdio** (JSON-RPC)
4. The server's lifespan initializes MongoDB, LLM, and embedding model
5. Claude Code discovers the 6 tools and can call them during conversation

### Stop Hook (Auto-Ingestion)

**File:** `.claude/settings.json`

A **Stop hook** runs when Claude Code finishes a response:
```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "...checks sentinel file...if not ingested, blocks with: 'Please run: /twin-memory extract current conversation'"
      }]
    }]
  }
}
```

This ensures that **every conversation gets ingested into the knowledge graph** before the session ends. It uses a sentinel file (`/tmp/.claude-twin-ingested-{SESSION_ID}`) to only prompt once per session.

### Skill Integration

The `twin-memory` skill is registered in `.claude/skills/twin-memory/SKILL.md` and can be invoked via `/twin-memory <query>`. It provides Claude with the decision tree for choosing the right tool.

---

## 10. E2E Example: Ingesting Decoding AI Articles via RSS

Here's the complete flow when the configured RSS feed `https://www.decodingai.com/feed` is processed:

### Step 0: Start Fresh (Empty Collections)

```bash
mongosh -u twin -p twin --authenticationDatabase admin twin \
  --eval 'db.documents.drop(); db.knowledge_graph.drop(); print("Collections cleared.")'
```

Both `documents` and `knowledge_graph` collections are now empty. The pipelines will recreate indexes as needed.

### Step 1: Trigger the Data Pipeline

```bash
make serve-workflows &        # Start Prefect worker
make run-substack-rss-data-pipeline  # Trigger RSS ingestion
```

Or via MCP: Claude Code calls `ingest_url("https://www.decodingai.com/p/some-article")` which routes through the URL ingestion router.

### Step 2: Fetch & Parse RSS Feed

```
https://www.decodingai.com/feed
    |-- httpx GET (30s timeout)
    |-- feedparser parses XML
    |-- Returns ~20 entry objects with title, summary, content, author, date, links
```

### Step 3: Extract Documents

For each RSS entry:
```
Raw entry
    |-- title: "The Role of Feature Stores in ML Systems"
    |-- author: "Paul Iusztin"  
    |-- published: "Mon, 15 Jan 2025 10:00:00 GMT" -> datetime(2025, 1, 15, 10, 0, 0, UTC)
    |-- summary: "Feature stores bridge the gap between..."
    |-- content[0].value: "<div><p>In production ML systems...</p>...</div>"
    |                        |-- BeautifulSoup strips HTML tags
    |                        |-- Preserves block structure (newlines around p, div, h1-6)
    |                        `-- Returns clean plain text
    |-- source_uri: "https://www.decodingai.com/p/the-role-of-feature-stores"
    `-> Document(source_type=SUBSTACK, source_uri=..., title=..., content=..., authors=["Paul Iusztin"], date=...)
```

### Step 4: Load Documents (Dedup + References)

```
Document
    |-- Check: Does source_uri already exist (non-LATENT)? -> Skip if yes
    |-- Extract URLs from HTML content -> ["https://arxiv.org/abs/2309.xxxxx", ...]
    |-- For each referenced URL:
    |     |-- Already in DB? -> Use existing document
    |     `-- New? -> Create LATENT Document(source_type=LATENT, source_uri=url)
    |-- doc.references = [ref1, ref2, ...]
    |-- If LATENT exists for this source_uri -> Upgrade with real content
    `-- Upsert to MongoDB `documents` collection
```

### Step 5: Memory Extraction

```bash
make run-memory-pipeline-extraction
```

For the article "The Role of Feature Stores in ML Systems":

```
Document.content (5000 tokens)
    |
    |-- [Chunking] -> 10 chunks of ~512 tokens each (64 overlap)
    |
    |-- [LLM Extraction] (5 chunks in parallel via Gemini)
    |   Chunk 1: "Paul Iusztin discusses how feature stores..."
    |     -> nodes: [{name: "paul iusztin", type: "person"},
    |                {name: "feature stores in ml", type: "episode", content: "..."}]
    |     -> edges: [{source: "paul iusztin", target: "feature stores in ml", type: "experienced"}]
    |
    |   Chunk 4: "The author prefers Feast for its simplicity..."
    |     -> nodes: [{name: "feast preference", type: "preference", content: "prefers Feast for simplicity"}]
    |     -> edges: [{source: "paul iusztin", target: "feast preference", type: "has"}]
    |
    |-- [Structural Entries]
    |   -> DOCUMENT node: "document:https://www.decodingai.com/p/the-role-of-feature-stores"
    |   -> 10 CHUNK nodes: "chunk:https://...#chunk-0" through "chunk:...#chunk-9"
    |   -> 10 PART_OF edges: chunk -> document
    |   -> 9 NEXT edges: chunk[0] -> chunk[1] -> ... -> chunk[9]
    |   -> 1 MENTIONS edge: document -> "person:paul iusztin"
    |   -> N REFERENCED edges: document -> referenced documents
    |
    |-- [Normalization]
    |   "paul iusztin" fuzzy matches existing "person:paul iusztin" (score 1.0)
    |   -> Merges into existing node, unions sources array
    |   "feature stores" vs "feature store" (score 0.93 > 0.85) -> Merged
    |
    `-- [Upsert] -> Bulk write to knowledge_graph collection
         Properties merged (new wins), sources array unioned, aliases unioned
```

### Step 6: Indexing

```bash
make run-memory-pipeline-indexing
```

```
All nodes with empty embeddings
    |-- Build text representation:
    |   "person: person:paul iusztin\naliases: ['paul']\n"
    |   "chunk: chunk:https://...#chunk-3\nsource_type: substack\ncontent: In production ML systems..."
    |
    |-- Sentence Transformers all-MiniLM-L6-v2 (local, 384 dims)
    |-- Batch of 64 nodes at a time
    `-- Update embedding field in MongoDB
    
Ensure indexes:
    |-- Text index on (name, properties.content, properties.aliases)
    `-- Vector search index on embedding (cosine, 384 dims)
```

### Step 7: Query via Claude Code

User in Claude Code: "What does Paul Iusztin think about harness engineering?"

Claude Code invokes the `twin-memory` skill, which calls `search_memory`:

```
query: "What does Paul Iusztin think about harness engineering?"
    |
    |-- [Vector Search] embed query -> cosine similarity on vector_index
    |   Top hits: "person:paul iusztin" (0.82), "episode:harness engineering practices" (0.79), ...
    |
    |-- [Text Search] $text: "paul iusztin harness engineering"
    |   Top hits: "chunk:...#chunk-1" (score 3.2), "person:paul iusztin" (score 2.8), ...
    |
    |-- [RRF Fusion] Combine rankings
    |   #1: "person:paul iusztin" (appeared in both lists, high combined score)
    |   #2: "episode:harness engineering practices"
    |   #3: "chunk:...#chunk-1"
    |
    |-- [Graph Expansion] 1-hop from seed nodes
    |   From "person:paul iusztin":
    |     -> "has" -> "preference:harness driven development"
    |     -> "experienced" -> "episode:harness engineering practices"
    |     -> "todo" -> "task:build evaluation harness"
    |   From "episode:harness engineering practices":
    |     -> "experienced" (reverse) -> "person:paul iusztin"
    |
    `-- QueryResult(nodes=[...], edges=[...])
        -> Claude summarizes: "Paul Iusztin has experience with harness engineering practices.
           He prefers harness-driven development and has a task to build an evaluation harness..."
```

### Alternative: Real-Time Ingestion via MCP

During a Claude Code conversation:
```
User: "Ingest this article: https://www.decodingai.com/p/the-role-of-feature-stores"

Claude Code -> ingest_url("https://www.decodingai.com/p/the-role-of-feature-stores")
    |-- URL router matches "decodingai.com" -> Substack article pipeline
    |-- Fetch HTML, extract metadata, parse body
    |-- Load document to MongoDB
    |-- Run extraction inline (chunk + LLM + structural + normalize + upsert)
    |-- Run embedding inline (embed new nodes)
    `-- Returns: {"title": "The Role of Feature Stores", "nodes": 15, "edges": 22}

Claude: "Ingested 'The Role of Feature Stores in ML Systems' - extracted 15 nodes and 22 edges."
```

### Auto-Ingestion of Conversations (Stop Hook)

At the end of every Claude Code session:
```
Stop hook fires -> checks sentinel file
    |-- First time this session? -> Block: "Please run: /twin-memory extract current conversation"
    |-- User runs: /twin-memory extract current conversation
    |-- Claude calls ingest_conversation(conversation_text)
    |-- Extracts people, tasks, episodes, preferences from the conversation
    |-- Knowledge graph grows with every conversation
    `-- Sentinel file created -> won't prompt again this session
```

---

## Architecture Diagram

```
                    ┌──────────────────────────────────────────────────┐
                    │              Claude Code (CLI / IDE)             │
                    │                                                  │
                    │  Skills: /twin-memory                           │
                    │  Hooks: Stop -> auto-ingest conversations       │
                    └─────────────────┬────────────────────────────────┘
                                      │ stdio (JSON-RPC)
                                      │
                    ┌─────────────────▼────────────────────────────────┐
                    │           FastMCP Server ("Twin Memory")         │
                    │                                                  │
                    │  Search:   query_memory | search_memory          │
                    │            deep_search_memory                    │
                    │  Write:    ingest_url | ingest_file              │
                    │            ingest_conversation                   │
                    └────────┬──────────────────┬──────────────────────┘
                             │                  │
              ┌──────────────▼──────┐    ┌──────▼───────────────────┐
              │   Query Engine      │    │   Ingestion Pipeline     │
              │                     │    │                          │
              │ NL->Pipeline (LLM)  │    │ URL Router -> ETL       │
              │ Hybrid Search (RRF) │    │ Chunk -> LLM Extract    │
              │ Graph Expansion     │    │ Structural Entries       │
              │ Deep Search         │    │ Normalize + Upsert       │
              └────────┬────────────┘    │ Embed + Index            │
                       │                 └──────┬──────────────────┘
                       │                        │
              ┌────────▼────────────────────────▼──────────────────┐
              │              MongoDB (Single Instance)             │
              │                                                    │
              │  documents collection         knowledge_graph      │
              │  ├─ source_uri (unique)       ├─ _id: "type:name"  │
              │  ├─ content, title            ├─ kind: node|edge   │
              │  ├─ authors, date             ├─ embedding[384]    │
              │  └─ references[]              ├─ text_index         │
              │                               └─ vector_index       │
              └────────────────────────────────────────────────────┘

              ┌────────────────────────────────────────────────────┐
              │              External Services                     │
              │                                                    │
              │  Gemini 2.5 Flash Lite -- LLM (extraction, NL)    │
              │  Sentence Transformers -- Embeddings (local)       │
              │  Prefect -- Workflow orchestration (batch ETLs)    │
              └────────────────────────────────────────────────────┘
```

---

## Tech Stack Summary

| Layer | Technology |
|-------|-----------|
| **MCP Framework** | FastMCP (stdio transport) |
| **LLM** | Google Gemini 2.5 Flash Lite |
| **Embeddings** | Sentence Transformers all-MiniLM-L6-v2 (384d, local) |
| **Database** | MongoDB (single instance with mongot for vector search) |
| **ODM** | Beanie (async Pydantic ODM for MongoDB) |
| **Orchestration** | Prefect (10 deployments for batch pipelines) |
| **Agent Platform** | Claude Code (CLI + IDE extensions) |
| **Search** | Hybrid: text index + vector search + RRF fusion + $graphLookup |
| **Config** | Pydantic Settings (.env) + YAML app config |
| **Python** | 3.14, fully async, uv package manager |
