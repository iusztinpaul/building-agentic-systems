// mcp.json (spec §10.1): `{mcpServers: {<name>: server}}`, a server
// being stdio `{command, args, env}` or streamable HTTP `{url,
// headers}` (an optional `type` must match), each with a
// `toolExposure` glob map. Sources, merged by
// server name, later wins whole: ~/.tree-agent/mcp.json, then
// <cwd>/.tree-agent/mcp.json for a trusted cwd only. The repo-root
// .mcp.json (Claude Code's) is never read (ADR-016 §3).

import { readFile } from "node:fs/promises";
import { z } from "zod";
import { projectPaths, userPaths } from "../config/paths.ts";

const Stdio = z.object({
  type: z.literal("stdio").optional(),
  command: z.string(),
  args: z.string().array().default([]),
  env: z.record(z.string()).default({}),
});
const Http = z.object({
  type: z.literal("http").optional(),
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
    // glob → exposure, first match wins (mcp/register.ts)
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
  if (!isObject(raw)) {
    throw new McpConfigError(`${source}: must be an object`);
  }
  // Iterated by hand: z.record() drops a server named `__proto__`.
  const servers = raw.mcpServers;
  if (!isObject(servers)) {
    const what =
      servers === undefined ? "is required" : "must be an object";
    throw new McpConfigError(`${source}: mcpServers ${what}`);
  }
  const mcpServers: Record<string, McpServer> = Object.create(null);
  for (const [name, entry] of Object.entries(servers)) {
    const problem = (text: string) =>
      new McpConfigError(`${source}: server "${name}": ${text}`);
    if (!name) throw problem("name must not be empty");
    if (isObject(entry) && "command" in entry && "url" in entry) {
      throw problem("set command (stdio) or url (HTTP), not both");
    }
    const result = McpServer.safeParse(entry);
    if (!result.success) throw problem(describe(result.error, entry));
    // z.string().url() takes any scheme (ftp:, file:); the transport
    // speaks HTTP only.
    if ("url" in result.data && !/^https?:\/\//i.test(result.data.url)) {
      throw problem("url must start with http:// or https://");
    }
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
  } else if (issue.code === "invalid_literal") {
    text = "must be http or stdio"; // `type`, the only literal
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
