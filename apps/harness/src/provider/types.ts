// The provider contract (spec §5). OpenRouterProvider speaks it over
// HTTP; FakeProvider replays scripts of it in tests.

import type { z } from "zod";
import type { AssistantMessage, Context, Usage } from "../types.ts";

export type JsonSchema = Record<string, unknown>;

// From GET /api/v1/models: context_length,
// top_provider.max_completion_tokens, supported_parameters, pricing
// (USD per token).
export interface ModelInfo {
  id: string;
  contextLength: number;
  maxOutputTokens: number;
  supported: Set<string>;
  pricing: { prompt: number; completion: number };
}

export interface ToolSchema {
  name: string;
  description: string;
  parameters: JsonSchema;
}

export interface ChatRequest {
  model: string;
  context: Context;
  tools: ToolSchema[];
  maxOutputTokens?: number;
}

// A stream ends with exactly one `done` or one `error`.
export type StreamEvent =
  | { type: "text_delta"; text: string }
  | { type: "tool_call_start"; index: number; id: string; name: string }
  | { type: "tool_call_delta"; index: number; argumentsDelta: string }
  | { type: "done"; message: AssistantMessage }
  | { type: "error"; code: string; message: string };

export interface StructuredRequest<T> {
  model: string;
  system: string;
  input: string;
  schema: z.ZodType<T>;
  name: string;
  maxOutputTokens?: number;
}

export interface TextRequest {
  model: string;
  system?: string;
  input: string;
  maxOutputTokens?: number;
}

export interface Provider {
  // Cached for the provider's lifetime (one provider per process).
  modelInfo(id: string): Promise<ModelInfo>;
  stream(req: ChatRequest, signal: AbortSignal): AsyncIterable<StreamEvent>;
  complete<T>(req: StructuredRequest<T>, signal?: AbortSignal): Promise<T>;
  // The sandbox llm() without a schema.
  completeText(
    req: TextRequest,
    signal?: AbortSignal,
  ): Promise<{ text: string; usage: Usage }>;
}

// complete/completeText/modelInfo reject with this; `code` matches
// the `error` StreamEvent's code.
export class ProviderError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "ProviderError";
    this.code = code;
  }
}
