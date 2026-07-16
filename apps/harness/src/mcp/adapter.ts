import { z } from "zod";
import type { Tool } from "../tools/types";
import { type McpServer, type McpToolDescriptor, callMcpTool } from "./client";

// Wrap an MCP tool as a native harness Tool.
//
// Naming: mcp__<server>__<tool>. The double-underscore is the Claude Code convention
// and keeps the server identity visible to the model in tool-use banners.
//
// Schema: the zod schema is a pass-through (any record); actual validation happens
// on the server side. `parametersJsonSchema` carries the real schema for Gemini so
// the model sees the right parameters.
//
// Classification: heuristic by name — anything that looks like a writer is marked
// destructive and therefore routed through the permission gate.

export const MCP_TOOL_PREFIX = "mcp__";

const DESTRUCTIVE_PATTERN =
  /^(ingest|write|create|delete|update|upsert|remove|add|set|append|push|modify|edit)/i;

export function classifyMcpTool(toolName: string): { isReadOnly: boolean; isDestructive: boolean } {
  const destructive = DESTRUCTIVE_PATTERN.test(toolName);
  return { isReadOnly: !destructive, isDestructive: destructive };
}

export function mcpToolToHarnessTool(
  server: McpServer,
  desc: McpToolDescriptor,
): Tool<Record<string, unknown>> {
  const { isReadOnly, isDestructive } = classifyMcpTool(desc.name);
  return {
    name: `${MCP_TOOL_PREFIX}${server.name}__${desc.name}`,
    description: desc.description || `MCP tool ${desc.name} from ${server.name}`,
    schema: z.record(z.string(), z.unknown()),
    parametersJsonSchema: desc.inputSchema,
    isReadOnly,
    isDestructive,
    async call(input) {
      return await callMcpTool(server, desc.name, input);
    },
  };
}

export function mcpServersToTools(servers: Iterable<McpServer>): Tool<Record<string, unknown>>[] {
  const out: Tool<Record<string, unknown>>[] = [];
  for (const server of servers) {
    for (const tool of server.tools) {
      out.push(mcpToolToHarnessTool(server, tool));
    }
  }
  return out;
}
