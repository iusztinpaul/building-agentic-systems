// The slash commands of this milestone. Each one is a call on
// AgentSession's public API plus the lines the TUI shows; /mode
// (206), /skill: (207), /mcp (209) and /compact (212) join later.

import type { SlashCommand } from "@earendil-works/pi-tui";
import type { AgentSession } from "../session.ts";

export const COMMANDS: SlashCommand[] = [
  { name: "help", description: "commands, keys, context files" },
  { name: "model", description: "switch the model", argumentHint: "<id>" },
  { name: "tools", description: "the tools the model can call" },
  { name: "reload", description: "re-read AGENTS.md / CLAUDE.md" },
  { name: "clear", description: "start a new session" },
  { name: "exit", description: "quit" },
];

const KEYS = [
  "Enter send (during a run: steer) · Alt+Enter follow-up",
  "Shift+Enter new line · Up/Down history · Esc abort the run",
  "Tab (empty input) expand/collapse tool results · Ctrl+C abort/quit",
];

export interface CommandResult {
  lines: string[];
  error?: boolean;
  exit?: boolean;
  cleared?: boolean; // a new session: the transcript starts over
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
  switch (name) {
    case "help":
      return { lines: help(session) };
    case "model":
      if (!arg) return { lines: [`model: ${session.model}`] };
      await session.setModel(arg);
      return { lines: [`model: ${session.model}`] };
    case "tools": {
      const names = session.toolNames();
      return { lines: [`tools (${names.length}): ${names.join(", ")}`] };
    }
    case "reload": {
      await session.reload();
      const n = session.contextFiles.length;
      return { lines: [`reloaded ${n} context file(s)`] };
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

function help(session: AgentSession): string[] {
  const commands = COMMANDS.map(
    (c) =>
      `/${c.name}${c.argumentHint ? ` ${c.argumentHint}` : ""}` +
      ` — ${c.description}`,
  );
  const files = session.contextFiles.map((f) => `  ${f.path}`);
  const trust = session.trusted
    ? "trusted directory: project files loaded"
    : "untrusted directory: user-level config only";
  return [
    ...commands,
    ...KEYS,
    `session: ${session.id ?? "(not saved yet)"} · ${trust}`,
    files.length ? "context files:" : "context files: none",
    ...files,
  ];
}
