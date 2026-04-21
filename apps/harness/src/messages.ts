// Type vocabulary reused by every milestone. Deliberately provider-neutral:
// `client.ts` translates to/from the Gemini SDK shapes at the boundary so a later
// swap to Anthropic or OpenAI is localized there.

export type Role = "user" | "assistant" | "system";

export interface TextBlock {
  type: "text";
  text: string;
}

export interface ToolUseBlock {
  type: "tool_use";
  id: string;
  name: string;
  input: Record<string, unknown>;
}

export interface ToolResultBlock {
  type: "tool_result";
  tool_use_id: string;
  // Gemini's functionResponse part is keyed by tool name (not id), so we carry it here.
  tool_name: string;
  content: string;
  is_error?: boolean;
}

export type ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock;

export interface Message {
  role: Role;
  content: string | ContentBlock[];
}

// Events yielded by `streamText`. `loop()` (agent/loop.ts) consumes these and re-emits
// its own higher-level events (assistant_text, tool_use, tool_result, done, error).
export type StreamEvent =
  | { type: "text_delta"; text: string }
  | { type: "function_call"; name: string; args: Record<string, unknown> }
  | { type: "done" };
