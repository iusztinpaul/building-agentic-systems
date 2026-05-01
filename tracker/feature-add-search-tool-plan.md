# Feature Plan: Add `search_web` — on-demand exploratory web search

## Summary

Add a new `search_web` MCP tool (and matching CLI / Make target) that lets the agent run on-demand web searches via Bright Data's SERP API and read the results directly — **without** ingesting them into the knowledge-graph memory by default. An opt-in `ingest=true` path composes with the existing `ingest-web-url-batch-etl` Prefect flow so the caller can selectively persist URLs they care about. The feature reuses the existing `BRIGHTDATA_API_KEY` and adds a new `BRIGHTDATA_SERP_ZONE` setting; SERP usage requires Bright Data's separate SERP API zone (different from the Web Unlocker zone already in use).

Decomposition rationale: a clean foundation (HTTP client + types + settings) before any wiring; the search-only MCP tool ships independently from the ingestion path so the headline "do not pollute memory" behavior is verifiable on its own; ingestion composes with what already exists rather than duplicating it; integration tests + docs at the end so live calls happen once the surface is stable.

## Tasks (in order)

1. **#006 — `tracker/006-bright-data-serp-client.groomed.md`** — Bright Data SERP API client + settings. Adds `BRIGHTDATA_SERP_ZONE` setting, `tree.data.web.types.SearchResult` Pydantic model, and `tree.data.web.web_serp.search()` async function (Google/Bing/Yandex, pagination via `start`, `brd_json=1` parsed JSON). Reuses existing `BrightDataConfigurationError` / `BrightDataRequestError`. Mocked unit tests only — no live network.
2. **#007 — `tracker/007-search-web-mcp-tool-and-cli.groomed.md`** — Expose `search_web` as an MCP tool in `tree.mcp.tools` and as a CLI script `apps/memory/scripts/search_web.py` with a `make memory-search-web QUERY="..."` Makefile target. Search-only, **no ingestion**. Depends on #006.
3. **#008 — `tracker/008-search-web-optional-ingest.groomed.md`** — Add opt-in `ingest=true` (with `ingest_top_k` / `ingest_urls`) parameters to `search_web`. Fires the existing `ingest-web-url-batch-etl/ingest-web-url-batch-etl` Prefect deployment fire-and-forget; returns the flow-run ID + tracking URL. Default behavior remains "no memory side-effects". Depends on #006, #007.
4. **#009 — `tracker/009-search-web-integration-tests-and-docs.groomed.md`** — Live integration tests (gated on `BRIGHTDATA_SERP_ZONE`), full e2e smoke run (search → ingest → memory query), README + `docs/agentic-graphrag-mcp-tools.md` updates. Depends on #006, #007, #008.

## Out of scope (intentional)

- **Image / shopping / news verticals** (Bright Data SERP supports `tbm=isch`, `tbm=nws`, etc.). Stick to organic web results in v1; add verticals as a follow-up if the agent actually needs them.
- **Maps / Hotels / Flights / Trends** specialised SERP features. Same reasoning.
- **Async SERP mode** (Bright Data's `?async=1` flow with `response_id`). Not needed for sub-5-second on-demand search; revisit if the agent ever needs to bulk-queue thousands of queries.
- **Caching SERP responses.** SERP results are time-sensitive; caching is a foot-gun. The cheap default (re-run) is correct for "on-demand exploratory". Could be added later behind a flag if a workload demands it.
- **Discover / intent-ranked search** (Bright Data's `bdata discover` semantic search). Out of scope here — the user asked for `/search`, which maps to the keyword SERP API.
- **Tool name change.** Sticking with `search_web` as proposed (verb_noun, distinct from existing `search_memory`). Flagged for human review below.

## Open questions

These do **not** block plan approval — defaults are proposed and applied in the specs. Flagging for the human in case any need correcting before #006 starts.

1. **Tool name `search_web`** — distinct from the internal-memory `search_memory`. Alternatives: `web_search`, `serp`, `search_serp`. Default applied: `search_web`. **Confirm or override.**
2. **Default search engine = Google** — the SERP API is most mature for Google. Bing/Yandex are parameterized but untested in the spec's user stories. **Confirm Google as default.**
3. **Default `num_results = 10`** — matches one Bright Data page; pagination kicks in only when the caller asks for more. **Confirm or pick a different default (e.g. 5 to save credits).**
4. **Ingest fire-and-forget vs. block-on-completion** — proposed: fire-and-forget (matches "on-demand exploratory" headline; SERP shouldn't block on a multi-minute pipeline). Trade-off: the agent doesn't know the URLs are ingested until it polls. **Confirm fire-and-forget, or switch to "block until completed" with a timeout.**
5. **Should `search_web` log SERP queries to MongoDB / Opik** for observability/eval? Existing tools use Opik traces implicitly via the LLM/embedding wrappers; the new tool doesn't go through either. Default applied: **no special tracing in this round** — match the existing tool conventions, add Opik later if/when the team starts tracing tool calls uniformly. **Confirm.**
6. **SERP zone naming** — proposed env var `BRIGHTDATA_SERP_ZONE`, setting field `brightdata_serp_zone: str`. Matches the existing `brightdata_unlocker_zone` pattern. **Confirm or rename.**

If the human is happy with all defaults, no edits needed — proceed to inner loop on #006.
