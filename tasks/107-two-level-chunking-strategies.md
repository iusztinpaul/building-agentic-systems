---
id: 107-two-level-chunking-strategies
feature: rag-graphrag-modes
status: pending
---

# Two-level chunking (**Parent chunk** / **Child chunk**) with `fixed_tokens` | `recursive` strategies and **Contextual header** text

Tags: `memory`, `rag`, `config`, `entities`
Depends on: #105
Blocks: #108
Implements: ADR-006 — Decisions 2, 4, 6

## Scope

Pure building blocks only (no pipeline wiring — that is #108, which also deletes the old
`chunk_document` and `extraction.chunk_size/chunk_overlap`).

**1. Config** — extend `MemoryConfig` (from #105):
```yaml
memory:
  mode: graphrag
  chunking:
    strategy: recursive        # fixed_tokens | recursive
    parent: { size: 4096, overlap: 0 }     # tokens (tiktoken cl100k_base)
    child:  { size: 256,  overlap: 32 }
```
Pydantic: `ChunkLevelConfig(size: int > 0, overlap: int >= 0)` with a validator
`overlap < size`; `ChunkingConfig(strategy: Literal["fixed_tokens","recursive"], parent, child)`
with a cross-key validator `child.size < parent.size`. Both YAML files (default + frozen
fixture) gain the block. Env hatch works unchanged, e.g.
`TREE_MEMORY__CHUNKING__CHILD__SIZE=128`. Leave `extraction.chunk_size/chunk_overlap` in
place for now (#108 removes them together with their only consumer).

**2. Splitter** — `tree/memory/rag/chunking.py`, deterministic, tiktoken `cl100k_base`
token-bounded (reuse the module-level encoder pattern from `extraction/core.py`):
- Types (Pydantic, `tree/memory/rag/types.py`): `ChildChunk(index: int, content: str)`,
  `ParentChunk(index: int, content: str, heading_path: list[str], children: list[ChildChunk])`.
- `split_document(text: str, config: ChunkingConfig) -> list[ParentChunk]`: split `text` into
  parents with `config.parent`, then each parent's `content` into children with
  `config.child`, SAME strategy at both levels. Children never cross a parent boundary.
  Empty/whitespace-only text → `[]`. A parent with content shorter than `child.size` tokens
  has exactly one child equal to its content.
- `fixed_tokens`: today's algorithm from `chunk_document` (sliding token window with
  overlap); `heading_path == []` for every parent.
- `recursive`: split on the first separator level that yields pieces, in this order:
  markdown ATX headings (`^#{1,6} `, section = heading line + body; the heading path is the
  stack of heading TEXTS from `#` down to the current level, e.g. `["Memory", "Parent
  retrieval"]`), then blank-line paragraphs, then sentences (`(?<=[.!?])\s+`), then raw
  tokens. Adjacent pieces are greedily merged while the merged token count ≤ `size`; a
  single piece longer than `size` recurses to the next separator level. `overlap` carries
  the last `overlap` tokens of the previous chunk into the next chunk at the same level.
  A parent's `heading_path` is the heading stack in force at its first token; a child
  inherits its parent's `heading_path` (children do not get their own).
- No LangChain. Remove `langchain-text-splitters`, `langchain-mongodb`, `langchain-google-genai`
  from `apps/memory/pyproject.toml` (`grep -rn langchain apps/memory/src apps/memory/tests
  apps/memory/scripts` is empty today) and refresh `uv.lock`.

**3. Contextual header** — `tree/memory/rag/embedding.py`:
`child_embedding_text(*, title: str | None, heading_path: list[str], content: str) -> str`
returns `"{title}\n{' > '.join(heading_path)}\n\n{content}"` with the header lines omitted when
empty (no title & no headings → just `content`), passed through `strip_invalid_chars`. This is
the ONLY text embedded for a **Child chunk**; the stored `properties.content` stays the raw
child text.

**4. Schema** (`tree/entities/memory.py`, `tree/entities/ontology.py`):
- `MemoryEntry` gains `parent_id: str | None = None` and `chunk_index: int | None = None`
  (top-level graph-modeling meta fields per ADR-001 §11).
- `NODE_REGISTRY["chunk"].subtypes = frozenset({"parent", "child"})` (closed) — a
  `MemoryEntry(kind="node", type="chunk", subtype="section")` is rejected by the existing
  subtype validator.
- `ChunkProperties` gains `title: str | None` and `heading_path: list[str] = []`
  (denormalised from document/parent, like the existing `source_type`/`source_uri`/`date`);
  `DocumentProperties` gains `title: str | None`. Every new field carries
  `Field(description=...)` (ADR-001 §13 — `test_field_descriptions.py` enforces it).
- Regenerate `tests/unit/entities/snapshots/ontology_schema.json` ONLY if it changes
  (`chunk`/`document` are not LLM-extractable, so it should not).

## Acceptance Criteria

- [ ] `load_app_config(frozen_config_path).memory.chunking` equals `strategy="recursive", parent=(4096, 0), child=(256, 32)`; `TREE_MEMORY__CHUNKING__CHILD__SIZE=128` overrides `child.size`.
- [ ] `ChunkingConfig(child={"size": 300, "overlap": 400}, ...)` raises (`overlap < size`); `ChunkingConfig(parent={"size": 200}, child={"size": 256})` raises (`child.size < parent.size`); `strategy="semantic"` raises.
- [ ] `split_document("", cfg) == []` and `split_document("   \n", cfg) == []` for both strategies.
- [ ] `fixed_tokens`, `parent.size=100, overlap=0`, a 250-token text → 3 parents whose concatenated contents decode to the original token sequence; with `overlap=20` → more parents than with `overlap=0`; every parent has `heading_path == []`.
- [ ] `recursive` on `"# A\n\npara1\n\n## B\n\npara2\n\n# C\n\npara3"` with `parent.size` large enough → parents whose `heading_path`s are `["A"]`, `["A","B"]`, `["C"]` (in this order) and whose contents each start with their heading line.
- [ ] `recursive` never emits a parent or child longer than `size` tokens (property test over 20 generated texts with mixed headings/paragraphs/long sentences, sizes `{64, 256, 1024}`).
- [ ] Every child's `content` is a substring of its parent's `content` (or, with overlap > 0, of the parent plus the previous sibling's tail); `children[i].index == i`; a parent whose content is < `child.size` tokens has exactly 1 child.
- [ ] `split_document(text, cfg)` called twice returns equal lists (determinism); result is JSON-serialisable via `.model_dump()`.
- [ ] `child_embedding_text(title="Memory for Agents", heading_path=["Retrieval","Parents"], content="body")` == `"Memory for Agents\nRetrieval > Parents\n\nbody"`; with `title=None, heading_path=[]` it returns `"body"`; control chars in `content` are stripped.
- [ ] `MemoryEntry(kind="node", type="chunk", subtype="child", parent_id="u:chunk:x#parent-0", chunk_index=3, ...)` validates; `subtype="section"` raises `ValueError` mentioning `['child', 'parent']`; `ChunkProperties(title=..., heading_path=[...])` and `DocumentProperties(title=...)` validate and every field has a non-empty description.
- [ ] `grep -n langchain apps/memory/pyproject.toml` is empty and `uv lock --check` passes (or `uv.lock` is regenerated and committed).
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` green.

## User Stories

### Story: Developer splits a markdown article and sees the hierarchy
1. Developer runs `split_document(open("article.md").read(), app_config.memory.chunking)` on a 12k-token article with 6 `##` sections.
2. They get ≈3–4 `ParentChunk`s (≤ 4096 tokens each), each with `heading_path` reflecting the section it starts in.
3. Each parent holds ≈10–16 `ChildChunk`s (≤ 256 tokens each) numbered `0..n-1`.

### Story: Book reader switches back to the Chapter-4 fixed-window splitter
1. Reader sets `memory.chunking.strategy: fixed_tokens` in `configs/default.yaml`.
2. `split_document` returns token-window parents/children with empty heading paths — the same algorithm `chunk_document` used before this feature.

### Story: Child embedding text carries document context the raw chunk lacks
1. A child chunk `"It stores parents without vectors."` from a parent under `["Parent-document retrieval"]` in the document titled `"Memory for AI Agents"` is prepared for embedding.
2. `child_embedding_text(...)` yields `"Memory for AI Agents\nParent-document retrieval\n\nIt stores parents without vectors."`.
3. The stored `content` for that child stays `"It stores parents without vectors."`.

### Story: Operator misconfigures child larger than parent
1. Operator sets `TREE_MEMORY__CHUNKING__CHILD__SIZE=8192` (parent 4096) and boots the worker.
2. Config validation fails with a message naming `child.size` and `parent.size`.

## Out of scope

- Wiring the splitter into the pipeline / persisting parent+child rows (#108).
- Semantic (embedding-based) chunking.
- Per-child heading paths (children inherit the parent's).

---

Blocked by: #105

## Log
