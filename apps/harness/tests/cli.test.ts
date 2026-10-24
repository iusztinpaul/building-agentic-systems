import { spawnSync } from "node:child_process";
import { EventEmitter } from "node:events";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { type CliDeps, main, resolveMode, type Stdin } from "../src/cli.ts";
import { FakeProvider, fakeDone, fakeModel } from "../src/provider/fake.ts";
import { OpenRouterProvider } from "../src/provider/openrouter.ts";
import type { Provider, StreamEvent } from "../src/provider/types.ts";
import type { AgentEvent, ToolCall } from "../src/types.ts";
import { fakeFetch, fixture, jsonResponse, sseResponse } from "./helpers/http.ts";
import { gone } from "./helpers/tmp.ts";

const CLI = fileURLToPath(new URL("../src/cli.ts", import.meta.url));

let home: string;

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "tree-agent-cli-"));
});

afterEach(() => {
  rmSync(home, { recursive: true, force: true });
});

function spawnCli(args: string[], extraEnv: Record<string, string> = {}) {
  // A clean env: no inherited OPENROUTER_API_KEY, a scratch TREE_AGENT_HOME.
  const env = { PATH: process.env.PATH ?? "", TREE_AGENT_HOME: home, ...extraEnv };
  return spawnSync(process.execPath, [CLI, ...args], { encoding: "utf8", env });
}

interface RunOptions {
  key?: string;
  stdin?: Stdin;
  env?: NodeJS.ProcessEnv;
  signals?: CliDeps["signals"];
}

async function run(
  argv: string[],
  provider: Provider | (() => Provider),
  { key = "test-key", stdin, env = {}, signals }: RunOptions = {},
) {
  const out: string[] = [];
  const err: string[] = [];
  const created: string[] = [];
  const deps: CliDeps = {
    env: { TREE_AGENT_HOME: home, ...(key && { OPENROUTER_API_KEY: key }), ...env },
    stdin,
    signals,
    stdout: { write: (t: string) => out.push(t) },
    stderr: { write: (t: string) => err.push(t) },
    createProvider: (apiKey) => {
      created.push(apiKey);
      return typeof provider === "function" ? provider() : provider;
    },
  };
  const code = await main(argv, deps);
  return { code, stdout: out.join(""), stderr: err.join(""), created };
}

describe("tree-agent (spawned)", () => {
  it("prints usage to stderr and exits 1 without a prompt", () => {
    const result = spawnCli([]);

    expect(result.status).toBe(1);
    expect(result.stdout).toBe("");
    expect(result.stderr).toMatch(/^usage: tree-agent /m);
  });

  it("names OPENROUTER_API_KEY on one stderr line and exits 1 when the key is missing", () => {
    const result = spawnCli(["hi"]);

    expect(result.status).toBe(1);
    expect(result.stdout).toBe("");
    expect(result.stderr.split("\n").filter((l) => !l.includes("ExperimentalWarning"))).toEqual([
      "OPENROUTER_API_KEY is not set (add it to .env)",
      "",
    ]);
  });
});

describe("main", () => {
  it("streams the text to stdout, ends with a newline and exits 0", async () => {
    const fake = new FakeProvider({
      streams: [
        [{ type: "text_delta", text: "Hi" }, { type: "text_delta", text: "!" }, fakeDone("Hi!")],
      ],
    });

    const result = await run(["say", "hi"], fake);

    expect(result).toMatchObject({ code: 0, stdout: "Hi!\n", stderr: "" });
    const [req] = fake.requests;
    expect(req?.model).toBe("anthropic/claude-sonnet-5.5");
    expect(req?.tools.map((t) => t.name)).toEqual(["read", "bash", "edit", "write"]);
    expect(req?.context.messages).toEqual([{ role: "user", content: "say hi" }]);
    expect(req?.context.system).toMatch(/^You are tree-agent/);
  });

  it("uses --model for the request", async () => {
    const fake = new FakeProvider({ streams: [[fakeDone("ok")]] });

    await run(["hi", "--model", "openai/gpt-5"], fake);

    expect(fake.requests[0]?.model).toBe("openai/gpt-5");
  });

  it("creates no provider and sends nothing when the key is missing", async () => {
    const result = await run(["hi"], new FakeProvider(), { key: "" });

    expect(result).toMatchObject({
      code: 1,
      stdout: "",
      stderr: "OPENROUTER_API_KEY is not set (add it to .env)\n",
      created: [],
    });
  });

  it("exits 1 naming a settings.json decisionModel that lacks structured_outputs", async () => {
    writeFileSync(join(home, "settings.json"), JSON.stringify({ decisionModel: "weak/model" }));
    const fake = new FakeProvider({
      models: [fakeModel("weak/model", { supported: new Set(["tools"]) })],
      streams: [[fakeDone("never")]],
    });

    const result = await run(["hi"], fake);

    expect(result).toMatchObject({
      code: 1,
      stdout: "",
      stderr: "decisionModel weak/model does not support structured_outputs\n",
    });
    expect(fake.requests).toEqual([]);
  });

  it("prints a provider error's message and exits 1", async () => {
    const fake = new FakeProvider({
      streams: [[{ type: "error", code: "401", message: "User not found." }]],
    });

    const result = await run(["hi"], fake);

    expect(result).toMatchObject({ code: 1, stdout: "", stderr: "User not found.\n" });
  });

  it("exits 1 with the usage line on an unknown flag", async () => {
    const result = await run(["hi", "--nope"], new FakeProvider());

    expect(result.code).toBe(1);
    expect(result.stderr).toMatch(/Unknown option '--nope'[\s\S]*usage: tree-agent /);
  });

  it("runs end to end on OpenRouterProvider with a recorded stream", async () => {
    const models = {
      data: [
        {
          id: "anthropic/claude-haiku-5.5",
          context_length: 1_000_000,
          top_provider: { max_completion_tokens: 128_000 },
          supported_parameters: ["structured_outputs", "tools"],
          pricing: { prompt: "0.0000001", completion: "0.0000005" },
        },
      ],
    };
    const http = fakeFetch((call) =>
      call.method === "GET" ? jsonResponse(models) : sseResponse(fixture("say-hi.recorded.sse")),
    );

    const result = await run(
      ["say hi"],
      () => new OpenRouterProvider({ apiKey: "test-key", fetch: http.fetch }),
    );

    expect(result).toMatchObject({ code: 0, stdout: "Hi.\n", stderr: "", created: ["test-key"] });
    expect(http.calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      "GET https://openrouter.ai/api/v1/models",
      "POST https://openrouter.ai/api/v1/chat/completions",
    ]);
  });
});

const piped = (text: string): Stdin => ({ isTTY: false, isPipe: true, read: async () => text });
const tty: Stdin = { isTTY: true, isPipe: false, read: async () => "" };
const textTurn = (text: string): StreamEvent[] => [{ type: "text_delta", text }, fakeDone(text)];
const toolTurn = (...calls: ToolCall[]): StreamEvent[] => [fakeDone("", calls)];
const bashCall = (id: string): ToolCall => ({ id, name: "bash", arguments: { command: "true" } });

describe("resolveMode", () => {
  it.each([
    { argv: ["hi"], isTTY: true, pipe: false, kind: "headless", from: "argv" },
    { argv: ["hi"], isTTY: false, pipe: true, kind: "headless", from: "argv" },
    { argv: [], isTTY: false, pipe: true, kind: "headless", from: "stdin" },
    { argv: ["--json"], isTTY: false, pipe: true, kind: "headless", from: "stdin" },
    { argv: ["--json"], isTTY: true, pipe: false, kind: "usage" },
    { argv: [], isTTY: true, pipe: false, kind: "tui" },
    { argv: [], isTTY: false, pipe: false, kind: "usage" },
  ])("$argv, TTY $isTTY, pipe $pipe → $kind", ({ argv, isTTY, pipe, kind, from }) => {
    const mode = resolveMode(argv, isTTY, pipe);

    expect(mode.kind).toBe(kind);
    if (from) expect(mode).toMatchObject({ from });
  });

  it("parses the M1 flags with their defaults", () => {
    expect(resolveMode(["do it"], true, false)).toEqual({
      kind: "headless",
      from: "argv",
      args: {
        prompt: "do it",
        model: undefined,
        maxTurns: 50,
        cwd: undefined,
        trust: false,
        json: false,
      },
    });
    expect(
      resolveMode(
        ["x", "--model", "a/b", "--max-turns", "3", "--cwd", "/w", "--trust", "--json"],
        true,
        false,
      ),
    ).toMatchObject({
      args: { prompt: "x", model: "a/b", maxTurns: 3, cwd: "/w", trust: true, json: true },
    });
  });

  it.each([["--nope"], ["--max-turns", "0"], ["--max-turns", "two"], ["--model"]])(
    "answers usage with an error for %s",
    (...argv) => {
      const mode = resolveMode(["hi", ...argv], true, false);

      expect(mode.kind).toBe("usage");
      expect(mode).toHaveProperty("error");
    },
  );
});

describe("main (headless)", () => {
  it("runs the first stdin line as the prompt and later lines as follow-ups in one session", async () => {
    const fake = new FakeProvider({ streams: [textTurn("A"), textTurn("B")] });

    const result = await run([], fake, { stdin: piped("a\nb\n") });

    expect(result).toMatchObject({ code: 0, stdout: "A\nB\n", stderr: "" });
    expect(fake.requests).toHaveLength(2);
    expect(fake.requests[1]?.context.messages.map((m) => m.content)).toEqual(["a", "A", "b"]);
  });

  it("prints two answers for 'What is 2+2?\\nAnd 3+3?\\n' on stdin", async () => {
    const fake = new FakeProvider({ streams: [textTurn("4"), textTurn("6")] });

    const result = await run([], fake, { stdin: piped("What is 2+2?\nAnd 3+3?\n") });

    expect(result.stdout).toBe("4\n6\n");
    expect(fake.requests[1]?.context.messages.at(-1)).toEqual({
      role: "user",
      content: "And 3+3?",
    });
  });

  it("prints usage and exits 1 for empty piped stdin", async () => {
    const fake = new FakeProvider();

    const result = await run([], fake, { stdin: piped("\n  \n") });

    expect(result.code).toBe(1);
    expect(result.stderr).toMatch(/^usage: tree-agent /);
    expect(result.created).toEqual([]);
  });

  it("does not read stdin when a positional prompt is given", async () => {
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    await run(["positional"], fake, { stdin: piped("from stdin\n") });

    expect(fake.requests[0]?.context.messages).toEqual([{ role: "user", content: "positional" }]);
  });

  it("prints usage and exits 1 on a TTY with no prompt (TUI placeholder until task 205)", async () => {
    const result = await run([], new FakeProvider(), { stdin: tty });

    expect(result).toMatchObject({ code: 1, stdout: "", created: [] });
    expect(result.stderr).toMatch(/^usage: tree-agent /);
  });

  it("prints usage and exits 1 with no TTY and no prompt", async () => {
    const result = await run([], new FakeProvider(), {
      stdin: { isTTY: false, isPipe: false, read: async () => "" },
    });

    expect(result).toMatchObject({ code: 1, stdout: "", created: [] });
    expect(result.stderr).toMatch(/^usage: tree-agent /);
  });

  it("emits JSONL with --json: every line parses to an AgentEvent", async () => {
    const fake = new FakeProvider({ streams: [toolTurn(bashCall("c1")), textTurn("done")] });

    const result = await run(["go", "--json", "--cwd", home], fake);

    expect(result.code).toBe(0);
    const lines = result.stdout.trimEnd().split("\n");
    const events = lines.map((line) => JSON.parse(line) as AgentEvent);
    const types = new Set([
      "turn_start",
      "text_delta",
      "tool_call",
      "tool_result",
      "turn_end",
      "agent_end",
      "error",
    ]);
    expect(events.every((e) => types.has(e.type))).toBe(true);
    expect(events.map((e) => e.type)).toEqual([
      "turn_start",
      "turn_end",
      "tool_call",
      "tool_result",
      "turn_start",
      "text_delta",
      "turn_end",
      "agent_end",
    ]);
    expect(events.at(-1)).toEqual({ type: "agent_end", reason: "completed" });
  });

  it.each(["SIGINT", "SIGTERM"] as const)(
    "aborts the run on %s, kills the running bash command and exits 1",
    async (signal) => {
      const pidFile = join(home, "pid");
      const sleep: ToolCall = {
        id: "c1",
        name: "bash",
        arguments: { command: `sleep 30 & echo $! > '${pidFile}'; wait` },
      };
      const fake = new FakeProvider({ streams: [toolTurn(sleep), textTurn("never")] });
      const signals = new EventEmitter();
      const started = Date.now();

      const running = run(["go", "--cwd", home], fake, { signals });
      while (!existsSync(pidFile) || !readFileSync(pidFile, "utf8")) {
        await new Promise((r) => setTimeout(r, 20));
      }
      signals.emit(signal);
      const result = await running;

      expect(Date.now() - started).toBeLessThan(5000);
      expect(result.code).toBe(1);
      expect(result.stderr).toContain("! Command aborted\n");
      expect(result.stderr).toContain("run ended: aborted\n");
      expect(fake.requests).toHaveLength(1);
      expect(await gone(Number(readFileSync(pidFile, "utf8")))).toBe(true);
      expect(signals.listenerCount("SIGINT") + signals.listenerCount("SIGTERM")).toBe(0);
    },
  );

  it("exits 1 on max_turns", async () => {
    const fake = new FakeProvider({
      streams: [toolTurn(bashCall("c1")), toolTurn(bashCall("c2"))],
    });

    const result = await run(["loop", "--max-turns", "1", "--cwd", home], fake);

    expect(result.code).toBe(1);
    expect(result.stderr).toContain("run ended: max_turns\n");
    expect(fake.requests).toHaveLength(1);
  });

  it("exits 1 on an unknown flag and on an invalid --max-turns", async () => {
    expect((await run(["hi", "--resume", "x"], new FakeProvider())).code).toBe(1);
    expect((await run(["hi", "--max-turns", "0"], new FakeProvider())).stderr).toMatch(
      /--max-turns must be a positive integer: 0\nusage: /,
    );
  });

  it("exits 1 naming a --cwd that is not a directory", async () => {
    const result = await run(["hi", "--cwd", join(home, "missing")], new FakeProvider());

    expect(result.code).toBe(1);
    expect(result.stderr).toBe(`--cwd ${join(home, "missing")} is not a directory\n`);
  });

  it("completes a multi-step file edit headless (CI story)", async () => {
    const cwd = join(home, "work");
    mkdirSync(cwd);
    const fake = new FakeProvider({
      streams: [
        toolTurn({ id: "w", name: "write", arguments: { path: "notes.md", content: "- one\n" } }),
        toolTurn({
          id: "e",
          name: "edit",
          arguments: { path: "notes.md", oldText: "- one\n", newText: "- one\n- two\n" },
        }),
        textTurn("Created notes.md with two bullets."),
      ],
    });

    const result = await run(["--json", "--trust", "--cwd", cwd], fake, {
      stdin: piped("Create notes.md with one bullet, then add a second.\n"),
    });

    expect(result.code).toBe(0);
    expect(readFileSync(join(cwd, "notes.md"), "utf8")).toBe("- one\n- two\n");
    const events = result.stdout
      .trimEnd()
      .split("\n")
      .map((l) => JSON.parse(l) as AgentEvent);
    const calls = events.flatMap((e) => (e.type === "tool_call" ? [e.call.name] : []));
    expect(calls).toEqual(["write", "edit"]);
    expect(events.filter((e) => e.type === "tool_result").every((e) => !e.isError)).toBe(true);
    expect(events.at(-1)).toEqual({ type: "agent_end", reason: "completed" });
  });
});

describe("main (trust)", () => {
  function project() {
    const cwd = join(home, "repo");
    mkdirSync(cwd);
    writeFileSync(join(home, "AGENTS.md"), "user rule");
    writeFileSync(join(cwd, "AGENTS.md"), "project rule");
    return cwd;
  }

  async function systemFor(argv: string[], env: NodeJS.ProcessEnv = {}) {
    const fake = new FakeProvider({ streams: [textTurn("ok")] });
    const result = await run(argv, fake, { env });
    expect(result.code).toBe(0);
    return fake.requests[0]?.context.system ?? "";
  }

  it("loads only ~/.tree-agent/AGENTS.md in an untrusted cwd", async () => {
    const cwd = project();

    const system = await systemFor(["hi", "--cwd", cwd]);

    expect(system).toContain(`# ${join(home, "AGENTS.md")}\nuser rule`);
    expect(system).not.toContain("project rule");
  });

  it("adds the project walk with --trust", async () => {
    const cwd = project();

    const system = await systemFor(["hi", "--cwd", cwd, "--trust"]);

    expect(system).toContain(`# ${join(cwd, "AGENTS.md")}\nproject rule`);
  });

  it("adds the project walk with TREE_AGENT_TRUST=1", async () => {
    const cwd = project();

    const system = await systemFor(["hi", "--cwd", cwd], { TREE_AGENT_TRUST: "1" });

    expect(system).toContain("project rule");
  });

  it("loads it without flags for a cwd listed in trusted.json", async () => {
    const cwd = project();
    writeFileSync(join(home, "trusted.json"), JSON.stringify([cwd]));

    const system = await systemFor(["hi", "--cwd", cwd]);

    expect(system).toContain("project rule");
  });

  it("reads .tree-agent/settings.json only in a trusted cwd", async () => {
    const cwd = project();
    mkdirSync(join(cwd, ".tree-agent"));
    writeFileSync(join(cwd, ".tree-agent", "settings.json"), JSON.stringify({ model: "proj/m" }));
    const untrusted = new FakeProvider({ streams: [textTurn("ok")] });
    const trusted = new FakeProvider({ streams: [textTurn("ok")] });

    await run(["hi", "--cwd", cwd], untrusted);
    await run(["hi", "--cwd", cwd, "--trust"], trusted);

    expect(untrusted.requests[0]?.model).toBe("anthropic/claude-sonnet-5.5");
    expect(trusted.requests[0]?.model).toBe("proj/m");
  });
});
