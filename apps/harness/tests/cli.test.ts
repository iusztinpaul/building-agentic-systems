import { spawnSync } from "node:child_process";
import { EventEmitter } from "node:events";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type CliDeps, main, resolveMode, type Stdin } from "../src/cli.ts";
import { log } from "../src/log.ts";
import { startMcp } from "../src/mcp/client.ts";
import { FakeProvider, fakeDone, fakeModel } from "../src/provider/fake.ts";
import { OpenRouterProvider } from "../src/provider/openrouter.ts";
import { type Provider, ProviderError, type StreamEvent } from "../src/provider/types.ts";
import { type Entry, SessionStore } from "../src/session-store/jsonl.ts";
import { discoverSkills } from "../src/skills/skills.ts";
import type { AgentEvent, ToolCall } from "../src/types.ts";
import { fakeFetch, fixture, jsonResponse, sseResponse } from "./helpers/http.ts";
import { FakeTerminal, settle } from "./helpers/terminal.ts";
import { gone } from "./helpers/tmp.ts";

const CLI = fileURLToPath(new URL("../src/cli.ts", import.meta.url));

let home: string;

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "tree-agent-cli-"));
});

afterEach(() => {
  vi.restoreAllMocks();
  rmSync(home, { recursive: true, force: true });
});

function spawnCli(args: string[], extraEnv: Record<string, string> = {}) {
  // A clean env: no inherited OPENROUTER_API_KEY, a scratch TREE_AGENT_HOME
  // and HOME (so the real ~/.agents/skills is never read).
  const env = { PATH: process.env.PATH ?? "", HOME: home, TREE_AGENT_HOME: home, ...extraEnv };
  return spawnSync(process.execPath, [CLI, ...args], { encoding: "utf8", env });
}

interface RunOptions {
  key?: string;
  stdin?: Stdin;
  env?: NodeJS.ProcessEnv;
  signals?: CliDeps["signals"];
  terminal?: FakeTerminal;
  discoverSkills?: CliDeps["discoverSkills"];
  startMcp?: CliDeps["startMcp"];
}

async function run(
  argv: string[],
  provider: Provider | (() => Provider),
  {
    key = "test-key",
    stdin,
    env = {},
    signals,
    terminal,
    discoverSkills,
    startMcp,
  }: RunOptions = {},
) {
  const out: string[] = [];
  const err: string[] = [];
  const created: string[] = [];
  const deps: CliDeps = {
    env: {
      HOME: home,
      TREE_AGENT_HOME: home,
      ...(key && { OPENROUTER_API_KEY: key }),
      ...env,
    },
    stdin,
    signals,
    terminal,
    discoverSkills,
    startMcp,
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

  it.each([[["--help"]], [["-h"]], [["hi", "--help"]], [["--json", "-h"]]])(
    "%j prints the usage on stdout and exits 0, with no key and nothing written",
    (argv) => {
      const result = spawnCli(argv);

      expect(result.status).toBe(0);
      expect(result.stderr).toBe("");
      expect(result.stdout).toMatch(/^usage: tree-agent [\s\S]*--help \| -h/);
      expect(readdirSync(home)).toEqual([]);
    },
  );

  it("prints an unknown option's error and the usage on stderr and exits 1", () => {
    const result = spawnCli(["--bogus"]);

    expect(result.status).toBe(1);
    expect(result.stdout).toBe("");
    expect(result.stderr).toMatch(/^Unknown option '--bogus'[\s\S]*\nusage: tree-agent /);
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
          id: "anthropic/claude-sonnet-5.5",
          context_length: 1_000_000,
          top_provider: { max_completion_tokens: 128_000 },
          supported_parameters: ["structured_outputs", "tools"],
          pricing: { prompt: "0.000003", completion: "0.000015" },
        },
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
// The auto-mode classifier's answer for a harmless call.
const safe = { dangerous: false, risk: "low", reason: "cwd-scoped and asked for" };

describe("resolveMode", () => {
  it.each([
    { argv: ["hi"], isTTY: true, pipe: false, kind: "headless", from: "argv" },
    { argv: ["hi"], isTTY: false, pipe: true, kind: "headless", from: "argv" },
    { argv: [], isTTY: false, pipe: true, kind: "headless", from: "stdin" },
    { argv: ["--json"], isTTY: false, pipe: true, kind: "headless", from: "stdin" },
    { argv: ["--json"], isTTY: true, pipe: false, kind: "usage" },
    { argv: [], isTTY: true, pipe: false, kind: "tui" },
    { argv: [], isTTY: false, pipe: false, kind: "usage" },
    { argv: ["--help"], isTTY: true, pipe: false, kind: "help" },
    { argv: ["-h"], isTTY: false, pipe: true, kind: "help" },
    { argv: ["hi", "--help", "--json"], isTTY: false, pipe: false, kind: "help" },
    { argv: ["-h", "--max-turns", "0"], isTTY: true, pipe: false, kind: "help" },
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
        resume: undefined,
        continue: false,
        permissionMode: undefined,
        onBlocked: "stop",
        skills: true,
        mcp: true,
      },
    });
    expect(resolveMode(["x", "--no-skills"], true, false)).toMatchObject({
      args: { skills: false },
    });
    expect(resolveMode(["x", "--no-mcp"], true, false)).toMatchObject({
      args: { mcp: false, skills: true },
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

  it("parses --resume <id> and --continue", () => {
    expect(resolveMode(["--resume", "abc"], true, false)).toMatchObject({
      kind: "tui",
      args: { resume: "abc", continue: false },
    });
    expect(resolveMode(["hi", "--continue"], true, false)).toMatchObject({
      kind: "headless",
      args: { continue: true },
    });
  });

  it.each([
    ["--nope"],
    ["--max-turns", "0"],
    ["--max-turns", "two"],
    ["--model"],
    ["--resume"],
    ["--resume", " "],
    ["--resume", "abc", "--continue"],
  ])("answers usage with an error for %s", (...argv) => {
    const mode = resolveMode(["hi", ...argv], true, false);

    expect(mode.kind).toBe("usage");
    expect(mode).toHaveProperty("error");
  });
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

  it("prints usage and exits 1 with no TTY and no prompt", async () => {
    const result = await run([], new FakeProvider(), {
      stdin: { isTTY: false, isPipe: false, read: async () => "" },
    });

    expect(result).toMatchObject({ code: 1, stdout: "", created: [] });
    expect(result.stderr).toMatch(/^usage: tree-agent /);
  });

  it("emits JSONL with --json: every line parses to an AgentEvent", async () => {
    const fake = new FakeProvider({
      streams: [toolTurn(bashCall("c1")), textTurn("done")],
      answers: [safe],
    });

    const result = await run(["go", "--json", "--cwd", home], fake);

    expect(result.code).toBe(0);
    const lines = result.stdout.trimEnd().split("\n");
    const events = lines.map((line) => JSON.parse(line) as AgentEvent);
    const types = new Set([
      "turn_start",
      "text_delta",
      "tool_call",
      "permission",
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
      "permission",
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
      const fake = new FakeProvider({
        streams: [toolTurn(sleep), textTurn("never")],
        answers: [safe],
      });
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
      answers: [safe],
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

  it("parses --compact-at and rejects a ratio outside 0.3–0.95 at parse time", async () => {
    expect(resolveMode(["x", "--compact-at", "0.5"], true, false)).toMatchObject({
      args: { compactAt: 0.5 },
    });
    for (const bad of ["0.2", "0.99", "half"]) {
      expect(resolveMode(["x", "--compact-at", bad], true, false)).toEqual({
        kind: "usage",
        error: `--compact-at must be from 0.3 to 0.95: ${bad}`,
      });
    }
    const rejected = await run(["hi", "--compact-at", "0.2"], new FakeProvider());
    expect(rejected.code).toBe(1);
    expect(rejected.created).toEqual([]);
  });

  it("--compact-at 0.5 compacts a 10 000 window at 5 000; --json prints the compaction event", async () => {
    const cwd = join(home, "big");
    mkdirSync(cwd);
    for (const name of ["a.txt", "b.txt"]) {
      writeFileSync(join(cwd, name), `${"y".repeat(99)}\n`.repeat(120)); // ≈ 3 000 tokens
    }
    // No usage on the answers: the estimate stays chars / 4.
    const answer = (content: string, toolCalls: ToolCall[] = []): StreamEvent => ({
      type: "done",
      message: {
        role: "assistant",
        content,
        toolCalls,
        stopReason: toolCalls.length ? "tool_calls" : "stop",
      },
    });
    const fake = () =>
      new FakeProvider({
        streams: [
          [answer("", [{ id: "a", name: "read", arguments: { path: "a.txt" } }])],
          [answer("", [{ id: "b", name: "read", arguments: { path: "b.txt" } }])],
          [answer("done")],
        ],
        answers: [
          {
            goal: "read both",
            constraints: [],
            progress: [],
            decisions: [],
            nextSteps: [],
            criticalContext: [],
          },
        ],
        models: [
          fakeModel("anthropic/claude-sonnet-5.5", { contextLength: 10_000 }),
          fakeModel("anthropic/claude-haiku-5.5"),
        ],
      });
    const events = (stdout: string) =>
      stdout
        .trimEnd()
        .split("\n")
        .map((l) => JSON.parse(l) as AgentEvent);

    const half = await run(["read both", "--json", "--cwd", cwd, "--compact-at", "0.5"], fake);
    const base = await run(["read both", "--json", "--cwd", cwd], fake);

    expect(half.code).toBe(0);
    const compaction = events(half.stdout).filter((e) => e.type === "compaction");
    expect(compaction).toEqual([
      { type: "compaction", before: expect.any(Number), after: expect.any(Number) },
    ]);
    const [{ before, after } = { before: 0, after: 0 }] = compaction as {
      before: number;
      after: number;
    }[];
    expect(before).toBeGreaterThanOrEqual(5_000);
    expect(before).toBeLessThan(8_000);
    expect(after).toBeLessThan(before);
    expect(base.code).toBe(0);
    expect(events(base.stdout).filter((e) => e.type === "compaction")).toEqual([]);
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
      answers: [safe, safe],
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

describe("main (skills)", () => {
  // `x` and the hidden `tree-memory` in the project, `mine` in ~/.agents/skills.
  function project(): string {
    const cwd = join(home, "proj");
    const put = (dir: string, name: string, extra = "") => {
      mkdirSync(join(dir, name), { recursive: true });
      writeFileSync(
        join(dir, name, "SKILL.md"),
        `---\nname: ${name}\ndescription: does ${name}\n${extra}---\n# ${name}\nDo the ${name} steps.\n`,
      );
    };
    put(join(cwd, ".agents", "skills"), "x");
    put(join(cwd, ".agents", "skills"), "tree-memory", "disable-model-invocation: true\n");
    put(join(home, ".agents", "skills"), "mine");
    return cwd;
  }
  const listed = (fake: FakeProvider) =>
    [...(fake.requests[0]?.context.system ?? "").matchAll(/<name>([^<]+)<\/name>/g)].map(
      (m) => m[1],
    );

  it("lists the skills of a trusted cwd and the user's, hidden ones left out", async () => {
    const cwd = project();
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    const result = await run(["hi", "--cwd", cwd, "--trust"], fake);

    expect(result).toMatchObject({ code: 0, stderr: "" });
    expect(listed(fake)).toEqual(["mine", "x"]);
    expect(fake.requests[0]?.context.system).toContain(
      `<location>${join(cwd, ".agents", "skills", "x", "SKILL.md")}</location>`,
    );
  });

  it("lists only the user-level skills in an untrusted cwd", async () => {
    const cwd = project();
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    await run(["hi", "--cwd", cwd], fake);

    expect(listed(fake)).toEqual(["mine"]);
  });

  it("--no-skills: no skills section and the walker is never called", async () => {
    const cwd = project();
    const walker = vi.fn(discoverSkills);
    const fake = new FakeProvider({ streams: [textTurn("ok"), textTurn("ok")] });

    const off = await run(["hi", "--cwd", cwd, "--trust", "--no-skills"], fake, {
      discoverSkills: walker,
    });

    expect(off.code).toBe(0);
    expect(walker).not.toHaveBeenCalled();
    expect(fake.requests[0]?.context.system).not.toContain("<skills>");

    await run(["hi", "--cwd", cwd, "--trust"], fake, { discoverSkills: walker });

    expect(walker).toHaveBeenCalledTimes(1);
    expect(fake.requests[1]?.context.system).toContain("<skills>");
  });

  const header = (cwd: string, name: string) =>
    `# ${join(cwd, ".agents", "skills", name, "SKILL.md")}\n`;

  it("/skill:<name> arg1 arg2 sends the SKILL.md path header, the body + the args as the user message", async () => {
    const cwd = project();
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    const result = await run(["/skill:x", "arg1", "arg2", "--cwd", cwd, "--trust"], fake);

    expect(result.code).toBe(0);
    expect(fake.requests[0]?.context.messages).toEqual([
      { role: "user", content: `${header(cwd, "x")}# x\nDo the x steps.\n\narg1 arg2` },
    ]);
  });

  it("/skill:tree-memory on this repo sends its SKILL.md path and substitutes $ARGUMENTS", async () => {
    const repo = fileURLToPath(new URL("../../..", import.meta.url));
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    const result = await run(
      ["/skill:tree-memory what do I know about Modal?", "--cwd", repo, "--trust", "--no-mcp"],
      fake,
    );

    expect(result.code).toBe(0);
    const message = String(fake.requests[0]?.context.messages[0]?.content);
    const [first, ...lines] = message.split("\n");
    expect(first).toBe(`# ${join(repo, ".agents", "skills", "tree-memory", "SKILL.md")}`);
    expect(lines).toContain("The query or instruction to run is: what do I know about Modal?");
    expect(message).not.toContain("$ARGUMENTS");
  });

  it("/skill: reaches a hidden skill, also as the first stdin line", async () => {
    const cwd = project();
    const fake = new FakeProvider({ streams: [textTurn("ok"), textTurn("ok")] });

    const result = await run(["--cwd", cwd, "--trust"], fake, {
      stdin: piped("/skill:tree-memory what do I know about Modal?\nthanks\n"),
    });

    expect(result.code).toBe(0);
    expect(fake.requests[1]?.context.messages.map((m) => m.content)).toEqual([
      `${header(cwd, "tree-memory")}# tree-memory\nDo the tree-memory steps.\n\nwhat do I know about Modal?`,
      "ok",
      "thanks",
    ]);
  });

  it("/skill:nope answers one line with the known names, sends nothing and exits 1", async () => {
    const cwd = project();
    const fake = new FakeProvider();

    const result = await run(["/skill:nope", "--cwd", cwd, "--trust", "--json"], fake);

    expect(result).toMatchObject({
      code: 1,
      stdout: "",
      stderr: 'unknown skill "nope"; known: mine, tree-memory, x\n',
    });
    expect(fake.requests).toEqual([]);
  });

  it("resolves a /skill: follow-up stdin line like the first line", async () => {
    const cwd = project();
    const fake = new FakeProvider({ streams: [textTurn("ok"), textTurn("ok")] });

    const result = await run(["--cwd", cwd, "--trust"], fake, {
      stdin: piped("/skill:x a\n/SKILL:tree-memory b\n"),
    });

    expect(result.code).toBe(0);
    expect(fake.requests[1]?.context.messages.map((m) => m.content)).toEqual([
      `${header(cwd, "x")}# x\nDo the x steps.\n\na`,
      "ok",
      `${header(cwd, "tree-memory")}# tree-memory\nDo the tree-memory steps.\n\nb`,
    ]);
  });

  it.each([
    [
      "an unknown follow-up",
      ["--cwd"],
      "hi\n/skill:nope\n",
      'unknown skill "nope"; known: mine, tree-memory, x',
    ],
    ["a bare /skill:", ["/skill:", "--cwd"], "", 'unknown skill ""; known: mine, tree-memory, x'],
    [
      "a bare /skill: line",
      ["--cwd"],
      "/skill:\n",
      'unknown skill ""; known: mine, tree-memory, x',
    ],
  ])("%s answers one line, sends nothing and exits 1", async (_label, head, stdin, line) => {
    const cwd = project();
    const fake = new FakeProvider();

    const result = await run([...head, cwd, "--trust"], fake, stdin ? { stdin: piped(stdin) } : {});

    expect(result).toMatchObject({ code: 1, stdout: "", stderr: `${line}\n` });
    expect(fake.requests).toEqual([]);
  });

  it("--no-skills + /skill:x says skills are disabled", async () => {
    const cwd = project();
    const fake = new FakeProvider();

    const result = await run(["/skill:x", "--cwd", cwd, "--trust", "--no-skills"], fake);

    expect(result).toMatchObject({
      code: 1,
      stderr: 'unknown skill "x"; skills are disabled (--no-skills)\n',
    });
    expect(fake.requests).toEqual([]);
  });

  it("prints each skill diagnostic once on stderr and still runs, stdout pure JSONL", async () => {
    const cwd = project();
    const bad = join(cwd, ".agents", "skills", "bad", "SKILL.md");
    mkdirSync(dirname(bad));
    writeFileSync(bad, "no frontmatter at all\n");
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    const result = await run(["hi", "--cwd", cwd, "--trust", "--json"], fake);

    expect(result.code).toBe(0);
    expect(result.stderr).toBe(
      `skills: ${bad}: bad frontmatter: the file must start with a --- line\n`,
    );
    for (const line of result.stdout.trimEnd().split("\n"))
      expect(() => JSON.parse(line)).not.toThrow();
    expect(listed(fake)).toEqual(["mine", "x"]);
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

describe("main (sessions)", () => {
  const sessions = () => join(home, "sessions");
  const filesOf = (cwd: string) => {
    const dir = join(sessions(), readdirSync(sessions()).find((d) => d === slugOf(cwd)) ?? "-");
    return existsSync(dir) ? readdirSync(dir).map((f) => join(dir, f)) : [];
  };
  const slugOf = (cwd: string) => `--${cwd.replace(/^\//, "").replace(/[/\\:]/g, "-")}--`;
  const entries = (file: string) =>
    readFileSync(file, "utf8")
      .trimEnd()
      .split("\n")
      .map((l) => JSON.parse(l) as Entry);
  const scratch = (name: string) => {
    const dir = join(home, name);
    mkdirSync(dir, { recursive: true });
    return dir;
  };

  it("saves a headless run as a session file and --resume <id> appends to it", async () => {
    const cwd = scratch("repo");
    await run(["first", "--cwd", cwd], new FakeProvider({ streams: [textTurn("one")] }));
    const [file = ""] = filesOf(cwd);
    const id = /_(.+)\.jsonl$/.exec(file)?.[1] ?? "";
    const fake = new FakeProvider({ streams: [textTurn("two")] });

    const result = await run(["second", "--cwd", cwd, "--resume", id], fake);

    expect(result.code).toBe(0);
    expect(filesOf(cwd)).toEqual([file]);
    expect(fake.requests[0]?.context.messages.map((m) => m.content)).toEqual([
      "first",
      "one",
      "second",
    ]);
    const all = entries(file);
    expect(all.map((e) => e.type)).toEqual(["session", "message", "message", "message", "message"]);
    for (let i = 1; i < all.length; i++) expect(all[i]?.parentId).toBe(all[i - 1]?.id);
  });

  it("--continue picks the newest session of this cwd, not another cwd's", async () => {
    const cwd = scratch("repo");
    const other = scratch("other");
    const store = SessionStore.jsonl(sessions());
    const older = store.create(cwd, "anthropic/claude-sonnet-5.5");
    store.append(older, { type: "message", message: { role: "user", content: "older" } });
    const newer = store.create(cwd, "anthropic/claude-sonnet-5.5");
    store.append(newer, { type: "message", message: { role: "user", content: "newer" } });
    const elsewhere = store.create(other, "anthropic/claude-sonnet-5.5");
    store.append(elsewhere, { type: "message", message: { role: "user", content: "other cwd" } });
    const past = Date.now() / 1000 - 3600;
    for (const f of filesOf(cwd)) if (f.includes(older)) utimesSync(f, past, past);
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    await run(["go on", "--cwd", cwd, "--continue"], fake);

    expect(fake.requests[0]?.context.messages.map((m) => m.content)).toEqual(["newer", "go on"]);
  });

  it.each([
    ["--continue", () => ["--continue"]],
    ["--resume <id>", (id: string) => ["--resume", id]],
  ])(
    "a crash-cut session resumed via %s warns exactly once and mends the file",
    async (_flag, flags) => {
      const cwd = scratch("repo");
      await run(["first", "--cwd", cwd], new FakeProvider({ streams: [textTurn("one")] }));
      const [file = ""] = filesOf(cwd);
      const id = /_(.+)\.jsonl$/.exec(file)?.[1] ?? "";
      appendFileSync(file, '{"id":"cut","type":"mess');
      const warn = vi.spyOn(log, "warn").mockImplementation(() => {});
      const fake = new FakeProvider({ streams: [textTurn("two")] });

      const result = await run(["second", "--cwd", cwd, ...flags(id)], fake);

      expect(result.code).toBe(0);
      const cut = warn.mock.calls.filter(([m]) => m.includes("not a session entry"));
      expect(cut).toHaveLength(1);
      expect(fake.requests[0]?.context.messages.map((m) => m.content)).toEqual([
        "first",
        "one",
        "second",
      ]);
      const lines = readFileSync(file, "utf8").trimEnd().split("\n");
      expect(lines.filter((l) => l.startsWith('{"id":"cut"'))).toEqual([
        '{"id":"cut","type":"mess',
      ]);
      expect(
        lines.filter((l) => !l.startsWith('{"id":"cut"')).map((l) => JSON.parse(l).type),
      ).toEqual(["session", "message", "message", "message", "message"]);
    },
  );

  it("--continue with no session for this cwd starts a new one", async () => {
    const cwd = scratch("fresh");
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    const result = await run(["hi", "--cwd", cwd, "--continue"], fake);

    expect(result.code).toBe(0);
    expect(fake.requests[0]?.context.messages).toEqual([{ role: "user", content: "hi" }]);
    expect(filesOf(cwd)).toHaveLength(1);
  });

  it.each([
    ["headless", ["hi"], undefined],
    ["the TUI", [], tty],
  ])("--resume <unknown> exits 1 with one line (%s)", async (_mode, argv, stdin) => {
    const terminal = new FakeTerminal();

    const result = await run(
      [...argv, "--resume", "nope", "--cwd", scratch("r")],
      new FakeProvider(),
      {
        stdin,
        terminal,
      },
    );

    expect(result).toMatchObject({ code: 1, stdout: "", stderr: "no session nope\n", created: [] });
    expect(terminal.started).toBe(false);
  });

  it("an explicit --model on --resume switches the model with a model_change", async () => {
    const cwd = scratch("repo");
    await run(["first", "--cwd", cwd], new FakeProvider({ streams: [textTurn("one")] }));
    const [file = ""] = filesOf(cwd);
    const id = /_(.+)\.jsonl$/.exec(file)?.[1] ?? "";
    const fake = new FakeProvider({ streams: [textTurn("two")] });

    await run(["second", "--cwd", cwd, "--resume", id, "--model", "x/y"], fake);

    expect(fake.requests[0]?.model).toBe("x/y");
    expect(entries(file).filter((e) => e.type === "model_change")).toMatchObject([
      { model: "x/y" },
    ]);
  });
});

describe("main (TUI)", () => {
  async function tui(argv: string[], env: NodeJS.ProcessEnv = {}) {
    const cwd = join(home, "proj");
    mkdirSync(cwd, { recursive: true });
    writeFileSync(join(cwd, "AGENTS.md"), "project rule");
    const terminal = new FakeTerminal();
    const fake = new FakeProvider({ streams: [textTurn("ok")] });
    const result = run(["--cwd", cwd, ...argv], fake, { stdin: tty, terminal, env });
    await shown(
      terminal,
      argv.includes("--trust") || trusted(cwd) ? STATUS : "Trust this directory?",
    );
    return { cwd, terminal, fake, result };
  }

  const STATUS = " · 0 MCP · "; // the status bar: the session is open
  const trusted = (cwd: string) =>
    existsSync(join(home, "trusted.json")) &&
    readFileSync(join(home, "trusted.json"), "utf8").includes(cwd);
  // Polls up to 2 s for `text` in what the TUI wrote to the terminal.
  async function shown(terminal: FakeTerminal, text: string): Promise<void> {
    for (let i = 0; i < 100 && !terminal.written.join("").includes(text); i++) await settle(20);
    expect(terminal.written.join("")).toContain(text);
  }

  it("opens the TUI on a TTY with no prompt and exits 0 on /exit", async () => {
    const t = await tui(["--trust"]);

    t.terminal.type("/exit");
    t.terminal.send("\r");

    expect(await t.result).toMatchObject({ code: 0, stderr: "" });
    expect(t.terminal.stopped).toBe(true);
  });

  it("asks once in an untrusted cwd: y writes trusted.json and loads the project's AGENTS.md", async () => {
    const t = await tui([]);
    expect(t.terminal.written.join("")).toContain("Trust this directory?");

    t.terminal.send("y");
    await shown(t.terminal, STATUS);
    t.terminal.type("hi");
    t.terminal.send("\r");
    for (let i = 0; i < 100 && t.fake.requests.length === 0; i++) await settle(20);
    t.terminal.send("\x03");
    await t.result;

    expect(JSON.parse(readFileSync(join(home, "trusted.json"), "utf8"))).toEqual([t.cwd]);
    expect(t.fake.requests[0]?.context.system).toContain("project rule");
  });

  it("n keeps the run to user-level config and writes nothing", async () => {
    const t = await tui([]);

    t.terminal.send("n");
    await shown(t.terminal, STATUS);
    t.terminal.type("hi");
    t.terminal.send("\r");
    for (let i = 0; i < 100 && t.fake.requests.length === 0; i++) await settle(20);
    t.terminal.send("\x03");
    await t.result;

    expect(existsSync(join(home, "trusted.json"))).toBe(false);
    expect(t.fake.requests[0]?.context.system).not.toContain("project rule");
  });

  it("/mcp lists 0 servers with only .mcp.json present (no .tree-agent/mcp.json)", async () => {
    const cwd = join(home, "proj");
    mkdirSync(cwd, { recursive: true });
    writeFileSync(
      join(cwd, ".mcp.json"),
      JSON.stringify({ mcpServers: { "tree-memory-local": { command: "uv" } } }),
    );
    const t = await tui(["--trust"]);

    t.terminal.type("/mcp");
    t.terminal.send("\r");
    await shown(t.terminal, "MCP servers (0)");
    t.terminal.send("\x03");

    expect((await t.result).code).toBe(0);
  });

  it("/mcp shows a configured server connected with its counts; the status bar counts it", async () => {
    const cwd = join(home, "proj");
    mkdirSync(join(cwd, ".tree-agent"), { recursive: true });
    const server = fileURLToPath(new URL("./fixtures/fake-mcp-server.mjs", import.meta.url));
    writeFileSync(
      join(cwd, ".tree-agent", "mcp.json"),
      JSON.stringify({
        mcpServers: {
          "fake-server": {
            command: process.execPath,
            args: [server],
            toolExposure: { echo: "direct", read_note: "deferred" },
          },
        },
      }),
    );
    const terminal = new FakeTerminal();
    const result = run(["--cwd", cwd, "--trust"], new FakeProvider({ streams: [] }), {
      stdin: tty,
      terminal,
    });
    await shown(terminal, " · 1 MCP · 0 activated");

    terminal.type("/mcp");
    terminal.send("\r");
    await shown(terminal, "fake-server: 2 tools; direct 1, deferred 1, codemode 0 · connected");
    terminal.send("\x03");

    expect((await result).code).toBe(0);
  });

  it("does not ask in a cwd listed in trusted.json", async () => {
    const cwd = join(home, "proj");
    mkdirSync(cwd, { recursive: true });
    writeFileSync(join(home, "trusted.json"), JSON.stringify([cwd]));
    const t = await tui([]);

    expect(t.terminal.written.join("")).not.toContain("Trust this directory?");
    t.terminal.send("\x03");
    expect((await t.result).code).toBe(0);
  });
});

describe("main (permissions)", () => {
  const work = () => {
    const cwd = join(home, "work");
    mkdirSync(cwd, { recursive: true });
    return cwd;
  };
  const writeA: ToolCall = { id: "w", name: "write", arguments: { path: "a.md", content: "a" } };
  const events = (stdout: string) =>
    stdout
      .trimEnd()
      .split("\n")
      .map((l) => JSON.parse(l) as AgentEvent);
  const sessionEntries = (): Entry[] => {
    const dir = join(home, "sessions", readdirSync(join(home, "sessions"))[0] ?? "");
    const [file = ""] = readdirSync(dir);
    return readFileSync(join(dir, file), "utf8")
      .trimEnd()
      .split("\n")
      .map((l) => JSON.parse(l) as Entry);
  };

  it("headless defaults to auto: a write is classified on the decision model, then runs", async () => {
    const cwd = work();
    // settings.permissions.mode is the TUI's: headless ignores it.
    writeFileSync(join(home, "settings.json"), JSON.stringify({ permissions: { mode: "ask" } }));
    const fake = new FakeProvider({
      streams: [toolTurn(writeA), textTurn("done")],
      answers: [safe],
    });

    const result = await run(["write a.md", "--cwd", cwd], fake);

    expect(result.code).toBe(0);
    expect(readFileSync(join(cwd, "a.md"), "utf8")).toBe("a");
    expect(fake.structuredRequests.map((r) => [r.name, r.model])).toEqual([
      ["danger_check", "anthropic/claude-haiku-5.5"],
    ]);
  });

  it("--permission-mode ask headless: the first write is denied (headless), blocked, exit 2", async () => {
    const cwd = work();
    const fake = new FakeProvider({ streams: [toolTurn(writeA), textTurn("never")] });

    const result = await run(["write a.md", "--cwd", cwd, "--permission-mode", "ask"], fake);

    expect(result.code).toBe(2);
    expect(result.stderr).toContain("run ended: blocked\n");
    expect(existsSync(join(cwd, "a.md"))).toBe(false);
    expect(fake.requests).toHaveLength(1);
    expect(fake.structuredRequests).toEqual([]);
    expect(sessionEntries().filter((e) => e.type === "permission")).toMatchObject([
      { type: "permission", call: writeA, decision: "deny", source: "headless" },
    ]);
  });

  it("--on-blocked continue: the model receives Blocked: … and the run exits 0", async () => {
    const cwd = work();
    const fake = new FakeProvider({
      streams: [toolTurn(writeA), textTurn("I was blocked; here is the text instead.")],
    });

    const result = await run(
      ["write a.md", "--cwd", cwd, "--permission-mode", "ask", "--on-blocked", "continue"],
      fake,
    );

    expect(result.code).toBe(0);
    expect(result.stdout).toBe("I was blocked; here is the text instead.\n");
    expect(fake.requests[1]?.context.messages.at(-1)).toMatchObject({
      role: "tool",
      content: "Blocked: needs approval (permission mode ask) and a headless run cannot prompt",
      isError: true,
    });
  });

  it("--permission-mode auto with a classifier answering dangerous: true exits 2", async () => {
    const cwd = work();
    const push: ToolCall = { id: "p", name: "bash", arguments: { command: "git push --force" } };
    const fake = new FakeProvider({
      streams: [toolTurn(push), textTurn("never")],
      answers: [{ dangerous: true, risk: "high", reason: "force-push rewrites remote history" }],
    });

    const result = await run(
      ["clean up and push", "--cwd", cwd, "--permission-mode", "auto"],
      fake,
    );

    expect(result.code).toBe(2);
    expect(result.stderr).toContain("! Blocked: dangerous: force-push rewrites remote history\n");
    expect(sessionEntries().filter((e) => e.type === "permission")).toMatchObject([
      {
        decision: "deny",
        source: "headless",
        reason: "dangerous: force-push rewrites remote history",
      },
    ]);
  });

  it("the classifier is down: the call is treated as dangerous, exit 2 — never a silent allow", async () => {
    const cwd = work();
    const fake = new FakeProvider({
      streams: [toolTurn(writeA), textTurn("never")],
      answers: [new ProviderError("network_error", "fetch failed")],
    });

    const result = await run(["write a.md", "--cwd", cwd], fake);

    expect(result.code).toBe(2);
    expect(existsSync(join(cwd, "a.md"))).toBe(false);
  });

  it("--json shows each permission event before the matching tool_result", async () => {
    const cwd = work();
    const fake = new FakeProvider({
      streams: [
        toolTurn({ id: "r", name: "read", arguments: { path: "." } }, writeA),
        textTurn("done"),
      ],
      answers: [safe],
    });

    const result = await run(["go", "--json", "--cwd", cwd], fake);

    expect(result.code).toBe(0);
    const seq = events(result.stdout).flatMap((e) =>
      e.type === "permission"
        ? [`permission:${e.call.id}:${e.source}`]
        : e.type === "tool_result"
          ? [`result:${e.callId}`]
          : [],
    );
    expect(seq).toEqual([
      "permission:r:readOnly",
      "result:r",
      "permission:w:classifier",
      "result:w",
    ]);
  });

  it.each([
    [["--permission-mode", "bypass"], "--permission-mode must be ask or auto: bypass"],
    [["--on-blocked", "ignore"], "--on-blocked must be stop or continue: ignore"],
  ])("exits 1 with usage on %j", async (flags, message) => {
    const result = await run(["hi", ...flags], new FakeProvider());

    expect(result.code).toBe(1);
    expect(result.stderr.startsWith(`${message}\nusage: `)).toBe(true);
  });

  it("parses --permission-mode and --on-blocked", () => {
    expect(
      resolveMode(["x", "--permission-mode", "ask", "--on-blocked", "continue"], true, false),
    ).toMatchObject({ args: { permissionMode: "ask", onBlocked: "continue" } });
  });
});

describe("main (TUI permissions)", () => {
  const STATUS = " · 0 MCP · ";
  async function shown(terminal: FakeTerminal, text: string): Promise<void> {
    for (let i = 0; i < 100 && !terminal.written.join("").includes(text); i++) await settle(20);
    expect(terminal.written.join("")).toContain(text);
  }
  async function tui(fake: FakeProvider, argv: string[] = []) {
    const cwd = join(home, "proj");
    mkdirSync(cwd, { recursive: true });
    const terminal = new FakeTerminal();
    const result = run(["--cwd", cwd, "--trust", ...argv], fake, { stdin: tty, terminal });
    await shown(terminal, STATUS);
    return { cwd, terminal, result };
  }
  const writeCall = (id: string, path: string): ToolCall => ({
    id,
    name: "write",
    arguments: { path, content: id },
  });

  it("defaults to ask: a write shows the prompt with its path relative to cwd; n blocks it", async () => {
    const fake = new FakeProvider({
      streams: [toolTurn(writeCall("w", "docs/a.md")), textTurn("It was blocked.")],
    });
    const t = await tui(fake);
    expect(t.terminal.written.join("")).toContain(" · ask · ");

    t.terminal.type("write it");
    t.terminal.send("\r");
    await shown(t.terminal, "Allow write?");
    t.terminal.send("n");
    await shown(t.terminal, "It was blocked.");
    t.terminal.send("\x03");

    expect((await t.result).code).toBe(0);
    expect(t.terminal.written.join("")).toContain("docs/a.md");
    expect(existsSync(join(t.cwd, "docs", "a.md"))).toBe(false);
    expect(fake.requests[1]?.context.messages.at(-1)).toMatchObject({
      content: "Blocked: denied by the user",
    });
    expect(fake.structuredRequests).toEqual([]);
  });

  it.each([
    ["Shift+Tab", (t: FakeTerminal) => t.send("\x1b[Z")],
    [
      "/mode auto",
      (t: FakeTerminal) => {
        t.type("/mode auto");
        t.send("\r");
      },
    ],
  ])(
    "%s switches to auto: the status bar says so, the next write is classified, settings.json is untouched",
    async (_name, toAuto) => {
      const settingsFile = join(home, "settings.json");
      const settings = `${JSON.stringify({ permissions: { mode: "ask" } }, null, 2)}\n`;
      writeFileSync(settingsFile, settings);
      const fake = new FakeProvider({
        streams: [toolTurn(writeCall("w", "a.md")), textTurn("written")],
        answers: [safe],
      });
      const t = await tui(fake);

      toAuto(t.terminal);
      await shown(t.terminal, " · auto · ");
      t.terminal.type("write it");
      t.terminal.send("\r");
      await shown(t.terminal, "written");
      t.terminal.send("\x03");
      await t.result;

      expect(t.terminal.written.join("")).not.toContain("Allow write?");
      expect(fake.structuredRequests).toHaveLength(1);
      expect(readFileSync(join(t.cwd, "a.md"), "utf8")).toBe("w");
      expect(readFileSync(settingsFile, "utf8")).toBe(settings);
    },
  );

  it("starts in settings.permissions.mode, and --permission-mode overrides it", async () => {
    writeFileSync(join(home, "settings.json"), JSON.stringify({ permissions: { mode: "auto" } }));
    const fromSettings = await tui(new FakeProvider());
    expect(fromSettings.terminal.written.join("")).toContain(" · auto · ");
    fromSettings.terminal.send("\x03");
    await fromSettings.result;

    const fromFlag = await tui(new FakeProvider(), ["--permission-mode", "ask"]);
    expect(fromFlag.terminal.written.join("")).toContain(" · ask · ");
    fromFlag.terminal.send("\x03");
    await fromFlag.result;
  });
});

describe("main (mcp)", () => {
  const SERVER = fileURLToPath(new URL("./fixtures/fake-mcp-server.mjs", import.meta.url));
  const marker = () => join(home, "mcp-pids");
  const pids = () =>
    existsSync(marker()) ? readFileSync(marker(), "utf8").trim().split("\n").map(Number) : [];
  const fakeServer = () => ({
    command: process.execPath,
    args: [SERVER],
    env: { FAKE_MCP_MARKER: marker() },
  });
  const writeMcp = (file: string, value: unknown) => {
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, typeof value === "string" ? value : JSON.stringify(value));
  };
  // A scratch project dir next to the fake home.
  const project = () => {
    const cwd = join(home, "work");
    mkdirSync(cwd, { recursive: true });
    return cwd;
  };

  it("starts the configured stdio server at startup and ends it when main returns", async () => {
    writeMcp(join(home, "mcp.json"), { mcpServers: { fake: fakeServer() } });
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    const result = await run(["hi", "--cwd", project()], fake);

    expect(result.code).toBe(0);
    expect(pids()).toHaveLength(1);
    expect(await gone(pids()[0] as number)).toBe(true);
    // The child's stderr ("fake-mcp-server: ready") is log-only.
    expect(result.stdout).toBe("ok\n");
    expect(result.stderr).toBe("");
  });

  it("--no-mcp: no mcp.json read (an invalid one passes), no child spawned, no startMcp call", async () => {
    writeMcp(join(home, "mcp.json"), "{ not json");
    const cwd = project();
    writeMcp(join(cwd, ".tree-agent", "mcp.json"), { mcpServers: { fake: fakeServer() } });
    const spy = vi.fn(startMcp);
    const fake = new FakeProvider({ streams: [textTurn("ok"), textTurn("ok")] });

    const off = await run(["hi", "--cwd", cwd, "--trust", "--no-mcp"], fake, { startMcp: spy });

    expect(off).toMatchObject({ code: 0, stderr: "" });
    expect(spy).not.toHaveBeenCalled();
    expect(pids()).toEqual([]);

    const on = await run(["hi", "--cwd", cwd, "--trust"], fake, { startMcp: spy });

    expect(spy).toHaveBeenCalledTimes(1);
    expect(on.code).toBe(1);
    expect(on.stderr).toContain(`${join(home, "mcp.json")}: invalid JSON`);
    expect(pids()).toEqual([]);
  });

  it("passes settings.json mcp.initializeTimeoutMs to startMcp (default 30 000)", async () => {
    const spy = vi.fn(startMcp);
    const fake = new FakeProvider({ streams: [textTurn("ok"), textTurn("ok")] });

    await run(["hi", "--cwd", project()], fake, { startMcp: spy });
    writeFileSync(
      join(home, "settings.json"),
      JSON.stringify({ mcp: { initializeTimeoutMs: 90_000 } }),
    );
    await run(["hi", "--cwd", project()], fake, { startMcp: spy });

    expect(spy.mock.calls.map(([o]) => o.initializeTimeoutMs)).toEqual([30_000, 90_000]);
  });

  it("a url without a scheme: one line naming the server and the key, exit 1", async () => {
    const cwd = project();
    writeMcp(join(cwd, ".tree-agent", "mcp.json"), {
      mcpServers: { "tree-memory": { url: "tree-memory.fastmcp.app/mcp" } },
    });
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    const result = await run(["hi", "--cwd", cwd, "--trust"], fake);

    expect(result.code).toBe(1);
    expect(result.stderr).toBe(
      `${join(cwd, ".tree-agent", "mcp.json")}: server "tree-memory": url must be a valid URL\n`,
    );
    expect(fake.requests).toHaveLength(0);
  });

  it("an untrusted cwd reads the user mcp.json only", async () => {
    const cwd = project();
    writeMcp(join(cwd, ".tree-agent", "mcp.json"), {
      mcpServers: { "tree-memory": { url: "tree-memory.fastmcp.app/mcp" } },
    });
    writeMcp(join(cwd, ".mcp.json"), "{ Claude Code's file, never parsed");
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    const result = await run(["hi", "--cwd", cwd], fake);

    expect(result).toMatchObject({ code: 0, stderr: "" });
  });

  const exposed = () => ({
    ...fakeServer(),
    toolExposure: { echo: "direct", read_note: "deferred" },
  });
  const names = (fake: FakeProvider, i = 0) => fake.requests[i]?.tools.map((t) => t.name);
  const BUILTINS = ["read", "bash", "edit", "write"];

  it("registers the server's tools: built-ins + the direct one + tool_search, and the mcp_servers line", async () => {
    const cwd = project();
    writeMcp(join(cwd, ".tree-agent", "mcp.json"), { mcpServers: { "fake-server": exposed() } });
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    const result = await run(["hi", "--cwd", cwd, "--trust"], fake);

    expect(result).toMatchObject({ code: 0, stderr: "" });
    expect(names(fake)).toEqual([...BUILTINS, "mcp__fake_server__echo", "tool_search"]);
    expect(fake.requests[0]?.context.system).toContain(
      "<mcp_servers>\n- fake-server: 2 tools; direct 1, deferred 1, codemode 0\n</mcp_servers>",
    );
  });

  it("the default exposure is codemode: no MCP tool and no tool_search reach the model, codemode does", async () => {
    writeMcp(join(home, "mcp.json"), { mcpServers: { fake: fakeServer() } });
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    await run(["hi", "--cwd", project()], fake);

    expect(names(fake)).toEqual([...BUILTINS, "codemode"]);
    const codemode = fake.requests[0]?.tools.find((t) => t.name === "codemode");
    expect(codemode?.description).toContain(
      "mcp__fake__echo(params: {}): Echo the arguments back as JSON text.",
    );
    expect(fake.requests[0]?.context.system).toContain(
      "- fake: 2 tools; direct 0, deferred 0, codemode 2",
    );
  });

  it("headless: a dangerous tools.x() inside a codemode script exits 2 even when the script catches", async () => {
    writeMcp(join(home, "mcp.json"), { mcpServers: { fake: fakeServer() } });
    const script = `
      try { await tools.mcp__fake__echo({ wipe: true }); }
      catch (e) { console.log("caught"); }
      return "never";
    `;
    const fake = new FakeProvider({
      streams: [
        toolTurn({ id: "cm", name: "codemode", arguments: { script, description: "echo" } }),
        textTurn("never"),
      ],
      answers: [
        { dangerous: false, risk: "low", reason: "runs a script" },
        { dangerous: true, risk: "high", reason: "wipes the server" },
      ],
    });

    const result = await run(["wipe it", "--cwd", project(), "--json"], fake);

    expect(result.code).toBe(2);
    const events = result.stdout
      .trimEnd()
      .split("\n")
      .map((l) => JSON.parse(l) as AgentEvent);
    expect(events.at(-1)).toEqual({ type: "agent_end", reason: "blocked" });
    expect(events).toContainEqual({
      type: "tool_result",
      callId: "cm",
      content: "Error: the script was stopped: a tool call was blocked",
      isError: true,
    });
    expect(fake.requests).toHaveLength(1);
  });

  it("--no-mcp: exactly the 4 built-ins, no tool_search, no mcp_servers section", async () => {
    const cwd = project();
    writeMcp(join(cwd, ".tree-agent", "mcp.json"), { mcpServers: { "fake-server": exposed() } });
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    await run(["hi", "--cwd", cwd, "--trust", "--no-mcp"], fake);

    expect(names(fake)).toEqual(BUILTINS);
    expect(fake.requests[0]?.context.system).not.toContain("<mcp_servers>");
    expect(fake.requests[0]?.context.system).not.toContain("tool_search");
  });

  it("--json: tool_search activates read_note, the next turn calls it, and --resume keeps it", async () => {
    const cwd = project();
    writeMcp(join(cwd, ".tree-agent", "mcp.json"), { mcpServers: { "fake-server": exposed() } });
    const NOTE = "mcp__fake_server__read_note";
    const fake = new FakeProvider({
      streams: [
        toolTurn({ id: "s1", name: "tool_search", arguments: { query: "read a note" } }),
        toolTurn({ id: "n1", name: NOTE, arguments: { id: "7" } }),
        textTurn("note 7 says hi"),
      ],
    });

    const result = await run(["read note 7", "--cwd", cwd, "--trust", "--json"], fake);

    expect(result.code).toBe(0);
    const events = result.stdout
      .trimEnd()
      .split("\n")
      .map((l) => JSON.parse(l) as AgentEvent);
    expect(events).toContainEqual({ type: "tool_activation", names: [NOTE] });
    expect(events).toContainEqual({
      type: "tool_result",
      callId: "n1",
      content: '{"id":"7","body":"note 7"}',
      isError: false,
    });
    expect(names(fake, 0)).not.toContain(NOTE);
    expect(names(fake, 1)).toContain(NOTE);
    // read_note is readOnlyHint: true, so the gate never asked
    expect(events).toContainEqual(
      expect.objectContaining({ type: "permission", source: "readOnly", decision: "allow" }),
    );

    const again = new FakeProvider({
      streams: [toolTurn({ id: "n2", name: NOTE, arguments: { id: "8" } }), textTurn("note 8")],
    });
    const resumed = await run(["and 8?", "--cwd", cwd, "--trust", "--continue", "--json"], again);

    expect(resumed.code).toBe(0);
    expect(names(again, 0)).toContain(NOTE);
    expect(resumed.stdout).not.toContain('"tool_activation"');
    expect(resumed.stdout).toContain('{\\"id\\":\\"8\\",\\"body\\":\\"note 8\\"}');
  });

  it("a trusted cwd starts the project server, and .mcp.json is never parsed", async () => {
    const cwd = project();
    writeMcp(join(cwd, ".tree-agent", "mcp.json"), { mcpServers: { fake: fakeServer() } });
    writeMcp(join(cwd, ".mcp.json"), "{ Claude Code's file, never parsed");
    const fake = new FakeProvider({ streams: [textTurn("ok")] });

    const result = await run(["hi", "--cwd", cwd, "--trust"], fake);

    expect(result).toMatchObject({ code: 0, stderr: "" });
    expect(pids()).toHaveLength(1);
  });
});
