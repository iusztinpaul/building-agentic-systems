# Immutable Log + Materialized View: A Two-Collection GraphRAG Architecture

This document describes the **event-sourcing-inspired** architecture for storing and querying a knowledge graph in MongoDB. The design uses two collections: an **append-only log** (`knowledge_graph_log`) that records every extraction event as an immutable entry, and a **materialized view** (`knowledge_graph`) that is periodically rebuilt from the log into a query-optimized, deduplicated graph. This pattern cleanly separates the write path (raw observations) from the read path (aggregated, indexed, embeddable graph), at the cost of operational complexity and RAM pressure during materialization.

> **Note:** This architecture was the initial design and is documented here for educational purposes. The system was later migrated to a simpler single-collection mutable approach with in-place upserts.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Ontology: Node Types, Edge Types, and Constraints](#2-ontology-node-types-edge-types-and-constraints)
3. [Data Model: Log Collection (`knowledge_graph_log`)](#3-data-model-log-collection-knowledge_graph_log)
4. [Data Model: Materialized Collection (`knowledge_graph`)](#4-data-model-materialized-collection-knowledge_graph)
5. [Extraction Pipeline](#5-extraction-pipeline)
6. [Materialization Pipeline](#6-materialization-pipeline)
7. [Retrieval Strategy](#7-retrieval-strategy)
8. [MongoDB Operational Considerations](#8-mongodb-operational-considerations)
9. [Pros and Cons](#9-pros-and-cons)
10. [Configuration Reference](#10-configuration-reference)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  WRITE PATH                                READ PATH                    │
│                                                                         │
│  ┌──────────┐    ┌──────────────────┐    ┌──────────────────┐           │
│  │documents │    │knowledge_graph_  │    │ knowledge_graph  │           │
│  │collection│    │      log         │    │  (materialized)  │           │
│  └────┬─────┘    └───────┬──────────┘    └───────┬──────────┘           │
│       │                  │                       │                      │
│       │  Extraction      │  Materialization      │  Query               │
│       │  Pipeline        │  Pipeline             │  Layer               │
│       │                  │                       │                      │
│       ▼                  ▼                       ▼                      │
│  chunk → LLM       aggregate →           vector search                  │
│  extract →         dedup →               + text search                  │
│  normalize →       $out →                → RRF fusion                   │
│  insert_many       embed →               → graph expansion              │
│                    reverse edges →        → hydrate nodes                │
│                    create indexes                                        │
│                                                                         │
│  Append-only           Rebuilt on             Reads from                 │
│  (source of truth)     each run               materialized view         │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

The architecture follows an **event-sourcing** analogy. The log collection is the **event store** -- the source of truth that records every observation (node or edge) extracted from every chunk of every document. The materialized collection is the **read model** -- a query-optimized projection rebuilt from the log via a MongoDB aggregation pipeline. The log is append-only; the materialized view is disposable and can be rebuilt at any time.

---

## 2. Ontology: Node Types, Edge Types, and Constraints

The ontology defines what the knowledge graph can contain and how entities relate to each other.

### 2.1 Node Types and Property Schemas

| Node Type | Enum Value | Property Fields | Created By |
|-----------|------------|-----------------|------------|
| **DOCUMENT** | `"document"` | `source_type: str`, `source_uri: str`, `date: str \| None` | Pipeline |
| **CHUNK** | `"chunk"` | `source_type: str`, `source_uri: str`, `content: str`, `date: str \| None` | Pipeline |
| **PERSON** | `"person"` | `aliases: list[str]`, `email: str \| None` | LLM |
| **TASK** | `"task"` | `content: str`, `date: str \| None` | LLM |
| **EPISODE** | `"episode"` | `content: str`, `date: str \| None` | LLM |
| **PREFERENCE** | `"preference"` | `content: str` | LLM |

### 2.2 Edge Types and Constraints

| Edge Type | Source Type | Target Type | Description | Created By |
|-----------|-------------|-------------|-------------|------------|
| **PART_OF** | CHUNK | DOCUMENT | Chunk belongs to a document | Pipeline |
| **NEXT** | CHUNK | CHUNK | Sequential ordering between chunks | Pipeline |
| **MENTIONS** | DOCUMENT | PERSON | Document mentions a person | Pipeline |
| **REFERENCED** | DOCUMENT | DOCUMENT | Document references another document | Pipeline |
| **RELATED_TO** | PERSON | PERSON | Two people are related or connected | LLM |
| **TODO** | PERSON | TASK | Person has a task or project to do | LLM |
| **EXPERIENCED** | PERSON | EPISODE | Person experienced a life or work episode | LLM |
| **HAS** | PERSON | PREFERENCE | Person has a preference or opinion | LLM |

Every edge must satisfy its constraint: the source and target node types must match exactly. These constraints are enforced during extraction parsing -- the LLM may hallucinate invalid edges, and the parser silently drops them.

### 2.3 LLM-Extractable vs Structural Types

The ontology splits into two categories:

- **LLM-Extractable types** are entities and relationships the LLM discovers from text:
  - Node types: PERSON, TASK, EPISODE, PREFERENCE
  - Edge types: RELATED_TO, TODO, EXPERIENCED, HAS

- **Structural types** are created deterministically by the pipeline (no LLM involved):
  - Node types: DOCUMENT, CHUNK
  - Edge types: PART_OF, NEXT, MENTIONS, REFERENCED

The LLM system prompt only includes extractable types. This prevents the LLM from generating structural entries that the pipeline already handles, avoiding conflicts and duplication.

The ontology schema is built at runtime via `get_ontology_schema()`, which constructs a JSON description from the Pydantic property models and edge constraints, then injects it into the LLM system prompt.

---

## 3. Data Model: Log Collection (`knowledge_graph_log`)

The log collection stores raw extraction events. Every time a document is processed, new entries are appended -- **never updated or deleted**. This immutability is enforced at the application level (the code only calls `insert_many`, never `update` or `delete`), not by MongoDB itself.

### 3.1 Base Entry: `KnowledgeGraphLogEntry`

The base Beanie document uses a **discriminated union** pattern (`is_root = True` in Settings) so that nodes and edges coexist in the same collection with different schemas:

| Field | Type | Description |
|-------|------|-------------|
| `_id` | `ObjectId` | Auto-generated MongoDB identifier |
| `kind` | `str` (indexed) | Discriminator: `"node"` or `"edge"` |
| `properties` | `dict[str, Any]` | Arbitrary key-value pairs |
| `source_document_id` | `ObjectId` (indexed) | The document this entry was extracted from |
| `chunk_id` | `str` | UUID linking to the specific chunk (provenance) |
| `created_at` | `datetime` (UTC) | When this entry was created |

### 3.2 NodeLogEntry

Extends the base with node-specific fields:

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"node"` (literal) | Always `"node"` |
| `name` | `str` (indexed) | Canonical lowercase name of the entity |
| `type` | `NodeType` | One of the six node types |

**Sample document:**

```json
{
  "_id": ObjectId("6651a1..."),
  "kind": "node",
  "name": "paul iusztin",
  "type": "person",
  "properties": {
    "aliases": ["paul", "pauliusztin"],
    "email": null
  },
  "source_document_id": ObjectId("6650f3..."),
  "chunk_id": "a3b7c9d1-e2f4-4a5b-8c6d-7e8f9a0b1c2d",
  "created_at": ISODate("2026-03-01T12:00:00Z")
}
```

### 3.3 EdgeLogEntry

Extends the base with edge-specific fields:

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"edge"` (literal) | Always `"edge"` |
| `source_node_id` | `str` (indexed) | Name of the source node (bare, no type prefix) |
| `source_type` | `NodeType` | Type of the source node |
| `target_node_id` | `str` (indexed) | Name of the target node (bare, no type prefix) |
| `target_type` | `NodeType` | Type of the target node |
| `type` | `EdgeType` | The relationship type |

**Sample document:**

```json
{
  "_id": ObjectId("6651a2..."),
  "kind": "edge",
  "source_node_id": "paul iusztin",
  "source_type": "person",
  "target_node_id": "write a book on agentic systems",
  "target_type": "task",
  "type": "todo",
  "properties": {},
  "source_document_id": ObjectId("6650f3..."),
  "chunk_id": "a3b7c9d1-e2f4-4a5b-8c6d-7e8f9a0b1c2d",
  "created_at": ISODate("2026-03-01T12:00:00Z")
}
```

**Important:** At the log stage, node identifiers are bare names (e.g., `"paul iusztin"`). The type prefix (e.g., `"person:paul iusztin"`) is only added during materialization.

---

## 4. Data Model: Materialized Collection (`knowledge_graph`)

The materialized collection is rebuilt from the log on each materialization run. It contains **deduplicated, aggregated** nodes and edges ready for search and traversal.

### 4.1 `KnowledgeGraphEntry` -- The Unified Model

A single Beanie document model handles both nodes and edges. Since `$out` bypasses Beanie's ORM layer entirely, the model uses permissive types:

| Field | Type | Used By | Description |
|-------|------|---------|-------------|
| `_id` | `Any` | Both | **Nodes:** `"type:name"` (str). **Edges:** `{source_node_id, target_node_id, type}` (dict) |
| `kind` | `str` (indexed) | Both | `"node"` or `"edge"` |
| `type` | `NodeType \| EdgeType` | Both | The specific type |
| `name` | `str \| None` | Nodes | Canonical name |
| `properties` | `dict[str, Any]` | Both | Merged properties from all log entries |
| `embedding` | `list[float]` | Nodes | Vector embedding (empty until materialization embeds it) |
| `source_node_id` | `str \| None` | Edges | Type-prefixed source node ID (e.g., `"person:alice"`) |
| `source_type` | `NodeType \| None` | Edges | Type of the source node |
| `target_node_id` | `str \| None` | Edges | Type-prefixed target node ID |
| `target_type` | `NodeType \| None` | Edges | Type of the target node |
| `direction` | `str \| None` | Edges | `"reverse"` for synthetic bidirectional edges |
| `sources` | `list[ObjectId]` | Both | Which source documents contributed to this entry |
| `created_at` | `datetime` | Both | Earliest observation (`$min` of log timestamps) |
| `updated_at` | `datetime` | Both | Latest observation (`$max` of log timestamps) |

### 4.2 Sample Documents

**Materialized node:**

```json
{
  "_id": "person:paul iusztin",
  "kind": "node",
  "type": "person",
  "name": "paul iusztin",
  "properties": {
    "aliases": ["paul", "pauliusztin"],
    "email": "paul@example.com"
  },
  "embedding": [0.0123, -0.0456, ...],
  "sources": [ObjectId("6650f3..."), ObjectId("6650f4...")],
  "created_at": ISODate("2026-03-01T12:00:00Z"),
  "updated_at": ISODate("2026-03-15T09:30:00Z")
}
```

**Materialized edge:**

```json
{
  "_id": {
    "source_node_id": "person:paul iusztin",
    "target_node_id": "task:write a book on agentic systems",
    "type": "todo"
  },
  "kind": "edge",
  "type": "todo",
  "source_node_id": "person:paul iusztin",
  "source_type": "person",
  "target_node_id": "task:write a book on agentic systems",
  "target_type": "task",
  "properties": {},
  "sources": [ObjectId("6650f3...")],
  "created_at": ISODate("2026-03-01T12:00:00Z"),
  "updated_at": ISODate("2026-03-01T12:00:00Z")
}
```

**Reverse edge (synthetic, for bidirectional traversal):**

```json
{
  "_id": {
    "source_node_id": "document:https://example.com/article",
    "target_node_id": "person:paul iusztin",
    "type": "mentions"
  },
  "kind": "edge",
  "type": "mentions",
  "source_node_id": "document:https://example.com/article",
  "source_type": "document",
  "target_node_id": "person:paul iusztin",
  "target_type": "person",
  "direction": "reverse",
  "properties": {},
  "sources": [ObjectId("6650f3...")],
  "created_at": ISODate("2026-03-01T12:00:00Z"),
  "updated_at": ISODate("2026-03-01T12:00:00Z")
}
```

### 4.3 Deduplication Semantics

Multiple log entries for the same entity merge into a single materialized document:

```
knowledge_graph_log                          knowledge_graph
┌──────────────────────────┐
│ NodeLogEntry #1          │
│ name: "paul iusztin"     │
│ type: "person"           │
│ properties:              │                 ┌──────────────────────────┐
│   aliases: ["paul"]      │  ── $group ──►  │ _id: "person:paul iusztin"
│ source_document_id: A    │     $merge      │ properties:              │
│ created_at: T1           │     Objects     │   aliases: ["paul"]      │
├──────────────────────────┤                 │   email: "paul@ex.com"   │
│ NodeLogEntry #2          │                 │ sources: [A, B]          │
│ name: "paul iusztin"     │                 │ created_at: T1 ($min)    │
│ type: "person"           │                 │ updated_at: T2 ($max)    │
│ properties:              │                 └──────────────────────────┘
│   email: "paul@ex.com"  │
│ source_document_id: B    │
│ created_at: T2           │
└──────────────────────────┘
```

**Merge rules:**
- **Nodes** are grouped by `(name, type)`. The composite `_id` becomes `"type:name"`.
- **Edges** are grouped by `(source_node_id, source_type, target_node_id, target_type, type)`.
- **Properties** are merged via `$mergeObjects` -- for fields present in multiple entries, the last one processed wins (MongoDB processes in insertion order).
- **Sources** are accumulated via `$addToSet` -- every unique source document ID is tracked.
- **Timestamps**: `created_at` uses `$min` (earliest observation), `updated_at` uses `$max` (latest).

---

## 5. Extraction Pipeline

The extraction pipeline transforms a source document into immutable log entries in `knowledge_graph_log`.

### 5.1 Overview

```
┌────────────────────────────────────────────────────────────────────┐
│                      EXTRACTION PIPELINE                           │
│                                                                    │
│  Document                                                          │
│  content    ──► Chunk ──► LLM Extract ──► Merge     ──► Persist   │
│  (text)         (512      (parallel,      results       to log    │
│                 tokens,    5 concurrent)                            │
│                 64 overlap)     │                                   │
│                                │                                   │
│                          ┌─────┴──────┐                            │
│                          │  Structural │                            │
│                          │  Entries    │                            │
│                          │ (DOCUMENT,  │                            │
│                          │  CHUNK,     │                            │
│                          │  PART_OF,   │──► Combine ──► Normalize  │
│                          │  NEXT,      │                (fuzzy      │
│                          │  MENTIONS,  │                 dedup)     │
│                          │  REFERENCED)│                            │
│                          └─────────────┘                            │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 5.2 Chunking

The document's text content is split into token-bounded chunks using OpenAI's `cl100k_base` tokenizer (via tiktoken):

- **Chunk size:** 512 tokens (configurable)
- **Chunk overlap:** 64 tokens (configurable)
- **Algorithm:** Sliding window -- `start += chunk_size - chunk_overlap`
- Each chunk receives a UUID for provenance tracking

The tokenizer ensures chunks break at token boundaries (not character boundaries), which better preserves semantic units for the LLM.

### 5.3 LLM Entity Extraction

Each chunk is sent to the LLM independently, with concurrency bounded by an `asyncio.Semaphore` (default: 5 concurrent requests per document).

The **system prompt** includes:
1. The ontology schema (extractable node types with their property schemas, edge types with their constraints)
2. The expected JSON output format
3. Rules: names must be lowercase, only use ontology types, respect edge constraints

The LLM returns JSON with `nodes` and `edges` arrays. The parser then validates each item:
- **Nodes**: Must have a valid `NodeType` from the extractable set. Names are lowercased and stripped. Invalid types are logged and skipped.
- **Edges**: Must have a valid `EdgeType` from the extractable set. Source and target types must match the `EDGE_CONSTRAINTS` registry exactly. Violations are logged and skipped.

### 5.4 Structural Entry Construction

After LLM extraction, the pipeline deterministically creates structural entries that the LLM should not generate:

**Nodes:**
- One **DOCUMENT** node per source document, with `name = source_uri`
- One **CHUNK** node per chunk, with `name = "{source_uri}#chunk-{idx}"` and the full chunk text stored in `properties.content`

**Edges:**
- **PART_OF**: Each CHUNK -> its parent DOCUMENT
- **NEXT**: `chunk[i]` -> `chunk[i+1]` for sequential ordering (created for `idx > 0`)
- **MENTIONS**: DOCUMENT -> each unique PERSON extracted by the LLM
- **REFERENCED**: DOCUMENT -> each referenced document URI (from pre-populated references on the source document)

### 5.5 Fuzzy Deduplication (Normalization)

The LLM may extract the same entity with slightly different names across chunks (e.g., "alice smith" and "alice smithe"). The normalization step merges near-duplicates:

1. Iterate through all nodes, grouped by type
2. For each node, compare its name against all previously kept nodes of the same type using `SequenceMatcher`
3. If the similarity ratio >= 0.85 (configurable threshold), merge into the existing canonical node:
   - **Property merge:** The kept node's properties take precedence on conflicts (`{**incoming, **kept}`)
   - **Canonical name:** The first-seen name becomes canonical
4. After all nodes are processed, remap all edge source/target names to use canonical names

This handles LLM inconsistencies like "paul iusztin" vs "paul iustin" or "machine learning" vs "machine learning systems".

### 5.6 Persistence to Log

The normalized extraction result is written to `knowledge_graph_log`:

- Nodes are converted to `NodeLogEntry` documents and inserted via `NodeLogEntry.insert_many()`
- Edges are converted to `EdgeLogEntry` documents and inserted via `EdgeLogEntry.insert_many()`
- All entries are timestamped with `datetime.now(tz=UTC)`
- All entries reference the `source_document_id` for provenance

The log is **append-only**. Running extraction twice for the same document appends duplicate entries. Deduplication happens at materialization time.

---

## 6. Materialization Pipeline

The materialization pipeline rebuilds the `knowledge_graph` collection from the log, adds embeddings, creates reverse edges for bidirectional traversal, and ensures search indexes exist.

### 6.1 Overview

```
┌────────────────────────────────────────────────────────────────────┐
│                   MATERIALIZATION PIPELINE                          │
│                                                                    │
│  knowledge_graph_log                                               │
│       │                                                            │
│       ▼                                                            │
│  ┌─────────────────┐                                               │
│  │ 1. Aggregate    │  $group nodes by (name, type)                 │
│  │    & Dedup      │  $group edges by (src, tgt, type)             │
│  │                 │  $mergeObjects properties                     │
│  │                 │  $addToSet sources                            │
│  │                 │  $unionWith (merge node + edge branches)      │
│  │                 │  $out → knowledge_graph (atomic replace)      │
│  └────────┬────────┘                                               │
│           ▼                                                        │
│  ┌─────────────────┐                                               │
│  │ 2. Reverse      │  Create synthetic edges for bidirectional     │
│  │    Edges        │  traversal (person ↔ document, etc.)          │
│  └────────┬────────┘                                               │
│           ▼                                                        │
│  ┌─────────────────┐                                               │
│  │ 3. Embed Nodes  │  Compute vectors for nodes with empty         │
│  │                 │  embeddings. Batch processing (64 per batch)   │
│  └────────┬────────┘                                               │
│           ▼                                                        │
│  ┌─────────────────┐                                               │
│  │ 4. Ensure       │  Text index: name, content, aliases           │
│  │    Indexes      │  Vector index: embedding (cosine, 768 dims)   │
│  └─────────────────┘                                               │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 6.2 The MongoDB Aggregation Pipeline

The pipeline processes the `knowledge_graph_log` collection in two branches, then merges them:

**Node branch:**

```javascript
// 1. Filter to nodes only
{ $match: { kind: "node" } }

// 2. Group by (name, type) -- deduplicates across documents/chunks
{ $group: {
    _id: { name: "$name", type: "$type" },
    properties: { $mergeObjects: "$properties" },
    sources: { $addToSet: "$source_document_id" },
    created_at: { $min: "$created_at" },
    updated_at: { $max: "$created_at" }
} }

// 3. Reshape: composite _id = "type:name", initialize empty embedding
{ $project: {
    _id: { $concat: ["$_id.type", ":", "$_id.name"] },
    kind: "node",
    name: "$_id.name",
    type: "$_id.type",
    properties: 1,
    embedding: [],
    sources: 1,
    created_at: 1,
    updated_at: 1
} }
```

**Edge branch:**

```javascript
// 1. Filter to edges only
{ $match: { kind: "edge" } }

// 2. Group by full edge identity (5-field composite key)
{ $group: {
    _id: {
      source_node_id: "$source_node_id",
      source_type: "$source_type",
      target_node_id: "$target_node_id",
      target_type: "$target_type",
      type: "$type"
    },
    properties: { $mergeObjects: "$properties" },
    sources: { $addToSet: "$source_document_id" },
    created_at: { $min: "$created_at" },
    updated_at: { $max: "$created_at" }
} }

// 3. Reshape: prefix node IDs with type (e.g., "person:alice")
{ $project: {
    _id: {
      source_node_id: { $concat: ["$_id.source_type", ":", "$_id.source_node_id"] },
      target_node_id: { $concat: ["$_id.target_type", ":", "$_id.target_node_id"] },
      type: "$_id.type"
    },
    kind: "edge",
    type: "$_id.type",
    source_node_id: { $concat: ["$_id.source_type", ":", "$_id.source_node_id"] },
    source_type: "$_id.source_type",
    target_node_id: { $concat: ["$_id.target_type", ":", "$_id.target_node_id"] },
    target_type: "$_id.target_type",
    properties: 1,
    sources: 1,
    created_at: 1,
    updated_at: 1
} }
```

**Merge and output:**

```javascript
// The node branch runs first, then merges with the edge branch
{ $unionWith: { coll: "knowledge_graph_log", pipeline: edge_branch } }

// Atomically replace the knowledge_graph collection
{ $out: "knowledge_graph" }
```

The `$out` stage drops the target collection and recreates it with the pipeline output. This is atomic but **destructive** -- all indexes on `knowledge_graph` are lost and must be rebuilt after every materialization.

### 6.3 Reverse Edge Creation

MongoDB's `$graphLookup` follows edges in one direction only. To traverse relationships bidirectionally (e.g., from a person to the documents that mention them, or from a document to the people it mentions), reverse edges are created.

**Bidirectional pairs:**
- PERSON <-> DOCUMENT
- DOCUMENT <-> DOCUMENT
- PERSON <-> PERSON

For each qualifying edge, a new document is inserted with:
- **Swapped** `source_node_id` and `target_node_id`
- **Swapped** `source_type` and `target_type`
- **Same** edge type, properties, sources, and timestamps
- **Added** `direction: "reverse"` flag (used by visualization to avoid double-rendering)

Duplicates (from re-running materialization) are handled gracefully via `insert_many(ordered=False)` with `BulkWriteError` catching.

### 6.4 Node Embedding

After materialization, all nodes with empty `embedding` arrays are embedded:

1. **Text representation** is built from each node:
   ```
   {type}: {_id}
   {key1}: {value1}
   {key2}: {value2}
   {content}
   ```
   The `content` property (which can be long for chunk nodes) is placed last.

2. Nodes are processed in batches (default: 64 per batch) to manage memory.
3. The embedding model is called with the batch of text representations.
4. Vectors are written back via `bulk_write` with `UpdateOne` operations.

Default embedding model: Gemini `text-embedding-004` producing 768-dimensional vectors.

### 6.5 Index Creation

Two search indexes are created (or recreated) after every materialization since `$out` destroys them:

**Text index** (standard MongoDB):
```javascript
createIndex(
  { "name": "text", "properties.content": "text", "properties.aliases": "text" },
  { name: "text_index" }
)
```
Enables `$text` queries that search across node names, chunk content, and person aliases.

**Vector search index** (Atlas Search / mongot):
```javascript
{
  name: "vector_index",
  type: "vectorSearch",
  definition: {
    fields: [{
      type: "vector",
      path: "embedding",
      numDimensions: 768,
      similarity: "cosine"
    }]
  }
}
```
Enables `$vectorSearch` queries for semantic similarity. After creation, the pipeline polls mongot for up to ~90 seconds waiting for the index to become ready (mongot syncs asynchronously from mongod).

---

## 7. Retrieval Strategy

The retrieval strategy is a **two-stage pipeline**: first find entry-point nodes via hybrid search, then expand the graph around them.

### 7.1 Overview

```
┌────────────────────────────────────────────────────────────────────┐
│                       RETRIEVAL PIPELINE                           │
│                                                                    │
│  User Query                                                        │
│       │                                                            │
│       ├─────────────────┬──────────────────┐                       │
│       ▼                 ▼                  │                       │
│  ┌──────────┐    ┌──────────┐              │                       │
│  │  Vector  │    │  Text    │              │                       │
│  │  Search  │    │  Search  │              │                       │
│  │          │    │          │              │                       │
│  │$vector   │    │$text     │              │                       │
│  │Search on │    │query on  │              │                       │
│  │embedding │    │name,     │              │                       │
│  │(cosine)  │    │content,  │              │                       │
│  │          │    │aliases   │              │                       │
│  └────┬─────┘    └────┬─────┘              │                       │
│       │               │                    │                       │
│       └───────┬───────┘                    │                       │
│               ▼                            │                       │
│        ┌────────────┐                      │                       │
│        │ RRF Fusion │                      │                       │
│        │ k=60       │                      │                       │
│        └─────┬──────┘                      │                       │
│              ▼                             │                       │
│        top_k seed nodes                    │                       │
│              │                             │                       │
│              ▼                             │                       │
│        ┌─────────────────┐                 │                       │
│        │ Graph Expansion │                 │                       │
│        │ $graphLookup    │                 │                       │
│        │ (outgoing +     │                 │                       │
│        │  incoming)      │                 │                       │
│        │ max_hops = 3    │                 │                       │
│        └────────┬────────┘                 │                       │
│                 ▼                          │                       │
│        ┌─────────────────┐                 │                       │
│        │ Hydrate Nodes   │                 │                       │
│        │ Dedup Edges     │                 │                       │
│        └────────┬────────┘                 │                       │
│                 ▼                          │                       │
│           QueryResult                      │                       │
│           (nodes + edges)                  │                       │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 7.2 Vector Search

Embeds the query string using the same embedding model as node embeddings, then runs a MongoDB `$vectorSearch` aggregation:

```javascript
{
  $vectorSearch: {
    index: "vector_index",
    path: "embedding",
    queryVector: [0.0123, -0.0456, ...],
    numCandidates: top_k * 10,     // Over-fetch for better recall
    limit: top_k,
    filter: { kind: "node" }       // Only search nodes, not edges
  }
},
{ $addFields: { _search_score: { $meta: "vectorSearchScore" } } }
```

Vector search captures **semantic similarity** -- it will find nodes conceptually related to the query even if they don't share exact terms. If the vector index is unavailable, the system falls back to text-only search.

### 7.3 Text Search

Runs a standard MongoDB `$text` query against the text index:

```javascript
{ $match: { kind: "node", $text: { $search: "the user query" } } },
{ $addFields: { _search_score: { $meta: "textScore" } } },
{ $sort: { _search_score: -1 } },
{ $limit: top_k }
```

Text search captures **lexical matches** -- exact keywords, partial matches, and stemmed variations across node names, chunk content, and person aliases. If the text index is unavailable, the system falls back to vector-only search.

### 7.4 Reciprocal Rank Fusion (RRF)

The vector and text results are merged using RRF, which combines rank positions rather than raw scores (making it robust to different score distributions):

```
score(doc) = sum( 1 / (k + rank) )   for each result list containing the doc
```

With `k = 60` (configurable), a document ranked 1st in both lists gets:
```
score = 1/(60+1) + 1/(60+1) = 2/61 ≈ 0.0328
```

A document ranked 1st in one list and absent from the other:
```
score = 1/(60+1) = 1/61 ≈ 0.0164
```

This naturally boosts documents that appear in both result sets while still including documents found by only one method. The final ranking is by fused score descending, returning the top `top_k` nodes.

### 7.5 Multi-Hop Graph Expansion

From the seed nodes, the graph is expanded via two `$graphLookup` passes on the `knowledge_graph` collection:

**Outgoing traversal:**
```javascript
{
  $graphLookup: {
    from: "knowledge_graph",
    startWith: "$_id",
    connectFromField: "target_node_id",
    connectToField: "source_node_id",
    as: "outgoing",
    maxDepth: 2,                                    // max_hops - 1 (0-indexed)
    restrictSearchWithMatch: { kind: "edge" }
  }
}
```

**Incoming traversal:**
```javascript
{
  $graphLookup: {
    from: "knowledge_graph",
    startWith: "$_id",
    connectFromField: "source_node_id",
    connectToField: "target_node_id",
    as: "incoming",
    maxDepth: 2,
    restrictSearchWithMatch: { kind: "edge" }
  }
}
```

Both directions are merged via `$setUnion`, then deduplicated by converting edge `_id` dicts to hashable tuples and tracking a `seen_edge_ids` set. All discovered node IDs (from edge endpoints) are collected into a set.

Finally, all discovered nodes are **hydrated** -- fetched as full documents from the collection via `find({kind: "node", _id: {$in: [...]})`.

The result is a `QueryResult` containing all reachable nodes and edges within `max_hops` of the seed nodes.

**Performance note:** `$graphLookup` is recursive -- once the index is loaded into memory, subsequent hops are very fast. The initial `$match` + `$in` for seed nodes works well with up to ~100-1000 entry points but can slow down with thousands.

---

## 8. MongoDB Operational Considerations

This section covers how MongoDB internals affect the two-collection architecture in production.

### 8.1 Atlas Deployment Model

When deployed on MongoDB Atlas:
- Atlas auto-configures a **replica set** with a minimum of 3 replicas for high availability
- Always writing to the primary; reads can be distributed to secondaries
- Common configuration: multi-cloud (AWS, Azure, GCP) with multiple availability zones (e.g., 2 in AWS), each replica in a different zone
- **Dedicated search nodes** (mongot) are available in Atlas, separating the search workload from the data workload entirely

### 8.2 RAM Competition: mongod vs mongot

On a single machine, mongod (the data process) and mongot (the Lucene-based search process for vector and text search) compete for memory:

- **Inverted indexes** (used by mongot) can be as large as or **larger than** the data itself. For example, 10 GB of data can produce 10+ GB of inverted indexes because they index every word in the documents, unlike B-Tree indexes that index only selected fields.
- This means a machine with 10 GB of data, 10 GB of mongot indexes, and 10 GB of RAM will see degraded mongod performance as the working set exceeds available memory.
- **Mitigation in Atlas:** Use dedicated search nodes so mongod and mongot have separate RAM pools. This is the recommended approach for production workloads with significant search traffic.

### 8.3 The RAM Problem with Two Collections

The core tension of the two-collection architecture is **RAM pressure during materialization**:

- The log collection is rarely read during normal operation -- it mostly stays on disk, which is fine for storage but becomes a problem when materialization pulls it into RAM.
- During materialization, the aggregation pipeline scans `knowledge_graph_log` and writes to `knowledge_graph`. Both collections need to be in memory simultaneously.
- If `$out` (or `$merge`) scans the **full** log without scoping to recent data, old entries get pulled into the working set, competing with the active data.
- **For infrequent syncs**, this is manageable. It becomes a problem when large amounts of data from both collections need to exist in RAM at the same time.

**Mitigation with `$merge`:** Instead of `$out` (which replaces everything), use `$merge` scoped to only process data from the last N seconds (e.g., last 20 seconds). This avoids loading historical data into memory. However, this adds complexity:
- Need a `created_at` filter in the aggregation pipeline
- Need a cleanup step to handle stale/deleted entries
- Need to handle the case where re-processing old data (e.g., from a month ago) is required

**The single-collection alternative** avoids this entirely -- with in-place upserts, there's only one collection to manage and no materialization step pulling old data into RAM.

### 8.4 Scaling: Vertical vs Horizontal

- **Vertical scaling** (upgrading machine size) increases cost **exponentially** -- doubling RAM or CPU often more than doubles the price.
- **Horizontal scaling** (sharding) scales cost **linearly** -- adding shards adds proportional capacity at proportional cost.

**Sharding details:**
- Each shard has its own replica set (e.g., 2 shards = 6 nodes with 3 replicas each)
- **MongoS** (the shard router) is the connection endpoint -- clients connect to it, and it routes queries to the appropriate shard(s)
- **Lucene/mongot is capped at ~2 billion items per shard** due to integer index limits
- Sharding adds significant cluster complexity, especially for vector search where indexes become very large across shards

### 8.5 Materialization Scheduling

With two collections, "when do I run materialization?" becomes an operational question:

- Run too infrequently: the materialized view becomes stale and queries miss recent data
- Run too frequently: RAM pressure increases as the log is scanned repeatedly
- Must be careful with aggregation filters to avoid loading old data into RAM
- **Options:**
  - `$out`: Full replace, simpler but destructive (loses indexes, forces rebuild)
  - `$merge`: Incremental, preserves indexes, but needs careful date scoping and a stale-document cleanup strategy

### 8.6 Immutability is Application-Level

MongoDB has no built-in "immutable collection" flag. The immutability of the `knowledge_graph_log` is enforced entirely by the application layer -- the code only calls `insert_many()`, never `update_one()`, `update_many()`, or `delete_many()`. This is a design choice, not a database guarantee. An application bug or ad-hoc shell command could corrupt the log.

---

## 9. Pros and Cons

### 9.1 Advantages

- **Full audit trail with temporal history.** Every extraction event is preserved with its timestamp, source document, and chunk provenance. You can reconstruct what was known at any point in time.
- **Idempotent re-extraction.** Running extraction twice for the same document simply appends duplicate log entries. The materialization step deduplicates via `$group`, so the materialized view self-corrects.
- **Clean separation of write and read paths.** The log is write-optimized (append-only, no indexes needed for queries). The materialized view is read-optimized (deduplicated, embedded, indexed).
- **Property merging across sources.** When multiple documents mention the same entity with different properties, `$mergeObjects` accumulates all properties into a single node. This naturally builds richer entity profiles over time.
- **Source provenance tracking.** The `$addToSet` on `sources` tracks exactly which source documents contributed to each entity, enabling attribution and debugging.
- **Low RAM impact during normal operation.** The log collection is rarely read after extraction -- it mostly stays on disk and doesn't compete with the working set.

### 9.2 Disadvantages

- **`$out` drops the collection and destroys indexes.** Every materialization forces a full index rebuild (text index + vector search index). The vector index also requires polling mongot for sync, adding latency. Switching to `$merge` preserves indexes but adds complexity.
- **No incremental materialization with `$out`.** The entire log is scanned and the entire view is rebuilt every time, regardless of how much data changed. This doesn't scale to millions of documents.
- **Two collections competing for RAM during materialization.** If the aggregation pipeline scans the full log, old data gets pulled into the working set, competing with active queries.
- **Reverse edges inflate storage and complicate inserts.** Bidirectional traversal requires synthetic reverse edges -- effectively doubling the edge count for qualifying relationships. These must be re-created after every `$out`.
- **Mixed `_id` types break typed ORM patterns.** Nodes use string IDs (`"person:alice"`), edges use dict IDs (`{source_node_id, target_node_id, type}`). This forces the Beanie model to use `id: Any`, losing type safety.
- **Complex aggregation pipeline hard to debug and test.** The multi-branch pipeline with `$unionWith` and `$out` is difficult to reason about, test in isolation, and debug when something goes wrong.
- **No real-time updates.** Changes in the log are invisible to queries until the next full materialization. The time between extraction and queryability is at least one materialization cycle.
- **Operational overhead.** Scheduling materialization, monitoring RAM usage, managing index rebuilds, and deciding when to re-process old data all add operational complexity.
- **Immutability not enforced at the database level.** An application bug or manual shell command could corrupt the log, and there's no database-level protection against it.
- **A single mutable collection is simpler.** In-place upserts avoid the materialization step entirely: simpler on RAM, simpler to operate, real-time visibility, no index rebuilds. The main trade-off is losing the temporal audit trail.

---

## 10. Configuration Reference

| Parameter | Default | Used In | Description |
|-----------|---------|---------|-------------|
| `extraction.chunk_size` | 512 | Chunking | Token count per chunk |
| `extraction.chunk_overlap` | 64 | Chunking | Token overlap between consecutive chunks |
| `extraction.llm_concurrency` | 5 | LLM extraction | Max parallel LLM calls per document |
| `extraction.similarity_threshold` | 0.85 | Fuzzy dedup | SequenceMatcher ratio for merging near-duplicate nodes |
| `query.top_k` | 10 | Search | Number of seed nodes returned by hybrid search |
| `query.max_hops` | 3 | Graph expansion | Maximum traversal depth from seed nodes |
| `query.rrf_k` | 60 | RRF fusion | RRF constant (higher = less weight to rank position) |
| `query.embedding_batch_size` | 64 | Node embedding | Batch size for embedding computation |
| `models.embedding.dimensions` | 768 | Vector index | Dimensionality of embedding vectors |
| `models.embedding.model` | `text-embedding-004` | Embedding | Embedding model identifier |
| `models.llm.model` | `gemini-2.5-flash-lite` | LLM extraction | LLM model for entity extraction |
