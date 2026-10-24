// A scripted Provider for tests: each stream() call replays the next
// StreamEvent[] script, complete()/completeText() return the next
// scripted answer, and every request is recorded for assertions.

import type { AssistantMessage, Usage } from "../types.ts";
import {
  type ChatRequest,
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
  // Error rejects the call.
  answers?: unknown[];
  // Answers for completeText(); an Error rejects the call.
  texts?: (string | Error)[];
  // When set, modelInfo() serves only these ids; otherwise any id
  // resolves to fakeModel(id).
  models?: ModelInfo[];
}

export class FakeProvider implements Provider {
  readonly requests: ChatRequest[] = [];
  readonly structuredRequests: StructuredRequest<unknown>[] = [];
  readonly textRequests: TextRequest[] = [];
  readonly #streams: StreamEvent[][];
  readonly #answers: unknown[];
  readonly #texts: (string | Error)[];
  readonly #models: Map<string, ModelInfo> | undefined;

  constructor(script: FakeScript = {}) {
    this.#streams = [...(script.streams ?? [])];
    this.#answers = [...(script.answers ?? [])];
    this.#texts = [...(script.texts ?? [])];
    this.#models =
      script.models && new Map(script.models.map((m) => [m.id, m]));
  }

  async modelInfo(id: string): Promise<ModelInfo> {
    if (!this.#models) return fakeModel(id);
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

  async complete<T>(req: StructuredRequest<T>): Promise<T> {
    this.structuredRequests.push(req);
    if (this.#answers.length === 0) {
      throw new Error("FakeProvider: no complete() answer scripted");
    }
    const answer = this.#answers.shift();
    if (answer instanceof Error) throw answer;
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
    return {
      text: answer,
      usage: {
        inputTokens: Math.ceil(req.input.length / 4),
        outputTokens: Math.ceil(answer.length / 4),
      },
    };
  }
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
    ...overrides,
  };
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
