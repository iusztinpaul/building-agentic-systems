---
id: 106-rag-cleaning-module
feature: rag-graphrag-modes
status: done
---

# Deterministic **Clean step** as a standalone pure module (`tree/memory/rag/cleaning.py`)

Tags: `memory`, `rag`, `pipeline`
Depends on: #105
Blocks: #108
Implements: ADR-006 — Decision 7

## Scope

Create the `tree.memory.rag` package (first module) with a **pure** cleaning module — no
Prefect, no DB, no `tree.memory.*` pipeline imports, no config import — so a future
fine-tuning pipeline can import the SAME function and avoid train/serve drift. Wire it as the
FIRST memory-pipeline stage: in `_extract_chunks_and_structural` (task ①,
`tree/memory/extraction/pipeline.py`) call `clean_text(document.content)` before chunking.

**Public API** (`tree/memory/rag/cleaning.py`):
- `clean_text(text: str) -> str` — the composed, idempotent pipeline (`clean_text(clean_text(x)) == clean_text(x)`), applying in order:
  1. `strip_invalid_chars(text) -> str` — remove control chars and lone surrogates. Move the
     regex from `tree/memory/embedding_text.py::_INVALID_EMBED_CHARS_RE` here; `embedding_text`
     imports `strip_invalid_chars` from the cleaning module (delete its private copy — ONE
     definition). Keep `\n` and `\t`.
  2. `normalize_markdown(text) -> str` — CRLF → LF; ATX headings normalised to `# ` form
     (`#Heading` → `# Heading`, setext `Heading\n=====` → `# Heading`, `-----` → `## `);
     collapse 3+ consecutive blank lines to 2; trailing whitespace per line stripped.
  3. `drop_boilerplate_lines(text) -> str` — drop a line when (a) it matches a small closed
     pattern list (cookie/consent banners: `accept all cookies`, `we use cookies`; nav
     residue: lines that are only `Home`, `Menu`, `Subscribe`, `Share`, `Sign in`, `Log in`;
     Substack residue: `Thanks for reading`, `Share this post`), matched case-insensitively
     on the WHOLE trimmed line, or (b) the identical non-empty trimmed line (≥ 20 chars)
     occurs 3+ times in the document (repeated headers/footers). Good: a `Subscribe` line
     between paragraphs is dropped. Bad: a paragraph that contains the word "subscribe" must
     NOT be touched.
  4. `collapse_whitespace(text) -> str` — runs of spaces/tabs inside a line → one space; never
     touches newlines (paragraph structure is what the recursive splitter keys on in #107).
- `clean_text("")` → `""`; `None` is not accepted (type-check only; callers pass `document.content or ""`).

Determinism is a hard requirement (Prefect `INPUTS` caching in #108 relies on the cleaned
text being a pure function of the raw text): no randomness, no clock, no config reads.

## Acceptance Criteria

- [x] `tree/memory/rag/__init__.py` and `tree/memory/rag/cleaning.py` exist; a unit test walks the AST of `cleaning.py` and asserts it imports only from the stdlib (`re`, `collections`, `typing`) — no `tree.*`, `prefect`, `pymongo`, `beanie` imports.
- [x] `clean_text("a\x00b\ud800c")` returns `"abc"`; `"\n"` and `"\t"` survive `strip_invalid_chars`.
- [x] `normalize_markdown("Title\n=====\n\ntext\r\nmore")` returns `"# Title\n\ntext\nmore"`; `"#Heading"` becomes `"# Heading"`; 5 consecutive blank lines collapse to exactly 2.
- [x] `drop_boilerplate_lines("Intro\nAccept all cookies\nBody")` returns `"Intro\nBody"`; a line `"You should subscribe to good newsletters."` is preserved (pattern matches whole line only).
- [x] A 21-char line repeated 3 times across the text is dropped everywhere; the same line repeated twice is kept (threshold is 3); a repeated 5-char line is kept (below the 20-char floor).
- [x] `collapse_whitespace("a   b\t\tc\n\nd")` returns `"a b c\n\nd"` (newlines untouched).
- [x] `clean_text` is idempotent for every fixture in the test module (`clean_text(clean_text(x)) == clean_text(x)`), and `clean_text("") == ""`.
- [x] `tree/memory/embedding_text.py` no longer defines `_INVALID_EMBED_CHARS_RE` / `_sanitize_for_embedding`; `node_to_embedding_text` still strips control chars (existing test `test_strips_control_chars_and_surrogates` passes via the imported `strip_invalid_chars`).
- [x] Task ① (`_extract_chunks_and_structural`) chunks `clean_text(document.content)`: a unit test with content `"Intro\nAccept all cookies\nBody"` yields chunk text without the cookie line.
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green.

## User Stories

### Story: Ingested Substack article loses its footer boilerplate
1. A `Document` with content ending in `"Thanks for reading!\nShare this post\n"` is passed to task ①.
2. The produced chunk texts contain neither `Thanks for reading!` nor `Share this post`.
3. The article body paragraphs are byte-identical apart from whitespace collapse.

### Story: Future fine-tuning code reuses the exact same cleaner
1. A script does `from tree.memory.rag.cleaning import clean_text` in a process that has no Mongo and no Prefect server.
2. The import succeeds without side effects (no DB connection, no config file read).
3. `clean_text(raw)` returns the same string the pipeline stored/chunked for that raw text.

### Story: Re-running the pipeline hits the Prefect cache
1. The same `Document` is run through task ① twice.
2. Both runs produce identical `ChunkedDocument` payloads (the cleaner is deterministic), so the second run is an `INPUTS` cache hit downstream.

### Story: A scraped page with Windows line endings and setext headings becomes clean markdown
1. Content `"Agents\r\n======\r\n\r\n\r\n\r\nBody   text"` is cleaned.
2. Result is `"# Agents\n\nBody text"`.

## Out of scope

- HTML → markdown conversion (done by the data pipeline / Bright Data before `Document.content`).
- Language detection, deduplication across documents, PII scrubbing.
- Any change to the Data Pipeline.

---

Blocked by: #105

## Log

### [SWE] 2026-09-05 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/rag/__init__.py` — new RAG package (docstring only, no re-exports, so importing a submodule pulls in nothing else).
- `apps/memory/src/tree/memory/rag/cleaning.py` — new pure Clean step: `clean_text`, `strip_invalid_chars`, `normalize_markdown`, `drop_boilerplate_lines`, `collapse_whitespace`; stdlib-only (`re`, `collections`).
- `apps/memory/src/tree/memory/embedding_text.py` — deleted `_INVALID_EMBED_CHARS_RE` / `_sanitize_for_embedding`; now imports `strip_invalid_chars` from the cleaning module (ONE definition).
- `apps/memory/src/tree/memory/extraction/pipeline.py` — task ① `_extract_chunks_and_structural` chunks `clean_text(document.content or "")`.
- `apps/memory/tests/unit/memory/rag/{__init__.py,test_cleaning.py}` — 33 tests: AST purity, per-stage behaviour, idempotence + determinism over 14 fixtures.
- `apps/memory/tests/unit/memory/extraction/test_pipeline.py` — 3 tests: cleaning before chunking, Substack footer never reaches chunks, deterministic chunk texts across runs.
- `apps/memory/tests/unit/memory/test_embedding_text.py` — `TestSanitizeForEmbedding` replaced by `TestSanitizationIsDelegatedToTheCleaningModule` (asserts the private copies are gone and node-text still strips control chars/surrogates); local imports lifted to module level.

**Tests**
- Unit: 2025 passing, 0 failing — `make memory-tests` (env target: local).
- Integration: N/A — no integration suite in this repo (AGENTS.md); e2e done by running task ① for real (Evidence).

**Acceptance criteria**
- [x] `rag/__init__.py` + `rag/cleaning.py` exist, AST-enforced stdlib-only — `tests/unit/memory/rag/test_cleaning.py::TestModulePurity::test_cleaning_module_imports_only_stdlib`
- [x] `clean_text("a\x00b\ud800c")` → `"abc"`; `\n`/`\t` survive — `TestStripInvalidChars::test_removes_control_chars_and_lone_surrogates`, `::test_keeps_newline_and_tab`, `::test_strips_each_invalid_class`
- [x] setext/CRLF/ATX/blank-run normalisation — `TestNormalizeMarkdown::test_converts_setext_h1_and_crlf`, `::test_converts_setext_h2_dashes`, `::test_adds_space_after_atx_hashes`, `::test_collapses_five_blank_lines_to_two_newlines`
- [x] cookie line dropped, "You should subscribe to good newsletters." preserved — `TestDropBoilerplateLines::test_drops_cookie_banner_line`, `::test_keeps_paragraph_that_merely_contains_a_pattern_word`, `::test_drops_each_closed_pattern`
- [x] 21-char line ×3 dropped; ×2 kept; 5-char ×3 kept — `TestDropBoilerplateLines::test_drops_line_repeated_three_times`, `::test_keeps_line_repeated_twice`, `::test_keeps_short_repeated_line`
- [x] `collapse_whitespace("a   b\t\tc\n\nd") == "a b c\n\nd"` — `TestCollapseWhitespace::test_collapses_space_and_tab_runs_without_touching_newlines`
- [x] idempotent for every fixture; `clean_text("") == ""` — `TestCleanText::test_is_idempotent` (14 fixtures), `::test_is_deterministic_across_calls`, `::test_empty_string_returns_empty`
- [x] `embedding_text` no longer defines the private sanitizer; node text still stripped — `tests/unit/memory/test_embedding_text.py::TestSanitizationIsDelegatedToTheCleaningModule::*`
- [x] task ① chunks cleaned text — `tests/unit/memory/extraction/test_pipeline.py::TestExtractChunksAndStructuralTask::test_content_is_cleaned_before_chunking` (+ `::test_substack_footer_boilerplate_never_reaches_chunks`, `::test_chunk_texts_are_deterministic_across_runs`)
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green — Evidence below

**Evidence**

```
$ make memory-tests
2025 passed in 15.35s
  tests/unit/memory/rag/test_cleaning.py ................................. [ 79%]
  tests/unit/memory/extraction/test_pipeline.py .......................... [ 64%]

$ make memory-format-check && make memory-lint-check
252 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ... Passed
```

Red-before-green check (pipeline wiring reverted, then restored):
```
FAILED tests/unit/memory/extraction/test_pipeline.py::TestExtractChunksAndStructuralTask::test_content_is_cleaned_before_chunking
FAILED tests/unit/memory/extraction/test_pipeline.py::TestExtractChunksAndStructuralTask::test_substack_footer_boilerplate_never_reaches_chunks
2 failed, 2023 passed
```

End-to-end, bare process (fine-tuning story — no Mongo, no Prefect):
```
$ uv run python -c "<import tree.memory.rag.cleaning; clean a scraped CRLF page>"
heavy modules imported: []
tree submodules: ['tree', 'tree.memory', 'tree.memory.rag', 'tree.memory.rag.cleaning']
'# Agents in production\n\n# Why agents\nAn agent loops over tools. You should subscribe to good newsletters.\nBadchar removed.\n\n'
idempotent: True
```

End-to-end through task ① (Substack-shaped Document):
```
extract_chunks_and_structural: doc_id=507f1f77bcf86cd799439011 n_chunks=1 n_structural_entries=3
chunk[0]: '# Agents in production\n\nAn agent loops over tools.\n'
```

**Notes**
- **Blank-line spec reconciliation.** The AC "5 consecutive blank lines collapse to exactly 2" and the User Story `"Agents\r\n======\r\n\r\n\r\n\r\nBody   text"` → `"# Agents\n\nBody text"` only agree if the count is of NEWLINES: the story's 3 blank lines become 1. Implemented as `\n{3,}` → `\n\n` (at most one blank line), which reproduces the story byte-for-byte; the test is named `test_collapses_five_blank_lines_to_two_newlines`.
- **Trailing punctuation in the phrase list.** The Substack story ships `"Thanks for reading!"` while the pattern list says `Thanks for reading`, so the whole-line match ignores trailing `!.,:;…`. Still whole-line only: "You should subscribe to good newsletters." is untouched.
- **`drop_boilerplate_lines` also collapses blank runs.** Deleting a line that sat between two blank lines would leave a 3+ newline run that `normalize_markdown` (which already ran) forbids, breaking `clean_text` idempotence. One shared `_collapse_blank_runs` helper, not a second mechanism.
- Setext underlines require 2+ `=`/`-` and a non-blank, non-`#` line above, so a lone `-` bullet and a thematic break after a blank line are left alone.
- `collapse_whitespace` uses `[ \t]+` → `" "`, so it also normalises leading indentation — fenced code blocks would lose indentation. Acceptable for the scraped-markdown corpus we ingest; if code fences become a problem, the fix is a fence-aware skip in this one function.
- `strip_invalid_chars` keeps the exact regex `embedding_text` used, so embedding text is byte-identical to before for existing rows.

### [Tester] 2026-09-05 14:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check` → "252 files already formatted" / "All checks passed!"; `make pre-commit` → all hooks Passed)
- Unit tests: 2025 passed / 0 failed (`make memory-tests`, env target: local)
- Integration tests: N/A — no integration suite in this repo per AGENTS.md
- Warnings: 0 (pytest summary reports no captured warnings; the one `UserWarning: Core Pydantic V1 functionality isn't compatible with Python 3.14` seen on stderr is emitted at `opik` import time, pre-existing, unrelated to this diff)

**E2E adversarial pass**
- Happy path: `clean_text("Intro\nAccept all cookies\nBody")` → `"Intro\nBody"` via `_extract_chunks_and_structural`; also ran the literal user-story input `clean_text("Agents\r\n======\r\n\r\n\r\n\r\nBody   text")` → `"# Agents\n\nBody text"` (PASS)
- Break path 1 (idempotence on nasty inputs — only-whitespace, only-boilerplate, mixed CRLF/CR): `clean_text("   \t  \n\n\t  \n   ")` → `"\n\n"`, `clean_text(clean_text(x)) == clean_text(x)` for all 4; `clean_text("Home\nMenu\nSubscribe\nShare\nSign in\nLog in\naccept all cookies\nwe use cookies")` → `""`, idempotent; `clean_text("a\r\nb\rc\r\n\rd")` → `"a\nb\nc\n\nd"`, idempotent (PASS)
- Break path 2 (repeat-count/length-floor boundaries): 19-char line ×5 kept (`drop_boilerplate_lines`, floor is strictly ≥20); 20-char line ×3 dropped (at the floor); mid-sentence `"If you liked this, please share this post with friends because it matters."` survives untouched (PASS)
- Break path 3 (heading normalisation inside a fenced code block): `clean_text` on a fenced ```python``` block turns `#comment no space` into `# comment no space` — `normalize_markdown`'s ATX-heading rule is NOT fence-aware and rewrites a Python comment's content, not just whitespace. Separately, 4-space code indentation collapses to 1 space via `collapse_whitespace` (the documented trade-off). Neither is in scope per the AC/story text or ADR-006 Decision 7, and the whitespace case is explicitly flagged by the SWE as an accepted trade-off; the ATX-comment mutation is a new finding, logged below as "Other issues found," not a blocking break path since no AC covers code-fence content (PASS with note)
- Break path 4 (module-purity / bare-process import): `sys.modules` diff around `from tree.memory.rag.cleaning import clean_text` in a bare `uv run python -c` → 13 new modules, 0 containing `prefect`/`pymongo`/`beanie`/`tree.config`; only `tree`, `tree.memory`, `tree.memory.rag`, `tree.memory.rag.cleaning` under `tree.*` (PASS)
- Break path 5 (duplicate-regex check): `grep -rn "x00-\\x08\|ud800-udfff"` across `apps/memory/src` → only 2 hits, both in `rag/cleaning.py` (definition + one use site); `embedding_text.py` no longer defines `_INVALID_EMBED_CHARS_RE`/`_sanitize_for_embedding` (PASS)

**Acceptance criteria**
- [x] PASS — `rag/__init__.py` + `rag/cleaning.py` exist, stdlib-only (AST-enforced) — `tests/unit/memory/rag/test_cleaning.py::TestModulePurity::test_cleaning_module_imports_only_stdlib` passes; manually confirmed via `sys.modules` diff (no prefect/pymongo/beanie/tree.config pulled in)
- [x] PASS — `clean_text("a\x00b\ud800c")` → `"abc"`; `\n`/`\t` survive `strip_invalid_chars` — verified literally: `clean_text('a\x00b\ud800c') == 'abc'` → True; `strip_invalid_chars('\n\t') == '\n\t'` → True
- [x] PASS — `normalize_markdown("Title\n=====\n\ntext\r\nmore")` → `"# Title\n\ntext\nmore"`; `"#Heading"` → `"# Heading"`; blank-run collapse — verified literally, all True. Note: the AC's "5 consecutive blank lines collapse to exactly 2" is ambiguous (newlines vs. blank-line count); the SWE's chosen interpretation (2 newlines = 1 blank line survives) is the only one that reproduces the User Story input byte-for-byte (`clean_text("Agents\r\n======\r\n\r\n\r\n\r\nBody   text") == "# Agents\n\nBody text"` verified True) — accepting the story as the tie-breaker per the SWE's documented reconciliation note
- [x] PASS — `drop_boilerplate_lines("Intro\nAccept all cookies\nBody")` → `"Intro\nBody"`; `"You should subscribe to good newsletters."` preserved — verified literally, both True
- [x] PASS — 21-char line ×3 dropped, ×2 kept, threshold=3, 20-char floor — verified literally: 21-char×3 dropped, 21-char×2 kept, 5-char×5 kept, 19-char×5 kept (below floor), 20-char×3 dropped (at floor)
- [x] PASS — `collapse_whitespace("a   b\t\tc\n\nd") == "a b c\n\nd"` — verified literally, True
- [x] PASS — idempotent for every fixture, `clean_text("") == ""` — `TestCleanText::test_is_idempotent` (14 fixtures) + `::test_empty_string_returns_empty` pass; additionally verified idempotence manually on 4 adversarial inputs not in the fixture list (see break path 1)
- [x] PASS — `embedding_text.py` no longer defines the private sanitizer — `grep -n "_INVALID_EMBED_CHARS_RE\|_sanitize_for_embedding" src/tree/memory/embedding_text.py` → no matches (exit 1); `node_to_embedding_text` still strips control chars via `TestSanitizationIsDelegatedToTheCleaningModule::test_node_text_strips_control_chars_and_surrogates`
- [x] PASS — task ① chunks `clean_text(document.content)` — `tree/memory/extraction/pipeline.py:305-307` calls `clean_text(document.content or "")` before `chunk_document`; `test_pipeline.py::TestExtractChunksAndStructuralTask::test_content_is_cleaned_before_chunking` uses the literal AC input `"Intro\nAccept all cookies\nBody"` and asserts the cookie line is absent from chunks
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green — see Test summary and Evidence

**Evidence**
```
$ make memory-format-check && make memory-lint-check
252 files already formatted
All checks passed!

$ make memory-tests
============================ 2025 passed in 15.47s =============================

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ... Passed

$ grep -rn "x00-\\x08\|ud800-udfff" apps/memory/src
apps/memory/src/tree/memory/rag/cleaning.py:33:_INVALID_CHARS_RE = re.compile(...)
apps/memory/src/tree/memory/rag/cleaning.py:93:    return _INVALID_CHARS_RE.sub("", text)

$ uv run python -c "clean_text('#comment no space' inside a python fence)"
'```python\n# comment no space\nx=1\n```'
```

**Other issues found**
- `normalize_markdown`'s ATX-heading normalisation (`#Heading` → `# Heading`) is not fenced-code-aware: a Python comment `#comment` inside a ```` ```python ```` block is rewritten to `# comment`, mutating code content (not just whitespace, unlike the already-documented `collapse_whitespace` indentation trade-off). No AC or ADR-006 Decision 7 text scopes code-fence handling, so not a blocker for this task — worth a follow-up task if the corpus ingests articles with code samples (Substack/technical blogs are named sources in the glossary's Listen sources).
- No other issues found. Scope is clean (only the 5 files/dirs listed in the SWE summary are touched), no `print()`, all functions typed, docstrings clear with good/bad examples per CLAUDE.md's "Key Principles."

**VERDICT: PASS**

### [PA] 2026-09-05 20:32 — Acceptance Review

**VERDICT: REJECT** (feature-level verdict for PR #41, `rag-graphrag-modes`)

Rollup Issue 3 is this task's: `normalize_markdown` rewrites `#comment` -> `# comment` and `collapse_whitespace` flattens indentation INSIDE fenced code blocks, so the retrieved `content` (CLI excerpt, MCP `search_memory`, LLM extraction input) carries un-runnable code for the code-heavy Substack corpus this repo ingests. #107 already made the splitter fence-aware; the Clean step must be consistent. Every AC of this task still holds — the spec (mine) did not anticipate fences.

Filed ONE rollup task for the whole feature: `tasks/112-pa-rejection-rag-graphrag-modes.md` (8 issues). Pipeline re-runs from the inner loop with the rollup task; on green, re-run acceptance on this task.

### [PA] 2026-09-05 23:20 — Acceptance Review (round 2)

**VERDICT: ACCEPT** (feature-level verdict for PR #41, `rag-graphrag-modes`, HEAD `08cd632`)

Rollup Issue 3 landed: `clean_text` leaves fenced code (``` and ~~~, unclosed fences included) byte-identical apart from CRLF->LF and `rstrip`; `#comment` and indentation survive, `#Heading` outside a fence still normalises, boilerplate counting skips fences. `cleaning.py` now owns the ONE fence definition and stays stdlib-only; idempotence fixtures cover the fenced article. Previously-PASS criteria re-checked and holding.

Rollup `tasks/done/112-pa-rejection-rag-graphrag-modes.md` implemented and Tester-PASSED (round 2). Hand off to the PR Reviewer.
