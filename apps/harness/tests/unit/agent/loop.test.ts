import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { type LoopEvent, loop } from "../../../src/agent/loop";
import type { Message } from "../../../src/messages";
import type { AnyTool } from "../../../src/tools/registry";
import type { Tool, ToolContext, ToolResult } from "../../../src/tools/types";
import { makeFakeGeminiClient } from "../../helpers/fake-gemini";

// Drain an async generator and return the events it produced.
async function collect(gen: AsyncGenerator<LoopEvent>): Promise<LoopEvent[]> {
  const out: LoopEvent[] = [];
  for await (const ev of gen) out.push(ev);
  return out;
}

// A configurable stub tool — used throughout the loop tests to avoid touching
// the filesystem or real subprocesses. Defaults to read-only + returning "(ok)".
function makeStubTool(opts: {
  name: string;
  isDestructive?: boolean;
  call?: (input: Record<string, unknown>, ctx: ToolContext) => Promise<ToolResult> | ToolResult;
  schemaThrows?: boolean;
}): AnyTool {
  return {
    name: opts.name,
    description: `stub tool ${opts.name}`,
    schema: opts.schemaThrows
      ? z.object({ required_field: z.string() })
      : z.record(z.string(), z.unknown()),
    parametersJsonSchema: { type: "object" },
    isReadOnly: !opts.isDestructive,
    isDestructive: opts.isDestructive ?? false,
    async call(input, ctx) {
      return await (opts.call?.(input, ctx) ?? { content: "(ok)" });
    },
  } as AnyTool;
}

function ctx(): ToolContext {
  return { cwd: "/tmp", signal: new AbortController().signal };
}

const SYSTEM_PROMPT = "you are a test";

describe("loop", () => {
  test("single text turn — no function calls, ends with end_turn", async () => {
    const { client } = makeFakeGeminiClient({
      turns: [{ text: ["hello", " world"] }],
    });

    const events = await collect(
      loop({
        client,
        messages: [{ role: "user", content: "hi" }],
        systemPrompt: SYSTEM_PROMPT,
        tools: [],
        toolContext: ctx(),
      }),
    );

    const texts = events.filter((e) => e.type === "assistant_text").map((e) => e.text);
    expect(texts).toEqual(["hello", " world"]);

    const done = events.at(-1);
    expect(done?.type).toBe("done");
    if (done?.type === "done") {
      expect(done.reason).toBe("end_turn");
      expect(done.messages).toHaveLength(2); // user + assistant
      expect(done.messages[0]?.role).toBe("user");
      expect(done.messages[1]?.role).toBe("assistant");
    }
  });

  test("tool round-trip — tool_use → tool_result → text → end_turn", async () => {
    const stub = makeStubTool({
      name: "stub_read",
      call: (input) => ({ content: `saw ${JSON.stringify(input)}` }),
    });
    const { client } = makeFakeGeminiClient({
      turns: [{ functionCalls: [{ name: "stub_read", args: { x: 1 } }] }, { text: "done" }],
    });

    const events = await collect(
      loop({
        client,
        messages: [{ role: "user", content: "do it" }],
        systemPrompt: SYSTEM_PROMPT,
        tools: [stub],
        toolContext: ctx(),
      }),
    );

    const shape = events.map((e) => e.type);
    expect(shape).toEqual(["tool_use", "tool_result", "assistant_text", "done"]);
    const toolResult = events.find((e) => e.type === "tool_result");
    expect(toolResult?.type).toBe("tool_result");
    if (toolResult?.type === "tool_result") {
      expect(toolResult.content).toContain('"x":1');
      expect(toolResult.isError).toBeFalsy();
    }
  });

  test("onMessage fires for every assistant turn + tool_result turn (including final text)", async () => {
    const stub = makeStubTool({ name: "stub", call: () => ({ content: "r" }) });
    const { client } = makeFakeGeminiClient({
      turns: [{ functionCalls: [{ name: "stub" }] }, { text: "final" }],
    });

    const msgs: Message[] = [];
    await collect(
      loop({
        client,
        messages: [{ role: "user", content: "go" }],
        systemPrompt: SYSTEM_PROMPT,
        tools: [stub],
        toolContext: ctx(),
        onMessage: (m) => msgs.push(m),
      }),
    );

    // assistant(tool_use) + user(tool_result) + assistant(final text).
    // This is the M4 bug-fix regression test: before the fix, the terminal
    // assistant text wasn't persisted.
    expect(msgs).toHaveLength(3);
    expect(msgs[0]?.role).toBe("assistant");
    expect(msgs[1]?.role).toBe("user");
    expect(msgs[2]?.role).toBe("assistant");
    const finalText = msgs[2]?.content;
    expect(Array.isArray(finalText) ? finalText[0] : undefined).toMatchObject({
      type: "text",
      text: "final",
    });
  });

  test("permission deny on a destructive tool yields tool_result(isError)", async () => {
    const stub = makeStubTool({ name: "destructive_write", isDestructive: true });
    const { client } = makeFakeGeminiClient({
      turns: [{ functionCalls: [{ name: "destructive_write", args: {} }] }, { text: "stopped" }],
    });

    const events = await collect(
      loop({
        client,
        messages: [{ role: "user", content: "write" }],
        systemPrompt: SYSTEM_PROMPT,
        tools: [stub],
        toolContext: ctx(),
        permission: async () => "deny",
      }),
    );

    const tr = events.find((e) => e.type === "tool_result");
    if (tr?.type !== "tool_result") throw new Error("missing tool_result");
    expect(tr.isError).toBe(true);
    expect(tr.content).toBe("Permission denied by user.");
  });

  test("permission gate only fires for destructive tools", async () => {
    const stub = makeStubTool({ name: "readonly_thing" }); // isDestructive: false
    let permissionCalls = 0;
    const { client } = makeFakeGeminiClient({
      turns: [{ functionCalls: [{ name: "readonly_thing", args: {} }] }, { text: "ok" }],
    });

    await collect(
      loop({
        client,
        messages: [{ role: "user", content: "read" }],
        systemPrompt: SYSTEM_PROMPT,
        tools: [stub],
        toolContext: ctx(),
        permission: async () => {
          permissionCalls += 1;
          return "allow";
        },
      }),
    );

    expect(permissionCalls).toBe(0);
  });

  test("PreToolUse hook block short-circuits before permission", async () => {
    const stub = makeStubTool({ name: "stub", isDestructive: true });
    const permissionCalls: number[] = [];
    const { client } = makeFakeGeminiClient({
      turns: [{ functionCalls: [{ name: "stub", args: {} }] }, { text: "moved on" }],
    });

    const events = await collect(
      loop({
        client,
        messages: [{ role: "user", content: "go" }],
        systemPrompt: SYSTEM_PROMPT,
        tools: [stub],
        toolContext: ctx(),
        onBeforeTool: async () => ({ block: true, reason: "policy says no" }),
        permission: async () => {
          permissionCalls.push(1);
          return "allow";
        },
      }),
    );

    const tr = events.find((e) => e.type === "tool_result");
    if (tr?.type !== "tool_result") throw new Error("missing tool_result");
    expect(tr.isError).toBe(true);
    expect(tr.content).toBe("policy says no");
    expect(permissionCalls).toHaveLength(0); // gate never reached
  });

  test("PostToolUse fires after a successful tool, with the tool's result", async () => {
    const stub = makeStubTool({
      name: "stub",
      call: () => ({ content: "result body" }),
    });
    const after: Array<{ name: string; content: string; isError: boolean }> = [];
    const { client } = makeFakeGeminiClient({
      turns: [{ functionCalls: [{ name: "stub" }] }, { text: "done" }],
    });

    await collect(
      loop({
        client,
        messages: [{ role: "user", content: "go" }],
        systemPrompt: SYSTEM_PROMPT,
        tools: [stub],
        toolContext: ctx(),
        onAfterTool: async (name, _input, res) => {
          after.push({ name, content: res.content, isError: res.isError ?? false });
        },
      }),
    );

    expect(after).toHaveLength(1);
    expect(after[0]?.name).toBe("stub");
    expect(after[0]?.content).toBe("result body");
  });

  test("max iterations — every turn is a function call, done reason is max_iterations", async () => {
    const stub = makeStubTool({ name: "stub" });
    const { client } = makeFakeGeminiClient({
      turns: [
        { functionCalls: [{ name: "stub" }] },
        { functionCalls: [{ name: "stub" }] },
        { functionCalls: [{ name: "stub" }] },
      ],
    });

    const events = await collect(
      loop({
        client,
        messages: [{ role: "user", content: "go" }],
        systemPrompt: SYSTEM_PROMPT,
        tools: [stub],
        toolContext: ctx(),
        maxIterations: 3,
      }),
    );

    const done = events.at(-1);
    expect(done?.type).toBe("done");
    if (done?.type === "done") expect(done.reason).toBe("max_iterations");
  });

  test("stream error bubbles up as an error event and stops the loop", async () => {
    const { client } = makeFakeGeminiClient({ turns: [{ error: "boom" }] });

    const events = await collect(
      loop({
        client,
        messages: [{ role: "user", content: "go" }],
        systemPrompt: SYSTEM_PROMPT,
        tools: [],
        toolContext: ctx(),
      }),
    );

    const err = events.find((e) => e.type === "error");
    expect(err).toBeTruthy();
    if (err?.type === "error") expect(err.message).toContain("boom");
    // No done event — loop returned early.
    expect(events.find((e) => e.type === "done")).toBeUndefined();
  });

  test("unknown tool name produces tool_result(isError) and loop continues", async () => {
    const { client } = makeFakeGeminiClient({
      turns: [{ functionCalls: [{ name: "not_registered" }] }, { text: "moved on" }],
    });

    const events = await collect(
      loop({
        client,
        messages: [{ role: "user", content: "go" }],
        systemPrompt: SYSTEM_PROMPT,
        tools: [],
        toolContext: ctx(),
      }),
    );

    const tr = events.find((e) => e.type === "tool_result");
    if (tr?.type !== "tool_result") throw new Error("missing tool_result");
    expect(tr.isError).toBe(true);
    expect(tr.content).toContain("Unknown tool: not_registered");
    // Loop reached the second turn — it didn't abort.
    expect(events.find((e) => e.type === "assistant_text")).toBeTruthy();
  });

  test("zod schema failure on input surfaces as tool_result(isError)", async () => {
    const stub = makeStubTool({ name: "stub", schemaThrows: true });
    const { client } = makeFakeGeminiClient({
      turns: [
        { functionCalls: [{ name: "stub", args: {} }] }, // missing required_field
        { text: "handled" },
      ],
    });

    const events = await collect(
      loop({
        client,
        messages: [{ role: "user", content: "go" }],
        systemPrompt: SYSTEM_PROMPT,
        tools: [stub],
        toolContext: ctx(),
      }),
    );

    const tr = events.find((e) => e.type === "tool_result");
    if (tr?.type !== "tool_result") throw new Error("missing tool_result");
    expect(tr.isError).toBe(true);
    // Zod errors mention the field name.
    expect(tr.content.toLowerCase()).toContain("required_field");
  });

  test("tool_use events carry a generated call id prefixed with call_", async () => {
    const stub = makeStubTool({ name: "stub" });
    const { client } = makeFakeGeminiClient({
      turns: [{ functionCalls: [{ name: "stub" }] }, { text: "done" }],
    });

    const events = await collect(
      loop({
        client,
        messages: [{ role: "user", content: "go" }],
        systemPrompt: SYSTEM_PROMPT,
        tools: [stub],
        toolContext: ctx(),
      }),
    );

    const use = events.find((e) => e.type === "tool_use");
    const res = events.find((e) => e.type === "tool_result");
    if (use?.type !== "tool_use") throw new Error("missing tool_use");
    if (res?.type !== "tool_result") throw new Error("missing tool_result");
    expect(use.id).toMatch(/^call_/);
    expect(res.id).toBe(use.id);
  });
});
