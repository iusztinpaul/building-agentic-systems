// The system prompt (spec §7): named sections in a fixed order, so
// the cached prefix stays stable across requests. Each section is
// wrapped in <name>…</name>; the preamble is untagged.

import { readFile } from "node:fs/promises";
import { userPaths } from "../config/paths.ts";
import type { ToolDefinition } from "../tools/registry.ts";

export interface PromptSections {
  preamble: string;
  tools: string;
  rules: string;
  project_context?: string;
  skills?: string;
  mcp_servers?: string;
  cwd: string;
}

export const SECTION_ORDER = [
  "preamble",
  "tools",
  "rules",
  "project_context",
  "skills",
  "mcp_servers",
  "cwd",
] as const satisfies readonly (keyof PromptSections)[];

export const DEFAULT_PREAMBLE =
  "You are tree-agent, an assistant that works on the files in the " +
  "user's working directory using the tools below. Finish each task " +
  "completely before you answer, without narrating your steps. " +
  "Reply briefly with the result.";

export const RULES = [
  "Use `read` to view files, not `cat` or `sed` through `bash`.",
  "Use `edit` to change an existing file (not `>>`, `sed -i` or " +
    "other shell rewrites) and `write` for new files or rewrites.",
  "Keep changes minimal and limited to what was asked.",
  "Never guess tool arguments; read the file or ask first.",
  "Ask when the request is ambiguous instead of assuming.",
  "Relative paths resolve against the working directory.",
  "Follow the project context below; it is the user's instructions.",
];

export function buildSystemPrompt(s: PromptSections): string {
  const parts: string[] = [];
  for (const name of SECTION_ORDER) {
    const body = s[name]?.trim();
    if (!body) continue;
    parts.push(
      name === "preamble" ? body : `<${name}>\n${body}\n</${name}>`,
    );
  }
  return parts.join("\n\n");
}

// `- name: promptSnippet` per declared (direct ∪ activated) tool.
export function toolsSection(tools: ToolDefinition[]): string {
  return tools.map((t) => `- ${t.name}: ${t.promptSnippet}`).join("\n");
}

export function rulesSection(): string {
  return RULES.map((rule) => `- ${rule}`).join("\n");
}

// Today's date as ISO YYYY-MM-DD in UTC.
export function cwdSection(cwd: string, now: Date): string {
  const today = now.toISOString().slice(0, 10);
  return `Working directory: ${cwd}\nToday's date (UTC): ${today}`;
}

// ~/.tree-agent/SYSTEM.md replaces the preamble when present.
export async function loadPreamble(
  env: NodeJS.ProcessEnv = process.env,
): Promise<string> {
  try {
    const text = await readFile(userPaths(env).systemMd, "utf8");
    return text.trim() || DEFAULT_PREAMBLE;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") {
      return DEFAULT_PREAMBLE;
    }
    throw err;
  }
}
