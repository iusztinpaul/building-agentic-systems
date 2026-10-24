// Headless mode (spec §15): one AgentSession, no prompter, and an
// event sink. The text sink prints each final assistant answer to
// stdout and tool activity to stderr; the --json sink prints every
// AgentEvent as one JSON line on stdout.

import type { AgentSession } from "../session.ts";
import type { AgentEndReason, AgentEvent } from "../types.ts";

export interface Out {
  write(text: string): unknown;
}

// Where SIGINT / SIGTERM arrive: `process` in the CLI, an
// EventEmitter in tests.
export interface Signals {
  once(event: "SIGINT" | "SIGTERM", listener: () => void): unknown;
  off(event: "SIGINT" | "SIGTERM", listener: () => void): unknown;
}

export interface HeadlessOptions {
  session: AgentSession;
  prompt: string;
  followUps: string[]; // e.g. every stdin line after the first
  json: boolean;
  stdout: Out;
  stderr: Out;
  signals?: Signals; // Ctrl+C / kill abort the run (and its bash)
}

// Returns the exit code: 0 completed, 2 blocked by the permission
// gate, 1 anything else, including a signal-aborted run.
export async function runHeadless(o: HeadlessOptions): Promise<number> {
  const sink = o.json ? jsonSink(o.stdout) : textSink(o.stdout, o.stderr);
  const off = o.session.on(sink);
  // `once`: a second Ctrl+C falls through to Node's default exit.
  const abort = () => o.session.abort();
  o.signals?.once("SIGINT", abort);
  o.signals?.once("SIGTERM", abort);
  for (const text of o.followUps) o.session.followUp(text);
  try {
    const reason = await o.session.prompt(o.prompt);
    return EXIT_CODES[reason] ?? 1;
  } finally {
    o.signals?.off("SIGINT", abort);
    o.signals?.off("SIGTERM", abort);
    off();
  }
}

const EXIT_CODES: Partial<Record<AgentEndReason, number>> = {
  completed: 0,
  blocked: 2,
};

export function jsonSink(stdout: Out): (event: AgentEvent) => void {
  return (event) => stdout.write(`${JSON.stringify(event)}\n`);
}

export function textSink(
  stdout: Out,
  stderr: Out,
): (event: AgentEvent) => void {
  return (event) => {
    if (event.type === "turn_end") {
      // A turn without tool calls is an answer; text next to tool
      // calls is narration.
      const { content, toolCalls } = event.message;
      const text = content.trim();
      if (!text) return;
      (toolCalls.length > 0 ? stderr : stdout).write(`${text}\n`);
    } else if (event.type === "tool_call") {
      const args = JSON.stringify(event.call.arguments);
      stderr.write(`> ${event.call.name} ${clip(args, 120)}\n`);
    } else if (event.type === "tool_result" && event.isError) {
      const first = event.content.split("\n")[0] ?? "";
      stderr.write(`! ${clip(first, 200)}\n`);
    } else if (event.type === "error") {
      stderr.write(`${event.message}\n`);
    } else if (event.type === "agent_end") {
      if (event.reason !== "completed" && event.reason !== "error") {
        stderr.write(`run ended: ${event.reason}\n`);
      }
    }
  };
}

function clip(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 3)}...` : text;
}
