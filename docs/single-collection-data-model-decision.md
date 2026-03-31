# Single-Collection Knowledge Graph: Data Model Decision

When migrating from the two-collection architecture (immutable log + materialized view) to a single mutable collection, we evaluated two data models for representing the knowledge graph. This document captures the analysis and rationale behind the final decision.

---

## Table of Contents

1. [Context](#1-context)
2. [Option A: Separate Edge Documents](#2-option-a-separate-edge-documents)
3. [Option B: Nested Relationships](#3-option-b-nested-relationships)
4. [Comparison: Pros and Cons](#4-comparison-pros-and-cons)
5. [Scaling Analysis: 5 Million Documents](#5-scaling-analysis-5-million-documents)
6. [Decision: Separate Edge Documents](#6-decision-separate-edge-documents)

---

## 1. Context

The original architecture used two MongoDB collections:

- **`knowledge_graph_log`**: Append-only immutable log of extraction events.
- **`knowledge_graph`**: Materialized view rebuilt from the log via `$out`.

This design had significant operational overhead: full index rebuilds on every materialization, RAM pressure from scanning both collections simultaneously, and no real-time updates. See [immutable-log-materialized-view-architecture.md](./immutable-log-materialized-view-architecture.md) for the full documentation of that approach.

The goal of the migration is to use a **single mutable collection** (`knowledge_graph`) where extraction upserts entities directly, eliminating the log collection and the materialization pipeline entirely.

Two data models were considered for the single collection:
- **Option A:** Nodes and edges as separate documents (edges are first-class).
- **Option B:** Relationships embedded within node documents (no separate edge documents).

---

## 2. Option A: Separate Edge Documents

Nodes and edges coexist in a single collection as independent documents, distinguished by the `kind` field. Both use **string `_id` values** for type safety.

### Node Document

```json
{
  "_id": "person:alice",
  "kind": "node",
  "type": "person",
  "name": "alice",
  "properties": {
    "aliases": ["alice doe"],
    "email": "alice@example.com"
  },
  "embedding": [0.0123, -0.0456, ...],
  "sources": [ObjectId("6650f3..."), ObjectId("6650f4...")],
  "created_at": ISODate("2026-03-01T12:00:00Z"),
  "updated_at": ISODate("2026-03-15T09:30:00Z")
}
```

- `_id`: Composite string `"type:name"` (e.g., `"person:alice"`, `"chunk:https://example.com/doc#chunk-0"`).
- `embedding`: Vector for semantic search, computed after extraction.
- `sources`: Array of source document ObjectIds that contributed to this node.

### Edge Document

```json
{
  "_id": "person:alice|todo|task:write a book",
  "kind": "edge",
  "type": "todo",
  "source_node_id": "person:alice",
  "source_type": "person",
  "target_node_id": "task:write a book",
  "target_type": "task",
  "properties": {},
  "sources": [ObjectId("6650f3...")],
  "created_at": ISODate("2026-03-01T12:00:00Z"),
  "updated_at": ISODate("2026-03-01T12:00:00Z")
}
```

- `_id`: Deterministic string `"source_id|type|target_id"` (e.g., `"person:alice|todo|task:write a book"`).
- `source_node_id` / `target_node_id`: Type-prefixed references to node `_id` values.

### Reverse Edge Document

For multi-hop bidirectional `$graphLookup` traversal, synthetic reverse edges are created:

```json
{
  "_id": "task:write a book|todo|person:alice",
  "kind": "edge",
  "type": "todo",
  "source_node_id": "task:write a book",
  "source_type": "task",
  "target_node_id": "person:alice",
  "target_type": "person",
  "direction": "reverse",
  "properties": {},
  "sources": [ObjectId("6650f3...")],
  "created_at": ISODate("2026-03-01T12:00:00Z"),
  "updated_at": ISODate("2026-03-01T12:00:00Z")
}
```

- `direction: "reverse"` marks this as a traversal aid, not a real relationship.
- The `_id` is naturally distinct from the forward edge (`"target|type|source"` vs `"source|type|target"`).

### Write Semantics

- **Nodes**: Upsert by `_id` with `$set` for properties, `$addToSet` for sources, `$min`/`$max` for timestamps.
- **Edges**: Same upsert pattern. Each edge is an independent operation.
- **Reverse edges**: Created during a post-extraction indexing step.

### `$graphLookup` Traversal

`$graphLookup` traverses edge documents by chaining `source_node_id` -> `target_node_id`:

```javascript
{
  $graphLookup: {
    from: "knowledge_graph",
    startWith: "$_id",
    connectFromField: "target_node_id",
    connectToField: "source_node_id",
    as: "connected",
    maxDepth: 2,
    restrictSearchWithMatch: { kind: "edge" }
  }
}
```

Reverse edges enable the traversal to chain bidirectionally (e.g., `person -> document -> person`) within a single `$graphLookup` pass. Without them, `$graphLookup` can only follow edges in one direction per pass, and multi-hop mixed-direction paths break.

---

## 3. Option B: Nested Relationships

Every entity is a single node document with relationships embedded as arrays. No separate edge documents exist.

### Node Document with Embedded Relationships

```json
{
  "_id": "person:alice",
  "type": "person",
  "attributes": {
    "aliases": ["alice doe"],
    "email": "alice@example.com"
  },
  "relationships": [
    {
      "target_id": "task:write a book",
      "type": "todo",
      "direction": "out",
      "attributes": {}
    },
    {
      "target_id": "document:https://example.com/article",
      "type": "mentions",
      "direction": "in",
      "attributes": {}
    }
  ],
  "out_target_ids": ["task:write a book"],
  "in_target_ids": ["document:https://example.com/article"],
  "sources": [ObjectId("6650f3...")],
  "embedding": [0.0123, -0.0456, ...],
  "created_at": ISODate("2026-03-01T12:00:00Z"),
  "updated_at": ISODate("2026-03-15T09:30:00Z")
}
```

- `relationships`: Array of objects holding the full metadata (type, direction, attributes) for each relationship.
- `out_target_ids`: Denormalized flat array of outgoing target `_id` values, used by `$graphLookup`.
- `in_target_ids`: Denormalized flat array of incoming source `_id` values, used by `$graphLookup`.

### Write Semantics

Adding a relationship requires **two document updates**:

1. **Source node**: `$addToSet` a relationship object to `relationships`, `$addToSet` the target ID to `out_target_ids`.
2. **Target node**: `$addToSet` a relationship object to `relationships`, `$addToSet` the source ID to `in_target_ids`.

### `$graphLookup` Traversal

`$graphLookup` follows the flat ID arrays directly between nodes:

```javascript
// Outgoing traversal
{
  $graphLookup: {
    from: "knowledge_graph",
    startWith: "$out_target_ids",
    connectFromField: "out_target_ids",
    connectToField: "_id",
    as: "outgoing",
    maxDepth: 2
  }
}

// Incoming traversal
{
  $graphLookup: {
    from: "knowledge_graph",
    startWith: "$in_target_ids",
    connectFromField: "in_target_ids",
    connectToField: "_id",
    as: "incoming",
    maxDepth: 2
  }
}
```

No reverse edges are needed since `in_target_ids` natively represents the reverse direction.

### Relationship Metadata Duplication

Every relationship is stored **twice** -- once in the source node (as an outgoing relationship) and once in the target node (as an incoming relationship). Both copies carry the same metadata (type, attributes). This means:

- Double storage for every relationship.
- Consistency risk: updating an attribute on one side requires updating it on the other side too.
- Double the write operations per relationship.

---

## 4. Comparison: Pros and Cons

### Option A: Separate Edge Documents

**Pros:**

- **Edges are first-class documents.** Easy to query globally (e.g., "find all MENTIONS edges" is a simple `{kind: "edge", type: "mentions"}` filter).
- **Edge metadata travels with `$graphLookup`.** During traversal, the edge documents are discovered, so you know *why* two nodes are connected (which relationship type, which attributes).
- **Independent upserts.** Each node and edge is upserted independently. No cross-document coordination. Parallel extraction workers don't block each other.
- **Fixed document size.** Node documents don't grow as relationships are added. Each edge is a small, predictable-size document (~200-500 bytes).
- **Sharding-friendly.** Edges distribute naturally across shards by their `_id`. No hot-document problem.
- **Single source of truth per relationship.** Each edge exists as exactly one document (forward). Reverse edges are traversal aids with no metadata to keep in sync.

**Cons:**

- **Reverse edges required.** Multi-hop bidirectional `$graphLookup` needs synthetic reverse edge documents, roughly doubling the edge count.
- **More documents overall.** N nodes + M edges + ~M reverse edges. For 5M documents, this could mean 25-30M total documents.
- **Two reads for node context.** Getting a node and all its relationships requires reading the node document plus querying its edges separately.
- **Nodes and edges mixed in one collection.** Distinguished by the `kind` field. Queries must always filter by `kind`.

### Option B: Nested Relationships

**Pros:**

- **No reverse edges.** `in_target_ids` natively represents the reverse direction. Eliminates an entire class of documents and the pipeline step that creates them.
- **Fewer documents.** Only N node documents. No separate edge documents.
- **Single read for full context.** One document read gets a node and all its relationships.
- **Cleaner model.** No `kind` field, no mixed document types. Everything is a node.
- **Less RAM pressure at small scale.** Fewer documents in the working set.

**Cons:**

- **Relationship metadata duplicated.** Every relationship is stored in both the source and target node. Double storage, double writes, and a consistency risk if one update fails.
- **`$graphLookup` loses edge metadata.** Traversal hops between nodes via ID arrays. The connected nodes are discovered, but not which relationship type connected them. Reconstruction requires post-traversal inspection of each node's `relationships` array.
- **Document size grows with relationships.** A frequently mentioned entity accumulates thousands of relationships, making its document large and expensive to read/update.
- **Write contention on hot entities.** Under concurrent extraction, popular entities (e.g., "machine learning") become write bottlenecks because MongoDB serializes writes to the same document.
- **Array manipulation complexity.** Removing or updating a specific relationship requires `$pull`/`$elemMatch` operations, which are more error-prone than upserting/deleting a whole document.
- **Global edge queries require scanning.** "Find all TODO relationships" means scanning all nodes and unwinding their `relationships` arrays, vs a simple filter on edge documents.
- **Dual-document upsert atomicity.** Adding one relationship requires updating two documents (source + target). If one write fails, the graph is inconsistent. Requires retry logic or cleanup.
- **Sharding hot spots.** Popular entities concentrate all their writes on one shard, creating uneven load distribution.

### Side-by-Side Summary

| Concern | Separate Edges (A) | Nested (B) |
|---|---|---|
| Document count | N + M + reverse edges | N only |
| Reverse edges | Required | Not needed |
| Relationship metadata during traversal | Available (edges are documents) | Lost (reconstruct post-traversal) |
| Upsert atomicity | Each edge is independent | Must update 2 documents per relationship |
| Document size | Fixed, predictable | Grows with relationships |
| Write contention | None (independent documents) | Hot entities bottleneck |
| Sharding | Even distribution | Hot shards on popular entities |
| Global edge queries | Simple filter | Scan + unwind |
| Metadata duplication | None (single document per edge) | Every relationship stored twice |
| RAM working set | More documents, uniform size | Fewer documents, variable size |

---

## 5. Scaling Analysis: 5 Million Documents

The system targets ingesting approximately 5 million documents (e.g., from arxiv datasets, Substack articles, and other sources). At this scale, the differences between the two models become decisive.

### Document Counts

With 5M source documents, assuming ~4 edges per document on average:

| Metric | Separate Edges (A) | Nested (B) |
|---|---|---|
| Node documents | ~10M (documents + chunks + persons + tasks + ...) | ~10M |
| Edge documents | ~20M forward + ~10M reverse = ~30M | 0 |
| Total documents | ~40M | ~10M |
| Storage per edge | ~300 bytes (one document) | ~200 bytes x 2 copies = ~400 bytes |

Option B has fewer documents but more storage per relationship due to duplication.

### Hot Entity Problem (Option B)

In a 5M-document arxiv dataset, common entities appear across thousands of documents:

- "machine learning" (as a topic/task) might be mentioned in 50,000+ documents.
- Each mention adds entries to `relationships`, `out_target_ids`, and `in_target_ids` arrays.
- At 50,000 relationships x ~200 bytes each = ~10MB per hot node document.
- Every new document mentioning "machine learning" requires an `$addToSet` on that 10MB document.
- Under concurrent extraction with 5+ workers, this becomes a serialized write bottleneck.

With Option A, those 50,000 mentions are 50,000 independent small edge documents. Concurrent workers upsert them in parallel with no contention.

### RAM Working Set

With MongoDB Atlas and a 5M-document dataset:

- **Option A (40M documents, ~300 bytes avg):** ~12 GB of data. Each document is small and uniform. The working set is predictable -- hot nodes and their frequently traversed edges stay in RAM, cold edges stay on disk.
- **Option B (10M documents, variable size):** ~8 GB of data (less total due to fewer documents, but duplication offsets some savings). Hot entity documents are large and must be fully loaded into RAM for any update or read. A single 10MB document displaces many smaller documents from the cache.

The variable document size in Option B makes RAM management less predictable. MongoDB's WiredTiger cache works best with uniform document sizes.

### Sharding

At 5M+ documents, sharding becomes relevant:

- **Option A:** Shard by `_id`. Edges distribute evenly because their `_id` strings are diverse (`"person:X|type|task:Y"`). No hot shards.
- **Option B:** Shard by `_id`. Popular entities like `"person:elon musk"` concentrate all their writes on one shard. The shard holding hot entities becomes a bottleneck while other shards are idle.

### Concurrent Extraction

With multiple Prefect workers processing documents in parallel:

- **Option A:** Worker 1 upserts `"person:alice|todo|task:X"`, Worker 2 upserts `"person:alice|todo|task:Y"`. These are different documents -- no contention.
- **Option B:** Worker 1 updates `person:alice` to add task:X to `relationships`. Worker 2 tries to update the same document to add task:Y. MongoDB serializes these writes. At high concurrency, this becomes a throughput bottleneck.

---

## 6. Decision: Separate Edge Documents

We chose **Option A (separate edge documents)** for the single-collection design based on the scaling requirements:

1. **No write contention.** With 5M documents and parallel extraction workers, independent edge documents avoid the serialized write bottleneck on hot entities.
2. **Predictable scaling.** Uniform document sizes give predictable RAM usage and even shard distribution.
3. **Edge metadata in traversal.** `$graphLookup` carries edge documents, preserving relationship type information without post-traversal reconstruction.
4. **No metadata duplication.** Each relationship is stored once (as a single edge document). Reverse edges are lightweight traversal aids.
5. **Simpler upsert logic.** Each node and each edge is an independent upsert operation. No cross-document coordination or consistency concerns.

The trade-off -- needing reverse edges and having more documents -- is acceptable because:
- Reverse edges are created automatically by the indexing pipeline and have deterministic string `_id` values, making them idempotent.
- The higher document count (~40M vs ~10M) is within MongoDB's comfortable range, especially with sharding.
- The unified string `_id` format (`"type:name"` for nodes, `"source|type|target"` for edges) fixes the `id: Any` problem from the original architecture, enabling typed Beanie models.
