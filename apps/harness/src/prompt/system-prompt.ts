// The system prompt (spec §7): named sections in a fixed order, so
// the cached prefix stays stable across requests. Each section is
// wrapped in <name>…</name>; the preamble is untagged.

import { readFile } from "node:fs/promises";
import { userPaths } from "../config/paths.ts";
import type { McpServerInfo } from "../mcp/register.ts";
import type { Skill } from "../skills/skills.ts";
import type { ToolDefinition } from "../tools/registry.ts";
import { TOOL_SEARCH } from "../tools/tool-search.ts";

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

export const MORE_TOOLS =
  "More tools are available; call `tool_search` to find them.";

// `- name: promptSnippet` per declared (direct ∪ activated) tool,
// plus MORE_TOOLS when tool_search is one of them.
export function toolsSection(tools: ToolDefinition[]): string {
  const lines = tools.map((t) => `- ${t.name}: ${t.promptSnippet}`);
  if (tools.some((t) => t.name === TOOL_SEARCH)) lines.push(MORE_TOOLS);
  return lines.join("\n");
}

// `- <name>: <N> tools; direct a, deferred b, codemode c` per
// configured server (none: no section).
export function mcpServersSection(
  servers: readonly McpServerInfo[],
): string {
  return servers.map((s) => `- ${serverLine(s)}`).join("\n");
}

export function serverLine(s: McpServerInfo): string {
  return (
    `${s.name}: ${s.tools} tools; direct ${s.direct}, ` +
    `deferred ${s.deferred}, codemode ${s.codemode}`
  );
}

export function rulesSection(): string {
  return RULES.map((rule) => `- ${rule}`).join("\n");
}

// The token estimate used for prompt budgets: 4 chars per token.
export const CHARS_PER_TOKEN = 4;

export const SKILLS_INTRO =
  "The following skills provide specialized instructions. Use `read` " +
  "on a skill's file\nwhen the task matches its description. Resolve " +
  "relative paths against the skill dir.";

const DESCRIPTION_CAP = 250; // chars, the last one `…`

// Spec §9, progressive disclosure: name + description + location per
// model-invocable skill, in the given order, until the section would
// pass `budgetTokens` (wrapper included). The rest is `dropped`; with
// nothing listed the section is empty (and omitted).
export function skillsSection(
  skills: readonly Skill[],
  budgetTokens: number,
): { text: string; dropped: string[] } {
  const head = `${SKILLS_INTRO}\n<available_skills>`;
  const tail = "</available_skills>";
  const budget = budgetTokens * CHARS_PER_TOKEN;
  let size = head.length + 1 + tail.length;
  const lines: string[] = [];
  const dropped: string[] = [];
  for (const s of skills) {
    if (s.disableModelInvocation) continue;
    const line =
      `  <skill><name>${xml(s.name)}</name>` +
      `<description>${xml(capDescription(s.description))}</description>` +
      `<location>${xml(s.location)}</location></skill>`;
    if (dropped.length > 0 || size + line.length + 1 > budget) {
      dropped.push(s.name);
      continue;
    }
    lines.push(line);
    size += line.length + 1;
  }
  if (lines.length === 0) return { text: "", dropped };
  return { text: [head, ...lines, tail].join("\n"), dropped };
}

// One line, at most DESCRIPTION_CAP code points.
export function capDescription(description: string): string {
  const chars = [...description.replace(/\s+/g, " ").trim()];
  if (chars.length <= DESCRIPTION_CAP) return chars.join("");
  return `${chars.slice(0, DESCRIPTION_CAP - 1).join("")}…`;
}

function xml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
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
