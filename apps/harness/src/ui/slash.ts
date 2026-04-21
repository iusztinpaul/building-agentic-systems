import { listSessions } from "../session/resume";

// Slash-command dispatcher — kept as a plain helper (not a component) because
// each command is a one-shot mutation of App state. The dispatcher returns a
// short message (for the `error`-kind UI block, which renders as an info panel)
// plus optional instructions to mutate history. M7 ships three commands; /resume
// is list-only in v1 — fancy interactive pickers are out of scope.

export interface SlashActions {
  clearHistory: () => void;
  cwd: string;
}

export interface SlashResult {
  info: string;
}

const HELP = [
  "Slash commands:",
  "  /help            — show this list",
  "  /clear           — reset the conversation (session file is kept)",
  "  /resume          — list recent sessions for this cwd",
  "",
  "Exit: Ctrl+C (from the prompt) or Esc",
].join("\n");

export function parseSlashCommand(input: string): { name: string; rest: string } | null {
  if (!input.startsWith("/")) return null;
  const body = input.slice(1).trimStart();
  const space = body.indexOf(" ");
  if (space === -1) return { name: body, rest: "" };
  return { name: body.slice(0, space), rest: body.slice(space + 1) };
}

export function dispatchSlashCommand(name: string, actions: SlashActions): SlashResult {
  if (name === "help" || name === "?" || name === "") {
    return { info: HELP };
  }
  if (name === "clear") {
    actions.clearHistory();
    return { info: "(conversation cleared — session file kept)" };
  }
  if (name === "resume") {
    const sessions = listSessions(actions.cwd, 10);
    if (sessions.length === 0) {
      return { info: "(no sessions recorded for this cwd)" };
    }
    const lines = sessions.map(
      (s) =>
        `  ${s.id.slice(0, 8)}  ${s.startedAt.slice(0, 19).replace("T", " ")}  ${s.firstPrompt}`,
    );
    return {
      info: [
        'Recent sessions (restart with `ARGS="--resume <id>" make harness-run`):',
        "",
        ...lines,
      ].join("\n"),
    };
  }
  return { info: `unknown slash command: /${name}  (try /help)` };
}

// Helper consumed by app.tsx — wraps the dispatcher call into a no-op Message[]
// update if the caller wants to align the UI history panel. Returns the info
// string for inline rendering.
export function applySlashCommand(input: string, actions: SlashActions): SlashResult | null {
  const parsed = parseSlashCommand(input);
  if (!parsed) return null;
  return dispatchSlashCommand(parsed.name, actions);
}
