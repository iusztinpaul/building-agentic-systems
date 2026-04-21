import type { z } from "zod";

export interface ToolContext {
  cwd: string;
  signal: AbortSignal;
  // future: permissions (M4), mcpClients (M5), depth (M6)
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
  isReadOnly: boolean;
  isDestructive: boolean;
  call: (input: TInput, ctx: ToolContext) => Promise<ToolResult>;
}
