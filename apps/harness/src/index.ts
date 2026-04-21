#!/usr/bin/env bun
// Tree — walking-skeleton CLI harness (Milestone 1 of docs/harness-plan.md).
// argv → Gemini streaming → stdout. Ink, tools, MCP, permissions, sessions, sub-agents,
// and hooks arrive at later milestones. The agent-loop abstraction lands at M2.

import type { GoogleGenAI } from "@google/genai";
import { createClient, streamText } from "./client";
import type { Message } from "./messages";

const SYSTEM_PROMPT =
  "You are Tree, a rooted personal assistant. Answer concisely. Cite sources when you have them.";

function parseArgs(argv: string[]): { prompt: string } {
  // tree --print "<prompt>"      explicit one-shot
  // tree "<prompt>"              positional one-shot
  let prompt: string | undefined;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--print") {
      prompt = argv[++i];
    } else if (a !== undefined && !a.startsWith("-")) {
      prompt = a;
    }
  }
  if (!prompt) {
    console.error('usage: tree --print "<prompt>"');
    console.error('   or: tree "<prompt>"');
    process.exit(2);
  }
  return { prompt };
}

async function main(): Promise<void> {
  const { prompt } = parseArgs(process.argv.slice(2));

  let client: GoogleGenAI;
  try {
    client = createClient();
  } catch (err) {
    console.error(`tree: ${err instanceof Error ? err.message : String(err)}`);
    process.exit(1);
  }

  const messages: Message[] = [{ role: "user", content: prompt }];

  try {
    for await (const event of streamText(client, messages, SYSTEM_PROMPT)) {
      if (event.type === "text_delta") {
        process.stdout.write(event.text);
      } else if (event.type === "done") {
        process.stdout.write("\n");
      }
    }
  } catch (err) {
    console.error(`\ntree: ${err instanceof Error ? err.message : String(err)}`);
    process.exit(1);
  }
}

main();
