import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { isTrusted } from "../../src/config/trust.ts";
import { useTmpDir } from "../helpers/tmp.ts";

const root = useTmpDir("trust");

function setup(trusted?: unknown) {
  const home = join(root(), "home");
  const cwd = join(root(), "repo");
  mkdirSync(home, { recursive: true });
  mkdirSync(cwd, { recursive: true });
  if (trusted !== undefined) {
    const text = typeof trusted === "string" ? trusted : JSON.stringify(trusted);
    writeFileSync(join(home, "trusted.json"), text);
  }
  return { cwd, env: { TREE_AGENT_HOME: home } as NodeJS.ProcessEnv };
}

describe("isTrusted", () => {
  it("is false without trusted.json, flag or env", async () => {
    const { cwd, env } = setup();

    await expect(isTrusted(cwd, { env })).resolves.toBe(false);
  });

  it("is true for a cwd listed in trusted.json", async () => {
    const { cwd, env } = setup([join(root(), "repo")]);

    await expect(isTrusted(cwd, { env })).resolves.toBe(true);
    await expect(isTrusted(`${cwd}/./`, { env })).resolves.toBe(true);
  });

  it("does not extend a listed directory's trust to its children or parent", async () => {
    const { cwd, env } = setup([join(root(), "repo")]);

    await expect(isTrusted(join(cwd, "sub"), { env })).resolves.toBe(false);
    await expect(isTrusted(root(), { env })).resolves.toBe(false);
  });

  it("is true with --trust or TREE_AGENT_TRUST=1, and not for other env values", async () => {
    const { cwd, env } = setup();

    await expect(isTrusted(cwd, { env, flag: true })).resolves.toBe(true);
    await expect(isTrusted(cwd, { env: { ...env, TREE_AGENT_TRUST: "1" } })).resolves.toBe(true);
    await expect(isTrusted(cwd, { env: { ...env, TREE_AGENT_TRUST: "yes" } })).resolves.toBe(false);
  });

  it("trusts nothing when trusted.json is not a JSON array of paths", async () => {
    for (const bad of ["{not json", { dirs: [] }, [1, 2]]) {
      const { cwd, env } = setup(bad);
      await expect(isTrusted(cwd, { env })).resolves.toBe(false);
    }
  });
});
