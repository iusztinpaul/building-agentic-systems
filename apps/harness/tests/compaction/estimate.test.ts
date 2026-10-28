import { describe, expect, it } from "vitest";
import { contextChars, messageChars, TokenEstimator } from "../../src/compaction/estimate.ts";
import type { Context, Message } from "../../src/types.ts";

const ctx = (chars: number): Context => ({
  system: "",
  messages: [{ role: "user", content: "x".repeat(chars) }],
});

describe("messageChars", () => {
  it("counts a user's text, a tool call's name + JSON args and a tool result's name + content", () => {
    const assistant: Message = {
      role: "assistant",
      content: "ok",
      toolCalls: [{ id: "c1", name: "read", arguments: { path: "a.ts" } }],
      stopReason: "tool_calls",
    };
    const tool: Message = {
      role: "tool",
      toolCallId: "c1",
      name: "read",
      content: "body",
      isError: false,
    };

    expect(messageChars({ role: "user", content: "hello" })).toBe(5);
    expect(messageChars(assistant)).toBe(2 + 4 + '{"path":"a.ts"}'.length);
    expect(messageChars(tool)).toBe(8);
    expect(contextChars({ system: "sys", messages: [assistant, tool] })).toBe(3 + 21 + 8);
  });
});

describe("TokenEstimator", () => {
  it("is chars / 4 (rounded up) before any usage", () => {
    const estimator = new TokenEstimator();

    expect(estimator.ratio).toBe(1);
    expect(estimator.estimateTokens(ctx(4_000))).toBe(1_000);
    expect(estimator.estimateTokens(ctx(5))).toBe(2);
  });

  it("applies the ratio of the last usage.inputTokens to the estimate at that time", () => {
    const estimator = new TokenEstimator();

    estimator.observe(ctx(4_000), 1_500); // estimated 1 000

    expect(estimator.ratio).toBe(1.5);
    expect(estimator.estimateTokens(ctx(8_000))).toBe(3_000);
    expect(estimator.messageTokens({ role: "user", content: "x".repeat(400) })).toBe(150);
  });

  it.each([
    [10_000, 2],
    [100, 0.5],
  ])("clamps a usage of %i for a 1 000-token estimate to %f", (inputTokens, ratio) => {
    const estimator = new TokenEstimator();

    estimator.observe(ctx(4_000), inputTokens);

    expect(estimator.ratio).toBe(ratio);
  });

  it("updates after every turn: the latest usage wins", () => {
    const estimator = new TokenEstimator();

    estimator.observe(ctx(4_000), 1_800);
    estimator.observe(ctx(4_000), 1_200);

    expect(estimator.ratio).toBe(1.2);
  });

  it("ignores a usage of 0 or an empty context, and reset() goes back to 1", () => {
    const estimator = new TokenEstimator();
    estimator.observe(ctx(0), 500);
    estimator.observe(ctx(4_000), 0);
    expect(estimator.ratio).toBe(1);

    estimator.observe(ctx(4_000), 1_500);
    estimator.reset();

    expect(estimator.ratio).toBe(1);
  });
});
