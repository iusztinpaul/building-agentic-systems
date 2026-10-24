import { spawnSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { stripTerminalSequences, type TUI } from "@earendil-works/pi-tui";
import { describe, expect, it } from "vitest";
import { FakeProvider, fakeDone, fakeModel } from "../../src/provider/fake.ts";
import type { ChatRequest, Provider, StreamEvent } from "../../src/provider/types.ts";
import { AgentSession, type SessionOptions } from "../../src/session.ts";
import { SessionStore } from "../../src/session-store/jsonl.ts";
import { builtinRegistry } from "../../src/tools/builtins.ts";
import { runTui } from "../../src/tui/app.ts";
import type { AgentEvent } from "../../src/types.ts";
import { FakeTerminal, settle } from "../helpers/terminal.ts";
import { useTmpDir } from "../helpers/tmp.ts";

const root = useTmpDir("tui");

// Streams `text` in two deltas, holding the turn open between them
// until the test calls release() (once per turn, before or after the
// turn reaches the hold) or the run is aborted.
class HeldProvider implements Provider {
  readonly requests: ChatRequest[] = [];
  #permits = 0;
  #waiter: (() => void) | undefined;

  async modelInfo(id: string) {
    return fakeModel(id, { contextLength: 100_000, pricing: { prompt: 0.00001, completion: 0 } });
  }

  async *stream(req: ChatRequest, signal: AbortSignal): AsyncGenerator<StreamEvent> {
    this.requests.push(structuredClone(req));
    const text = `answer ${this.requests.length}`;
    yield { type: "text_delta", text: text.slice(0, 3) };
    if (this.#permits > 0) this.#permits--;
    else if (!signal.aborted) {
      await new Promise<void>((resolve) => {
        this.#waiter = resolve;
        signal.addEventListener("abort", () => resolve());
      });
      this.#waiter = undefined;
    }
    if (signal.aborted) {
      const partial = text.slice(0, 3);
      yield {
        type: "done",
        message: { role: "assistant", content: partial, toolCalls: [], stopReason: "aborted" },
      };
      return;
    }
    yield { type: "text_delta", text: text.slice(3) };
    yield fakeDone(text, [], { inputTokens: 5000, outputTokens: 10 });
  }

  release(): void {
    if (this.#waiter) this.#waiter();
    else this.#permits++;
  }

  complete(): Promise<never> {
    throw new Error("unused");
  }

  completeText(): Promise<never> {
    throw new Error("unused");
  }
}

async function start(provider: Provider, o: Partial<SessionOptions> & { askTrust?: boolean } = {}) {
  const home = join(root(), "home");
  const cwd = join(root(), "repo");
  mkdirSync(home, { recursive: true });
  mkdirSync(cwd, { recursive: true });
  const terminal = new FakeTerminal();
  const opened: boolean[] = [];
  let session: AgentSession | undefined;
  let tui: TUI | undefined;
  const events: AgentEvent[] = [];
  const exit = runTui({
    terminal,
    cwd,
    askTrust: o.askTrust ?? false,
    open: async (trust) => {
      opened.push(trust);
      session = await AgentSession.create({
        provider,
        model: "test/model",
        tools: builtinRegistry({ tmpDir: join(home, "tmp") }),
        cwd,
        maxTurns: 5,
        trusted: trust,
        contextMaxChars: 40_000,
        env: { TREE_AGENT_HOME: home },
        ...o,
      });
      session.on((e) => events.push(e));
      return session;
    },
    onStart: (t) => {
      tui = t;
    },
  });
  const screen = () =>
    (tui?.render(100) ?? []).map((l) => stripTerminalSequences(l).trim()).filter(Boolean);
  lastScreen = () => screen().join("\n");
  const current = () => {
    if (!session) throw new Error("session not open yet");
    return session;
  };
  // Ready once the trust question or the status bar is on screen.
  await until(() => screen().some((l) => l.includes("Trust this") || l.includes(" · 0 MCP · ")));
  return { terminal, exit, opened, screen, session: current, events, cwd, home };
}

let lastScreen = () => "";
const until = async (check: () => boolean) => {
  for (let i = 0; i < 100 && !check(); i++) await settle(20);
  if (!check()) throw new Error(`timed out; screen:\n${lastScreen()}`);
};

describe("runTui", () => {
  it("exits 0 on /exit and restores the terminal", async () => {
    const t = await start(new FakeProvider());

    t.terminal.type("/exit");
    t.terminal.send("\r");

    expect(await t.exit).toBe(0);
    expect(t.terminal.stopped).toBe(true);
  });

  it("sends Enter to prompt() when idle and streams the answer into the transcript", async () => {
    const provider = new HeldProvider();
    const t = await start(provider);

    t.terminal.type("hello");
    t.terminal.send("\r");
    await until(() => t.screen().includes("ans"));
    expect(t.session().running).toBe(true);
    provider.release();
    await until(() => !t.session().running);

    const screen = t.screen();
    expect(screen).toContain("› hello");
    expect(screen).toContain("answer 1");
    expect(screen.at(-1)).toBe(
      "test/model · ask · 5k / 100k (5 %) · $0.0500 · 0 MCP · 0 activated",
    );
    t.terminal.send("\x03");
    expect(await t.exit).toBe(0);
  });

  it("steers on Enter and queues a follow-up on Alt+Enter during a run", async () => {
    const provider = new HeldProvider();
    const t = await start(provider);
    t.terminal.type("edit the files");
    t.terminal.send("\r");
    await until(() => t.session().running);

    t.terminal.type("skip the tests for now");
    t.terminal.send("\r");
    t.terminal.type("when done, summarise the diff");
    t.terminal.send("\x1b\r");
    provider.release();
    await until(() => provider.requests.length === 2);
    provider.release();
    await until(() => provider.requests.length === 3);
    provider.release();
    await until(() => !t.session().running);

    const users = (i: number) =>
      provider.requests[i]?.context.messages.filter((m) => m.role === "user").map((m) => m.content);
    expect(users(1)).toEqual(["edit the files", "skip the tests for now"]);
    expect(users(2)).toEqual([
      "edit the files",
      "skip the tests for now",
      "when done, summarise the diff",
    ]);
    expect(t.screen()).toContain("› skip the tests for now (steer)");
    expect(t.screen()).toContain("› when done, summarise the diff (follow-up)");
    t.terminal.send("\x03");
    await t.exit;
  });

  it("aborts on Esc: the partial text stays, the input is live again, the gauge stays", async () => {
    const provider = new HeldProvider();
    const store = SessionStore.inMemory();
    const t = await start(provider, { store });
    t.terminal.type("first");
    t.terminal.send("\r");
    await until(() => t.session().running);
    provider.release();
    await until(() => !t.session().running);
    t.terminal.type("a long answer please");
    t.terminal.send("\r");
    await until(() => t.screen().includes("ans"));

    t.terminal.send("\x1b");
    await until(() => !t.session().running);

    expect(t.screen()).toContain("(aborted)");
    expect(t.screen().at(-1)).toContain("5k / 100k (5 %) · $0.0500");
    const saved = store.load(t.session().id ?? "").messages.at(-1);
    expect(saved).toMatchObject({ content: "ans", stopReason: "aborted" });
    t.terminal.type("/exit");
    t.terminal.send("\r");
    expect(await t.exit).toBe(0);
  });

  it("expands tool results with Tab on an empty input", async () => {
    const call = { id: "c1", name: "bash", arguments: { command: "printf 'one\\ntwo'" } };
    const t = await start(
      new FakeProvider({ streams: [[fakeDone("", [call])], [fakeDone("done")]] }),
    );
    t.terminal.type("run it");
    t.terminal.send("\r");
    await until(() => t.screen().includes("done"));
    expect(t.screen()).toContain("one (+1 lines, Tab)");

    t.terminal.send("\t");
    await settle();

    expect(t.screen()).toContain("two");
    t.terminal.send("\x03");
    await t.exit;
  });

  it("asks for trust first and opens the session with the answer", async () => {
    for (const [key, trust] of [
      ["y", true],
      ["n", false],
    ] as const) {
      const t = await start(new FakeProvider(), { askTrust: true });
      expect(t.screen().join("\n")).toContain(`Trust this directory? ${t.cwd}`);
      expect(t.opened).toEqual([]);

      t.terminal.send(key);
      await until(() => t.screen().some((l) => l.includes(" · 0 MCP · ")));

      expect(t.opened).toEqual([trust]);
      expect(t.screen().join("\n")).not.toContain("Trust this directory?");
      t.terminal.send("\x03");
      await t.exit;
    }
  });

  it("shows a resumed session's transcript", async () => {
    const store = SessionStore.inMemory();
    const id = store.create(join(root(), "repo"), "test/model");
    store.append(id, { type: "message", message: { role: "user", content: "yesterday" } });
    store.append(id, {
      type: "message",
      message: { role: "assistant", content: "an old answer", toolCalls: [], stopReason: "stop" },
    });

    const t = await start(new FakeProvider(), { store, resume: id });

    expect(t.screen()).toEqual(
      expect.arrayContaining(["› yesterday", "an old answer", `resumed session ${id}`]),
    );
    t.terminal.send("\x03");
    await t.exit;
  });

  it("clears the transcript on /clear and shows errors from commands", async () => {
    const t = await start(new FakeProvider({ streams: [[fakeDone("old answer")]] }));
    t.terminal.type("hi");
    t.terminal.send("\r");
    await until(() => t.screen().includes("old answer"));

    t.terminal.type("/clear");
    t.terminal.send("\r");
    await until(() => t.screen().includes("new session"));
    t.terminal.type("/bogus");
    t.terminal.send("\r");
    await until(() => t.screen().includes("unknown command /bogus (see /help)"));

    expect(t.screen()).not.toContain("old answer");
    t.terminal.send("\x03");
    await t.exit;
  });

  it("aborts a running prompt on Ctrl+C and exits on a second Ctrl+C", async () => {
    const provider = new HeldProvider();
    const t = await start(provider);
    t.terminal.type("go");
    t.terminal.send("\r");
    await until(() => t.session().running);

    t.terminal.send("\x03");
    await until(() => !t.session().running);
    t.terminal.send("\x03");

    expect(await t.exit).toBe(0);
    expect(t.events.find((e) => e.type === "agent_end")).toEqual({
      type: "agent_end",
      reason: "aborted",
    });
  });

  it("restores the terminal and rethrows when the session cannot open", async () => {
    const terminal = new FakeTerminal();
    const run = runTui({
      terminal,
      cwd: "/",
      askTrust: false,
      open: async () => {
        throw new Error("settings.json: invalid settings");
      },
    });

    await expect(run).rejects.toThrow("settings.json: invalid settings");
    expect(terminal.stopped).toBe(true);
  });
});

describe("the TUI's imports", () => {
  it("never reach the provider, the loop or the tools (types.ts and session.ts only)", () => {
    const tui = fileURLToPath(new URL("../../src/tui", import.meta.url));
    const grep = spawnSync("grep", ["-rln", "provider/\\|loop/\\|tools/", tui], {
      encoding: "utf8",
    });

    expect(grep.stdout).toBe("");
    expect(grep.status).toBe(1); // no match; 2 would be an error
  });
});
