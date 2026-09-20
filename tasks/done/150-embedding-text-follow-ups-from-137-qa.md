---
id: 150-embedding-text-follow-ups-from-137-qa
status: done
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

- [x] `test_supersession_embeds_the_sanitised_statement`: a statement containing `\x00` and a lone surrogate -> the fake embedding model receives `strip_invalid_chars(statement).strip()`, with `input_type="document"`; the persisted `properties.statement` is still the raw string.
- [x] `test_supersession_skips_the_embed_for_a_blank_statement`: a statement of only invalid characters / whitespace -> `embed` is NOT called, the supersession is written with `embedding == []`, and the existing WARNING path is not needed (no exception).
- [x] `test_supersession_text_equals_backfill_text`: for the same PREFERENCE row, the text embedded by the supersession path == `entity_embedding_text(row)`.
- [x] `TestFactObjectPrecedence` (`entity_embedding_text`): `{"object": "new", "object_": "old"}` -> `"new"`; `{"object_": "old"}` -> `"old"`; `{"object": None, "object_": "old"}` -> `"old"`; `{"object": "", "object_": "old"}` -> the `node_to_embedding_text` fallback, NOT `"old"`; `{"object": "  ", "object_": "old"}` -> the fallback.
- [x] `test_prospective_entity_text_is_one_function`: `grep -rn "def _entity_embeddable_text\|def _embeddable_text" apps/memory/src` -> 0 hits; `pipeline.py` and `add_entity.py` both import `prospective_entity_embedding_text`; for a GENERIC, a PREFERENCE and a FACT entity (with `aliases` / `confidence` in `properties`) its output == `entity_embedding_text` on the hand-built persisted row.
- [x] Every pre-existing test of `embedding_text`, `add_entity`, `preference_supersession` and the pipeline's embed-entities stage stays green (renamed where the helper was renamed).
- [x] `## Log` states the verdict on the two tuple loops of item 2 with the line numbers read.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

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

### [SWE] 2026-09-20 21:35 — Implementation

**Environment / safety statement**
`make env-status` -> `local (.env)` throughout. No pipeline run, no `memory-reset-embeddings`, no `modal`
command, no `.env` / `.env.prod` read, no write to any tenant (unit suite only, on its own fixtures). Test
commands run ONE at a time. No commit (Tester goes first); still on `feat/voyage-4-and-modal-embedding-catalog`.

**Files modified**
- `apps/memory/src/tree/memory/embedding_text.py` — item 2: FACT text now reads `object` on PRESENCE
  (`properties["object"] if properties.get("object") is not None else properties.get("object_")`), so a
  present-but-blank/non-string `object` falls back to `node_to_embedding_text` and NEVER to the stale legacy
  `object_`. Item 3: new `prospective_entity_embedding_text(*, entity_type, name, canonical_name, properties)`
  — the ONE builder of the persisted-row shape (`aliases` / `confidence` stripped via the named
  `_TOP_LEVEL_ONLY_PROPERTIES`) that then calls `entity_embedding_text`. Module docstring updated (it named the
  two now-deleted private helpers).
- `apps/memory/src/tree/memory/graph/add_entity.py` — `_embeddable_text` DELETED (with its section header);
  the dedup/persist call site and the import now use `prospective_entity_embedding_text`.
- `apps/memory/src/tree/memory/pipeline.py` — `_entity_embeddable_text` DELETED; task ④'s
  `embeddable_text_by_key[key] = prospective_entity_embedding_text(...)`; import swapped (the old
  `entity_embedding_text` import existed only for the deleted helper); the task-⑥ comment at ~1566 that still
  named `_embeddable_text` updated.
- `apps/memory/src/tree/memory/graph/preference_supersession.py` — item 1: `_maybe_supersede` embeds
  `strip_invalid_chars(new_statement).strip()` (sanitize-THEN-strip, the same order and the same shared
  sanitizer `entity_embedding_text` uses; `graph/` -> `rag/` is the allowed import direction). A statement that
  sanitizes to blank skips the embed entirely — the guard sits BEFORE the `try`, so `embed` is never called and
  no exception is needed — and the supersession is written without an embedding column. `new_statement` stays
  RAW for the judge prompt, the node slug and the persisted `properties.statement`. Item 2 root-cause sweep:
  `_fact_object` now applies the same presence rule (see verdict below).
- `apps/memory/tests/unit/memory/test_embedding_text.py` — `TestFactObjectPrecedence` (6 cases incl. 4
  parameterized blank shapes + a non-string `object`) and `TestProspectiveEntityEmbeddingText`
  (`test_prospective_entity_text_is_one_function` — an AST scan of ALL of `src/tree` for a `def
  _embeddable_text` / `def _entity_embeddable_text` plus an IDENTITY check that both modules' symbol *is* the
  shared function — and a 3-way parity case over GENERIC / PREFERENCE / FACT).
- `apps/memory/tests/unit/memory/graph/test_preference_supersession.py` — `_FakeEmbedding` now records the
  texts it is asked to embed; `TestSupersessionEmbedsSanitisedText` (3 tests: sanitised input + raw persisted
  statement + `input_type="document"`; blank statement -> no embed call, row still written;
  supersession-text == `entity_embedding_text(the row it just persisted)`) and `TestFactObjectPresenceRule`
  (2 tests: blank `object` does not fall through to the stale `object_`; a legacy-only `object_` row still
  supersedes and embeds `"Bucharest"`).
- `apps/memory/tests/unit/memory/test_pipeline.py`, `apps/memory/tests/unit/memory/graph/test_add_entity.py` —
  renamed to the shared function (no thin wrappers kept). The two "all three paths" parity tests are now
  two-path (`prospective_entity_embedding_text` vs `rag.indexing.node_embedding_text`) because the two inline
  writers are literally one function; `test_preference_text_is_sanitized_identically_on_all_three_paths` is
  therefore `..._on_both_paths`, pointing at the identity assertion that replaces the third leg.

**Verdict on item 2's two tuple loops (read at HEAD before the change)**
- `preference_supersession.py:240` `_fact_object`, `for key in ("object", "object_")` — **same shadowing,
  FIXED.** It stopped at the first NON-BLANK value, not the first PRESENT key, so `{"object": "", "object_":
  "Bucharest"}` returned `"Bucharest"`. That string is not cosmetic: it becomes `new_statement`, i.e. the item-1
  embed INPUT, while `entity_embedding_text` on the same row now returns the generic node-text — exactly the
  inline-vs-backfill divergence this task exists to remove. With the presence rule the row has no usable object
  and the resolver skips it (`if sp is None or not statement: continue`), so nothing is judged or embedded on
  stale text. Pinned by `TestFactObjectPresenceRule` (both directions, incl. the legacy-only row still working).
- `preference_supersession.py:364` `_candidate_statement`, `for key in ("statement", "content", "object",
  "object_")` — **left unchanged, deliberately.** It is a heterogeneous cross-type chain (preference statement
  -> legacy preference `content` -> fact object -> legacy fact object) whose result only ever becomes the OLD
  statement in the judge PROMPT; it is never an embed input, so it cannot cause vector drift. "First present
  key wins" across that chain would also drop the documented legacy `content` fallback for pre-typed-slot
  preference rows (a blank `statement` would stop the chain dead). The reasoning is now in its docstring.

**Deviations from the AC wording (flagged, not silent)**
- AC2 says the supersession is written "with `embedding == []`". `_write_supersession` has
  `if new_vector: set_payload["embedding"] = new_vector` — with no vector the row is written with NO
  `embedding` key at all, and I did not change that. **The decisive point: this row shape is PRE-EXISTING and
  unchanged by this task.** The old `except Exception` branch already set `new_vector = []` and hit the same
  `if new_vector:` guard, so a Voyage 400 has always produced exactly this row (diff the branch: same outcome,
  now reached without the API round-trip). Writing a literal `[]` instead would be a NEW behaviour in a
  function this task has no reason to touch. Secondary support that the shape is harmless: `_backfill_filter`
  (`rag/indexing.py:145`) selects on `"embedding": {"$in": [[], None]}`, and a Mongo `$in` containing `null`
  matches a MISSING field too — so the row is backfill-eligible either way (asserted from the Mongo semantics,
  not exercised: the suite's `_FakeCollection._matches` skips operators it does not implement). The test
  asserts the observable form: `embed` never called + `not new_row.get("embedding")`.
- One deliberate (and required) divergence between the two paths, for the blank case only: a statement that
  sanitizes to blank is NOT embedded inline (AC2: never embed a blank string), whereas the backfill embeds that
  row's generic node-text (`"preference: p\nstatement:  "` — non-blank). That is the intended outcome: the row
  carries no inline vector and the backfill gives it the node-text one. Verified in the probe output below.

**Tests**
- Unit: 3806 passing, 0 failing, 0 warnings — `make memory-tests`. Baseline NOT measured: my first suite run
  was already post-change and `git stash` / `restore` are disallowed here, so I have no observed pre-task
  number. 17 tests were added (8 + 4 + 3 + 2 across the four new classes), so ~3789 is what a run at `bdd47e0`
  should show — stated as arithmetic, not as something I saw.
- Integration: N/A by design (AGENTS.md — neither app has an integration suite).
- Non-vacuity, proven by flipping the fix line and re-running the suite (then restoring; no
  `git checkout`/`restore`/`stash` used anywhere):
  - `embedding_text.py` FACT branch reverted to `properties.get("object") or properties.get("object_")` -> 2
    failed: `TestFactObjectPrecedence::test_a_blank_object_falls_back_to_node_text_not_to_the_legacy_one[empty]`
    and `::test_a_non_string_object_falls_back_to_node_text`. (The `whitespace` / `control-char` /
    `all-invalid` params stay green under the old code — `"  "` is truthy — so they pin the blank-check
    fallback, not the precedence fix; the `empty` param is the discriminator.)
  - `embeddable_statement = strip_invalid_chars(new_statement).strip()` -> `= new_statement` -> 3 failed:
    `TestSupersessionEmbedsSanitisedText::test_supersession_embeds_the_sanitised_statement`,
    `::test_supersession_skips_the_embed_for_a_blank_statement`, `::test_supersession_text_equals_backfill_text`.
  - `_fact_object` reverted to the `for key in (...)` loop AND a dummy `def _embeddable_text()` re-added to
    `add_entity.py` -> 2 failed:
    `TestFactObjectPresenceRule::test_blank_object_is_not_shadowed_by_the_legacy_column` and
    `TestProspectiveEntityEmbeddingText::test_prospective_entity_text_is_one_function`.

**Acceptance criteria**
- [x] `test_supersession_embeds_the_sanitised_statement` — `tests/unit/memory/graph/test_preference_supersession.py::TestSupersessionEmbedsSanitisedText::test_supersession_embeds_the_sanitised_statement` (asserts `embedding_model.texts == [["prefers light mode"]]`, `roles == ["document"]`, persisted `properties.statement` still `"prefers light\x00 mode\ud800"`).
- [x] `test_supersession_skips_the_embed_for_a_blank_statement` — `::test_supersession_skips_the_embed_for_a_blank_statement` (see the AC-wording deviation above for `embedding == []`).
- [x] `test_supersession_text_equals_backfill_text` — `::test_supersession_text_equals_backfill_text`, computed from the row the resolver just persisted (not from a literal).
- [x] `TestFactObjectPrecedence` — `tests/unit/memory/test_embedding_text.py::TestFactObjectPrecedence` (all five AC shapes + a non-string `object`).
- [x] `test_prospective_entity_text_is_one_function` — `tests/unit/memory/test_embedding_text.py::TestProspectiveEntityEmbeddingText::test_prospective_entity_text_is_one_function`; live grep: `grep -rn "def _entity_embeddable_text\|def _embeddable_text" apps/memory/src` -> 0 hits (exit 1).
- [x] Pre-existing tests stay green (renamed where the helper was) — 3806 passed, incl. `TestEntityEmbeddableText`, `test_inline_and_backfill_text_agree_for_preference_and_fact`, `TestDispatchEntityWriteReusesVector`.
- [x] `## Log` states the verdict on both tuple loops with line numbers — above.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env).

**Evidence**
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
312 files left unchanged / All checks passed! / 312 files already formatted / All checks passed!

$ make pre-commit
prettier..Passed  ruff check..Passed  ruff format..Passed  biome check (harness)..Passed

$ make memory-tests
============================ 3806 passed in 49.88s =============================

$ grep -rn "def _entity_embeddable_text\|def _embeddable_text" apps/memory/src ; echo "exit=$?"
exit=1                      # 0 hits

$ grep -rn "_entity_embeddable_text\|_embeddable_text" apps/memory/src apps/memory/tests   # prose too
tests/unit/memory/test_embedding_text.py:200:  duplicated = {"_embeddable_text", "_entity_embeddable_text"}
                            # the only mention left is the guard test that FORBIDS them

$ uv --directory apps/memory run python <read-only probe of the three fixes; no DB access>
raw='prefers light\x00 mode\ud800'   supersession -> 'prefers light mode'   backfill -> 'prefers light mode'
raw='  prefers light\x0c mode  '     supersession -> 'prefers light mode'   backfill -> 'prefers light mode'
raw='prefiere modo oscuro 🌙 — café'  supersession -> 'prefiere modo oscuro 🌙 — café'  backfill -> same (unmangled)
raw='\x00 \ud800'                    supersession -> ''  (skip the embed)   backfill -> 'preference: p\nstatement:  '
fact {'object': 'lives in Berlin', 'object_': 'lives in Paris'} -> 'lives in Berlin'
fact {'object_': 'lives in Paris'}                             -> 'lives in Paris'
fact {'object': None, 'object_': 'lives in Paris'}             -> 'lives in Paris'
fact {'object': '', 'object_': 'lives in Paris'}    -> 'fact: paul-lives-in\nobject_: lives in Paris'   (node-text, NOT the stale value alone)
fact {'object': '\x00', 'object_': 'lives in Paris'} -> 'fact: paul-lives-in\nobject: \nobject_: lives in Paris'
prospective(preference, +aliases/+confidence) = 'prefers dark mode' == backfill 'prefers dark mode'
```
Step 7 note: the "run it like a user" step is a READ-ONLY in-process probe of the three changed functions
(above) instead of a pipeline run — `make memory-run-*` and `make memory-reset-embeddings` are forbidden here
(the local MongoDB is shared with the human's real memory) and this change has no CLI/runtime surface of its
own: it is pure text construction. The live re-embed remains #141's e2e.

**Notes**
- Pre-existing, NOT fixed here (out of scope: "any other change to what is embedded"):
  `preference_supersession.py:213` `_preference_statement` falls back to `properties.content` when `statement`
  is missing/blank, while `entity_embedding_text` has no `content` fallback and returns the node-text. So a
  legacy content-only preference row still embeds different bytes inline vs in the backfill. Not touched —
  it is a second, independent divergence class (which KEY is read, not which BYTES), and closing it changes
  what a legacy row embeds. Worth a follow-up task.
- No new import edge: `embedding_text.py` is the neutral `memory/` module both layers already import, and
  `preference_supersession.py` lives in `graph/`, which may import `rag/` (ADR-006); `rag/` still imports
  nothing from `graph/`. `tests/unit/memory/test_package_layout.py` is green unchanged.
- Nothing in `docs/adr/`, `docs/glossary.md`, `tasks/141`, `tasks/151`, `tasks/152` was touched. The task's
  frontmatter is now `status: in-progress`.

### [Tester] 2026-09-20 — QA

**Environment / safety statement**
`make env-status` → `local (.env)` throughout. Ran test commands ONE AT A TIME (never two pytest/make
processes concurrently). No `modal` command run. `.env` / `.env.prod` never opened, catted or grepped
(direnv had already exported the local `MONGO_*` / `VOYAGE_API_KEY` vars into the shell — used those, never
read the files). No real pipeline run (`make memory-run-*`, `make memory-serve-workflows`,
`make memory-reset-embeddings` never invoked). One live read/write experiment against the local shared
MongoDB used a throwaway `ObjectId()` user + a throwaway `_id` row, deleted immediately after (proven 0
residual rows for that user). All other DB access was read-only counts/queries against the real user's
`memory`/`users` collections — no statement/object text from the real user's data is reproduced below, only
shapes and counts. No `git checkout`/`restore`/`stash`/`clean` used anywhere; two mutate-then-revert probes
(below) proved byte-identical via `shasum` before/after.

**Test summary**
- Format: `make memory-format-check` → PASS (312 files already formatted)
- Lint: `make memory-lint-check` → PASS (All checks passed!)
- Pre-commit: `make pre-commit` → PASS (prettier / ruff check / ruff format / biome all Passed)
- Unit tests: `make memory-tests` → 3806 passed / 0 failed (run twice: once before the two adversarial
  mutation probes, once after reverting them — identical `3806 passed` both times)
- Warnings: 0 pytest warnings; only the pre-existing, unrelated Python-3.14/opik/pydantic-v1 `UserWarning`
  at interpreter startup (same as #137's QA)

**E2E adversarial pass**
- Happy path: `uv --directory apps/memory run python -c "prospective_entity_embedding_text(entity_type=NodeType.PREFERENCE, name='n', canonical_name='n', properties={'statement': 'dark mode'})"` → `'dark mode'`, equal to `entity_embedding_text` on the equivalent persisted row (PASS).
- Break path 1 (non-vacuity / mutation kill, item 2): reverted `embedding_text.py`'s presence-rule FACT branch to the old `properties.get("object") or properties.get("object_")`, ran `uv --directory apps/memory run pytest tests/unit/memory/test_embedding_text.py -q` → exactly 2 failed (`test_a_blank_object_falls_back_to_node_text_not_to_the_legacy_one[empty]`, `test_a_non_string_object_falls_back_to_node_text`), 45 passed — matches the SWE's claimed red set exactly. Reverted the edit; `shasum` of the file before mutation (`5f0019e8...`) == `shasum` after revert (`5f0019e8...`); `git diff -- embedding_text.py | shasum` before/after also identical (`eabe7d0a...`). PASS.
- Break path 2 (non-vacuity / mutation kill, item 1): reverted `preference_supersession.py`'s `embeddable_statement = strip_invalid_chars(new_statement).strip()` to `= new_statement`, ran `uv --directory apps/memory run pytest tests/unit/memory/graph/test_preference_supersession.py -q` → exactly 3 failed (the 3 `TestSupersessionEmbedsSanitisedText` tests), 30 passed. Reverted; `shasum` before (`651ba9a9...`) == after (`651ba9a9...`); `git diff | shasum` before/after identical (`f14c6d6b...`). PASS.
- Break path 3 (state edge / hostile-shape parity table, item 2 point (a)): tabulated `entity_embedding_text` vs `_fact_object` over the 7 named FACT shapes (`{object valid}`, `{object "", object_ valid}`, `{object None, object_ valid}`, `{object "\x00", object_ valid}`, `{object missing, object_ valid}`, `{object 123}`, `{}`) via a read-only in-process probe (no DB). Only apparent divergence: `{"object": "\x00", "object_": "Paris"}` → `entity_embedding_text` gives the generic-fallback text (non-blank) while `_fact_object` returns the raw `"\x00"` (truthy, so supersession is NOT skipped) — but `_maybe_supersede` then sanitizes `"\x00"` to blank and takes the no-embed branch (same as the disclosed blank-statement case), so the inline path never embeds anything for this row and there is no bytes-divergence, only "deferred to backfill" (same accepted pattern as AC2). Verified `_fact_object`'s truthy-return-for-"\x00"` is UNCHANGED from the old `for key in ("object","object_")` loop (same `.strip()` behaviour on a control char, both before and after this diff) — not a regression. No shape produced two *different non-blank* embedded texts for the same persisted row. PASS (no blocking finding; see Other issues found for a documentation nit).
- Break path 4 (boundary/hostile inputs, invariant check requested in the task): built `prospective_entity_embedding_text` directly (no DB) with a control character, a lone high surrogate, leading/trailing whitespace, an all-invalid-character string, an emoji + em-dash, RTL Arabic text, combining diacritical marks, and a 200,000-character statement. Every case: no crash, `prospective_entity_embedding_text(...) == entity_embedding_text(persisted_row)` (inline/backfill parity holds), and the emoji/RTL/combining-marks cases round-tripped byte-identical to `statement.strip()` (no mangling of legitimate Unicode). PASS.
- Break path 5 (failure mode / Mongo semantics, point (b)): live experiment on a throwaway `ObjectId()` user in the local shared MongoDB — inserted one `memory` row with `kind="node", type="fact"` and **no `embedding` key at all**; ran the exact `_backfill_filter(user_id)` as a `find()` → the throwaway row was the one and only match, confirming Mongo's `$in: [[], None]` matches a MISSING field, not just `[]`/`None` values. Then read the same row back through `MemoryEntry.model_validate(...)` and `MemoryEntry.get(...)` (Beanie) → both parsed successfully, `entry.embedding == []` (the field's `default_factory=list` fires cleanly on the missing key, no validation error). Deleted the throwaway row immediately after; `count_documents({"user_id": throwaway})` → 0 residual rows. PASS — resolves the AC2-wording concern: the SWE's disclosed "no `embedding` key, not `embedding == []`" shape is backfill-eligible and ODM-safe, functionally equivalent to the AC's literal wording.

**Read-only counts against the real user (`paul@example.com`, identified by fact-count=64/preference-count=12 matching the task's stated numbers; `embedded_rows == 2423`, matching #137's dry-run count exactly — confirms this is the same real tenant)**
- Total FACT rows: 64. Rows with `object` key missing/`None`: 1 (and that row has no `object_` key either — same fallback to generic text before and after this diff, no behavioural change). Rows with `object_` key present at all: **0**.
- Rows with `object_` present AND `object` unusable (missing/`None`/blank-string/non-string): **0**.
- Total PREFERENCE rows: 12. Rows with `statement` missing/`None`/blank: **0**. Content-only legacy preference rows (`content` present, `statement` unusable): **0**.
- Conclusion for point (a)'s last question: this change alters what **0** of the human's existing real rows would embed to on the next backfill — the fixes are currently inert on the real tenant's data (they matter for future/legacy rows and for #141's re-pin correctness, not for today's real data).

**Acceptance criteria**
- [x] PASS — `test_supersession_embeds_the_sanitised_statement` — `apps/memory/tests/unit/memory/graph/test_preference_supersession.py::TestSupersessionEmbedsSanitisedText::test_supersession_embeds_the_sanitised_statement` passes; read the test body — asserts `embedding_model.texts == [["prefers light mode"]]`, `roles == ["document"]`, persisted `properties.statement == raw_statement` (`"prefers light\x00 mode\ud800"`).
- [x] PASS — `test_supersession_skips_the_embed_for_a_blank_statement` — same file, passes; asserts `embedding_model.texts == []`, `decisions[0].superseded is True`, `not new_row.get("embedding")`. Ruling on the AC-wording deviation: verified live (break path 5 above) that a MISSING `embedding` key is functionally equivalent to `embedding == []` for both the backfill filter and the ODM read — accepted, not a blocker.
- [x] PASS — `test_supersession_text_equals_backfill_text` — same file, passes; computed from the persisted row the resolver just wrote (`entity_embedding_text(persisted_row)`), not a literal.
- [x] PASS — `TestFactObjectPrecedence` — `apps/memory/tests/unit/memory/test_embedding_text.py::TestFactObjectPrecedence`, all 6 cases pass (5 AC shapes + non-string `object`); mutation-killed independently (break path 1).
- [x] PASS — `test_prospective_entity_text_is_one_function` — passes; live-reran `grep -rn "def _entity_embeddable_text\|def _embeddable_text" apps/memory/src apps/memory/tests apps/memory/scripts apps/memory/deploy` → 0 hits (exit 1); both call sites (`add_entity.py`, `pipeline.py`) import and call `prospective_entity_embedding_text`, verified by reading the diffs directly.
- [x] PASS — pre-existing tests stay green, renamed where the helper was — `test_pipeline.py::TestEntityEmbeddableText` / `TestDispatchEntityWriteReusesVector`, `test_add_entity.py::test_inline_and_backfill_text_agree_for_preference_and_fact` / `test_preference_text_is_sanitized_identically_on_both_paths` all present and passing in the 3806-passed run; no thin wrappers left (`grep` confirms no leftover `_embeddable_text`/`_entity_embeddable_text` defs anywhere in `src`/`tests`/`scripts`/`deploy`).
- [x] PASS — `## Log` states the verdict on both tuple loops with line numbers — present in the `[SWE] 2026-09-20 21:35` entry; independently re-read `_fact_object` (fixed, same presence rule) and `_candidate_statement` (left unchanged, judge-prompt-only) at their current locations and confirm the stated reasoning matches the code.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env) — reproduced above, twice, bracketing the mutation probes.

**Other checks (adversarial ideas from the brief, not separate ACs)**
- `_CachedSingleEmbedding` key-vs-embedded-text invariant: `TestDispatchEntityWriteReusesVector::test_no_second_embed_call_reuses_node_text_vector` still pins "computed once" (unchanged assertion shape, now keyed through `prospective_entity_embedding_text`); independently re-verified the identity (`add_entity_module.prospective_entity_embedding_text is prospective_entity_embedding_text`, `pipeline_module`'s imported symbol too — same object, not just equal-looking functions).
- Deleted symbols: `grep -rn "def _entity_embeddable_text\|def _embeddable_text" apps/memory/src apps/memory/tests apps/memory/scripts apps/memory/deploy` → 0 hits.
- Layering: `grep -rn "^from tree.memory.graph\|^import tree.memory.graph" apps/memory/src/tree/memory/rag/` → 0 hits (ADR-006 intact); `test_package_layout.py` already lists `embedding_text.py` as shared.
- Roles: `git diff | grep input_type` → only the pre-existing `input_type="document"` call site (unchanged value) and the pre-existing `EmbeddingRole | None` test-fixture parameter — no role was changed anywhere in the diff.
- Point (c) (pre-existing content-only preference divergence): confirmed via `git diff` that `_preference_statement` (the function with the `properties.content` fallback) is NOT present anywhere in this task's diff — untouched. Independently reproduced the divergence itself with a direct probe: `{"content": "legacy text only"}` (no `statement` key) → `entity_embedding_text` returns the generic node-text (`"preference: x\nlegacy text only"`) while `_preference_statement` returns `"legacy text only"` (the literal embed input) — genuinely different bytes for the same row. Confirmed pre-existing and correctly left out of scope; real-tenant impact is 0 rows (counts above).

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-format-check
312 files already formatted

$ make memory-lint-check
All checks passed!

$ make pre-commit
prettier..Passed  ruff check..Passed  ruff format..Passed  biome check (harness)..Passed

$ make memory-tests
============================ 3806 passed in 49.09s =============================

$ grep -rn "def _entity_embeddable_text\|def _embeddable_text" apps/memory/src apps/memory/tests apps/memory/scripts apps/memory/deploy ; echo exit=$?
exit=1

# mutation kill #1 (item 2), then revert:
$ uv --directory apps/memory run pytest tests/unit/memory/test_embedding_text.py -q
2 failed, 45 passed in 1.32s
$ shasum apps/memory/src/tree/memory/embedding_text.py   # before == after: 5f0019e86df30730bf423d18d245e430c85d665c

# mutation kill #2 (item 1), then revert:
$ uv --directory apps/memory run pytest tests/unit/memory/graph/test_preference_supersession.py -q
3 failed, 30 passed in 0.73s
$ shasum apps/memory/src/tree/memory/graph/preference_supersession.py   # before == after: 651ba9a9ada1773858536eb9bc4a9b140ac4c06c

$ make memory-tests   # re-run after both reverts
============================ 3806 passed in 48.87s =============================

# live Mongo experiment (throwaway user, deleted immediately):
backfill_filter matched our throwaway row: True | total matches for user: 1
raw doc has 'embedding' key: False
MemoryEntry parsed ok; entry.embedding == [] (default_factory=list applied)
Beanie .get() parsed ok; embedding == []
deleted throwaway row count: 1
remaining rows for throwaway user (should be 0): 0

# real-user read-only counts (no statement/object text printed):
total_facts: 64  facts_with_object__key_present: 0  facts_object__present_AND_object_unusable: 0
total_prefs: 12  prefs_content_only: 0
embedded_rows(any type): 2423   # matches #137's dry-run count exactly (same real tenant)

$ git status --porcelain   # after all probes — no scratch residue, same 9 files as before QA
 M apps/memory/src/tree/memory/embedding_text.py
 M apps/memory/src/tree/memory/graph/add_entity.py
 M apps/memory/src/tree/memory/graph/preference_supersession.py
 M apps/memory/src/tree/memory/pipeline.py
 M apps/memory/tests/unit/memory/graph/test_add_entity.py
 M apps/memory/tests/unit/memory/graph/test_preference_supersession.py
 M apps/memory/tests/unit/memory/test_embedding_text.py
 M apps/memory/tests/unit/memory/test_pipeline.py
 M tasks/150-embedding-text-follow-ups-from-137-qa.md
```

**Other issues found (not blocking)**
- `_fact_object`'s docstring/comment could note explicitly that a control-character-only `object` (e.g. `"\x00"`) is still treated as "present and usable" (truthy after `.strip()`, since `.strip()` doesn't remove control characters) and will drive a real supersession attempt (candidates fetched, judge called on raw `"\x00"`) before `_maybe_supersede`'s own sanitize-to-blank guard silently drops the vector. This is pre-existing behaviour (verified unchanged from the old `for key in (...)` loop) and does not cause a bytes-divergence bug (the inline path never embeds in this case, matching the accepted blank-statement pattern), but a reader could reasonably expect `_fact_object` and `entity_embedding_text`'s "usability" definitions to agree exactly, and they don't for this one shape. Worth a one-line docstring note in a future pass; not blocking this task.
- No test exercises the FACT-with-control-character-object variant of the blank-embed/still-superseded path (only the PREFERENCE variant is tested in `TestSupersessionEmbedsSanitisedText`). Functionally covered by the same `_maybe_supersede` code path (type-agnostic), verified by direct reasoning + the probe above, so not a blocking gap — but a follow-up test would remove the need to reason about it.
- Confirms the SWE's own disclosed, deliberately out-of-scope finding (`_preference_statement`'s `content` fallback divergence) is real, pre-existing, and currently affects 0 real rows — correctly left as a follow-up rather than folded into this task.

**VERDICT: PASS**
