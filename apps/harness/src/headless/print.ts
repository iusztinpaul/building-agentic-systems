// Headless mode (spec §15): one AgentSession, no prompter, and an
// event sink. The text sink prints each final assistant answer to
// stdout and tool activity to stderr; the --json sink prints every
// AgentEvent as one JSON line on stdout.

import type { AgentSession } from "../session.ts";
import { parseSkillCommand } from "../skills/skills.ts";
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
// gate, 1 anything else, including a signal-aborted run or an
// unusable `/skill:` line: unknown name, empty message or
// --no-skills (nothing is sent then).
export async function runHeadless(o: HeadlessOptions): Promise<number> {
  for (const notice of o.session.skillNotices) {
    o.stderr.write(`${notice}\n`);
  }
  // Every `/skill:` line (the prompt and each follow-up) is resolved
  // before anything runs, so one bad name sends nothing.
  let texts: string[];
  try {
    texts = [o.prompt, ...o.followUps].map((text) => {
      const skill = parseSkillCommand(text);
      return skill ? o.session.skillMessage(skill.name, skill.args) : text;
    });
  } catch (err) {
    o.stderr.write(`${(err as Error).message}\n`);
    return 1;
  }
  const [prompt = "", ...followUps] = texts;
  const sink = o.json ? jsonSink(o.stdout) : textSink(o.stdout, o.stderr);
  const off = o.session.on(sink);
  // `once`: a second Ctrl+C falls through to Node's default exit.
  const abort = () => o.session.abort();
  o.signals?.once("SIGINT", abort);
  o.signals?.once("SIGTERM", abort);
  for (const text of followUps) o.session.followUp(text);
  try {
    const reason = await o.session.prompt(prompt);
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
    } else if (event.type === "tool_activation") {
      stderr.write(`activated: ${event.names.join(", ")}\n`);
    } else if (event.type === "compaction") {
      const { before, after } = event;
      stderr.write(`compacted ${before} → ${after} tokens\n`);
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
