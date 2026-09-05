---
id: 107-two-level-chunking-strategies
feature: rag-graphrag-modes
status: done
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

- [x] `load_app_config(frozen_config_path).memory.chunking` equals `strategy="recursive", parent=(4096, 0), child=(256, 32)`; `TREE_MEMORY__CHUNKING__CHILD__SIZE=128` overrides `child.size`.
- [x] `ChunkingConfig(child={"size": 300, "overlap": 400}, ...)` raises (`overlap < size`); `ChunkingConfig(parent={"size": 200}, child={"size": 256})` raises (`child.size < parent.size`); `strategy="semantic"` raises.
- [x] `split_document("", cfg) == []` and `split_document("   \n", cfg) == []` for both strategies.
- [x] `fixed_tokens`, `parent.size=100, overlap=0`, a 250-token text → 3 parents whose concatenated contents decode to the original token sequence; with `overlap=20` → more parents than with `overlap=0`; every parent has `heading_path == []`.
- [x] `recursive` on `"# A\n\npara1\n\n## B\n\npara2\n\n# C\n\npara3"` with `parent.size` large enough → parents whose `heading_path`s are `["A"]`, `["A","B"]`, `["C"]` (in this order) and whose contents each start with their heading line.
- [x] `recursive` never emits a parent or child longer than `size` tokens (property test over 20 generated texts with mixed headings/paragraphs/long sentences, sizes `{64, 256, 1024}`).
- [x] Every child's `content` is a substring of its parent's `content` (or, with overlap > 0, of the parent plus the previous sibling's tail); `children[i].index == i`; a parent whose content is < `child.size` tokens has exactly 1 child.
- [x] `split_document(text, cfg)` called twice returns equal lists (determinism); result is JSON-serialisable via `.model_dump()`.
- [x] `child_embedding_text(title="Memory for Agents", heading_path=["Retrieval","Parents"], content="body")` == `"Memory for Agents\nRetrieval > Parents\n\nbody"`; with `title=None, heading_path=[]` it returns `"body"`; control chars in `content` are stripped.
- [x] `MemoryEntry(kind="node", type="chunk", subtype="child", parent_id="u:chunk:x#parent-0", chunk_index=3, ...)` validates; `subtype="section"` raises `ValueError` mentioning `['child', 'parent']`; `ChunkProperties(title=..., heading_path=[...])` and `DocumentProperties(title=...)` validate and every field has a non-empty description.
- [x] `grep -n langchain apps/memory/pyproject.toml` is empty and `uv lock --check` passes (or `uv.lock` is regenerated and committed).
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green.

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

### [SWE] 2026-09-05 17:20 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — `ChunkLevelConfig` (`size > 0`, `overlap >= 0`, validator `overlap < size`) + `ChunkingConfig` (`strategy: fixed_tokens|recursive`, validator `child.size < parent.size`); `MemoryConfig.chunking`.
- `apps/memory/configs/default.yaml`, `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — the `memory.chunking` block (recursive, 4096/0, 256/32). `extraction.chunk_size/chunk_overlap` left in place for #108.
- `apps/memory/src/tree/memory/rag/types.py` (new) — `ChildChunk`, `ParentChunk` (Pydantic, every field described).
- `apps/memory/src/tree/memory/rag/chunking.py` (new) — `split_document`; module-level `cl100k_base` encoder; `fixed_tokens` = today's `chunk_document` window; `recursive` = ATX headings (heading-stack, code-fence aware) → blank-line paragraphs → sentences → raw tokens, greedy merge, overlap inside the size budget.
- `apps/memory/src/tree/memory/rag/embedding.py` (new) — `child_embedding_text(*, title, heading_path, content)` through `strip_invalid_chars`.
- `apps/memory/src/tree/entities/memory.py` — `MemoryEntry.parent_id`, `MemoryEntry.chunk_index`.
- `apps/memory/src/tree/entities/ontology.py` — `chunk` subtypes closed to `{parent, child}`; `ChunkProperties.title/heading_path`; `DocumentProperties.title`.
- `apps/memory/pyproject.toml` + `uv.lock` — `langchain-text-splitters`, `langchain-mongodb`, `langchain-google-genai` removed (287 lock lines, 20 transitive packages gone).
- Tests: `tests/unit/config/test_app_config.py::TestChunkingConfig`, `tests/unit/entities/test_memory.py::TestChunkHierarchyFields`, `tests/unit/entities/test_ontology.py::TestChunkAndDocumentContextProperties`, `tests/unit/memory/rag/test_chunking.py`, `test_embedding.py`, `test_types.py` (new).

**Tests**
- Unit: 2189 passing, 0 failing, 0 warnings (`make memory-tests`, env local) — 151 of them new.
- Integration: N/A — this repo has no integration suite (deliberate); e2e is the real-pipeline run below.

**Acceptance criteria**
- [x] frozen/default YAML + env hatch — `test_app_config.py::TestChunkingConfig::{test_chunking_block_loaded_from_frozen_config,test_chunking_block_loaded_from_default_yaml,test_child_size_env_override}`
- [x] validators (`overlap < size`, `child.size < parent.size`, unknown strategy) — `TestChunkingConfig::{test_overlap_at_or_above_size_raises,test_child_size_at_or_above_parent_size_raises,test_child_size_env_override_above_parent_fails_the_load,test_unknown_strategy_raises_naming_both_allowed_values}`
- [x] blank text → `[]` for both strategies — `test_chunking.py::TestEmptyInput::test_blank_text_yields_no_parents`
- [x] fixed_tokens 250 tokens/size 100 → 3 parents, token-exact concat, more parents with overlap, empty heading paths — `TestFixedTokensStrategy` (5 tests)
- [x] recursive heading stack `["A"] / ["A","B"] / ["C"]` + contents start with their heading line — `TestRecursiveHeadingPaths::{test_heading_stack_is_tracked_across_levels,test_each_parent_starts_with_its_heading_line}`
- [x] no chunk exceeds `size` (20 generated texts × sizes 64/256/1024, overlap on) — `TestChunkBoundsProperty::test_no_chunk_exceeds_its_configured_size`
- [x] child ⊂ parent, dense indexes, single child under `child.size` — `TestParentChildHierarchy` (6 tests, both strategies)
- [x] determinism + `model_dump` JSON — `TestDeterminismAndSerialisation` (5 tests)
- [x] contextual header exact strings + control-char stripping — `test_embedding.py::TestChildEmbeddingText` (11 tests)
- [x] `parent_id`/`chunk_index`/closed chunk subtypes/property models + descriptions — `test_memory.py::TestChunkHierarchyFields` (6), `test_ontology.py::TestChunkAndDocumentContextProperties` (7), plus the existing `test_field_descriptions.py` sweep
- [x] no langchain in the manifest, lock in sync — `grep -c langchain apps/memory/pyproject.toml` → 0, `uv lock --check` → OK
- [x] format/lint/tests green (evidence below)

**Evidence**
```
$ make memory-tests
============================ 2189 passed in 18.75s =============================
$ make memory-format-check && make memory-lint-check
258 files already formatted
All checks passed!
$ make pre-commit
prettier / ruff check / ruff format / biome check .......................Passed
$ cd apps/memory && uv lock --check && grep -c langchain pyproject.toml
Resolved 242 packages in 16ms
0
```

End-to-end (User Stories 1-3) — the splitter run over a real 4.6k-token markdown
document with the SHIPPED config (`app_config.memory.chunking`):

```
$ uv run python  # split_document(clean_text(tutorials/2_4_2_deploying_to_the_cloud.md), app_config.memory.chunking)
config: recursive parent=4096/0 child=256/32
document: 4607 tokens
parent 0:  2386 tok  children= 15 max_child=255  heading_path=['Deploying to the Cloud']
parent 1:  2220 tok  children= 13 max_child=251  heading_path=['Deploying to the Cloud', 'Prefect Cloud: The Pipelines, Hosted']
embedding text: 'Deploying to the Cloud\nDeploying to the Cloud > Prefect Cloud: The Pipelines, Hosted\n\n## Prefect Cloud: ...'
stored content: '## Prefect Cloud: The Pipelines, Hosted\n\nTo create the Prefe'
deterministic: True
fixed_tokens: 2 parents, paths=[[], []], sizes=[4096, 511]
```

**Notes**
- The e2e run found a real defect the unit tests had not: `# comment` lines inside fenced code blocks were parsed as ATX headings, producing the heading path `['→ ensure MONGO_SCHEME=mongodb+srv']`. Fixed at the root (`_fenced_ranges` masks fenced regions before heading detection, CommonMark's unclosed-fence rule included) with 4 regression tests in `TestFencedCodeBlocks`. Paragraph/sentence splitting stays fence-blind on purpose — a blank line inside a fence only shifts a boundary; a fake heading corrupts every child's contextual header.
- Recursive chunks are contiguous character SPANS of the source, merged by offsets and trimmed once at emission. That is what makes "every child is a substring of its parent" hold even with overlap on, and it is why the size check measures the TRIMMED text (a leading space changes tokenisation: `" retrieval"` = 1 token, `"retrieval"` = 2 — measuring untrimmed leaked 66-token children into a 64-token budget during development).
- Overlap counts against the size budget (LangChain's rule): if the carried tail would push a chunk over `size`, the chunk starts without overlap rather than exceeding the bound. The `<= size` guarantee is what the embedding batcher relies on.
- Deliberate trade-offs: the greedy merge re-encodes the candidate merged text on every step (O(pieces × size) tokens per level) — measurably fine at 4096 tokens (the 4.6k-token document above splits in well under a second); if documents grow past ~100k tokens, cache per-piece counts and bound the re-encode to the boundary. `_token_char_offsets` builds a byte→char map per call; same ceiling.
- `tiktoken.encode(..., disallowed_special=())` is a deliberate deviation from `chunk_document`, which raises `ValueError` on a document containing the literal `<|endoftext|>` — scraped text does. It only differs on inputs where the old code crashed.
- Observation for #108 (NOT changed here — the format is pinned by ADR-006 §4, the glossary and this task's AC): when a document's `title` equals its H1, the contextual header repeats it (`"Deploying to the Cloud\nDeploying to the Cloud > ..."`). If PA wants dedup, it is a one-line change in `child_embedding_text` plus a glossary note.
- `tests/unit/entities/snapshots/ontology_schema.json` is unchanged, as predicted: `chunk`/`document` are not LLM-extractable, so neither the closed subtype set nor the new properties reach the prompt schema.
- NOT RUN: no pipeline/e2e Prefect flow — nothing is wired to the splitter yet (#108 owns that), so `run-pipelines-e2e` has no changed path to exercise.

### [Tester] 2026-09-05 18:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit`)
- Unit tests: 2189 passed / 0 failed (`make memory-tests`, env local)
- Integration tests: N/A — no integration suite in this repo (deliberate)
- Warnings: 0

**E2E adversarial pass**
- Happy path: `split_document(_HEADED_TEXT, cfg)` recursive, `child_embedding_text(...)` exact-string case, MemoryEntry chunk validation — all match spec verbatim (PASS)
- Break path 1 (boundary: single 29k-token paragraph, no sentence punctuation, `parent.size=500/child.size=100`): falls through to raw token level, 58 parents, max parent 500 tok / max child 100 tok — never exceeds budget (PASS)
- Break path 2 (state edge: heading depth-skip, `# A` then `### C` with `## ` missing, `parent.size=6`): `heading_path` for the `### C` section is `["A", "C"]` — stack correctly pops only levels `>= 3` and nests under the last shallower heading, no crash (PASS)
- Break path 3 (malformed structure: adjacent headings `# A\n# B\n\npara`, `parent.size=3`): three pieces `["A"], ["B"], ["B"]` — no crash, second heading correctly supersedes the first in the stack (PASS)
- Break path 4 (state edge: near-max overlap `overlap=size-1` at both fixed_tokens and recursive, and huge overlap at both parent+child levels simultaneously `overlap=49/size=50` and `overlap=9/size=10`): all four combinations terminate in well under a second (0.001s–0.03s), no infinite loop (PASS)
- Break path 5 (malformed input: CRLF straight into the splitter, no upstream cleaning; and an unclosed markdown code fence containing `# not a heading` lines): both produce valid parents with no crash; the unclosed-fence content is correctly excluded from heading detection to end-of-document per the code's CommonMark rule (PASS)
- Break path 6 (boundary: `child.size == parent.size - 1` fixed_tokens; parent content exactly `child.size` tokens): no crash, single child equals parent content exactly as specified (PASS)
- Break path 7 (hostile/degenerate boundary — code smell found, not a regression): `fixed_tokens` with a tiny `size` cutting through a multi-token emoji (`"🧠"` = 3 cl100k tokens) decodes a raw token slice mid-character, emitting U+FFFD replacement characters into chunk content. Reproduced with `chunk_document`'s original algorithm in `tree/memory/extraction/core.py:76-88` — this is faithfully-ported PRE-EXISTING behavior (the AC explicitly requires porting "today's algorithm from chunk_document" byte-for-byte), not a regression introduced by this task. The `recursive` strategy does NOT have this problem — `_token_char_offsets` snaps token boundaries to valid character boundaries, so the same emoji-heavy input under `recursive` correctly falls back to emitting the whole unsplittable emoji as its own chunk rather than corrupting it. Logged under "Other issues found" below, not a FAIL.

**Acceptance criteria** (all independently re-run via `uv run python` one-liners against the worktree, not just read from the SWE's test files)
- [x] PASS — frozen config + env override — `load_app_config(frozen_config_path).memory.chunking` == `strategy="recursive", parent=(4096,0), child=(256,32)`; `TREE_MEMORY__CHUNKING__CHILD__SIZE=128` → `child.size == 128`. Evidence: manual run, both assertions held.
- [x] PASS — validators — `overlap>=size`, `child.size>=parent.size`, `strategy="semantic"` each raise `pydantic.ValidationError`; the child/parent message reads `"...child.size must be smaller than memory.chunking.parent.size. Found child.size=256, parent.size=200."`. Also reproduced the Operator user-story end to end: `TREE_MEMORY__CHUNKING__CHILD__SIZE=8192 uv run python -c "from tree.config.app_config import app_config"` fails at import with `memory.chunking\n  Value error, Misconfigured chunking: ... Found child.size=8192, parent.size=4096.`
- [x] PASS — blank input — `split_document("", cfg) == []` and `split_document("   \n", cfg) == []` for both `fixed_tokens` and `recursive`.
- [x] PASS — fixed_tokens 250-token/size-100 case — built an exact 250-cl100k-token text; overlap=0 gives 3 parents sized `[100,100,50]`, concatenation re-encodes identical to the original token sequence, all `heading_path == []`; overlap=20 gives 4 parents (`>` 3).
- [x] PASS — recursive heading-path case on the literal AC string, `parent.size` large enough to hold one section but not merge two (per the SWE's own `test_heading_stack_is_tracked_across_levels`, `size=8`): `heading_path`s == `[["A"], ["A","B"], ["C"]]` in order, each `content` starts with its heading line. Note: at `parent.size=4096` (also "large enough" under a naive reading) the greedy merge collapses the whole text into ONE parent (`heading_path=["A"]`) — this is documented, separately-tested behavior (`test_sections_merge_greedily_and_keep_the_first_path`), and the SWE's narrower reading of "large enough" (fits one section, not two) is the only self-consistent one given the spec's own greedy-merge rule. Not a defect; flagging only because the AC wording is ambiguous — see "Other issues found."
- [x] PASS — property test — ran `TestChunkBoundsProperty` directly (80 passed, 20 seeds × {sizes×levels}); additionally ran my own 150-case fuzz (50 seeds × 3 sizes, headings/paragraphs/sentences mixed) with independent tiktoken re-encoding of every emitted parent/child — 0 size violations.
- [x] PASS — child⊂parent / dense indices / single-child-under-budget — parent with content < `child.size` tokens has exactly 1 child equal to its content; `children[i].index == i` verified across both strategies on a 1000-word document; every child's content verified as an exact substring of its parent's content with overlap on, both strategies.
- [x] PASS — determinism + serialisation — ran `split_document` in two SEPARATE `uv run python` processes (not just two calls in one process) on the same recursive-with-overlap config and diffed the JSON `model_dump()` output byte-for-byte — identical.
- [x] PASS — contextual header — `child_embedding_text(title="Memory for Agents", heading_path=["Retrieval","Parents"], content="body")` == `"Memory for Agents\nRetrieval > Parents\n\nbody"` exactly; `title=None, heading_path=[]` → `"body"` exactly; control chars (`\x00`, `\x07`) stripped from output.
- [x] PASS — schema — `MemoryEntry(kind="node", type="chunk", subtype="child", parent_id=..., chunk_index=3, ...)` validates; `subtype="section"` raises `ValueError` containing `"['child', 'parent']"`; `ChunkProperties(title=..., heading_path=[...])` and `DocumentProperties(title=...)` validate; every field on both models has a non-empty description (checked programmatically, not just read).
- [x] PASS — no langchain — `grep -rn langchain apps/memory/src apps/memory/tests apps/memory/scripts apps/memory/deploy apps/memory/pyproject.toml apps/memory/uv.lock` → 0 matches; `cd apps/memory && uv lock --check` → `Resolved 242 packages` (no diff needed).
- [x] PASS — format/lint/tests — reran independently: `make memory-format-check && make memory-lint-check` → "258 files already formatted" / "All checks passed!"; `make pre-commit` → all hooks passed; `make memory-tests` → 2189 passed, 0 warnings.

**Evidence**
```
$ make memory-tests
============================ 2189 passed in 16.93s =============================
$ grep -rn langchain apps/memory/src apps/memory/tests apps/memory/scripts apps/memory/deploy apps/memory/pyproject.toml apps/memory/uv.lock
(no output, exit 1)
$ cd apps/memory && uv lock --check
Resolved 242 packages in 2ms
$ TREE_MEMORY__CHUNKING__CHILD__SIZE=8192 uv run python -c "from tree.config.app_config import app_config"
pydantic_core._pydantic_core.ValidationError: 1 validation error for AppConfig
memory.chunking
  Value error, Misconfigured chunking: memory.chunking.child.size must be smaller than
  memory.chunking.parent.size. Found child.size=8192, parent.size=4096.
```

**Other issues found**
- (pre-existing, not a regression) `fixed_tokens`/`_fixed_window` in `apps/memory/src/tree/memory/rag/chunking.py` decodes raw `tiktoken` slices at a fixed token index, which can cut through a multi-token Unicode character (e.g. a 3-token emoji) and emit `U+FFFD` replacement characters into a chunk's content. Identical to the legacy `chunk_document` in `tree/memory/extraction/core.py:76-88` that the AC requires porting verbatim, so out of scope to fix here — worth a follow-up task since `recursive` (this task's default) does not have the problem (`_token_char_offsets` snaps to character boundaries).
- AC5's "`parent.size` large enough" wording is ambiguous between "large enough to hold one section" (what the SWE tested and what actually produces 3 distinct parents) and "large enough to hold everything" (which merges to 1 parent, per the greedy-merge rule this same task defines). Recommend tightening the AC wording for future reference; no code change needed — the SWE's interpretation is the only one consistent with the documented merge behavior.
- Minor: `title == H1` duplicates the title in the contextual header (SWE flagged this themselves for #108) — out of scope here per ADR-006 §4 pinning the format; noted for completeness only.

**VERDICT: PASS**

### [PA] 2026-09-05 20:32 — Acceptance Review

**VERDICT: REJECT** (feature-level verdict for PR #41, `rag-graphrag-modes`)

Two rollup items touch this task: Issue 2 — the story "Book reader switches back to the Chapter-4 fixed-window splitter" is silently broken because `clean-and-chunk`'s `INPUTS` cache key does not include the chunking config (the fix lands in `pipeline.py`, #108's file, but it is this task's story that fails); Issue 8 — `fixed_tokens` decodes raw token slices and emits U+FFFD on multi-token characters (pre-existing, but re-shipped and re-documented here; `_token_char_offsets` already exists to fix it). The `recursive` strategy, the config validators and their error messages, and the Contextual header are right.

Filed ONE rollup task for the whole feature: `tasks/112-pa-rejection-rag-graphrag-modes.md` (8 issues). Pipeline re-runs from the inner loop with the rollup task; on green, re-run acceptance on this task.

### [PA] 2026-09-05 23:20 — Acceptance Review (round 2)

**VERDICT: ACCEPT** (feature-level verdict for PR #41, `rag-graphrag-modes`, HEAD `08cd632`)

Rollup Issues 2 and 8 landed: the "switch back to the Chapter-4 fixed-window splitter" story now works (chunking config is in task ①'s cache key — live proof `Completed` -> flip -> `Completed` -> repeat `Cached`); `fixed_tokens` slices the source string via `_token_char_offsets` (compounding-offset bug fixed), never emits U+FFFD, and no chunk exceeds its `size` except a single indivisible character. ASCII output byte-identical to the Chapter-4 window. `recursive`, validators and the **Contextual header** unchanged. Non-blocking follow-up recorded in 112: the carve-out is code-point, not grapheme-cluster, based.

Rollup `tasks/done/112-pa-rejection-rag-graphrag-modes.md` implemented and Tester-PASSED (round 2). Hand off to the PR Reviewer.
