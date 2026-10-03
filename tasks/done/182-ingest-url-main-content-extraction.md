---
id: 182-ingest-url-main-content-extraction
status: done
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

- [x] `trafilatura>=2.3` is a main dependency; `uv lock` updated; `make memory-tests` runs with it.
- [x] `extract_main_content` returns markdown with headings, links, lists and tables of the main content and
      none of the `<nav>`/`<aside>`/`<footer>` link texts; returns `None` under 300 non-whitespace chars.
- [x] `fetch_and_extract_web` fetches HTML first; on a rich page it makes exactly one Unlocker request and
      stamps `metadata.extraction = "trafilatura"`; on a thin page it makes a second `markdown` request,
      logs a WARNING and stamps `"brightdata_markdown"`.
- [x] Title = metadata title, else first H1, else URL tail; summary unchanged.
- [x] `scrape_web` behaviour and tests unchanged.
- [x] README row and glossary rows updated.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [x] Live (local): the gofastmcp page ingests with `extraction: trafilatura`, article-first content and
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

### [SWE] 2026-10-03 19:05 — BLOCKED: live probe contradicts the spec on the motivating page type

**Files modified (uncommitted)**
- `apps/memory/pyproject.toml` — `trafilatura>=2.3` in `[project.dependencies]` (main dep, clustering-style comment).
- `apps/memory/uv.lock` — trafilatura 2.3.0 + courlan/htmldate/justext/lxml/lxml-html-clean/tld/babel; forced bumps
  charset-normalizer 3.4.4→3.5.2, dateparser 1.3.0→1.4.3, urllib3 2.6.3→2.8.0 (trafilatura/htmldate minimums).
- No `web_extract.py` / `web.py` / test changes yet.

**Evidence** — live Unlocker fetch (`data_format=markdown` and `html`) and the spec-exact `trafilatura.extract(...)` call
(`output_format="markdown", include_links, include_formatting, include_tables, include_comments=False`):

| page | Bright Data md (chars / code fences) | trafilatura md (chars / fences) | dup sentences | glued `](…)word` | H1 kept |
|---|---|---|---|---|---|
| gofastmcp.com/deployment/prefect-horizon (Mintlify) | 13138 / 5 | 6489 / **0** | 6 | 3 | no |
| gofastmcp.com/apps/low-level (Mintlify) | 14797 / 11 | 5449 / **0** | 3 | 5 | no |
| simonwillison.net blog post | 9992 / 2 | 6275 / 2 | 0 | 0 | yes |
| github.com/adbar/trafilatura (README) | 23011 / 2 | 7535 / 2 | 0 | 0 | no |
| example.com (thin) | 172 | 132 non-ws chars → fallback | – | – | – |

Nav/sidebar is gone on all four (0 leaked nav anchor texts; GitHub bare-link share 50% → 6%). But on Mintlify docs
pages every code block is dropped: Tailwind classes on the code wrapper (`print:print-color-exact`,
`overflow-y-hidden!`) match trafilatura's `AUTHOR_DISCARD_XPATHS` / `OVERALL_DISCARD_XPATH`. Mintlify's
`<span data-as="p">` also produces duplicated sentences (e.g. "`fastmcp logout` before you run before you run").
Stripping all `class` attributes before `extract()` restores the Mintlify fences (0→5, 0→11) without nav leakage, but
loses the blog's 2 fences and mangles the GitHub README's code block — no generic fix found. Escalated to PA.

### [SWE] 2026-10-03 22:05 — Implementation (after orchestrator decision on the fork)

**Orchestrator decision (verbatim, human pre-authorised)**
> 1. Implement option B′ = targeted attribute stripping: before trafilatura.extract, remove `class` and `id` ONLY from each `<pre>` element and its ancestor chain (up to but not including <body>), leaving the rest of the DOM untouched. Re-measure on the same 4 rich pages (Mintlify ×2, simonwillison blog, GitHub README) with your probe table columns (chars / code blocks / duplicated sentences / glued links / nav leakage).
>    - ACCEPT B′ if: code blocks are preserved on all 4 pages (Mintlify 5 and 11 or close; blog 2; GitHub 2 un-mangled), no nav anchor text leaks, and no page gets worse than option A on duplicates/glued links.
>    - Otherwise REJECT B′ and ship option A (spec as written), and document "code blocks may be dropped on sites whose code wrappers match trafilatura's discard rules (e.g. Mintlify/Tailwind)" as a known limitation in ADR-013 §6 with the upgrade trigger (e.g. per-generator preprocessing or a different extractor).
>    Either way, put the measurement table for A vs B′ in your log.
> 2. Option D — always: if the extracted markdown has no H1, prepend `# {title}` using the page's own title; prefer the page <h1> text if present in the HTML, else the metadata title (site suffix like " - FastMCP" acceptable if no h1). Unit-test it.
> 3. Residual duplicated sentences on Mintlify (span[data-as=p]): do NOT add site-specific hacks; just record the measured count as a known limitation if it remains.
> 4. Fix the LATENT-placeholder promotion to merge metadata (`existing.metadata = {**existing.metadata, **doc.metadata}`) so metadata.extraction is recorded — confirmed, it's a bug otherwise; unit test it.
> 5. Run trafilatura.extract via `await asyncio.to_thread(...)`.

**A vs B′ (same saved Unlocker HTML, spec-exact `extract()` call) — B′ REJECTED, A shipped**

| page | A chars / code blocks / dup / glued / nav leaks | B′ chars / code blocks / dup / glued / nav leaks |
|---|---|---|
| gofastmcp `/deployment/prefect-horizon` | 6489 / 0 / 6 / 3 / none | 6807 / 5 / 6 / 3 / none |
| gofastmcp `/apps/low-level` | 5449 / 0 / 3 / 5 / none | 9808 / 11 / 0 / 0 / none |
| simonwillison.net one-shot-python-tools | 6275 / 2 / 0 / 0 / none | 5708 / **0** / 0 / 0 / none |
| github.com/adbar/trafilatura | 7535 / 2 / 0 / 0 / none | 7533 / **1, mangled (`\>>>`)** / 0 / 0 / none |

B′ fails the "blog 2; GitHub 2 un-mangled" condition. Keeping `<pre>`'s own attributes (ancestors only) gives identical numbers — the blog's code
recognition depends on ancestor classes.

**Files modified**
- `apps/memory/pyproject.toml` / `apps/memory/uv.lock` — `trafilatura>=2.3` MAIN dependency (comment: Prefect Managed per-run install runs the web leaf); forced bumps charset-normalizer 3.5.2, dateparser 1.4.3, urllib3 2.8.0.
- `apps/memory/src/tree/data/web/web_extract.py` (new) — `ExtractedPage`, `MIN_EXTRACTED_CHARS = 300`, `extract_main_content` (spec-exact call; `None` under 300 non-ws chars or empty HTML; metadata title fail-open; prepends `# {first page <h1> | metadata title}` when the markdown has no H1 — BeautifulSoup, the codebase's existing parser).
- `apps/memory/src/tree/data/web/web.py` — `fetch_and_extract_web`: HTML first → `asyncio.to_thread(extract_main_content, …)` → thin ⇒ WARNING + second `markdown` request; `metadata={"extraction": …}`; title = metadata title → first H1 → URL tail. `load_web_document` LATENT promotion merges `metadata`.
- `apps/memory/tests/unit/data/web/test_web_extract.py` (new), `tests/unit/data/web/test_web.py` (fetch tests rewritten for the two paths + LATENT metadata merge test).
- `docs/glossary.md` — **Clean step** "Distinct from **Main-content extraction** …" clause; **Main-content extraction** row: `include_comments=False`, H1 prepend, known limitation.
- `docs/adrs/013_horizon_scale_mcp_surface.md` §6 — implementation decision, known limitations, upgrade trigger (explicitly authorised by the orchestrator).
- `apps/memory/README.md` — `ingest_url` row.

**Tests**
- Unit: 4868 passed, 0 failed (`make memory-tests`, env-status local). The 299/300 boundary test was mutation-checked: counting all chars instead of non-whitespace fails `[299-thin]`. Integration: N/A (no suite).
- `make memory-format-check`, `make memory-lint-check`, `make pre-commit`: clean.

**Acceptance criteria**
- [x] main dependency + lock — `pyproject.toml`, `uv.lock`; suite runs with it.
- [x] headings/links/lists/tables kept, nav/aside/footer dropped, `None` under 300 — `test_web_extract.py::TestExtractMainContent::*` (incl. `test_threshold_counts_only_non_whitespace_chars[299-thin|300-kept]`).
- [x] HTML first; rich = 1 request + `trafilatura`; thin = `html`,`markdown` + WARNING + `brightdata_markdown` — `test_web.py::TestFetchAndExtractWeb::test_rich_page_makes_one_html_request`, `::test_thin_page_falls_back_to_brightdata_markdown`.
- [x] title chain — `::test_returns_document_with_web_source` (metadata), `::test_title_falls_back_to_first_h1_without_metadata_title`, `::test_title_falls_back_to_url_path_tail`, `TestDeriveTitle`.
- [x] `scrape_web` untouched (`web_scrape.py` diff empty; its tests unchanged and green).
- [x] README + glossary rows.
- [x] QA loop green.
- [x] Live (local) — below.

**Evidence — live `fetch_and_extract_web` (real Unlocker) vs Bright Data markdown (= what `scrape_web` returns)**
```
page                          BD md chars/fences/bare-link share   stored chars/fences/bare-link share  extraction
gofastmcp prefect-horizon     13138 / 5 / 0.056 (menu present)      6508 / 0 / 0.000                     trafilatura
gofastmcp apps/low-level      14797 / 11 / 0.035 (menu present)     5517 / 0 / 0.000                     trafilatura
simonwillison blog            9992 / 4 / 0.187                      6323 / 2 / 0.000                     trafilatura
github adbar/trafilatura      23008 / 2 / 0.497                     7608 / 2 / 0.057                     trafilatura
example.com                   172                                   172 (BD markdown)                    brightdata_markdown
WARNING tree.data.web.web Main-content extraction too thin for https://example.com/ — falling back to Bright Data markdown (second Unlocker request)
```
Stored heads: `# Prefect Horizon\n\n[Prefect Horizon](…)`, `# Custom HTML Apps\n\n[Interactive Tools](…)`, `## Building Python tools…`,
`# Trafilatura: Discover Web Content, Extract Text and Metadata\n\n```…`.

**Evidence — e2e (local Prefect serve from this worktree, `make memory-run-pipeline MODE=online`, rag mode, user paul.iusztin@example.com)**
```
$ make memory-run-pipeline MODE=online SOURCE="https://gofastmcp.com/apps/low-level"   → Done. Flow completed successfully.
clean_and_chunk: doc_id=6ac150c407a863771f49e54c n_parents=1 n_children=8 ; load_rag_rows: rows_written=10
mongosh: title "Custom HTML Apps - FastMCP" | metadata {"extraction":"trafilatura"} | content 5517 chars, starts "# Custom HTML Apps"
         69 non-empty lines, 0 bare [text](url) lines (0.0 %)
memory rows: document 1, chunk/parent 1, chunk/child 8 — every chunk 0 bare-link lines, e.g.
  p0:c6 "makes API calls, you need to declare those domains in the CSP configuration. See [Security](https://…"
$ make memory-search QUERY="declare API domains in the CSP configuration of a custom HTML MCP app"
  [0.033] Custom HTML Apps - FastMCP — Custom HTML Apps   (top parent = the article)
$ make memory-run-pipeline MODE=online SOURCE="https://example.org/"   → Done. Flow completed successfully.
  WARNING | tree.data.web.web - Main-content extraction too thin for https://example.org/ — falling back to Bright Data markdown (second Unlocker request)
  mongosh: metadata {"extraction":"brightdata_markdown"}, 3 memory rows
Cleanup: deleted both documents (2) + their memory rows (10 + 3); serve process stopped by PID.
```

**Known limitations (recorded in ADR-013 §6)**
- Mintlify code blocks dropped (prefect-horizon 5→0, low-level 11→0); the blog keeps 2 of its 4 code blocks. So the story query
  "ontoolresult content block" no longer surfaces the page: 1 of 3 `ontoolresult` mentions survives; the other two sat in code blocks.
- Mintlify duplicated sentences: 6 (prefect-horizon) and 3 (low-level), plus glued link tails (`…)wrap`) and inline-code tail
  repeats (e.g. "No need to set it manually.\n. No need to set it manually.") — no site-specific hacks, per decision.
- H1 detection is two-sided: on simonwillison.net trafilatura emits 2 of 4 code blocks UNFENCED, so `# requires-python = …` comment lines satisfy the "has an H1" check (and look like headings to the chunker); had the prepend fired, the first page `<h1>` there is the site header "Simon Willison’s Weblog". Both noted, not patched (a `<main>`/`<article>`-scoped h1 would be a new heuristic — PA).
- Fallback-path title is still `_derive_title` on Bright Data markdown (e.g. `Example.Org`, not the metadata "Example Domain"), as specified.

**Notes**
- Prefect Managed (prod) signal still pending: record the first post-merge nightly / offline run id whose web leaf imports trafilatura.
- `trafilatura` is a `[project.dependencies]` entry (not a dev group/extra), so Horizon's / Prefect Managed `pip install ./apps/memory` gets it.

### [SWE] 2026-10-03 22:20 — Fixes: best-of-two extraction (supersedes the "A shipped" limitations above)

**Orchestrator decision #2 (verbatim)**
> Good work — one refinement before the Tester (orchestrator decision; same tree, DO NOT commit). Shipping A loses every Mintlify code block, which is too costly for a technical memory. Your table shows A and B′ fail on disjoint pages, so implement a generic best-of-two rule, no site-specific logic:
> - Run A (spec-exact). If the HTML contains ≥1 `<pre>` and A's markdown has fewer fenced code blocks than the number of `<pre>` elements, ALSO run B′ (strip class/id from each <pre> and its ancestor chain) on a copy of the HTML, and keep whichever result has MORE fenced code blocks (tie → A). Both run inside the same to_thread call; no extra Bright Data request.
> - Record which variant won in metadata, e.g. metadata.extraction stays "trafilatura" and add metadata.extraction_variant: "default" | "pre_unwrapped" (or fold into one field if cleaner — keep the glossary/ADR consistent).
> - Unit tests: a Mintlify-like fixture (code wrapper with discard-matching classes) → B′ wins with code kept; a blog-like fixture where B′ loses code → A wins; no-<pre> page → B′ never runs.
> - Re-measure live on the same 4 pages + example.com and put the table in the log (expect Mintlify 5/11 code blocks, blog 2, GitHub 2 un-mangled). Re-run your Story-1 query check (search for "ontoolresult content block" after a local re-ingest of low-level) if cheap.
> - Update ADR-013 §6 / glossary Main-content extraction row: the rule and the remaining known limitations (Mintlify duplicated sentences/glued links, fenceless code comment lines can look like H1). Paste my decision text from this message and the previous one verbatim into the task log (ADR can stay condensed).
> - Fallback title on the brightdata_markdown path: use the page's metadata/<title> if Bright Data markdown has an H1 or it's cheaply available; otherwise keep the spec's URL-tail — your call, note it.
> Re-run the full local QA loop (format-fix, lint-fix, format-check, lint-check, pre-commit, memory-tests env-status local), clean up any local e2e documents you create, stop processes by PID, append a SWE log entry, and report back.

(Decision #1 is quoted verbatim in the previous entry.)

**What changed**
- `web_extract.py`: `_best_markdown` runs the spec-exact call. If `"<pre"` is in the HTML and the fenced-block count is below the number of `<pre>` elements, it strips `class`/`id` from each `<pre>` and its ancestors (stopping at `body`/`html`) in a BeautifulSoup copy and runs again. MORE fences wins; a tie keeps `default`. `ExtractedPage.variant` holds `default` | `pre_unwrapped`. `page_title(html)` is now public.
- `web.py`: the trafilatura path writes `metadata = {"extraction": "trafilatura", "extraction_variant": <variant>}`.
- **Fallback title (my call):** the brightdata_markdown path now also uses the page metadata title, `await asyncio.to_thread(page_title, html)`. The HTML is already fetched, so there is no extra request. Title is therefore metadata title → markdown H1 → URL tail on BOTH paths, matching the acceptance criterion literally. Example: example.com is now "Example Domain" instead of "Example.Com".
- Tests:
  - `TestPreUnwrappedVariant`:
    - Mintlify-like fixture (gofastmcp's Tailwind classes) → `pre_unwrapped` with the code fence kept and no nav.
    - Blog-like fixture (`.highlight`, `#primary`, `.entry`) → the second run happens (2 `extract` calls) and `default` wins with the code kept.
    - No `<pre>` → 1 `extract` call.
    - Tie → `default`.
    - The second run sees a stripped COPY, with `<nav>` intact.
  - `TestPageTitle`.
  - `test_fallback_title_prefers_page_metadata_title`.
  - The metadata assertions are now exact dicts.
- Docs: ADR-013 §6, the glossary **Main-content extraction** row and the README `ingest_url` row now describe the rule and the remaining limitations.

**Live re-measure (real Unlocker, `fetch_and_extract_web`)**

| page | variant | code blocks (BD md → stored) | chars | dup | glued | bare-link share | escaped `\>>>` |
|---|---|---|---|---|---|---|---|
| gofastmcp `/deployment/prefect-horizon` (5 `<pre>`) | pre_unwrapped | 5 → 4 | 6807 | 6 | 3 | 0.0 | 0 |
| gofastmcp `/apps/low-level` (11 `<pre>`) | pre_unwrapped | 11 → 11 | 9852 | 0 | 0 | 0.0 | 0 |
| simonwillison.net post (8 `<pre>`) | default (B′ ran, 0 fences, lost) | 4 → 2 | 6323 | 0 | 0 | 0.0 | 0 |
| github.com/adbar/trafilatura (2 `<pre>`) | default (B′ not run) | 2 → 2 | 7608 | 0 | 0 | 0.057 | 0 |
| example.com | — (`brightdata_markdown`, WARNING) | 0 | 172 | 0 | 0 | 0.0 | 0 |

Heads: `Deployment\n\n# Prefect Horizon\n\nThe MCP platform from the FastMCP team…` and `Apps\n\n# Custom HTML Apps\n\nBuild apps with…`. The unwrap run keeps Mintlify's one-word breadcrumb line above the H1.

**e2e (local serve from this worktree, rag mode)**
```
$ make memory-run-pipeline MODE=online SOURCE="https://gofastmcp.com/apps/low-level"  → Done. Flow completed successfully.
clean_and_chunk: n_parents=1 n_children=18 ; load_rag_rows: rows_written=20
mongosh: metadata {"extraction":"trafilatura","extraction_variant":"pre_unwrapped"} | 9852 chars | 11 fences | 0/208 bare-link lines
         18 children, 0 bare-link lines; 3 children contain "ontoolresult" (c7, c8, c14)
$ make memory-search QUERY="ontoolresult content block"
  [0.016] Custom HTML Apps - FastMCP   ← top parent (Story 1 now holds)
Cleanup: 1 document + 20 memory rows deleted; serve tree (46397, 46403, 46410, 46411) stopped by PID.
```

**Tests / QA:** `make memory-tests` 4876 passed (env-status local); `memory-format-check`, `memory-lint-check` and `pre-commit` clean.

**Remaining known limitations** (ADR-013 §6)
- Mintlify duplicated sentences and glued links: 6 and 3 on prefect-horizon (low-level is now 0/0).
- prefect-horizon keeps 4 of its 5 code blocks.
- The blog keeps 2 of 4. Its other 2 come out unfenced, so `# requires-python` comment lines satisfy the H1 check. Had the prepend fired, the first page `<h1>` there is the site header.
- Pages with ≥1 `<pre>` that trafilatura never fences pay a second CPU-only run.
- The Prefect Managed post-merge run id is still to be recorded.

### [Tester] 2026-10-03 23:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`memory-format-check` 333 files clean, `memory-lint-check`, `pre-commit` all Passed)
- Unit tests: 4876 passed / 0 failed (`make memory-tests`, env-status local, 0 warnings). Integration: N/A (no suite, per AGENTS.md)
- `uv lock --check` clean; `scrape_web.py`, `web_scraper_api.py`, `web_unlocker.py` diffs empty.

**E2E adversarial pass** (real Unlocker; documents built via `fetch_and_extract_web` against a scratch DB `qa182_scratch`, dropped afterwards; nothing written to `tree`)
- Happy path, 5 live pages in one `asyncio.gather` (batch-style concurrency): prefect-horizon `pre_unwrapped` 4 fences / 6807 chars / 0 bare-link lines of 87; low-level `pre_unwrapped` 11 fences / 9852 chars / 0 of 208; github adbar/trafilatura `default` 2 fences / 7608; react.dev `pre_unwrapped` 4 fences / 1 of 101 bare; reddit `default` (10/20 bare links, thin-ish but stored). All match the SWE's table. (PASS)
- Break 1, non-HTML (PDF `arxiv.org/pdf/1706.03762`, 2.1 MB): trafilatura -> None -> WARNING + 2nd request -> `brightdata_markdown`, no crash. Title/content are binary garbage, but identical to the pre-change behaviour (Bright Data returns raw PDF for markdown too; `_derive_title` on that body gave the same garbage). Pre-existing, not a regression. (PASS with note)
- Break 2, error page (`httpbin.org/status/404`): empty body -> fallback path, `brightdata_markdown`, empty content, title "404"; no crash. (PASS)
- Break 3, thin page (example.com): fallback + WARNING, `metadata == {"extraction": "brightdata_markdown"}`, title "Example Domain". (PASS)
- Break 4, boundary/malformed synthetic HTML via `extract_main_content` (all returned `None` or a page, never raised): empty, whitespace-only, `\x00`-garbage, JS-only SPA shell (+`<noscript>`), script-only article, null bytes in text, emoji/RTL/CJK (preserved), unclosed `<pre>`, `<PRE>` uppercase, empty `<pre>`s, ascii-art `<pre>`, 50 000-deep `<div>` nesting (no RecursionError, falls back), mojibake input. (PASS)
- Break 5, size/performance: 6.7 MB HTML with 1 `<pre>` -> 4.7 s total (both runs, in `to_thread`), 0.6 s for 3000 unfenced `<pre>`s (second run pays CPU only). Acceptable, off the event loop. (PASS)
- Break 6, hostile input: `<script>alert(1)</script>` dropped; `javascript:` link text kept as plain; title `# evil [x](y)` yields `# # evil [x](y)` prepend (cosmetic, see notes). (PASS with note)
- Routing: YouTube / Substack handlers in `online_pipeline.py` never reach the web leaf (routing unchanged; online/offline tests green). `search_web(ingest=true)` -> `extract_batch` -> `fetch_and_extract_web` (web_pipeline.py:48/91) is the same function, so same extraction + fallback.

**Acceptance criteria**
- [x] PASS — trafilatura>=2.3 main dependency, lock updated, suite runs — `pyproject.toml` `[project.dependencies]`, `uv lock --check` clean, 4876 passed.
- [x] PASS — `extract_main_content` keeps headings/links/lists/tables, drops nav/aside/footer, `None` < 300 non-ws chars — `test_web_extract.py` (17 tests incl. 299/300 boundary); live: 0 nav leakage on 5 pages.
- [x] PASS — HTML first; rich = 1 request + `trafilatura`; thin = html, markdown, WARNING, `brightdata_markdown` — `test_web.py::TestFetchAndExtractWeb`; live WARNING text matches spec exactly (example.com, PDF, 404).
- [x] PASS — title chain metadata -> H1 -> URL tail — live: "Prefect Horizon - FastMCP", "Example Domain"; unit tests.
- [x] PASS — `scrape_web` unchanged — empty diff on web_scrape.py; tests green.
- [x] PASS — README row + glossary rows — read the diff: README `ingest_url` row, glossary **Main-content extraction** + **Clean step** "distinct from"; ADR-013 §6 text (rule, `extraction_variant`, H1 prepend, title on both paths, LATENT metadata merge, limitations) all match `web_extract.py`/`web.py`.
- [x] PASS — format/lint/pre-commit/tests green.
- [x] PASS — Live (local): SWE's e2e evidence (18 children, 0 bare-link lines, Story-1 query top parent) is consistent with my independent live re-measure above; I did not repeat the Prefect serve run and created no Document/memory rows (scratch DB dropped, 0 rows with `metadata.extraction` in `tree.documents`).

**Other issues found (non-blocking)**
- trafilatura logs ERROR-level lines (`parsed tree length: 0, wrong data type or not valid HTML`, `empty HTML tree`) on every thin/non-HTML page just before the project's WARNING — log noise, could be silenced by setting the `trafilatura` logger to CRITICAL in the helper; follow-up.
- `page_title` is unbounded and unsanitised: a page title starting with `#` yields a doubled `# # …` H1 prepend, and a binary body (PDF) yields garbage titles (same as before this change).
- Non-HTML URLs (PDF) now cost two multi-MB Unlocker fetches instead of one; a content-type/`%PDF` sniff would save one. Follow-up.
- Pre-existing and unrelated: `uv pip check` flags litellm/beanie vs the local Python 3.14.
- Prefect Managed post-merge run id is still outstanding (recorded as pending by SWE).

**VERDICT: PASS**

### [SWE] 2026-10-03 22:45 — Fixes: Tester follow-ups (log noise, heading sanitising)

- `web_extract.py`: `logging.getLogger("trafilatura").setLevel(logging.CRITICAL)` at import, so the "empty HTML tree" and "parsed tree length: 0" ERROR lines no longer surface. Our own WARNING is unchanged and still covers thin pages.
- `web_extract.py`: `_as_heading` strips leading `#` and whitespace from the prepended heading (`# evil [x](y)` → `# evil [x](y)`, not `# # …`) and caps it at `MAX_HEADING_CHARS = 300`. A title of only `#` characters prepends nothing.
- Tests:
  - `TestPrependedHeadingSanitised`: 3 hash variants, the 300 cap, the hashes-only title.
  - `TestTrafilaturaLogNoise`: red before the logger change, green after.
- QA (env-status local): `make memory-tests` 4882 passed; `memory-format-check`, `memory-lint-check` and `pre-commit` clean.
- Still pending post-merge: the Prefect Managed run id of the first nightly / offline run whose web leaf imports trafilatura (Scope 7).
