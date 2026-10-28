// A scripted Provider for tests: each stream() call replays the next
// StreamEvent[] script, complete()/completeText()/decisions() return
// the next scripted answer, and every request is recorded for
// assertions.

import { Settings } from "../config/settings.ts";
import type { AssistantMessage, Usage } from "../types.ts";
import { parseDecisions } from "./decisions.ts";
import {
  type ChatRequest,
  type DecisionsRequest,
  type DecisionsResponse,
  type ModelInfo,
  type Provider,
  ProviderError,
  type StreamEvent,
  type StructuredRequest,
  type TextRequest,
} from "./types.ts";

export interface FakeScript {
  // One script per stream() call, consumed in order.
  streams?: StreamEvent[][];
  // Answers for complete(), validated with the request's schema; an
  // Error rejects the call. A FakeAnswer function computes the answer
  // per request (it may delay, or never settle).
  answers?: unknown[];
  // Answers for completeText(); an Error rejects the call. A bare
  // string's usage is estimated (chars / 4); `{text, usage}` scripts it.
  texts?: FakeText[];
  // 200 bodies for decisions() in the wire shape (a recorded fixture
  // fits), validated like the real provider's; an Error rejects. A
  // FakeDecision function computes one (it may delay, never settle).
  decisions?: unknown[];
  // When set, modelInfo() serves only these ids; otherwise the
  // default decisionModel resolves to fakeDecisionModel(id) and any
  // other id to fakeModel(id).
  models?: ModelInfo[];
}

export type FakeText = string | Error | { text: string; usage: Usage };

export type FakeAnswer = (
  req: StructuredRequest<unknown>,
  signal?: AbortSignal,
) => unknown;

export type FakeDecision = (
  req: DecisionsRequest,
  signal?: AbortSignal,
) => unknown;

const DECISION_MODEL = Settings.parse({}).decisionModel;

export class FakeProvider implements Provider {
  readonly requests: ChatRequest[] = [];
  readonly structuredRequests: StructuredRequest<unknown>[] = [];
  readonly textRequests: TextRequest[] = [];
  readonly decisionRequests: DecisionsRequest[] = [];
  readonly #streams: StreamEvent[][];
  readonly #answers: unknown[];
  readonly #texts: FakeText[];
  readonly #decisions: unknown[];
  readonly #models: Map<string, ModelInfo> | undefined;

  constructor(script: FakeScript = {}) {
    this.#streams = [...(script.streams ?? [])];
    this.#answers = [...(script.answers ?? [])];
    this.#texts = [...(script.texts ?? [])];
    this.#decisions = [...(script.decisions ?? [])];
    this.#models =
      script.models && new Map(script.models.map((m) => [m.id, m]));
  }

  async modelInfo(id: string): Promise<ModelInfo> {
    if (!this.#models) {
      return id === DECISION_MODEL ? fakeDecisionModel(id) : fakeModel(id);
    }
    const info = this.#models.get(id);
    if (!info) {
      throw new ProviderError("unknown_model", `unknown model "${id}"`);
    }
    return info;
  }

  // Like the real provider: an abort between events ends the stream
  // with a `done` that keeps the partial text.
  async *stream(
    req: ChatRequest,
    signal: AbortSignal,
  ): AsyncGenerator<StreamEvent> {
    this.requests.push(structuredClone(req));
    const script = this.#streams.shift();
    if (!script) {
      throw new Error(
        `FakeProvider: no stream scripted for call ${this.requests.length}`,
      );
    }
    let text = "";
    for (const event of script) {
      if (signal.aborted) {
        yield { type: "done", message: aborted(text) };
        return;
      }
      if (event.type === "text_delta") text += event.text;
      yield event;
    }
  }

  async complete<T>(
    req: StructuredRequest<T>,
    signal?: AbortSignal,
  ): Promise<T> {
    this.structuredRequests.push(req);
    if (this.#answers.length === 0) {
      throw new Error("FakeProvider: no complete() answer scripted");
    }
    const next = this.#answers.shift();
    const answer =
      typeof next === "function"
        ? await (next as FakeAnswer)(req, signal)
        : next;
    if (answer instanceof Error) throw answer;
    req.onUsage?.(estimate(req.input, JSON.stringify(answer) ?? ""));
    return req.schema.parse(answer);
  }

  async completeText(
    req: TextRequest,
  ): Promise<{ text: string; usage: Usage }> {
    this.textRequests.push(req);
    const answer = this.#texts.shift();
    if (answer === undefined) {
      throw new Error("FakeProvider: no completeText() answer scripted");
    }
    if (answer instanceof Error) throw answer;
    if (typeof answer !== "string") return answer;
    return { text: answer, usage: estimate(req.input, answer) };
  }

  async decisions(
    req: DecisionsRequest,
    signal?: AbortSignal,
  ): Promise<DecisionsResponse> {
    this.decisionRequests.push(req);
    if (this.#decisions.length === 0) {
      throw new Error("FakeProvider: no decisions() answer scripted");
    }
    const next = this.#decisions.shift();
    const body =
      typeof next === "function"
        ? await (next as FakeDecision)(req, signal)
        : next;
    if (body instanceof Error) throw body;
    return parseDecisions(req, body);
  }
}

function estimate(input: string, output: string): Usage {
  return {
    inputTokens: Math.ceil(input.length / 4),
    outputTokens: Math.ceil(output.length / 4),
  };
}

// A capable model: 200k context, tools + structured outputs.
export function fakeModel(
  id: string,
  overrides: Partial<ModelInfo> = {},
): ModelInfo {
  return {
    id,
    contextLength: 200_000,
    maxOutputTokens: 32_000,
    supported: new Set(["tools", "structured_outputs", "response_format"]),
    pricing: { prompt: 0.000003, completion: 0.000015 },
    isDecisionModel: false,
    ...overrides,
  };
}

// A decision model as the decisions list describes one: no
// supported parameters, input billed, output free.
export function fakeDecisionModel(
  id: string,
  overrides: Partial<ModelInfo> = {},
): ModelInfo {
  return fakeModel(id, {
    contextLength: 262_144,
    maxOutputTokens: 235_929,
    supported: new Set(),
    pricing: { prompt: 0.000000042, completion: 0 },
    isDecisionModel: true,
    ...overrides,
  });
}

// The `done` event a script ends with: text, optional calls, usage.
export function fakeDone(
  content: string,
  toolCalls: AssistantMessage["toolCalls"] = [],
  usage: Usage = { inputTokens: 100, outputTokens: 20 },
): StreamEvent {
  return {
    type: "done",
    message: {
      role: "assistant",
      content,
      toolCalls,
      stopReason: toolCalls.length > 0 ? "tool_calls" : "stop",
      usage,
    },
  };
}

function aborted(text: string): AssistantMessage {
  return {
    role: "assistant",
    content: text,
    toolCalls: [],
    stopReason: "aborted",
  };
}
