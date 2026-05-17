# ADR-001: Data Model and Ontology for the Knowledge-Graph Memory

- **Status:** Accepted
- **Date:** 2026-05-16
- **Deciders:** Paul (project owner)
- **Context references:**
  - `plan.md` (canonical implementation plan)
  - `docs/conversations-storage-tradeoffs.md` (ancillary tradeoff doc)
  - Reference: `neo4j-agent-memory` (`agent-memory/notes/DATA_MODELS.md`, `agent-memory/src/neo4j_agent_memory/schema/models.py`)

## Context

Tree is a personal assistant rooted in a knowledge-graph memory. The graph is the durable substrate the agent reads and writes against; everything else (ETL, MCP tools, queries) feeds into or pulls from it. We need a data model that (a) tolerates messy, LLM-extracted content; (b) supports text, semantic, and graph traversal in a single store; (c) is multi-tenant from day one; and (d) is extensible without a redeploy every time we add a new domain concept.

The `neo4j-agent-memory` reference project demonstrated a workable POLE+O ontology (Person/Object/Location/Event + Organization), a `RELATED_TO {type: ...}` edge-collapse trick, and a bi-temporal preference model. We're porting the ideas — not the storage — into a MongoDB-backed unified-memory collection (`knowledge_graph`) with Atlas Search + Vector Search on top. This ADR captures the data-model and ontology decisions taken on 2026-05-16; the implementation lands across six PR-sized phases described in `plan.md`.

Current code grounding the decisions: `apps/memory/src/tree/entities/knowledge_graph.py` (single polymorphic `KnowledgeGraphEntry`), `apps/memory/src/tree/entities/ontology.py` (closed enums + `NODE_PROPERTIES` / `EDGE_CONSTRAINTS` dicts), `apps/memory/src/tree/entities/documents.py` (`Document` model with `SourceType.CONVERSATION`).

## Decision Summary

- **One mutable `knowledge_graph` collection** holds both nodes and edges as polymorphic rows; upsert semantics via deterministic string `_id`s.
- **Multi-tenancy is dual-enforced**: `_id` embeds `user_id` for DB-level correctness, and every row also carries an indexed `user_id` field for fast filtered reads.
- **`User` is a tenant identity, not a POLE+O node**: a separate `users` collection holds it; the active user is represented inside the KG as a `person:self` node flagged `is_active_user=True`.
- **Conversations are `Document(source_type=CONVERSATION)`** — no new collections, no `Message` node type. See `docs/conversations-storage-tradeoffs.md` for the full Option-A/B walk-through.
- **POLE+O is the canonical ontology**: Person, Organization, Location, Event, Object — plus `fact` and `preference` as proposition-style entities.
- **`NodeType` becomes a `NODE_REGISTRY`**; downstream apps register custom subtypes via `register_node_subtype()`. Tree's `task`/`episode`/`topic`/`project` are the first consumers.
- **POLE+O domain edges collapse into a single `related_to` type** discriminated by `semantic_type`, validated against a `RELATION_SEMANTICS` registry.
- **Strict-by-omission relations**: no generic catch-all `related_to`; ontology violations are dropped and logged to `extraction_rejections`.
- **Structural edges stay distinct**: `part_of`, `next`, `mentions`, `referenced`, `same_as` keep their own type identity (they aren't POLE+O domain relations).
- **Extractor provenance is metadata**, not a node — `ExtractorInfo` is a small Pydantic model carried on every LLM-produced row.
- **Per-type `*Properties` models, no shared base** (option A) — domain fields live in `properties` dict; graph-modeling meta fields live on `KnowledgeGraphEntry`.
- **Validation is two-tier**: envelope strict (drop the row, log), fields lenient (drop the field, keep the row).
- **Every property attribute carries `Field(description=...)`** — the description IS the prompt context.
- **`FACT` is island-style** — registered as a node, but participates in zero edges.
- **Strict-mode preferences**: only the active user holds preferences; third-party preference statements become `fact` rows.
- **Preferences connect only to `person:self` (`has`) and other preferences (`superseded_by`, `same_as`)** — scoping lives in `PreferenceProperties.context`, not in graph edges.
- **`PreferenceProperties` is typed-slot** with a closed `PreferenceCategory` enum.
- **Bi-temporal `valid_from` / `valid_until`** on facts and preferences; supersession via `superseded_by`.
- **Dedup has three tiers**: auto-merge (cosine ≥ 0.95), flag (≥ 0.85), fuzzy (rapidfuzz ≥ 90); status lifecycle lives on `SameAsProperties`.
- **`MentionsProperties`** carries `confidence`, `start_pos`, `end_pos` to support mention-highlighting UX.
- **Embedding model + dimension are pinned in `tree.config.settings`** as the single source of truth — `KnowledgeGraphEntry.embedding`, the mongot vector index config, and any query-time validation all read from there.

## Detailed Decisions

### 1. Storage model — single `knowledge_graph` collection, polymorphic rows

- **Decision:** keep both nodes and edges in one MongoDB collection (`knowledge_graph`) as rows of `KnowledgeGraphEntry`. `kind` discriminates node vs. edge; `type` discriminates within. Nodes carry `_id = "{user_id}:{type}:{name}"`; edges carry `_id = "{source_node_id}|{type}|{target_node_id}"`. Upsert semantics throughout.
- **Alternatives considered:** separate `nodes` and `edges` collections; relational store with foreign keys.
- **Rationale:** deterministic string `_id`s give us idempotent writes for free; polymorphism keeps graph queries to a single collection scan; one Atlas index per signal (vector, text, structural) covers the whole memory.
- **Consequences:** rows carry some fields they don't use (edges ignore `aliases`/`embedding`, nodes ignore `source_node_id`/`target_node_id`). We accept the schema breadth because the workload is read-heavy graph traversal, not field economy.

### 2. Multi-tenancy — `_id` prefix + indexed `user_id` field (dual enforcement)

- **Decision:** every node `_id` is `"{user_id}:{type}:{name}"`; every row also carries an indexed `user_id: PydanticObjectId` field. Edges are tenant-scoped by construction because both endpoint ids already contain the user prefix. No cross-user references in Phase 1.
- **Alternatives considered:** `_id` prefix only (slower reads — `$regex` prefix scan); `user_id` field only (cross-user `_id` collisions possible at write time); per-tenant collections (operationally heavy, breaks Atlas index sharing).
- **Rationale:** the two layers do different jobs. `_id` makes cross-user collisions DB-impossible; the indexed field lets us use cheap equality predicates in queries and in `$vectorSearch`/`$search` filter clauses.
- **Consequences:** every read/write call site must take `user_id` as a required parameter. Enforced via a `KGQuery` helper class and a CI grep rule on raw `KnowledgeGraphEntry.find(...)`. Existing `(source_type, source_uri)` unique index on `Document` becomes `(user_id, source_type, source_uri)`.

### 3. User identity vs Person entity — `users` collection + auto-created `person:self`

- **Decision:** `User` is a tenant identity stored in a separate `users` collection (fields: `id`, `identifier` unique, `attributes: dict`, timestamps). On `User.insert()` a hook writes a `KnowledgeGraphEntry` node with `_id = "{user_id}:person:self"`, `subtype="individual"`, `properties.is_active_user=True`. There is **no `:User` node inside the KG** and **no `User.self_person_id` field**.
- **Alternatives considered:** `:User` node inside `knowledge_graph`; `User.self_person_id` pointer.
- **Rationale:** `:User` would muddy POLE+O with tenant-administration concerns. A `self_person_id` pointer would be a second source of truth for "who is the active user?" — the `is_active_user=True` flag plus a sparse compound index on `(user_id, type, "properties.is_active_user")` gives us a single source of truth with no drift.
- **Consequences:** every preference and first-person edge attaches to `person:self`. A first-person resolver step (~10 lines, idempotent) redirects LLM-emitted person nodes whose name/aliases match the user's display name onto `person:self` before write — prevents the user and a contact-named-after-the-user from racing for the same `_id`.

### 4. Conversations are Documents (not KG nodes, not separate collections)

- **Decision:** a conversation = one `Document(source_type=CONVERSATION, content=raw_transcript)`. Idempotency via the existing `(user_id, source_type, source_uri)` index; `source_uri` is a caller-supplied session id (or content hash if none). Chunking and extraction handle conversations identically to articles or RSS items.
- **Alternatives considered:** (A) `CONVERSATION` and `MESSAGE` as KG node types; (B) separate `conversations` + `messages` collections. Both walked through in `docs/conversations-storage-tradeoffs.md`.
- **Rationale:** our actual inputs are raw chat-text dumps from sessions like this one — there are no reliable role/turn delimiters, so a structured `Message` model adds nothing. Conversations are *just text*, and `documents` is already the right home for "raw text from a named source."
- **Consequences:** no new collections, no new node types, no new pipelines — `data/conversation_pipeline.py` stays as the entry point and just gets `user_id` plumbing. If a future source hands us reliably delimited turns (Slack export, OpenAI API logs), Option B becomes worth revisiting for *that* source. Until then: YAGNI.

### 5. POLE+O ontology — canonical 5 types + Tree-specific subtype extensions

- **Decision:** canonical LLM-extractable node types are `person`, `organization`, `location`, `event`, `object`, plus `fact` and `preference` for proposition-style content. Each canonical type has a closed `subtypes` set matched to the reference (forensic subtypes deliberately excluded — see "Out of scope"). Tree's domain extensions (`task`, `episode`, `topic`, `project`) land as registered subtypes of `object`/`event`, **not** as canonical POLE+O entries.
- **Alternatives considered:** keep the existing flat `NodeType` enum and add new values; canonical-only POLE+O with no extension story.
- **Rationale:** POLE+O is a well-trodden, semantically coherent ontology. Anchoring on it gives us a real schema rather than a grab-bag of node types. Putting Tree's concepts through the same extension API we offer to downstream apps validates the extensibility story by self-application.
- **Consequences:** the canonical POLE+O block in `ontology.py` stays clean; Tree's extensions live in a clearly-marked section (or `entities/ontology_tree_extensions.py`). The LLM prompt grows substantially — Phase 3 acceptance includes an extraction-quality eval before vs. after.

### 6. Closed enum → extensible registry (`NODE_REGISTRY`, `register_node_subtype()`)

- **Decision:** replace the closed `NodeType: StrEnum` with a `NODE_REGISTRY: dict[str, NodeTypeSpec]`. Each spec carries `name`, `properties_schema`, `description`, optional closed `subtypes` set, and `llm_extractable` flag. `register_node_subtype(parent_type, subtype, description, extra_properties)` lets downstream apps add subtypes with optional Pydantic `*Extras` payload (option a — simpler than a full `(type, subtype) → BaseModel` registry).
- **Alternatives considered:** keep `NodeType: StrEnum` forever (every new concept is a code change to entities); full `(type, subtype) → BaseModel` registry (over-engineered for current scope).
- **Rationale:** the registry makes ontology growth a pure-Python registration call. Subtype-properties option (a) keeps the registry shape flat — parent's `properties_schema` is base, subtype's `extra_properties` are an optional overlay.
- **Consequences:** `NodeType` survives as a thin compat shim re-exporting registered names during the migration; eventually deleted. Old `TASK`/`EPISODE` references are routed (`TASK → ("object", "task")`, `EPISODE → ("event", "episode")`).

### 7. POLE+O edge collapse — single `related_to` + `semantic_type` discriminator + `RELATION_SEMANTICS`

- **Decision:** all POLE+O domain relations are stored as `KnowledgeGraphEntry(type="related_to", semantic_type="employed_by"|"knows"|...)`. The catalogue lives in `RELATION_SEMANTICS: dict[str, RelationSemanticSpec]`, each spec listing allowed `(source_type, target_type)` pairs, a `properties: type[BaseModel] | None` schema, and a description. The initial set is 14 named semantics (`knows`, `alias_of`, `member_of`, `employed_by`, `owns`, `uses`, `located_at`, `resides_at`, `headquarters_at`, `participated_in`, `occurred_at`, `involved`, `subsidiary_of`, `partner_with`).
- **Alternatives considered:** one distinct `EdgeType` enum value per relation (the original ontology shape); a fully open string `type` with no registry.
- **Rationale:** parity with the reference's single-relation extraction prompt (LLM emits one relation envelope with a `type` field); cleaner registry shape (add a new typed relation = one `RELATION_SEMANTICS` entry, no enum churn).
- **Consequences:** queries like "find all employment edges" become `find({type: "related_to", semantic_type: "employed_by", user_id})` — one extra equality predicate. Covered by a compound index `(user_id, type, semantic_type)`.

### 8. Strict-by-omission relations — no generic catch-all; reject + log

- **Decision:** LLM-emitted relations that don't match any of the 14 named semantics, or that violate a semantic's `(source_type, target_type)` constraint, are **dropped at validation**. A rejected-extraction logging surface (`extraction_rejections` collection) records the attempted `(semantic_type, source_type, target_type)` triple plus a sample of the chunk text.
- **Alternatives considered:** the reference's generic `related_to` catch-all (anything that doesn't fit a named semantic gets persisted as a generic edge); silently drop without logging.
- **Rationale:** the ontology is a hard contract on what the KG can express. A catch-all turns the graph into a typed-by-accident free-text mess. Logging the rejections converts schema gaps into signal — if `(person, "mentored_by", person)` shows up 50 times across users, that's the trigger to add a new named semantic.
- **Consequences:** without the logging, strict policy becomes silent data loss. Logging is part of Phase 3 acceptance, not optional.

### 9. Structural edges stay distinct enums — `part_of`, `next`, `mentions`, `referenced`, `same_as`

- **Decision:** structural edges keep their own type identity (not collapsed under `related_to`). `mentions` is broadened to `chunk → any POLE+O entity` except `preference` (carve-out — see decision 16). `same_as` is broadened to `any POLE+O entity → same type`.
- **Alternatives considered:** collapse everything (including structural edges) under one mega-type with a `kind` discriminator.
- **Rationale:** structural edges aren't POLE+O domain relations — they describe the corpus's physical structure (chunking, ordering, provenance, dedup). Their queries and indexes have different shapes than domain edges (e.g., `part_of` is always chunk→document, not a free-pair relation).
- **Consequences:** the validation path forks: structural edges validate against the structural-edge type set; domain edges validate against `related_to` + `RELATION_SEMANTICS`. Two small paths beat one bloated one.

### 10. Extractor provenance as metadata (not a `:Extractor` node)

- **Decision:** every LLM-produced row carries `extractor: ExtractorInfo | None` (`{name, version, extraction_time_ms?}`) populated by the pipeline. Structural rows (Document, Chunk) skip `extractor` — they aren't produced by an LLM.
- **Alternatives considered:** the reference's `:Extractor` node + `EXTRACTED_BY` edge.
- **Rationale:** cheaper than a separate node + edge per row, still queryable via `find({"extractor.name": "gemini-2.5-pro", "extractor.version": "..."})`. The metadata field accepts the future case where multiple extractors coexist without needing a separate node.
- **Consequences:** if we ever want extractor-level aggregation stats (rows-per-extractor, time-windowed), we either pivot via aggregation or revisit. Not planned.

### 11. Properties model strategy — option A (per-type `*Properties`, no base class)

- **Decision:** each canonical type has its own independent `*Properties` Pydantic model. There is **no shared `EntityProperties` base class**. Graph-modeling meta fields (`name`, `canonical_name`, `aliases`, `subtype`, `description`, `confidence`, `embedding`, `merged_into`, `valid_from`, `valid_until`, `extractor`, `semantic_type`) live on `KnowledgeGraphEntry`; only domain fields live in the `*Properties` payload which serializes into `KnowledgeGraphEntry.properties`.
- **Alternatives considered:** shared `EntityProperties` base class with type-specific subclasses; one mega-model with discriminator.
- **Rationale:** a shared base would smear graph-modeling concerns (`valid_from`, `confidence`) into per-type models, where they don't belong — they're memory-system fields, not domain fields. Per-type independence keeps each `*Properties` tightly scoped to *its* domain vocabulary.
- **Consequences:** `model_json_schema()` per type stays focused (LLM prompt context isn't polluted with `valid_from`); meta-field changes happen in one place (`KnowledgeGraphEntry`).

### 12. Field-level validation — envelope strict, fields lenient

- **Decision:** two-tier validation of LLM extraction output:
  - **Envelope (strict):** drop the whole row + log to `extraction_rejections` if `type` isn't registered, `semantic_type`/pair isn't allowed (edges), `name` is empty (nodes), or `subtype` is outside its parent's closed set.
  - **Field (lenient):** for each key in raw `properties`, try Pydantic-validate against the `*Properties` field annotation (plus subtype `extra_properties`); keep what validates, drop what doesn't, log dropped fields to `extraction_dropped_fields`. Write the row even if all property fields fail.
- **Alternatives considered:** strict everywhere (drop the row on any field error); lenient everywhere (let bogus types through).
- **Rationale:** an edge with an unmatched semantic represents a relationship the ontology can't express — nothing salvageable, drop. A node like `person:alice` with garbage in `email` is still a useful identity claim — keep the node, let dedup merge it with later better-formed emissions via `same_as`. Most `*Properties` fields are `Optional` to support this lenient stance.
- **Consequences:** lives in the extraction pipeline between Task 2 (LLM extract) and Task 3 (resolve). Implementation uses `TypeAdapter(field.annotation).validate_python(value)` per field, never raises out.

### 13. `Field(description=...)` discipline on every attribute

- **Decision:** every attribute on every `*Properties` model (nodes AND `RelationSemanticSpec.properties` for edges) carries a `Field(description="…")`. Descriptions are brief, action-oriented, with examples where ambiguous (≤15 words target).
- **Alternatives considered:** descriptions only on ambiguous fields; rely on field names alone.
- **Rationale:** descriptions flow into the LLM prompt via `model_json_schema()` — they ARE the model's only context for what a field means. Missing descriptions = vague extraction.
- **Consequences:** code review must enforce this. A `description: str | None = Field(default=None)` on a person model with no description text is rejected.

### 14. FACT as island-style proposition node (no edges allowed)

- **Decision:** `fact` is registered in `NODE_REGISTRY` (LLM-extractable) but participates in **zero** entries in `RELATION_SEMANTICS` and is **not** an allowed endpoint for any structural edge (`mentions`, `same_as`, `has`, `part_of`, `next`, `referenced`, `superseded_by`). `FactProperties` carries `subject`, `predicate`, `object` (all strings), plus bi-temporal `valid_from`/`valid_until`. Retrieval is by embedding similarity or `name`/`subject`/`object` lookup only.
- **Alternatives considered:** facts as edges between resolved entities; facts with optional `subject_ref`/`object_ref` pointers.
- **Rationale:** if both endpoints resolve to entities you'd want to traverse, the right answer is a typed edge — not a fact. The fact's whole point is to avoid inventing throwaway entity nodes for literals or partial-resolution cases. Island-style enforced at the envelope validator: any edge with a `fact` endpoint is dropped at write time.
- **Consequences:** facts can't be reached via graph traversal — only via vector search or string lookup. That's by design. Phase 5 generalizes the supersession resolver so facts can chain (`superseded_by` is the one exception to "no edges" — chains are between two facts only).

### 15. Strict-mode preferences — only the active user holds preferences

- **Decision:** the LLM extraction prompt forbids extracting preferences attributed to third parties. First-person statements ("I prefer X") → emit a `preference` + `has` edge from `person:self`. Third-party statements ("Paul prefers X") → emit a `fact` with `subject="paul"`, `predicate="prefers"`, `object="X"`.
- **Alternatives considered:** allow preferences on any person; allow but tag with subject.
- **Rationale:** preferences exist to drive assistant behavior for the user. Third-party preferences are useful facts but they're not the assistant's compass — letting them in dilutes the preference partition and complicates "show me the user's UI preferences" queries.
- **Consequences:** prompt-enforced from Phase 3 onward (even though the preference enhancements land in Phase 5). The `has` edge from `person:self → preference` is written deterministically by the pipeline, not the LLM.

### 16. Preference connections — only to `person:self` and other preferences

- **Decision:** preferences connect ONLY to `person` (via `has`, always from `person:self` in strict mode) and `preference` (via `superseded_by` for bi-temporal chains, or `same_as` for dedup). Preferences do NOT connect to `object`, `organization`, `location`, `event`, `fact`, `document`, `chunk`, or anything else. Entity scoping ("vegetarian at Italian restaurants") lives in `PreferenceProperties.context` (free string), not as a graph edge.
- **Alternatives considered:** an `applies_to: preference → entity` edge for entity-scoped preferences; a `mentions: chunk → preference` carve-in for provenance via graph.
- **Rationale:** keeps preferences a tight, queryable partition. The `mentions` broadening of Phase 3 explicitly carves preferences out — preference provenance lives in `KnowledgeGraphEntry.sources: list[PydanticObjectId]`, not as a graph edge.
- **Consequences:** if "find preferences scoped to entity X" ever becomes a hot query, `applies_to` is a strict superset of this design and can be added later without breaking anything. Until then: deferred indefinitely.

### 17. Typed-slot `PreferenceProperties` with closed `PreferenceCategory` enum

- **Decision:** replace the existing free-form `content: str` `PreferenceProperties` with typed slots: `statement: str` (≤80 chars canonical statement), `category: PreferenceCategory` (closed enum: `ui`, `language`, `food`, `communication`, `work_style`, `time`, `social`, `aesthetic`, `other`), `target: str | None`, `over: str | None`, `context: str | None`, `strength: Literal["weak", "moderate", "strong"]`.
- **Alternatives considered:** keep `content` free-text; open string `category`.
- **Rationale:** the closed `category` enum drives the supersession partition (only preferences in the same `(user_id, category)` are candidates for `superseded_by`) and makes "show me my communication preferences" a fast indexed query. Typed slots give the LLM clear extraction targets.
- **Consequences:** the existing `PreferenceProperties.content` field is removed; migration is part of the Phase-5 supersession landing.

### 18. Bi-temporal validity + supersession (preferences and facts)

- **Decision:** `valid_from` and `valid_until` (both `datetime | None`, UTC-aware) live on `KnowledgeGraphEntry`. The resolver writes them on preferences and facts. A `superseded_by: preference → preference` edge (and analogous chain for facts) records the chain. Open intervals (`valid_until = None`) are "current". Supersession is triggered by a small LLM-judge call when two preferences share `(user_id, category)` and are semantically contradictory (cosine-similar `statement` but opposite stance) — distinct from dedup (which merges *near-duplicate* preferences via `same_as`).
- **Alternatives considered:** delete-on-update (lose history); single `valid_at` timestamp (no supersession chain).
- **Rationale:** bi-temporal is the most LLM-assistant-relevant feature in the reference — "what did the user prefer in October?" is a real query. Two-branch resolver (contradiction-judge first, dedup-threshold second) avoids the failure mode where embedding similarity alone can't tell "prefers dark mode" from "prefers light mode" apart (both UI prefs, both embed similarly).
- **Consequences:** the LLM judge is a must-have, not optional. Queries gain a bi-temporal filter shape (`valid_until: None` for current, range predicates for historical).

### 19. Dedup tiers — auto-merge (0.95) / flag (0.85) / fuzzy (90) + `SameAsProperties` status lifecycle

- **Decision:** dedup runs three tiers, configured in `DedupConfig` under `tree.config.settings`:
  - cosine ≥ 0.95 → emit `same_as` with `status=confirmed`; resolver merges.
  - cosine in `[0.85, 0.95)` → emit `same_as` with `status=pending`; awaits review.
  - rapidfuzz ratio ≥ 90 → fuzzy match counts toward `same_as` (`match_type=fuzzy` or `both`).
- `SameAsProperties` carries `confidence: float`, `match_type: SameAsMatchType` (`embedding`/`fuzzy`/`both`), `status: SameAsStatus` (`pending`/`confirmed`/`rejected`). Applied uniformly across POLE+O nodes, preferences, and facts. `match_same_type_only=True` — never `person ↔ organization`.
- **Alternatives considered:** auto-merge everything above a single threshold (false-positive merges are unrecoverable); manual-only dedup (doesn't scale).
- **Rationale:** the three tiers + status lifecycle let the dedup pipeline emit *candidate* matches without auto-merging — a reviewer or downstream heuristic flips `pending → confirmed` before `merged_into` is actually set.
- **Consequences:** dedup-threshold branch and contradiction-judge branch (decision 18) are orthogonal but ordered. Contradiction-judge runs first; if it fires, emit `superseded_by` and skip the dedup merge.

### 20. `MentionsProperties` (confidence, start_pos, end_pos)

- **Decision:** `mentions` edges (chunk → any POLE+O entity except `preference`) carry `MentionsProperties` with `confidence: float` (default 1.0), `start_pos: int | None`, `end_pos: int | None` (character offsets within the chunk content, end exclusive).
- **Alternatives considered:** no properties on `mentions` (just an edge presence); confidence only.
- **Rationale:** start/end offsets enable downstream "highlight the mention in chunk text" UX. Confidence lets the extractor express graded certainty about whether the chunk *genuinely* mentions the entity.
- **Consequences:** offsets are `Optional` — extractors that don't compute them can omit, and the lenient field-validator drops invalid offsets without losing the edge.

### 21. Embedding model + dimension pinned in settings (single source of truth)

- **Decision:** `embedding_model: str` and `embedding_dim: int` live in `apps/memory/src/tree/config/settings.py`. Voyage AI (per the project tech stack); specific model identifier and dimension are pinned in Phase 1 (e.g., `voyage-3` at 1024). The mongot vector-index config under `docker/` reads `numDimensions` from this value, as does any future query-time validation. Phase 1 acceptance includes an assertion that `settings.embedding_dim` matches the live mongot index's declared dimension.
- **Alternatives considered:** hard-code the dimension in multiple places; let the embedding model pick its own dimension at runtime.
- **Rationale:** the Atlas Vector Search index definition is dimension-coupled — mismatch = write-time index errors. One source of truth, two readers (writes + mongot config), one parity check.
- **Consequences:** changing the embedding model is a deliberate migration (re-embed + reindex), not a config flip.

## Out of scope (deliberately excluded)

- **Reasoning layer** (ReasoningTrace / Step / ToolCall / Tool node types) — confirmed skip.
- **`:Extractor` node + `EXTRACTED_BY` edge** — replaced by the `extractor: ExtractorInfo` metadata field (decision 10).
- **Dynamic Neo4j-style labels** (`:Entity:Person:Individual`) — pure Neo4j feature; equivalent in MongoDB is `{type, subtype}` + indexes on both.
- **Generic catch-all `related_to` fallback** — the reference's pattern; rejected (decision 8).
- **Wikipedia/Wikidata enrichment** — out of scope.
- **Spatial Point indexes / geocoding** — not in scope unless we want geographic `LOCATION` search.
- **Hybrid (regex / spaCy / structured-rule) extractors** — LLM-only.
- **DB-persisted `:Schema` config** — code-defined registry is enough; revisit only if downstream apps need ontology changes without redeploy.
- **`applies_to: preference → entity` edge** — replaced by `PreferenceProperties.context` (decision 16).
- **Forensic POLE+O subtypes** (suspect / weapon / etc.) — irrelevant to a personal assistant.
- **Free-text `type` attribute on `Location`/`Organization`** (reference legacy) — closed `subtype` set instead.
- **Tool aggregation stats** — no extractor-level rollups planned.
- **`linked_entities` on preferences** — replaced by `context: str` (decision 16).

## Phased rollout

| Phase | Branch | Summary |
|---|---|---|
| **1** | `feat/multi-tenancy` | Add `User` entity + `users` collection, `user_id` on `Document`/`KnowledgeGraphEntry`, `build_node_id(user_id, ...)`, `KGQuery` helper, self-person auto-create hook, embedding model/dim pinned in settings, mongot filter on `user_id`, one-shot migration script, two-user isolation integration test. |
| **2** | small follow-up | Wire `user_id` through `data/conversation_pipeline.py`; define `source_uri` convention for conversations; remove dead "structured messages" sketch. |
| **3** | ontology refactor | `NodeType` enum → `NODE_REGISTRY`; canonical POLE+O types + their `*Properties`; `RELATION_SEMANTICS` registry; collapse domain edges into `related_to` + `semantic_type`; strict-by-omission validation + `extraction_rejections`/`extraction_dropped_fields` logging; `Field(description=...)` discipline; `MentionsProperties`/`SameAsProperties`; `ExtractorInfo` metadata. |
| **4** | facts | Register `fact` as island-style node (zero edges); `FactProperties` (`subject`/`predicate`/`object` + bi-temporal); envelope validator rejects any edge with a `fact` endpoint. |
| **5** | preferences + bi-temporal | Typed-slot `PreferenceProperties` + closed `PreferenceCategory`; strict-mode preference extraction; `has` deterministic edge from `person:self`; `superseded_by` resolver branch (LLM judge); three-tier dedup via `SameAsProperties.status`; bi-temporal generalization to facts. |
| **6** | provenance edges (deferred) | Lift `sources: list[PydanticObjectId]` into `extracted_from` edges only if a concrete query needs it. Skip until then. |

## Open questions / future revisits

- **Per-request MCP `user_id` sourcing** — Phase 1 uses a server-start `--user-id` / `TREE_USER_IDENTIFIER` arg as a module-level constant. Refactoring to a per-request context-var is deferred post-Phase-1.
- **Structured `{role, content}` conversation ingestion** — deferred until a real source hands us reliably delimited turns (Slack export, OpenAI API logs, MCP streaming). Option B in `docs/conversations-storage-tradeoffs.md` becomes the play for that source.
- **Schema-in-graph (`:Schema` node)** — revisit only if multi-tenant ontology divergence becomes a real need.
- **Provenance edges (Phase 6)** — skip until a concrete query exists that `KnowledgeGraphEntry.sources` can't answer.
- **`applies_to: preference → entity` edge** — superset extension over decision 16; add if entity-scoped preference queries become hot.
- **`User.identifier` format-of-truth** — email by default; OIDC `sub` if/when auth is wired. Free string for now.

## References

- `plan.md` — canonical implementation plan and decision history.
- `docs/conversations-storage-tradeoffs.md` — full Option-A/B walk-through for conversation storage.
- `neo4j-agent-memory` reference repo — `agent-memory/notes/DATA_MODELS.md` and `agent-memory/src/neo4j_agent_memory/schema/models.py`.
- `apps/memory/src/tree/entities/knowledge_graph.py` — current `KnowledgeGraphEntry` model (pre-Phase-1).
- `apps/memory/src/tree/entities/ontology.py` — current `NODE_PROPERTIES` / `EDGE_CONSTRAINTS` registries (pre-Phase-3).
- `apps/memory/src/tree/entities/documents.py` — current `Document` / `SourceType` model.
