import { describe, expect, it } from "vitest";
import { OpenRouterProvider } from "../../src/provider/openrouter.ts";
import { parseSse } from "../../src/provider/sse.ts";
import type { StreamEvent } from "../../src/provider/types.ts";
import { byteStream, collect, fakeFetch, fixture, sse, sseResponse } from "../helpers/http.ts";

const REQUEST = {
  model: "anthropic/claude-sonnet-5.5",
  context: { system: "sys", messages: [{ role: "user" as const, content: "hi" }] },
  tools: [],
};

async function streamFixture(text: string, chunkSize?: number): Promise<StreamEvent[]> {
  const { fetch } = fakeFetch(() => sseResponse(text, chunkSize));
  const provider = new OpenRouterProvider({ apiKey: "test-key", fetch });
  return collect(provider.stream(REQUEST, new AbortController().signal));
}

function chunk(delta: Record<string, unknown>, finish: string | null = null) {
  return { choices: [{ index: 0, delta, finish_reason: finish }] };
}

describe("parseSse", () => {
  it("yields each event's data and skips ':' keep-alive comments", async () => {
    const payloads = await collect(parseSse(byteStream(fixture("say-hi.recorded.sse"))));

    expect(payloads).toHaveLength(3);
    expect(payloads.some((p) => p.includes("OPENROUTER PROCESSING"))).toBe(false);
    expect(JSON.parse(payloads[0] ?? "").choices[0].delta.content).toBe("Hi.");
  });

  it("ends the iterable at data: [DONE] and ignores anything after it", async () => {
    const text = "data: one\n\ndata: [DONE]\n\ndata: after\n\n";

    expect(await collect(parseSse(byteStream(text)))).toEqual(["one"]);
  });

  it.each([1, 3, 7, 64])("gives the same payloads when bytes arrive %i at a time", async (size) => {
    const text = `: OPENROUTER PROCESSING\n\ndata: {"t":"héllo 👋"}\n\ndata: two\n\ndata: [DONE]\n\n`;

    expect(await collect(parseSse(byteStream(text, size)))).toEqual(['{"t":"héllo 👋"}', "two"]);
  });

  it("accepts CRLF line endings", async () => {
    const text = ": keep-alive\r\n\r\ndata: one\r\n\r\ndata: [DONE]\r\n\r\n";

    expect(await collect(parseSse(byteStream(text)))).toEqual(["one"]);
  });

  it("joins multi-line data and ignores event/id/retry fields", async () => {
    const text = "event: message\nid: 7\nretry: 10\ndata: a\ndata:b\n\n";

    expect(await collect(parseSse(byteStream(text)))).toEqual(["a\nb"]);
  });

  it("drops an unterminated event at EOF", async () => {
    expect(await collect(parseSse(byteStream("data: one\n\ndata: {partial")))).toEqual(["one"]);
  });
});

describe("chat stream decoding (parseSse + OpenRouterProvider.stream)", () => {
  it("emits the text deltas then one error after a keep-alive and a mid-stream error chunk", async () => {
    const events = await streamFixture(fixture("keepalive-then-error.sse"));

    expect(events).toEqual([
      { type: "text_delta", text: "Hello" },
      { type: "text_delta", text: " there" },
      { type: "error", code: "server_error", message: "Provider disconnected unexpectedly" },
    ]);
  });

  it("treats an error chunk as terminal even when more chunks follow", async () => {
    const text = sse(
      { error: { code: "server_error", message: "boom" }, choices: [] },
      chunk({ content: "never" }, "stop"),
    );

    const events = await streamFixture(text);

    expect(events).toEqual([{ type: "error", code: "server_error", message: "boom" }]);
  });

  it("emits exactly one done for the recorded say-hi stream despite the trailing usage chunk", async () => {
    const events = await streamFixture(fixture("say-hi.recorded.sse"));

    expect(events.filter((e) => e.type === "done")).toHaveLength(1);
    expect(events.at(-1)).toEqual({
      type: "done",
      message: {
        role: "assistant",
        content: "Hi.",
        toolCalls: [],
        stopReason: "stop",
        usage: { inputTokens: 17, outputTokens: 6 },
      },
    });
  });

  it("reassembles a tool call whose arguments arrive in three chunks", async () => {
    const events = await streamFixture(fixture("tool-call-three-chunks.sse"), 5);

    const done = events.at(-1);
    expect(done?.type).toBe("done");
    if (done?.type !== "done") return;
    expect(done.message.toolCalls).toEqual([
      { id: "toolu_01", name: "search_memory", arguments: { query: "pricing" } },
    ]);
    expect(done.message.stopReason).toBe("tool_calls");
    expect(done.message.usage).toEqual({ inputTokens: 399, outputTokens: 50, cachedTokens: 128 });
    expect(events.filter((e) => e.type === "tool_call_start")).toEqual([
      { type: "tool_call_start", index: 0, id: "toolu_01", name: "search_memory" },
    ]);
    expect(events.filter((e) => e.type === "tool_call_delta")).toHaveLength(3);
  });

  it("reassembles the recorded OpenRouter tool-call stream", async () => {
    const events = await streamFixture(fixture("tool-call.recorded.sse"));

    const done = events.at(-1);
    if (done?.type !== "done") throw new Error(`expected done, got ${done?.type}`);
    expect(done.message.toolCalls).toEqual([
      {
        id: "toolu_01BZ6ec43sAmwsyUQrMpFUQk",
        name: "search_memory",
        arguments: { query: "pricing" },
      },
    ]);
    expect(events.filter((e) => e.type === "done")).toHaveLength(1);
  });

  it("marks a call with invalid JSON arguments with a parseError instead of throwing", async () => {
    const events = await streamFixture(fixture("invalid-tool-args.sse"));

    const done = events.at(-1);
    if (done?.type !== "done") throw new Error(`expected done, got ${done?.type}`);
    const [bad, good] = done.message.toolCalls;
    expect(done.message.content).toBe("Let me look.");
    expect(bad).toMatchObject({ id: "toolu_bad", name: "search_memory", arguments: {} });
    expect(bad?.parseError).toMatch(/^invalid JSON arguments for search_memory: /);
    expect(good).toEqual({ id: "toolu_ok", name: "read", arguments: { path: "a.md" } });
  });

  it("marks non-object JSON arguments with a parseError", async () => {
    const text = sse(
      chunk({
        tool_calls: [{ index: 0, id: "c1", function: { name: "read", arguments: "[1,2]" } }],
      }),
      chunk({}, "tool_calls"),
    );

    const done = (await streamFixture(text)).at(-1);

    if (done?.type !== "done") throw new Error("expected done");
    expect(done.message.toolCalls[0]).toEqual({
      id: "c1",
      name: "read",
      arguments: {},
      parseError: "arguments for read must be a JSON object",
    });
  });

  it("treats empty arguments as an empty object", async () => {
    const text = sse(
      chunk({ tool_calls: [{ index: 0, id: "c1", function: { name: "ls", arguments: "" } }] }),
      chunk({}, "tool_calls"),
    );

    const done = (await streamFixture(text)).at(-1);

    if (done?.type !== "done") throw new Error("expected done");
    expect(done.message.toolCalls).toEqual([{ id: "c1", name: "ls", arguments: {} }]);
  });

  it("reports a body that ends before any finish_reason as an error, not a done", async () => {
    const events = await streamFixture('data: {"choices":[{"delta":{"content":"Hal"}}]}\n\n');

    expect(events).toEqual([
      { type: "text_delta", text: "Hal" },
      { type: "error", code: "incomplete_stream", message: "stream ended before a finish_reason" },
    ]);
  });

  it("reports an unparseable data payload as an error", async () => {
    const events = await streamFixture("data: not json\n\n");

    expect(events).toEqual([
      { type: "error", code: "invalid_chunk", message: "unparseable stream chunk: not json" },
    ]);
  });
});
