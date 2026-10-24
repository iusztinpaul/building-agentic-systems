import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { FakeProvider, fakeDone, fakeModel } from "../src/provider/fake.ts";
import type { Provider, StreamEvent } from "../src/provider/types.ts";
import { AgentSession, type SessionOptions } from "../src/session.ts";
import { type Entry, SessionStore } from "../src/session-store/jsonl.ts";
import { discoverSkills } from "../src/skills/skills.ts";
import { builtinRegistry } from "../src/tools/builtins.ts";
import type { ToolRegistry } from "../src/tools/registry.ts";
import type { AgentEvent, Message, PermissionPrompter, ToolCall } from "../src/types.ts";
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

// Approves every call once, as a user pressing "y" would.
const approveAll: PermissionPrompter = { ask: async () => "once" };

async function setup(
  streams: StreamEvent[][],
  o: Partial<SessionOptions> = {},
  answers: unknown[] = [], // the classifier's, in auto
) {
  const home = join(root(), "home");
  const cwd = join(root(), "repo", "sub");
  mkdirSync(home, { recursive: true });
  mkdirSync(cwd, { recursive: true });
  const fake = new FakeProvider({ streams, answers });
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
    prompter: approveAll,
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

  it("appends every finished message to its store, and resumes from the store's messages", async () => {
    const store = SessionStore.inMemory();
    const t = await setup([textTurn("a")], { store });

    await t.session.prompt("one");
    const resumed = await setup([textTurn("b")], { store, resume: t.session.id });
    await resumed.session.prompt("two");

    const id = resumed.session.id ?? "";
    expect(id).toBe(t.session.id);
    expect(store.load(id).messages.map((m) => m.content)).toEqual(["one", "a", "two", "b"]);
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

describe("AgentSession persistence", () => {
  const sessionsDir = () => join(root(), "home", "sessions");
  const entries = (): Entry[] => {
    const dirs = existsSync(sessionsDir()) ? readdirSync(sessionsDir()) : [];
    const files = dirs.flatMap((d) =>
      readdirSync(join(sessionsDir(), d)).map((f) => join(sessionsDir(), d, f)),
    );
    if (files.length !== 1) throw new Error(`expected 1 session file, got ${files.length}`);
    return readFileSync(files[0] ?? "", "utf8")
      .trimEnd()
      .split("\n")
      .map((l) => JSON.parse(l) as Entry);
  };
  const jsonl = () => SessionStore.jsonl(sessionsDir());

  it("writes nothing until the first message, then a session line with cwd and model", async () => {
    const t = await setup([textTurn("hi")], { store: jsonl() });
    expect(existsSync(sessionsDir())).toBe(false);
    expect(t.session.id).toBeUndefined();

    await t.session.prompt("hello");

    const [head, ...rest] = entries();
    expect(head).toMatchObject({ type: "session", version: 1, cwd: t.cwd, model: "test/model" });
    expect(rest.map((e) => e.type === "message" && e.message.role)).toEqual(["user", "assistant"]);
    const [slug = ""] = readdirSync(sessionsDir());
    expect(readdirSync(join(sessionsDir(), slug))).toEqual([
      expect.stringMatching(new RegExp(`_${t.session.id}\\.jsonl$`)),
    ]);
  });

  it("has the user and assistant messages on disk while the tool call still runs", async () => {
    const tools = builtinRegistry({ tmpDir: "/tmp/x" });
    let seen: string[] = [];
    tools.register({
      name: "peek",
      description: "d",
      promptSnippet: "s",
      parameters: z.object({}),
      exposure: "direct",
      isReadOnly: true,
      execute: async () => {
        seen = entries().map((e) => (e.type === "message" ? e.message.role : e.type));
        return { content: "peeked" };
      },
    });
    const t = await setup(
      [[fakeDone("", [{ id: "p", name: "peek", arguments: {} }])], textTurn("ok")],
      { tools, store: jsonl() },
    );

    await t.session.prompt("go");

    expect(seen).toEqual(["session", "user", "assistant", "permission"]);
    expect(entries().map((e) => (e.type === "message" ? e.message.role : e.type))).toEqual([
      "session",
      "user",
      "assistant",
      "permission",
      "tool",
      "assistant",
    ]);
  });

  it("has the partial assistant message (aborted) on disk when turn_end reaches listeners", async () => {
    const t = await setup(
      [
        [
          { type: "text_delta", text: "Hel" },
          { type: "text_delta", text: "lo" },
          fakeDone("Hello"),
        ],
      ],
      { store: jsonl() },
    );
    let onDisk: Entry | undefined;
    t.session.on((e) => {
      if (e.type === "text_delta") t.session.abort();
      if (e.type === "turn_end") onDisk = entries().at(-1);
    });

    await t.session.prompt("long answer please");

    expect(onDisk).toMatchObject({
      type: "message",
      message: { role: "assistant", content: "Hel", stopReason: "aborted" },
    });
  });

  it("keeps steering and follow-ups in context order on disk", async () => {
    const t = await setup(
      [toolTurn(writeCall("c1", "a.md", "a")), textTurn("two"), textTurn("three")],
      {
        store: jsonl(),
      },
    );
    t.session.on((e) => {
      if (e.type === "text_delta" && e.text === "working") {
        t.session.steer("steered");
        t.session.followUp("followed");
      }
    });

    await t.session.prompt("first");

    const disk = entries().flatMap((e) => (e.type === "message" ? [e.message] : []));
    expect(disk).toEqual(t.session.messages);
    expect(disk.filter((m) => m.role === "user").map((m) => m.content)).toEqual([
      "first",
      "steered",
      "followed",
    ]);
  });

  it("resumes a session: messages, model, activated tools, and the next prompt chains on", async () => {
    const tools = builtinRegistry({ tmpDir: "/tmp/x" });
    registerDeferred(tools);
    const first = await setup([textTurn("a")], { store: jsonl() });
    await first.session.prompt("one");
    await first.session.setModel("other/model");
    const id = first.session.id ?? "";
    // what task 209 will write when tool_search activates a tool
    jsonl().append(id, { type: "tool_activation", names: ["mcp__x__lookup"] });

    const again = await setup([textTurn("b")], { store: jsonl(), resume: id, tools });
    await again.session.prompt("two");

    expect(again.session.model).toBe("other/model");
    expect(again.session.toolNames()).toContain("mcp__x__lookup");
    expect(again.fake.requests[0]?.model).toBe("other/model");
    expect(again.fake.requests[0]?.context.messages.map((m) => m.content)).toEqual([
      "one",
      "a",
      "two",
    ]);
    const all = entries();
    for (let i = 1; i < all.length; i++) expect(all[i]?.parentId).toBe(all[i - 1]?.id);
    expect(all.map((e) => e.type)).toEqual([
      "session",
      "message",
      "message",
      "model_change",
      "tool_activation",
      "message",
      "message",
    ]);
  });

  it("rejects an unknown resume id", async () => {
    await expect(setup([], { store: jsonl(), resume: "nope" })).rejects.toThrow("no session nope");
  });

  it("setModel appends model_change and the next request uses the new model", async () => {
    const t = await setup([textTurn("a"), textTurn("b")], { store: jsonl() });
    await t.session.prompt("one");

    await t.session.setModel("anthropic/claude-haiku-5.5");
    await t.session.prompt("two");

    expect(t.fake.requests.map((r) => r.model)).toEqual([
      "test/model",
      "anthropic/claude-haiku-5.5",
    ]);
    expect(entries().find((e) => e.type === "model_change")).toMatchObject({
      model: "anthropic/claude-haiku-5.5",
    });
    expect(t.session.status().model).toBe("anthropic/claude-haiku-5.5");
  });

  it("setModel rejects an unknown model and changes nothing", async () => {
    const fake = new FakeProvider({ streams: [textTurn("a")], models: [fakeModel("test/model")] });
    const t = await setup([], { provider: fake, store: jsonl() });

    await expect(t.session.setModel("no/such")).rejects.toThrow(/unknown model/);

    expect(t.session.model).toBe("test/model");
    expect(existsSync(sessionsDir())).toBe(false);
  });

  it("refuses setModel and clear while a prompt runs", async () => {
    const t = await setup([textTurn("slow")]);
    const run = t.session.prompt("one");

    await expect(t.session.setModel("x/y")).rejects.toThrow(/while a prompt is running/);
    expect(() => t.session.clear()).toThrow(/while a prompt is running/);
    await run;
  });

  it("clear starts a new session file and an empty context", async () => {
    const tools = builtinRegistry({ tmpDir: "/tmp/x" });
    registerDeferred(tools);
    const store = jsonl();
    const t = await setup([textTurn("a"), textTurn("b")], { store, tools });
    await t.session.prompt("one");
    const first = t.session.id;
    tools.activate(["mcp__x__lookup"]);
    t.session.followUp("stale");

    t.session.clear();
    await t.session.prompt("two");

    expect(t.session.id).not.toBe(first);
    expect(
      store
        .list(t.cwd)
        .map((s) => s.id)
        .sort(),
    ).toEqual([first, t.session.id].sort());
    expect(t.fake.requests[1]?.context.messages).toEqual([{ role: "user", content: "two" }]);
    expect(t.session.toolNames()).not.toContain("mcp__x__lookup");
  });

  it("reports model, mode, the last input tokens, context length and cost in status()", async () => {
    const fake = new FakeProvider({
      streams: [
        [fakeDone("a", [], { inputTokens: 1000, outputTokens: 100 })],
        [fakeDone("b", [], { inputTokens: 3000, outputTokens: 200 })],
      ],
      models: [
        fakeModel("test/model", {
          contextLength: 10_000,
          pricing: { prompt: 0.001, completion: 0.01 },
        }),
      ],
    });
    const t = await setup([], { provider: fake, permissionMode: "auto" });
    expect(t.session.status()).toEqual({
      model: "test/model",
      mode: "auto",
      tokens: undefined,
      contextLength: 10_000,
      cost: 0,
      mcpServers: 0,
      activatedTools: 0,
    });

    await t.session.prompt("one");
    await t.session.prompt("two");

    const status = t.session.status();
    expect(status.tokens).toBe(3000);
    expect(status.cost).toBeCloseTo(1000 * 0.001 + 100 * 0.01 + 3000 * 0.001 + 200 * 0.01);
  });

  it("keeps the gauge and the cost after an aborted turn, and restores the cost on resume", async () => {
    const streams = [
      [fakeDone("a", [], { inputTokens: 1000, outputTokens: 100 })],
      [{ type: "text_delta", text: "par" }, { type: "text_delta", text: "tial" }, fakeDone("x")],
    ] satisfies StreamEvent[][];
    const priced = fakeModel("test/model", { pricing: { prompt: 0.001, completion: 0.01 } });
    const fake = new FakeProvider({ streams, models: [priced] });
    const t = await setup([], { provider: fake, store: jsonl() });
    await t.session.prompt("one");
    const off = t.session.on((e) => e.type === "text_delta" && t.session.abort());
    await t.session.prompt("two");
    off();

    expect(t.session.status()).toMatchObject({ tokens: 1000, cost: 2 });
    const resumed = await setup([], {
      provider: new FakeProvider({ models: [priced] }),
      store: jsonl(),
      resume: t.session.id,
    });
    expect(resumed.session.status()).toMatchObject({ tokens: 1000, cost: 2 });
  });
});

describe("AgentSession permissions", () => {
  const bash = (id: string, command = "true"): ToolCall => ({
    id,
    name: "bash",
    arguments: { command },
  });
  const safe = { dangerous: false, risk: "low", reason: "cwd-scoped" };
  const results = (messages: readonly Message[]) =>
    messages.flatMap((m) => (m.role === "tool" ? [m.content] : []));

  it("records every resolution as a permission entry, then event, before the tool runs", async () => {
    const sessions = join(root(), "sessions");
    const store = SessionStore.jsonl(sessions);
    const t = await setup(
      [toolTurn({ id: "r", name: "read", arguments: { path: "x" } }, bash("b")), textTurn("ok")],
      { store, permissionMode: "auto" },
      [safe],
    );

    await t.session.prompt("go");

    const types = t.events.map((e) => e.type);
    expect(types.slice(3, 9)).toEqual([
      "tool_call",
      "permission",
      "tool_result",
      "tool_call",
      "permission",
      "tool_result",
    ]);
    const permissions = t.events.filter((e) => e.type === "permission");
    expect(permissions).toEqual([
      {
        type: "permission",
        call: { id: "r", name: "read", arguments: { path: "x" } },
        decision: "allow",
        source: "readOnly",
      },
      {
        type: "permission",
        call: bash("b"),
        decision: "allow",
        source: "classifier",
        reason: "cwd-scoped",
      },
    ]);
    const [dir = ""] = readdirSync(sessions);
    const [file = ""] = readdirSync(join(sessions, dir));
    const entries = readFileSync(join(sessions, dir, file), "utf8")
      .trimEnd()
      .split("\n")
      .map((l) => JSON.parse(l) as Entry);
    const kinds = entries.map((e) => (e.type === "message" ? e.message.role : e.type));
    expect(kinds).toEqual([
      "session",
      "user",
      "assistant",
      "permission",
      "tool",
      "permission",
      "tool",
      "assistant",
    ]);
    expect(entries[5]).toMatchObject({
      type: "permission",
      call: bash("b"),
      decision: "allow",
      source: "classifier",
      reason: "cwd-scoped",
    });
    expect(entries[3]).not.toHaveProperty("reason");
  });

  it("in ask without a prompter: denies the first write, ends blocked, runs nothing else", async () => {
    const t = await setup([toolTurn(writeCall("w", "a.md", "a"), bash("b")), textTurn("never")], {
      prompter: null,
      permissionMode: "ask",
    });

    const reason = await t.session.prompt("write a.md");

    expect(reason).toBe("blocked");
    expect(t.events.at(-1)).toEqual({ type: "agent_end", reason: "blocked" });
    expect(existsSync(join(t.cwd, "a.md"))).toBe(false);
    expect(results(t.session.messages)).toEqual([
      "Blocked: needs approval (permission mode ask) and a headless run cannot prompt",
      "Not run: aborted.",
    ]);
    expect(t.events.filter((e) => e.type === "permission")).toEqual([
      expect.objectContaining({ decision: "deny", source: "headless" }),
    ]);
    expect(t.fake.requests).toHaveLength(1);
    expect(t.fake.structuredRequests).toEqual([]);
  });

  it("with onBlocked continue: the model gets Blocked: … and the run completes", async () => {
    const t = await setup(
      [toolTurn(writeCall("w", "a.md", "a")), textTurn("I could not write it.")],
      { prompter: null, permissionMode: "auto", onBlocked: "continue" },
      [{ dangerous: true, risk: "high", reason: "overwrites a file" }],
    );

    const reason = await t.session.prompt("write a.md");

    expect(reason).toBe("completed");
    expect(t.fake.requests[1]?.context.messages.at(-1)).toEqual({
      role: "tool",
      toolCallId: "w",
      name: "write",
      content: "Blocked: dangerous: overwrites a file",
      isError: true,
    });
  });

  it("uses the decision model and sends the user's last 3 messages to the classifier", async () => {
    const t = await setup(
      [textTurn("1"), textTurn("2"), textTurn("3"), toolTurn(bash("b")), textTurn("ok")],
      { permissionMode: "auto", decisionModel: "test/decider" },
      [safe],
    );

    for (const text of ["one", "two", "three", "four"]) await t.session.prompt(text);

    const [sent] = t.fake.structuredRequests;
    expect(sent?.model).toBe("test/decider");
    expect(sent?.input).toContain("1. two\n2. three\n3. four\n");
  });

  it("switches the mode for the rest of the session: the next call is classified", async () => {
    const asked: string[] = [];
    const t = await setup(
      [toolTurn(bash("b1", "ls")), toolTurn(bash("b2", "pwd")), textTurn("ok")],
      {
        permissionMode: "ask",
        prompter: {
          ask: async (call) => {
            asked.push(call.id);
            t.session.setPermissionMode("auto"); // e.g. Shift+Tab while asked
            return "once";
          },
        },
      },
      [safe],
    );

    await t.session.prompt("go");

    expect(asked).toEqual(["b1"]);
    expect(t.fake.structuredRequests).toHaveLength(1);
    expect(t.session.status().mode).toBe("auto");
  });

  it("forgets session approvals and cached verdicts on clear()", async () => {
    const asked: string[] = [];
    const t = await setup(
      [
        toolTurn(bash("b1")),
        toolTurn(bash("b2")),
        textTurn("a"),
        toolTurn(bash("b3")),
        textTurn("b"),
      ],
      {
        prompter: {
          ask: async (call) => {
            asked.push(call.id);
            return "session";
          },
        },
      },
    );

    await t.session.prompt("one");
    t.session.clear();
    await t.session.prompt("two");

    expect(asked).toEqual(["b1", "b3"]);
  });

  // Regression (task 206 QA): the write ran after an abort during
  // the classifier call.
  it("runs nothing when aborted while the classifier is asked, and records no decision", async () => {
    let classifierSignal: AbortSignal | undefined;
    const t = await setup(
      [toolTurn(writeCall("w", "a.txt", "x")), textTurn("never")],
      { permissionMode: "auto", prompter: null },
      [
        // Ignores the abort and answers "safe" late, as a slow
        // upstream would.
        async (_req: unknown, signal?: AbortSignal) => {
          classifierSignal = signal;
          await new Promise((r) => setTimeout(r, 150));
          return safe;
        },
      ],
    );
    t.session.on((e) => {
      if (e.type === "tool_call") setTimeout(() => t.session.abort(), 20);
    });

    const reason = await t.session.prompt("write a.txt");
    await new Promise((r) => setTimeout(r, 200)); // the late answer lands

    expect(reason).toBe("aborted");
    expect(existsSync(join(t.cwd, "a.txt"))).toBe(false);
    expect(classifierSignal?.aborted).toBe(true);
    expect(results(t.session.messages)).toEqual(["Not run: aborted."]);
    expect(t.events.some((e) => e.type === "permission")).toBe(false);
    expect(t.events.at(-1)).toEqual({ type: "agent_end", reason: "aborted" });
  });

  it("does not gate a registered tool the model cannot call yet; activated, it is gated", async () => {
    const asked: string[] = [];
    const tools = builtinRegistry({ tmpDir: join(root(), "tmp") });
    tools.register({
      name: "mcp__fs__delete",
      description: "d",
      promptSnippet: "s",
      parameters: z.object({ path: z.string() }),
      exposure: "deferred",
      isReadOnly: false,
      execute: async () => ({ content: "deleted" }),
    });
    const del = (id: string): ToolCall => ({
      id,
      name: "mcp__fs__delete",
      arguments: { path: "x" },
    });
    const t = await setup(
      [toolTurn(del("d1")), textTurn("a"), toolTurn(del("d2")), textTurn("b")],
      {
        tools,
        permissionMode: "ask",
        prompter: {
          ask: async (call) => {
            asked.push(call.id);
            return "session";
          },
        },
      },
    );

    await t.session.prompt("one");
    tools.activate(["mcp__fs__delete"]);
    await t.session.prompt("two");

    expect(results(t.session.messages)).toEqual(["Unknown tool: mcp__fs__delete", "deleted"]);
    expect(asked).toEqual(["d2"]);
    expect(t.events.filter((e) => e.type === "permission")).toHaveLength(1);
  });

  it("does not gate an unknown tool: the registry answers it", async () => {
    const t = await setup([toolTurn({ id: "x", name: "nope", arguments: {} }), textTurn("ok")], {
      prompter: null,
    });

    expect(await t.session.prompt("go")).toBe("completed");
    expect(t.events.some((e) => e.type === "permission")).toBe(false);
    expect(results(t.session.messages)).toEqual(["Unknown tool: nope"]);
  });
});

describe("AgentSession with a provider that streams until aborted", () => {
  it("returns from prompt() within a second of abort(), with the partial text kept", async () => {
    const provider: Provider = {
      modelInfo: async (id) => fakeModel(id),
      async *stream(_req, signal) {
        yield { type: "text_delta", text: "a long " };
        await new Promise((resolve) => signal.addEventListener("abort", resolve));
        yield {
          type: "done",
          message: { role: "assistant", content: "a long ", toolCalls: [], stopReason: "aborted" },
        };
      },
      complete: async () => {
        throw new Error("unused");
      },
      completeText: async () => {
        throw new Error("unused");
      },
    };
    const t = await setup([], { provider });
    const run = t.session.prompt("go");
    await new Promise((r) => setTimeout(r, 20));
    const started = Date.now();

    t.session.abort();

    expect(await run).toBe("aborted");
    expect(Date.now() - started).toBeLessThan(1000);
    expect(t.session.messages.at(-1)).toMatchObject({ content: "a long ", stopReason: "aborted" });
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

describe("AgentSession skills", () => {
  // Skills under ~/.agents/skills (HOME is a scratch dir); the model's
  // contextLength sets the listing budget (1 % of it).
  async function withSkills(
    names: string[],
    o: Partial<SessionOptions> = {},
    contextLength = 200_000,
    extra: Record<string, string> = {},
  ) {
    const home = join(root(), "home");
    const skills = join(home, ".agents", "skills");
    for (const name of names) {
      mkdirSync(join(skills, name), { recursive: true });
      writeFileSync(
        join(skills, name, "SKILL.md"),
        `---\nname: ${name}\ndescription: does ${name}\n${extra[name] ?? ""}---\n# ${name}\n`,
      );
    }
    const cwd = join(root(), "repo");
    mkdirSync(cwd, { recursive: true });
    const fake = new FakeProvider({
      streams: [textTurn("ok"), textTurn("ok")],
      models: [
        fakeModel("test/model", { contextLength }),
        fakeModel("big/model", { contextLength: 1_000_000 }),
      ],
    });
    const discover = vi.fn(discoverSkills);
    const session = await AgentSession.create({
      provider: fake,
      model: "test/model",
      tools: builtinRegistry({ tmpDir: join(home, "tmp") }),
      cwd,
      maxTurns: 5,
      trusted: false,
      contextMaxChars: 40_000,
      env: { HOME: home, TREE_AGENT_HOME: join(home, ".tree-agent") },
      skills: { budgetRatio: 0.01, discover },
      ...o,
    });
    return { fake, session, discover, skills };
  }
  const listed = (system: string) =>
    [...system.matchAll(/<skill><name>([^<]+)<\/name>/g)].map((m) => m[1]);

  it("adds the skills section between project_context and cwd", async () => {
    const t = await withSkills(["alpha"]);

    await t.session.prompt("hi");

    const system = t.fake.requests[0]?.context.system ?? "";
    const tags = sections(system).filter((tag) => tag !== "available_skills");
    expect(tags).toEqual(["tools", "rules", "skills", "cwd"]);
    expect(listed(system)).toEqual(["alpha"]);
    expect(t.session.skillNotices).toEqual([]);
  });

  it("with contextLength 20 000 stops the listing at 200 tokens and warns once with the dropped names", async () => {
    const names = Array.from({ length: 12 }, (_, i) => `skill-${String(i).padStart(2, "0")}`);
    const t = await withSkills(names, {}, 20_000);

    await t.session.prompt("hi");

    const system = t.fake.requests[0]?.context.system ?? "";
    const section = system.split("<skills>\n")[1]?.split("\n</skills>")[0] ?? "";
    expect(section.length).toBeLessThanOrEqual(800);
    const shown = listed(system);
    const dropped = names.slice(shown.length);
    expect(shown).toEqual(names.slice(0, shown.length));
    expect(dropped.length).toBeGreaterThan(0);
    expect(t.session.skillNotices).toEqual([
      `skills: over the listing budget (200 tokens), not listed: ${dropped.join(", ")}`,
    ]);
  });

  it("re-budgets the listing on /model: a bigger context lists every skill", async () => {
    const names = Array.from({ length: 12 }, (_, i) => `skill-${String(i).padStart(2, "0")}`);
    const t = await withSkills(names, {}, 20_000);

    await t.session.setModel("big/model");
    await t.session.prompt("hi");

    expect(listed(t.fake.requests[0]?.context.system ?? "")).toEqual(names);
    expect(t.session.skillNotices).toEqual([]);
  });

  it("hides a disable-model-invocation skill from the section and keeps it for /skill:", async () => {
    const t = await withSkills(["shown", "secret"], {}, 200_000, {
      secret: "disable-model-invocation: true\n",
    });

    await t.session.prompt("hi");

    const system = t.fake.requests[0]?.context.system ?? "";
    expect(listed(system)).toEqual(["shown"]);
    expect(system).not.toContain("secret");
    expect(t.session.skills.map((s) => s.name)).toEqual(["secret", "shown"]);
    expect(t.session.skillMessage("secret", "why?")).toBe("# secret\n\nwhy?");
  });

  it("answers an unknown skill name with the known ones", async () => {
    const t = await withSkills(["alpha", "beta"]);

    expect(() => t.session.skillMessage("nope", "")).toThrow(
      'unknown skill "nope"; known: alpha, beta',
    );
  });

  it("prefixes each discovery diagnostic for the startup notice", async () => {
    const t = await withSkills(["alpha"]);
    const bad = join(t.skills, "bad", "SKILL.md");
    mkdirSync(join(t.skills, "bad"));
    writeFileSync(bad, "---\ndescription: x\n---\n");

    await t.session.reload();

    expect(t.session.skillNotices).toEqual([`skills: ${bad}: missing name`]);
    expect(t.session.skills.map((s) => s.name)).toEqual(["alpha"]);
  });

  it("re-discovers on reload()", async () => {
    const t = await withSkills(["alpha"]);
    mkdirSync(join(t.skills, "later"));
    writeFileSync(join(t.skills, "later", "SKILL.md"), "---\nname: later\ndescription: d\n---\n");

    await t.session.reload();
    await t.session.prompt("hi");

    expect(listed(t.fake.requests[0]?.context.system ?? "")).toEqual(["alpha", "later"]);
    expect(t.discover).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["absent", undefined],
    ["null (--no-skills)", null],
  ])("skills %s: no walker call and no section", async (_label, skills) => {
    const discover = vi.fn(discoverSkills);
    const t = await withSkills(["alpha"], { skills: skills && { budgetRatio: 0.01, discover } });

    await t.session.prompt("hi");

    expect(t.fake.requests[0]?.context.system).not.toContain("<skills>");
    expect(discover).not.toHaveBeenCalled();
    expect(() => t.session.skillMessage("alpha", "")).toThrow(
      'unknown skill "alpha"; skills are disabled (--no-skills)',
    );
  });

  it("says known: (none) when skills are enabled and none were found", async () => {
    const t = await withSkills([]);

    expect(() => t.session.skillMessage("alpha", "")).toThrow(
      'unknown skill "alpha"; known: (none)',
    );
  });

  it("sends an empty-body skill's args alone and refuses an empty message", async () => {
    const t = await withSkills(["empty"]);
    writeFileSync(join(t.skills, "empty", "SKILL.md"), "---\nname: empty\ndescription: d\n---\n\n");
    await t.session.reload();

    expect(t.session.skillMessage("empty", "q")).toBe("q");
    expect(() => t.session.skillMessage("empty", " ")).toThrow(
      'skill "empty" has an empty body and no args; nothing sent',
    );
  });
});
