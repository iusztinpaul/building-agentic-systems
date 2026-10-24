import { describe, expect, it } from "vitest";
import { z } from "zod";
import { FakeProvider, fakeDone, fakeModel } from "../../src/provider/fake.ts";
import type { ChatRequest, StreamEvent } from "../../src/provider/types.ts";
import { collect } from "../helpers/http.ts";

function request(content: string): ChatRequest {
  return { model: "m", context: { system: "s", messages: [{ role: "user", content }] }, tools: [] };
}

const signal = () => new AbortController().signal;

describe("FakeProvider.stream", () => {
  it("replays one script per call, in order, and records every request", async () => {
    const first: StreamEvent[] = [{ type: "text_delta", text: "a" }, fakeDone("a")];
    const second: StreamEvent[] = [{ type: "text_delta", text: "b" }, fakeDone("b")];
    const fake = new FakeProvider({ streams: [first, second] });

    const run1 = await collect(fake.stream(request("one"), signal()));
    const run2 = await collect(fake.stream(request("two"), signal()));

    expect(run1).toEqual(first);
    expect(run2).toEqual(second);
    expect(fake.requests.map((r) => r.context.messages[0])).toEqual([
      { role: "user", content: "one" },
      { role: "user", content: "two" },
    ]);
  });

  it("records a snapshot, so later mutation of the caller's context does not rewrite history", async () => {
    const fake = new FakeProvider({ streams: [[fakeDone("ok")]] });
    const req = request("one");

    await collect(fake.stream(req, signal()));
    req.context.messages.push({ role: "user", content: "later" });

    expect(fake.requests[0]?.context.messages).toHaveLength(1);
  });

  it("throws when a call has no script left", async () => {
    const fake = new FakeProvider();

    await expect(collect(fake.stream(request("x"), signal()))).rejects.toThrow(
      "FakeProvider: no stream scripted for call 1",
    );
  });

  it("ends with an aborted done keeping the partial text when the signal fires mid-script", async () => {
    const controller = new AbortController();
    const fake = new FakeProvider({
      streams: [
        [
          { type: "text_delta", text: "Par" },
          { type: "text_delta", text: "tial" },
          fakeDone("Partial"),
        ],
      ],
    });

    const events: StreamEvent[] = [];
    for await (const event of fake.stream(request("x"), controller.signal)) {
      events.push(event);
      controller.abort();
    }

    expect(events).toEqual([
      { type: "text_delta", text: "Par" },
      {
        type: "done",
        message: { role: "assistant", content: "Par", toolCalls: [], stopReason: "aborted" },
      },
    ]);
  });

  it("fakeDone marks tool-call messages with stopReason tool_calls", () => {
    const call = { id: "c1", name: "read", arguments: { path: "a" } };

    expect(fakeDone("", [call])).toMatchObject({
      message: { stopReason: "tool_calls", toolCalls: [call] },
    });
    expect(fakeDone("hi")).toMatchObject({ message: { stopReason: "stop", toolCalls: [] } });
  });
});

describe("FakeProvider.complete / completeText", () => {
  const Verdict = z.object({ dangerous: z.boolean() });
  const structured = { model: "m", system: "s", input: "i", schema: Verdict, name: "v" };

  it("returns scripted answers validated by the request's schema and records the request", async () => {
    const fake = new FakeProvider({ answers: [{ dangerous: true }] });

    await expect(fake.complete(structured)).resolves.toEqual({ dangerous: true });
    expect(fake.structuredRequests).toEqual([structured]);
  });

  it("rejects a scripted Error and a schema-invalid answer", async () => {
    const fake = new FakeProvider({ answers: [new Error("timeout"), { dangerous: "yes" }] });

    await expect(fake.complete(structured)).rejects.toThrow("timeout");
    await expect(fake.complete(structured)).rejects.toThrow(/Expected boolean/);
  });

  it("computes a function answer from the request and its signal, then validates it", async () => {
    const seen: unknown[] = [];
    const fake = new FakeProvider({
      answers: [
        async (req: { input: string }, signal?: AbortSignal) => {
          seen.push(req.input, signal);
          return { dangerous: req.input === "i" };
        },
        () => ({ dangerous: "no" }),
      ],
    });
    const signal = new AbortController().signal;

    await expect(fake.complete(structured, signal)).resolves.toEqual({ dangerous: true });
    await expect(fake.complete(structured)).rejects.toThrow(/Expected boolean/);
    expect(seen).toEqual(["i", signal]);
  });

  it("returns scripted texts with estimated usage", async () => {
    const fake = new FakeProvider({ texts: ["summary"] });

    const result = await fake.completeText({ model: "m", input: "12345678" });

    expect(result).toEqual({ text: "summary", usage: { inputTokens: 2, outputTokens: 2 } });
    expect(fake.textRequests).toEqual([{ model: "m", input: "12345678" }]);
  });
});

describe("FakeProvider.modelInfo", () => {
  it("resolves any id to a capable fake model by default", async () => {
    const info = await new FakeProvider().modelInfo("any/model");

    expect(info.id).toBe("any/model");
    expect(info.supported.has("structured_outputs")).toBe(true);
  });

  it("serves only the scripted models when given a list", async () => {
    const weak = fakeModel("weak/model", { supported: new Set(["tools"]) });
    const fake = new FakeProvider({ models: [weak] });

    await expect(fake.modelInfo("weak/model")).resolves.toBe(weak);
    await expect(fake.modelInfo("other")).rejects.toThrow('unknown model "other"');
  });
});
