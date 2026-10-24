import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import { FakeProvider, fakeDone, fakeModel } from "../../src/provider/fake.ts";
import type { StreamEvent } from "../../src/provider/types.ts";
import { AgentSession } from "../../src/session.ts";
import { type Entry, SessionStore } from "../../src/session-store/jsonl.ts";
import { builtinRegistry } from "../../src/tools/builtins.ts";
import { COMMANDS, runCommand } from "../../src/tui/commands.ts";
import { useTmpDir } from "../helpers/tmp.ts";

const root = useTmpDir("commands");
const textTurn = (text: string): StreamEvent[] => [fakeDone(text)];

async function setup(streams: StreamEvent[][] = [], trusted = true) {
  const home = join(root(), "home");
  const cwd = join(root(), "repo");
  mkdirSync(home, { recursive: true });
  mkdirSync(cwd, { recursive: true });
  const fake = new FakeProvider({
    streams,
    models: [fakeModel("test/model"), fakeModel("anthropic/claude-haiku-5.5")],
  });
  const tools = builtinRegistry({ tmpDir: join(home, "tmp") });
  tools.register({
    name: "mcp__x__lookup",
    description: "d",
    promptSnippet: "s",
    parameters: z.object({}),
    exposure: "deferred",
    isReadOnly: true,
    execute: async () => ({ content: "" }),
  });
  const sessions = join(home, "sessions");
  const session = await AgentSession.create({
    provider: fake,
    model: "test/model",
    tools,
    cwd,
    maxTurns: 5,
    trusted,
    contextMaxChars: 40_000,
    env: { TREE_AGENT_HOME: home },
    store: SessionStore.jsonl(sessions),
  });
  const files = () =>
    readdirSync(sessions).flatMap((d) =>
      readdirSync(join(sessions, d)).map((f) => join(sessions, d, f)),
    );
  const entries = (file: string) =>
    readFileSync(file, "utf8")
      .trimEnd()
      .split("\n")
      .map((l) => JSON.parse(l) as Entry);
  return { fake, session, tools, cwd, home, files, entries };
}

describe("runCommand", () => {
  it("/model <id> appends model_change and the next request uses the new model", async () => {
    const t = await setup([textTurn("a"), textTurn("b")]);
    await t.session.prompt("one");

    const result = await runCommand(t.session, "model", "anthropic/claude-haiku-5.5");
    await t.session.prompt("two");

    expect(result).toEqual({ lines: ["model: anthropic/claude-haiku-5.5"] });
    expect(t.fake.requests.map((r) => r.model)).toEqual([
      "test/model",
      "anthropic/claude-haiku-5.5",
    ]);
    const [file = ""] = t.files();
    expect(t.entries(file).filter((e) => e.type === "model_change")).toMatchObject([
      { model: "anthropic/claude-haiku-5.5" },
    ]);
  });

  it("/model with an unknown id is an error line and keeps the model", async () => {
    const t = await setup();

    const result = await runCommand(t.session, "model", "no/such");

    expect(result.error).toBe(true);
    expect(result.lines[0]).toMatch(/unknown model "no\/such"/);
    expect(t.session.model).toBe("test/model");
  });

  it("/model without an id shows the current one", async () => {
    const t = await setup();

    expect(await runCommand(t.session, "model", "")).toEqual({ lines: ["model: test/model"] });
  });

  it("/tools lists exactly the direct and the activated tools", async () => {
    const t = await setup();
    expect(await runCommand(t.session, "tools", "")).toEqual({
      lines: ["tools (4): read, bash, edit, write"],
    });

    t.tools.activate(["mcp__x__lookup"]);

    expect(await runCommand(t.session, "tools", "")).toEqual({
      lines: ["tools (5): read, bash, edit, write, mcp__x__lookup"],
    });
  });

  it("/reload re-reads AGENTS.md: the change is in the next recorded system prompt", async () => {
    const t = await setup([textTurn("a"), textTurn("b")]);
    writeFileSync(join(t.cwd, "AGENTS.md"), "- use tabs");
    await t.session.reload();
    await t.session.prompt("one");
    writeFileSync(join(t.cwd, "AGENTS.md"), "- use spaces");

    const result = await runCommand(t.session, "reload", "");
    await t.session.prompt("two");

    expect(result.lines).toEqual(["reloaded 1 context file(s)"]);
    expect(t.fake.requests[0]?.context.system).toContain("- use tabs");
    expect(t.fake.requests[1]?.context.system).toContain("- use spaces");
    expect(t.fake.requests[1]?.context.system).not.toContain("- use tabs");
  });

  it("/clear starts a new session file", async () => {
    const t = await setup([textTurn("a"), textTurn("b")]);
    await t.session.prompt("one");
    const first = t.session.id;

    const result = await runCommand(t.session, "clear", "");
    await t.session.prompt("two");

    expect(result).toEqual({ lines: ["new session"], cleared: true });
    expect(t.session.id).not.toBe(first);
    expect(t.files()).toHaveLength(2);
    expect(t.fake.requests[1]?.context.messages).toEqual([{ role: "user", content: "two" }]);
  });

  it("/mode ask|auto switches the session's permission mode and shows it", async () => {
    const t = await setup();

    expect(await runCommand(t.session, "mode", "")).toEqual({ lines: ["permission mode: ask"] });
    expect(await runCommand(t.session, "mode", "auto")).toEqual({
      lines: ["permission mode: auto"],
    });
    expect(t.session.status().mode).toBe("auto");
    expect(await runCommand(t.session, "mode", "ask")).toEqual({ lines: ["permission mode: ask"] });
    expect(t.session.permissionMode).toBe("ask");
  });

  it("/mode with another value is an error line and keeps the mode", async () => {
    const t = await setup();

    expect(await runCommand(t.session, "mode", "bypass")).toEqual({
      lines: ["usage: /mode ask|auto (got bypass)"],
      error: true,
    });
    expect(t.session.permissionMode).toBe("ask");
  });

  it("/exit asks the TUI to exit", async () => {
    const t = await setup();

    expect(await runCommand(t.session, "exit", "")).toEqual({ lines: [], exit: true });
  });

  it("/help lists the commands, the keys, the session and the context files", async () => {
    const t = await setup([textTurn("a")], false);
    writeFileSync(join(t.home, "AGENTS.md"), "user rules");
    await t.session.reload();
    await t.session.prompt("hi");

    const { lines } = await runCommand(t.session, "help", "");

    for (const c of COMMANDS) expect(lines.some((l) => l.startsWith(`/${c.name}`))).toBe(true);
    expect(lines.join("\n")).toContain("Alt+Enter follow-up");
    expect(lines).toContain(
      `session: ${t.session.id} · untrusted directory: user-level config only`,
    );
    expect(lines.slice(-2)).toEqual(["context files:", `  ${join(t.home, "AGENTS.md")}`]);
  });

  it("answers an unknown command with an error line", async () => {
    const t = await setup();

    expect(await runCommand(t.session, "nope", "")).toEqual({
      lines: ["unknown command /nope (see /help)"],
      error: true,
    });
  });
});
