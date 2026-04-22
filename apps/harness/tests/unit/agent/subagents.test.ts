import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  MAX_DEPTH,
  type SubagentProgressEvent,
  makeSpawnSubagent,
} from "../../../src/agent/subagents";
import type { AnyTool } from "../../../src/tools/registry";
import type { ToolResult } from "../../../src/tools/types";
import { makeFakeGeminiClient } from "../../helpers/fake-gemini";
import { mkTmpDir } from "../../helpers/tmp-fs";

// Redirect SessionStore writes (which nest under ~/.tree/projects/…) to a tmp
// $HOME so sub-agent tests don't pollute the developer's real home dir.
let savedHome: string | undefined;
beforeEach(() => {
  savedHome = process.env.HOME;
  process.env.HOME = mkTmpDir("tree-subagent-test-");
});
afterEach(() => {
  if (savedHome === undefined) process.env.HOME = "";
  else process.env.HOME = savedHome;
});

// Stub tool — same shape the loop tests use, minimal and destructive-aware.
function stub(name: string, opts: { destructive?: boolean } = {}): AnyTool {
  return {
    name,
    description: `stub ${name}`,
    schema: z.record(z.string(), z.unknown()),
    parametersJsonSchema: { type: "object" },
    isReadOnly: !opts.destructive,
    isDestructive: opts.destructive ?? false,
    async call(): Promise<ToolResult> {
      return { content: `${name} ok` };
    },
  } as AnyTool;
}

// task tool is special — subagents.ts filters it by name. We just need it
// present in the allTools list to exercise the filter; we never actually dispatch.
const TASK_TOOL = stub("task");

// Native tools matching real classifications. We only use `name` +
// `isDestructive` for the filter assertions, so trivial stubs suffice.
const ALL_TOOLS: AnyTool[] = [
  stub("read"),
  stub("write", { destructive: true }),
  stub("edit", { destructive: true }),
  stub("bash", { destructive: true }),
  stub("glob"),
  stub("grep"),
  stub("todo"),
  TASK_TOOL,
  // Also drop in a pseudo-MCP tool to verify the `explore` filter picks it up
  // when read-only.
  { ...stub("mcp__tree-memory__search_memory"), isReadOnly: true, isDestructive: false },
  // … and skips read-write MCP tools
  {
    ...stub("mcp__tree-memory__ingest_conversation"),
    isReadOnly: false,
    isDestructive: true,
  },
];

// Extract the tool names a sub-agent actually presents to Gemini on its first
// call. config.tools is what `toGeminiTools` produced, shaped as
// [{ functionDeclarations: [{ name, ... }, ...] }].
function capturedToolNames(config: unknown): string[] {
  if (typeof config !== "object" || config === null) return [];
  const c = config as { tools?: Array<{ functionDeclarations?: Array<{ name?: string }> }> };
  const decls = c.tools?.[0]?.functionDeclarations ?? [];
  return decls.map((d) => d.name ?? "").filter(Boolean);
}

const SYSTEM_PROMPT = "you are tree";
const PARENT_SESSION_ID = "parent-123";

function makeSpawnContext(turns: Parameters<typeof makeFakeGeminiClient>[0]["turns"]) {
  const { client, calls } = makeFakeGeminiClient({ turns });
  const events: SubagentProgressEvent[] = [];
  const spawn = makeSpawnSubagent({
    client,
    baseSystemPrompt: SYSTEM_PROMPT,
    allTools: ALL_TOOLS,
    parentSessionId: PARENT_SESSION_ID,
    cwd: process.env.HOME ?? "/tmp",
    onProgress: (ev) => events.push(ev),
  });
  return { spawn, calls, events };
}

describe("makeSpawnSubagent", () => {
  test("general subagent runs a trivial text turn and returns a clean SubagentResult", async () => {
    const { spawn } = makeSpawnContext([{ text: "summary goes here" }]);
    const result = await spawn({
      type: "general",
      description: "find something",
      prompt: "please summarize",
      parentDepth: 0,
      parentSignal: new AbortController().signal,
    });

    expect(result.stopped_reason).toBe("end_turn");
    expect(result.summary).toBe("summary goes here");
    expect(result.tool_uses).toBe(0);
    expect(result.subagent_id).toMatch(/^sub_/);
    expect(result.duration_ms).toBeGreaterThanOrEqual(0);
  });

  test("explore filter exposes read/glob/grep/todo + read-only MCP; hides writers + task", async () => {
    const { spawn, calls } = makeSpawnContext([{ text: "done" }]);
    await spawn({
      type: "explore",
      description: "peek",
      prompt: "list files",
      parentDepth: 0,
      parentSignal: new AbortController().signal,
    });
    const names = capturedToolNames(calls[0]?.config);
    expect(names).toContain("read");
    expect(names).toContain("glob");
    expect(names).toContain("grep");
    expect(names).toContain("todo");
    expect(names).toContain("mcp__tree-memory__search_memory");
    expect(names).not.toContain("write");
    expect(names).not.toContain("edit");
    expect(names).not.toContain("bash");
    expect(names).not.toContain("task");
    expect(names).not.toContain("mcp__tree-memory__ingest_conversation");
  });

  test("plan filter exposes only read/glob/grep", async () => {
    const { spawn, calls } = makeSpawnContext([{ text: "plan" }]);
    await spawn({
      type: "plan",
      description: "design",
      prompt: "plan it",
      parentDepth: 0,
      parentSignal: new AbortController().signal,
    });
    const names = capturedToolNames(calls[0]?.config);
    expect(names.sort()).toEqual(["glob", "grep", "read"]);
  });

  test("general filter keeps task when there's depth budget", async () => {
    const { spawn, calls } = makeSpawnContext([{ text: "ok" }]);
    await spawn({
      type: "general",
      description: "top",
      prompt: "go",
      parentDepth: 0,
      parentSignal: new AbortController().signal,
    });
    const names = capturedToolNames(calls[0]?.config);
    expect(names).toContain("task");
    expect(names).toContain("write");
  });

  test("general filter drops task when depth is already at MAX_DEPTH", async () => {
    const { spawn, calls } = makeSpawnContext([{ text: "ok" }]);
    // parentDepth = MAX_DEPTH - 1 ⇒ sub's depth = MAX_DEPTH, no task allowed.
    await spawn({
      type: "general",
      description: "at max",
      prompt: "go",
      parentDepth: MAX_DEPTH - 1,
      parentSignal: new AbortController().signal,
    });
    const names = capturedToolNames(calls[0]?.config);
    expect(names).not.toContain("task");
    expect(names).toContain("write"); // other tools still present
  });

  test("exceeding MAX_DEPTH refuses the spawn and doesn't call Gemini", async () => {
    const { spawn, calls } = makeSpawnContext([]); // no turns scripted — if the loop fires, the fake throws
    const result = await spawn({
      type: "general",
      description: "too deep",
      prompt: "x",
      parentDepth: MAX_DEPTH,
      parentSignal: new AbortController().signal,
    });
    expect(result.stopped_reason).toBe("depth_exceeded");
    expect(result.summary).toContain("depth cap");
    expect(calls).toHaveLength(0);
  });

  test("unknown subagent_type returns a clean error result", async () => {
    const { spawn } = makeSpawnContext([]);
    const result = await spawn({
      // biome-ignore lint/suspicious/noExplicitAny: intentionally bypassing the union
      type: "bogus" as any,
      description: "n/a",
      prompt: "x",
      parentDepth: 0,
      parentSignal: new AbortController().signal,
    });
    expect(result.stopped_reason).toBe("error");
    expect(result.summary).toContain("unknown subagent_type");
  });

  test("onProgress emits start → end for a trivial text-only sub-agent", async () => {
    const { spawn, events } = makeSpawnContext([{ text: "hello" }]);
    await spawn({
      type: "general",
      description: "greet",
      prompt: "hi",
      parentDepth: 0,
      parentSignal: new AbortController().signal,
    });

    const kinds = events.map((e) => e.kind);
    expect(kinds[0]).toBe("start");
    expect(kinds.at(-1)).toBe("end");
    expect(kinds).toContain("assistant_text");
    // start event carries the description + type + depth
    const start = events[0];
    if (start?.kind !== "start") throw new Error("first event not start");
    expect(start.description).toBe("greet");
    expect(start.type).toBe("general");
    expect(start.depth).toBe(1);
  });

  test("onProgress carries tool_use → tool_result for a call made inside the sub-agent", async () => {
    const { spawn, events } = makeSpawnContext([
      { functionCalls: [{ name: "read", args: { file_path: "/x" } }] },
      { text: "done" },
    ]);
    await spawn({
      type: "general",
      description: "read x",
      prompt: "x",
      parentDepth: 0,
      parentSignal: new AbortController().signal,
    });
    const kinds = events.map((e) => e.kind);
    expect(kinds).toEqual(["start", "tool_use", "tool_result", "assistant_text", "end"]);
  });
});
