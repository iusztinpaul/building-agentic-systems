#!/usr/bin/env bun
// Tree — harness entry (Milestone 5 of docs/harness-plan.md).
// Modes:
//   tree --print "<prompt>"           CLI streaming, fresh session
//   tree "<prompt>"                   same as --print
//   tree                              Ink REPL, fresh session
//   tree --resume                     list recent sessions and exit
//   tree --resume <id> [prompt…]      load <id>; replay into --print or Ink
//   tree --continue [prompt…]         load most recent for this cwd; ditto
//
// On startup the harness reads the root .mcp.json and spawns each server as a
// stdio subprocess. Discovered MCP tools register alongside the built-in ones
// under mcp__<server>__<tool> names.

import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { render } from "ink";
import { loop } from "./agent/loop";
import { App } from "./app";
import { createClient } from "./client";
import { mcpServersToTools } from "./mcp/adapter";
import { type McpServer, connectMcpServer } from "./mcp/client";
import { findMcpConfig, loadMcpConfig, resolveSpawn } from "./mcp/config";
import type { Message } from "./messages";
import { newSessionId, sessionPath, sessionsDirFor } from "./session/paths";
import { findMostRecent, listSessions, loadSession } from "./session/resume";
import { SessionStore } from "./session/store";
import { type AnyTool, builtInTools } from "./tools/registry";
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
  "You have native tools (bash, read, write, edit, glob, grep, todo) and MCP tools",
  "prefixed mcp__<server>__<name> — use them proactively, prefer tools over speculation.",
  "Answer concisely.",
].join(" ");

interface Args {
  prompt?: string;
  resume?: boolean;
  resumeId?: string;
  continueRecent?: boolean;
  noMcp?: boolean;
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
    } else if (a === "--no-mcp") {
      out.noMcp = true;
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

async function bootMcpServers(cwd: string, skip: boolean): Promise<Map<string, McpServer>> {
  const servers = new Map<string, McpServer>();
  if (skip) return servers;

  const configPath = findMcpConfig(cwd);
  if (!configPath) return servers;

  const config = loadMcpConfig(configPath);
  const configDir = dirname(configPath);
  const entries = Object.entries(config.mcpServers ?? {});
  if (entries.length === 0) return servers;

  // Each server independent — one failure doesn't block the rest.
  await Promise.all(
    entries.map(async ([name, cfg]) => {
      try {
        const spawn = resolveSpawn(cfg, configDir);
        const server = await connectMcpServer(name, spawn);
        servers.set(name, server);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error(`tree: mcp server "${name}" failed to start: ${msg}`);
      }
    }),
  );

  return servers;
}

async function runPrintMode(
  client: ReturnType<typeof createClient>,
  prompt: string,
  toolContext: ToolContext,
  tools: AnyTool[],
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
    tools,
    toolContext,
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

  const id = newSessionId();
  const store = new SessionStore(sessionPath(cwd, id), id, cwd, true);
  return { store, history: [] };
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  const cwd = findRepoRoot();

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

  const mcpServers = await bootMcpServers(cwd, args.noMcp ?? false);
  const mcpTools = mcpServersToTools(mcpServers.values());
  const allTools: AnyTool[] = [...builtInTools, ...mcpTools];
  if (mcpServers.size > 0) {
    const names = Array.from(mcpServers.values()).flatMap((s) =>
      s.tools.map((t) => `mcp__${s.name}__${t.name}`),
    );
    console.error(
      `\x1b[2mtree: ${mcpServers.size} mcp server(s) up; ${mcpTools.length} tools: ${names.join(", ")}\x1b[0m`,
    );
  }

  const abort = new AbortController();
  process.on("SIGINT", () => abort.abort());
  const toolContext: ToolContext = { cwd, signal: abort.signal, mcpServers };

  try {
    if (args.prompt !== undefined) {
      await runPrintMode(client, args.prompt, toolContext, allTools, history, store);
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
        tools={allTools}
        toolContext={toolContext}
        session={store}
        initialHistory={history}
      />,
    );
    await instance.waitUntilExit();
  } finally {
    await Promise.all(Array.from(mcpServers.values()).map((s) => s.close()));
  }
}

main();
