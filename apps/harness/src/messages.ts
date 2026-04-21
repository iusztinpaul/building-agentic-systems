// Type vocabulary reused by every later milestone. Deliberately provider-neutral:
// `client.ts` translates to/from the Gemini SDK shapes at the boundary so that a later
// swap to Anthropic or OpenAI is localized there.

export type Role = "user" | "assistant" | "system";

export interface TextBlock {
  type: "text";
  text: string;
}

// Stubs for Milestone 2 (tool calling). Kept here so the vocabulary is stable
// and every later module can import from one place.
export interface ToolUseBlock {
  type: "tool_use";
  id: string;
  name: string;
  input: unknown;
}

export interface ToolResultBlock {
  type: "tool_result";
  tool_use_id: string;
  content: string;
  is_error?: boolean;
}

export type ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock;

export interface Message {
  role: Role;
  content: string | ContentBlock[];
}

// Events yielded by `streamText` / (at M2) `loop`. Keeping the set narrow at M1 —
// `tool_use`, `tool_result`, and `error` land at M2.
export type StreamEvent = { type: "text_delta"; text: string } | { type: "done" };
