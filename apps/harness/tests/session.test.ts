import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import { FakeProvider, fakeDone } from "../src/provider/fake.ts";
import type { StreamEvent } from "../src/provider/types.ts";
import { AgentSession, InMemorySessionStore, type SessionOptions } from "../src/session.ts";
import { builtinRegistry } from "../src/tools/builtins.ts";
import type { ToolRegistry } from "../src/tools/registry.ts";
import type { AgentEvent, ToolCall } from "../src/types.ts";
import { useTmpDir } from "./helpers/tmp.ts";

const root = useTmpDir("session");

const writeCall = (id: string, path: string, content: string): ToolCall => ({
  id,
  name: "write",
  arguments: { path, content },
});

function toolTurn(...calls: ToolCall[]): StreamEvent[] {
  return [{ type: "text_delta", text: "working" }, fakeDone("", calls)];
}

const textTurn = (text: string): StreamEvent[] => [{ type: "text_delta", text }, fakeDone(text)];

async function setup(streams: StreamEvent[][], o: Partial<SessionOptions> = {}) {
  const home = join(root(), "home");
  const cwd = join(root(), "repo", "sub");
  mkdirSync(home, { recursive: true });
  mkdirSync(cwd, { recursive: true });
  const fake = new FakeProvider({ streams });
  const env = { TREE_AGENT_HOME: home };
  const session = await AgentSession.create({
    provider: fake,
    model: "test/model",
    tools: builtinRegistry({ tmpDir: join(home, "tmp") }),
    cwd,
    maxTurns: 10,
    trusted: false,
    contextMaxChars: 40_000,
    env,
    now: () => new Date("2026-10-24T12:00:00Z"),
    ...o,
  });
  const events: AgentEvent[] = [];
  session.on((e) => events.push(e));
  return { fake, session, events, home, cwd };
}

const sections = (system: string) => [...system.matchAll(/^<([a-z_]+)>$/gm)].map((m) => m[1]);

describe("AgentSession", () => {
  it("runs a prompt to completion and returns the agent_end reason", async () => {
    const t = await setup([textTurn("hello")]);

    const reason = await t.session.prompt("hi");

    expect(reason).toBe("completed");
    expect(t.session.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(t.events.at(-1)).toEqual({ type: "agent_end", reason: "completed" });
  });

  it("runs the real tools in its cwd", async () => {
    const t = await setup([toolTurn(writeCall("c1", "notes.md", "- one\n")), textTurn("done")]);

    await t.session.prompt("write notes");

    expect(t.events).toContainEqual({
      type: "tool_result",
      callId: "c1",
      content: "Wrote 6 bytes to notes.md",
      isError: false,
    });
  });

  it("sends a system prompt whose section order is stable across requests", async () => {
    const t = await setup([toolTurn(writeCall("c1", "a.md", "a")), textTurn("ok")]);

    await t.session.prompt("go");

    const [first, second] = t.fake.requests.map((r) => r.context.system);
    expect(sections(first ?? "")).toEqual(["tools", "rules", "cwd"]);
    expect(sections(second ?? "")).toEqual(sections(first ?? ""));
    expect(first).toContain(`Working directory: ${t.cwd}\nToday's date (UTC): 2026-10-24`);
    expect(first).toContain("- read: ");
  });

  it("lists an activated tool in the tools section only after activate()", async () => {
    const tools = builtinRegistry({ tmpDir: "/tmp/x" });
    registerDeferred(tools);
    const t = await setup([textTurn("a"), textTurn("b")], { tools });

    await t.session.prompt("one");
    tools.activate(["mcp__x__lookup"]);
    await t.session.prompt("two");

    const [before, after] = t.fake.requests;
    expect(before?.context.system).not.toContain("mcp__x__lookup");
    expect(before?.tools.map((s) => s.name)).not.toContain("mcp__x__lookup");
    expect(after?.context.system).toContain("- mcp__x__lookup: look things up");
    expect(after?.tools.map((s) => s.name)).toContain("mcp__x__lookup");
  });

  it("puts ~/.tree-agent/AGENTS.md, /repo/AGENTS.md and /repo/sub/CLAUDE.md in project_context, in order", async () => {
    const home = join(root(), "home");
    mkdirSync(join(root(), "repo", "sub"), { recursive: true });
    mkdirSync(home, { recursive: true });
    writeFileSync(join(home, "AGENTS.md"), "user-level");
    writeFileSync(join(root(), "repo", "AGENTS.md"), "repo-level");
    writeFileSync(join(root(), "repo", "sub", "CLAUDE.md"), "sub-level");
    const t = await setup([textTurn("ok")], { trusted: true });

    await t.session.prompt("hi");

    const system = t.fake.requests[0]?.context.system ?? "";
    expect(sections(system)).toEqual(["tools", "rules", "project_context", "cwd"]);
    const context = system.split("<project_context>\n")[1]?.split("\n</project_context>")[0];
    const ours = (context ?? "").split(/\n\n(?=# )/).filter((b) => b.includes(root()));
    expect(ours).toEqual([
      `# ${join(home, "AGENTS.md")}\nuser-level`,
      `# ${join(root(), "repo", "AGENTS.md")}\nrepo-level`,
      `# ${join(root(), "repo", "sub", "CLAUDE.md")}\nsub-level`,
    ]);
  });

  it("loads only ~/.tree-agent/AGENTS.md when the cwd is not trusted", async () => {
    const home = join(root(), "home");
    mkdirSync(join(root(), "repo", "sub"), { recursive: true });
    mkdirSync(home, { recursive: true });
    writeFileSync(join(home, "AGENTS.md"), "user-level");
    writeFileSync(join(root(), "repo", "AGENTS.md"), "repo-level");
    const t = await setup([textTurn("ok")]);

    await t.session.prompt("hi");

    const system = t.fake.requests[0]?.context.system ?? "";
    expect(system).toContain("user-level");
    expect(system).not.toContain("repo-level");
  });

  it("re-reads the context files on reload()", async () => {
    const t = await setup([textTurn("a"), textTurn("b")]);
    await t.session.prompt("one");
    writeFileSync(join(t.home, "AGENTS.md"), "added later");

    await t.session.reload();
    await t.session.prompt("two");

    expect(t.fake.requests[0]?.context.system).not.toContain("added later");
    expect(t.fake.requests[1]?.context.system).toContain("added later");
  });

  it("delivers steer() made while the model streams after the turn's tool results", async () => {
    const t = await setup([toolTurn(writeCall("c1", "a.md", "a")), textTurn("switched")]);
    t.session.on((e) => {
      if (e.type === "text_delta" && e.text === "working") t.session.steer("use pnpm, not npm");
    });

    await t.session.prompt("install deps");

    const second = t.fake.requests[1]?.context.messages ?? [];
    expect(second.map((m) => m.role)).toEqual(["user", "assistant", "tool", "user"]);
    expect(second.at(-1)).toEqual({ role: "user", content: "use pnpm, not npm" });
  });

  it("keeps both queues across an abort and delivers them on the next prompt()", async () => {
    const t = await setup([
      [{ type: "text_delta", text: "Hel" }, { type: "text_delta", text: "lo" }, fakeDone("Hello")],
      textTurn("answer two"),
      textTurn("answer follow-up"),
    ]);
    const off = t.session.on((e) => {
      if (e.type === "text_delta") {
        t.session.steer("steered");
        t.session.followUp("followed");
        t.session.abort();
      }
    });

    const aborted = await t.session.prompt("one");
    off();
    const next = await t.session.prompt("two");

    expect(aborted).toBe("aborted");
    expect(t.session.messages[1]).toMatchObject({ content: "Hel", stopReason: "aborted" });
    expect(next).toBe("completed");
    expect(t.fake.requests[1]?.context.messages.slice(-2)).toEqual([
      { role: "user", content: "steered" },
      { role: "user", content: "two" },
    ]);
    expect(t.fake.requests[2]?.context.messages.at(-1)).toEqual({
      role: "user",
      content: "followed",
    });
  });

  it("runs a queued follow-up after the prompt completes, in the same run", async () => {
    const t = await setup([textTurn("four"), textTurn("six")]);
    t.session.followUp("And 3+3?");

    const reason = await t.session.prompt("What is 2+2?");

    expect(reason).toBe("completed");
    expect(t.session.messages.map((m) => m.content)).toEqual([
      "What is 2+2?",
      "four",
      "And 3+3?",
      "six",
    ]);
    expect(t.events.filter((e) => e.type === "agent_end")).toHaveLength(1);
  });

  it("refuses a second prompt() while one is running", async () => {
    const t = await setup([textTurn("slow")]);
    const first = t.session.prompt("one");

    await expect(t.session.prompt("two")).rejects.toThrow(/a prompt is running/);
    await first;
  });

  it("caps a tool result over 64 KB in afterToolCall", async () => {
    const tools = builtinRegistry({ tmpDir: "/tmp/x" });
    tools.register({
      name: "huge",
      description: "d",
      promptSnippet: "s",
      parameters: z.object({}),
      exposure: "direct",
      isReadOnly: true,
      execute: async () => ({ content: "x".repeat(200_000) }),
    });
    const t = await setup(
      [[fakeDone("", [{ id: "h", name: "huge", arguments: {} }])], textTurn("ok")],
      { tools },
    );

    await t.session.prompt("go");

    const tool = t.fake.requests[1]?.context.messages.at(-1);
    expect(tool?.content.length).toBeLessThan(70_000);
    expect(tool?.content).toContain("bytes omitted");
  });

  it("appends every finished message to its store, and starts from the store's messages", async () => {
    const store = new InMemorySessionStore();
    const t = await setup([textTurn("a")], { store });

    await t.session.prompt("one");
    const resumed = await setup([textTurn("b")], { store });
    await resumed.session.prompt("two");

    expect(store.load().map((m) => m.content)).toEqual(["one", "a", "two", "b"]);
    expect(resumed.fake.requests[0]?.context.messages.map((m) => m.content)).toEqual([
      "one",
      "a",
      "two",
    ]);
  });

  it("reports an error and agent_end(error) when a hook throws", async () => {
    const tools = builtinRegistry({ tmpDir: "/tmp/x" });
    tools.declared = () => {
      throw new Error("registry exploded"); // prepareTurn rebuilds the prompt from it
    };
    const t = await setup([textTurn("never")], { tools });

    const reason = await t.session.prompt("hi");

    expect(reason).toBe("error");
    expect(t.events).toEqual([
      { type: "error", message: "registry exploded" },
      { type: "agent_end", reason: "error" },
    ]);
    expect(t.session.running).toBe(false);
  });
});

function registerDeferred(tools: ToolRegistry): void {
  tools.register({
    name: "mcp__x__lookup",
    description: "Look things up",
    promptSnippet: "look things up",
    parameters: z.object({ q: z.string() }),
    exposure: "deferred",
    isReadOnly: true,
    execute: async () => ({ content: "found" }),
  });
}
