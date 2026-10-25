import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  checkModels,
  loadSettings,
  requireApiKey,
  Settings,
  SettingsError,
} from "../../src/config/settings.ts";
import { INITIALIZE_TIMEOUT_MS } from "../../src/mcp/client.ts";
import { FakeProvider, fakeDecisionModel, fakeModel } from "../../src/provider/fake.ts";

const DEFAULTS = {
  model: "anthropic/claude-sonnet-5.5",
  decisionModel: "typesafe/jev-1.14",
  smallModel: "anthropic/claude-haiku-5.5",
  permissions: { mode: "ask", dangerThreshold: 0.3 },
  compaction: { triggerRatio: 0.8, keepRecentTokens: 20_000 },
  codemode: {
    inlineBudgetTokens: 3_000,
    timeoutMs: 60_000,
    memoryMb: 256,
    maxToolCalls: 50,
    outputMaxBytes: 20_000,
    concurrency: 4,
    llm: {
      depth: 1,
      maxCalls: 64,
      maxInputChars: 200_000,
      maxTotalTokens: 400_000,
      maxSubTurns: 8,
    },
  },
  mcp: { initializeTimeoutMs: 60_000 },
  contextFiles: { maxChars: 40_000 },
  skills: { listingBudgetRatio: 0.01 },
};

let home: string;
const env = () => ({ TREE_AGENT_HOME: home });
const writeSettings = (value: unknown) =>
  writeFileSync(
    join(home, "settings.json"),
    typeof value === "string" ? value : JSON.stringify(value),
  );

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "tree-agent-settings-"));
});

afterEach(() => {
  rmSync(home, { recursive: true, force: true });
});

describe("Settings schema", () => {
  it("parses {} to the spec §16 defaults", () => {
    expect(Settings.parse({})).toEqual(DEFAULTS);
  });
});

describe("loadSettings", () => {
  it("returns the defaults when ~/.tree-agent/settings.json does not exist", async () => {
    await expect(loadSettings({ env: env() })).resolves.toEqual(DEFAULTS);
  });

  it("merges the user file over the defaults key by key", async () => {
    writeSettings({ model: "openai/gpt-5", compaction: { keepRecentTokens: 5_000 } });

    const settings = await loadSettings({ env: env() });

    expect(settings.model).toBe("openai/gpt-5");
    expect(settings.compaction).toEqual({ triggerRatio: 0.8, keepRecentTokens: 5_000 });
  });

  it("lets flags replace the file, and an unset flag keeps the file's value", async () => {
    writeSettings({ model: "openai/gpt-5", decisionModel: "openai/gpt-5-mini" });

    const flagged = await loadSettings({ env: env(), flags: { model: "x/flag" } });
    const unset = await loadSettings({ env: env(), flags: { model: undefined } });

    expect(flagged.model).toBe("x/flag");
    expect(flagged.decisionModel).toBe("openai/gpt-5-mini");
    expect(unset.model).toBe("openai/gpt-5");
  });

  it("layers <projectDir>/.tree-agent/settings.json between the user file and the flags", async () => {
    writeSettings({ model: "user/model", decisionModel: "user/decision" });
    const project = join(home, "repo");
    mkdirSync(join(project, ".tree-agent"), { recursive: true });
    writeFileSync(
      join(project, ".tree-agent", "settings.json"),
      JSON.stringify({ model: "project/model", contextFiles: { maxChars: 9 } }),
    );

    const trusted = await loadSettings({ env: env(), projectDir: project });
    const flagged = await loadSettings({
      env: env(),
      projectDir: project,
      flags: { model: "x/flag" },
    });
    const untrusted = await loadSettings({ env: env() });

    expect(trusted).toMatchObject({
      model: "project/model",
      decisionModel: "user/decision",
      contextFiles: { maxChars: 9 },
    });
    expect(flagged.model).toBe("x/flag");
    expect(untrusted.model).toBe("user/model");
  });

  it("rejects an unknown key with the zod issue list naming the file", async () => {
    writeSettings({ modle: "typo", compaction: { trigger: 0.5 } });

    const error = await loadSettings({ env: env() }).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(SettingsError);
    expect((error as Error).message).toBe(
      [
        `${join(home, "settings.json")}: invalid settings`,
        "  - compaction: Unrecognized key(s) in object: 'trigger'",
        "  - (root): Unrecognized key(s) in object: 'modle'",
      ].join("\n"),
    );
  });

  it("rejects triggerRatio 0.2 with the issue path and bound", async () => {
    writeSettings({ compaction: { triggerRatio: 0.2 } });

    await expect(loadSettings({ env: env() })).rejects.toThrow(
      "  - compaction.triggerRatio: Number must be greater than or equal to 0.3",
    );
  });

  it.each([
    [0.04, "Number must be greater than or equal to 0.05"],
    [0.96, "Number must be less than or equal to 0.95"],
    ["0.3", "Expected number, received string"],
  ])("rejects permissions.dangerThreshold %j with the key path", async (value, issue) => {
    writeSettings({ permissions: { dangerThreshold: value } });

    await expect(loadSettings({ env: env() })).rejects.toThrow(
      `  - permissions.dangerThreshold: ${issue}`,
    );
  });

  it("reads the codemode sandbox limits and rejects a concurrency of 0 (it would hang)", async () => {
    writeSettings({ codemode: { memoryMb: 128, outputMaxBytes: 5_000 } });
    expect((await loadSettings({ env: env() })).codemode).toMatchObject({
      memoryMb: 128,
      outputMaxBytes: 5_000,
      timeoutMs: 60_000,
    });

    writeSettings({ codemode: { concurrency: 0 } });
    await expect(loadSettings({ env: env() })).rejects.toThrow(
      "  - codemode.concurrency: Number must be greater than 0",
    );
  });

  it("reads mcp.initializeTimeoutMs (default = the client's fallback) and rejects 0 and a fraction", async () => {
    expect(Settings.parse({}).mcp.initializeTimeoutMs).toBe(INITIALIZE_TIMEOUT_MS);
    writeSettings({ mcp: { initializeTimeoutMs: 90_000 } });
    expect((await loadSettings({ env: env() })).mcp).toEqual({ initializeTimeoutMs: 90_000 });

    writeSettings({ mcp: { initializeTimeoutMs: 0 } });
    await expect(loadSettings({ env: env() })).rejects.toThrow(
      "  - mcp.initializeTimeoutMs: Number must be greater than 0",
    );
    writeSettings({ mcp: { initializeTimeoutMs: 1.5 } });
    await expect(loadSettings({ env: env() })).rejects.toThrow(
      "  - mcp.initializeTimeoutMs: Expected integer, received float",
    );
    writeSettings({ mcp: { timeoutMs: 1_000 } });
    await expect(loadSettings({ env: env() })).rejects.toThrow(
      "  - mcp: Unrecognized key(s) in object: 'timeoutMs'",
    );
  });

  it("rejects an *Ms value past setTimeout's 2^31 - 1 ceiling with the key path", async () => {
    writeSettings({ mcp: { initializeTimeoutMs: 3_000_000_000 } });

    await expect(loadSettings({ env: env() })).rejects.toThrow(
      "  - mcp.initializeTimeoutMs: Number must be less than or equal to 2147483647",
    );
  });

  it("names the file when settings.json is not valid JSON", async () => {
    writeSettings("{ model: ");

    await expect(loadSettings({ env: env() })).rejects.toThrow(
      new RegExp(`^${join(home, "settings.json")}: invalid JSON: `),
    );
  });
});

describe("requireApiKey", () => {
  it.each([{}, { OPENROUTER_API_KEY: "" }, { OPENROUTER_API_KEY: "  " }])(
    "throws one line naming OPENROUTER_API_KEY for %o",
    (vars) => {
      expect(() => requireApiKey(vars)).toThrow(
        new SettingsError("OPENROUTER_API_KEY is not set (add it to .env)"),
      );
    },
  );

  it("returns the trimmed key", () => {
    expect(requireApiKey({ OPENROUTER_API_KEY: " test-key \n" })).toBe("test-key");
  });
});

describe("checkModels", () => {
  const provider = () =>
    new FakeProvider({
      models: [
        fakeModel("anthropic/claude-haiku-5.5"),
        fakeDecisionModel("typesafe/jev-1.14"),
        fakeModel("weak/model", { supported: new Set(["tools", "max_tokens"]) }),
      ],
    });

  it.each([
    [
      { decisionModel: "anthropic/claude-haiku-5.5" },
      "decisionModel anthropic/claude-haiku-5.5 is not a decision model (output modality " +
        '"decisions"); see https://openrouter.ai/models?output_modalities=decisions',
    ],
    [
      { smallModel: "typesafe/jev-1.14" },
      "smallModel typesafe/jev-1.14 is a decision model; it must be a chat model with " +
        "structured_outputs",
    ],
    [{ smallModel: "weak/model" }, "smallModel weak/model does not support structured_outputs"],
  ])("fails with one exact line for %j", async (layer, message) => {
    const check = checkModels(provider(), Settings.parse(layer));

    await expect(check).rejects.toThrow(new SettingsError(message));
    await expect(check).rejects.toBeInstanceOf(SettingsError);
  });

  it("passes on the defaults: a decision model and a chat model with structured_outputs", async () => {
    await expect(checkModels(provider(), Settings.parse({}))).resolves.toBeUndefined();
  });

  it("surfaces the provider's unknown model error for an unlisted id", async () => {
    const settings = Settings.parse({ decisionModel: "acme/nope" });

    await expect(checkModels(provider(), settings)).rejects.toThrow('unknown model "acme/nope"');
  });
});
