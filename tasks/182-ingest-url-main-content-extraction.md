---
id: 182-ingest-url-main-content-extraction
status: pending
feature: horizon-mcp-fixes
---

# `ingest_url` keeps the article, not the site chrome: Unlocker HTML → trafilatura markdown (headings, links, tables), falling back to Bright Data's markdown when the extraction is thin

Tags: `web`, `data`, `ingestion`, `dependency`, `brightdata`
Depends on: —
Blocks: —
Implements: ADR-013 §6

## Problem

`ingest_url` stores a page's navigation / sidebar boilerplate: a `gofastmcp.com` docs page ingested as
chunks that were mostly menu links. Fetch path: `fetch_and_extract_web`
(`apps/memory/src/tree/data/web/web.py`) → `fetch_url(url, data_format="markdown")`
(`web_unlocker.py`), i.e. Bright Data's whole-page markdown. Used by the thin MCP flow (`ingest_url`) AND
`extract_batch` (`web_pipeline.py`, which `search_web(ingest=true)` triggers). The **Clean step** removes
repeated lines but not a one-off menu.

## Scope

**Human decision (final):** fetch HTML via the Unlocker and extract the main content with `trafilatura`
(new dependency) to markdown (keep headings and links); fall back to today's Bright Data markdown when the
extraction yields too little. Applies to `ingest_url` and the batch path; `scrape_web` is unchanged.
Non-goal: re-ingesting already-stored documents.

1. **Dependency:** `trafilatura>=2.3` in `[project.dependencies]` of `apps/memory/pyproject.toml` (+ `uv lock`)
   — a MAIN dependency (the Prefect Managed per-run `pip install ./apps/memory` runs the web leaf); with the
   same style of comment as the clustering block. Import at module level in the new helper (lxml import is
   ~0.3 s; no lazy-import test needed).
2. **Pure helper** `extract_main_content(html: str, url: str) -> ExtractedPage | None` in a new
   `apps/memory/src/tree/data/web/web_extract.py` (no network, no Mongo): `trafilatura.extract(html,
   url=url, output_format="markdown", include_links=True, include_formatting=True, include_tables=True,
   include_comments=False)`; `ExtractedPage` (Pydantic) = `{markdown: str, title: str | None}` with
   `title` from `trafilatura.extract_metadata(html)` (fail-open to `None`). Returns `None` when trafilatura
   returns `None` or the markdown has fewer than `MIN_EXTRACTED_CHARS = 300` non-whitespace characters.
   (trafilatura 2.3.0 verified: the call above yields `# Heading`, `[text](url)`, `- item` markdown and
   drops `<nav>`/`<footer>`.)
3. **`fetch_and_extract_web`:** `html = await fetch_url(url, data_format="html")` → `extracted =
   extract_main_content(html, url)`; if `None`: `logger.warning("Main-content extraction too thin for %s —
   falling back to Bright Data markdown (second Unlocker request)", url)` and `markdown = await
   fetch_url(url, data_format="markdown")` (today's path, a SECOND billable Tier-B request — accepted and
   recorded in ADR-013 §6). Title: `extracted.title` → `_derive_title(markdown, url)` (first H1 → URL
   tail); summary as today. `Document.metadata["extraction"] = "trafilatura" | "brightdata_markdown"` so
   the Tester can tell which path a row took. No other schema change; the **Clean step** still runs
   downstream unchanged.
4. **Untouched:** `scrape_web` / `scrape_one` keep calling `fetch_url(data_format=...)` directly;
   `web_scraper_api.py` untouched.
5. **Docs:** `docs/glossary.md` new **Main-content extraction** row + a "distinct from" clause on
   **Clean step** (Part 2); `apps/memory/README.md` `ingest_url` row ("main content extracted with
   trafilatura; falls back to Bright Data markdown"); ADR-002 Tier-B note goes through ADR-013 §6 (no
   ADR-002 edit).
6. **Tests** (`tests/unit/data/web/test_web.py`, new `test_web_extract.py`): inline HTML with `<nav>` (10
   links), `<aside>`, `<main>` (an `h1`, three paragraphs, a link, a list, a table) and `<footer>` →
   markdown contains the `h1`, the paragraphs, `[link](…)`, the list; contains none of the nav anchor
   texts; `title` is the metadata title; a thin page (`<main>ok</main>`) → `None`; `fetch_and_extract_web`
   with `fetch_url` mocked: rich page → ONE call (`data_format="html"`), `metadata["extraction"] ==
   "trafilatura"`; thin page → TWO calls in order `html`, `markdown`, content = the markdown body,
   `metadata["extraction"] == "brightdata_markdown"`, WARNING logged; `_derive_title` fallbacks; `scrape_one`
   still requests `markdown` (existing tests).
7. **Live verification** (LOCAL env): `make memory-run-pipeline MODE=online
   SOURCE="https://gofastmcp.com/apps/low-level"` (or `ingest_url` on the local MCP server) → in mongosh the
   Document's `content` starts at the article heading, `metadata.extraction == "trafilatura"`, and the share
   of lines that are bare `[text](url)` links is < 20 % (count in a one-liner); record the content length vs
   a `scrape_web` of the same URL (which still shows the menu). One deliberately thin URL (e.g. a page
   behind a JS shell) → `brightdata_markdown` path with the WARNING. Delete the two documents afterwards.
   **Prefect Managed (prod) signal, post-merge:** the worker side cannot be live-verified without writing
   to prod; the first post-merge nightly / offline run whose web leaf imports `trafilatura` is the proof
   (a per-run `pip install` wheel failure there routes straight back to this task). Record that run id in
   the Log when it happens.

## Acceptance criteria

- [ ] `trafilatura>=2.3` is a main dependency; `uv lock` updated; `make memory-tests` runs with it.
- [ ] `extract_main_content` returns markdown with headings, links, lists and tables of the main content and
      none of the `<nav>`/`<aside>`/`<footer>` link texts; returns `None` under 300 non-whitespace chars.
- [ ] `fetch_and_extract_web` fetches HTML first; on a rich page it makes exactly one Unlocker request and
      stamps `metadata.extraction = "trafilatura"`; on a thin page it makes a second `markdown` request,
      logs a WARNING and stamps `"brightdata_markdown"`.
- [ ] Title = metadata title, else first H1, else URL tail; summary unchanged.
- [ ] `scrape_web` behaviour and tests unchanged.
- [ ] README row and glossary rows updated.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [ ] Live (local): the gofastmcp page ingests with `extraction: trafilatura`, article-first content and
      < 20 % bare-link lines; one thin page takes the fallback; evidence in the Log.

## User Stories

### Story: The user saves a docs page and later finds its content, not its menu
1. The user says "save https://gofastmcp.com/apps/low-level"; the model calls `ingest_url(url=…)`.
2. The worker fetches the HTML, extracts the article (`# Low-level MCP Apps` …), stores it; the model later calls `search_memory(query="ontoolresult content block")` and the top parent is the article paragraph about `ontoolresult`, not a list of "Getting started / Servers / Clients" links.

### Story: A sparse page still ingests
1. `ingest_url` on a page whose main content is a short notice.
2. The log shows `WARNING Main-content extraction too thin for <url> — falling back to Bright Data markdown (second Unlocker request)`; the document is stored with `metadata.extraction: brightdata_markdown`.

### Story: `search_web(ingest=true)` benefits without extra wiring
1. The model calls `search_web(query=…, ingest=true, ingest_top_k=2)` (when that deployment is registered).
2. `extract_batch` runs `fetch_and_extract_web` on both URLs — the same extraction and fallback.

### Story: `scrape_web` is the same as yesterday
1. The model calls `scrape_web(urls=[...])`.
2. It still answers Bright Data's markdown of the whole page (menu included) — the inline-reading tool does not change.

---

Blocked by: (none)

## Log

### [PA] 2026-10-03 18:10 — Grooming

**Summary**
Web ingestion fetches HTML and keeps the main content via trafilatura's markdown (headings, links, lists,
tables), falling back to Bright Data's whole-page markdown — a second billable request — only when the
extraction is thinner than 300 characters; rows record which path they took.

**Key decisions**
- trafilatura is a main dependency (the Prefect Managed install must run the web leaf).
- "Too little" is a number: 300 non-whitespace characters. A `favor_recall` second rung before the billable
  fallback is NOT built (upgrade trigger in ADR-013 §6).
- `metadata.extraction` stamps the path so QA can verify without reading logs.
- `scrape_web` stays whole-page: it is the "read it inline" tool and the user may want the chrome.

**Dependencies**
- None.

**User stories**
- 4 stories: the docs page, a sparse page, the batch path, unchanged `scrape_web`.

Ready for implementation.
