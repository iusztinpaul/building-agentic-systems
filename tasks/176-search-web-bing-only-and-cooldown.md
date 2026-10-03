---
id: 176-search-web-bing-only-and-cooldown
status: pending
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

- [ ] `tree.data.web.web_serp.search` has no `engine` parameter; `SearchEngine`, the Google/Yandex URL
      builders and the Google host list are gone (grep: no `google.com/search`, no `yandex`).
- [ ] The parser returns one `SearchResult` per `li.b_algo` with a resolvable href; `/ck/a?…&u=a1…` hrefs
      decode to the real URL; Bing chrome links and duplicates are skipped; ranks are 1-based and continue
      across pages.
- [ ] A 2xx cooldown body is retried once after `_COOLDOWN_RETRY_SECONDS` (15.0); a second cooldown raises
      `BrightDataCooldownError`; a real empty SERP returns `[]` (INFO), an unrecognised body `[]` (WARNING).
- [ ] MCP `search_web` schema has no `engine`; its answer has no `engine` key; a cooldown answers
      `{"error_type": "fetch_failed", "retryable": true, "message": …}`; `ERROR_CONTRACT` still verbatim in
      the docstring; `test_error_envelope.py` AST guard green.
- [ ] `scripts/search_web.py`, the Makefile help, `apps/memory/README.md` and the Bright Data skill reference
      carry no `engine`/Google wording for `search_web`.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [ ] Live (local): the query above returns ≥ 5 external URLs; evidence in the Log.
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
