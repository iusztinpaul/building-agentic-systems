# Plan of attack: porting `neo4j-agent-memory` ideas into `tree`

Reference doc: `/Users/pauliusztin/Documents/01-Projects/test-neo4j-agent-memory/agent-memory/notes/DATA_MODELS.md`

## Decisions locked in (2026-05-16)

These were the four open decisions plus tactical follow-ups. All settled:

1. **Multi-tenant `_id`:** strict isolation — node `_id = "{user_id}:{type}:{name}"`, edge `_id = "{source_node_id}|{type}|{target_node_id}"` (source/target already contain the user prefix). Same canonical name across users = different nodes. No cross-user references in Phase 1. **Two enforcement layers work together**: (a) `_id` embeds `user_id` for correctness — cross-user collisions are impossible at the DB level; (b) a redundant indexed `user_id` field on every row for fast filtered reads (an indexed equality predicate beats a `$regex` prefix scan on `_id`).
2. **Conversations are `Document`s.** A conversation = one `Document(source_type=CONVERSATION)` with `content` = the raw transcript text. No new collections (`conversations`, `messages`) and no new node types (`CONVERSATION`, `MESSAGE`). The existing chunker/extractor handles them like any other text source. Rationale: our inputs are unstructured chat text dumps from sessions like this one — there are no reliable role/turn delimiters to fragment on, so a structured `Message` model adds nothing. Provenance lives at chunk granularity, which is the right size for "passages of the conversation." See `docs/conversations-storage-tradeoffs.md` for the full discussion.
3. **TASK / EPISODE / TOPIC / PROJECT:** registered as **Tree-specific subtype extensions**, not canonical POLE+O.
   - `task → object/task` (action item / conversational throwaway).
   - `episode → event/episode` (retrospective life or work experience).
   - `topic → object/topic` (subject matter discussed in content — pairs with the `mentions` edge for content retrieval).
   - `project → object/project` (lightweight pointer to externally-tracked work; rich details live in Linear/Notion/etc., not in the KG).
   - All four are registered via `register_node_subtype()` (Phase 3 API) — Tree is its own first customer of the extension mechanism. Canonical `object`/`event` subtype sets stay matched to the reference (minus deliberate exclusions).
   - **Subtype-properties: option (a)** — subtype-specific fields are optional fields on either the parent's `*Properties` model OR on a small per-subtype `*Extras` model registered alongside. Simpler than a full `(type, subtype) → BaseModel` registry.
4. **Conversation ingestion path:** keep the existing `ingest_conversation` Prefect pipeline (currently in `data/conversation_pipeline.py`) as a distinct entry point from `data_pipeline`. It writes a `Document(source_type=CONVERSATION, content=raw_text)` and nothing else — downstream chunking and extraction are uniform with every other source. Idempotent re-ingest via `(user_id, source_type=CONVERSATION, source_uri)`, where `source_uri` is a caller-supplied session id (or a content hash if no id is available).
   - **Deferred:** a future structured-input path (e.g., when streaming `{role, content}` from an MCP/API where turns are reliably delimited) can revisit a dedicated `messages` collection. Not now — YAGNI.
5. **Migration story:** wipe and rebuild. Drop `knowledge_graph`; backfill `user_id` on existing `documents` to a single seeded user; re-run the memory pipeline under that user. No dual-format read code.
6. **`user_id` is required everywhere.** No silent default at runtime. Backfill is a one-shot migration, not a fallback.

## Recommendation for user modeling

A separate `users` collection holds the canonical user; every entry that needs tenant scoping carries `user_id`. **No `:User` node inside the KG collection.**

**`User` is the tenant identity, NOT a POLE+O entity.** The active user is tracked via `user_id` (in `users`) and represented in the KG by `person:self` (with `is_active_user=True`). These are two distinct things: `user_id` is the *who-owns-this-row* tag for tenant scoping; `person:self` is the user's representation *as a node within their own KG*. The reference makes the same distinction (`:User` ≠ `:Person`) for the same reason — keeps the ontology clean of tenant-administration concerns.

- **Multi-tenancy scoping:** every read/write filters on `user_id` (indexed property predicate).
- **Self-person auto-creation:** on `User` insert, the application writes a `person` node with `_id = "{user_id}:person:self"`, `name="self"`, `canonical_name=User.attributes.name (or identifier)`, `subtype="individual"`, and `properties.is_active_user=True`. This node is the canonical home for everything the user "owns" in their KG (preferences, future first-person tasks, etc.).
- **No `User.self_person_id` field.** The `is_active_user=True` flag on the person node is the single source of truth — query with `find_one({user_id, type: "person", "properties.is_active_user": true})` (sparse compound index). Eliminates drift between two equivalent claims.
- **First-person resolver:** during extraction, if the LLM emits a `person` node whose name/aliases match the user's display name or known aliases (from `User.attributes`), the resolver redirects the node id to `person:self` before write. ~10 lines, idempotent. Prevents `person:paul` (the user) and `person:paul` (a contact named Paul) from racing for the same `_id`.

This keeps POLE+O semantically clean while giving the user a single explicit entity to attach personal-knowledge edges to.

## Phased migration

Six PR-sized chunks. Order matters — later phases depend on earlier ones.

### Phase 1 — Multi-tenancy foundation

**Branch:** `feat/multi-tenancy`. **Files:** new `entities/users.py`; modify `entities/documents.py`, `entities/knowledge_graph.py`, every pipeline entrypoint and MCP tool.

- New `User` Beanie model in `users` collection. Fields: `id`, `identifier` (unique, e.g. email), `attributes: dict` (name, locale, prefs, etc.), timestamps. **No `self_person_id` field** — the active-user person is found via `properties.is_active_user=True` on the person node itself.
- **Self-person auto-creation hook** on `User.insert()` writes `KnowledgeGraphEntry(_id="{user_id}:person:self", type="person", subtype="individual", name="self", canonical_name=user.attributes.get("name", user.identifier), properties={"is_active_user": True, ...})`. The seed-user migration script (below) calls this hook.
- Add `user_id: PydanticObjectId` (indexed) to `Document` and `KnowledgeGraphEntry`.
- `build_node_id(user_id, type, name) -> "{user_id}:{type}:{name}"`.
- `build_edge_id(source_node_id, type, target_node_id)` unchanged in shape — source/target node ids already carry the user prefix, so edges are tenant-scoped by construction.
- Existing `(source_type, source_uri)` unique index on `Document` becomes `(user_id, source_type, source_uri)`.
- Pipelines, MCP tools, query layer all take `user_id` as a required parameter. No fallback.
- Orchestrator deployments accept `user_id` and pass it through.
- **One-shot migration script** (`scripts/migrate_multi_tenancy.py`):
  1. Create seed user (identifier = configurable, e.g. the local dev email). The `User.insert()` hook auto-writes the `person:self` node for this user.
  2. `update_many` on `documents` to set `user_id` = seed user.
  3. Drop `knowledge_graph` collection (then re-write the seed user's `person:self` node, since step 3 wiped it).
  4. Trigger memory extraction + indexing pipelines for that user.

**Multi-tenancy enforcement details (locked in from the multi-tenancy deep dive):**

- **`User` schema:** `id: PydanticObjectId`, `identifier: Indexed(str, unique=True)` (e.g., email/OIDC sub — stable external handle), `attributes: dict`, timestamps. Lives in `entities/users.py`. **No `self_person_id` field** — the self-person is identified by `properties.is_active_user=True` on the node.
- **Indexes:**
  - `Document`: `(user_id, source_type, source_uri)` unique.
  - `KnowledgeGraphEntry`: at minimum `(user_id, kind, type)` and `(user_id, type, name)`. Existing indexes get `user_id` prepended.
- **Type-system enforcement:** every entry point (pipelines, MCP tools, query layer) takes `user_id: PydanticObjectId` as a **required, non-Optional** parameter. No defaults. Forgetting → type-checker error.
- **`KGQuery` helper class:** all reads of `knowledge_graph` go through a small class whose constructor takes `user_id` and whose methods derive every filter from `self.user_id`. Eliminates "forgot to include user_id" bugs at the call-site level. Grep for raw `KnowledgeGraphEntry.find(...)` in CI to enforce.
- **Atlas Vector/Text Search:** pre-filter on `user_id` via the native filter clause in `$vectorSearch` / `$search.compound.filter`. Mongot config under `docker/` must declare `user_id` as a filterable field. **Not** per-tenant indexes — overkill for this workload.
- **Embedding model + dimension pinned in Phase 1.** The Atlas Vector Search index definition is dimension-coupled — mismatch = index errors at write time. Phase 1 must lock:
  - **Model:** Voyage AI (per `CLAUDE.md` tech stack). Pick a specific model identifier (e.g., `voyage-3` / `voyage-3-large`) and stash it in `settings.py` as a constant.
  - **Dimension:** matches the chosen model (e.g., 1024 for `voyage-3`). Same value flows into: `KnowledgeGraphEntry.embedding` annotation (informally; Pydantic doesn't enforce list length), the mongot vector index config (`numDimensions`), and any future query-time validation.
  - **Where it lives:** `tree.config.settings` carries `embedding_model: str` and `embedding_dim: int`; the indexing pipeline and mongot config both read from it. A mismatch test in Phase 1 acceptance: assert `settings.embedding_dim` matches whatever the live mongot index declares.
- **MCP server `user_id` sourcing (Phase 1 default):** server starts with a `--user-id <objectid>` / `TREE_USER_IDENTIFIER=<identifier>` arg; every tool call uses that. Multi-tenant request-scoped sourcing is a small refactor later (module-level constant → per-request context-var).
- **Known gap surfaced 2026-05-16:** the existing `mcp__tree-memory__ingest_conversation` tool exposes no `user_id` parameter — it falls back to a session-level default. Phase 1 must add `user_id` to the tool signature (or resolve it from server startup config) before any multi-tenant story is real.

**Phase 1 acceptance — the single most valuable integration test:** a **two-user isolation test**. Seed two users (A, B). Ingest different documents/conversations for each. Run memory extraction for each. Then exercise *every* query path (find by type, find by name, neighbors, semantic search, text search) for user A and assert zero rows belonging to user B leak through. One test catches a whole class of regressions.

**Open tactical questions in Phase 1:**
- Format-of-truth for `User.identifier`: email by default; OIDC `sub` if/when we wire auth. Stays a free string for now.

### Phase 2 — Conversation ingestion polish (small)

The big Phase 2 (Message modeling, KG-as-chat, sliding-window extraction) is **gone** — conversations are just Documents (decision #2). What remains is a small cleanup pass.

**Files:** `data/conversation_pipeline.py`, possibly `entities/documents.py`.

- Wire `user_id` through `ingest_conversation` (required, no fallback — per decision #6).
- Define `source_uri` semantics for conversations: prefer caller-supplied session id; fall back to a content hash if none. Document the convention.
- Confirm the chunker handles long transcripts reasonably (existing splitter is `langchain-text-splitters`; chat transcripts are typically shorter than articles so this should be fine — verify with a long-session test).
- Optional small UX field on `Document` for conversations: `metadata.session_started_at` (tz-aware UTC). Stored in the existing `metadata: dict` if it exists, otherwise leave for later.
- Remove any dead code on the old "structured messages" path that was sketched but never landed.

That's it for Phase 2. Real complexity moves to Phase 3 (ontology refactor).

**Deferred (revisit only when needed):**
- Structured `{role, content}` ingestion path with a dedicated `messages` collection. Trigger to revisit: a real source that hands us reliably delimited turns (Slack export, OpenAI API logs, etc.).

### Phase 3 — POLE+O ontology refactor

**Files:** `entities/knowledge_graph.py`, `entities/ontology.py`.

The big one — make the ontology extensible AND land the full POLE+O node-type set (Person, Organization, Location, Event, Object) plus all their typed edges. Gap analysis against the reference is in this conversation's tree-memory entry from 2026-05-16.

**Strategy:** stop treating `NodeType` as a closed enum. Move to a registry where built-in types are registered at import time and downstream apps can register custom types.

```python
# ontology.py

@dataclass(frozen=True)
class NodeTypeSpec:
    name: str                                # "person", "object", "event", ...
    properties_schema: type[BaseModel]
    description: str
    subtypes: set[str] | None = None         # None = freeform; set = closed
    llm_extractable: bool = True
    # Phase 3 open: subtype_properties: dict[str, type[BaseModel]] | None = None
    # vs. one superset properties_schema with optional subtype fields.

NODE_REGISTRY: dict[str, NodeTypeSpec] = {}
EDGE_REGISTRY: dict[str, EdgeTypeSpec] = {}    # same pattern for edges

def register_node_type(spec: NodeTypeSpec) -> None: ...
def register_edge_type(spec: EdgeTypeSpec) -> None: ...
```

**Node types to register (LLM-extractable, the POLE+O canonical set):**

| `name` | Subtypes (closed) | Type-specific properties |
|---|---|---|
| `person` | `individual`, `alias`, `persona` | `email?`, `date_of_birth?`, `nationality?`, `occupation?` |
| `organization` | `company`, `nonprofit`, `government`, `educational`, `political`, `religious`, `military` | `jurisdiction?`, `registration_number?` |
| `location` | `address`, `city`, `region`, `country`, `landmark`, `coordinates` | `address?`, `city?`, `country?`, `coordinates?` |
| `event` | `incident`, `meeting`, `transaction`, `communication`, `travel`, `employment`, `observation` (canonical POLE+O only) | `date?`, `time?`, `duration?`, `outcome?` |
| `object` | `vehicle`, `phone`, `email`, `document`, `device`, `software` (canonical POLE+O only) | `identifier?`, `make?`, `model?`, `serial_number?` |

**Extension API for downstream apps to register custom subtypes.** Tree is the first consumer of this API — `task`, `episode`, `topic`, `project` all come in this way, NOT as canonical POLE+O subtypes. Validates the extensibility story by self-application.

```python
@dataclass(frozen=True)
class SubtypeSpec:
    name: str
    description: str
    extra_properties: type[BaseModel] | None = None    # optional Pydantic model with extra fields

def register_node_subtype(
    parent_type: str,                                  # must already be registered
    subtype: str,                                      # snake_case
    description: str = "",
    extra_properties: type[BaseModel] | None = None,
) -> None:
    """Add a custom subtype to an existing closed-subtype node type.
    Raises if parent_type is unknown or has freeform (None) subtypes."""
```

At validation time: parent's `properties_schema` is the base; if the row's `subtype` has `extra_properties`, those are also validated against the row's `properties` dict.

**Tree's subtype extensions** (registered in `entities/ontology_tree_extensions.py` or at the bottom of `ontology.py` under a clearly-marked section — keeps the canonical POLE+O block clean):

```python
# Tree personal-assistant extensions
register_node_subtype("object", "task",    description="Action item or conversational throwaway")
register_node_subtype("event",  "episode", description="Retrospective life or work experience")
register_node_subtype("object", "topic",   description="Subject matter discussed in content")

class ProjectExtras(BaseModel):
    external_ref: ExternalRef | None = None            # {system, id, url?} — see below
register_node_subtype("object", "project",
                       description="Pointer to externally-tracked project",
                       extra_properties=ProjectExtras)

class ExternalRef(BaseModel):
    system: str                                        # e.g. "linear", "notion", "todoist"
    id: str                                            # external system's id
    url: str | None = None
```

| `parent_type` | `subtype` | Extra properties | Notes |
|---|---|---|---|
| object | task | — | Tasks the user mentions in conversation. Throwaway-friendly. |
| event | episode | — | Episodes / retrospective experiences. |
| object | topic | — | Subject matter / knowledge domain. Pairs with `mentions` edge from chunks. |
| object | project | `external_ref: ExternalRef \| None` | Lightweight handle; rich details (status, tasks, deadlines) live in the task manager. LLM populates `name` only; `external_ref` set via direct write (MCP tool / sync job), not LLM extraction. |

**No new `RELATION_SEMANTICS` for tasks/projects.** Tasks and projects don't connect in the KG — their relationship lives in the task manager. Skips the `part_of_project` edge entirely.

**Graph-modeling meta fields live on `KnowledgeGraphEntry` (decision: option A).** Type-specific fields live in the per-type `*Properties` model and serialize into `KnowledgeGraphEntry.properties`. No base `EntityProperties` Pydantic class — each POLE+O type's `*Properties` is independent and carries only that type's domain fields.

Common fields **on `KnowledgeGraphEntry`** (added or already present):
- `name` (already)
- `canonical_name` (already)
- `aliases: list[str]` (already)
- `subtype: str | None` (new — validated against the parent type's `subtypes` set when closed)
- `description: str | None` (new)
- `confidence: float` (already on nodes; new on edges)
- `embedding: list[float]` (already)
- `merged_into: str | None` (already)
- `valid_from: datetime | None`, `valid_until: datetime | None` (new — bi-temporal; nodes for facts/preferences in Phase 4/5, edges everywhere)
- `extractor: ExtractorInfo | None` (new — see below)
- `semantic_type: str | None` (new — populated only on `related_to` edges; validated against `RELATION_SEMANTICS`; indexed)

**Extractor provenance as metadata, not a separate node.**

```python
class ExtractorInfo(BaseModel):
    name: str                              # e.g., "gemini-2.5-pro"
    version: str                           # model version or pipeline release tag
    extraction_time_ms: int | None = None  # optional perf metric
```

Every row written by the extraction pipeline carries `extractor` populated by the running pipeline (single source today; the field accepts the future case where multiple extractors coexist without needing a `:Extractor` node + `EXTRACTED_BY` edge). Cheaper than a separate node, queryable via `find({extractor.name: "gemini-2.5-pro", extractor.version: "..."})`.

Structural rows (Document, Chunk) skip `extractor` — they aren't produced by an LLM.

**POLE+O domain edges — collapsed into a single `related_to` edge type with a `semantic_type` discriminator** (mirrors the reference's `:RELATED_TO {type: ...}` pattern).

All POLE+O domain relations are stored as `KnowledgeGraphEntry(type="related_to", semantic_type="employed_by"|"knows"|…)`. The semantic-type catalogue lives in a separate registry `RELATION_SEMANTICS: dict[str, RelationSemanticSpec]` that validates allowed `(source_type, target_type)` pairs per semantic.

```python
@dataclass(frozen=True)
class RelationSemanticSpec:
    name: str                                    # "employed_by", "knows", ...
    allowed_pairs: list[tuple[str, str]]          # [(source_type, target_type), ...]
    properties: dict[str, type]                    # property name → expected python type
    description: str

RELATION_SEMANTICS: dict[str, RelationSemanticSpec] = {}
```

| `semantic_type` | Allowed (source → target) pairs | Semantic-specific properties (live in `properties` dict) |
|---|---|---|
| `knows` | person → person | — |
| `alias_of` | person → person | — |
| `member_of` | person → organization | `role`, `start_date`, `end_date` |
| `employed_by` | person → organization | `position`, `start_date`, `end_date` |
| `owns` | person\|organization → object | `acquisition_date`, `status` |
| `uses` | person → object | — |
| `located_at` | person\|object\|organization\|event → location | `from_date`, `to_date`, `status` |
| `resides_at` | person → location | `from_date`, `to_date` |
| `headquarters_at` | organization → location | — |
| `participated_in` | person\|organization → event | `role` |
| `occurred_at` | event → location | — |
| `involved` | event → object | `role` |
| `subsidiary_of` | organization → organization | — |
| `partner_with` | organization → organization | — |

**Why the collapse (reversal of an earlier "skip" decision).** Two motivations:
1. **Parity with the reference's extraction prompt.** The LLM emits a single relation envelope with a `type` field; we don't need 14 separate prompt sections.
2. **Cleaner registry shape.** A new typed relation = one `RELATION_SEMANTICS` entry, no new `EdgeType` enum churn, no new edge constraint plumbing per relation.

Cost: queries like "find all employment edges" become `find({type: "related_to", semantic_type: "employed_by", user_id: ...})` — one extra equality predicate. Cheap with the `(user_id, type, semantic_type)` compound index.

**Strict-by-omission policy.** No generic catch-all `related_to` fallback (the reference's pattern is rejected). Relationships the LLM emits that don't fit any of the 14 named semantics, or violate a `(source_type, target_type)` pair constraint, are **dropped at validation**. The ontology is a hard contract on what the KG can express.

**Phase 3 acceptance — rejected-extraction logging.** When the validator rejects an edge, log the attempted `(semantic_type, source_type, target_type)` triple (plus a sample of the chunk text) to a `extraction_rejections` collection or structured log stream. This surfaces schema gaps as signal: if `(person, "mentored_by", person)` shows up 50 times across users, that's the trigger to add a new named semantic — not to relax the validator. Without this logging, the strict policy becomes silent data loss.

**Structural edges stay as distinct `EdgeType` enum values** — they aren't POLE+O domain relations and benefit from their own type identity (`part_of`, `next`, `mentions`, `referenced`, `same_as`). `mentions` is broadened: chunk → any POLE+O entity. `same_as` is broadened: any POLE+O entity → same type.

**Structural-edge property models** (same `Field(description=...)` + lenient field-validation rules as POLE+O edges):

```python
class MentionsProperties(BaseModel):
    """Properties for chunk -> entity mention edges (extraction provenance)."""
    confidence: float = Field(
        default=1.0,
        description="LLM confidence the chunk genuinely mentions this entity (0.0-1.0)."
    )
    start_pos: int | None = Field(
        default=None,
        description="Character offset of the mention's start within the chunk content. Optional."
    )
    end_pos: int | None = Field(
        default=None,
        description="Character offset of the mention's end (exclusive) within the chunk content. Optional."
    )


class SameAsMatchType(StrEnum):
    EMBEDDING = "embedding"            # cosine similarity over node embeddings
    FUZZY = "fuzzy"                    # rapidfuzz string match on name/aliases
    BOTH = "both"                      # both signals agreed


class SameAsStatus(StrEnum):
    PENDING = "pending"                # candidate awaiting human / heuristic confirmation
    CONFIRMED = "confirmed"            # accepted: targets can be merged
    REJECTED = "rejected"              # explicitly not the same entity


class SameAsProperties(BaseModel):
    """Properties for same_as edges (entity dedup)."""
    confidence: float = Field(description="Match confidence (0.0-1.0); from the dedup branch that emitted the edge.")
    match_type: SameAsMatchType = Field(description="Which signal produced the match.")
    status: SameAsStatus = Field(default=SameAsStatus.PENDING, description="Lifecycle state — supports human-in-the-loop dedup confirmation.")
```

`MentionsProperties.start_pos`/`end_pos` enable downstream "highlight the mention in chunk text" UX. `SameAsProperties.status` lets the dedup pipeline emit *candidate* matches (status=`pending`) without auto-merging — a reviewer or downstream heuristic flips them to `confirmed` before the resolver actually merges (`merged_into`).

**Schema changes to `KnowledgeGraphEntry` (consolidated):**
- `type: str` (was `NodeType`/`EdgeType` union) — validated against `NODE_REGISTRY` (nodes) or the structural-edge set + `"related_to"` (edges).
- All the new common fields listed above (`subtype`, `description`, `valid_from`, `valid_until`, `extractor`, `semantic_type`).
- Indexes: prepend `user_id`; add `(user_id, type, semantic_type)` for `related_to` filtering; add `(user_id, kind, type, name)` to support fast canonical lookups.
- `properties: dict[str, Any]` — already exists in shape; now serializes per-type `*Properties` payload AND per-semantic edge properties (role, dates, position, etc.).

**LLM extraction prompt updates:** `get_ontology_schema()` reads from the registry and emits the schemas for all LLM-extractable POLE+O types. The prompt grows substantially — worth an eval comparing extraction quality on the existing test set before vs. after.

Two policy rules also land in the prompt (these are *behavioral* rules the schema can't enforce on its own):
1. **Strict-mode preferences** (Phase 5 policy, prompt-enforced from Phase 3 onward): only extract preferences attributed to the speaker (the user). Third-party preference statements → emit a `fact` (Phase 4), not a `preference`.
2. **First-person redirect** (Phase 1 resolver, prompt-aware): the LLM emits `person` nodes for any named individual including the speaker; the post-LLM resolver redirects first-person matches to `person:self`.

**Field-level validation of LLM extraction output (new Phase 3 acceptance).**

The LLM sees the property schema in the prompt; the pipeline validates the LLM's emission against the same schema and filters down to what's valid. Two-tier policy — envelope-level strict, field-level lenient:

```
LLM output (JSON)
  │
  ▼
┌─ Envelope validation (STRICT) ──────────────────────────────┐
│  - type is a registered name (NODE_REGISTRY or related_to)  │
│  - if edge: semantic_type is a registered name in            │
│    RELATION_SEMANTICS AND (source_type, target_type)         │
│    pair is allowed                                           │
│  - if node: name is non-empty (deterministic _id needs it)   │
│  - subtype, if set, is in the parent type's allowed set      │
│                                                              │
│  FAIL → drop the whole row, log to extraction_rejections     │
└──────────────────────────────────────────────────────────────┘
  │
  ▼ (envelope OK)
┌─ Field-level validation (LENIENT) ──────────────────────────┐
│  For each key in raw `properties` dict:                      │
│    try → Pydantic validate against the type's *Properties    │
│           (plus subtype's extra_properties if present)       │
│    pass → keep the field                                     │
│    fail → drop the field, log to extraction_dropped_fields   │
│                                                              │
│  The row is written with whatever validated, INCLUDING when  │
│  all property fields fail. An entity with empty properties is│
│  still a useful identity claim — the resolver/dedup step can │
│  merge it with later better-formed emissions via same_as.    │
└──────────────────────────────────────────────────────────────┘
  │
  ▼
KnowledgeGraphEntry (validated, upserted)
```

**Why the asymmetry between edge-level (strict) and field-level (lenient):**

- An edge with an unmatched semantic OR a disallowed source/target pair represents a relationship the ontology *cannot express*. There's nothing salvageable. Drop it.
- A node with `type=person, name=alice` is a useful identity claim even if `email` and `nationality` are garbage. The node identity is the durable part; field payload is independent and individually droppable.

This means most `*Properties` fields will be marked `Optional` (`field: T | None = None` or with a `default_factory`). Fields that *would* be required are technically still "required" in the prompt schema (so the LLM is told to provide them), but the validator treats them like every other field: invalid → dropped, not reject-the-row.

**Implementation shape:**

```python
def validate_properties(
    raw: dict[str, Any],
    schema: type[BaseModel],
    extras: type[BaseModel] | None = None,           # subtype extras, if any
) -> tuple[dict[str, Any], list[ValidationError]]:
    """Per-field validation: keep valid, drop invalid, never raise."""
    validated: dict[str, Any] = {}
    errors: list[ValidationError] = []
    combined_fields = {**schema.model_fields, **(extras.model_fields if extras else {})}
    for key, value in raw.items():
        if key not in combined_fields:
            errors.append(...)  # unknown field, drop
            continue
        field = combined_fields[key]
        try:
            # Validate against the single field's annotation
            adapter = TypeAdapter(field.annotation)
            validated[key] = adapter.validate_python(value)
        except ValidationError as e:
            errors.append(e)   # individual field invalid, drop
    return validated, errors
```

Lives in the extraction pipeline between the LLM call and the write step — between Task 2 (LLM extract) and Task 3 (resolve) in the existing 6-task pipeline.

**`Field(description=...)` discipline on every attribute (Phase 3 requirement).**

Every attribute on every `*Properties` model (and every `RelationSemanticSpec.properties` model — see edge symmetry below) MUST have a `Field(description="…")`. The description flows directly into the prompt via `model_json_schema()` and is the LLM's *only* context for what the field means. Missing descriptions = vague extraction.

Style: brief, action-oriented, with examples where ambiguous. ≤15 words is the target.

```python
# Good
class PersonProperties(BaseModel):
    email: str | None = Field(
        default=None,
        description="Primary email address. ISO RFC 5322 format. Omit if not stated."
    )
    occupation: str | None = Field(
        default=None,
        description="Current professional role or job title (e.g. 'software engineer')."
    )

# Bad — what's a 'description' for a person?
class PersonProperties(BaseModel):
    description: str | None = Field(default=None)   # ← LLM has no idea what to put here
```

**Edge symmetry — `RelationSemanticSpec.properties` becomes `type[BaseModel]`.**

For consistency, edge property schemas use the same Pydantic + `Field(description=...)` shape as node properties, and validate via the same `validate_properties()` function:

```python
@dataclass(frozen=True)
class RelationSemanticSpec:
    name: str
    allowed_pairs: list[tuple[str, str]]
    properties: type[BaseModel] | None = None       # was: dict[str, type]
    description: str

# Example
class EmployedByProperties(BaseModel):
    position: str | None = Field(default=None, description="Job title or role at the organization.")
    start_date: datetime | None = Field(default=None, description="Employment start date (ISO 8601).")
    end_date: datetime | None = Field(default=None, description="Employment end date; omit if current.")

RELATION_SEMANTICS["employed_by"] = RelationSemanticSpec(
    name="employed_by",
    allowed_pairs=[("person", "organization")],
    properties=EmployedByProperties,
    description="Person is/was employed by an organization.",
)
```

Edge-level structural validation (allowed pair, registered semantic) stays strict — drops the whole edge. Edge field-level validation is lenient — drops invalid fields, keeps the edge.

**Logging surfaces (Phase 3 acceptance, two collections):**

- `extraction_rejections` — already in plan; rows fully dropped (envelope-level failures, unmatched edges).
- `extraction_dropped_fields` — new; field-level drops. Schema: `{user_id, type, subtype, semantic_type?, dropped_keys: list[str], raw_values: dict, errors: list[str], chunk_id, timestamp}`. Audit periodically to find LLM patterns that point at missing fields ("the LLM keeps trying to emit `phone_number` on person — promote it to a real field").

**Migration:** keep `NodeType` enum as a thin compat shim re-exporting registered names. Old `TASK` and `EPISODE` references get either re-routed (TASK → `("object", "task")`) or deprecated. Eventually delete.

**Note: TASK and EPISODE fold-in is a Phase 3 concern, not earlier** — until the registry + subtype field land, the existing enum stays as-is.

### Phase 4 — Facts (escape-hatch proposition type)

Register `fact` in `NODE_REGISTRY` as **LLM-extractable, island-style** (no edges to other nodes — retrieved by embedding similarity only). Reference §4.4 spells out the decision tree for when to emit a `fact` vs. an entity edge vs. a preference; lift that directly into the extraction prompt.

**`FactProperties`:**
- `subject: str` — the proposition's left side (can be a free-text string OR a resolved node name)
- `predicate: str` — the relation (e.g., "prefers", "lives_in", "speaks")
- `object: str` — the right side
- `valid_from: datetime | None`, `valid_until: datetime | None` — bi-temporal
- Plus the common `confidence`, `embedding` already on `KnowledgeGraphEntry`.

**Why island-style.** If both subject and object resolve to entities you'd want to traverse, emit a typed edge instead. If neither (or only one) resolves, you'd be forced to invent throwaway entity nodes — the fact's whole point is to avoid that. Facts are queryable via vector similarity (`"what does Paul prefer for editors?"` → top-k facts by embedding) without contaminating the entity graph.

**Edge constraints — explicit "island" enforcement.** `fact` is registered in `NODE_REGISTRY` but participates in **zero** entries in `RELATION_SEMANTICS` and is **not** an allowed target/source for any structural edge (`mentions`, `same_as`, `has`, `part_of`, `next`, `referenced`, `superseded_by`). The envelope-level edge validator (Phase 3) rejects any edge with a `fact` endpoint at write time — facts cannot be linked, period. Retrieval is by `name`/`subject`/`object` string lookup or embedding similarity only.

Independent of Phase 3 mechanically, but only meaningful once the registry exists (so it can be added without enum-editing). Lands as the second-smallest phase after Phase 2.

### Phase 5 — Preference enhancements + bi-temporal supersession

Enhance the existing `preference` node into a first-class, temporally aware, tightly-scoped entity. The most LLM-assistant-relevant feature in the reference.

**Strict-mode preference extraction (policy, not just schema):**

- **Only the active user holds preferences.** The LLM extraction prompt explicitly forbids extracting preferences attributed to third parties. First-person statements ("I prefer X") → emit a `preference` node + `has` edge from `person:self`. Third-party statements ("Paul prefers X") → emit a `FACT` node (Phase 4) with `subject="paul"`, `predicate="prefers"`, `object="X"`, not a preference.
- **Every preference attaches to exactly one node: `person:self`.** The `has` edge from self → preference is written deterministically by the pipeline, not by the LLM (the LLM emits only the preference node).

**Connection rule (tight by design):**

```
Preferences connect ONLY to:
  - person      (via `has` — always from person:self in strict mode)
  - preference  (via `superseded_by` or `same_as` — bi-temporal chain / dedup)

Preferences DO NOT connect to:
  - object, organization, location, event, fact, document, chunk, anything else
```

Entity scoping (e.g., "I prefer vegetarian at Italian restaurants") lives in the `context` field on the preference node, **not** as a graph edge. If "find preferences scoped to entity X" ever becomes a hot query, an `applies_to` edge can be added later — it's a strict superset of this design.

**`mentions` carve-out:** the broadened Phase 3 `mentions` edge (chunk → any POLE+O entity) does **not** target `preference`. Preference provenance lives in the existing `KnowledgeGraphEntry.sources: list[PydanticObjectId]` field.

**Typed-slot `PreferenceProperties`** (replaces the free-form `content: str`):

```python
class PreferenceCategory(StrEnum):
    UI = "ui"
    LANGUAGE = "language"
    FOOD = "food"
    COMMUNICATION = "communication"
    WORK_STYLE = "work_style"
    TIME = "time"
    SOCIAL = "social"
    AESTHETIC = "aesthetic"
    OTHER = "other"

class PreferenceProperties(BaseModel):
    statement: str = Field(description="Short canonical preference statement, ≤80 chars (e.g., 'prefers dark mode')")
    category: PreferenceCategory                                # closed enum — drives filter queries
    target: str | None = Field(description="What is preferred — resolved entity name OR free string for abstract concepts")
    over: str | None = Field(description="What is dis-preferred when comparative")
    context: str | None = Field(description="When/where the preference applies (replaces graph-edge scoping)")
    strength: Literal["weak", "moderate", "strong"] = "moderate"
```

Plus the common `confidence`, `embedding`, `valid_from`, `valid_until`, `extractor` on `KnowledgeGraphEntry`. The closed `category` enum drives the supersession partition (next section) and makes "show me my communication preferences" a fast indexed query.

**New edge types to register:**

| `name` | source → target | Purpose |
|---|---|---|
| `superseded_by` | preference → preference | Bi-temporal chain — newer preference points at the one it replaced. Resolver-written, never LLM-emitted. |

(`applies_to` is **NOT** in this plan — deferred indefinitely. Scoping lives in `PreferenceProperties.context`.)

**Resolver change — "preference contradiction" branch:** when two preferences share `(user_id, category)` and are semantically contradictory (cosine-similar `statement` embeddings but opposite stance — confirmed by a small LLM judge call), the new one:

1. Sets `valid_from = now` on itself.
2. Sets `valid_until = now` on the old one.
3. Emits `new -[superseded_by]-> old`.

This is distinct from the dedupe branch (which merges *near-duplicate* preferences via `same_as`). Don't conflate them — embedding similarity alone can't tell "prefers dark mode" from "prefers light mode" apart (both UI prefs). The LLM judge is the must-have piece.

**Dedup thresholds (config, three tiers).** The reference's three-tier model maps cleanly onto our `same_as` lifecycle via `SameAsProperties.status`:

```python
class DedupConfig(BaseSettings):
    auto_merge_threshold: float = 0.95   # embedding cosine ≥ this → emit same_as with status=confirmed; resolver merges
    flag_threshold: float = 0.85         # embedding cosine in [flag, auto_merge) → emit same_as with status=pending; awaits review
    fuzzy_threshold: int = 90            # rapidfuzz ratio ≥ this → fuzzy match counts toward same_as (match_type=fuzzy or both)
    match_same_type_only: bool = True    # only consider same-type candidates (person↔person, never person↔organization)
```

These live in `tree.config.settings` and are applied uniformly across all entity types (POLE+O nodes, preferences, facts). The resolver writes `SameAsProperties.confidence` = the similarity score that produced the match, so downstream review tooling can sort by closeness.

**Contradiction-judge branch is orthogonal to dedup-threshold branch.** A pair of preferences can simultaneously be embedding-similar (would trigger dedup) AND semantically contradictory (would trigger supersession). The contradiction-judge runs *first* — if it fires, emit `superseded_by`, skip the dedup merge. If it doesn't fire (preferences agree or just one is a paraphrase), the dedup branch runs and emits `same_as`.

Queries gain bi-temporality:

```python
# "What is the user's current UI preference?"
KGQuery(user_id).find_nodes(
    type="preference",
    filter={"properties.category": "ui", "valid_until": None}
)

# "What was the user's UI preference in October 2025?"
ts = datetime(2025, 10, 1, tzinfo=UTC)
KGQuery(user_id).find_nodes(
    type="preference",
    filter={
        "properties.category": "ui",
        "valid_from": {"$lte": ts},
        "$or": [{"valid_until": None}, {"valid_until": {"$gt": ts}}],
    }
)
```

**Same bi-temporal pattern applies to `fact`** — `valid_from`/`valid_until` are already on `FactProperties` per Phase 4. Phase 5 generalizes the supersession resolver so facts can chain too (when a new fact contradicts an existing one on the same `(subject, predicate)`).

### Phase 6 — Provenance edges as a real graph layer

Today provenance lives in `KnowledgeGraphEntry.sources: list[PydanticObjectId]`. The reference uses `EXTRACTED_FROM` edges. Both work. Lift to edges only when there's a concrete query we can't answer today (e.g., "which entities did this single message produce?"). Skip until needed.

## What to skip / can't translate

- **Reasoning layer** — confirmed skip.
- **Hybrid extractors** (regex/spacy/structured rules) — confirmed LLM-only.
- **Dynamic Neo4j labels** (`:Entity:Person:Individual`) — pure Neo4j feature. Equivalent in MongoDB is `{type: "person", subtype: "individual"}` + indexes on both.
- **Spatial Point indexes / geocoding** — not in scope unless we want `LOCATION` search.
- **Wikipedia/Wikidata enrichment** — out of scope.
- **`:Extractor` provenance *node* + `EXTRACTED_BY` edge** — replaced by an `extractor: ExtractorInfo` metadata field on every node/edge (Phase 3). Lighter and still queryable.
- **DB-persisted `:Schema` config** — code-defined registry is enough; revisit only if users want ontology changes without redeploy.

**Reversals (what we *are* now doing, but earlier marked skip):**
- `RELATED_TO {type: ...}` collapse trick — Phase 3 adopts this for all POLE+O domain edges. Reasons in Phase 3 section.

## Things not mentioned in the original notes but worth considering

1. **Bi-temporal for preferences + supersession** (Phase 5) — the most useful reference idea for a personal assistant.
2. **`FACT` as escape hatch** (Phase 4) — without it the LLM has to invent entities for literals (bad) or drop information (worse).
3. **Provenance for conversations** — since conversations are Documents and chunks are KG nodes (`NodeType.CHUNK`), provenance is already covered: KG entries' `sources` list chunk ids, and chunks point back at the conversation Document via `PART_OF`. No schema change needed. The `SourceRef` (collection + id) generalization noted earlier in this plan is *not* required for Phase 1 — `sources: list[PydanticObjectId]` stays as it is.

(Items previously here — bi-temporal, FACT, subtypes everywhere, edge `confidence` — have been promoted into Phase 3/4/5 as concrete scope.)

## Current-state snapshot (pre-Phase-1)

**Existing collections:**
- `documents` — `Document` model at `apps/memory/src/tree/entities/documents.py:19`. Indexed by `(source_type, source_uri)` unique. Conversations currently shoehorned in as `SourceType.CONVERSATION`.
- `knowledge_graph` — `KnowledgeGraphEntry` model at `apps/memory/src/tree/entities/knowledge_graph.py:54`. Holds both nodes and edges; node `_id = "type:name"`, edge `_id = "source|type|target"`.

**Existing closed enums (`knowledge_graph.py:13`+):**
- `NodeType`: DOCUMENT, CHUNK, PERSON, TASK, EPISODE, PREFERENCE
- `EdgeType`: PART_OF, NEXT, MENTIONS, REFERENCED, RELATED_TO, TODO, EXPERIENCED, HAS, SAME_AS

**Existing ontology wiring** (`apps/memory/src/tree/entities/ontology.py`):
- `NODE_PROPERTIES` dict maps each `NodeType` → `*Properties` Pydantic model.
- `EDGE_CONSTRAINTS` supports multiple `(source_type, target_type)` pairs per edge.
- `LLM_EXTRACTABLE_NODE_TYPES = {PERSON, TASK, EPISODE, PREFERENCE}`.
- `LLM_EXTRACTABLE_EDGE_TYPES = {RELATED_TO, TODO, EXPERIENCED, HAS}`.
- `get_ontology_schema()` builds the JSON schema injected into the LLM extraction prompt.

**Pipeline shape** (`apps/memory/src/tree/memory/extraction/pipeline.py`): 6 Prefect tasks — extract chunks + structural edges → LLM extract → resolve → embed → dedupe → write. Idempotent, retryable, checkpointed.

**No user/tenant concept anywhere.** Conversations already flow through `data/conversation_pipeline.py` as `Document(source_type=CONVERSATION)` — that path stays (decision #2), just gets `user_id` plumbing in Phase 1/2.

## Next step

Kick off **Phase 1** on `feat/multi-tenancy` with TDD:
1. Write unit tests for `User` model, `build_node_id` with `user_id`, and `Document` / `KnowledgeGraphEntry` user_id propagation.
2. Implement the entities + id builders.
3. Wire `user_id` through pipelines + MCP tools (each propagation is a small, testable change).
4. Write the migration script + integration test that runs it against a fresh local Mongo.
5. End-to-end run: seed user → backfill docs → drop KG → memory pipelines → query, confirm everything scoped to the seed user.
