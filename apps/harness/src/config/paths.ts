// Where tree-agent keeps user-level state: ~/.tree-agent, or
// $TREE_AGENT_HOME when set (tests and sandboxes point it elsewhere).

import { homedir } from "node:os";
import { join, resolve } from "node:path";

export interface UserPaths {
  home: string;
  settings: string;
  mcp: string;
  mcpAuth: string; // one OAuth token file per HTTP MCP server
  trusted: string;
  agentsMd: string;
  systemMd: string; // replaces the prompt preamble when present
  skills: string;
  sessions: string;
  tmp: string; // full tool output that was truncated for the model
}

export function userPaths(env: NodeJS.ProcessEnv = process.env): UserPaths {
  const home = env.TREE_AGENT_HOME
    ? resolve(env.TREE_AGENT_HOME)
    : join(homedir(), ".tree-agent");
  return {
    home,
    settings: join(home, "settings.json"),
    mcp: join(home, "mcp.json"),
    mcpAuth: join(home, "mcp-auth"),
    trusted: join(home, "trusted.json"),
    agentsMd: join(home, "AGENTS.md"),
    systemMd: join(home, "SYSTEM.md"),
    skills: join(home, "skills"),
    sessions: join(home, "sessions"),
    tmp: join(home, "tmp"),
  };
}

// Project-level config under <cwd>/.tree-agent, read only when the
// cwd is trusted (spec §16). Never <cwd>/.mcp.json: that is Claude
// Code's file, with another schema and owner (ADR-016 §3).
export function projectPaths(cwd: string): {
  settings: string;
  mcp: string;
} {
  const dir = join(cwd, ".tree-agent");
  return {
    settings: join(dir, "settings.json"),
    mcp: join(dir, "mcp.json"),
  };
}
