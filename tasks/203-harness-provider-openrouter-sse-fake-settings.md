---
id: 203-harness-provider-openrouter-sse-fake-settings
status: pending
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

- [ ] `sse.ts`: skips `:` comment lines; `data: [DONE]` ends the iterable; a mid-stream chunk with a top-level
      `error` yields a `StreamEvent error` and nothing after it; the trailing usage chunk does not produce a
      second `done`; tool-call deltas split across chunks are reassembled by `index`; invalid JSON arguments
      yield an assistant message whose call is marked for an `isError` result, never a throw.
- [ ] `openrouter.ts` offline (injected `fetch`): the request body carries `messages[0].role === "system"`,
      `stream: true`, `stream_options.include_usage`, the `tools` array also on the request that carries tool
      results, assistant `tool_calls[].function.arguments` as a JSON string with `content: null`, and
      `{role:"tool", tool_call_id}` results; headers include `Authorization`, `HTTP-Referer`, `X-Title`.
- [ ] `finish_reason` maps `stop|tool_calls|length|error`; an HTTP 400 whose message contains "context length"
      yields `error` with `code: "context_length_exceeded"`; other 4xx/5xx carry the body's error code.
- [ ] `complete<T>` sends `response_format: {type: "json_schema", json_schema: {name, strict: true, schema}}`
      and `provider: {require_parameters: true}`; a first answer that fails `schema.parse` triggers exactly one
      retry whose input ends with the parse error; a second failure rejects.
- [ ] `modelInfo` hits `GET /api/v1/models` once per process (second call served from cache) and maps
      `contextLength`, `maxOutputTokens`, `supported`, `pricing`; an unknown id rejects with a message naming it.
- [ ] A `decisionModel` whose `supported_parameters` lacks `structured_outputs` makes startup exit 1 with one
      line naming the model (FakeProvider / fake models list).
- [ ] `Settings.parse({})` equals the §16 defaults (`model` `anthropic/claude-sonnet-5.5`, `decisionModel`
      `anthropic/claude-haiku-5.5`, `compaction.triggerRatio` 0.8, `codemode.llm.depth` 1 …);
      `TREE_AGENT_HOME=/tmp/x` makes every path resolve under `/tmp/x`; a `settings.json` with an unknown
      key or `triggerRatio: 0.2` fails with the zod issue list.
- [ ] `FakeProvider` replays scripted events in order and exposes the recorded `ChatRequest`s; every later
      task's loop/permission/compaction tests build on it.
- [ ] Live (`describe.skipIf(!process.env.OPENROUTER_API_KEY)`): one streamed turn with a tool call on the
      main model; `complete<T>` returns schema-valid JSON on the decision model. Both report as skipped when
      the key is absent (negative: `env -u OPENROUTER_API_KEY pnpm exec vitest run` is green with 2 skips).
- [ ] [HUMAN] `make harness-run PROMPT="say hi"` prints a greeting and exits 0; with the key blanked it prints
      one line naming `OPENROUTER_API_KEY` and exits 1.
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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
