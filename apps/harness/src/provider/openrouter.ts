// OpenRouter over raw fetch: the chat-completions wire (spec §5) with
// no SDK in between. `fetch` is injectable so tests replay recorded
// SSE bodies offline.

import { ZodError, type z } from "zod";
import { zodToJsonSchema } from "zod-to-json-schema";
import { log } from "../log.ts";
import type {
  AssistantMessage,
  Context,
  Message,
  StopReason,
  ToolCall,
  Usage,
} from "../types.ts";
import { parseSse } from "./sse.ts";
import {
  type ChatRequest,
  type JsonSchema,
  type ModelInfo,
  type Provider,
  ProviderError,
  type StreamEvent,
  type StructuredRequest,
  type TextRequest,
  type ToolSchema,
} from "./types.ts";

export const OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1";
export const CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded";
const REFERER = "https://github.com/iusztinpaul/building-agentic-systems";

export interface OpenRouterOptions {
  apiKey: string;
  fetch?: typeof fetch;
  baseUrl?: string;
}

export class OpenRouterProvider implements Provider {
  readonly #apiKey: string;
  readonly #fetch: typeof fetch;
  readonly #baseUrl: string;
  #models: Promise<Map<string, ModelInfo>> | undefined;

  constructor(options: OpenRouterOptions) {
    this.#apiKey = options.apiKey;
    this.#fetch = options.fetch ?? fetch;
    this.#baseUrl = options.baseUrl ?? OPENROUTER_BASE_URL;
  }

  async modelInfo(id: string): Promise<ModelInfo> {
    this.#models ??= this.#loadModels().catch((err: unknown) => {
      this.#models = undefined; // let the next call retry
      throw err;
    });
    const info = (await this.#models).get(id);
    if (!info) {
      throw new ProviderError(
        "unknown_model",
        `unknown model "${id}": not listed by GET /api/v1/models`,
      );
    }
    return info;
  }

  // Never throws: ends with exactly one `done` or `error` event.
  async *stream(
    req: ChatRequest,
    signal: AbortSignal,
  ): AsyncGenerator<StreamEvent> {
    const body = {
      model: req.model,
      messages: toWireMessages(req.context),
      ...(req.tools.length > 0 && { tools: toWireTools(req.tools) }),
      ...(req.maxOutputTokens && { max_tokens: req.maxOutputTokens }),
      stream: true,
      stream_options: { include_usage: true },
    };
    let res: Response;
    try {
      res = await this.#post("/chat/completions", body, signal);
    } catch (err) {
      yield signal.aborted ? abortedDone("") : networkError(err);
      return;
    }
    if (!res.ok || !res.body) {
      yield { type: "error", ...(await httpError(res)) };
      return;
    }
    yield* decodeChatStream(parseSse(res.body), signal);
  }

  async complete<T>(
    req: StructuredRequest<T>,
    signal?: AbortSignal,
  ): Promise<T> {
    const responseFormat = {
      type: "json_schema",
      json_schema: {
        name: req.name,
        strict: true,
        schema: strictJsonSchema(req.schema),
      },
    };
    let input = req.input;
    for (let attempt = 1; ; attempt++) {
      const { text } = await this.#completion(
        {
          model: req.model,
          messages: [
            { role: "system", content: req.system },
            { role: "user", content: input },
          ],
          response_format: responseFormat,
          provider: { require_parameters: true },
          ...(req.maxOutputTokens && { max_tokens: req.maxOutputTokens }),
        },
        signal,
      );
      try {
        return req.schema.parse(JSON.parse(text));
      } catch (err) {
        const reason = describeParseError(err);
        if (attempt === 2) {
          throw new ProviderError(
            "invalid_structured_output",
            `${req.name}: ${reason}`,
          );
        }
        log.info(`${req.name}: retrying invalid answer (${reason})`);
        input =
          `${req.input}\n\nYour previous answer did not match the ` +
          `schema. Answer again with JSON only. Error: ${reason}`;
      }
    }
  }

  completeText(
    req: TextRequest,
    signal?: AbortSignal,
  ): Promise<{ text: string; usage: Usage }> {
    const messages = [
      ...(req.system ? [{ role: "system", content: req.system }] : []),
      { role: "user", content: req.input },
    ];
    return this.#completion(
      {
        model: req.model,
        messages,
        ...(req.maxOutputTokens && { max_tokens: req.maxOutputTokens }),
      },
      signal,
    );
  }

  async #completion(
    body: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<{ text: string; usage: Usage }> {
    const res = await this.#post("/chat/completions", body, signal);
    if (!res.ok) {
      const { code, message } = await httpError(res);
      throw new ProviderError(code, message);
    }
    const data = await readJson<WireCompletion>(res);
    if (data.error) {
      const { code, message } = chunkError(data.error);
      throw new ProviderError(code, message);
    }
    return {
      text: data.choices?.[0]?.message?.content ?? "",
      usage: toUsage(data.usage),
    };
  }

  async #loadModels(): Promise<Map<string, ModelInfo>> {
    log.debug("GET /models");
    const res = await this.#fetch(`${this.#baseUrl}/models`, {
      headers: this.#headers(),
    });
    if (!res.ok) {
      const { code, message } = await httpError(res);
      throw new ProviderError(code, `GET /api/v1/models: ${message}`);
    }
    const { data } = await readJson<{ data: WireModel[] }>(res);
    return new Map(data.map((m) => [m.id, toModelInfo(m)]));
  }

  #post(
    path: string,
    body: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<Response> {
    log.debug(`POST ${path} model=${String(body.model)}`);
    return this.#fetch(`${this.#baseUrl}${path}`, {
      method: "POST",
      headers: {
        ...this.#headers(),
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      signal,
    });
  }

  #headers(): Record<string, string> {
    return {
      Authorization: `Bearer ${this.#apiKey}`,
      "HTTP-Referer": REFERER,
      "X-Title": "tree-agent",
    };
  }
}

// ---- request side ------------------------------------------------

export function toWireMessages(context: Context): WireMessage[] {
  return [
    { role: "system", content: context.system },
    ...context.messages.map(toWireMessage),
  ];
}

function toWireMessage(message: Message): WireMessage {
  switch (message.role) {
    case "user":
      return { role: "user", content: message.content };
    case "tool":
      return {
        role: "tool",
        tool_call_id: message.toolCallId,
        content: message.content,
      };
    case "assistant":
      if (message.toolCalls.length === 0) {
        return { role: "assistant", content: message.content };
      }
      return {
        role: "assistant",
        content: message.content === "" ? null : message.content,
        tool_calls: message.toolCalls.map((call) => ({
          id: call.id,
          type: "function",
          function: {
            name: call.name,
            // Always a valid JSON string, even for a parseError call.
            arguments: JSON.stringify(call.arguments),
          },
        })),
      };
  }
}

function toWireTools(tools: ToolSchema[]): unknown[] {
  return tools.map((tool) => ({
    type: "function",
    function: {
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters,
    },
  }));
}

// Strict structured outputs need every property required; the openAi
// target turns `.optional()` into a required nullable field.
function strictJsonSchema(schema: z.ZodType): JsonSchema {
  const json = zodToJsonSchema(schema, {
    target: "openAi",
    $refStrategy: "none",
  }) as JsonSchema;
  delete json.$schema;
  return json;
}

// ---- response side -----------------------------------------------

// Turns SSE `data` payloads into StreamEvents. Text and tool-call
// deltas stream through; tool calls are reassembled by `index` and
// JSON-parsed once, at `done`. The usage chunk that repeats
// finish_reason before [DONE] is accounting only.
export async function* decodeChatStream(
  payloads: AsyncIterable<string>,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  let text = "";
  let finish: string | undefined;
  let usage: Usage | undefined;
  const calls = new Map<number, PendingCall>();

  try {
    for await (const payload of payloads) {
      let chunk: WireChunk;
      try {
        chunk = JSON.parse(payload) as WireChunk;
      } catch {
        yield {
          type: "error",
          code: "invalid_chunk",
          message: `unparseable stream chunk: ${payload.slice(0, 200)}`,
        };
        return;
      }
      if (!chunk || typeof chunk !== "object" || Array.isArray(chunk)) {
        yield {
          type: "error",
          code: "invalid_chunk",
          message: `unexpected stream chunk: ${payload.slice(0, 200)}`,
        };
        return;
      }
      if (chunk.error) {
        yield { type: "error", ...chunkError(chunk.error) };
        return;
      }
      if (chunk.usage) usage = toUsage(chunk.usage);
      const choice = chunk.choices?.[0];
      if (!choice) continue;

      const content = choice.delta?.content;
      if (content) {
        text += content;
        yield { type: "text_delta", text: content };
      }
      for (const delta of toolCallDeltas(choice.delta?.tool_calls)) {
        let call = calls.get(delta.index);
        if (!call) {
          call = {
            id: delta.id ?? `call_${delta.index}`,
            name: delta.function?.name ?? "",
            arguments: "",
          };
          calls.set(delta.index, call);
          yield {
            type: "tool_call_start",
            index: delta.index,
            id: call.id,
            name: call.name,
          };
        }
        const piece = delta.function?.arguments;
        if (piece) {
          call.arguments += piece;
          yield {
            type: "tool_call_delta",
            index: delta.index,
            argumentsDelta: piece,
          };
        }
      }
      if (choice.finish_reason) finish = choice.finish_reason;
    }
  } catch (err) {
    yield signal?.aborted ? abortedDone(text) : networkError(err);
    return;
  }

  // A truncated body must not pass as a complete answer.
  if (finish === undefined) {
    yield {
      type: "error",
      code: "incomplete_stream",
      message: "stream ended before a finish_reason",
    };
    return;
  }
  const toolCalls = [...calls.entries()]
    .sort(([a], [b]) => a - b)
    .map(([, call]) => finishCall(call));
  yield {
    type: "done",
    message: {
      role: "assistant",
      content: text,
      toolCalls,
      stopReason: toStopReason(finish),
      ...(usage && { usage }),
    },
  };
}

// A malformed upstream delta (tool_calls not an array, a delta with no
// numeric index) is dropped rather than keyed `undefined`.
function toolCallDeltas(raw: unknown): WireToolCallDelta[] {
  if (raw === undefined || raw === null) return [];
  if (!Array.isArray(raw)) {
    log.debug(`ignoring non-array tool_calls: ${preview(raw)}`);
    return [];
  }
  return raw.filter((delta: WireToolCallDelta | null) => {
    if (typeof delta?.index === "number") return true;
    log.debug(`ignoring tool_call delta without index: ${preview(delta)}`);
    return false;
  });
}

function preview(value: unknown): string {
  return String(JSON.stringify(value)).slice(0, 200);
}

export function toStopReason(finishReason: string): StopReason {
  switch (finishReason) {
    case "tool_calls":
    case "length":
    case "error":
      return finishReason;
    case "content_filter":
      return "error";
    default:
      return "stop";
  }
}

function finishCall(call: PendingCall): ToolCall {
  const base = { id: call.id, name: call.name };
  const raw = call.arguments.trim() === "" ? "{}" : call.arguments;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch (err) {
    return {
      ...base,
      arguments: {},
      parseError: `invalid JSON arguments for ${call.name}: ${
        (err as Error).message
      }`,
    };
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return {
      ...base,
      arguments: {},
      parseError: `arguments for ${call.name} must be a JSON object`,
    };
  }
  return { ...base, arguments: value as Record<string, unknown> };
}

function abortedDone(text: string): StreamEvent {
  const message: AssistantMessage = {
    role: "assistant",
    content: text,
    toolCalls: [],
    stopReason: "aborted",
  };
  return { type: "done", message };
}

function networkError(err: unknown): StreamEvent {
  const message = err instanceof Error ? err.message : String(err);
  return { type: "error", code: "network_error", message };
}

// HTTP 4xx/5xx before streaming: the body is plain JSON
// `{error: {code, message, metadata?: {raw}}}` (or not JSON at all).
async function httpError(
  res: Response,
): Promise<{ code: string; message: string }> {
  const text = await res.text().catch(() => "");
  let code = String(res.status);
  let message = text.slice(0, 500) || `HTTP ${res.status}`;
  let raw = "";
  try {
    const body = JSON.parse(text) as { error?: WireError };
    if (body.error) {
      ({ code, message } = chunkError(body.error, code));
      const upstream = body.error.metadata?.raw;
      raw = typeof upstream === "string" ? upstream : "";
    }
  } catch {
    // not JSON: keep the status code and the text
  }
  if (raw) message = `${message}: ${raw.slice(0, 300)}`;
  if (res.status === 400 && /context length/i.test(message)) {
    code = CONTEXT_LENGTH_EXCEEDED;
  }
  return { code, message };
}

// A 200 whose body is not a JSON object (an HTML page from a proxy)
// rejects with a ProviderError, never a bare SyntaxError.
async function readJson<T>(res: Response): Promise<T> {
  const text = await res.text();
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    value = undefined;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ProviderError(
      "invalid_response",
      `response body is not a JSON object: ${text.slice(0, 200)}`,
    );
  }
  return value as T;
}

function chunkError(
  error: WireError,
  fallbackCode = "provider_error",
): { code: string; message: string } {
  return {
    code: String(error.code ?? fallbackCode),
    message: error.message ?? "OpenRouter returned an error",
  };
}

function describeParseError(err: unknown): string {
  if (err instanceof ZodError) {
    return err.issues
      .map((i) => `${i.path.join(".") || "(root)"}: ${i.message}`)
      .join("; ");
  }
  return err instanceof Error ? err.message : String(err);
}

function toUsage(usage: WireUsage | undefined): Usage {
  const cached = usage?.prompt_tokens_details?.cached_tokens;
  return {
    inputTokens: usage?.prompt_tokens ?? 0,
    outputTokens: usage?.completion_tokens ?? 0,
    ...(cached ? { cachedTokens: cached } : {}),
  };
}

function toModelInfo(model: WireModel): ModelInfo {
  return {
    id: model.id,
    contextLength: model.context_length,
    maxOutputTokens:
      model.top_provider?.max_completion_tokens ?? model.context_length,
    supported: new Set(model.supported_parameters ?? []),
    pricing: {
      prompt: Number(model.pricing?.prompt ?? 0),
      completion: Number(model.pricing?.completion ?? 0),
    },
  };
}

// ---- wire shapes (only the fields we read or write) --------------

type WireMessage =
  | { role: "system" | "user"; content: string }
  | { role: "tool"; tool_call_id: string; content: string }
  | {
      role: "assistant";
      content: string | null;
      tool_calls?: {
        id: string;
        type: "function";
        function: { name: string; arguments: string };
      }[];
    };

interface WireError {
  code?: string | number;
  message?: string;
  metadata?: { raw?: unknown };
}

interface WireUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  prompt_tokens_details?: { cached_tokens?: number };
}

interface WireChunk {
  error?: WireError;
  usage?: WireUsage;
  choices?: {
    finish_reason?: string | null;
    delta?: {
      content?: string | null;
      tool_calls?: WireToolCallDelta[];
    };
  }[];
}

interface WireToolCallDelta {
  index: number;
  id?: string;
  function?: { name?: string; arguments?: string };
}

interface WireCompletion {
  error?: WireError;
  usage?: WireUsage;
  choices?: { message?: { content?: string | null } }[];
}

interface WireModel {
  id: string;
  context_length: number;
  top_provider?: { max_completion_tokens?: number | null };
  supported_parameters?: string[];
  pricing?: { prompt?: string; completion?: string };
}

interface PendingCall {
  id: string;
  name: string;
  arguments: string;
}
