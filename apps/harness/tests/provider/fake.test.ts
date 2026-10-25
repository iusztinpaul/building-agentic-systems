import { describe, expect, it } from "vitest";
import { z } from "zod";
import { FakeProvider, fakeDecisionModel, fakeDone, fakeModel } from "../../src/provider/fake.ts";
import type { ChatRequest, DecisionsRequest, StreamEvent } from "../../src/provider/types.ts";
import { verdict } from "../helpers/decisions.ts";
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

describe("FakeProvider.decisions", () => {
  const ask: DecisionsRequest = {
    model: "typesafe/jev-1.14",
    state: { tool: "bash" },
    questions: {
      dangerous: { type: "noul", instructions: "Dangerous?" },
      risk: { type: "choice", instructions: "Risk?", criteria: { low: null, high: null } },
    },
  };

  it("returns scripted wire bodies parsed into DecisionsResponse and records each request", async () => {
    const fake = new FakeProvider({ decisions: [verdict(0.95, "high", { cost: 0.00002 })] });

    const res = await fake.decisions(ask);

    expect(res).toEqual({
      id: "gen-dec-fake",
      model: "typesafe/jev-1.14-20261014",
      provider: "TypeSafe",
      answers: {
        dangerous: { type: "noul", noul: 0.95 },
        risk: { type: "choice", choice: "high", confidence: 1, probabilities: { high: 1 } },
      },
      usage: { inputTokens: 500, outputTokens: 0, cost: 0.00002 },
    });
    expect(fake.decisionRequests).toEqual([ask]);
  });

  it("rejects a scripted Error, an invalid shape and a missing answer with invalid_response", async () => {
    const fake = new FakeProvider({
      decisions: [
        new Error("503"),
        { ...verdict(0.5), answers: { ...verdict(0.5).answers, dangerous: { noul: 1.2 } } },
        { ...verdict(0.5), answers: { dangerous: { type: "noul", noul: 0.5 } } },
      ],
    });

    await expect(fake.decisions(ask)).rejects.toThrow("503");
    await expect(fake.decisions(ask)).rejects.toMatchObject({ code: "invalid_response" });
    await expect(fake.decisions(ask)).rejects.toThrow('decisions: no choice answer for "risk"');
  });

  it("computes a function answer from the request and its signal; one may never settle", async () => {
    const seen: unknown[] = [];
    const fake = new FakeProvider({
      decisions: [
        async (req: DecisionsRequest, signal?: AbortSignal) => {
          seen.push(req.model, signal);
          await new Promise((r) => setTimeout(r, 10));
          return verdict(0.02);
        },
        () => new Promise(() => {}),
      ],
    });
    const signal = new AbortController().signal;

    await expect(fake.decisions(ask, signal)).resolves.toMatchObject({
      answers: { dangerous: { noul: 0.02 } },
    });
    const never = await Promise.race([
      fake.decisions(ask).then(() => "settled"),
      new Promise((r) => setTimeout(() => r("pending"), 20)),
    ]);
    expect(never).toBe("pending");
    expect(seen).toEqual(["typesafe/jev-1.14", signal]);
  });

  it("throws when no decisions answer is scripted", async () => {
    await expect(new FakeProvider().decisions(ask)).rejects.toThrow(
      "FakeProvider: no decisions() answer scripted",
    );
  });
});

describe("FakeProvider.modelInfo", () => {
  it("resolves any id to a capable fake chat model by default", async () => {
    const info = await new FakeProvider().modelInfo("any/model");

    expect(info.id).toBe("any/model");
    expect(info.supported.has("structured_outputs")).toBe(true);
    expect(info.isDecisionModel).toBe(false);
  });

  it("resolves the default decisionModel to a fake decision model by default", async () => {
    const info = await new FakeProvider().modelInfo("typesafe/jev-1.14");

    expect(info).toEqual(fakeDecisionModel("typesafe/jev-1.14"));
    expect(info).toMatchObject({
      isDecisionModel: true,
      supported: new Set(),
      pricing: { prompt: 0.000000042, completion: 0 },
    });
  });

  it("serves only the scripted models when given a list", async () => {
    const weak = fakeModel("weak/model", { supported: new Set(["tools"]) });
    const fake = new FakeProvider({ models: [weak] });

    await expect(fake.modelInfo("weak/model")).resolves.toBe(weak);
    await expect(fake.modelInfo("other")).rejects.toThrow('unknown model "other"');
  });
});
