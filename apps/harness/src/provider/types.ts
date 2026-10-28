// The provider contract (spec §5). OpenRouterProvider speaks it over
// HTTP; FakeProvider replays scripts of it in tests.

import type { z } from "zod";
import type { AssistantMessage, Context, Usage } from "../types.ts";

export type JsonSchema = Record<string, unknown>;

// From GET /api/v1/models (chat) and GET /api/v1/models
// ?output_modalities=decisions (decision models): context_length,
// top_provider.max_completion_tokens, supported_parameters, pricing
// (USD per token).
export interface ModelInfo {
  id: string;
  contextLength: number;
  maxOutputTokens: number;
  supported: Set<string>;
  pricing: { prompt: number; completion: number };
  isDecisionModel: boolean; // listed by the decisions list only
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
  // Called with each attempt's usage (a parse retry is a second).
  onUsage?: (usage: Usage) => void;
}

export interface TextRequest {
  model: string;
  system?: string;
  input: string;
  maxOutputTokens?: number;
}

// The Decisions API (POST /api/alpha/decisions): typed questions
// about a `state`, answered with probabilities and no text.
export type DecisionQuestion =
  | {
      type: "noul"; // p(yes)
      instructions: string;
      criteria?: { true: string; false: string };
    }
  | {
      type: "choice";
      instructions: string;
      criteria: Record<string, string | null>; // option → guidance
    }
  | {
      type: "score";
      instructions: string;
      criteria: string[]; // ordered level descriptions
    };

export interface DecisionsRequest {
  model: string;
  state: string | Record<string, unknown> | unknown[];
  questions: Record<string, DecisionQuestion>;
}

export type DecisionAnswer =
  | { type: "noul"; noul: number }
  | {
      type: "choice";
      choice: string;
      confidence: number;
      probabilities: Record<string, number>;
    }
  | {
      type: "score";
      score: number;
      confidence: number;
      probabilities: Record<string, number>;
      legend: Record<string, string>;
    };

export interface DecisionsResponse {
  id: string;
  model: string; // the dated snapshot that answered
  provider: string;
  answers: Record<string, DecisionAnswer>; // one per question
  // cost: USD, from the body's usage.cost
  usage: { inputTokens: number; outputTokens: number; cost: number };
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
  // A decision model's answers; rejects with ProviderError.
  decisions(
    req: DecisionsRequest,
    signal?: AbortSignal,
  ): Promise<DecisionsResponse>;
}

// complete/completeText/decisions/modelInfo reject with this; `code`
// matches the `error` StreamEvent's code.
export class ProviderError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "ProviderError";
    this.code = code;
  }
}
