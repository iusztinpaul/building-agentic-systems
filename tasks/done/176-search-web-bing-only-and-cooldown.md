---
id: 176-search-web-bing-only-and-cooldown
status: done
feature: horizon-mcp-fixes
---

# `search_web` is Bing-only: no `engine` parameter, a Bing `b_algo` parser that decodes `/ck/a` redirects, and Bright Data's cooldown body retried once then answered as a retryable envelope

Tags: `web`, `mcp`, `brightdata`, `serp`, `cli`, `docs`
Depends on: —
Blocks: —
Implements: ADR-013 §1 (amends ADR-008 §2's `search_web` surface and the **Tool error envelope** row)

## Problem

`search_web` returns `[]` for every query, locally and on Horizon. Verified live: Google now wraps every
organic link in an encrypted `/goto?url=<token>` redirect in ALL Bright Data formats (raw HTML,
`data_format: markdown`, `format: json`); `brd_json=1` collapses to `{general, input}`; neither the SERP
zone nor the Unlocker zone resolves `/goto` ("this endpoint is not supported"). `_parse_serp_html`
(`apps/memory/src/tree/data/web/web_serp.py`) keeps only `h3`-inside-`<a>` with an external href → 0
results. Bing works: results are `li.b_algo h2 a`; hrefs are either direct or
`https://www.bing.com/ck/a?...&u=a1<base64url>`, which decodes to the real URL (9 real URLs for "beanie
odm mongodb async"). Separately, Bright Data sometimes answers HTTP 200 with the plain-text body
`This query recently failed and cannot be attempted at this time. Please try again later, after a
minimum of 15 seconds.` — today that silently becomes `[]`.

## Scope

**Human decisions (final):** `search_web` is Bing-only — the `engine` parameter is DROPPED from the MCP
tool (Google/Yandex code paths removed; `country`/`language` keep mapping to Bing's `cc`/`setLang`);
the cooldown body is detected, waited out (~15 s) and retried ONCE in-tool, and a second cooldown answers
a retryable **Tool error envelope** instead of `[]`; a genuinely empty SERP still answers `[]`.

1. **`apps/memory/src/tree/data/web/web_serp.py` — Bing only.**
   - Delete `SearchEngine`, the `engine` keyword of `search` (new signature:
     `search(query, *, num_results=10, country=None, language=None, timeout_seconds=30.0)`), the
     Google/Yandex branches of `_build_serp_url` (Bing: `q`, `cc`, `setLang`, `first=offset+1`), the
     Google host list in `_NON_ORGANIC_HOST_SUFFIXES` and the Yandex entry of `_NO_RESULTS_INDICATORS`.
     Rewrite the module docstring (the "cli_serp does not support brd_json" paragraph stays as history;
     add the dated Google `/goto` finding and why Bing).
   - `_parse_serp_html` → a Bing parser: every `li.b_algo` → title = `h2 > a` text, href = that `a[href]`;
     snippet = the `li`'s `.b_caption p` text (fallback: today's container walk), capped at
     `_SNIPPET_MAX_CHARS`; duplicates dropped on the RESOLVED url; rank continues across pages.
   - `_resolve_bing_href(href) -> str | None` (pure): a direct `http(s)://` href whose host is not
     `bing.com` / `*.bing.com` / `microsoft.com` → itself; a `https://www.bing.com/ck/a?…&u=a1<token>` →
     take `u`, strip the leading `a1`, base64url-decode with `=` padding restored, keep it only if the
     result starts with `http://` / `https://`; anything else (images/videos/maps chrome, ads, `javascript:`)
     → `None` (skipped). `_is_organic_url` is folded into it.
   - `_NO_RESULTS_INDICATORS` = `("there are no results for", "no results found", "did not match any")`.
   - **Cooldown:** `_COOLDOWN_MARKER = "This query recently failed and cannot be attempted at this time"`,
     `_COOLDOWN_RETRY_SECONDS = 15.0`, `_is_cooldown_body(text) -> bool` (case-insensitive substring on
     the first 300 chars). In the page loop, a 2xx whose body is a cooldown: WARNING log (query preview,
     page offset), `await asyncio.sleep(_COOLDOWN_RETRY_SECONDS)`, re-POST the same page ONCE; a second
     cooldown raises `BrightDataCooldownError` (subclass of `BrightDataRequestError`, message = Bright
     Data's sentence). `asyncio.sleep` is called through the module (`asyncio.sleep`) so tests patch it.
2. **`apps/memory/src/tree/mcp/tools.py` — `search_web`.** Drop the `engine` parameter and the `"engine"`
   key of the answer (`{"query", "results"[, "ingest"]}`); the docstring says Bing (one sentence: "Bing
   organic results via Bright Data's SERP API — Google is not offered: its organic links are opaque
   `/goto` tokens the SERP zone cannot resolve (2026-10)"); catch `BrightDataCooldownError` BEFORE
   `BrightDataRequestError` → `tool_error("fetch_failed", f"Bright Data SERP is cooling down this query —
   retry in 15 s or more: {exc}", retryable=True)`. The `ERROR_CONTRACT` sentence stays verbatim
   (`test_error_envelope.py` anchor); no new `error`/`detail` dict literals (AST guard, exempt count 2).
3. **CLI + Make + docs.** `apps/memory/scripts/search_web.py`: remove `--engine` and the `engine` key of
   its printed JSON; `apps/memory/Makefile` `search-web` help: drop `[ENGINE=google]`; `apps/memory/README.md`:
   the `search_web` row says "Bing via Bright Data SERP" and the JSON example loses `"engine": "google"`;
   `.agents/skills/bright-data-best-practices/references/serp-api.md`: a dated note (Google organic links
   are `/goto` tokens in every format; this project queries Bing) if that file documents engine choice;
   `.agents/skills/tree-memory/*.md`: no `engine` mention today — verify, change nothing otherwise.
4. **Tests** (`tests/unit/data/web/test_web_serp.py`, `tests/unit/mcp/test_search_web.py`,
   `tests/unit/mcp/test_error_envelope.py`; `/squid-testing-python` conventions, fixtures inline — there is
   no web fixtures dir):
   - `TestSearchEngines` → `TestBingUrl` (`q`, `cc`, `setLang`, `first` = 1 then 11; `brd_json` absent).
   - Parser: inline Bing HTML with (a) a `b_algo` with a direct href + `.b_caption p` snippet, (b) a
     `b_algo` whose href is `/ck/a?…&u=a1<base64url of https://example.com/x>` → url
     `https://example.com/x`, (c) a `bing.com/images/...` chrome link and an `h2 a` outside `li.b_algo`
     → both skipped, (d) two results resolving to the same URL → one; "There are no results for" page →
     `[]` + INFO; unexpected body → `[]` + WARNING (existing tests, re-pointed).
   - `_resolve_bing_href`: direct, `ck/a` with/without padding, `ck/a` decoding to a non-http string →
     `None`, `javascript:` → `None`.
   - Cooldown: `mocker.patch("tree.data.web.web_serp.asyncio.sleep", AsyncMock())`; cooldown then a
     result page → results returned, `sleep` awaited once with `15.0`, two POSTs; cooldown twice →
     `BrightDataCooldownError`, two POSTs, one sleep.
   - Tool: `search_web` has no `engine` in its schema (inspect the registered `FunctionTool.parameters`);
     cooldown → `{"error_type": "fetch_failed", "retryable": true}` and the message contains "15 s";
     the envelope table in `test_error_envelope.py` gains that row.
   - `scripts/search_web.py` CLI test (CliRunner): `--engine` is an unknown option.
5. **Live verification** (LOCAL env, `make env-status` → local; evidence pasted into `## Log`):
   `make memory-search-web QUERY="beanie odm mongodb async" NUM_RESULTS=5` → ≥ 5 results, every `url`
   external (`bing.com` / `/goto` absent), snippets non-empty for most. Repeat the same query twice within
   15 s to try to provoke the cooldown; if it appears, the log shows the WARNING + the retry.
   Horizon (post-merge, scheduled by the orchestrator): `search_web` via `tree.mcp.client.get_cloud_client`
   → the same shape; paste the first result's `url` into the Log.

## Acceptance criteria

- [x] `tree.data.web.web_serp.search` has no `engine` parameter; `SearchEngine`, the Google/Yandex URL
      builders and the Google host list are gone (grep: no `google.com/search`, no `yandex`).
- [x] The parser returns one `SearchResult` per `li.b_algo` with a resolvable href; `/ck/a?…&u=a1…` hrefs
      decode to the real URL; Bing chrome links and duplicates are skipped; ranks are 1-based and continue
      across pages.
- [x] A 2xx cooldown body is retried once after `_COOLDOWN_RETRY_SECONDS` (15.0); a second cooldown raises
      `BrightDataCooldownError`; a real empty SERP returns `[]` (INFO), an unrecognised body `[]` (WARNING).
- [x] MCP `search_web` schema has no `engine`; its answer has no `engine` key; a cooldown answers
      `{"error_type": "fetch_failed", "retryable": true, "message": …}`; `ERROR_CONTRACT` still verbatim in
      the docstring; `test_error_envelope.py` AST guard green.
- [x] `scripts/search_web.py`, the Makefile help, `apps/memory/README.md` and the Bright Data skill reference
      carry no `engine`/Google wording for `search_web`.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [x] Live (local): the query above returns ≥ 5 external URLs; evidence in the Log.
- [ ] [HUMAN] Live (Horizon, post-merge): the same query via the cloud client returns ≥ 5 external URLs;
      evidence in the Log.

## User Stories

### Story: The assistant searches the web and gets real pages
1. The model calls `search_web(query="beanie odm mongodb async", num_results=5)` on `tree-memory-local`.
2. The server POSTs ONE Bright Data SERP request for `https://www.bing.com/search?q=beanie+odm+mongodb+async&first=1`.
3. The answer is `{"query": …, "results": [{rank: 1, title: "Beanie - Asynchronous Python ODM for MongoDB", url: "https://beanie-odm.dev/", snippet: "…"}, …]}` — five entries, no `bing.com/ck` URL.

### Story: Bright Data cools the query down, the tool waits it out
1. The model calls `search_web(query="prefect horizon fastmcp")` twice within 15 s.
2. The second call's first page answers HTTP 200 with the cooldown sentence.
3. The server logs `WARNING … SERP cooldown for query=… — retrying once in 15.0s`, sleeps, re-requests and returns the results. The model sees ordinary results ~15 s late.

### Story: The cooldown persists, the model gets a retryable envelope
1. Both attempts answer the cooldown body.
2. The tool answers `{"error_type": "fetch_failed", "retryable": true, "message": "Bright Data SERP is cooling down this query — retry in 15 s or more: This query recently failed…"}`.
3. The tree-memory skill's rule applies: the model tells the user the web search is cooling down and retries once later, instead of saying "no results".

### Story: An old client still sends `engine`
1. A client calls `search_web(query="x", engine="google")`.
2. FastMCP rejects the unknown argument — `ToolError: 1 validation error for call[search_web] engine: Unexpected keyword argument` (verified in-process on fastmcp 3.2.0) — the tool never reaches Bright Data; the tool's schema shows `query, num_results, country, language, ingest, ingest_top_k, ingest_urls` only.

### Story: The operator runs the CLI
1. `make memory-search-web QUERY="beanie odm mongodb async" NUM_RESULTS=5`.
2. The log prints `Got 5 result(s) for query='beanie odm mongodb async':` and the five titles/URLs; `make memory-search-web … ENGINE=google` is ignored by Make and the script accepts no `--engine`.

---

Blocked by: (none)

## Log

### [PA] 2026-10-03 17:40 — Grooming

**Summary**
`search_web` becomes Bing-only (the `engine` parameter is dropped), with a `li.b_algo` parser that decodes
Bing's `/ck/a` redirects, and Bright Data's 200-OK cooldown body is retried once after 15 s then answered
as a retryable `fetch_failed` envelope rather than `[]`.

**Key decisions**
- Cooldown code is `fetch_failed` (retryable), NOT the spec's example `search_unavailable`: ADR-008 §2 reserves
  that code for the memory retrieval legs, and `tools.py` already warns against confusing the two.
- The `"engine"` answer key goes with the parameter: a constant carries no information.
- The retry sleep is a module constant and `asyncio.sleep` is patched in tests — no 15 s unit test.
- The parser is Bing-specific on purpose (one engine, one selector set); the engine-agnostic `h3`-in-`a`
  heuristic is what broke.

**Dependencies**
- None.

**User stories**
- 5 stories: real results, cooldown waited out, cooldown envelope, stale `engine` client, operator CLI.

**Open questions** (defaults chosen)
- `fetch_failed` vs `search_unavailable` for the cooldown (see Feature Plan open question 1).

Ready for implementation.

### [SWE] 2026-10-03 18:40 — Implementation

**Files modified**
- `apps/memory/src/tree/data/web/web_serp.py` — Bing only: `SearchEngine`/`engine`/Google+Yandex builders and host list gone; `li.b_algo` parser; `_resolve_bing_href` (direct href or `/ck/a?…&u=a1<base64url>` decode, padding restored); `BrightDataCooldownError(BrightDataRequestError)`; `_COOLDOWN_MARKER` / `_COOLDOWN_RETRY_SECONDS = 15.0` / `_is_cooldown_body` (first 300 chars, case-insensitive); `_fetch_serp_page` waits out ONE cooldown via `asyncio.sleep` and re-POSTs the same page; docstring gains the dated Google `/goto` finding.
- `apps/memory/src/tree/mcp/tools.py` — `search_web` drops `engine` (param + answer key), docstring carries the Bing sentence, `except BrightDataCooldownError` before `BrightDataRequestError` → `tool_error("fetch_failed", "Bright Data SERP is cooling down this query — retry in 15 s or more: …", retryable=True)`. `ERROR_CONTRACT` sentence untouched; no new dict literals.
- `apps/memory/scripts/search_web.py` — `--engine` and the `engine` JSON key removed.
- `apps/memory/Makefile` — `search-web` help loses `[ENGINE=google]` AND the `$(if $(ENGINE),--engine …)` forwarding line (so `ENGINE=google` is ignored by Make, not forwarded into a CLI usage error).
- `apps/memory/README.md` — `search_web` row says Bing via Bright Data SERP (+ cooldown behaviour); JSON example loses `"engine": "google"`.
- `.agents/skills/bright-data-best-practices/references/serp-api.md` — dated 2026-10 note in Overview (Google `/goto` in every format; this project queries Bing; `/ck/a` decoding; the cooldown sentence). `.claude/skills` is a symlink to `.agents/skills` — one copy.
- `.agents/skills/tree-memory/*.md` — grepped, no `engine` mention; unchanged.
- `apps/memory/tests/unit/data/web/test_web_serp.py` — SERP stub HTML is now Bing-shaped (`li.b_algo > h2 > a` + `.b_caption p`); `TestSearchEngines` → `TestBingUrl`; new `TestBingParser`, `TestResolveBingHref`, `TestCooldown`; empty/unexpected-shape tests re-pointed (Bing "There are no results for", `query=` instead of `engine=`); pagination asserts `first=1` then `first=11` and ranks 1..15.
- `apps/memory/tests/unit/mcp/test_search_web.py` — no `engine` kwarg/key; cooldown → exact envelope; registered `FunctionTool.parameters` has no `engine`; CLI `--engine` → exit 2 "No such option"; printed CLI JSON keys = `{query, results}`.
- `apps/memory/tests/unit/mcp/test_error_envelope.py` — `TestMapping` gains the cooldown row (via `_envelope()`, so the three-key shape is asserted); AST guard still at exempt count 2.

**Tests**
- Unit: 4741 passed, 0 failing — `make memory-tests` (env-status: local).
- Integration: N/A — the project has no integration suite (AGENTS.md); live run below instead.

**Acceptance criteria**
- [x] No `engine` / `SearchEngine` / Google+Yandex builders — `test_web_serp.py::TestBingUrl::test_search_has_no_engine_parameter`; `grep -rni "google.com/search\|yandex\|SearchEngine" apps/memory/src apps/memory/scripts` → no matches.
- [x] One result per resolvable `li.b_algo`, `/ck/a` decoded, chrome + duplicates skipped, ranks continue — `TestBingParser::*`, `TestResolveBingHref::*`, `TestSearchPagination::test_paginates_when_num_results_exceeds_page_size`.
- [x] Cooldown retried once after 15.0 s, second cooldown raises; empty SERP → `[]` INFO, unrecognised → `[]` WARNING — `TestCooldown::*`, `TestSearchEmptyResultLogging::*`.
- [x] MCP schema/answer without `engine`, cooldown envelope, `ERROR_CONTRACT` verbatim, AST guard green — `test_search_web.py::TestSearchWebMcpTool::test_schema_has_no_engine_parameter`, `::test_cooldown_error_is_a_retryable_fetch_failed`, `test_error_envelope.py::TestMapping::test_search_web_reports_a_serp_cooldown_as_retryable_fetch_failed`, `::test_every_tool_docstring_states_the_error_contract`, the AST guard.
- [x] CLI / Makefile / README / skill reference carry no `engine`/Google wording for `search_web` — `TestSearchWebCli::test_engine_is_an_unknown_option`, `::test_printed_payload_has_no_engine_key`; manual diff review.
- [x] format-check, lint-check, pre-commit, memory-tests green — output below.
- [x] Live (local): ≥ 5 external URLs — evidence below.
- [ ] [HUMAN] Live (Horizon, post-merge) — scheduled by the orchestrator.

**Evidence**
```
$ make memory-format-check   → 329 files already formatted
$ make memory-lint-check     → All checks passed!
$ make pre-commit            → prettier / ruff check / ruff format / biome check: Passed
$ make env-status            → Env target: local (.env)
$ make memory-tests          → ============ 4741 passed in 56.97s ============

$ make memory-search-web QUERY="beanie odm mongodb async" NUM_RESULTS=5
Running Bing SERP query via Bright Data (query=beanie odm mongodb async)
Got 5 result(s) for query='beanie odm mongodb async':
[1] BeanieODM/beanie: Asynchronous Python ODM for MongoDB - GitHub — https://github.com/BeanieODM/beanie
[2] Beanie Documentation — https://beanie-odm.dev/
[3] mnivedhan/beanie-odm: Asynchronous Python ODM for MongoDB — https://github.com/mnivedhan/beanie-odm
[4] Beanie: async Python ODM for MongoDB | Hysen Labs — https://hysenlabs.com/en/projects/beanieodm-beanie
[5] How to Use Beanie ODM for Async MongoDB with Python — https://oneuptime.com/blog/post/2026-03-31-mongodb-beanie-odm-async-python/view
{"query": "beanie odm mongodb async", "results": [ … 5 entries, every snippet non-empty (126–132 chars) … ]}
(the concurrent twin run also returned 5 external URLs, incl. https://pypi.org/project/beanie/)

# In-process MCP (live Bright Data, uv --directory apps/memory run --env-file ../../.env):
schema: ['country', 'ingest', 'ingest_top_k', 'ingest_urls', 'language', 'num_results', 'query']
stale engine -> ValidationError: 1 validation error for call[search_web] engine  Unexpected keyword argument
keys: ['query', 'results'] n= 5
1 https://github.com/BeanieODM/beanie | snippet chars: 132
2 https://beanie-odm.dev/ | snippet chars: 128
...
```

**Notes**
- Cooldown NOT reproduced live: two concurrent runs of the beanie query, then three concurrent runs of "prefect horizon fastmcp" (2× 3 results, 1× `httpx.ReadTimeout` at 30 s) and an immediate re-run (3 results). No cooldown body appeared, so the WARNING + retry path is verified by unit tests only (`TestCooldown`).
- Live HTML finding: in Bing's image-caption layout (`b_imgcap_*`), the snippet `p.b_lineclamp2` sits beside the title and `.b_caption.b_rich` is empty. Snippet = `.b_caption p` → any `p` in the `li` → the `li`'s own text minus title. The fallback is bounded to the `li` instead of today's 6-level parent walk, which from `h2 > a` would climb into neighbouring results (pinned by `test_snippet_falls_back_to_the_result_text_minus_title`).
- `_resolve_bing_href`: host filter (`bing.com`, `*.bing.com`, apex `microsoft.com` only — `learn.microsoft.com` stays) applies to direct hrefs; a decoded `/ck/a` target is kept iff it is `http(s)://` (per spec).
- Trade-off: duplicates are dropped within a page, not across pages, and pagination still stops on a page with < 10 results. A Bing page that renders < 10 `b_algo` (rich blocks) ends pagination early for `num_results > 10`. Upgrade path: pass the seen-URL set across pages and stop on 0 new results.
- Adjacent, out of scope: `scripts/search_web.py` doesn't catch `httpx.TimeoutException`/`ConnectError`, so a Bright Data read timeout prints a traceback (exit 1) instead of a one-line error. The MCP tool already maps these to `network_error`. Candidate follow-up task.
- tech-docs: context7 is not connected in this session; I used the repo's `serp-api.md` Bing parameter table (`q`/`cc`/`setLang`/`first`) plus a live Bright Data probe of the Bing HTML (10 `li.b_algo`, all `/ck/a` hrefs; 2 `h2 a` outside `b_algo` — a top block and a `bing.com/aclk` ad).

### [Tester] 2026-10-03 19:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (329 files formatted; ruff "All checks passed!"; prettier/ruff/biome Passed)
- Unit tests: 4741 passed / 0 failed (`make memory-tests`, env-status: local)
- Integration tests: N/A (project has none, per AGENTS.md)
- Warnings: 0 in the pytest summary

**E2E adversarial pass** (live Bright Data unless stated; scripts in the session scratchpad)
- Happy path: `search("python asyncio tutorial", num_results=5)` and the SWE's beanie query -> 5 external results, no `bing.com` / `/goto` URLs, snippets non-empty (PASS)
- Non-English + geo: `country=de, language=de-DE` "datenschutz grundverordnung einwilligung" -> 8 German .de results; `jp/ja` "東京 おすすめ ラーメン" -> 10 results (tabelog.com etc.) (PASS; two runs hit a 30 s ReadTimeout under 3-way concurrency, 48 s observed once — Bright Data latency, not code)
- Niche gibberish "zxqvbn flurbleglorp 8f7a9d xyzzyplugh" -> 4-5 loosely related results, no leak, no crash (PASS)
- Hostile query `a&b=c;rm -rf / $(whoami) \`id\` <script>alert(1)</script> ' OR 1=1 --` -> properly URL-encoded, 5 normal results (PASS); emoji query -> 5 results (PASS); 400-word query -> ReadTimeout at 30 s (Bright Data side; surfaces as an exception from `search`, mapped to `network_error` by the tool) (PASS w/ note)
- Empty / whitespace query -> `ValueError: query must not be empty`; `num_results=0` -> `ValueError` (PASS)
- Ads: "buy cheap laptop online" -> 2 organic results (Amazon, Newegg), no `bing.com/aclk` leak (PASS)
- Chrome skip: `u=a1…` that decodes to `/videos/riverview/relatedvideo?q=…` (a video-pack item) is skipped as spec'd (PASS)
- Cooldown, through the real `search_web` tool with a patched `httpx.AsyncClient.post`: cooldown -> OK with the REAL 15 s sleep -> 3 results, WARNING logged, 2 POSTs, 15.0 s (PASS); cooldown x2 -> `{"error_type":"fetch_failed","retryable":true,"message":"Bright Data SERP is cooling down this query — retry in 15 s or more: This query recently failed…"}` (PASS); upper-case marker with leading text -> envelope (PASS); marker quoted 400 chars deep inside a real SERP -> not treated as cooldown (PASS); HTTP 429 -> `fetch_failed` retryable (PASS); garbage 200 body -> `[]` + WARNING (PASS)
- Pagination, `num_results=15`, live, same query repeated 10x: returned 8, 2, 9, 15, 8, 15, 9, 15, 15, 15 results; of the 15-result runs, 4 of 6 contained duplicate URLs, e.g. `https://www.datacamp.com/tutorial/python-async-programming` at ranks [8, 13], `https://realpython.com/ref/stdlib/asyncio/` at [6, 13] (FAIL — see below)
- Raw Bing page probe, same query/offset: `li.b_algo` count varies per request (10, 9, 6, 6, 5, 10) — so page 1 is frequently < 10, and the `len(page_results) < _PAGE_SIZE` stop rule ends pagination early (FAIL — see below)

**Acceptance criteria**
- [x] PASS — no `engine`/`SearchEngine`/Google/Yandex in `web_serp.py`; grep clean; `TestBingUrl::test_search_has_no_engine_parameter`
- [ ] FAIL — one result per resolvable `li.b_algo`, chrome + duplicates skipped, ranks continue across pages
      Expected: no URL appears twice in one answer.
      Actual: live `search("python asyncio tutorial", num_results=15)` returns the same URL at two ranks across the page boundary in 4 of 6 runs (see above). `_parse_serp_html` dedupes per page only (`web_serp.py:242`, fresh `seen_urls` per call).
      Fix: thread one `seen_urls` set through the page loop in `search` (pass it into `_parse_serp_html` / `_parse_organic_or_warn`, keep rank = `len(aggregated)+1`), plus a regression test with two pages sharing a URL.
- [x] PASS — cooldown retried once after 15.0 s, second raises `BrightDataCooldownError`, empty SERP `[]` INFO, unrecognised `[]` WARNING (unit `TestCooldown`, `TestSearchEmptyResultLogging`; plus the e2e runs above)
- [x] PASS — MCP schema/answer without `engine`, cooldown envelope, `ERROR_CONTRACT` verbatim, AST guard green
- [x] PASS — CLI / Makefile / README / skill reference: no `engine`/Google wording for `search_web` (git diff reviewed; `bdata` CLI `--engine` in `.agents/skills/search` is a different tool)
- [x] PASS — format-check, lint-check, pre-commit, memory-tests green
- [x] PASS — Live (local): >= 5 external URLs
- [ ] [HUMAN] Live (Horizon, post-merge) — awaiting human verification

**Other issues found**
1. Pagination under-delivery (FAIL, same fix PR): with Bing, a page often renders < 10 `b_algo` (observed 2, 5, 6, 9 on the same query), so `if len(page_results) < _PAGE_SIZE: break` (`web_serp.py:503`) stops early; `num_results=15` returned 2 / 8 / 9 results in 5 of 10 live runs. The old rule was written for Google's fixed 10. Fix: stop on a page that adds 0 NEW results (or on the no-results indicator), not on `< 10`; add a test where page 1 has 6 results and page 2 has more. Note the same flake hits `num_results=10`: a lone degraded page of 2 returns 2 with no second fetch.
2. Cooldown on page >= 2 discards the good earlier pages (e2e: page 1 OK with 10, page 2 cooldown x2 -> envelope only, results lost). Suggest: if `aggregated` is non-empty, log WARNING and return what we have; raise only when nothing was collected. Not in AC; judgment call for the orchestrator.
3. Direct Bing hrefs carry a `?msockid=…` tracking parameter (e.g. `https://tabelog.com/ramen/tokyo/rank/?msockid=3cd4…`). Harmless but pollutes URLs fed to `ingest_url`/dedup; optional: strip `msockid`.
4. `scripts/search_web.py` still doesn't catch `httpx.TimeoutException` (SWE flagged; ReadTimeout observed live at 30 s several times, so it WILL happen) -> traceback instead of a one-line error. Out of scope; recommend a follow-up task.
5. Tool docstring says language e.g. "en", Bing's `setLang` wants a locale-like value (`en-US`, `de-DE`); `de-DE` worked live. Nit.

**VERDICT: FAIL** — 1 AC (duplicates skipped) violated live across the page boundary, plus a pagination stop rule that makes `num_results > 10` unreliable on Bing. Everything else (suite, lint, cooldown path, envelope, CLI/Make/docs, schema) PASS.


### [SWE] 2026-10-03 19:05 — Fixes (Tester FAIL 1–2 + orchestrator items 3–6)

**Files modified**
- `apps/memory/src/tree/data/web/web_serp.py`
  - FAIL 1: one `seen_urls` set lives in `search` and is threaded through `_parse_organic_or_warn` → `_parse_serp_html` (updated in place), so a url from an earlier page is dropped; rank = `len(aggregated) + 1`, a skipped duplicate consumes no rank. `_parse_serp_html` now returns `(results, duplicates)`, so a page whose results were ALL already seen logs INFO "SERP page added no new results" instead of the misleading "unexpected shape" WARNING.
  - FAIL 2: the `< _PAGE_SIZE` stop rule is gone. The loop pages until `num_results` is reached, a page adds 0 NEW urls (covers Bing's "There are no results for" page, past-the-end pages and replayed pages), or `max_pages = ceil(num_results / _PAGE_SIZE) + _EXTRA_PAGES` (2) pages were fetched (4 for `num_results=15`).
  - Item 3: `BrightDataCooldownError` from page ≥ 2 (after its one retry) → WARNING "SERP cooldown persisted for query=… at offset=… — returning the N result(s) already collected" + return them; re-raised only when nothing was collected.
  - Item 4: `_strip_tracking_params` drops `msockid` (keeps all other params, blank values and the fragment) from direct AND decoded `/ck/a` urls, before dedup — so `?msockid=A` / `?msockid=B` collapse to one result.
  - Item 6: `language` docstring → "mapped to Bing's `setLang` (e.g. `en` or `en-US`)".
- `apps/memory/src/tree/mcp/tools.py` — item 6: `language` arg doc says Bing's `setLang`, e.g. "en" or "en-US".
- `apps/memory/scripts/search_web.py` — item 5: `except httpx.TimeoutException` → `SERP request timed out: <msg or exception class>`, `except httpx.ConnectError` → `SERP request could not connect: …`, both exit 1 (cooldown is already covered by the `BrightDataRequestError` clause); item 6: `--language` help + usage example `de-DE`.
- `apps/memory/tests/unit/data/web/test_web_serp.py` — `_patch_async_client` now answers Bing's no-results page once the scripted responses run out (the page that ends pagination), so single-page tests need not script it. New: `TestSearchPagination::test_keeps_paging_after_a_short_page` (6 then 10 → 15, `first` = 1, 11), `::test_drops_a_url_already_returned_by_an_earlier_page` (regression, `/ck/a`-wrapped duplicate on page 2, ranks 1..12 contiguous), `::test_stops_on_a_page_that_adds_no_new_url` (replayed page → 2 posts, INFO, no WARNING), `::test_page_count_is_capped` (1 new url per page → exactly 4 posts), `::test_stops_paginating_on_the_no_results_page` (replaces `..._fewer_than_page_size`); `TestResolveBingHref::test_strips_the_msockid_tracking_param` (5 cases) + `::test_urls_differing_only_by_msockid_are_one_result`; `TestCooldown::test_cooldown_on_a_later_page_returns_the_results_collected` (10 kept, 3 posts, 1 sleep, WARNING with `offset=10`); the cooldown-then-results test now expects 3 posts (cooldown, retry, end page).
- `apps/memory/README.md` — `search_web` row: a later page cooling down returns the results already collected.
- `apps/memory/tests/unit/mcp/test_search_web.py` — `TestSearchWebCli::test_network_errors_exit_one_without_a_traceback` (ReadTimeout with/without message, ConnectError, BrightDataCooldownError → exit 1 via `SystemExit`, exact log line); locale test uses `de-DE`.

Red check: with the old behaviour patched back in (fresh `seen_urls` per page, `< _PAGE_SIZE` break, no `msockid` strip) 10 of the new/updated tests fail; restored → green.

**Offset choice: advance by `_PAGE_SIZE` (10), not by what Bing served.** Live probe, "python asyncio tutorial", page 1 then `first=11` vs `first=<page-1 count + 1>` (8 samples):
```
p1=8  | first=11: 5 overlap_p1=0 | first=9:  5 overlap_p1=0
p1=9  | first=11: 10 overlap_p1=1 | first=10: 10 overlap_p1=1
p1=10 | first=11: 6 overlap_p1=0 | first=11: 6 overlap_p1=0
p1=9  | first=11: 6 overlap_p1=3 | first=10: 10 overlap_p1=9   <- served-count offset replays page 1
p1=8  | first=11: 6 overlap_p1=1 | first=9:  6 overlap_p1=2
p1=9  | first=11: 6 overlap_p1=1 | first=10: 5 overlap_p1=2
p1=8  | first=11: 5 overlap_p1=1 | first=9:  5 overlap_p1=1
p1=10 | first=11: 10 overlap_p1=10 | first=11: 6 overlap_p1=2  <- Bing ignored first=11 once
```
`first` indexes Bing's 10-slot grid (rich blocks occupy slots), not our parsed list; the served-count offset never did better and once overlapped 9/9. Overlap that remains is removed by the shared `seen_urls`.

**Tests**
- Unit: 4756 passed, 0 failing — `make memory-tests` (env-status: local).
- Integration: N/A (no suite, per AGENTS.md).

**Acceptance criteria**
- [x] Parser / dedup / ranks across pages — re-checked: `TestSearchPagination::test_drops_a_url_already_returned_by_an_earlier_page`, `::test_keeps_paging_after_a_short_page`, live runs below (15/15 unique every completed run).
- (all other ACs unchanged from the previous entry)

**Evidence**
```
$ make memory-format-check   → 329 files already formatted
$ make memory-lint-check     → All checks passed!
$ make pre-commit            → prettier / ruff check / ruff format / biome check: Passed
$ make env-status            → Env target: local (.env)
$ make memory-tests          → ============ 4756 passed in 53.86s ============

# Live, search("python asyncio tutorial", num_results=15), sequential runs (POSTed `first=` values recorded):
run0: n=15 unique=15 ranks 1..15 pages=[1, 11, 21]  bing/msockid urls=0  11s
run1: ReadTimeout (30 s, Bright Data)
run2: ReadTimeout
run3: ReadTimeout
run4: n=15 unique=15 ranks 1..15 pages=[1, 11, 21]  8s
run5: n=15 unique=15 ranks 1..15 pages=[1, 11]      7s
run6: n=15 unique=15 ranks 1..15 pages=[1, 11, 21]  8s
run7: n=15 unique=15 ranks 1..15 pages=[1, 11, 21]  7s
--- second batch
run0: n=15 unique=15 pages=[1, 11, 21]  8s
run1: n=15 unique=15 pages=[1, 11, 21]  8s
run2: ReadTimeout after pages=[1]        30s
run3: n=15 unique=15 pages=[1, 11, 21]  11s
run4: ReadTimeout after pages=[1, 11]    32s
run5: n=15 unique=15 pages=[1, 11]      14s
(+ an earlier concurrent batch: 4/4 completed runs n=15 unique=15, 1 ReadTimeout)

$ make memory-search-web QUERY="python asyncio tutorial" NUM_RESULTS=15  → Got 15 result(s)
$ make memory-search-web QUERY="東京 おすすめ ラーメン" NUM_RESULTS=10 COUNTRY=jp LANGUAGE=ja
  → Got 10 result(s); [1] https://tabelog.com/ramen/tokyo/rank/   (the Tester saw ?msockid=… on this url; now stripped)
$ make memory-search-web QUERY="beanie odm mongodb async" NUM_RESULTS=5 → Got 5 result(s), all external
$ uv run python scripts/search_web.py --help → -l, --language TEXT  Optional language, mapped to Bing's setLang (e.g. 'en' or 'en-US').
```

**Notes**
- Every completed instrumented live run returned exactly 15 unique urls with ranks 1..15 (13/13; plus the CLI run: 15). Page 2 usually serves 5–6, so most runs need page 3 — within the cap of 4.
- Stop-on-0-new, as directed: in 1 of 8 probes Bing ignored `first=11` and replayed page 1; under this rule that run stops and returns ~10 for `num_results=15`. Not seen in the 13 instrumented verification runs. If it shows up in practice, the relaxation is "tolerate one 0-new page before stopping", bounded by the same cap.
- Bright Data `httpx.ReadTimeout` at the 30 s per-request timeout: 6 of 23 live searches this session, on page 1 AND on page 2. More pages per search → more exposure. A timeout on page ≥ 2 still discards the pages already collected (only the cooldown keeps them, per item 3); the MCP tool maps it to `network_error` (retryable). Candidate follow-up, not done here: treat `httpx.TimeoutException` on page ≥ 2 like the cooldown (return the collected results + WARNING).
- CLI network errors (item 5) were verified by unit tests only; no live timeout was forced through the CLI.

### [SWE] 2026-10-03 19:11 — Fixes (orchestrator decisions A–C)

**Files modified**
- `apps/memory/src/tree/data/web/web_serp.py`
  - A: the page loop catches `BrightDataCooldownError`, `httpx.TimeoutException` and `httpx.ConnectError` together. On a page after the first, it logs WARNING `SERP page failed for query=… at offset=… (<ExceptionType>) — returning the N result(s) already collected` and returns them. On page 1 (nothing collected) it re-raises, and the MCP tool's existing handler answers `network_error` (retryable) for timeout/connect and `fetch_failed` for cooldown.
  - B: `_DEFAULT_TIMEOUT_SECONDS = 60.0` (one named constant), the default of `search(timeout_seconds=…)`, the only place the SERP timeout lives. Neither the MCP tool nor the CLI passes its own value.
  - C: `_MAX_CONSECUTIVE_NO_NEW_PAGES = 2`. A page with no new url increments a counter and a page with new urls resets it. Paging stops on: `num_results` reached, Bing's "no results" page (immediately), the 2nd consecutive no-new page, or the page cap. Consequence: a page-1 body of unexpected shape (WARNING) is now followed by ONE page-2 fetch instead of stopping.
- `apps/memory/README.md` — `search_web` row: a later page that times out (60 s per request) or cannot connect also returns the results already collected.
- `apps/memory/tests/unit/data/web/test_web_serp.py`
  - New C tests (`TestSearchPagination`): `test_tolerates_one_replayed_page` (page 1, replay, new page → 15, 3 posts, no WARNING), `test_stops_on_the_second_consecutive_page_with_nothing_new` (3 posts, cap 5 not reached), `test_a_no_new_page_between_new_pages_resets_the_tolerance`.
  - New A and B tests (`TestCooldown`): `test_network_error_on_a_later_page_returns_the_results_collected[read-timeout|connect-error]`, `test_network_error_on_the_first_page_raises[…]`, `test_default_timeout_is_sixty_seconds`.
  - Updated: the request-shape and locale tests now use a real empty SERP, so they still make one POST; `test_non_cooldown_body_is_not_retried` asserts `first` = 1 then 11 (page 2, not a same-page retry); the later-page cooldown test asserts the new WARNING text, including `BrightDataCooldownError`.
- Red check: with 30 s, no network-error catch and a tolerance of 1 patched back in, 7 tests fail. Restored, they pass.

**Worst-case wall time** (`max_pages = ceil(n/10) + 2`; each page = up to 2 POSTs at 60 s plus a 15 s cooldown sleep):
- Without cooldown: `max_pages × 60 s`, i.e. 180 s for the default n=10 and 240 s for n=15.
- With a cooldown that succeeds on retry on EVERY page: `max_pages × (2 × 60 + 15)` s, i.e. 405 s for n=10 and 540 s for n=15. A cooldown that persists ends the search, so at most one failed cooldown wait happens per search.
- httpx's 60 s applies per phase (connect / write / read / pool), not to the whole request, so a pathological single request could exceed 60 s. The live max observed was 39 s.
- The MCP tool has no overall deadline of its own. If one is wanted, a per-search wall-clock budget is the follow-up.

**Tests**
- Unit: 4763 passed, 0 failing — `make memory-tests` (env-status: local).

**Evidence**
```
$ make memory-format-check   → 329 files already formatted
$ make memory-lint-check     → All checks passed!
$ make pre-commit            → prettier / ruff check / ruff format / biome check: Passed
$ make env-status            → Env target: local (.env)
$ make memory-tests          → ============ 4763 passed in 55.63s ============

# Live, search("python asyncio tutorial", num_results=15), 8 sequential runs:
run0: n=15 unique=15 ranks 1..15 pages=[1, 11]      bing/msockid=0  6s
run1: n=15 unique=15 ranks 1..15 pages=[1, 11, 21]  39s   <- a request > 30 s, would have timed out before B
run2: n=15 unique=15 ranks 1..15 pages=[1, 11]      5s
run3: n=15 unique=15 ranks 1..15 pages=[1, 11]      7s
run4: n=15 unique=15 ranks 1..15 pages=[1, 11, 21]  8s
run5: n=15 unique=15 ranks 1..15 pages=[1, 11, 21]  10s
run6: n=15 unique=15 ranks 1..15 pages=[1, 11, 21]  11s
run7: n=15 unique=15 ranks 1..15 pages=[1, 11, 21]  5s
```
8/8 returned 15 unique URLs with 0 timeouts, no partial-results WARNING and no "no new results" replay.

**Notes**
- The new network-error tests live in `TestCooldown` beside the later-page cooldown test, since they share its behaviour. A rename to something like `TestPageFailures` is cosmetic and was not done.
- The A and C paths (later-page timeout, replayed page) were not triggered live in this batch. They are covered by unit tests only.

### [Tester] 2026-10-03 19:30 — Re-QA after SWE fix rounds

**Test summary**
- Format / lint / pre-commit: PASS (329 files formatted; "All checks passed!"; prettier/ruff/biome Passed)
- Unit tests: 4763 passed / 0 failed (`make memory-tests`, env-status: local)
- Integration tests: N/A (project has none)
- Warnings: 0 in the pytest summary

**Re-verification of my previous FAILs (live, sequential, `search("python asyncio tutorial")`, `first=` values recorded)**
- `num_results=15`, 10 runs: 10/10 n=15, unique=15, ranks 1..15 contiguous, 0 bing.com / msockid / `/goto` urls; pages posted `[1,11,21]` or `[1,11]`. Latencies 7-14 s, with 4 slow runs (44, 46, 93, 95 s; Bright Data side, absorbed by the 60 s per-request timeout; no timeout surfaced). Before: 4 of 6 completed runs had duplicate URLs, 5 of 10 returned 2/8/9.
- `num_results=10`, 10 runs: 10/10 n=10, unique=10, ranks contiguous, 0 bad urls (pages `[1]`, `[1,11]`, once `[1,11,21]`).
- Both previous FAILs (cross-page duplicates, short-page early stop) are fixed.

**E2E adversarial pass (regression + new paths)**
- Happy path: `make memory-search-web QUERY="beanie odm mongodb async" NUM_RESULTS=5 ENGINE=google` -> 5 external results; `ENGINE=google` is ignored by Make (empty continuation lines in the echoed command, no `--engine` forwarded) (PASS)
- Non-English/geo: `jp/ja` "東京 おすすめ ラーメン" n=10 -> 10 results, `https://tabelog.com/ramen/tokyo/rank/` with NO `?msockid` (previous FAIL item 3 fixed) (PASS)
- Hostile query `a&b=c;rm -rf / $(whoami) <script>alert(1)</script> ' OR 1=1 --` -> 5 normal results, no crash (PASS)
- CLI `--engine google` -> `Error: No such option: --engine`; `--query ""` -> "Invalid input: query must not be empty" exit 1; `-n 0` -> "Invalid input: num_results must be >= 1" (PASS)
- CLI forced ReadTimeout (patched `httpx.AsyncClient.post`) -> `SERP request timed out: ReadTimeout`, exit 1, no traceback (PASS; previous FAIL item 4 fixed)
- Via the real `search_web` tool, scripted Bright Data: page 1 real + page 2 cooldown x2 -> partial results returned (2, Bing served 2 live) + WARNING "SERP page failed … (BrightDataCooldownError) — returning the 2 result(s) already collected" (PASS); page 1 real + page 2 ReadTimeout -> 3 results + WARNING (ReadTimeout) (PASS); page 1 ReadTimeout -> `{"error_type":"network_error","retryable":true,…}` (PASS); page 1 ConnectError -> `network_error` retryable (PASS); page 1 cooldown x2 -> `{"error_type":"fetch_failed","retryable":true,"message":"Bright Data SERP is cooling down this query — retry in 15 s or more: …"}` (PASS); garbage 200 body forever -> `[]` + WARNING, 2 POSTs (page 1 plus the one tolerated page 2) (PASS); live page 1 replayed forever, `num_results=25` -> returns the 5 collected, stops on the second consecutive no-new page (PASS)
- Grep: no `google.com/search` / `yandex` / `SearchEngine` / `engine` parameter in `web_serp.py`, `tools.py`, the CLI, Makefile or the `search_web` README row (PASS)

**Acceptance criteria**
- [x] PASS — no `engine`/Google/Yandex code (grep above; `TestBingUrl::test_search_has_no_engine_parameter`)
- [x] PASS — one result per resolvable `li.b_algo`, `/ck/a` decoded, chrome + duplicates skipped (now across pages too), ranks 1-based and contiguous — 20/20 live runs, `TestSearchPagination::test_drops_a_url_already_returned_by_an_earlier_page`, `TestBingParser`, `TestResolveBingHref`
- [x] PASS — cooldown retried once after 15.0 s, second raises `BrightDataCooldownError`; empty SERP `[]` INFO, unrecognised `[]` WARNING — `TestCooldown`, `TestSearchEmptyResultLogging`, e2e above
- [x] PASS — MCP schema/answer without `engine`, cooldown envelope, `ERROR_CONTRACT` verbatim, AST guard — `test_search_web.py`, `test_error_envelope.py` (all in the 4763 passed)
- [x] PASS — CLI / Makefile / README / skill reference carry no `engine`/Google wording
- [x] PASS — format-check, lint-check, pre-commit, memory-tests green
- [x] PASS — Live (local): >= 5 external URLs
- [ ] [HUMAN] Live (Horizon, post-merge) — awaiting human verification

**Other issues found (non-blocking)**
1. Bright Data latency is long-tailed: 4 of 20 live `search` calls took 44-95 s total (multi-page). Worst case per SWE note is 240 s for n=15 and the tool has no overall deadline; a per-search wall-clock budget is a reasonable follow-up.
2. `network_error` message for a ReadTimeout is `Could not reach Bright Data SERP API: ` with an empty tail (httpx timeout exceptions stringify to ""). Cosmetic; use `str(exc) or type(exc).__name__` as the CLI already does.
3. A page-1 body of unexpected shape now costs one extra page-2 POST before returning `[]` (consequence of the 2-consecutive-no-new rule). Acceptable.
4. A `httpx.TimeoutException` on page >= 2 returns partial results with a WARNING only; the caller cannot tell the answer is truncated. Acceptable per the orchestrator's decision A.

**VERDICT: PASS** — both prior FAILs verified fixed live (20/20 runs unique, contiguous ranks), regression pass clean, suite and lint green.

### [SWE] 2026-10-03 19:45 — Cosmetic fix before commit (Tester note)

**Files modified**
- `apps/memory/src/tree/mcp/tools.py` — `search_web` `network_error` message uses `str(exc) or type(exc).__name__`, so a bare httpx timeout no longer leaves an empty tail after the colon.
- `apps/memory/tests/unit/mcp/test_search_web.py` — `test_network_error_with_empty_message_names_the_exception` pins `"Could not reach Bright Data SERP API: ReadTimeout"`.

**Evidence**
```
$ make memory-format-check && make memory-lint-check   # 329 files already formatted / All checks passed!
$ make pre-commit                                       # all hooks Passed
$ make memory-tests                                     # 4764 passed in 58.20s (env-status: local)
```
