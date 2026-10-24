// Where tree-agent keeps user-level state: ~/.tree-agent, or
// $TREE_AGENT_HOME when set (tests and sandboxes point it elsewhere).

import { homedir } from "node:os";
import { join, resolve } from "node:path";

export interface UserPaths {
  home: string;
  settings: string;
  mcp: string;
  trusted: string;
  agentsMd: string;
  skills: string;
  sessions: string;
}

export function userPaths(env: NodeJS.ProcessEnv = process.env): UserPaths {
  const home = env.TREE_AGENT_HOME
    ? resolve(env.TREE_AGENT_HOME)
    : join(homedir(), ".tree-agent");
  return {
    home,
    settings: join(home, "settings.json"),
    mcp: join(home, "mcp.json"),
    trusted: join(home, "trusted.json"),
    agentsMd: join(home, "AGENTS.md"),
    skills: join(home, "skills"),
    sessions: join(home, "sessions"),
  };
}
