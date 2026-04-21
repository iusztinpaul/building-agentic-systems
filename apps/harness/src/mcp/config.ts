import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

// Matches the structure of the root .mcp.json agents / harnesses already share.
// Only the fields the harness needs to spawn a stdio MCP server are modeled.

export interface McpServerConfig {
  command: string;
  args?: string[];
  env?: Record<string, string>;
}

export interface McpConfig {
  mcpServers?: Record<string, McpServerConfig>;
}

// Walk up from `start` to find an .mcp.json. Mirrors findRepoRoot so the harness
// picks up the repo-root config regardless of where it was invoked from.
export function findMcpConfig(start: string): string | null {
  let dir = start;
  for (let i = 0; i < 8; i++) {
    const candidate = join(dir, ".mcp.json");
    if (existsSync(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

export function loadMcpConfig(path: string): McpConfig {
  const text = readFileSync(path, "utf-8");
  return JSON.parse(text) as McpConfig;
}

// Spawn parameters for a given server config.
//   cwd     = directory containing .mcp.json (so paths like `apps/memory` resolve)
//   env     = inherit process.env + server-specific overrides
//   command = uv / python / node / bun / etc. — taken verbatim from the config
export interface McpSpawn {
  command: string;
  args: string[];
  env: Record<string, string>;
  cwd: string;
}

export function resolveSpawn(cfg: McpServerConfig, configDir: string): McpSpawn {
  const processEnv: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (typeof v === "string") processEnv[k] = v;
  }
  return {
    command: cfg.command,
    args: cfg.args ?? [],
    env: { ...processEnv, ...(cfg.env ?? {}) },
    cwd: configDir,
  };
}
