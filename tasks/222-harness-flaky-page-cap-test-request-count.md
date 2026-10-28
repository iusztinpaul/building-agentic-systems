---
id: 222-harness-flaky-page-cap-test-request-count
status: pending
feature: harness-test-flakes
---

# De-flake `tests/mcp/client.test.ts` "tools/list stops after 100 pages of endlessly new cursors, warning once" (CI saw 99 recorded requests instead of 100)

Tags: `harness`, `tests`, `flake`, `ci`
Depends on: None
Blocks: —
Implements: AGENTS.md verification cadence (a green `make harness-tests` must mean green; a timing-dependent
assertion in CI is noise the On-Call engineer has to triage every time it fires).

## Problem

GitHub Actions run 40189346890 (PR #48, head `ac40eeb`, 2026-10-25) failed the `harness (typescript)` job in
`apps/harness/tests/mcp/client.test.ts`:

```
× McpClient over stdio (fake-mcp-server.mjs) > tools/list stops after 100 pages of endlessly new cursors, warning once
  → expected [ 'tools/list', 'tools/list', …(97) ] to have a length of 100 but got 99
```

The commit range of that push did not touch the page cap, the stdio transport or this test; the next push
(`c1d7b9b`, run 40190591585) passed, as did every earlier run today. So the assertion that counts the requests
the fake server recorded races the client's give-up: the 100th `tools/list` is sent and the client stops, but
the fake server's record of that request (written from its own process) can lag behind the moment the test
reads it. Bad: counting side effects of another process after an unsynchronised stop. Good: asserting on what
the client itself did, or waiting for the fake server's record to settle.

## Scope

1. Make the assertion deterministic: either count requests on the client side (the JSON-RPC ids the client
   issued, exposed through an existing test seam), or have the fake server acknowledge each `tools/list` in
   its response (e.g. echo a page counter in the result) so the 100th page is observable from the client, or
   poll the recorded-request list until it stops growing (with a bound) before asserting. Keep the test's
   intent: exactly 100 pages fetched, one warning, no 101st request.
2. Scan `tests/mcp/client.test.ts` and `tests/mcp/transport-stdio.test.ts` for the same pattern (asserting on
   the fake server's recorded requests right after a client-side stop) and fix them the same way.
3. Run the file 20× serially and 5× under `make harness-tests` with the machine busy (e.g. `yes > /dev/null &`
   ×8) to show 0 failures; record the counts in the log.

## Out of scope

- Any change to the page cap (100) or the warning text (task 208/209 behaviour).
- The two timing budgets already widened in task 218's fold-ins.

## Acceptance criteria

- [ ] The test no longer reads the fake server's request log without synchronisation (diff shows the new
      mechanism); its intent (100 pages, one warning) is unchanged.
- [ ] 20 serial runs and 5 loaded full-suite runs: 0 failures (log the commands and counts).
- [ ] Cadence green: `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit`.

## Notes

- Never print `OPENROUTER_API_KEY` or `.env` values; never edit `.env*`, `apps/memory`, or the untracked
  "docs/Tree-Agent Harness Implementation Spec.md"; CI gets no `OPENROUTER_API_KEY`; Biome lineWidth 100 in
  `tests/`; one vitest file per module.

## Log

### [Orchestrator] 2026-10-25 14:05 — Filed from /squid-review-ci
Filed as an infra/flake task after run 40189346890 went red once and 40190591585 went green with no related
change. Not a blocker for PR #48.
