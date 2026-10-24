import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { type CliDeps, main } from "../src/cli.ts";
import { FakeProvider, fakeDone, fakeModel } from "../src/provider/fake.ts";
import { OpenRouterProvider } from "../src/provider/openrouter.ts";
import type { Provider } from "../src/provider/types.ts";
import { fakeFetch, fixture, jsonResponse, sseResponse } from "./helpers/http.ts";

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

async function run(argv: string[], provider: Provider | (() => Provider), key = "test-key") {
  const out: string[] = [];
  const err: string[] = [];
  const created: string[] = [];
  const deps: CliDeps = {
    env: { TREE_AGENT_HOME: home, ...(key && { OPENROUTER_API_KEY: key }) },
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
    expect(req?.tools).toEqual([]);
    expect(req?.context.messages).toEqual([{ role: "user", content: "say hi" }]);
    expect(req?.context.system).not.toBe("");
  });

  it("uses --model for the request", async () => {
    const fake = new FakeProvider({ streams: [[fakeDone("ok")]] });

    await run(["hi", "--model", "openai/gpt-5"], fake);

    expect(fake.requests[0]?.model).toBe("openai/gpt-5");
  });

  it("creates no provider and sends nothing when the key is missing", async () => {
    const result = await run(["hi"], new FakeProvider(), "");

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
