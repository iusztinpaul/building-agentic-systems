// MCP tools as registry entries (spec §10.3): each listed tool is a
// ToolDefinition named mcp__<server>__<tool>, exposed by the first
// matching `toolExposure` glob (default `codemode`); `hidden` tools
// are never registered. The server validates the arguments: the zod
// side only checks "an object", the provider gets the server's own
// inputSchema.

import { z } from "zod";
import { log as defaultLog, type Logger } from "../log.ts";
import type { JsonSchema } from "../provider/types.ts";
import type {
  Exposure,
  ToolDefinition,
  ToolRegistry,
  ToolResult,
} from "../tools/registry.ts";
import { spillOver } from "../tools/truncate.ts";
import type { McpCallResult, McpClient, McpTool } from "./client.ts";

export const TOOL_NAME = /^[a-z][a-z0-9_]*$/; // spec §8
const MAX_TOOL_NAME = 64; // the providers' function-name cap
export const MAX_DESCRIPTION = 2_048; // chars
const MAX_RESULT_BYTES = 20 * 1024; // more is spilled to tmp

// What registration reads from a client; tests pass stubs.
export type RegistrableClient = Pick<
  McpClient,
  "name" | "server" | "tools" | "connected" | "callTool"
>;

// One configured server, for the `mcp_servers` prompt line and /mcp.
export interface McpServerInfo {
  name: string; // the config key, as written
  client: Pick<McpClient, "connected">; // live connection state
  tools: number; // registered: hidden ones are not counted
  direct: number;
  deferred: number;
  codemode: number;
}

const Params = z.object({}).passthrough();

export interface RegisterOptions {
  tmpDir: string; // where a result over 20 KB is saved in full
  log?: Logger;
}

// Registers every listed tool of every client, in order. A server
// whose key has no usable segment, or shares its segment with an
// earlier server, registers nothing (one warning); a tool whose name
// is empty, invalid, too long or taken is skipped with a warning. It
// never stops the others.
export function registerMcpTools(
  registry: ToolRegistry,
  clients: readonly RegistrableClient[],
  options: RegisterOptions,
): McpServerInfo[] {
  const log = options.log ?? defaultLog;
  const owners = new Map<string, string>(); // segment → first key
  return clients.map((client) => {
    const info: McpServerInfo = {
      name: client.name,
      client,
      tools: 0,
      direct: 0,
      deferred: 0,
      codemode: 0,
    };
    const segment = serverSegment(client.name);
    const owner = owners.get(segment);
    if (!TOOL_NAME.test(segment)) {
      log.warn(
        `mcp "${client.name}": tools not registered: server key ` +
          `"${client.name}" has no usable name segment`,
      );
      return info;
    }
    if (owner !== undefined) {
      log.warn(
        `mcp "${client.name}": tools not registered: servers ` +
          `"${owner}" and "${client.name}" both map to "${segment}"`,
      );
      return info;
    }
    owners.set(segment, client.name);
    const globs = client.server.toolExposure;
    for (const tool of client.tools) {
      const exposure = exposureFor(tool.name, globs);
      if (exposure === "hidden") continue;
      try {
        registry.register(mcpTool(client, tool, exposure, options.tmpDir));
      } catch (err) {
        log.warn(
          `mcp "${client.name}": tool "${tool.name}" not registered: ` +
            (err as Error).message,
        );
        continue;
      }
      info.tools++;
      info[exposure]++;
    }
    return info;
  });
}

// `tree-memory-local` → `tree_memory_local`: every name stays a valid
// `tools.<name>` property in codemode scripts. A run of other
// characters (`_` included) becomes one `_`, edges trimmed, so the
// segment never holds the `__` separator (`a--b` → `a_b`).
export function serverSegment(server: string): string {
  return server
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "");
}

export function mcpToolName(server: string, tool: string): string {
  return `mcp__${serverSegment(server)}__${tool}`;
}

// The first glob (declaration order) matching the bare tool name;
// `*` matches any run of characters.
export function exposureFor(
  tool: string,
  globs: Record<string, Exposure>,
): Exposure {
  for (const [glob, exposure] of Object.entries(globs)) {
    const body = glob.split("*").map(escapeRegExp).join(".*");
    if (new RegExp(`^${body}$`).test(tool)) return exposure;
  }
  return "codemode";
}

function mcpTool(
  client: RegistrableClient,
  tool: McpTool,
  exposure: Exposure,
  tmpDir: string,
): ToolDefinition<typeof Params> {
  if (!tool.name) throw new Error("tool name is empty");
  const name = mcpToolName(client.name, tool.name);
  if (!TOOL_NAME.test(name)) {
    throw new Error(`"${name}" does not match ${TOOL_NAME.source}`);
  }
  if (name.length > MAX_TOOL_NAME) {
    throw new Error(
      `"${name}" is ${name.length} chars, over the ${MAX_TOOL_NAME}-char cap`,
    );
  }
  const description = [...(tool.description ?? "")]
    .slice(0, MAX_DESCRIPTION)
    .join("");
  const firstLine = description
    .split("\n")
    .map((line) => line.trim())
    .find(Boolean);
  return {
    name,
    description,
    promptSnippet: firstLine ?? `${tool.name} (MCP server ${client.name})`,
    parameters: Params,
    inputSchema: providerSchema(tool.inputSchema),
    exposure,
    isReadOnly: tool.annotations?.readOnlyHint === true,
    async execute(params) {
      return toToolResult(await client.callTool(tool.name, params), tmpDir);
    },
  };
}

// The server's schema as sent to the provider: always an object, no
// `$schema` (as the built-ins' zod-derived ones).
function providerSchema(schema: Record<string, unknown>): JsonSchema {
  const copy: JsonSchema = { ...schema, type: "object" };
  delete copy.$schema;
  return copy;
}

// Text content joined by newlines (other kinds as a placeholder);
// over 20 KB the model gets head + tail and the tmp path. A codemode
// script gets `structuredContent` (FastMCP's `{result: "<json>"}`
// wrapper of a `str` return unwrapped), else the text parsed as
// JSON, else the text itself.
export function toToolResult(
  result: McpCallResult,
  tmpDir: string,
): ToolResult {
  const texts: string[] = [];
  const parts = result.content.map((item) => {
    if (item.type !== "text" || typeof item.text !== "string") {
      return `[${item.type} content]`;
    }
    texts.push(item.text);
    return item.text;
  });
  const text = texts.join("\n");
  let content = parts.join("\n");
  if (!content && result.structuredContent) {
    content = JSON.stringify(result.structuredContent);
  }
  return {
    content: spillOver(content || "(no output)", MAX_RESULT_BYTES, tmpDir),
    isError: result.isError,
    structured:
      result.structuredContent === undefined
        ? parseJson(text)
        : unwrapResult(result.structuredContent),
  };
}

// Exactly `{result: <string>}` whose string is JSON → the parsed
// value; anything else as is.
function unwrapResult(structured: Record<string, unknown>): unknown {
  const keys = Object.keys(structured);
  const { result } = structured;
  if (
    keys.length !== 1 ||
    keys[0] !== "result" ||
    typeof result !== "string"
  ) {
    return structured;
  }
  try {
    return JSON.parse(result);
  } catch {
    return structured;
  }
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
