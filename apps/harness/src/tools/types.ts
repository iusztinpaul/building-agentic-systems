import type { z } from "zod";
import type { McpServer } from "../mcp/client";

export interface ToolContext {
  cwd: string;
  signal: AbortSignal;
  // MCP server handles — keyed by the server name from .mcp.json. Threaded through
  // so sub-agents (M6) can reuse the parent's live subprocesses instead of
  // respawning them. Absent when no .mcp.json is loaded.
  mcpServers?: Map<string, McpServer>;
  // future: permissions state (M4), depth (M6)
}

export interface ToolResult {
  content: string;
  isError?: boolean;
}

// The schema's input type is deliberately `unknown` (not `TInput`) so tools can use
// `.default(...)` and `.optional()` on zod fields: those make the parsed output type
// differ from the input the model supplies. `TInput` is the post-parse type.
export interface Tool<TInput = unknown> {
  name: string;
  description: string;
  // biome-ignore lint/suspicious/noExplicitAny: ZodTypeDef's Def parameter is internal
  schema: z.ZodType<TInput, any, unknown>;
  // Override for tools whose input shape can't be expressed in zod — notably MCP tools
  // that arrive with a server-defined JSON Schema. When set, this is handed to the
  // Gemini `parametersJsonSchema`; the zod `schema` is still used for runtime
  // validation (typically a pass-through record for MCP tools).
  parametersJsonSchema?: Record<string, unknown>;
  isReadOnly: boolean;
  isDestructive: boolean;
  call: (input: TInput, ctx: ToolContext) => Promise<ToolResult>;
}
