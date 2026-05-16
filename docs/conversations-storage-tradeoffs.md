# Where should Conversations and Messages live?

> **Resolution (2026-05-16): neither A nor B — conversations are `Document`s.**
>
> The two options below assume *structured* conversation input (`list[{role, content, ...}]`). Our actual input is unstructured text dumps from chat sessions (no reliable role/turn delimiters), so there's nothing to fragment into individual messages. A conversation becomes one `Document(source_type=CONVERSATION)` with `content=raw_transcript`, chunked and extracted like any other text source. No new collections, no new node types.
>
> The Option A/B comparison below is preserved as design history — and as the reference for when/if we later wire a source that *does* hand us delimited turns (Slack export, OpenAI API logs, etc.). That trigger reopens Option B.

Two options for storing chat data in Tree:

- **Option A** — add `CONVERSATION` and `MESSAGE` as new node types inside the existing `knowledge_graph` collection.
- **Option B** — keep them in dedicated `conversations` and `messages` collections, mirroring how `documents` already sits alongside `knowledge_graph`.

This doc lays out the tradeoffs. Decision lives in `plan.md`.

## Option A — Conversations + Messages inside `knowledge_graph`

### Pros

- **First-class cross-layer edges.** `MENTIONS` (MESSAGE → PERSON), `EXTRACTED_FROM` (PERSON → MESSAGE), `HAS_MESSAGE` (CONV → MSG) are real edge documents in the same collection as everything else. One uniform edge model.
- **Mirrors the reference design.** The reference doc's whole point is short-term ↔ long-term cross-referencing via graph edges. Easier to lift their query patterns wholesale.
- **One embedding/indexing pipeline.** Messages flow through the same embed → resolve → dedupe path as chunks. No duplication.
- **Single mental model.** "The user's memory is one collection." Easier to reason about, easier to expose as a single MCP surface.
- **Uniform provenance.** `sources` always points within the same collection — no heterogeneous ids.
- **Future graph queries are easy.** "Show me every entity ever mentioned by Alice in conversations last week" is one collection scan with the right indexes.

### Cons

- **Schema bloat.** Messages get wedged into the polymorphic `KnowledgeGraphEntry` shape: unused `canonical_name`, `aliases`, `confidence`, `merged_into`, `source_node_id`/`target_node_id` fields on every message row. `message.properties.content` instead of `message.content`.
- **Working-set pollution.** Chat is high-volume, high-churn. Mixes volatile message data with curated, mostly-permanent KG entities. Cache, index size, and scan times for graph queries grow with chat traffic, not with entity count.
- **Retention coupling.** Want to TTL old messages? You're writing TTLs scoped by `type == "message"` on a collection that mostly contains things you'd never delete. Easy to miswrite a delete.
- **Index cost.** New indexes for `(user_id, conversation_id, timestamp)` etc. layered on top of KG indexes — same collection pays for both query patterns.
- **Breaks the symmetry we already chose.** Documents are raw content → separate collection. Messages are also raw content but go in KG. Inconsistent.
- **`NodeType` enum keeps growing** with non-knowledge concepts (or once Phase 3 lands, the registry has more structural-only entries). Ontology surface stays muddy.

## Option B — Separate `conversations` and `messages` collections

### Pros

- **Consistent with documents/KG split.** Raw content → its own collection, extracted knowledge → KG. One coherent rule across the codebase.
- **Clean schemas.** `Message` is a focused model: `role`, `content`, `timestamp`, `tool_calls`, `conversation_id`, `user_id`. No polymorphism, no unused fields.
- **Natural indexes.** `(user_id, conversation_id, timestamp)` is the message access pattern; index it directly. KG indexes stay tuned for graph traversal.
- **Independent lifecycle.** TTL, archive, redact, summarize-then-prune — apply to `messages` without touching KG. Retention policies become a property of the collection, not conditional code.
- **Tunable search.** Atlas Vector Search index on `messages` can be tuned for short-text recency-biased recall, separate from the KG entity index.
- **Smaller KG.** Working set scales with entities, not chat volume. Graph queries stay fast as chat grows.
- **Provenance already works.** `KnowledgeGraphEntry.sources: list[PydanticObjectId]` is already generic and already points at `documents`. Pointing at `messages` is a one-line conceptual extension.
- **Easier to delete the legacy `conversation_pipeline.py`.** The replacement is just "another data pipeline" alongside `data_pipeline`, fitting the existing pattern.

### Cons

- **Cross-layer edges become heterogeneous.** If we want a `MENTIONS` edge from MESSAGE → PERSON as a real graph edge (not just a `sources` backref), the edge lives in `knowledge_graph` but its `source_node_id` references a `messages._id`. Two-collection edges work, but tooling/visualizers may assume single-collection.
- **Two pipelines to maintain.** A `messages` ingest path plus the KG extraction step that reads from messages. Vs. one combined pipeline.
- **Lookup cost when joining.** "Entities mentioned in this message" is a `$lookup` or app-level join instead of a single-collection scan. Mongo's `$lookup` is fine but not free.
- **Drift risk.** Two collections evolving independently can drift in conventions (id format, timestamp handling, user_id scoping) if we're not disciplined.
- **Provenance edges become awkward (if we add them in Phase 6).** An `EXTRACTED_FROM` edge from PERSON → MESSAGE would point at a foreign collection — still doable, but the edge model needs to record which collection the target lives in.
- **Diverges further from the reference.** Less direct lift of their query patterns; we'd be re-deriving.

## Where the choice pivots

One question compresses it: **how heavily do you expect to query messages *as a graph*** (traversing into them from entities, vs. traversing out of them to find mentions)?

- Mostly **time-ordered retrieval** ("last N messages", "messages in conversation X") and entity extraction → **Option B** wins cleanly.
- Heavy **graph traversal involving messages as nodes** ("show the subgraph of entities co-occurring in this thread", "find conversations where Alice and Bob both appear") → **Option A** earns its keep.

For a personal assistant where chat is mostly a *source* of knowledge that gets extracted into the KG (and then the KG is what you query), Option B is the natural fit. If chat itself is a primary thing you traverse over, Option A.

## Why we ultimately picked neither

Both options assume the input is structured: a `list[{role, content, ...}]` we can persist turn-by-turn. Our actual input today is **raw chat text dumps** — full transcripts copy-pasted from sessions like the one this design conversation took place in. Without reliable role delimiters, there's nothing to split into `Message` rows.

Once you accept that constraint, the question dissolves: a conversation is *just text*, and we already have a perfectly good place for "raw text from a named source" — `documents`. So:

- **Conversation = `Document(source_type=CONVERSATION, content=raw_transcript)`.** No envelope-and-payload split.
- **Idempotency** via the existing `(user_id, source_type, source_uri)` unique index on `documents`. `source_uri` is a caller-supplied session id, or a content hash if none.
- **Provenance** at chunk granularity — same as articles. KG entries' `sources` list chunk ids; chunks point back at the parent conversation Document via `PART_OF`.
- **No new collections, no new node types, no new pipelines beyond the existing `ingest_conversation` Prefect entry point.**

### When to revisit Option B

If we later add a source that *does* hand us reliably delimited turns — Slack export, OpenAI API logs, MCP streaming `{role, content}` — Option B becomes worth implementing for *that* source. The trigger is structured input, not the chat use case in general.

Until then: YAGNI.
