// mcp.json (spec §10.1): `{mcpServers: {<name>: server}}`, a server
// being stdio `{command, args, env}` or streamable HTTP `{url,
// headers}`, each with a `toolExposure` glob map. Sources, merged by
// server name, later wins whole: ~/.tree-agent/mcp.json, then
// <cwd>/.tree-agent/mcp.json for a trusted cwd only. The repo-root
// .mcp.json (Claude Code's) is never read (ADR-016 §3).

import { readFile } from "node:fs/promises";
import { z } from "zod";
import { projectPaths, userPaths } from "../config/paths.ts";

const Stdio = z.object({
  command: z.string(),
  args: z.string().array().default([]),
  env: z.record(z.string()).default({}),
});
const Http = z.object({
  url: z.string().url(),
  headers: z.record(z.string()).default({}),
});
export const Exposure = z.enum([
  "direct",
  "deferred",
  "codemode",
  "hidden",
]);

export const McpServer = z.union([Stdio, Http]).and(
  z.object({
    // glob → exposure, first match wins (applied in task 209)
    toolExposure: z.record(Exposure).default({}),
  }),
);
export const McpConfig = z.object({ mcpServers: z.record(McpServer) });

export type McpServer = z.infer<typeof McpServer>;
export type StdioServer = Extract<McpServer, { command: string }>;
export type HttpServer = Extract<McpServer, { url: string }>;
export type Exposure = z.infer<typeof Exposure>;

export class McpConfigError extends Error {
  override name = "McpConfigError";
}

export interface LoadMcpOptions {
  env?: NodeJS.ProcessEnv;
  projectDir?: string; // a trusted cwd: its .tree-agent/mcp.json too
}

export async function loadMcpConfig(
  options: LoadMcpOptions = {},
): Promise<Record<string, McpServer>> {
  const user = await readConfig(userPaths(options.env).mcp);
  const project = options.projectDir
    ? await readConfig(projectPaths(options.projectDir).mcp)
    : {};
  return { ...user, ...project };
}

// Validates one file's content; errors read
// `<source>: server "<name>": <key> <problem>`.
export function parseMcpConfig(
  raw: unknown,
  source: string,
): z.infer<typeof McpConfig> {
  const top = z.object({ mcpServers: z.record(z.unknown()) });
  const parsed = top.safeParse(raw);
  if (!parsed.success) {
    throw new McpConfigError(`${source}: ${describe(parsed.error)}`);
  }
  const mcpServers: Record<string, McpServer> = {};
  for (const [name, entry] of Object.entries(parsed.data.mcpServers)) {
    const problem = (text: string) =>
      new McpConfigError(`${source}: server "${name}": ${text}`);
    if (isObject(entry) && "command" in entry && "url" in entry) {
      throw problem("set command (stdio) or url (HTTP), not both");
    }
    const result = McpServer.safeParse(entry);
    if (!result.success) throw problem(describe(result.error, entry));
    mcpServers[name] = result.data;
  }
  return { mcpServers };
}

async function readConfig(
  file: string,
): Promise<Record<string, McpServer>> {
  let text: string;
  try {
    text = await readFile(file, "utf8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw err;
  }
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (err) {
    throw new McpConfigError(
      `${file}: invalid JSON: ${(err as Error).message}`,
    );
  }
  return parseMcpConfig(raw, file).mcpServers;
}

// The first problem as `<key> <predicate>`. A union failure is
// reported from the branch the entry meant: `url` → HTTP, `command`
// → stdio.
function describe(error: z.ZodError, entry?: unknown): string {
  const issue = error.issues[0];
  if (!issue) return "invalid";
  if (issue.code !== "invalid_union") return phrase(issue);
  if (!isObject(entry)) return "must be an object";
  const branch = "url" in entry ? 1 : "command" in entry ? 0 : undefined;
  const chosen =
    branch === undefined ? undefined : issue.unionErrors[branch];
  if (!chosen) return "needs command (stdio) or url (HTTP)";
  return describe(chosen, entry);
}

function phrase(issue: z.ZodIssue): string {
  let text = issue.message;
  if (issue.code === "invalid_type") {
    // an enum reports `expected` as "'a' | 'b'"
    const options = issue.expected.replaceAll("'", "").split(" | ");
    text =
      issue.received === "undefined"
        ? "is required"
        : options.length > 1
          ? `must be one of ${options.join(", ")}`
          : `must be ${article(issue.expected)}`;
  } else if (issue.code === "invalid_enum_value") {
    text = `must be one of ${issue.options.join(", ")}`;
  } else if (
    issue.code === "invalid_string" &&
    issue.validation === "url"
  ) {
    text = "must be a valid URL";
  }
  const path = issue.path.join(".");
  return path ? `${path} ${text}` : text;
}

function article(noun: string): string {
  return /^[aeiou]/.test(noun) ? `an ${noun}` : `a ${noun}`;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" && value !== null && !Array.isArray(value)
  );
}
