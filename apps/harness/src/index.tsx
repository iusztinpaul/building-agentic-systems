#!/usr/bin/env bun
// Tree — harness entry (Milestone 3 of docs/harness-plan.md).
// With `--print "<prompt>"` or a positional prompt: CLI streaming to stdout.
// Without a prompt: mount the Ink REPL.

import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { render } from "ink";
import { loop } from "./agent/loop";
import { App } from "./app";
import { createClient } from "./client";
import type { Message } from "./messages";
import { builtInTools } from "./tools/registry";
import type { ToolContext } from "./tools/types";

// When invoked via `make harness-run`, cwd is apps/harness/. That's almost never what
// the user wants — they want repo-scope globs/reads. Walk up to find the monorepo root.
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

function parseArgs(argv: string[]): { prompt?: string } {
  let prompt: string | undefined;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--print") {
      prompt = argv[++i];
    } else if (a !== undefined && !a.startsWith("-")) {
      prompt = a;
    }
  }
  return { prompt };
}

function truncate(s: string, n = 200): string {
  if (s.length <= n) return s;
  return `${s.slice(0, n)}… (${s.length - n} more chars)`;
}

async function runPrintMode(
  client: ReturnType<typeof createClient>,
  prompt: string,
  toolContext: ToolContext,
): Promise<void> {
  const messages: Message[] = [{ role: "user", content: prompt }];

  let lastEvent: "text" | "tool" | "none" = "none";
  for await (const ev of loop({
    client,
    messages,
    systemPrompt: SYSTEM_PROMPT,
    tools: builtInTools,
    toolContext,
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

async function main(): Promise<void> {
  const { prompt } = parseArgs(process.argv.slice(2));

  const client = (() => {
    try {
      return createClient();
    } catch (err) {
      console.error(`tree: ${err instanceof Error ? err.message : String(err)}`);
      process.exit(1);
    }
  })();

  const abort = new AbortController();
  process.on("SIGINT", () => abort.abort());

  const toolContext: ToolContext = { cwd: findRepoRoot(), signal: abort.signal };

  if (prompt !== undefined) {
    await runPrintMode(client, prompt, toolContext);
    return;
  }

  // Interactive mode — mount Ink.
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
    />,
  );
  await instance.waitUntilExit();
}

main();
