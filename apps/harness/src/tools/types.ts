import type { z } from "zod";
import type { McpServer } from "../mcp/client";

// Request/result shapes for spawning a sub-agent. Defined here so ToolContext can
// reference them without dragging in agent/subagents' runtime dependencies.
export interface SpawnSubagentRequest {
  type: "general" | "explore" | "plan";
  description: string;
  prompt: string;
  parentDepth: number;
  parentSignal: AbortSignal;
  mcpServers?: Map<string, McpServer>;
}

export interface SpawnSubagentResult {
  summary: string;
  tool_uses: number;
  duration_ms: number;
  subagent_id: string;
  stopped_reason:
    | "end_turn"
    | "max_iterations"
    | "timeout"
    | "max_tool_calls"
    | "depth_exceeded"
    | "error";
}

export type SpawnSubagent = (req: SpawnSubagentRequest) => Promise<SpawnSubagentResult>;

export interface ToolContext {
  cwd: string;
  signal: AbortSignal;
  // MCP server handles — keyed by the server name from .mcp.json. Threaded through
  // so sub-agents (M6) reuse the parent's live subprocesses instead of respawning.
  mcpServers?: Map<string, McpServer>;
  // 0 at top level; +1 per sub-agent. Capped at 2 by the subagent registry.
  depth?: number;
  // Set by the top-level caller. When absent, the task tool returns an error
  // instead of silently doing nothing.
  spawnSubagent?: SpawnSubagent;
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
