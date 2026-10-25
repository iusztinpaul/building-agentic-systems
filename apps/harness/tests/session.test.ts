import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { contextChars } from "../src/compaction/estimate.ts";
import { Settings } from "../src/config/settings.ts";
import { type RegistrableClient, registerMcpTools } from "../src/mcp/register.ts";
import { FakeProvider, type FakeScript, fakeDone, fakeModel } from "../src/provider/fake.ts";
import type { Provider, StreamEvent, TextRequest } from "../src/provider/types.ts";
import { AgentSession, type SessionOptions } from "../src/session.ts";
import { type Entry, SessionStore } from "../src/session-store/jsonl.ts";
import { discoverSkills } from "../src/skills/skills.ts";
import { builtinRegistry } from "../src/tools/builtins.ts";
import type { Exposure, ToolRegistry } from "../src/tools/registry.ts";
import { registerToolSearch } from "../src/tools/tool-search.ts";
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
  answers: unknown[] = [], // the classifier's, in auto; llm()'s schema answers
  more: Pick<FakeScript, "texts" | "models"> = {}, // llm() answers, prices
) {
  const home = join(root(), "home");
  const cwd = join(root(), "repo", "sub");
  mkdirSync(home, { recursive: true });
  mkdirSync(cwd, { recursive: true });
  const fake = new FakeProvider({ streams, answers, ...more });
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

  it("reports model, mode, estimateTokens, context length and cost in status()", async () => {
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
      // The system prompt alone, chars / 4 (no usage yet: ratio 1).
      tokens: Math.ceil(t.session.systemPrompt().length / 4),
      contextLength: 10_000,
      cost: 0,
      mcpServers: 0,
      activatedTools: 0,
    });

    await t.session.prompt("one");
    await t.session.prompt("two");

    // The last request's usage (3000) over its estimate is the ratio
    // (clamped 0.5–2); the gauge is ratio × chars / 4 of the context.
    const sent = fake.requests[1]?.context ?? { system: "", messages: [] };
    const ratio = Math.min(2, Math.max(0.5, 3000 / (contextChars(sent) / 4)));
    const now = { system: sent.system, messages: [...t.session.messages] };
    const status = t.session.status();
    expect(status.tokens).toBe(Math.ceil((contextChars(now) / 4) * ratio));
    expect(status.cost).toBeCloseTo(1000 * 0.001 + 100 * 0.01 + 3000 * 0.001 + 200 * 0.01);
  });

  it("keeps the cost after an aborted turn and restores it on resume, the gauge at ratio 1", async () => {
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

    expect(t.session.status()).toMatchObject({ cost: 2 });
    const resumed = await setup([], {
      provider: new FakeProvider({ models: [priced] }),
      store: jsonl(),
      resume: t.session.id,
    });
    const system = resumed.session.systemPrompt();
    const messages = [...resumed.session.messages];
    expect(resumed.session.status()).toMatchObject({
      tokens: Math.ceil(contextChars({ system, messages }) / 4),
      cost: 2,
    });
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
    expect(t.session.skillMessage("secret", "why?")).toBe(
      `# ${join(t.skills, "secret", "SKILL.md")}\n# secret\n\nwhy?`,
    );
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

  it("sends an empty-body skill's header and args and refuses an empty message", async () => {
    const t = await withSkills(["empty"]);
    const location = join(t.skills, "empty", "SKILL.md");
    writeFileSync(location, "---\nname: empty\ndescription: d\n---\n\n");
    await t.session.reload();

    expect(t.session.skillMessage("empty", "q")).toBe(`# ${location}\nq`);
    expect(() => t.session.skillMessage("empty", " ")).toThrow(
      'skill "empty" has an empty body and no args; nothing sent',
    );
  });
});

describe("AgentSession with MCP tools and tool_search", () => {
  const sessionsDir = () => join(root(), "home", "sessions");
  const jsonl = () => SessionStore.jsonl(sessionsDir());
  const fileEntries = (): Entry[] => {
    const [dir = ""] = readdirSync(sessionsDir());
    const [file = ""] = readdirSync(join(sessionsDir(), dir));
    return readFileSync(join(sessionsDir(), dir, file), "utf8")
      .trimEnd()
      .split("\n")
      .map((l) => JSON.parse(l) as Entry);
  };
  const memoryServer = (connected = true): RegistrableClient => ({
    name: "tree-memory-local",
    server: {
      command: "uv",
      args: [],
      env: {},
      toolExposure: { search_memory: "direct", "visualize_*": "deferred" },
    },
    connected,
    tools: [
      { name: "search_memory", description: "Search the memory.", inputSchema: { type: "object" } },
      {
        name: "visualize_memory_structure",
        description: "Show how the memory is organised.",
        inputSchema: { type: "object" },
      },
      {
        name: "visualize_memory_embeddings",
        description: "Show a 2D map of the memory's topics.",
        inputSchema: { type: "object" },
      },
      { name: "ingest_url", description: "Add a web page.", inputSchema: { type: "object" } },
      { name: "search_web", description: "Search the web.", inputSchema: { type: "object" } },
    ],
    callTool: async (name) => ({
      content: [{ type: "text", text: `${name} ran` }],
      isError: false,
    }),
  });
  const mcpTools = (...clients: RegistrableClient[]) => {
    const tools = builtinRegistry({ tmpDir: join(root(), "home", "tmp") });
    const mcpServers = registerMcpTools(tools, clients, { tmpDir: join(root(), "home", "tmp") });
    registerToolSearch(tools);
    return { tools, mcpServers };
  };
  const STRUCTURE = "mcp__tree_memory_local__visualize_memory_structure";
  const EMBEDDINGS = "mcp__tree_memory_local__visualize_memory_embeddings";
  const call = (id: string, name: string, args: Record<string, unknown> = {}): ToolCall => ({
    id,
    name,
    arguments: args,
  });

  it("without MCP the model sees exactly the 4 built-ins and no mcp_servers section", async () => {
    const t = await setup([textTurn("ok")]);

    await t.session.prompt("hi");

    expect(t.fake.requests[0]?.tools.map((s) => s.name)).toEqual(["read", "bash", "edit", "write"]);
    expect(t.fake.requests[0]?.context.system).not.toContain("<mcp_servers>");
    expect(t.fake.requests[0]?.context.system).not.toContain("tool_search");
  });

  it("sends the direct MCP tool and tool_search, and lists the server's counts in mcp_servers", async () => {
    const { tools, mcpServers } = mcpTools(memoryServer());
    const t = await setup([textTurn("ok")], { tools, mcpServers });

    await t.session.prompt("hi");

    const [request] = t.fake.requests;
    expect(request?.tools.map((s) => s.name)).toEqual([
      "read",
      "bash",
      "edit",
      "write",
      "mcp__tree_memory_local__search_memory",
      "tool_search",
    ]);
    expect(request?.context.system).toContain(
      "<mcp_servers>\n- tree-memory-local: 5 tools; direct 1, deferred 2, codemode 2\n</mcp_servers>",
    );
    expect(request?.context.system).toContain(
      "- tool_search: find more tools by describing what you need\n" +
        "More tools are available; call `tool_search` to find them.\n</tools>",
    );
  });

  it("status() counts the connected MCP servers", async () => {
    const { tools, mcpServers } = mcpTools(memoryServer(), {
      ...memoryServer(false),
      name: "down",
      tools: [],
    });
    const t = await setup([], { tools, mcpServers });

    expect(t.session.mcpServers.map((s) => s.name)).toEqual(["tree-memory-local", "down"]);
    expect(t.session.status()).toMatchObject({ mcpServers: 1, activatedTools: 0 });
  });

  it("tool_search activates the visualize tools: next request, tools section, event, entry on disk", async () => {
    const { tools, mcpServers } = mcpTools(memoryServer());
    const t = await setup(
      [
        toolTurn(call("s1", "tool_search", { query: "visualize memory" })),
        toolTurn(call("v1", STRUCTURE)),
        textTurn("here is the picture"),
      ],
      { tools, mcpServers, store: jsonl() },
    );

    await t.session.prompt("show me a picture of my memory");

    const [first, second] = t.fake.requests;
    expect(first?.tools.map((s) => s.name)).not.toContain(STRUCTURE);
    expect(second?.tools.map((s) => s.name)).toEqual(
      expect.arrayContaining([STRUCTURE, EMBEDDINGS]),
    );
    expect(second?.context.system).toContain(`- ${STRUCTURE}: Show how the memory is organised.`);
    const types = t.events.map((e) =>
      e.type === "tool_result" ? `${e.type}:${e.callId}` : e.type,
    );
    const activation = types.indexOf("tool_activation");
    expect(activation).toBeGreaterThan(types.indexOf("tool_call"));
    expect(activation).toBeLessThan(types.indexOf("tool_result:s1"));
    expect(t.events).toContainEqual({ type: "tool_activation", names: [STRUCTURE, EMBEDDINGS] });
    expect(t.events).toContainEqual({
      type: "tool_result",
      callId: "v1",
      content: "visualize_memory_structure ran",
      isError: false,
    });
    expect(t.session.status().activatedTools).toBe(2);
    const onDisk = fileEntries();
    expect(onDisk.filter((e) => e.type === "tool_activation")).toEqual([
      expect.objectContaining({ names: [STRUCTURE, EMBEDDINGS] }),
    ]);
    // after the assistant message that asked (and the readOnly
    // permission), before its tool result
    const kinds = onDisk.map((e) => (e.type === "message" ? e.message.role : e.type));
    expect(kinds.slice(0, 6)).toEqual([
      "session",
      "user",
      "assistant",
      "permission",
      "tool_activation",
      "tool",
    ]);
  });

  it("a second search for already-activated tools writes no new activation", async () => {
    const { tools, mcpServers } = mcpTools(memoryServer());
    const t = await setup(
      [
        toolTurn(call("s1", "tool_search", { query: "visualize memory" })),
        toolTurn(call("s2", "tool_search", { query: "visualize memory" })),
        textTurn("ok"),
      ],
      { tools, mcpServers, store: jsonl() },
    );

    await t.session.prompt("go");

    expect(t.events.filter((e) => e.type === "tool_activation")).toHaveLength(1);
    expect(fileEntries().filter((e) => e.type === "tool_activation")).toHaveLength(1);
  });

  it("--resume restores the activation: listed and sent without a new tool_search", async () => {
    const first = mcpTools(memoryServer());
    const a = await setup(
      [toolTurn(call("s1", "tool_search", { query: "embeddings map" })), textTurn("found it")],
      { tools: first.tools, mcpServers: first.mcpServers, store: jsonl() },
    );
    await a.session.prompt("find the map tool");
    const id = a.session.id ?? "";

    const again = mcpTools(memoryServer());
    const b = await setup([toolTurn(call("e1", EMBEDDINGS)), textTurn("map")], {
      tools: again.tools,
      mcpServers: again.mcpServers,
      store: jsonl(),
      resume: id,
    });

    expect(b.session.toolNames()).toContain(EMBEDDINGS);
    expect(b.session.status().activatedTools).toBe(1);
    await b.session.prompt("draw it");

    expect(b.fake.requests[0]?.tools.map((s) => s.name)).toContain(EMBEDDINGS);
    expect(b.fake.requests[0]?.context.system).toContain(`- ${EMBEDDINGS}:`);
    expect(b.events.filter((e) => e.type === "tool_activation")).toEqual([]);
    expect(b.events).toContainEqual(
      expect.objectContaining({ type: "tool_result", callId: "e1", isError: false }),
    );
    expect(fileEntries().filter((e) => e.type === "tool_activation")).toHaveLength(1);
  });

  it("clear() forgets the activation; a new search announces it again", async () => {
    const { tools, mcpServers } = mcpTools(memoryServer());
    const search = toolTurn(call("s1", "tool_search", { query: "visualize" }));
    const t = await setup([search, textTurn("a"), search, textTurn("b")], { tools, mcpServers });

    await t.session.prompt("one");
    t.session.clear();
    expect(t.session.toolNames()).not.toContain(STRUCTURE);
    await t.session.prompt("two");

    expect(t.events.filter((e) => e.type === "tool_activation")).toHaveLength(2);
  });
});

describe("AgentSession with codemode", () => {
  const LIMITS = {
    inlineBudgetTokens: 3_000,
    timeoutMs: 5_000,
    memoryMb: 64,
    maxToolCalls: 50,
    outputMaxBytes: 20_000,
    concurrency: 4,
    llm: Settings.parse({}).codemode.llm,
  };
  const codemode = () => ({ ...LIMITS, tmpDir: join(root(), "home", "tmp") });
  // A registry with the built-ins and one codemode-exposed MCP tool.
  const withMcp = (server = memoryLike()) => {
    const tools = builtinRegistry({ tmpDir: join(root(), "home", "tmp") });
    const mcpServers = registerMcpTools(tools, [server], { tmpDir: join(root(), "home", "tmp") });
    registerToolSearch(tools);
    return { tools, mcpServers, codemode: codemode() };
  };
  const memoryLike = (toolExposure: Record<string, Exposure> = {}): RegistrableClient => ({
    name: "tree-memory-local",
    server: { command: "uv", args: [], env: {}, toolExposure },
    connected: true,
    tools: [
      "search_memory",
      "visualize_memory_structure",
      "visualize_memory_embeddings",
      "search_web",
      "scrape_web",
      "ingest_url",
      "ingest_file",
      "ingest_conversation",
    ].map((name) => ({ name, description: `${name} tool.`, inputSchema: { type: "object" } })),
    callTool: async (name) => ({
      content: [{ type: "text", text: `${name} ran` }],
      isError: false,
    }),
  });
  const script = (source: string): ToolCall => ({
    id: "cm",
    name: "codemode",
    arguments: { script: source, description: "write some files" },
  });
  const toolResult = (messages: readonly Message[]) =>
    messages.flatMap((m) => (m.role === "tool" ? [m.content] : []));
  const safe = { dangerous: false, risk: "low", reason: "cwd-scoped" };

  it("with the committed .tree-agent/mcp.json the direct set is exactly the 4 built-ins, search_memory, tool_search, codemode", async () => {
    const committed = JSON.parse(
      readFileSync(new URL("../../../.tree-agent/mcp.json", import.meta.url), "utf8"),
    ) as { mcpServers: Record<string, { toolExposure: Record<string, Exposure> }> };
    const exposure = committed.mcpServers["tree-memory-local"]?.toolExposure ?? {};
    const t = await setup([textTurn("ok")], withMcp(memoryLike(exposure)));

    await t.session.prompt("hi");

    expect(t.fake.requests[0]?.tools.map((s) => s.name)).toEqual([
      "read",
      "bash",
      "edit",
      "write",
      "mcp__tree_memory_local__search_memory",
      "tool_search",
      "codemode",
    ]);
  });

  it("ask: prompts for codemode, then for a write inside it; a deny rejects and the script's catch goes on", async () => {
    const asked: { call: ToolCall; summary: string }[] = [];
    const t = await setup(
      [
        toolTurn(
          script(`
            try { await tools.write({ path: "a.md", content: "a" }); }
            catch (e) { console.log("skipped:", e.message); }
            return "partial";
          `),
        ),
        textTurn("I skipped a.md"),
      ],
      {
        ...withMcp(),
        permissionMode: "ask",
        prompter: {
          ask: async (call, summary) => {
            asked.push({ call, summary });
            return call.name === "write" ? "deny" : "once";
          },
        },
      },
    );

    const reason = await t.session.prompt("write a.md");

    expect(reason).toBe("completed");
    expect(asked.map((a) => a.call.name)).toEqual(["codemode", "write"]);
    expect(asked[0]?.summary).toContain("write some files\n");
    expect(asked[1]).toMatchObject({ call: { origin: "codemode" }, summary: "a.md" });
    expect(toolResult(t.session.messages)).toEqual([
      'skipped: Blocked: denied by the user\n"partial"',
    ]);
    expect(existsSync(join(t.cwd, "a.md"))).toBe(false);
    expect(t.events.filter((e) => e.type === "permission")).toMatchObject([
      { call: { name: "codemode" }, decision: "allow", source: "user" },
      { call: { name: "write", origin: "codemode" }, decision: "deny", source: "user" },
    ]);
  });

  it("ask: the script waits on the prompt (answered after 100 ms) and the call then runs", async () => {
    const t = await setup(
      [
        toolTurn(script('return await tools.write({ path: "b.md", content: "b" });')),
        textTurn("ok"),
      ],
      {
        ...withMcp(),
        permissionMode: "ask",
        prompter: {
          ask: async (call) => {
            if (call.name === "write") await new Promise((r) => setTimeout(r, 100));
            return "once";
          },
        },
        codemode: { ...codemode(), timeoutMs: 50 }, // the prompt does not count
      },
    );

    await t.session.prompt("write b.md");

    expect(readFileSync(join(t.cwd, "b.md"), "utf8")).toBe("b");
    expect(toolResult(t.session.messages)).toEqual(['"Wrote 1 bytes to b.md"']);
  });

  it("a read-only tool inside a script never prompts", async () => {
    const asked: string[] = [];
    const t = await setup(
      [toolTurn(script('return (await tools.read({ path: "n.md" })).lines;')), textTurn("ok")],
      {
        ...withMcp(),
        permissionMode: "ask",
        prompter: {
          ask: async (call) => {
            asked.push(call.name);
            return "once";
          },
        },
      },
    );
    writeFileSync(join(t.cwd, "n.md"), "one\ntwo\n");

    await t.session.prompt("read n.md");

    expect(asked).toEqual(["codemode"]);
    expect(t.events).toContainEqual(
      expect.objectContaining({
        type: "permission",
        call: expect.objectContaining({ name: "read" }),
        source: "readOnly",
      }),
    );
  });

  it("parallel calls get one dialog at a time, first come first asked", async () => {
    let open = 0;
    let peak = 0;
    const asked: string[] = [];
    const t = await setup(
      [
        toolTurn(
          script(
            'await Promise.all(["a", "b", "c"].map((p) => tools.write({ path: p + ".md", content: p })));',
          ),
        ),
        textTurn("ok"),
      ],
      {
        ...withMcp(),
        permissionMode: "ask",
        prompter: {
          ask: async (call) => {
            peak = Math.max(peak, ++open);
            asked.push(String(call.arguments.path ?? call.name));
            await new Promise((r) => setTimeout(r, 20));
            open--;
            return "once";
          },
        },
      },
    );

    await t.session.prompt("write three");

    expect(peak).toBe(1);
    expect(asked).toEqual(["codemode", "a.md", "b.md", "c.md"]);
    for (const p of ["a", "b", "c"]) expect(existsSync(join(t.cwd, `${p}.md`))).toBe(true);
  });

  it("a `session` answer covers the parallel calls queued behind it", async () => {
    const asked: string[] = [];
    const t = await setup(
      [
        toolTurn(
          script(
            'await Promise.all(["a", "b"].map((p) => tools.write({ path: p + ".md", content: p })));',
          ),
        ),
        textTurn("ok"),
      ],
      {
        ...withMcp(),
        permissionMode: "ask",
        prompter: {
          ask: async (call) => {
            asked.push(call.name);
            return call.name === "write" ? "session" : "once";
          },
        },
      },
    );

    await t.session.prompt("write two");

    expect(asked).toEqual(["codemode", "write"]);
    expect(
      t.events
        .filter((e) => e.type === "permission")
        .map((e) => e.type === "permission" && e.source),
    ).toEqual(["user", "user", "session"]);
  });

  it("headless: a dangerous call inside a script ends the run blocked, even inside try/catch", async () => {
    const sessions = join(root(), "sessions");
    const t = await setup(
      [
        toolTurn(
          script(`
            try { await tools.write({ path: "~/.ssh/config", content: "x" }); }
            catch (e) { console.log("caught"); await tools.write({ path: "after.md", content: "x" }); }
            return "never";
          `),
        ),
        textTurn("never"),
      ],
      {
        ...withMcp(),
        store: SessionStore.jsonl(sessions),
        prompter: null,
        permissionMode: "auto",
      },
      [safe, { dangerous: true, risk: "high", reason: "writes ~/.ssh/config" }, safe],
    );

    const reason = await t.session.prompt("configure ssh");

    expect(reason).toBe("blocked");
    expect(t.events.at(-1)).toEqual({ type: "agent_end", reason: "blocked" });
    expect(toolResult(t.session.messages)).toEqual([
      "Error: the script was stopped: a tool call was blocked",
    ]);
    expect(existsSync(join(t.cwd, "after.md"))).toBe(false);
    expect(t.fake.requests).toHaveLength(1);
    expect(t.fake.structuredRequests).toHaveLength(2); // codemode + the write; never the second
    const [dir = ""] = readdirSync(sessions);
    const [file = ""] = readdirSync(join(sessions, dir));
    const entries = readFileSync(join(sessions, dir, file), "utf8")
      .trimEnd()
      .split("\n")
      .map((l) => JSON.parse(l) as Entry);
    expect(entries.filter((e) => e.type === "permission")).toMatchObject([
      { call: { name: "codemode" }, decision: "allow", source: "classifier" },
      {
        call: { name: "write", origin: "codemode" },
        decision: "deny",
        source: "headless",
        reason: "dangerous: writes ~/.ssh/config",
      },
    ]);
  });

  it("headless with onBlocked continue: the script catches the deny and the run completes", async () => {
    const t = await setup(
      [
        toolTurn(
          script(
            'try { await tools.write({ path: "x.md", content: "x" }); } catch (e) { return e.message; }',
          ),
        ),
        textTurn("skipped"),
      ],
      { ...withMcp(), prompter: null, permissionMode: "auto", onBlocked: "continue" },
      [safe, { dangerous: true, risk: "high", reason: "nope" }],
    );

    expect(await t.session.prompt("go")).toBe("completed");
    expect(toolResult(t.session.messages)).toEqual(['"Blocked: dangerous: nope"']);
  });

  it("a codemode-only MCP tool is reachable from a script and gated like any other", async () => {
    const asked: string[] = [];
    const t = await setup(
      [
        toolTurn(script("return await tools.mcp__tree_memory_local__scrape_web({ url: 'x' });")),
        textTurn("ok"),
      ],
      {
        ...withMcp(),
        permissionMode: "ask",
        prompter: {
          ask: async (call) => {
            asked.push(call.name);
            return "once";
          },
        },
      },
    );

    await t.session.prompt("scrape");

    expect(t.fake.requests[0]?.tools.map((s) => s.name)).not.toContain(
      "mcp__tree_memory_local__scrape_web",
    );
    expect(asked).toEqual(["codemode", "mcp__tree_memory_local__scrape_web"]);
    expect(toolResult(t.session.messages)).toEqual(['"scrape_web ran"']);
  });

  // spec §10.6: pull 40 hits into the sandbox, map with llm(), reduce with a schema.
  const WORKED_EXAMPLE = `
    const hits = await tools.mcp__tree_memory_local__search_memory({
      query: "pricing decisions", limit: 40 });
    const facts = await Promise.all(hits.map(h => llm(
      \`Extract pricing facts as bullets:\\n\${h.text}\`)));
    const answer = await llm(
      \`Merge into one list, dedupe:\\n\${facts.join("\\n")}\`,
      { schema: { type: "object", properties: {
          facts: { type: "array", items: { type: "string" } } },
        required: ["facts"] } });
    return answer.facts;`;
  const DECISION = "anthropic/claude-haiku-5.5"; // the settings default
  const PRICES = [
    fakeModel("test/model", { pricing: { prompt: 0.000003, completion: 0.000015 } }),
    fakeModel(DECISION, { pricing: { prompt: 0.000001, completion: 0.000002 } }),
  ];
  const withHits = () => {
    const hits = Array.from({ length: 40 }, (_, i) => ({ text: `HIT-${i}: pricing note ${i}` }));
    const server = memoryLike();
    return withMcp({
      ...server,
      // read-only, as the real search_memory declares itself
      tools: server.tools.map((tool) => ({ ...tool, annotations: { readOnlyHint: true } })),
      callTool: async () => ({
        content: [{ type: "text", text: JSON.stringify(hits) }],
        isError: false,
      }),
    });
  };
  const workedExample = async (o: Partial<SessionOptions> = {}) => {
    const asked: string[] = [];
    const t = await setup(
      [toolTurn(script(WORKED_EXAMPLE)), textTurn("Two facts.")],
      {
        ...withHits(),
        // 41 calls, inside the default maxCalls (64): the spec's example as printed
        codemode: codemode(),
        permissionMode: "ask",
        prompter: {
          ask: async (call) => {
            asked.push(call.name);
            return "once";
          },
        },
        ...o,
      },
      [{ facts: ["Pro is $5", "Team is $20"] }],
      { texts: Array.from({ length: 40 }, (_, i) => `- fact ${i}`), models: PRICES },
    );
    return { ...t, asked };
  };

  it("the §10.6 worked example grows the context by exactly 2 and no hit text enters it", async () => {
    const t = await workedExample();
    let before = -1;
    let after = -1;
    t.session.on((e) => {
      if (e.type === "tool_call") before = t.session.messages.length - 1; // the call's message is in
      if (e.type === "tool_result") after = t.session.messages.length;
    });

    const reason = await t.session.prompt("summarise every pricing decision");

    expect(reason).toBe("completed");
    expect(after - before).toBe(2); // the assistant codemode call and its tool result
    expect(t.session.messages.map((m) => m.role)).toEqual([
      "user",
      "assistant",
      "tool",
      "assistant",
    ]);
    expect(toolResult(t.session.messages)).toEqual(['["Pro is $5","Team is $20"]']);
    const context = JSON.stringify(t.session.messages);
    expect(context).not.toContain("HIT-");
    expect(context).not.toContain("fact 7");
    expect(t.fake.textRequests).toHaveLength(40);
    expect(t.fake.textRequests.every((r) => r.input.includes("HIT-"))).toBe(true);
    expect(t.fake.structuredRequests).toHaveLength(1);
    const llmCalls = t.events.filter((e) => e.type === "llm_call");
    expect(llmCalls).toHaveLength(41);
    expect(llmCalls.every((e) => e.depth === 1 && e.model === DECISION && e.usage)).toBe(true);
  });

  it("ask mode: llm() never prompts; the only question is the codemode call itself", async () => {
    const t = await workedExample();

    await t.session.prompt("summarise");

    expect(t.asked).toEqual(["codemode"]);
    const decided = t.events.flatMap((e) =>
      e.type === "permission" ? [[e.call.name, e.source]] : [],
    );
    expect(decided).toEqual([
      ["codemode", "user"],
      ["mcp__tree_memory_local__search_memory", "readOnly"],
    ]); // 41 llm() calls, no permission entry for any of them
  });

  it("the session cost sums the main turns and every llm_call's usage × its model's pricing, and survives a resume", async () => {
    const store = SessionStore.inMemory();
    const t = await workedExample({ store });

    await t.session.prompt("summarise");

    const turns = t.events.flatMap((e) =>
      e.type === "turn_end" && e.message.usage ? [e.message.usage] : [],
    );
    const calls = t.events.flatMap((e) => (e.type === "llm_call" && e.usage ? [e.usage] : []));
    const main = turns.reduce(
      (sum, u) => sum + u.inputTokens * 0.000003 + u.outputTokens * 0.000015,
      0,
    );
    const sub = calls.reduce(
      (sum, u) => sum + u.inputTokens * 0.000001 + u.outputTokens * 0.000002,
      0,
    );
    expect(calls).toHaveLength(41);
    expect(sub).toBeGreaterThan(0);
    expect(t.session.status().cost).toBeCloseTo(main + sub, 12);

    const id = t.session.id ?? "";
    expect(store.load(id).spend.filter((s) => s.model === DECISION)).toHaveLength(41);
    const resumed = await setup([], { store, resume: id }, [], { models: PRICES });
    expect(resumed.session.status().cost).toBeCloseTo(main + sub, 12);
  });

  it("the cost rises with each llm_call as it arrives, before the codemode result", async () => {
    const t = await workedExample();
    const costs: number[] = [];
    t.session.on((e) => {
      if (e.type === "llm_call") costs.push(t.session.status().cost);
    });

    await t.session.prompt("summarise");

    expect(costs).toHaveLength(41);
    for (let i = 1; i < costs.length; i++) {
      expect(costs[i]).toBeGreaterThan(costs[i - 1] ?? 0);
    }
  });

  it("headless depth 2: a dangerous call inside the sub-loop ends the run blocked; the script's catch never runs", async () => {
    const t = await setup(
      [
        toolTurn(
          script(`
            try { return await llm("configure ssh", {tools: true}); }
            catch (e) { console.log("caught"); return "swallowed"; }
          `),
        ),
        toolTurn(writeCall("s1", "~/.ssh/config", "x")), // the sub-loop
        textTurn("never"),
        textTurn("never"),
      ],
      {
        ...withMcp(),
        codemode: { ...codemode(), llm: { ...LIMITS.llm, depth: 2 } },
        prompter: null,
        permissionMode: "auto",
      },
      [safe, { dangerous: true, risk: "high", reason: "writes ~/.ssh/config" }],
    );

    const reason = await t.session.prompt("configure ssh");

    expect(reason).toBe("blocked");
    expect(t.events.filter((e) => e.type === "agent_end")).toEqual([
      { type: "agent_end", reason: "blocked" },
    ]);
    expect(toolResult(t.session.messages)).toEqual([
      "Error: the script was stopped: a tool call was blocked",
    ]);
    expect(t.fake.requests).toHaveLength(2); // the main turn and one sub-loop turn
    expect(t.events.filter((e) => e.type === "permission")).toMatchObject([
      { call: { name: "codemode" }, decision: "allow", source: "classifier" },
      { call: { name: "write" }, decision: "deny", source: "headless" },
    ]);
  });

  it("depth 2: the sub-loop's read never prompts, its write goes through the same gate in ask mode", async () => {
    const asked: string[] = [];
    const subRead: ToolCall = { id: "s1", name: "read", arguments: { path: "notes.md" } };
    const subWrite = writeCall("s2", "out.md", "summary");
    const t = await setup(
      [
        toolTurn(script('return await llm("Summarise notes.md into out.md", {tools: true})')),
        toolTurn(subRead), // the sub-loop
        toolTurn(subWrite),
        textTurn("Wrote the summary."),
        textTurn("Done."), // the main loop again
      ],
      {
        ...withMcp(),
        codemode: { ...codemode(), llm: { ...LIMITS.llm, depth: 2 } },
        permissionMode: "ask",
        prompter: {
          ask: async (call) => {
            asked.push(call.name);
            return "once";
          },
        },
      },
    );
    writeFileSync(join(t.cwd, "notes.md"), "a note");

    const reason = await t.session.prompt("summarise my notes");

    expect(reason).toBe("completed");
    expect(asked).toEqual(["codemode", "write"]);
    expect(t.events.filter((e) => e.type === "permission")).toMatchObject([
      { call: { name: "codemode" }, source: "user" },
      { call: { name: "read" }, decision: "allow", source: "readOnly" },
      { call: { name: "write" }, decision: "allow", source: "user" },
    ]);
    expect(readFileSync(join(t.cwd, "out.md"), "utf8")).toBe("summary");
    expect(t.fake.requests[1]?.tools.map((s) => s.name)).not.toContain("codemode");
    expect(toolResult(t.session.messages)).toEqual(['"Wrote the summary."']);
    expect(t.session.messages.map((m) => m.role)).toEqual([
      "user",
      "assistant",
      "tool",
      "assistant",
    ]);
    expect(t.events.filter((e) => e.type === "llm_call")).toMatchObject([{ depth: 2 }]);
    expect(t.events.filter((e) => e.type === "agent_end")).toEqual([
      { type: "agent_end", reason: "completed" },
    ]);
  });
  it("depth 2: the sub-loop has no tool_search, so it never activates a deferred tool in the main registry", async () => {
    const mcp = withMcp(memoryLike({ "visualize_*": "deferred" }));
    const t = await setup(
      [
        toolTurn(script('return await llm("visualize my memory", {tools: true})')),
        toolTurn({ id: "s1", name: "tool_search", arguments: { query: "visualize memory" } }),
        textTurn("No visualize tool here."), // the sub-loop's answer
        textTurn("Done."), // the main loop again
      ],
      { ...mcp, codemode: { ...codemode(), llm: { ...LIMITS.llm, depth: 2 } } },
    );

    expect(await t.session.prompt("visualize")).toBe("completed");

    const sub = t.fake.requests[1]?.tools.map((s) => s.name) ?? [];
    expect(sub).not.toContain("codemode");
    expect(sub).not.toContain("tool_search");
    expect(t.fake.requests[2]?.context.messages[2]).toMatchObject({
      role: "tool",
      name: "tool_search",
      isError: true,
    });
    expect(mcp.tools.activated()).toEqual([]);
    expect(t.events.filter((e) => e.type === "tool_activation")).toEqual([]);
    const main = t.fake.requests[3]?.tools.map((s) => s.name) ?? [];
    expect(main).toContain("tool_search");
    expect(main.filter((n) => n.includes("visualize"))).toEqual([]);
  });

  it("an llm() call settling after an abort reports nothing after agent_end, but is still saved", async () => {
    // Notices the abort late, as a hung request torn down by fetch:
    // it settles only when the test says so, after agent_end.
    const deferred = () => {
      let resolve!: () => void;
      const promise = new Promise<void>((r) => (resolve = r));
      return { promise, resolve };
    };
    const started = deferred();
    const late = deferred();
    class LateAbort extends FakeProvider {
      override async completeText(_req: TextRequest, signal?: AbortSignal): Promise<never> {
        const aborted = new Promise((resolve) => signal?.addEventListener("abort", resolve));
        started.resolve();
        await aborted;
        await late.promise;
        throw new Error("This operation was aborted");
      }
    }
    const provider = new LateAbort({
      streams: [toolTurn(script('return await llm("hang")')), textTurn("never")],
    });
    const store = SessionStore.inMemory();
    const saved: string[] = [];
    const llmCallSaved = deferred();
    const append = store.append.bind(store);
    store.append = (id, body) => {
      saved.push(body.type);
      if (body.type === "llm_call") llmCallSaved.resolve();
      return append(id, body);
    };
    const t = await setup([], { ...withMcp(), provider, store });

    const run = t.session.prompt("hang");
    await started.promise; // the llm() request is in flight
    t.session.abort();
    expect(await run).toBe("aborted");
    expect(saved).not.toContain("llm_call"); // still in flight
    late.resolve(); // the late settle, strictly after agent_end
    await llmCallSaved.promise;
    await new Promise((resolve) => setImmediate(resolve)); // any stray event lands

    expect(t.events.at(-1)).toEqual({ type: "agent_end", reason: "aborted" });
    expect(t.events.filter((e) => e.type === "llm_call")).toEqual([]);
    expect(saved).toContain("llm_call"); // the --resume cost stays true
  });
});

describe("AgentSession compaction", () => {
  const tokens = (n: number) => "x".repeat(n * 4); // n estimated tokens
  // A done without usage: the estimate stays chars / 4 (ratio 1).
  const bare = (content: string, calls: ToolCall[] = []): StreamEvent => ({
    type: "done",
    message: {
      role: "assistant",
      content,
      toolCalls: calls,
      stopReason: calls.length > 0 ? "tool_calls" : "stop",
    },
  });
  // One turn whose write call carries n tokens of arguments.
  const writeTurn = (id: string, n: number) => [bare("", [writeCall(id, `${id}.md`, tokens(n))])];
  const answerTurn = (text: string) => [bare(text)];
  const writes = (n: number, each: number) =>
    Array.from({ length: n }, (_, i) => writeTurn(`c${i + 1}`, each));
  const summary = (goal: string) => ({
    goal,
    constraints: [],
    progress: [],
    decisions: [],
    nextSteps: [],
    criticalContext: ["c1.md"],
  });
  const small = fakeModel("test/model", { contextLength: 10_000 });
  const isSummary = (m?: Message) => m?.role === "user" && m.content.startsWith("<summary>\n");
  const compactions = (events: AgentEvent[]) => events.filter((e) => e.type === "compaction");
  const overflow: StreamEvent = {
    type: "error",
    code: "context_length_exceeded",
    message: "This endpoint's maximum context length is 10000 tokens",
  };
  const sessionsDir = () => join(root(), "home", "sessions");
  const lines = (): Entry[] =>
    readdirSync(sessionsDir()).flatMap((d) =>
      readdirSync(join(sessionsDir(), d)).flatMap((f) =>
        readFileSync(join(sessionsDir(), d, f), "utf8")
          .trimEnd()
          .split("\n")
          .map((l) => JSON.parse(l) as Entry),
      ),
    );

  it("compacts in prepareTurn past 80 % of a 10 000 window: [summary, …tail], same system prompt, activated tools sent", async () => {
    const tools = builtinRegistry({ tmpDir: join(root(), "tmp") });
    registerDeferred(tools);
    tools.activate(["mcp__x__lookup"]);
    const t = await setup(
      [...writes(3, 3_000), answerTurn("done")],
      { tools },
      [summary("write three notes")],
      { models: [small] },
    );

    expect(await t.session.prompt("write three notes")).toBe("completed");

    const [, , third, fourth] = t.fake.requests;
    expect(third?.context.messages.some(isSummary)).toBe(false);
    const sent = fourth?.context.messages ?? [];
    expect(isSummary(sent[0])).toBe(true);
    expect(sent.filter(isSummary)).toHaveLength(1);
    // The tail starts at an assistant turn: c2's call, then its result.
    expect(sent.slice(1).map((m) => m.role)).toEqual(["assistant", "tool", "assistant", "tool"]);
    expect(sent[1]).toMatchObject({ toolCalls: [{ id: "c2" }] });
    expect(sent[2]).toMatchObject({ role: "tool", toolCallId: "c2" });
    expect(fourth?.context.system).toBe(third?.context.system);
    expect(fourth?.tools.map((s) => s.name)).toContain("mcp__x__lookup");
    expect(fourth?.context.system).toContain("- mcp__x__lookup: look things up");
    const [event] = compactions(t.events);
    expect(compactions(t.events)).toHaveLength(1);
    expect(event).toMatchObject({ type: "compaction" });
    if (event?.type !== "compaction") throw new Error("no compaction");
    expect(event.before).toBeGreaterThanOrEqual(8_000);
    expect(event.after).toBeLessThan(event.before);
    // Between the third answer and the fourth request.
    const at = t.events.indexOf(event);
    expect(t.events.slice(at + 1).filter((e) => e.type === "turn_start")).toHaveLength(1);
    const request = t.fake.structuredRequests[0];
    expect(request?.name).toBe("compaction_summary");
    expect(request?.model).toBe("anthropic/claude-haiku-5.5");
    expect(request?.input).toContain("[User]: write three notes");
  });

  it("triggerRatio 0.5 (--compact-at 0.5) compacts at 5 000 where 0.8 does not", async () => {
    const streams = () => [...writes(3, 1_800), answerTurn("done")];
    const at = await setup(
      streams(),
      { compaction: { triggerRatio: 0.5, keepRecentTokens: 20_000 } },
      [summary("g")],
      {
        models: [small],
      },
    );
    const base = await setup(streams(), {}, [], { models: [small] });

    await at.session.prompt("go");
    await base.session.prompt("go");

    const firstSummary = at.fake.requests.findIndex((r) => isSummary(r.context.messages[0]));
    expect(firstSummary).toBe(3);
    expect(compactions(at.events)).toHaveLength(1);
    expect(compactions(base.events)).toEqual([]);
    expect(base.session.status().tokens).toBeGreaterThan(5_000);
    expect(base.session.status().tokens).toBeLessThan(8_000);
  });

  it("re-evaluates the trigger after /model: a smaller window compacts on the next turn", async () => {
    const models = [fakeModel("test/model"), fakeModel("test/small", { contextLength: 10_000 })];
    const t = await setup(
      [...writes(3, 3_000), answerTurn("done"), answerTurn("ok")],
      {},
      [summary("g")],
      {
        models,
      },
    );
    await t.session.prompt("write");
    expect(compactions(t.events)).toEqual([]);

    await t.session.setModel("test/small");
    await t.session.prompt("next");

    const next = t.fake.requests[4];
    expect(next?.model).toBe("test/small");
    expect(isSummary(next?.context.messages[0])).toBe(true);
    expect(next?.context.messages.at(-1)).toEqual({ role: "user", content: "next" });
    expect(compactions(t.events)).toHaveLength(1);
  });

  it("merges the previous summary on a second compaction: one <summary> block", async () => {
    const t = await setup(
      [...writes(4, 3_000), answerTurn("done")],
      {},
      [summary("first goal"), summary("second goal")],
      { models: [small] },
    );

    await t.session.prompt("go");

    expect(compactions(t.events)).toHaveLength(2);
    const second = t.fake.structuredRequests[1]?.input ?? "";
    expect(second).toContain("<previous-summary>\n## Goal\nfirst goal\n");
    expect(second).toContain("Update the existing structured summary");
    expect(second).not.toContain("[User]: go"); // summarised the first time
    const sent = t.fake.requests[4]?.context.messages ?? [];
    expect(sent.filter((m) => m.content.includes("<summary>"))).toHaveLength(1);
    expect(sent[0]?.content).toContain("second goal");
    expect(sent[0]?.content).not.toContain("first goal");
  });

  it("/compact <instructions> runs now and passes them to the summariser; nothing to do is false", async () => {
    const t = await setup(
      [answerTurn(tokens(3_000)), answerTurn("ok")],
      {
        compaction: { triggerRatio: 0.8, keepRecentTokens: 1_000 },
      },
      [summary("g")],
    );
    expect(await t.session.compact()).toBe(false); // empty context
    await t.session.prompt("one");
    await t.session.prompt("two");

    expect(await t.session.compact("focus on the test failures")).toBe(true);

    const input = t.fake.structuredRequests[0]?.input ?? "";
    expect(input.endsWith("Additional focus: focus on the test failures")).toBe(true);
    expect(t.session.messages.map((m) => m.role)).toEqual(["user", "user", "assistant"]);
    expect(isSummary(t.session.messages[0])).toBe(true);
    expect(compactions(t.events)).toHaveLength(1);
    expect(t.session.running).toBe(false);
  });

  it("refuses /compact while a prompt runs", async () => {
    const t = await setup([textTurn("a")]);
    const run = t.session.prompt("go");

    await expect(t.session.compact()).rejects.toThrow("cannot compact while a prompt is running");
    await run;
  });

  it("context_length_exceeded: compacts and retries the request once; nothing is lost on disk", async () => {
    const store = SessionStore.jsonl(sessionsDir());
    const t = await setup(
      [answerTurn(tokens(3_000)), [overflow], answerTurn("ok")],
      { store, compaction: { triggerRatio: 0.8, keepRecentTokens: 1_000 } },
      [summary("g")],
    );
    await t.session.prompt("one");

    expect(await t.session.prompt("two")).toBe("completed");

    expect(t.fake.requests).toHaveLength(3);
    const retry = t.fake.requests[2]?.context.messages ?? [];
    expect(retry.map((m) => m.role)).toEqual(["user", "user"]);
    expect(isSummary(retry[0])).toBe(true);
    expect(retry[1]).toEqual({ role: "user", content: "two" });
    expect(t.events.filter((e) => e.type === "error")).toEqual([]);
    expect(compactions(t.events)).toHaveLength(1);
    const saved = lines().flatMap((e) => (e.type === "message" ? [e.message.content] : []));
    expect(saved).toEqual(["one", tokens(3_000), "two", "ok"]);
  });

  it("a second overflow after the retry ends with agent_end(error)", async () => {
    const t = await setup(
      [answerTurn(tokens(3_000)), [overflow], [overflow]],
      { compaction: { triggerRatio: 0.8, keepRecentTokens: 1_000 } },
      [summary("g")],
    );
    await t.session.prompt("one");

    expect(await t.session.prompt("two")).toBe("error");

    expect(t.fake.requests).toHaveLength(3);
    expect(t.events).toContainEqual({ type: "error", message: overflow.message });
    expect(t.events.at(-1)).toEqual({ type: "agent_end", reason: "error" });
  });

  it("three failed summaries: one error event, no more attempts, the run goes on", async () => {
    const fail = (n: number) => new Error(`not JSON ${n}`);
    const t = await setup(
      [...writes(6, 3_000), answerTurn("done")],
      {},
      [fail(1), fail(2), fail(3)],
      {
        models: [small],
      },
    );

    expect(await t.session.prompt("go")).toBe("completed");

    // Due before requests 3, 4, 5 and 6; tried 3 times, never again.
    expect(t.fake.structuredRequests).toHaveLength(3);
    const errors = t.events.filter((e) => e.type === "error");
    expect(errors).toHaveLength(1);
    expect(errors[0]).toMatchObject({
      message: expect.stringContaining("compaction failed 3 times"),
    });
    expect(compactions(t.events)).toEqual([]);
    expect(t.fake.requests).toHaveLength(7);
  });

  it("the session figure: a compaction entry loads as [summary][tail] + activations, the originals stay on disk", async () => {
    const tools = builtinRegistry({ tmpDir: join(root(), "tmp") });
    registerDeferred(tools);
    tools.activate(["mcp__x__lookup"]);
    const t = await setup(
      [...writes(3, 3_000), answerTurn("done")],
      { tools, store: SessionStore.jsonl(sessionsDir()) },
      [summary("write three notes")],
      { models: [small] },
    );
    await t.session.prompt("write three notes");

    const restored = SessionStore.jsonl(sessionsDir()).load(t.session.id ?? "");

    expect(restored.messages).toEqual(t.session.messages);
    expect(restored.messages.map((m) => m.role)).toEqual([
      "user",
      "assistant",
      "tool",
      "assistant",
      "tool",
      "assistant",
    ]);
    expect(isSummary(restored.messages[0])).toBe(true);
    expect(restored.activated).toEqual(["mcp__x__lookup"]);
    const entries = lines();
    const messages = entries.filter((e) => e.type === "message");
    expect(messages).toHaveLength(8); // the prompt, 4 answers, 3 results
    const compaction = entries.find((e) => e.type === "compaction");
    const firstKept = messages.find(
      (e) => e.message.role === "assistant" && e.message.toolCalls[0]?.id === "c2",
    );
    expect(compaction).toMatchObject({ firstKeptEntryId: firstKept?.id });
    expect(entries.findIndex((e) => e.type === "tool_activation")).toBeLessThan(
      entries.indexOf(compaction as Entry),
    );

    // Resumed, it sends the figure and compacts again from the right entry.
    const resumedTools = builtinRegistry({ tmpDir: join(root(), "tmp") });
    registerDeferred(resumedTools);
    const resumed = await setup(
      [writeTurn("d1", 3_000), writeTurn("d2", 3_000), answerTurn("again")],
      { tools: resumedTools, store: SessionStore.jsonl(sessionsDir()), resume: t.session.id },
      [summary("second")],
      { models: [small] },
    );
    await resumed.session.prompt("more");
    const first = resumed.fake.requests[0];
    expect(isSummary(first?.context.messages[0])).toBe(true);
    expect(first?.tools.map((s) => s.name)).toContain("mcp__x__lookup");
    expect(compactions(resumed.events)).toHaveLength(1);
    const reloaded = SessionStore.jsonl(sessionsDir()).load(t.session.id ?? "");
    expect(reloaded.messages).toEqual(resumed.session.messages);
    expect(reloaded.messages[0]?.content).toContain("second");
    expect(reloaded.messages.length).toBeGreaterThan(1);
  });
});
