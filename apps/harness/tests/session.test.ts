import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import { FakeProvider, fakeDone, fakeModel } from "../src/provider/fake.ts";
import type { Provider, StreamEvent } from "../src/provider/types.ts";
import { AgentSession, type SessionOptions } from "../src/session.ts";
import { type Entry, SessionStore } from "../src/session-store/jsonl.ts";
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

    expect(seen).toEqual(["session", "user", "assistant"]);
    expect(entries().map((e) => (e.type === "message" ? e.message.role : e.type))).toEqual([
      "session",
      "user",
      "assistant",
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
