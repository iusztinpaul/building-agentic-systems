import { describe, expect, it, vi } from "vitest";
import {
  Compactor,
  cutPoint,
  isOverflow,
  keepTokens,
  MAX_CONSECUTIVE_FAILURES,
  previousSummary,
  renderSummary,
  retryOnOverflow,
  type Summary,
  summaryInput,
  TOOL_RESULT_BYTES,
  UPDATE_PROMPT,
} from "../../src/compaction/compaction.ts";
import { log } from "../../src/log.ts";
import { FakeProvider, fakeDone } from "../../src/provider/fake.ts";
import { CONTEXT_LENGTH_EXCEEDED } from "../../src/provider/openrouter.ts";
import type { StreamEvent } from "../../src/provider/types.ts";
import { summaryMessage } from "../../src/session-store/jsonl.ts";
import type { AgentEvent, Context, Message, ToolCall } from "../../src/types.ts";

// Every message weighs `tokens` (its content length / 4 = its tokens).
const user = (content: string): Message => ({ role: "user", content });
const call = (id: string): ToolCall => ({ id, name: "read", arguments: { path: `${id}.ts` } });
const assistant = (content: string, ...calls: ToolCall[]): Message => ({
  role: "assistant",
  content,
  toolCalls: calls,
  stopReason: calls.length ? "tool_calls" : "stop",
});
const result = (id: string, content: string): Message => ({
  role: "tool",
  toolCallId: id,
  name: "read",
  content,
  isError: false,
});
const tokens = (n: number): string => "x".repeat(n * 4);
const weigh = (m: Message): number => Math.ceil(m.content.length / 4);

const summary: Summary = {
  goal: "Fix the failing parser tests",
  constraints: ["use pnpm"],
  progress: ["read src/parser.ts"],
  decisions: [],
  nextSteps: ["patch tokenize()", "run make harness-tests"],
  criticalContext: ["src/parser.ts", "TypeError: x is undefined"],
};

describe("keepTokens", () => {
  it("is keepRecentTokens, capped at half the trigger", () => {
    const settings = { triggerRatio: 0.8, keepRecentTokens: 20_000 };

    expect(keepTokens(settings, 1_000_000)).toBe(20_000);
    expect(keepTokens(settings, 10_000)).toBe(4_000);
  });
});

describe("cutPoint", () => {
  it("keeps ≈ keep tokens of tail and cuts at a user message", () => {
    const messages = [
      user(tokens(100)),
      assistant(tokens(5_000)),
      user(tokens(100)),
      assistant(tokens(5_000)),
      user(tokens(100)),
      assistant(tokens(5_000)),
    ];

    // Walk back 6 000: messages 5 and 4 → the user at 4 is the cut.
    expect(cutPoint(messages, 5_050, weigh)).toBe(4);
  });

  it("moves a boundary that falls inside a tool batch forward to the next user message", () => {
    const messages = [
      user(tokens(100)), // 0
      assistant("", call("a"), call("b")), // 1
      result("a", tokens(15_000)), // 2
      result("b", tokens(15_000)), // 3: the 20 000 boundary lands here
      assistant("found it"), // 4
      user(tokens(100)), // 5
      assistant(tokens(5_000)), // 6
    ];

    const cut = cutPoint(messages, 20_000, weigh);

    expect(cut).toBe(5);
    expect(messages[cut]?.role).toBe("user");
    // Nothing kept is a tool result without its assistant call.
    expect(messages.slice(cut).some((m) => m.role === "tool")).toBe(false);
  });

  it("never starts the tail on a tool result: with no user ahead, the next assistant turn", () => {
    const messages = [
      user("go"), // 0: the one prompt of an autonomous run
      assistant("", call("a")),
      result("a", tokens(6_000)),
      assistant("", call("b")), // 3
      result("b", tokens(3_000)), // 4: the boundary
      assistant("", call("c")), // 5
      result("c", tokens(1_500)),
    ];

    const cut = cutPoint(messages, 4_000, weigh);

    expect(cut).toBe(5);
    expect(messages.slice(cut).map((m) => m.role)).toEqual(["assistant", "tool"]);
  });

  it("with only tool results after the boundary, cuts at the turn that owns them", () => {
    const messages = [
      user("go"),
      assistant("", call("a")),
      result("a", tokens(6_000)),
      assistant("", call("b"), call("c")), // 3
      result("b", tokens(3_000)), // 4
      result("c", tokens(3_000)), // 5: the boundary
    ];

    expect(cutPoint(messages, 2_000, weigh)).toBe(3);
  });

  it("returns 0 (nothing to summarise) when the whole context fits in the tail", () => {
    const messages = [user(tokens(100)), assistant(tokens(100)), user("again")];

    expect(cutPoint(messages, 20_000, weigh)).toBe(0);
  });

  it("returns 0 when the head would only be the previous summary", () => {
    const messages = [summaryMessage("## Goal\nship"), user(tokens(5_000)), assistant("ok")];

    expect(cutPoint(messages, 1_000, weigh)).toBe(0);
  });
});

describe("summaries", () => {
  it("renders the six sections as markdown, (none) for an empty list", () => {
    expect(renderSummary(summary)).toBe(
      [
        "## Goal\nFix the failing parser tests",
        "## Constraints\n- use pnpm",
        "## Progress\n- read src/parser.ts",
        "## Decisions\n- (none)",
        "## Next steps\n1. patch tokenize()\n2. run make harness-tests",
        "## Critical context\n- src/parser.ts\n- TypeError: x is undefined",
      ].join("\n\n"),
    );
  });

  it("reads back the text of a <summary> message, and nothing from an ordinary one", () => {
    expect(previousSummary(summaryMessage("## Goal\nship"))).toBe("## Goal\nship");
    expect(previousSummary(user("<summary> is a tag"))).toBeUndefined();
    expect(previousSummary(assistant("x"))).toBeUndefined();
    expect(previousSummary(undefined)).toBeUndefined();
  });

  it("serialises the head with tool calls and results cut to head + tail", () => {
    const input = summaryInput(
      [user("fix it"), assistant("", call("a")), result("a", "y".repeat(10_000))],
      undefined,
    );

    expect(input).toContain("<conversation>\n[User]: fix it\n\n");
    expect(input).toContain('[Assistant tool calls]: read({"path":"a.ts"})');
    expect(input).toContain("bytes omitted");
    expect(input.length).toBeLessThan(TOOL_RESULT_BYTES + 3_000);
    expect(input).not.toContain("<previous-summary>");
  });

  it("asks to update a previous summary and appends the user's instructions", () => {
    const input = summaryInput([user("more")], "## Goal\nship", "focus on the test failures");

    expect(input).toContain("<previous-summary>\n## Goal\nship\n</previous-summary>");
    expect(input).toContain(UPDATE_PROMPT);
    expect(input.endsWith("Additional focus: focus on the test failures")).toBe(true);
  });
});

describe("Compactor", () => {
  const settings = { triggerRatio: 0.8, keepRecentTokens: 1_000 };
  const long: Context = {
    system: "sys",
    messages: [user(tokens(2_000)), assistant(tokens(2_000)), user("next"), assistant("ok")],
  };

  function compactor(answers: unknown[]) {
    const fake = new FakeProvider({ answers });
    const events: AgentEvent[] = [];
    const c = new Compactor({
      provider: fake,
      decisionModel: "test/decider",
      settings,
      emit: (e) => events.push(e),
    });
    return { c, fake, events };
  }

  it("is due at triggerRatio × contextLength of the estimate", () => {
    const { c } = compactor([]);

    expect(c.due(long, 5_000)).toBe(true); // 4 003 ≥ 4 000
    expect(c.due(long, 6_000)).toBe(false);
  });

  it("is never due on an unknown contextLength, and warns once", () => {
    const { c } = compactor([]);
    const warn = vi.spyOn(log, "warn").mockImplementation(() => {});
    try {
      expect(c.due(long, 0)).toBe(false);
      expect(c.due(long, 0)).toBe(false);

      expect(warn).toHaveBeenCalledTimes(1);
      expect(warn.mock.calls[0]?.[0]).toContain("contextLength is unknown");
    } finally {
      warn.mockRestore();
    }
  });

  it("summarises the head on the decision model with the Summary schema", async () => {
    const { c, fake } = compactor([summary]);

    const done = await c.compact(long, 100_000);

    expect(done).toEqual({
      cut: 2,
      summary: renderSummary(summary),
      before: expect.any(Number),
      after: expect.any(Number),
    });
    expect(done?.after).toBeLessThan(done?.before ?? 0);
    expect(fake.structuredRequests[0]).toMatchObject({
      name: "compaction_summary",
      model: "test/decider",
    });
  });

  it("emits one error after 3 consecutive failures, then is disabled", async () => {
    const { c, events } = compactor([
      new Error("garbage 1"),
      new Error("garbage 2"),
      new Error("garbage 3"),
    ]);

    for (let i = 0; i < MAX_CONSECUTIVE_FAILURES; i++) {
      expect(c.disabled).toBe(false);
      await expect(c.compact(long, 100_000)).rejects.toThrow(`garbage ${i + 1}`);
    }

    expect(c.disabled).toBe(true);
    expect(c.due(long, 1_000)).toBe(false);
    expect(events).toEqual([
      {
        type: "error",
        message: expect.stringContaining("compaction failed 3 times in a row (garbage 3)"),
      },
    ]);
  });

  it("a success in between resets the count", async () => {
    const { c, events } = compactor([new Error("a"), new Error("b"), summary, new Error("c")]);

    for (let i = 0; i < 4; i++) await c.compact(long, 100_000).catch(() => undefined);

    expect(c.disabled).toBe(false);
    expect(events).toEqual([]);
  });

  it("does not count an aborted summary as a failure", async () => {
    const { c } = compactor([summary, summary, summary]);
    const abort = new AbortController();
    abort.abort(new Error("aborted"));

    for (let i = 0; i < 3; i++) {
      await expect(c.compact(long, 100_000, { signal: abort.signal })).rejects.toThrow("aborted");
    }

    expect(c.disabled).toBe(false);
  });
});

describe("retryOnOverflow", () => {
  const overflow: StreamEvent = {
    type: "error",
    code: CONTEXT_LENGTH_EXCEEDED,
    message: "This endpoint's maximum context length is 10000 tokens",
  };
  const lengthAtOnce: StreamEvent = {
    type: "done",
    message: { role: "assistant", content: "", toolCalls: [], stopReason: "length" },
  };
  const request = {
    model: "m",
    context: { system: "", messages: [user("hi")] },
    tools: [],
  };

  async function collect(stream: AsyncIterable<StreamEvent>): Promise<StreamEvent[]> {
    const events: StreamEvent[] = [];
    for await (const e of stream) events.push(e);
    return events;
  }

  it("recovers and sends the request once more", async () => {
    const fake = new FakeProvider({ streams: [[overflow], [fakeDone("ok")]] });
    let recovered = 0;
    const provider = retryOnOverflow(fake, async () => ++recovered > 0);

    const events = await collect(provider.stream(request, new AbortController().signal));

    expect(recovered).toBe(1);
    expect(fake.requests).toHaveLength(2);
    expect(events).toEqual([fakeDone("ok")]);
  });

  it("a second overflow is an error, with no third request", async () => {
    const fake = new FakeProvider({ streams: [[overflow], [lengthAtOnce]] });
    const provider = retryOnOverflow(fake, async () => true);

    const events = await collect(provider.stream(request, new AbortController().signal));

    expect(fake.requests).toHaveLength(2);
    expect(events).toEqual([
      expect.objectContaining({ type: "error", code: CONTEXT_LENGTH_EXCEEDED }),
    ]);
  });

  it("yields the overflow as an error when nothing was compacted", async () => {
    const fake = new FakeProvider({ streams: [[lengthAtOnce]] });
    const provider = retryOnOverflow(fake, async () => false);

    const events = await collect(provider.stream(request, new AbortController().signal));

    expect(fake.requests).toHaveLength(1);
    expect(events).toEqual([
      expect.objectContaining({ type: "error", code: CONTEXT_LENGTH_EXCEEDED }),
    ]);
  });

  it("only an overflow on the first event counts: a length stop after text is a normal answer", () => {
    const cut: StreamEvent = {
      type: "done",
      message: { role: "assistant", content: "partial", toolCalls: [], stopReason: "length" },
    };

    expect(isOverflow(overflow)).toBe(true);
    expect(isOverflow(lengthAtOnce)).toBe(true);
    expect(isOverflow(cut)).toBe(false);
    expect(isOverflow({ type: "error", code: "429", message: "rate limited" })).toBe(false);
  });
});
