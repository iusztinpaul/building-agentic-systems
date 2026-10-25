// The slash commands of this milestone. Each one is a call on
// AgentSession's public API plus the lines the TUI shows; /compact
// (212) joins later. /skill:<name> [args] answers with
// the prompt to run (the SKILL.md body + args) instead of lines.

import type { SlashCommand } from "@earendil-works/pi-tui";
import { serverLine } from "../prompt/system-prompt.ts";
import type { AgentSession } from "../session.ts";

export const COMMANDS: SlashCommand[] = [
  { name: "help", description: "commands, keys, context files" },
  { name: "model", description: "switch the model", argumentHint: "<id>" },
  {
    name: "mode",
    description: "permission mode for this session",
    argumentHint: "ask|auto",
  },
  { name: "tools", description: "the tools the model can call" },
  { name: "mcp", description: "MCP servers, their tools and state" },
  { name: "reload", description: "re-read AGENTS.md / CLAUDE.md" },
  { name: "clear", description: "start a new session" },
  { name: "exit", description: "quit" },
];

// COMMANDS plus one /skill:<name> per skill, for autocompletion.
export function commandsFor(session: AgentSession): SlashCommand[] {
  const skills = session.skills.map((s) => ({
    name: `skill:${s.name}`,
    description: clip(s.description.replace(/\s+/g, " "), 60),
    argumentHint: "[args]",
  }));
  return [...COMMANDS, ...skills];
}

const KEYS = [
  "Enter send (during a run: steer) · Alt+Enter follow-up",
  "Shift+Enter new line · Up/Down history · Esc abort the run",
  "Tab (empty input) expand/collapse tool results · Ctrl+C abort/quit",
  "Shift+Tab toggle the permission mode (ask ↔ auto)",
];

export interface CommandResult {
  lines: string[];
  error?: boolean;
  exit?: boolean;
  cleared?: boolean; // a new session: the transcript starts over
  prompt?: string; // /skill: — the user message to send
}

export async function runCommand(
  session: AgentSession,
  name: string,
  arg: string,
): Promise<CommandResult> {
  try {
    return await run(session, name, arg);
  } catch (err) {
    return { lines: [(err as Error).message], error: true };
  }
}

async function run(
  session: AgentSession,
  name: string,
  arg: string,
): Promise<CommandResult> {
  if (name.startsWith("skill:")) {
    return { lines: [], prompt: session.skillMessage(name.slice(6), arg) };
  }
  switch (name) {
    case "help":
      return { lines: help(session) };
    case "model":
      if (!arg) return { lines: [`model: ${session.model}`] };
      await session.setModel(arg);
      return { lines: [`model: ${session.model}`] };
    case "mode":
      if (arg === "ask" || arg === "auto") session.setPermissionMode(arg);
      else if (arg) {
        return {
          lines: [`usage: /mode ask|auto (got ${arg})`],
          error: true,
        };
      }
      return { lines: [`permission mode: ${session.permissionMode}`] };
    case "tools": {
      const names = session.toolNames();
      return { lines: [`tools (${names.length}): ${names.join(", ")}`] };
    }
    case "mcp":
      return { lines: mcpLines(session) };
    case "reload": {
      await session.reload();
      const n = session.contextFiles.length;
      const notices = session.skillNotices;
      return { lines: [`reloaded ${n} context file(s)`, ...notices] };
    }
    case "clear":
      session.clear();
      return { lines: ["new session"], cleared: true };
    case "exit":
      return { lines: [], exit: true };
    default:
      return {
        lines: [`unknown command /${name} (see /help)`],
        error: true,
      };
  }
}

// One line per server of the mcp.json files (never .mcp.json).
function mcpLines(session: AgentSession): string[] {
  const servers = session.mcpServers;
  return [
    `MCP servers (${servers.length})${servers.length ? ":" : ""}`,
    ...servers.map(
      (s) =>
        `  ${serverLine(s)} · ` +
        (s.client.connected ? "connected" : "not connected"),
    ),
  ];
}

function help(session: AgentSession): string[] {
  const commands = COMMANDS.map(
    (c) =>
      `/${c.name}${c.argumentHint ? ` ${c.argumentHint}` : ""}` +
      ` — ${c.description}`,
  );
  const files = session.contextFiles.map((f) => `  ${f.path}`);
  const skills = session.skills.map((s) => s.name);
  const trust = session.trusted
    ? "trusted directory: project files loaded"
    : "untrusted directory: user-level config only";
  return [
    ...commands,
    `/skill:<name> [args] — run a skill (${skills.length}): ` +
      (skills.join(", ") || "none"),
    ...KEYS,
    `session: ${session.id ?? "(not saved yet)"} · ${trust}`,
    files.length ? "context files:" : "context files: none",
    ...files,
  ];
}

function clip(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
