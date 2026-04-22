import { describe, expect, test } from "bun:test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { findMcpConfig, loadMcpConfig, resolveSpawn } from "../../../src/mcp/config";
import { mkTmpDir } from "../../helpers/tmp-fs";

describe("findMcpConfig", () => {
  test("walks up and returns the first .mcp.json it hits", () => {
    const root = mkTmpDir();
    writeFileSync(join(root, ".mcp.json"), "{}");
    const deep = join(root, "a", "b", "c");
    mkdirSync(deep, { recursive: true });

    expect(findMcpConfig(deep)).toBe(join(root, ".mcp.json"));
  });

  test("returns null when none exists within the climb budget", () => {
    const dir = mkTmpDir();
    expect(findMcpConfig(dir)).toBeNull();
  });

  test("finds a config in the starting dir itself", () => {
    const dir = mkTmpDir();
    writeFileSync(join(dir, ".mcp.json"), "{}");
    expect(findMcpConfig(dir)).toBe(join(dir, ".mcp.json"));
  });
});

describe("loadMcpConfig", () => {
  test("parses a valid mcpServers block", () => {
    const dir = mkTmpDir();
    const path = join(dir, ".mcp.json");
    writeFileSync(
      path,
      JSON.stringify({
        mcpServers: {
          "tree-memory": {
            command: "uv",
            args: ["run", "python", "scripts/serve_mcp.py"],
            env: { ENV_FILE_PATH: "../../.env" },
          },
        },
      }),
    );
    const cfg = loadMcpConfig(path);
    expect(cfg.mcpServers?.["tree-memory"]?.command).toBe("uv");
    expect(cfg.mcpServers?.["tree-memory"]?.args).toEqual([
      "run",
      "python",
      "scripts/serve_mcp.py",
    ]);
    expect(cfg.mcpServers?.["tree-memory"]?.env).toEqual({
      ENV_FILE_PATH: "../../.env",
    });
  });

  test("handles an empty file by throwing (caller decides how to recover)", () => {
    const dir = mkTmpDir();
    const path = join(dir, ".mcp.json");
    writeFileSync(path, "not-json");
    expect(() => loadMcpConfig(path)).toThrow();
  });
});

describe("resolveSpawn", () => {
  test("merges process.env with the server's env overrides (server wins)", () => {
    const previous = process.env.TREE_TEST_VAR;
    process.env.TREE_TEST_VAR = "from-process";
    try {
      const spawn = resolveSpawn(
        {
          command: "uv",
          args: [],
          env: { TREE_TEST_VAR: "from-server", EXTRA: "yes" },
        },
        "/some/dir",
      );
      expect(spawn.env.TREE_TEST_VAR).toBe("from-server");
      expect(spawn.env.EXTRA).toBe("yes");
    } finally {
      if (previous === undefined) process.env.TREE_TEST_VAR = "";
      else process.env.TREE_TEST_VAR = previous;
    }
  });

  test("uses the config dir as spawn cwd", () => {
    const spawn = resolveSpawn({ command: "uv", args: [] }, "/repo/root");
    expect(spawn.cwd).toBe("/repo/root");
  });

  test("passes args verbatim, defaulting to []", () => {
    const withArgs = resolveSpawn({ command: "bun", args: ["run", "server.ts"] }, "/d");
    expect(withArgs.args).toEqual(["run", "server.ts"]);

    const noArgs = resolveSpawn({ command: "bun" }, "/d");
    expect(noArgs.args).toEqual([]);
  });
});
