import { describe, expect, it } from "vitest";
import { z } from "zod";
import {
  CONTEXT_LENGTH_EXCEEDED,
  OpenRouterProvider,
  toStopReason,
} from "../../src/provider/openrouter.ts";
import type { ChatRequest, StreamEvent, ToolSchema } from "../../src/provider/types.ts";
import { ProviderError } from "../../src/provider/types.ts";
import type { Message } from "../../src/types.ts";
import { collect, fakeFetch, fixture, jsonResponse, sse, sseResponse } from "../helpers/http.ts";

const MAIN = "anthropic/claude-sonnet-5.5";
const DECISION = "anthropic/claude-haiku-5.5";

const SEARCH: ToolSchema = {
  name: "search_memory",
  description: "Search the personal memory.",
  parameters: {
    type: "object",
    properties: { query: { type: "string" } },
    required: ["query"],
    additionalProperties: false,
  },
};

// The second request of a tool round trip: user → assistant tool call → tool result.
const TOOL_ROUND_TRIP: Message[] = [
  { role: "user", content: "Search memory for pricing" },
  {
    role: "assistant",
    content: "",
    toolCalls: [{ id: "toolu_01", name: "search_memory", arguments: { query: "pricing" } }],
    stopReason: "tool_calls",
  },
  {
    role: "tool",
    toolCallId: "toolu_01",
    name: "search_memory",
    content: "Pricing: $5/month.",
    isError: false,
  },
];

function chatRequest(overrides: Partial<ChatRequest> = {}): ChatRequest {
  return {
    model: MAIN,
    context: { system: "You are tree-agent.", messages: [{ role: "user", content: "hi" }] },
    tools: [],
    ...overrides,
  };
}

function provider(fetch: typeof globalThis.fetch): OpenRouterProvider {
  return new OpenRouterProvider({ apiKey: "test-key", fetch });
}

function chunk(delta: Record<string, unknown>, finish: string | null = null) {
  return { choices: [{ index: 0, delta, finish_reason: finish }] };
}

function completion(content: string | null) {
  return {
    choices: [{ message: { role: "assistant", content } }],
    usage: { prompt_tokens: 12, completion_tokens: 3 },
  };
}

async function streamWith(response: () => Response, req = chatRequest()) {
  const http = fakeFetch(response);
  const events = await collect(provider(http.fetch).stream(req, new AbortController().signal));
  return { events, calls: http.calls };
}

describe("OpenRouterProvider.stream request", () => {
  it("sends the chat-completions wire shape on a request that carries tool results", async () => {
    const req = chatRequest({
      tools: [SEARCH],
      context: { system: "You are tree-agent.", messages: TOOL_ROUND_TRIP },
    });

    const { calls } = await streamWith(() => sseResponse(fixture("say-hi.recorded.sse")), req);

    expect(calls).toHaveLength(1);
    const [call] = calls;
    expect(call?.url).toBe("https://openrouter.ai/api/v1/chat/completions");
    expect(call?.method).toBe("POST");
    const body = call?.body;
    expect(body.model).toBe(MAIN);
    expect(body.stream).toBe(true);
    expect(body.stream_options).toEqual({ include_usage: true });
    expect(body.messages[0]).toEqual({ role: "system", content: "You are tree-agent." });
    expect(body.messages[1]).toEqual({ role: "user", content: "Search memory for pricing" });
    expect(body.messages[2]).toEqual({
      role: "assistant",
      content: null,
      tool_calls: [
        {
          id: "toolu_01",
          type: "function",
          function: { name: "search_memory", arguments: '{"query":"pricing"}' },
        },
      ],
    });
    expect(body.messages[3]).toEqual({
      role: "tool",
      tool_call_id: "toolu_01",
      content: "Pricing: $5/month.",
    });
    expect(body.tools).toEqual([
      {
        type: "function",
        function: {
          name: "search_memory",
          description: "Search the personal memory.",
          parameters: SEARCH.parameters,
        },
      },
    ]);
  });

  it("sends the Authorization, HTTP-Referer and X-Title headers", async () => {
    const { calls } = await streamWith(() => sseResponse(fixture("say-hi.recorded.sse")));

    const headers = calls[0]?.headers ?? {};
    expect(headers.Authorization).toBe("Bearer test-key");
    expect(headers["HTTP-Referer"]).toMatch(/^https:\/\//);
    expect(headers["X-Title"]).toBe("tree-agent");
    expect(headers["Content-Type"]).toBe("application/json");
  });

  it("omits the tools key when there are no tools and maps maxOutputTokens to max_tokens", async () => {
    const { calls } = await streamWith(
      () => sseResponse(fixture("say-hi.recorded.sse")),
      chatRequest({ maxOutputTokens: 256 }),
    );

    expect(calls[0]?.body).not.toHaveProperty("tools");
    expect(calls[0]?.body.max_tokens).toBe(256);
  });

  it("keeps assistant text next to its tool calls and serialises a parseError call as {}", async () => {
    const messages: Message[] = [
      { role: "user", content: "go" },
      {
        role: "assistant",
        content: "Let me look.",
        toolCalls: [{ id: "c1", name: "read", arguments: {}, parseError: "invalid JSON" }],
        stopReason: "tool_calls",
      },
    ];

    const { calls } = await streamWith(
      () => sseResponse(fixture("say-hi.recorded.sse")),
      chatRequest({ context: { system: "s", messages } }),
    );

    expect(calls[0]?.body.messages[2]).toMatchObject({
      content: "Let me look.",
      tool_calls: [{ function: { name: "read", arguments: "{}" } }],
    });
  });
});

describe("OpenRouterProvider.stream finish and errors", () => {
  it.each([
    ["stop", "stop"],
    ["tool_calls", "tool_calls"],
    ["length", "length"],
    ["error", "error"],
  ])("maps finish_reason %s to stopReason %s", async (finish, stopReason) => {
    const body = sse(chunk({ content: "x" }), chunk({}, finish));

    const { events } = await streamWith(() => sseResponse(body));

    const done = events.at(-1);
    expect(done?.type).toBe("done");
    expect(done?.type === "done" && done.message.stopReason).toBe(stopReason);
  });

  it("maps content_filter to error and unknown reasons to stop", () => {
    expect(toStopReason("content_filter")).toBe("error");
    expect(toStopReason("end_turn")).toBe("stop");
  });

  it("turns the recorded HTTP 400 context-overflow body into context_length_exceeded", async () => {
    const overflow = {
      error: {
        message:
          "This endpoint's maximum context length is 16384 tokens. However, you requested about 18010 tokens (18000 of text input, 10 in the output). Please reduce the length of either one, or use the context-compression plugin to compress your prompt automatically.",
        code: 400,
        metadata: { provider_name: null },
      },
    };

    const { events } = await streamWith(() => jsonResponse(overflow, 400));

    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ type: "error", code: CONTEXT_LENGTH_EXCEEDED });
    expect(events[0]?.type === "error" && events[0].message).toMatch(/maximum context length/);
  });

  it("finds a context overflow hidden in the upstream metadata.raw of a 400", async () => {
    const body = {
      error: {
        message: "Provider returned error",
        code: 400,
        metadata: { raw: '{"message":"prompt is too long: exceeds the context length"}' },
      },
    };

    const { events } = await streamWith(() => jsonResponse(body, 400));

    expect(events[0]).toMatchObject({ type: "error", code: CONTEXT_LENGTH_EXCEEDED });
  });

  it.each([
    [401, { error: { code: 401, message: "User not found." } }, "401", "User not found."],
    [402, { error: { code: 402, message: "Insufficient credits" } }, "402", "Insufficient credits"],
    [429, { error: { code: 429, message: "Rate limited" } }, "429", "Rate limited"],
    [
      400,
      { error: { code: 400, message: "Provider returned error" } },
      "400",
      "Provider returned error",
    ],
  ])("carries the body's error code for HTTP %i", async (status, body, code, message) => {
    const { events } = await streamWith(() => jsonResponse(body, status));

    expect(events).toEqual([{ type: "error", code, message }]);
  });

  it("falls back to the HTTP status when a 5xx body is not JSON", async () => {
    const { events } = await streamWith(
      () => new Response("<html>Bad gateway</html>", { status: 502 }),
    );

    expect(events).toEqual([{ type: "error", code: "502", message: "<html>Bad gateway</html>" }]);
  });

  it("reports a fetch failure as a network_error event instead of throwing", async () => {
    const { events } = await streamWith(() => {
      throw new TypeError("fetch failed");
    });

    expect(events).toEqual([{ type: "error", code: "network_error", message: "fetch failed" }]);
  });

  it("ends an aborted stream with a done that keeps the partial text", async () => {
    const controller = new AbortController();
    const http = fakeFetch((call) => {
      const first = new TextEncoder().encode(
        `data: ${JSON.stringify(chunk({ content: "Par" }))}\n\n`,
      );
      const body = new ReadableStream<Uint8Array>({
        start(stream) {
          stream.enqueue(first);
          call.signal?.addEventListener("abort", () =>
            stream.error(new DOMException("aborted", "AbortError")),
          );
        },
      });
      return new Response(body, { status: 200 });
    });

    const events: StreamEvent[] = [];
    for await (const event of provider(http.fetch).stream(chatRequest(), controller.signal)) {
      events.push(event);
      if (event.type === "text_delta") controller.abort();
    }

    expect(events).toEqual([
      { type: "text_delta", text: "Par" },
      {
        type: "done",
        message: { role: "assistant", content: "Par", toolCalls: [], stopReason: "aborted" },
      },
    ]);
  });

  it("ends with an aborted done when the signal fires before the response", async () => {
    const controller = new AbortController();
    controller.abort();
    const http = fakeFetch(() => {
      throw new DOMException("aborted", "AbortError");
    });

    const events = await collect(provider(http.fetch).stream(chatRequest(), controller.signal));

    expect(events).toEqual([
      {
        type: "done",
        message: { role: "assistant", content: "", toolCalls: [], stopReason: "aborted" },
      },
    ]);
  });
});

const Verdict = z.object({ dangerous: z.boolean(), reason: z.string() });

describe("OpenRouterProvider.complete", () => {
  it("sends a strict json_schema response_format with require_parameters", async () => {
    const http = fakeFetch(() => jsonResponse(completion('{"dangerous":false,"reason":"ok"}')));

    const verdict = await provider(http.fetch).complete({
      model: DECISION,
      system: "Classify.",
      input: "rm -rf build",
      schema: Verdict,
      name: "danger_check",
      maxOutputTokens: 200,
    });

    expect(verdict).toEqual({ dangerous: false, reason: "ok" });
    expect(http.calls).toHaveLength(1);
    const body = http.calls[0]?.body;
    expect(body).not.toHaveProperty("stream");
    expect(body.model).toBe(DECISION);
    expect(body.max_tokens).toBe(200);
    expect(body.messages).toEqual([
      { role: "system", content: "Classify." },
      { role: "user", content: "rm -rf build" },
    ]);
    expect(body.provider).toEqual({ require_parameters: true });
    expect(body.response_format).toEqual({
      type: "json_schema",
      json_schema: {
        name: "danger_check",
        strict: true,
        schema: {
          type: "object",
          properties: { dangerous: { type: "boolean" }, reason: { type: "string" } },
          required: ["dangerous", "reason"],
          additionalProperties: false,
        },
      },
    });
  });

  it("retries once with the parse error appended when the first answer fails the schema", async () => {
    const answers = ['{"dangerous":"maybe","reason":"?"}', '{"dangerous":true,"reason":"deletes"}'];
    const http = fakeFetch((_call, i) => jsonResponse(completion(answers[i] ?? null)));

    const verdict = await provider(http.fetch).complete({
      model: DECISION,
      system: "Classify.",
      input: "rm -rf /",
      schema: Verdict,
      name: "danger_check",
    });

    expect(verdict).toEqual({ dangerous: true, reason: "deletes" });
    expect(http.calls).toHaveLength(2);
    const retryInput: string = http.calls[1]?.body.messages[1].content;
    expect(retryInput.startsWith("rm -rf /")).toBe(true);
    expect(retryInput).toMatch(/Error: dangerous: Expected boolean, received string$/);
  });

  it("retries when the first answer is not JSON at all", async () => {
    const answers = ["Sure! Here you go", '{"dangerous":false,"reason":"fine"}'];
    const http = fakeFetch((_call, i) => jsonResponse(completion(answers[i] ?? null)));

    const verdict = await provider(http.fetch).complete({
      model: DECISION,
      system: "s",
      input: "ls",
      schema: Verdict,
      name: "danger_check",
    });

    expect(verdict.dangerous).toBe(false);
    expect(http.calls[1]?.body.messages[1].content).toMatch(/Error: .*JSON/);
  });

  it("rejects after the retry also fails, with exactly two requests sent", async () => {
    const http = fakeFetch(() => jsonResponse(completion('{"dangerous":1}')));

    const result = provider(http.fetch).complete({
      model: DECISION,
      system: "s",
      input: "ls",
      schema: Verdict,
      name: "danger_check",
    });

    await expect(result).rejects.toMatchObject({
      name: "ProviderError",
      code: "invalid_structured_output",
      message: expect.stringMatching(/^danger_check: dangerous: Expected boolean/),
    });
    expect(http.calls).toHaveLength(2);
  });

  it("rejects an HTTP error with its code and does not retry", async () => {
    const http = fakeFetch(() =>
      jsonResponse({ error: { code: 404, message: "No endpoints" } }, 404),
    );

    const result = provider(http.fetch).complete({
      model: DECISION,
      system: "s",
      input: "ls",
      schema: Verdict,
      name: "danger_check",
    });

    await expect(result).rejects.toEqual(new ProviderError("404", "No endpoints"));
    expect(http.calls).toHaveLength(1);
  });
});

describe("OpenRouterProvider.completeText", () => {
  it("sends a plain non-streaming request and returns the text with usage", async () => {
    const http = fakeFetch(() => jsonResponse(completion("a summary")));

    const result = await provider(http.fetch).completeText({
      model: DECISION,
      system: "Summarise.",
      input: "long text",
    });

    expect(result).toEqual({ text: "a summary", usage: { inputTokens: 12, outputTokens: 3 } });
    const body = http.calls[0]?.body;
    expect(body.messages).toEqual([
      { role: "system", content: "Summarise." },
      { role: "user", content: "long text" },
    ]);
    expect(body).not.toHaveProperty("response_format");
    expect(body).not.toHaveProperty("stream");
  });

  it("sends no system message when none is given", async () => {
    const http = fakeFetch(() => jsonResponse(completion("x")));

    await provider(http.fetch).completeText({ model: DECISION, input: "hi" });

    expect(http.calls[0]?.body.messages).toEqual([{ role: "user", content: "hi" }]);
  });
});

describe("OpenRouterProvider malformed upstream responses", () => {
  it.each([
    ["null", null],
    ["a number", 42],
    ["an array", []],
    ["a string", "x"],
  ])("ends the stream with invalid_chunk when a chunk is %s", async (_label, value) => {
    const { events } = await streamWith(() => sseResponse(sse(chunk({ content: "Hi" }), value)));

    expect(events).toEqual([
      { type: "text_delta", text: "Hi" },
      {
        type: "error",
        code: "invalid_chunk",
        message: `unexpected stream chunk: ${JSON.stringify(value)}`,
      },
    ]);
  });

  it.each([
    ["tool_calls is not an array", { tool_calls: "oops" }],
    ["a tool_call delta has no index", { tool_calls: [{ id: "x", function: { name: "read" } }] }],
    ["a tool_call delta is null", { tool_calls: [null] }],
  ])("ignores the delta and keeps streaming when %s", async (_label, delta) => {
    const valid = { index: 0, id: "toolu_1", function: { name: "read", arguments: "{}" } };
    const body = sse(
      chunk(delta),
      chunk({ content: "ok" }),
      chunk({ tool_calls: [valid] }),
      chunk({}, "tool_calls"),
    );

    const { events } = await streamWith(() => sseResponse(body));

    expect(events.filter((e) => e.type === "tool_call_start")).toEqual([
      { type: "tool_call_start", index: 0, id: "toolu_1", name: "read" },
    ]);
    expect(events.at(-1)).toEqual({
      type: "done",
      message: {
        role: "assistant",
        content: "ok",
        toolCalls: [{ id: "toolu_1", name: "read", arguments: {} }],
        stopReason: "tool_calls",
      },
    });
  });

  it("rejects complete with invalid_response on a 200 non-JSON body, without a retry", async () => {
    const http = fakeFetch(() => new Response("<html>proxy</html>", { status: 200 }));

    const result = provider(http.fetch).complete({
      model: DECISION,
      system: "s",
      input: "ls",
      schema: Verdict,
      name: "danger_check",
    });

    await expect(result).rejects.toMatchObject({
      name: "ProviderError",
      code: "invalid_response",
      message: "response body is not a JSON object: <html>proxy</html>",
    });
    expect(http.calls).toHaveLength(1);
  });

  it.each([
    ["an HTML page", "<html>proxy</html>"],
    ["JSON null", "null"],
  ])("rejects completeText with invalid_response when a 200 body is %s", async (_label, text) => {
    const http = fakeFetch(() => new Response(text, { status: 200 }));

    const result = provider(http.fetch).completeText({ model: DECISION, input: "hi" });

    await expect(result).rejects.toBeInstanceOf(ProviderError);
    await expect(result).rejects.toMatchObject({ code: "invalid_response" });
  });
});

// Trimmed copies of GET /api/v1/models entries recorded 2026-10-24.
const MODELS = {
  data: [
    {
      id: MAIN,
      context_length: 1_000_000,
      top_provider: { context_length: 1_000_000, max_completion_tokens: 128_000 },
      supported_parameters: ["max_tokens", "response_format", "structured_outputs", "tools"],
      pricing: { prompt: "0.000002", completion: "0.00001", web_search: "0.01" },
    },
    {
      id: "vendor/no-limit",
      context_length: 8192,
      top_provider: { context_length: 8192, max_completion_tokens: null },
      supported_parameters: ["tools"],
      pricing: { prompt: "0", completion: "0" },
    },
  ],
};

describe("OpenRouterProvider.modelInfo", () => {
  it("maps context_length, max_completion_tokens, supported_parameters and pricing", async () => {
    const http = fakeFetch(() => jsonResponse(MODELS));

    const info = await provider(http.fetch).modelInfo(MAIN);

    expect(info).toEqual({
      id: MAIN,
      contextLength: 1_000_000,
      maxOutputTokens: 128_000,
      supported: new Set(["max_tokens", "response_format", "structured_outputs", "tools"]),
      pricing: { prompt: 0.000002, completion: 0.00001 },
    });
    expect(http.calls[0]).toMatchObject({
      url: "https://openrouter.ai/api/v1/models",
      method: "GET",
    });
  });

  it("hits GET /api/v1/models once and serves later lookups from the cache", async () => {
    const http = fakeFetch(() => jsonResponse(MODELS));
    const openrouter = provider(http.fetch);

    await Promise.all([openrouter.modelInfo(MAIN), openrouter.modelInfo(MAIN)]);
    await openrouter.modelInfo("vendor/no-limit");

    expect(http.calls).toHaveLength(1);
  });

  it("falls back to the context length when max_completion_tokens is null", async () => {
    const http = fakeFetch(() => jsonResponse(MODELS));

    const info = await provider(http.fetch).modelInfo("vendor/no-limit");

    expect(info.maxOutputTokens).toBe(8192);
  });

  it("rejects an unknown id with a message naming it", async () => {
    const http = fakeFetch(() => jsonResponse(MODELS));

    await expect(provider(http.fetch).modelInfo("acme/nope")).rejects.toThrow(
      'unknown model "acme/nope": not listed by GET /api/v1/models',
    );
  });

  it("does not cache a failed models request", async () => {
    const http = fakeFetch((_call, i) =>
      i === 0 ? new Response("down", { status: 503 }) : jsonResponse(MODELS),
    );
    const openrouter = provider(http.fetch);

    await expect(openrouter.modelInfo(MAIN)).rejects.toMatchObject({ code: "503" });
    await expect(openrouter.modelInfo(MAIN)).resolves.toMatchObject({ id: MAIN });
    expect(http.calls).toHaveLength(2);
  });
});

// Live: real OpenRouter, skipped without the key (CI never has it).
describe.skipIf(!process.env.OPENROUTER_API_KEY)("OpenRouter live", () => {
  const live = new OpenRouterProvider({ apiKey: process.env.OPENROUTER_API_KEY ?? "" });

  it("streams a turn with a tool call on the main model, then answers from the result", async () => {
    const signal = AbortSignal.timeout(60_000);
    const system = "You are a memory assistant. Always call search_memory before answering.";
    const first: Message[] = [{ role: "user", content: "What did I note about pricing?" }];

    const turn1 = await collect(
      live.stream(
        {
          model: MAIN,
          context: { system, messages: first },
          tools: [SEARCH],
          maxOutputTokens: 300,
        },
        signal,
      ),
    );
    const done1 = turn1.at(-1);
    if (done1?.type !== "done") throw new Error(`turn 1 ended with ${JSON.stringify(done1)}`);
    const [call] = done1.message.toolCalls;
    expect(call?.name).toBe("search_memory");
    expect(typeof call?.arguments.query).toBe("string");
    expect(call?.parseError).toBeUndefined();

    const second: Message[] = [
      ...first,
      done1.message,
      {
        role: "tool",
        toolCallId: call?.id ?? "",
        name: "search_memory",
        content: "2026-10-01: pricing is $5/month for the starter plan.",
        isError: false,
      },
    ];
    const turn2 = await collect(
      live.stream(
        {
          model: MAIN,
          context: { system, messages: second },
          tools: [SEARCH],
          maxOutputTokens: 300,
        },
        signal,
      ),
    );
    const done2 = turn2.at(-1);
    if (done2?.type !== "done") throw new Error(`turn 2 ended with ${JSON.stringify(done2)}`);
    expect(done2.message.content).toMatch(/\$5/);
    expect(done2.message.usage?.inputTokens).toBeGreaterThan(0);
  }, 90_000);

  it("complete<T> returns schema-valid JSON on the decision model", async () => {
    // The auto-mode classifier's schema (spec §11.1), incl. reason.max(200).
    // Strict mode does not enforce maxLength on this model (observed
    // 2026-10-24: a >200-char reason, fixed by the retry), so the prompt
    // states the limit too.
    const DangerCheck = z.object({
      dangerous: z.boolean(),
      risk: z.enum(["low", "medium", "high"]),
      reason: z.string().max(200),
    });

    const verdict = await live.complete(
      {
        model: DECISION,
        system:
          "You are a safety classifier for a coding agent. Keep `reason` to one sentence under 200 characters.",
        input: "Tool: bash\nCommand: rm -rf ~/.ssh",
        schema: DangerCheck,
        name: "danger_check",
        maxOutputTokens: 300,
      },
      AbortSignal.timeout(60_000),
    );

    expect(DangerCheck.parse(verdict)).toEqual(verdict);
    expect(verdict.dangerous).toBe(true);
  }, 90_000);
});
