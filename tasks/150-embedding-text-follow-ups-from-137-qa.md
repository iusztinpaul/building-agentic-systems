---
id: 150-embedding-text-follow-ups-from-137-qa
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Three small embedding-text follow-ups from #137's QA: sanitise the supersession statement, stop `object` being shadowed by a falsy value, build the persisted-row dict once

Tags: `memory`, `embedding`, `bug`, `refactor`
Depends on: None (independent of #143-#149; runs here only to keep ONE branch, ONE PR in NNN order)
Blocks: #141 (runs before #151 and #152 only by NNN order — neither needs anything from this task)
Implements: ADR-009 — Decision 7 ("the backfill embeds PREFERENCE/FACT on the same text as the inline path": one helper, the same BYTES on every path)

## Scope

**EXECUTION ORDER of the rework: 143 -> 144 -> 145 -> 146 -> 147 -> 148 -> 149 -> 150 -> 151 -> 152 -> 141.**
No Modal code is touched; no process other than the test suite is started.

**This task is ONLY the three #137-QA embedding-text items below.** Commits 22734df (#148) and d3a9fe2 (#149)
and #147's QA notes say their follow-ups are "routed to #150": they were re-homed at the 2026-09-20
re-grooming so each task stays atomic — the LLM cache identity is **#151**, the two pre-warm fixes, the two
deploy-script static guards and the cosmetics are **#152**. Do NOT implement any of them here.

Run test commands one at a time — two concurrent pytest runs contend for the local MongoDB/Prefect and stall.
Do not run the real memory pipelines against the local database (shared with the human's `main` checkout).

Three pre-existing defects the Tester logged during #137's QA. They matter before #141 because #141's re-pin
reads dedup / supersession similarities: a statement embedded on different bytes inline vs in the backfill
moves exactly those numbers.

**1. `preference_supersession.py:417` embeds an UNSANITISED statement.**
`embedded = await embedding_model.embed([new_statement], input_type="document")` sends the raw statement, while
every other path embeds `strip_invalid_chars(statement).strip()` through
`tree.memory.embedding_text.entity_embedding_text` (Voyage answers 400 on control characters and lone
surrogates; and the vector written on the superseding row must be embedded on the SAME bytes the backfill would
use, or a reset changes it). Fix: embed the sanitised text — the one-line
`strip_invalid_chars(new_statement).strip()`, imported from `tree.memory.rag.cleaning` exactly as
`embedding_text.py` does. If it sanitises to blank, skip the embed and take the EXISTING "write the supersession
without embedding column" branch (never embed a blank string). The persisted `properties.statement` stays raw.

**2. FACT `object or object_` truthy-shadow** — `embedding_text.py:261`:
`special = properties.get("object") or properties.get("object_")`. A PRESENT but falsy `object` (`""`, and any
non-string falsy value) silently falls through to the legacy `object_` of the same row — a row that carries
both embeds on the STALE pre-rename text. Fix: `object` wins whenever the key is present and not `None`;
`object_` is read only when `object` is absent/`None`. A blank winner then falls back to
`node_to_embedding_text` through the existing blank check — never to `object_`. Apply the same presence rule
to the two tuple loops in `preference_supersession.py` (~240 and ~364, `for key in ("object", "object_")` /
`("statement", "content", "object", "object_")`) ONLY if reading them shows the same shadowing; if they
already stop at the first PRESENT key, leave them and say so in `## Log`.

**3. The persisted-row dict is built twice.** `pipeline.py::_entity_embeddable_text` (~292-322) and
`graph/add_entity.py::_embeddable_text` (~332) each assemble `{"type", "name", "canonical_name", "properties"
minus aliases/confidence}` by hand before calling `entity_embedding_text`; their docstrings promise
"byte-for-byte" agreement that nothing enforces. Fix: ONE function in `tree/memory/embedding_text.py` —
`prospective_entity_embedding_text(*, entity_type: NodeType, name: str, canonical_name: str, properties:
dict[str, Any]) -> str` — builds the dict and calls `entity_embedding_text`; both call sites call it and both
private helpers are DELETED (update their tests to the new name; do not keep thin wrappers). Check
`tests/unit/memory/test_package_layout.py`: `embedding_text.py` is shared by `rag/` and `graph/` already, so no
new import edge is created.

## Out of scope
- Any other change to what is embedded (roles, chunk text, node-text format). Re-embedding existing rows: the
  human's post-merge Embedding reset (#141's [HUMAN] line) picks the fixes up.
- A data migration for rows carrying both `object` and `object_`.
- The LLM cache identity (#151); the pre-warm fixes, the deploy-script static guards and the `MODAL_SERVER_NAME`
  rename (#152).

## Acceptance Criteria

- [ ] `test_supersession_embeds_the_sanitised_statement`: a statement containing `\x00` and a lone surrogate -> the fake embedding model receives `strip_invalid_chars(statement).strip()`, with `input_type="document"`; the persisted `properties.statement` is still the raw string.
- [ ] `test_supersession_skips_the_embed_for_a_blank_statement`: a statement of only invalid characters / whitespace -> `embed` is NOT called, the supersession is written with `embedding == []`, and the existing WARNING path is not needed (no exception).
- [ ] `test_supersession_text_equals_backfill_text`: for the same PREFERENCE row, the text embedded by the supersession path == `entity_embedding_text(row)`.
- [ ] `TestFactObjectPrecedence` (`entity_embedding_text`): `{"object": "new", "object_": "old"}` -> `"new"`; `{"object_": "old"}` -> `"old"`; `{"object": None, "object_": "old"}` -> `"old"`; `{"object": "", "object_": "old"}` -> the `node_to_embedding_text` fallback, NOT `"old"`; `{"object": "  ", "object_": "old"}` -> the fallback.
- [ ] `test_prospective_entity_text_is_one_function`: `grep -rn "def _entity_embeddable_text\|def _embeddable_text" apps/memory/src` -> 0 hits; `pipeline.py` and `add_entity.py` both import `prospective_entity_embedding_text`; for a GENERIC, a PREFERENCE and a FACT entity (with `aliases` / `confidence` in `properties`) its output == `entity_embedding_text` on the hand-built persisted row.
- [ ] Every pre-existing test of `embedding_text`, `add_entity`, `preference_supersession` and the pipeline's embed-entities stage stays green (renamed where the helper was renamed).
- [ ] `## Log` states the verdict on the two tuple loops of item 2 with the line numbers read.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

## User Stories

### Story: A preference whose statement came from a PDF with a stray control character
1. The user's new preference supersedes an old one; the extracted statement carries `\x0c`.
2. Before: the embed call could answer HTTP 400 and the superseding row was written without a vector. After: the row is embedded on the cleaned text and is searchable at once.

### Story: Operator runs an Embedding reset after a model change
1. Reset -> indexing re-embeds every PREFERENCE row from `properties.statement` via `entity_embedding_text`.
2. The re-embedded vector of a superseding row is identical (same bytes in) to the one the supersession path wrote inline — supersession comparisons do not drift after a reset.

### Story: A FACT row written across the `object_` -> `object` rename
1. The row holds `object: "lives in Berlin"` and a stale `object_: "lives in Paris"`.
2. It embeds on `lives in Berlin`. With `object: ""` it embeds on its node-text — never on the stale `Paris`.

### Story: The next engineer changes which properties are excluded from the embedded text
1. They edit ONE function, `prospective_entity_embedding_text`.
2. The inline path (`add_entity`) and the pipeline's pre-computed vectors change together; the "byte-for-byte" promise is now structural.

---

Blocked by: (none)

## Log

### [PA] 2026-09-20 13:40 — Grooming (new task: D7's three pre-existing follow-ups, as ONE small task)

**Summary**
Three embedding-text defects logged by #137's QA: an unsanitised statement in supersession, a truthy-`or` that lets a falsy `object` be shadowed by legacy `object_`, and a persisted-row dict duplicated in two modules.

**Key decisions**
- One task, not folded into the Modal tasks: none of the three touches Modal code, and folding them would blur those diffs.
- Placed before #141 so the re-pin measures the fixed text; independent of #143-#149 (may run in parallel if the orchestrator wants).
- The shared helper lives in `embedding_text.py`, which both layers already import — no new `rag/` <-> `graph/` edge.

**Dependencies**
- None.

**User stories**
- 4 stories: control character in a statement, reset stability, the renamed FACT property, one place to change.

Ready for implementation.

### [PA] 2026-09-20 19:30 — Re-grooming (the "routed to #150" follow-ups are re-homed to #151 / #152)

**What changed and why**
- Three sources pointed their follow-ups here: commit 22734df (#148 — the LLM cache-identity gap), commit d3a9fe2 (#149 — the two pre-warm findings) and #147's Tester log (the two deploy-script static-guard gaps). This body never carried them, so the tracker did not hold the work.
- Embedding text, Prefect cache keys, the Pre-warm and the deploy-script guards are four unrelated concerns; one commit for all of them is not atomic. This task therefore stays EXACTLY its three #137-QA items (scope, AC and stories untouched — the file is not renamed, its slug is still accurate) and the rest moved: **#151** = the LLM identity in the two LLM tasks' `INPUTS` cache key; **#152** = the two pre-warm fixes + the two static guards + the cosmetics.
- Added: the new execution order (… 150 -> 151 -> 152 -> 141), the "do not implement them here" pointer, the two run-hygiene lines, one Out-of-scope bullet.

**Dependencies**
- None. #151 and #152 do not depend on this task; the order is NNN order only.

Ready for implementation.
