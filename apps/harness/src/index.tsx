#!/usr/bin/env bun
// Tree — harness entry (Milestone 4 of docs/harness-plan.md).
// Modes:
//   tree --print "<prompt>"           CLI streaming, fresh session
//   tree "<prompt>"                   same as --print
//   tree                              Ink REPL, fresh session
//   tree --resume                     list recent sessions and exit
//   tree --resume <id> [prompt…]      load <id>; replay into --print or Ink
//   tree --continue [prompt…]         load most recent for this cwd; ditto

import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { render } from "ink";
import { loop } from "./agent/loop";
import { App } from "./app";
import { createClient } from "./client";
import type { Message } from "./messages";
import { newSessionId, sessionPath, sessionsDirFor } from "./session/paths";
import { findMostRecent, listSessions, loadSession } from "./session/resume";
import { SessionStore } from "./session/store";
import { builtInTools } from "./tools/registry";
import type { ToolContext } from "./tools/types";

function findRepoRoot(start: string = process.cwd()): string {
  let dir = start;
  for (let i = 0; i < 8; i++) {
    if (existsSync(join(dir, ".git")) || existsSync(join(dir, "docker-compose.yml"))) {
      return dir;
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return start;
}

const SYSTEM_PROMPT = [
  "You are Tree, a rooted personal assistant running inside a coding-agent harness.",
  "You have tools available: bash, read, write, edit, glob, grep, todo.",
  "Use them proactively — prefer tools over speculation. Answer concisely.",
].join(" ");

interface Args {
  prompt?: string;
  resume?: boolean;
  resumeId?: string;
  continueRecent?: boolean;
}

function parseArgs(argv: string[]): Args {
  const out: Args = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--print") {
      out.prompt = argv[++i];
    } else if (a === "--resume") {
      out.resume = true;
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("-")) {
        out.resumeId = next;
        i += 1;
      }
    } else if (a === "--continue") {
      out.continueRecent = true;
    } else if (a !== undefined && !a.startsWith("-")) {
      out.prompt = a;
    }
  }
  return out;
}

function truncate(s: string, n = 200): string {
  if (s.length <= n) return s;
  return `${s.slice(0, n)}… (${s.length - n} more chars)`;
}

function printSessionList(cwd: string): void {
  const sessions = listSessions(cwd, 10);
  if (sessions.length === 0) {
    console.log(`(no sessions for cwd=${cwd})`);
    console.log(`sessions live under ${sessionsDirFor(cwd)}`);
    return;
  }
  console.log(`recent sessions for cwd=${cwd}:\n`);
  for (const s of sessions) {
    const id = s.id.slice(0, 8);
    const when = s.startedAt.slice(0, 19).replace("T", " ");
    console.log(`  ${id}  ${when}  ${s.firstPrompt}`);
  }
  console.log(`\nrun: ARGS="--resume <id>" PROMPT="…" make harness-run`);
}

async function runPrintMode(
  client: ReturnType<typeof createClient>,
  prompt: string,
  toolContext: ToolContext,
  initialHistory: Message[],
  session: SessionStore,
): Promise<void> {
  const userMessage: Message = { role: "user", content: prompt };
  const messages: Message[] = [...initialHistory, userMessage];
  session.appendMessage(userMessage);

  let lastEvent: "text" | "tool" | "none" = "none";
  for await (const ev of loop({
    client,
    messages,
    systemPrompt: SYSTEM_PROMPT,
    tools: builtInTools,
    toolContext,
    // CLI mode auto-allows destructive tools — no operator to prompt. Still logged.
    permission: async (toolName, input) => {
      session.appendEvent("permission", {
        tool: toolName,
        input,
        decision: "allow",
        source: "cli-auto",
      });
      return "allow";
    },
    onMessage: (m) => session.appendMessage(m),
  })) {
    if (ev.type === "assistant_text") {
      process.stdout.write(ev.text);
      lastEvent = "text";
    } else if (ev.type === "tool_use") {
      if (lastEvent === "text") process.stdout.write("\n");
      process.stdout.write(`\x1b[2m[${ev.name}] ${truncate(JSON.stringify(ev.input))}\x1b[0m\n`);
      lastEvent = "tool";
    } else if (ev.type === "tool_result") {
      const tag = ev.isError ? "error" : "ok";
      process.stdout.write(
        `\x1b[2m  → ${tag}: ${truncate(ev.content.replace(/\n/g, " ⏎ "))}\x1b[0m\n`,
      );
      lastEvent = "tool";
    } else if (ev.type === "done") {
      if (lastEvent === "text") process.stdout.write("\n");
      if (ev.reason === "max_iterations") {
        process.stdout.write("\x1b[33m(hit max iterations)\x1b[0m\n");
      }
      return;
    } else if (ev.type === "error") {
      console.error(`\ntree: ${ev.message}`);
      process.exit(1);
    }
  }
}

interface ResolvedSession {
  store: SessionStore;
  history: Message[];
}

function resolveSession(args: Args, cwd: string): ResolvedSession | null {
  // --resume without id → list + exit. Caller handles.
  if (args.resume && !args.resumeId) return null;

  if (args.resumeId) {
    const summary = listSessions(cwd, 100).find((s) => s.id.startsWith(args.resumeId ?? ""));
    if (!summary) {
      console.error(`tree: no session found matching id=${args.resumeId} in cwd=${cwd}`);
      process.exit(1);
    }
    const { messages, sessionId } = loadSession(summary.path);
    const store = new SessionStore(summary.path, sessionId ?? summary.id, cwd, false);
    return { store, history: messages };
  }

  if (args.continueRecent) {
    const summary = findMostRecent(cwd);
    if (!summary) {
      console.error(`tree: --continue found no prior sessions for cwd=${cwd}`);
      process.exit(1);
    }
    const { messages, sessionId } = loadSession(summary.path);
    const store = new SessionStore(summary.path, sessionId ?? summary.id, cwd, false);
    return { store, history: messages };
  }

  // Fresh session.
  const id = newSessionId();
  const store = new SessionStore(sessionPath(cwd, id), id, cwd, true);
  return { store, history: [] };
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  const cwd = findRepoRoot();

  // --resume without id → list and exit (works in both CLI and TUI invocations).
  if (args.resume && !args.resumeId) {
    printSessionList(cwd);
    return;
  }

  const client = (() => {
    try {
      return createClient();
    } catch (err) {
      console.error(`tree: ${err instanceof Error ? err.message : String(err)}`);
      process.exit(1);
    }
  })();

  const resolved = resolveSession(args, cwd);
  if (!resolved) {
    printSessionList(cwd);
    return;
  }
  const { store, history } = resolved;

  const abort = new AbortController();
  process.on("SIGINT", () => abort.abort());
  const toolContext: ToolContext = { cwd, signal: abort.signal };

  if (args.prompt !== undefined) {
    await runPrintMode(client, args.prompt, toolContext, history, store);
    return;
  }

  if (!process.stdout.isTTY) {
    console.error(
      'tree: interactive mode requires a TTY. Use --print "<prompt>" or pipe a prompt as arg.',
    );
    process.exit(1);
  }

  const instance = render(
    <App
      client={client}
      systemPrompt={SYSTEM_PROMPT}
      tools={builtInTools}
      toolContext={toolContext}
      session={store}
      initialHistory={history}
    />,
  );
  await instance.waitUntilExit();
}

main();
