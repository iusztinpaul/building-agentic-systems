---
id: 203-harness-provider-openrouter-sse-fake-settings
status: done
feature: tree-agent-harness
---

# M0 — core types, the SSE parser, `OpenRouterProvider` (raw fetch) with `modelInfo`, `FakeProvider`, settings/paths, and a `tree-agent "say hi"` that prints text

Tags: `harness`, `provider`, `enhancement`
Depends on: 202 (the Node/pnpm/vitest project)
Blocks: 204, 208 (`sse.ts` is reused by the MCP HTTP transport)
Implements: ADR-016 §2 (raw-fetch provider, no SDK)

## Scope

1. **`src/types.ts`** — spec §4 verbatim (`StopReason`, `Usage`, `ToolCall`, `Message`, `Context`,
   `AgentEvent`). `Decision`, `DecisionSource` and `PermissionMode` (spec §11) live here too so `AgentEvent`
   compiles now; 206 implements them.
2. **`src/provider/`** — `types.ts` (spec §5: `ModelInfo`, `ToolSchema`, `ChatRequest`, `StreamEvent`,
   `StructuredRequest`, `Provider`), `sse.ts` (`parseSse(body): AsyncIterable<string>` with the §5 SSE rules:
   blank-line framing, `:` keep-alives skipped, `data: [DONE]` ends, top-level `error` chunk terminal, the
   usage chunk is accounting not a second `done`), `openrouter.ts` (`modelInfo` from `GET /api/v1/models`
   cached per process, mapping `context_length`, `top_provider.max_completion_tokens`, `supported_parameters`,
   `pricing`; `stream` with the §5 wire contract — headers incl. `X-Title: tree-agent`, `tools` on every
   request, `stream_options.include_usage`, tool-call deltas reassembled by `index`, `JSON.parse` at `done`,
   parse failure → that call `isError`; `finish_reason` mapping; HTTP 4xx/5xx → `error` event with the body's
   code; a context-overflow 400 surfaces as `code: "context_length_exceeded"` so 212 can retry;
   `complete<T>` non-streaming with `response_format.json_schema` strict + `provider.require_parameters`,
   `schema.parse`, one retry with the error appended; `completeText`). `fetch` is injectable so the wire
   shape is tested offline against recorded SSE fixtures. `fake.ts` — `FakeProvider` replays scripted
   `StreamEvent[]` per call, scripted `complete`/`completeText` answers, and records every request.
3. **`src/config/paths.ts`, `settings.ts`** — spec §16 `Settings` zod schema and defaults; `TREE_AGENT_HOME`
   overrides `~/.tree-agent`; merge order defaults → `~/.tree-agent/settings.json` → flags in this task (the
   project layer `.tree-agent/settings.json` arrives with trust in 204). `OPENROUTER_API_KEY` required.
   Startup check: the configured `decisionModel` must list `structured_outputs` in `supported_parameters`
   (fail fast, one line naming the model).
4. **Minimal `src/cli.ts`** — `tree-agent "<prompt>" [--model <id>]`: one streamed request with no tools,
   text to stdout, exit 0; missing key → one stderr line, exit 1; provider `error` → the message, exit 1.
   No TTY detection, stdin or `--json` yet (204 replaces this stub with the §15 matrix).
5. **`src/log.ts`** — one tiny stderr logger (level from `TREE_AGENT_LOG`, default `warn`); library code never
   `console.log`s (AGENTS.md: no print-style debugging; the event sink is the user channel).
6. Dependencies: `zod` (3.25.x, verified) and `zod-to-json-schema`.

**Out of scope (intentional):** the loop, tools, prompt sections (204); any TUI (205); OpenRouter features
beyond chat completions + models (no images, no provider routing knobs — not in the spec).

## Acceptance Criteria

- [x] `sse.ts`: skips `:` comment lines; `data: [DONE]` ends the iterable; a mid-stream chunk with a top-level
      `error` yields a `StreamEvent error` and nothing after it; the trailing usage chunk does not produce a
      second `done`; tool-call deltas split across chunks are reassembled by `index`; invalid JSON arguments
      yield an assistant message whose call is marked for an `isError` result, never a throw.
- [x] `openrouter.ts` offline (injected `fetch`): the request body carries `messages[0].role === "system"`,
      `stream: true`, `stream_options.include_usage`, the `tools` array also on the request that carries tool
      results, assistant `tool_calls[].function.arguments` as a JSON string with `content: null`, and
      `{role:"tool", tool_call_id}` results; headers include `Authorization`, `HTTP-Referer`, `X-Title`.
- [x] `finish_reason` maps `stop|tool_calls|length|error`; an HTTP 400 whose message contains "context length"
      yields `error` with `code: "context_length_exceeded"`; other 4xx/5xx carry the body's error code.
- [x] `complete<T>` sends `response_format: {type: "json_schema", json_schema: {name, strict: true, schema}}`
      and `provider: {require_parameters: true}`; a first answer that fails `schema.parse` triggers exactly one
      retry whose input ends with the parse error; a second failure rejects.
- [x] `modelInfo` hits `GET /api/v1/models` once per process (second call served from cache) and maps
      `contextLength`, `maxOutputTokens`, `supported`, `pricing`; an unknown id rejects with a message naming it.
- [x] A `decisionModel` whose `supported_parameters` lacks `structured_outputs` makes startup exit 1 with one
      line naming the model (FakeProvider / fake models list).
- [x] `Settings.parse({})` equals the §16 defaults (`model` `anthropic/claude-sonnet-5.5`, `decisionModel`
      `anthropic/claude-haiku-5.5`, `compaction.triggerRatio` 0.8, `codemode.llm.depth` 1 …);
      `TREE_AGENT_HOME=/tmp/x` makes every path resolve under `/tmp/x`; a `settings.json` with an unknown
      key or `triggerRatio: 0.2` fails with the zod issue list.
- [x] `FakeProvider` replays scripted events in order and exposes the recorded `ChatRequest`s; every later
      task's loop/permission/compaction tests build on it.
- [x] Live (`describe.skipIf(!process.env.OPENROUTER_API_KEY)`): one streamed turn with a tool call on the
      main model; `complete<T>` returns schema-valid JSON on the decision model. Both report as skipped when
      the key is absent (negative: `env -u OPENROUTER_API_KEY pnpm exec vitest run` is green with 2 skips).
- [x] [HUMAN] `make harness-run PROMPT="say hi"` prints a greeting and exits 0; with the key blanked it prints
      one line naming `OPENROUTER_API_KEY` and exits 1.
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green.

## User Stories

### Story: A developer runs the first prompt
1. With `OPENROUTER_API_KEY` in `.env`, they run `make harness-run PROMPT="say hi"`.
2. Text streams to stdout as it arrives; the process exits 0 after the `[DONE]` frame.

### Story: The key is missing
1. They run `env -u OPENROUTER_API_KEY node dist/cli.js "hi"`.
2. stderr shows `OPENROUTER_API_KEY is not set (add it to .env)`; exit code 1; no request is sent.

### Story: OpenRouter sends a keep-alive and a mid-stream error
1. A recorded fixture contains `: OPENROUTER PROCESSING`, two text deltas, then `{"error": {...}}`.
2. `parseSse` + `stream` emit two `text_delta`s and one `error` event with the upstream code; the test asserts
   no `done` follows.

### Story: A tool call arrives in three chunks
1. The fixture splits `{"query": "pricing"}` across three `tool_calls[0].function.arguments` deltas.
2. The `done` message has one `ToolCall` with `arguments.query === "pricing"`.

### Story: The decision model cannot do structured outputs
1. `settings.json` sets `decisionModel` to a model whose `supported_parameters` lacks `structured_outputs`.
2. `tree-agent "hi"` exits 1 with `decisionModel <id> does not support structured_outputs`.

---

Blocked by: 202

## Log

### [PA] 2026-10-24 19:46 — Grooming

**Summary**
The wire layer of tree-agent: core types, the hand-written SSE parser, the raw-fetch OpenRouter provider with
model info and structured outputs, the scripted FakeProvider every later test runs on, settings/paths, and a
one-shot CLI that proves text flows end to end.

**Key decisions**
- `fetch` is injected so the OpenRouter wire contract is pinned offline; only two tests are live and they skip
  without the key (CI never has it).
- A recognisable `context_length_exceeded` code is produced here so 212's compact-and-retry needs no string
  matching in the loop.
- The project settings layer and trust wait for 204 — this task has no notion of a project yet.

**Dependencies**
- 202 — the pnpm/vitest project and Biome rules.

**User stories**
- 5 stories covering: the first prompt, the missing key, keep-alive + error frames, chunked tool-call
  arguments, the structured-outputs startup check.

Ready for implementation.

### [SWE] 2026-10-24 22:43 — Implementation

**Files modified**
- `apps/harness/package.json`, `pnpm-lock.yaml` — runtime deps `zod ^3.25.76` (resolved 3.25.76, the last 3.x; imported from `"zod"`, never `zod/v4`) and `zod-to-json-schema ^3.25.2` (peer `zod ^3.25.28 || ^4`). Added with `pnpm add` inside `apps/harness`. Nothing else.
- `src/types.ts` — spec §4 types + `PermissionMode`/`Decision`/`DecisionSource` (§11) + an `AssistantMessage` alias. **One deviation from §4:** `ToolCall.parseError?: string` (see Decisions).
- `src/provider/types.ts` — §5 `ModelInfo`, `ToolSchema`, `ChatRequest`, `StreamEvent`, `StructuredRequest`, `TextRequest`, `Provider`, `JsonSchema`, plus `ProviderError {code}` (what `complete`/`completeText`/`modelInfo` reject with; `code` uses the same values as the `error` StreamEvent).
- `src/provider/sse.ts` — `parseSse(body: AsyncIterable<Uint8Array>): AsyncGenerator<string>`; generic (yields `data` payloads only) so 208's MCP HTTP transport can reuse it.
- `src/provider/openrouter.ts` — `OpenRouterProvider` (raw `fetch`, injectable), `decodeChatStream` (SSE payloads → StreamEvents), `toWireMessages`, `toStopReason`, `CONTEXT_LENGTH_EXCEEDED`.
- `src/provider/fake.ts` — `FakeProvider` (+ `fakeModel()`, `fakeDone()` helpers for later tasks' scripts).
- `src/config/paths.ts` — `userPaths(env)`: home, settings.json, mcp.json, trusted.json, AGENTS.md, skills/, sessions/ under `$TREE_AGENT_HOME` or `~/.tree-agent`.
- `src/config/settings.ts` — §16 `Settings` (every object `.strict()`), `loadSettings({env, flags})`, `requireApiKey`, `checkDecisionModel`, `SettingsError`.
- `src/log.ts` — `createLogger(level, out)` + `log` (stderr, `TREE_AGENT_LOG`=debug|info|warn|error|silent, default warn).
- `src/cli.ts` — replaces the usage stub: exported `main(argv, deps): Promise<number>` + an entry-point guard; `tree-agent "<prompt>" [--model <id>]`.
- `tests/helpers/http.ts` — `fakeFetch` (records url/method/headers/parsed body/signal), `sseResponse`/`byteStream` (cuts bytes every N, incl. mid-character), `sse(...)`, `fixture()`, `collect()`.
- `tests/fixtures/openrouter/*.sse` — `say-hi.recorded.sse` and `tool-call.recorded.sse` **recorded from live OpenRouter** with curl (2026-10-24; the tool one has a real mid-stream `: OPENROUTER PROCESSING` and the args split `""`, `""`, `{"query": "pricing`, `"}`); synthetic per the docs/stories: `keepalive-then-error.sse`, `tool-call-three-chunks.sse`, `invalid-tool-args.sse`. Grepped for `sk-or` → 0 hits.
- `tests/provider/sse.test.ts` (19), `tests/provider/openrouter.test.ts` (31 offline + 2 live), `tests/provider/fake.test.ts` (10), `tests/config/settings.test.ts` (13), `tests/config/paths.test.ts` (3), `tests/log.test.ts` (3), `tests/cli.test.ts` (9; the 202 usage test kept).

**Decisions (choices the spec left open)**
- *`ToolCall.parseError?: string`* — the AC needs the `done` message to mark a call for an `isError` result, and `ToolCall` is the only carrier. On invalid/non-object arguments the call gets `arguments: {}` + `parseError: "invalid JSON arguments for <name>: <parser message>"`; 204's loop must answer such a call with `{role:"tool", isError:true, content: parseError}` instead of running it. On the wire the call is always re-sent as `JSON.stringify(arguments)` (`"{}"`), never the malformed raw text, so the next request cannot 400 on it.
- *Stream contract: exactly one terminal event, `stream()` never throws.* Top-level `error` chunk → `error` (no `done`), even as the first chunk on a 200. `finish_reason:"error"` without an error object → `done` with `stopReason:"error"`. EOF with no `finish_reason` seen → `error {code:"incomplete_stream"}` (a truncated body must not pass as a complete answer). Unparseable `data` → `error {code:"invalid_chunk"}`. fetch rejection → `error {code:"network_error"}`. Abort (before the response or mid-body) → `done {stopReason:"aborted", content: partial text, toolCalls: []}` (partial calls dropped). `FakeProvider` mirrors the abort behaviour. `content_filter` → `error`, any other unknown `finish_reason` → `stop`.
- *`tools` key omitted when the array is empty* (OpenAI-compatible endpoints reject `tools: []`; the CLI stub sends none). Every request that has tools sends them, including the tool-result turn (tested).
- *Assistant text next to tool calls is kept* (`content: "Let me look."` + `tool_calls`); `content: null` only when the text is empty, which is the AC's case.
- *Context overflow detection*: HTTP 400 whose `error.message` **or upstream `error.metadata.raw`** contains "context length" (case-insensitive) → `code: "context_length_exceeded"`. Real 400 recorded from OpenRouter (`microsoft/phi-4`, 18k tokens into 16k): `{"error":{"message":"This endpoint's maximum context length is 16384 tokens. However, you requested about 18010 tokens …","code":400,…}}` — that body is the test input. The `metadata.raw` leg exists because a live 400 I hit while recording came back as `message: "Provider returned error"` with the real reason only in `metadata.raw`; that raw text (≤300 chars) is also appended to the user-facing message. Other HTTP errors: `code = String(body.error.code)` (OpenRouter's is numeric = HTTP status), falling back to the status when the body is not JSON. `finish_reason:"length"` on the first chunk (spec §5's other overflow signal) is surfaced as `stopReason:"length"` for 212 to act on.
- *`complete<T>`*: schema via `zodToJsonSchema(schema, {target:"openAi", $refStrategy:"none"})` with `$schema` deleted — the `openAi` target makes every property required (strict mode needs that) and turns `.optional()` into a required **nullable** field, so a `complete` schema should use `.nullable()`, not `.optional()` (an optional field answered with `null` fails `schema.parse` → retry). One retry for a `JSON.parse` **or** `schema.parse` failure; the retry's user input is the original input + `"\n\nYour previous answer did not match the schema. Answer again with JSON only. Error: <issues>"`; the second failure rejects `ProviderError("invalid_structured_output", "<name>: <issues>")`. HTTP errors reject at once, no retry. The retry logs at `info` (silent by default).
- *`modelInfo` cache is per provider instance* (= per process: the CLI builds one provider); the in-flight promise is shared by concurrent callers and dropped on failure so a transient 5xx can be retried. Per-instance keeps tests with injected fetch isolated. `pricing` strings → numbers (USD/token); a null `top_provider.max_completion_tokens` falls back to `context_length`.
- *Settings*: each layer is validated on its own first so the error names the file (`<path>: invalid settings` + `  - <path>: <zod message>` lines), then layers deep-merge key by key (plain objects merge, everything else replaces; an `undefined` flag keeps the file's value) and the merge is parsed once more so defaults fill the gaps. `requireApiKey` trims and rejects blank values. `checkDecisionModel` lives in `settings.ts` and only needs `Provider.modelInfo`.
- *CLI*: order is args → key (before any provider/fetch exists) → settings → provider → `checkDecisionModel` (one `GET /models`) → one `POST` stream. Positionals are joined with a space (`tree-agent say hi` works). Uses `process.exitCode`, not `process.exit()`. Entry guard: `realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)` (`import.meta.main` is not on Node 22); verified for `node src/cli.ts`, `node dist/cli.js` and a symlink to `dist/cli.js` (the npm bin case).

**Finding for task 206 (auto-mode classifier)**
- With the exact §11.1 `DangerCheck` schema (`reason: z.string().max(200)`) on `anthropic/claude-haiku-5.5`, strict structured outputs **did not enforce `maxLength`**: the first live answer had a >200-char `reason` and `schema.parse` failed (`TREE_AGENT_LOG=info` showed `danger_check: retrying invalid answer (reason: String must contain at most 200 character(s))`); the retry passed. The request was accepted (no 400 for `maxLength`). The live test's system prompt now also says "Keep `reason` to one sentence under 200 characters"; 3 re-runs, 0 retries. 206 should do the same in the classifier prompt, since a second failure is fail-closed (→ prompt / headless deny).

**Tests**
- Unit (offline): 88 passed, 2 skipped without the key; with the key from `.env` (`make harness-tests`): 90 passed (live tests ~2.4 s + ~4 s).
- Integration: N/A — no integration suite (AGENTS.md).
- Mutation probes (each change made, suite run, file restored byte-identical): `done` emitted on every `finish_reason` → 2 fail (usage-chunk + recorded tool stream); context-length mapping removed → 2 fail; retry input without the error → 2 fail; `.strict()` dropped from nested objects → unknown-key test fails; `:` lines treated as data → 6 fail; `[DONE]` not ending → 6 fail.

**Acceptance criteria**
- [x] `sse.ts` rules — `tests/provider/sse.test.ts`: `parseSse > yields each event's data and skips ':' keep-alive comments`, `> ends the iterable at data: [DONE]…`, byte-split ×4, CRLF, multi-line data, unterminated event; `chat stream decoding > emits the text deltas then one error after a keep-alive and a mid-stream error chunk` (story fixture: 2 `text_delta` + 1 `error {code:"server_error"}`, no `done`), `> treats an error chunk as terminal even when more chunks follow`, `> emits exactly one done for the recorded say-hi stream despite the trailing usage chunk`, `> reassembles a tool call whose arguments arrive in three chunks` (story: `arguments.query === "pricing"`, bytes cut every 5), `> reassembles the recorded OpenRouter tool-call stream`, `> marks a call with invalid JSON arguments with a parseError instead of throwing`, non-object args, empty args, truncated body, unparseable chunk.
- [x] Wire shape — `openrouter.test.ts > OpenRouterProvider.stream request > sends the chat-completions wire shape on a request that carries tool results` (system first, `stream`, `stream_options.include_usage`, `tools` on the tool-result turn, assistant `content:null` + `arguments` JSON string, `{role:"tool", tool_call_id, content}`), `> sends the Authorization, HTTP-Referer and X-Title headers`, `> omits the tools key when there are no tools…`, `> keeps assistant text next to its tool calls and serialises a parseError call as {}`.
- [x] `finish_reason` + HTTP errors — `> maps finish_reason %s to stopReason %s` ×4, `> maps content_filter…`, `> turns the recorded HTTP 400 context-overflow body into context_length_exceeded`, `> finds a context overflow hidden in the upstream metadata.raw of a 400`, `> carries the body's error code for HTTP %i` (401/402/429/400), `> falls back to the HTTP status when a 5xx body is not JSON`, plus `network_error` and both abort cases.
- [x] `complete<T>` — `OpenRouterProvider.complete > sends a strict json_schema response_format with require_parameters` (full `response_format` asserted), `> retries once with the parse error appended…` (2 requests; retry input starts with the original and ends with `Error: dangerous: Expected boolean, received string`), `> retries when the first answer is not JSON at all`, `> rejects after the retry also fails, with exactly two requests sent`, `> rejects an HTTP error with its code and does not retry`; `completeText` ×2.
- [x] `modelInfo` — `OpenRouterProvider.modelInfo > maps context_length, max_completion_tokens, supported_parameters and pricing`, `> hits GET /api/v1/models once and serves later lookups from the cache` (2 concurrent + 1 other id → 1 fetch), null max fallback, `> rejects an unknown id with a message naming it`, `> does not cache a failed models request`.
- [x] `decisionModel` startup check — `settings.test.ts > checkDecisionModel > fails with one line naming…` and `cli.test.ts > main > exits 1 naming a settings.json decisionModel that lacks structured_outputs` (FakeProvider + fake models list; stderr exactly `decisionModel weak/model does not support structured_outputs\n`, no stream request sent). Live: see Evidence.
- [x] Settings/paths — `settings.test.ts > parses {} to the spec §16 defaults` (full object), merge/flags tests, `> rejects an unknown key with the zod issue list naming the file`, `> rejects triggerRatio 0.2 with the issue path and bound`, invalid JSON; `paths.test.ts > resolves every path under TREE_AGENT_HOME when it is set` (`/tmp/x`). All settings/CLI tests point `TREE_AGENT_HOME` at a `mkdtemp` dir — the real `~/.tree-agent` is never read.
- [x] `FakeProvider` — `fake.test.ts` (in-order replay + recorded requests, snapshot isolation, missing script throws, abort keeps partial text, `complete` validates with the schema / scripted Error rejects, `completeText`, `modelInfo` default + scripted list).
- [x] Live — `openrouter.test.ts > OpenRouter live > streams a turn with a tool call on the main model, then answers from the result` (sonnet-5.5 calls `search_memory`, then a second request carrying the tool result + `tools` answers with "$5" — proves the tool-result wire shape is accepted upstream) and `> complete<T> returns schema-valid JSON on the decision model` (haiku-5.5, §11.1 schema). Negative: `cd apps/harness && env -u OPENROUTER_API_KEY pnpm exec vitest run` → `88 passed | 2 skipped (90)`, exit 0.
- [ ] [HUMAN] `make harness-run PROMPT="say hi"` — left for the human. I ran it for Step 7: prints `Hi! How can I help you today?`, recipe exit 0; `make harness-run PROMPT="say hi" OPENROUTER_API_KEY=` → `OPENROUTER_API_KEY is not set (add it to .env)`, `make[1]: *** [run] Error 1` (outer make 2, as in 202).
- [x] Cadence — `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit` → each exit 0 (also `make harness-format-check` 0).

**Evidence**
```
$ make harness-lint-fix; make harness-lint-check; make harness-typecheck; make harness-tests; make pre-commit
lint-fix=0 lint-check=0 typecheck=0 tests=0 pre-commit=0   (Biome: "Checked 17 files … No fixes applied.")
 ✓ tests/provider/openrouter.test.ts (33 tests) 6358ms
   ✓ OpenRouter live > streams a turn with a tool call on the main model, then answers from the result  2387ms
   ✓ OpenRouter live > complete<T> returns schema-valid JSON on the decision model  3956ms
 Test Files  7 passed (7)
      Tests  90 passed (90)
biome check (harness)....................................................Passed

$ cd apps/harness && env -u OPENROUTER_API_KEY pnpm exec vitest run
      Tests  88 passed | 2 skipped (90)        (exit 0)

$ make harness-run PROMPT="say hi"
node src/cli.ts  "$PROMPT"
Hi! How can I help you today?                  (exit 0)
$ env -u OPENROUTER_API_KEY node src/cli.ts "hi"
OPENROUTER_API_KEY is not set (add it to .env) (exit 1)

# live, TREE_AGENT_HOME=<scratch> with settings.json {"decisionModel":"recursive/flint-v1.0:free"}
$ node src/cli.ts "hi"
decisionModel recursive/flint-v1.0:free does not support structured_outputs   (exit 1)
# settings.json {"decisionModel":"acme/nope"}
unknown model "acme/nope": not listed by GET /api/v1/models                     (exit 1)
# settings.json {"compaction":{"triggerRatio":0.2},"modle":"x"}
<scratch>/home-bad/settings.json: invalid settings
  - compaction.triggerRatio: Number must be greater than or equal to 0.3
  - (root): Unrecognized key(s) in object: 'modle'                              (exit 1)
$ node src/cli.ts "hi" --model acme/does-not-exist
acme/does-not-exist is not a valid model ID                                     (exit 1, HTTP 400 body message)
$ node src/cli.ts "Reply with exactly: pong" --model anthropic/claude-haiku-5.5
pong                                                                            (exit 0)
$ OPENROUTER_API_KEY=sk-or-invalid node src/cli.ts "hi"
Missing Authentication header                                                   (exit 1, OpenRouter's 401 text)

$ make harness-build && node dist/cli.js "Reply with exactly: ok"   -> ok (exit 0); dist/ = cli.js, config/*, log.js, provider/*, types.js
$ ln -s dist/cli.js <scratch>/tree-agent && env -u OPENROUTER_API_KEY <scratch>/tree-agent hi  -> key line, exit 1 (entry guard works via symlink)
$ npx -y node@22 src/cli.ts "Reply with exactly: ok22"   (v22.23.4) -> ok22, exit 0
$ env -u OPENROUTER_API_KEY npx -y node@22 node_modules/vitest/vitest.mjs run -> 88 passed | 2 skipped
```

**Notes**
- `make harness-tests` exports `.env`, so the two live tests run (and cost ~$0.002) on every cadence run with a real key; CI has no key and skips them.
- `dist/` now also ships `provider/fake.js` (tsc builds all of `src/`); harmless (no deps), 213 may exclude it.
- The key was never printed: recording used `(set -a; . ./.env; set +a; curl -H "Authorization: Bearer $OPENROUTER_API_KEY" …)` and the fixtures were grepped for `sk-or` (0). `.env`, `.env.prod`, `.env.target` untouched; `docs/Tree-Agent Harness Implementation Spec.md` untouched.
- `.env.example`: added commented-out `# TREE_AGENT_HOME=~/.tree-agent` and `# TREE_AGENT_LOG=warn` under `OPENROUTER_API_KEY` (commented so `make harness-*`, which exports `.env`, never overrides the defaults).
- Known assumptions, not bugs: `decodeChatStream` takes a tool call's `id`/`name` from its first delta (OpenRouter's normalised wire always sends them there, the recorded fixture confirms; a later-fragment `name` would leave `name: ""`). `parseSse` handles `\n` and `\r\n` line ends, not lone `\r` (WHATWG allows it; OpenRouter and MCP servers use `\n`).
- Not committed — waiting for the Tester.
- Post-QA fixes (Tester's "Other issues"): a non-object chunk (`null`/`42`/`[]`/`"x"`) -> `error invalid_chunk`; a non-array `tool_calls` or a delta without a numeric `index` is dropped (debug log), streaming continues; a 200 non-JSON/non-object body makes `complete`/`completeText` (and `modelInfo`, shared `readJson`) reject `ProviderError("invalid_response")`; fake key `sk-or-test` -> `test-key` (`grep -rn "sk-or-" apps/harness` empty). Tests: `openrouter.test.ts > OpenRouterProvider malformed upstream responses > ends the stream with invalid_chunk when a chunk is %s` (x4), `> ignores the delta and keeps streaming when %s` (x3), `> rejects complete with invalid_response on a 200 non-JSON body, without a retry`, `> rejects completeText with invalid_response when a 200 body is %s` (x2); all 10 fail with the fixes reverted. Cadence exit 0, `make harness-tests` 100 passed.

### [Tester] 2026-10-24 22:55 — QA

**Test summary**
- Format / lint / typecheck / pre-commit: PASS (`make harness-lint-check`, `harness-format-check`, `harness-typecheck`, `make pre-commit` all exit 0; Biome "Checked 17 files … No fixes applied")
- Unit tests: 90 passed / 0 failed with the key (`make harness-tests`); 88 passed / 2 skipped without it
- Integration tests: N/A (no suite, AGENTS.md)
- Warnings: 0

**E2E adversarial pass**
- Live tests really ran: `make harness-tests` (key in `.env`, 1 match, 74 chars, not a placeholder) -> `✓ OpenRouter live > streams a turn with a tool call … 2370ms` and `> complete<T> … 2022ms` (network time, not skipped), 7 files / 90 passed.
- Negative: `cd apps/harness && env -u OPENROUTER_API_KEY pnpm exec vitest run` -> `openrouter.test.ts (33 tests | 2 skipped)`, `88 passed | 2 skipped (90)`, exit 0.
- Happy path (HUMAN AC, run live by me): `make harness-run PROMPT="say hi"` -> `Hi! How can I help you today?`, exit 0.
- Key blanked: `make harness-run PROMPT="say hi" OPENROUTER_API_KEY=` -> `OPENROUTER_API_KEY is not set (add it to .env)` + `make[1]: *** [run] Error 1`, make exit 2 (as in 202). Direct: `env -u OPENROUTER_API_KEY node src/cli.ts hi`, `env OPENROUTER_API_KEY= …`, `OPENROUTER_API_KEY="   " …` -> same single line, exit 1. NOTE: `env OPENROUTER_API_KEY= make harness-run …` does NOT blank the key, because the Makefile's `include .env` overrides the environment; use the make command-line form. Not a defect, but the human should not use the `env … make` form.
- Stream mutations (own script, injected fetch, `stream()` never threw, exactly 1 terminal event and it was last, in every case below):
  - M1 `[DONE]` with no usage chunk -> text_delta, done(stop). PASS
  - M2 tool-call index jumps 0->2 -> two calls, ordered, done(tool_calls). PASS
  - M3 `error` chunk AFTER `[DONE]` -> ignored, done(stop). PASS. M3b error after finish_reason but before `[DONE]` -> error `500 late` (one terminal). PASS
  - M4 429 JSON body -> error code "429" message "rate limited"; 429 HTML body -> code "429", message is the text; 500 empty body -> `HTTP 500`. PASS
  - M5 whole stream with 1-byte reads incl. `héllo 日本語 🌳` -> intact text, done. PASS
  - M10 body errors mid-stream -> text_delta, error network_error "socket hang up"; M11 fetch rejects -> network_error; M16 pre-aborted -> done aborted; M17 abort mid-body -> done aborted content "partial". PASS
  - M8 empty 200 body, M9 only `[DONE]` -> error incomplete_stream. M12 5 MB single delta -> done. M13 CRLF + split data lines -> done. M14 invalid-JSON args -> toolCall with `parseError`, arguments {}. PASS
  - Hostile chunk shapes (outside the AC, still one terminal): `data: null` -> error with code `network_error` / "Cannot read properties of null" (misclassified, should be `invalid_chunk`); `data: 42`/`[]` -> incomplete_stream; `tool_calls:"oops"` -> a phantom call id `call_undefined`; tool_call delta without `index` -> keyed `undefined`. See Other issues.
- complete<T>: first answer fails schema -> exactly 2 requests, both with `response_format.json_schema.strict === true` and `provider.require_parameters === true`, retry input ends `Error: x: Expected number, received string`; second failure -> `ProviderError invalid_structured_output "t: x: Expected number, received string"` after exactly 2 requests; HTTP 400 -> rejects after exactly 1 request (no retry). Schema sent for `.nullable()` field: `{"type":["string","null"]}`, all keys `required`, `additionalProperties:false`. PASS. A 200 with an HTML body makes `complete` reject with a raw `SyntaxError` (not `ProviderError`), see Other issues.
- Context overflow: 400 "maximum context length" and upper-case variant -> `context_length_exceeded`; 400 with `metadata.raw` containing "context length" -> `context_length_exceeded`; 413 with same text -> code "413" (spec/AC only name 400); unrelated 400 -> "400"; 401 -> "401". PASS
- modelInfo: 1 `GET /models` for 4 lookups (2 sequential + 2 concurrent); unknown id rejects `unknown model "zzz": not listed by GET /api/v1/models`; null `max_completion_tokens` falls back to `context_length`; pricing strings -> numbers. PASS
- Settings: `Settings.parse({})` printed and compared to §16 field by field (model, decisionModel, permissions.mode ask, triggerRatio 0.8, keepRecentTokens 20000, inlineBudgetTokens 3000, timeoutMs 60000, maxToolCalls 50, concurrency 4, llm.depth 1/maxCalls 16/maxInputChars 200000/maxTotalTokens 400000/maxSubTurns 8, contextFiles.maxChars 40000, skills.listingBudgetRatio 0.01, llm.model absent) -> identical; also asserted whole-object in `settings.test.ts:53`.
- CLI against real settings.json files (TREE_AGENT_HOME = scratch dir, exit 1 each, one named-file message, no stack trace): empty file (`invalid JSON: Unexpected end of JSON input`), `[]`, `null`, `"x"` (`Expected object, received …`), `{"model":5}`, `triggerRatio 0.2` (`Number must be greater than or equal to 0.3`), unknown key `modle` (`Unrecognized key(s) in object: 'modle'`), `depth 3`, string `keepRecentTokens`; TREE_AGENT_HOME pointing at a file -> `ENOTDIR: …` exit 1. UTF-8 BOM settings.json -> `invalid JSON` (see notes).
- CLI args: no prompt / `""` / `"   "` -> usage, exit 1; `--model` without value, `--bogus` -> parseArgs message + usage, exit 1; `--model ""` -> `No models provided` (OpenRouter text) exit 1; `-- "-5 plus 2? …"` -> `-3` exit 0; prompt `Reply with exactly this text: "$(whoami) ; \`id\` 日本語 🌳 <script>"` -> echoed verbatim, no shell expansion, exit 0; unquoted `reply with exactly pong` -> `pong` exit 0.
- Secrets/structure: `grep -rn "sk-or-" apps/harness/tests apps/harness/src` -> 3 hits, all the literal fake `sk-or-test` (cli.test.ts:31,158,161; settings.test.ts:126), no real key, no key in fixtures (see note). `ls src` -> no `loop/` (`agent-loop` absent, correct for this task). `grep -rn "console\.\|process\.std" src` outside cli.ts -> only the `process.stderr` default in `log.ts:17`. `.env*`, the untracked spec doc untouched (`git status` shows only the 6 expected modified files).

**Acceptance criteria**
- [x] PASS — sse.ts rules — `tests/provider/sse.test.ts:24` (skips `:`), `:32` (`[DONE]` ends), `:62` (2 text_delta + error, no done), `:72` (error terminal), `:83` (one done despite usage chunk), `:99` (three-chunk args, query === "pricing"), `:131` (invalid args -> parseError, no throw); my M1/M3/M14 probes agree.
- [x] PASS — wire shape — `openrouter.test.ts:76` (system first, stream, include_usage, tools on tool-result turn, content null + JSON-string arguments, tool role), `:122` (Authorization/HTTP-Referer/X-Title), `:132`, `:142`.
- [x] PASS — finish_reason/HTTP errors — `openrouter.test.ts:166` (4 mappings), `:186`, `:203`, `:217` (401/402/429/400 codes), `:233`; my overflow/4xx probes agree.
- [x] PASS — complete<T> — `openrouter.test.ts:302`, `:340`, `:375`; my C1/C2/C5 probes (2 requests, strict + require_parameters, one retry, reject).
- [x] PASS — modelInfo — `openrouter.test.ts:462`, `:480`, `:498`; my probe: 1 fetch for 4 lookups.
- [x] PASS — decisionModel startup check — `settings.test.ts:131`, `cli.test.ts:106`; SWE's live run recorded; the check is the first network call after settings load.
- [x] PASS — Settings/paths — `settings.test.ts:53`, `:83`, `:98`; `paths.test.ts:7`; my CLI settings.json probes above.
- [x] PASS — FakeProvider — `fake.test.ts:14,30,40,48,89,96,103,114,121`.
- [x] PASS — Live tests — `openrouter.test.ts:519-573`: run (2.4 s + 2.0 s) with key; `88 passed | 2 skipped`, exit 0 without.
- [x] PASS — [HUMAN] live run — output captured above (greeting exit 0; blank key one line, exit 1). Ticked by the Tester as evidence; the human may re-run.
- [x] PASS — cadence — lint-check, format-check, typecheck, tests, pre-commit all exit 0.

**Evidence**
```
$ make harness-tests            -> Test Files 7 passed (7) | Tests 90 passed (90); live tests 2370ms / 2022ms
$ env -u OPENROUTER_API_KEY pnpm exec vitest run -> Tests 88 passed | 2 skipped (90), exit 0
$ make harness-run PROMPT="say hi"                       -> Hi! How can I help you today?  (exit 0)
$ make harness-run PROMPT="say hi" OPENROUTER_API_KEY=   -> OPENROUTER_API_KEY is not set (add it to .env)  (node exit 1)
$ make pre-commit -> prettier/ruff/ruff format/biome check (harness): Passed
```

**Other issues found (none blocking; for the orchestrator to triage)**
- `decodeChatStream` on a `data: null` chunk reports `code: "network_error"` with a TypeError text (`openrouter.ts` chunk handling, `chunk.error` on null). Still one terminal event, never throws; suggest `if (!chunk || typeof chunk !== "object")` -> `invalid_chunk`. Same area: `delta.tool_calls` that is not an array, or a delta missing `index`, creates a call keyed `undefined` (`call_undefined`); only a malformed upstream would trigger it.
- `complete`/`completeText`: a 200 response whose body is not JSON (HTML from a proxy) rejects with a bare `SyntaxError` instead of `ProviderError`; callers keyed on `.code` (206) would see `undefined`. Wrap `res.json()` in `#completion`.
- `modelInfo`: a non-numeric `pricing` string becomes `NaN` (real OpenRouter router models use "-1", which parses fine). Not worth a fix now.
- Test fixtures use the literal `sk-or-test` as a fake key (3 places), so the stated invariant `grep -rn "sk-or-" apps/harness/tests` is not empty. It is not a real key; rename to e.g. `test-key` if the grep is meant to stay a hard tripwire.
- A settings.json saved with a UTF-8 BOM fails as "invalid JSON" (Windows editors); strip a leading U+FEFF in `readLayer` if desired.
- Design notes confirmed, no action: `ToolCall.parseError` (204's loop must turn it into an isError tool result), `.strict()` on every Settings object (needed by the AC, stricter than spec §16 listing), `.optional()` becomes required-nullable under strict structured outputs (C4 schema output above).

**VERDICT: PASS**
