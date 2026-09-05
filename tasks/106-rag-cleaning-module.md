---
id: 106-rag-cleaning-module
feature: rag-graphrag-modes
status: pending
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

- [ ] `tree/memory/rag/__init__.py` and `tree/memory/rag/cleaning.py` exist; a unit test walks the AST of `cleaning.py` and asserts it imports only from the stdlib (`re`, `collections`, `typing`) — no `tree.*`, `prefect`, `pymongo`, `beanie` imports.
- [ ] `clean_text("a\x00b\ud800c")` returns `"abc"`; `"\n"` and `"\t"` survive `strip_invalid_chars`.
- [ ] `normalize_markdown("Title\n=====\n\ntext\r\nmore")` returns `"# Title\n\ntext\nmore"`; `"#Heading"` becomes `"# Heading"`; 5 consecutive blank lines collapse to exactly 2.
- [ ] `drop_boilerplate_lines("Intro\nAccept all cookies\nBody")` returns `"Intro\nBody"`; a line `"You should subscribe to good newsletters."` is preserved (pattern matches whole line only).
- [ ] A 21-char line repeated 3 times across the text is dropped everywhere; the same line repeated twice is kept (threshold is 3); a repeated 5-char line is kept (below the 20-char floor).
- [ ] `collapse_whitespace("a   b\t\tc\n\nd")` returns `"a b c\n\nd"` (newlines untouched).
- [ ] `clean_text` is idempotent for every fixture in the test module (`clean_text(clean_text(x)) == clean_text(x)`), and `clean_text("") == ""`.
- [ ] `tree/memory/embedding_text.py` no longer defines `_INVALID_EMBED_CHARS_RE` / `_sanitize_for_embedding`; `node_to_embedding_text` still strips control chars (existing test `test_strips_control_chars_and_surrogates` passes via the imported `strip_invalid_chars`).
- [ ] Task ① (`_extract_chunks_and_structural`) chunks `clean_text(document.content)`: a unit test with content `"Intro\nAccept all cookies\nBody"` yields chunk text without the cookie line.
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` green.

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
