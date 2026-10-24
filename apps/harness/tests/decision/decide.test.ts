import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { Settings } from "../../src/config/settings.ts";
import { DecisionTimeout, decide } from "../../src/decision/decide.ts";
import { FakeProvider } from "../../src/provider/fake.ts";
import type { StructuredRequest } from "../../src/provider/types.ts";

const Verdict = z.object({ ok: z.boolean(), why: z.string() });
const base = { name: "verdict", system: "Judge.", input: "x", schema: Verdict };

describe("decide", () => {
  it("returns the parsed answer of one complete() on the given model", async () => {
    const fake = new FakeProvider({ answers: [{ ok: true, why: "fine" }] });

    const answer = await decide(fake, { ...base, model: "test/decider" });

    expect(answer).toEqual({ ok: true, why: "fine" });
    expect(fake.structuredRequests).toEqual([
      { model: "test/decider", system: "Judge.", input: "x", schema: Verdict, name: "verdict" },
    ]);
  });

  it("defaults to the settings' default decisionModel", async () => {
    const fake = new FakeProvider({ answers: [{ ok: true, why: "" }] });

    await decide(fake, base);

    expect(fake.structuredRequests[0]?.model).toBe(Settings.parse({}).decisionModel);
    expect(fake.structuredRequests[0]?.model).toBe("anthropic/claude-haiku-5.5");
  });

  it("rejects with the provider's error", async () => {
    const fake = new FakeProvider({ answers: [new Error("upstream 503")] });

    await expect(decide(fake, base)).rejects.toThrow("upstream 503");
  });

  it("serves a cache hit without calling the provider, keyed on input by default", async () => {
    const fake = new FakeProvider({ answers: [{ ok: true, why: "first" }] });
    const cache = new Map<string, z.infer<typeof Verdict>>();

    const first = await decide(fake, { ...base, cache });
    const second = await decide(fake, { ...base, cache });

    expect(second).toEqual(first);
    expect(fake.structuredRequests).toHaveLength(1);
    expect([...cache.keys()]).toEqual(["x"]);
  });

  it("uses cacheKey instead of the input when given one", async () => {
    const fake = new FakeProvider({ answers: [{ ok: false, why: "k" }] });
    const cache = new Map<string, z.infer<typeof Verdict>>();

    await decide(fake, { ...base, input: "one", cache, cacheKey: "k" });
    const again = await decide(fake, { ...base, input: "two", cache, cacheKey: "k" });

    expect(again).toEqual({ ok: false, why: "k" });
    expect(fake.structuredRequests).toHaveLength(1);
  });

  it("does not cache a failure", async () => {
    const fake = new FakeProvider({ answers: [new Error("down"), { ok: true, why: "up" }] });
    const cache = new Map<string, z.infer<typeof Verdict>>();

    await expect(decide(fake, { ...base, cache })).rejects.toThrow("down");
    expect(await decide(fake, { ...base, cache })).toEqual({ ok: true, why: "up" });
    expect(fake.structuredRequests).toHaveLength(2);
  });
});

describe("decide with a signal", () => {
  it("rejects with the signal's reason on abort, cancels the request and caches nothing", async () => {
    const signals: (AbortSignal | undefined)[] = [];
    let answer = (_: unknown) => {};
    const fake = new FakeProvider({
      answers: [
        // Ignores the signal and answers after the abort.
        (_req: StructuredRequest<unknown>, signal?: AbortSignal) => {
          signals.push(signal);
          return new Promise((r) => {
            answer = r;
          });
        },
      ],
    });
    const cache = new Map<string, z.infer<typeof Verdict>>();
    const run = new AbortController();

    const pending = decide(fake, { ...base, cache, signal: run.signal });
    run.abort(new Error("user aborted"));
    answer({ ok: true, why: "late" });

    await expect(pending).rejects.toThrow("user aborted");
    expect(signals[0]?.aborted).toBe(true);
    await Promise.resolve();
    expect(cache.size).toBe(0);
  });

  it("rejects at once without a request when the signal is already aborted", async () => {
    const fake = new FakeProvider({ answers: [{ ok: true, why: "never" }] });
    const run = new AbortController();
    run.abort(new Error("gone"));

    await expect(decide(fake, { ...base, signal: run.signal })).rejects.toThrow("gone");
    expect(fake.structuredRequests).toEqual([]);
  });
});

describe("decide with timeoutMs", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // Never answers; records the signal it was given.
  function silent() {
    const signals: (AbortSignal | undefined)[] = [];
    const fake = new FakeProvider({
      answers: [
        (_req: StructuredRequest<unknown>, signal?: AbortSignal) => {
          signals.push(signal);
          return new Promise(() => {});
        },
      ],
    });
    return { fake, signals };
  }

  it("rejects at the deadline, not before, and aborts the request", async () => {
    const { fake, signals } = silent();
    let settled: unknown;
    const pending = decide(fake, { ...base, timeoutMs: 1_000 }).catch((err) => {
      settled = err;
    });

    await vi.advanceTimersByTimeAsync(999);
    expect(settled).toBeUndefined();
    await vi.advanceTimersByTimeAsync(1);
    await pending;

    expect(settled).toBeInstanceOf(DecisionTimeout);
    expect((settled as Error).message).toBe("verdict: no answer within 1000 ms");
    expect(signals[0]?.aborted).toBe(true);
  });

  it("returns an answer that beats the deadline and leaves no timer behind", async () => {
    const fake = new FakeProvider({ answers: [{ ok: true, why: "quick" }] });

    const answer = await decide(fake, { ...base, timeoutMs: 1_000 });

    expect(answer).toEqual({ ok: true, why: "quick" });
    expect(vi.getTimerCount()).toBe(0);
  });
});
