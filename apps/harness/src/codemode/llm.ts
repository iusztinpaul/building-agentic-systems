// llm() inside the sandbox (spec §10.6, ADR-016 §9): the primitive
// that makes a codemode script recursive. A script pulls data into
// variables, calls llm() per slice and returns only the result, so
// the main context never holds the data. Depth 1 (the default) is a
// plain completion: no tools, no sandbox. Depth 2
// (codemode.llm.depth: 2) runs `{tools: true}` calls as a sub-loop
// over the model's own tools minus codemode and tool_search, through
// the same gate.
// llm() itself is never gated: it is budgeted (calls, input chars,
// tokens) and every call is reported as an llm_call event. It is an
// async host function, so it shares the sandbox's one concurrency
// cap with tools.x().

import { z } from "zod";
import type { Settings } from "../config/settings.ts";
import { runLoop } from "../loop/agent-loop.ts";
import type { GateResult } from "../loop/hooks.ts";
import type { ModelInfo, Provider } from "../provider/types.ts";
import { type ToolContext, ToolRegistry } from "../tools/registry.ts";
import { TOOL_SEARCH } from "../tools/tool-search.ts";
import type {
  AgentEndReason,
  AgentEvent,
  Context,
  ToolCall,
  Usage,
} from "../types.ts";
import { isRecord } from "./bridge.ts";
import type { AsyncHostFn, HostCall } from "./sandbox.ts";

export const DEFAULT_SYSTEM =
  "You are a sub-task worker. Answer only what is asked.";
export const DEFAULT_MAX_TOKENS = 2_048;

export type LlmCallEvent = AgentEvent & { type: "llm_call" };
export type LlmSettings = Settings["codemode"]["llm"];

export interface LlmOptions {
  provider: Provider;
  settings: LlmSettings;
  decisionModel: string; // the model when settings.model is unset
  registry: ToolRegistry; // depth 2: the sub-loop's tools
  gate: (call: ToolCall) => Promise<GateResult>;
  ctx: ToolContext; // the codemode call's
  exclude: string; // codemode itself: never in a sub-loop (nor tool_search)
  // Every call that reached the provider, with the model's pricing.
  onCall?: (event: LlmCallEvent, pricing: ModelInfo["pricing"]) => void;
}

// What a script passes; unknown keys are an error (a typo would
// otherwise be silently ignored).
const CallOptions = z
  .object({
    schema: z.record(z.unknown()).optional(),
    model: z.string().min(1).optional(),
    system: z.string().optional(),
    maxTokens: z.number().int().positive().optional(),
    tools: z.boolean().optional(),
  })
  .strict();
type CallOptions = z.infer<typeof CallOptions>;

const PREFIX = "llm budget exceeded";

// One per script: the budgets count across all its llm() calls.
export function createLlm(o: LlmOptions): AsyncHostFn {
  const s = o.settings;
  let calls = 0;
  let tokens = 0;

  return async (input, host) => {
    const [prompt, raw] = JSON.parse(input) as [string, unknown];
    const parsed = CallOptions.safeParse(raw ?? {});
    if (!parsed.success) {
      const issue = parsed.error.issues[0];
      const where = issue?.path.join(".") || "options";
      throw new TypeError(`llm(): ${where}: ${issue?.message}`);
    }
    const opts = parsed.data;
    // A schema it cannot represent costs nothing: no call is counted.
    const schema = opts.schema && zodFromJsonSchema(opts.schema);
    // `tools` is only honoured when depth 2 is configured.
    const loop = opts.tools === true && s.depth >= 2;
    const depth = o.ctx.depth + (loop ? 2 : 1);
    if (depth > s.depth) {
      throw new Error(
        `llm() at depth ${depth} is beyond codemode.llm.depth = ` +
          `${s.depth}; nested llm() calls are not supported`,
      );
    }
    if (++calls > s.maxCalls) {
      throw new Error(
        `${PREFIX}: maxCalls=${s.maxCalls} per script ` +
          "(codemode.llm.maxCalls)",
      );
    }
    const chars = prompt.length + (opts.system?.length ?? 0);
    if (chars > s.maxInputChars) {
      throw new Error(
        `${PREFIX}: maxInputChars=${s.maxInputChars} per call ` +
          `(this one has ${chars}; codemode.llm.maxInputChars)`,
      );
    }
    // Calls already running when the total crosses the line still
    // finish (they are paid for); every later one throws.
    if (tokens >= s.maxTotalTokens) {
      throw new Error(
        `${PREFIX}: maxTotalTokens=${s.maxTotalTokens} per script ` +
          `(used ${tokens}; codemode.llm.maxTotalTokens)`,
      );
    }
    if (loop && opts.schema) {
      throw new TypeError(
        "llm(): schema and tools cannot be combined; call llm() " +
          "again with the answer and a schema",
      );
    }
    const model = opts.model ?? s.model ?? o.decisionModel;
    // Up front: an unknown model fails before anything is sent, and
    // the event carries the price the session cost needs.
    const { pricing, maxOutputTokens } = await o.provider.modelInfo(model);
    const usage: Usage = { inputTokens: 0, outputTokens: 0 };
    const spend = (u: Usage) => {
      usage.inputTokens += u.inputTokens;
      usage.outputTokens += u.outputTokens;
      tokens += u.inputTokens + u.outputTokens;
    };
    // Never more than the model's output cap (the catalogue's
    // max_completion_tokens, else its context length).
    const maxTokens = Math.min(
      opts.maxTokens ?? DEFAULT_MAX_TOKENS,
      maxOutputTokens || Number.POSITIVE_INFINITY,
    );
    const call = { prompt, opts, schema, maxTokens, model, host, spend };
    try {
      const answer = loop
        ? await subLoop(o, call, depth)
        : await complete(o, call);
      return JSON.stringify(answer) ?? "null";
    } finally {
      // Failed calls too: what they used is spent.
      const used = usage.inputTokens + usage.outputTokens > 0;
      const event: LlmCallEvent = {
        type: "llm_call",
        depth,
        model,
        ...(used && { usage: { ...usage } }),
      };
      o.onCall?.(event, pricing);
    }
  };
}

interface Call {
  prompt: string;
  opts: CallOptions;
  schema: z.ZodTypeAny | undefined;
  maxTokens: number;
  model: string;
  host: HostCall;
  spend: (usage: Usage) => void;
}

// Depth 1: one completion; with a schema, complete<T> (its one parse
// retry included), else completeText.
async function complete(o: LlmOptions, c: Call): Promise<unknown> {
  const system = c.opts.system ?? DEFAULT_SYSTEM;
  const maxOutputTokens = c.maxTokens;
  const { signal } = c.host;
  if (c.schema) {
    return o.provider.complete(
      {
        model: c.model,
        system,
        input: c.prompt,
        schema: c.schema,
        name: "llm",
        maxOutputTokens,
        onUsage: c.spend,
      },
      signal,
    );
  }
  const { text, usage } = await o.provider.completeText(
    { model: c.model, system, input: c.prompt, maxOutputTokens },
    signal,
  );
  c.spend(usage);
  return text;
}

// Depth 2: a fresh Context and runLoop over the model's own tools
// (direct ∪ activated) minus codemode, at most maxSubTurns turns.
// Minus tool_search too: it would activate in the main registry,
// never in this copy.
// Only the gate is shared with the main loop: the session's other
// hooks (prompt rebuild, queues, events) belong to the main run.
async function subLoop(
  o: LlmOptions,
  c: Call,
  depth: number,
): Promise<string> {
  const tools = new ToolRegistry();
  for (const tool of o.registry.declared()) {
    if (tool.name !== o.exclude && tool.name !== TOOL_SEARCH) {
      tools.register(tool);
    }
  }
  tools.activate(o.registry.activated());
  const ctx: Context = {
    system: c.opts.system ?? DEFAULT_SYSTEM,
    messages: [{ role: "user", content: c.prompt }],
  };
  let reason = "error" as AgentEndReason; // set by agent_end
  let error = "";
  await runLoop(ctx, {
    provider: o.provider,
    model: c.model,
    tools,
    hooks: {
      // A prompt is the user's time, not the script's.
      beforeToolCall: async (call) => {
        const release = c.host.hold();
        try {
          return await o.gate(call);
        } finally {
          release();
        }
      },
      emit: (event) => {
        if (event.type === "turn_end" && event.message.usage) {
          c.spend(event.message.usage);
        } else if (event.type === "error") {
          error = event.message;
        } else if (event.type === "agent_end") {
          reason = event.reason;
        }
      },
    },
    maxTurns: o.settings.maxSubTurns,
    signal: c.host.signal,
    cwd: o.ctx.cwd,
    depth,
  });
  if (reason === "max_turns") {
    throw new Error(
      `llm() sub-loop stopped at maxSubTurns=${o.settings.maxSubTurns} ` +
        "without an answer (codemode.llm.maxSubTurns)",
    );
  }
  if (reason !== "completed") {
    throw new Error(
      `llm() sub-loop ended ${reason}${error && `: ${error}`}`,
    );
  }
  const last = ctx.messages.at(-1);
  return last?.role === "assistant" ? last.content : "";
}

// The script's JSON Schema as zod, so complete<T> sends it (strict:
// every property required, no extra keys; an optional one may be
// null) and validates the answer. The subset structured outputs
// accept: type (or a list), properties/required, items, enum/const of
// primitives, anyOf/oneOf, minItems/maxItems, minLength/maxLength/
// pattern, minimum/maximum/exclusiveMinimum/exclusiveMaximum/
// multipleOf, additionalProperties: false; annotations (description,
// title, default, examples, $comment, $defs) are kept or ignored.
// `{}` and a `$ref` are any value (refs are not resolved). Any other
// keyword is a TypeError naming it: never a silently looser schema.
export function zodFromJsonSchema(
  schema: unknown,
  path = "schema",
): z.ZodTypeAny {
  if (!isRecord(schema)) {
    throw new TypeError(`llm(): ${path}: must be a JSON Schema object`);
  }
  if ("$ref" in schema) return z.unknown();
  for (const key of Object.keys(schema)) {
    if (!KEYWORDS.has(key)) {
      throw new TypeError(`llm(): ${path}: "${key}" is not supported`);
    }
  }
  const out = convert(schema, path);
  return typeof schema.description === "string"
    ? out.describe(schema.description)
    : out;
}

const ANNOTATIONS = [
  "description",
  "title",
  "default",
  "examples",
  "$comment",
  "$schema",
  "$id",
  "$defs",
  "definitions",
];
const CONSTRAINTS = [
  "minItems",
  "maxItems",
  "minLength",
  "maxLength",
  "pattern",
  "minimum",
  "maximum",
  "exclusiveMinimum",
  "exclusiveMaximum",
  "multipleOf",
];
const KEYWORDS = new Set([
  ...ANNOTATIONS,
  ...CONSTRAINTS,
  "type",
  "enum",
  "const",
  "anyOf",
  "oneOf",
  "properties",
  "required",
  "additionalProperties",
  "items",
]);

function convert(s: Record<string, unknown>, path: string): z.ZodTypeAny {
  if ("enum" in s) {
    if (!Array.isArray(s.enum)) {
      throw new TypeError(`llm(): ${path}.enum: must be an array`);
    }
    return literals(s.enum, `${path}.enum`);
  }
  if ("const" in s) return literals([s.const], `${path}.const`);
  const key = "anyOf" in s ? "anyOf" : "oneOf" in s ? "oneOf" : undefined;
  if (key) {
    const union = s[key];
    if (!Array.isArray(union)) {
      throw new TypeError(`llm(): ${path}.${key}: must be an array`);
    }
    return anyOf(
      union.map((u, i) => zodFromJsonSchema(u, `${path}.${key}[${i}]`)),
    );
  }
  const { type } = s;
  if (Array.isArray(type)) {
    return anyOf(type.map((t) => convert({ ...s, type: t }, path)));
  }
  switch (type) {
    case "string":
      return string(s, path);
    case "number":
      return number(z.number(), s, path);
    case "integer":
      return number(z.number().int(), s, path);
    case "boolean":
      return z.boolean();
    case "null":
      return z.null();
    case "array": {
      const items =
        "items" in s
          ? zodFromJsonSchema(s.items, `${path}.items`)
          : z.unknown();
      let out = z.array(items);
      const min = count(s, "minItems", path);
      const max = count(s, "maxItems", path);
      if (min !== undefined) out = out.min(min);
      if (max !== undefined) out = out.max(max);
      return out;
    }
    case "object":
    case undefined:
      if (type === "object" || "properties" in s) return object(s, path);
      break;
    default:
      throw new TypeError(
        `llm(): ${path}.type: "${String(type)}" is not a JSON Schema type`,
      );
  }
  // No type: only annotations make "any value"; a constraint would
  // otherwise be dropped.
  const constraint =
    CONSTRAINTS.find((k) => k in s) ?? ("items" in s ? "items" : undefined);
  if (constraint) {
    throw new TypeError(`llm(): ${path}: "${constraint}" needs a "type"`);
  }
  return z.unknown();
}

function object(s: Record<string, unknown>, path: string): z.ZodTypeAny {
  if ("additionalProperties" in s && s.additionalProperties !== false) {
    throw new TypeError(
      `llm(): ${path}.additionalProperties: only false is supported`,
    );
  }
  const props = isRecord(s.properties) ? s.properties : {};
  const required = new Set(Array.isArray(s.required) ? s.required : []);
  // A null-prototype shape: a "__proto__" key would set the prototype.
  const shape: Record<string, z.ZodTypeAny> = Object.create(null);
  for (const [key, value] of Object.entries(props)) {
    if (key === "__proto__") {
      throw new TypeError(
        `llm(): ${path}.properties: "__proto__" is not a supported name`,
      );
    }
    const field = zodFromJsonSchema(value, `${path}.properties.${key}`);
    shape[key] = required.has(key) ? field : field.nullish();
  }
  return z.object(shape);
}

function string(s: Record<string, unknown>, path: string): z.ZodTypeAny {
  let out = z.string();
  const min = count(s, "minLength", path);
  const max = count(s, "maxLength", path);
  if (min !== undefined) out = out.min(min);
  if (max !== undefined) out = out.max(max);
  if ("pattern" in s) {
    let re: RegExp;
    try {
      re = new RegExp(String(s.pattern));
    } catch (err) {
      throw new TypeError(
        `llm(): ${path}.pattern: ${(err as Error).message}`,
      );
    }
    out = out.regex(re);
  }
  return out;
}

function number(
  base: z.ZodNumber,
  s: Record<string, unknown>,
  path: string,
): z.ZodTypeAny {
  let out = base;
  const bound = (key: string): number | undefined => {
    if (!(key in s)) return undefined;
    const v = s[key];
    if (typeof v !== "number" || !Number.isFinite(v)) {
      throw new TypeError(`llm(): ${path}.${key}: must be a number`);
    }
    return v;
  };
  const min = bound("minimum");
  const max = bound("maximum");
  const gt = bound("exclusiveMinimum");
  const lt = bound("exclusiveMaximum");
  const step = bound("multipleOf");
  if (min !== undefined) out = out.min(min);
  if (max !== undefined) out = out.max(max);
  if (gt !== undefined) out = out.gt(gt);
  if (lt !== undefined) out = out.lt(lt);
  if (step !== undefined) out = out.multipleOf(step);
  return out;
}

// A non-negative integer keyword (minItems, maxLength, …), if set.
function count(
  s: Record<string, unknown>,
  key: string,
  path: string,
): number | undefined {
  if (!(key in s)) return undefined;
  const v = s[key];
  if (typeof v !== "number" || !Number.isInteger(v) || v < 0) {
    throw new TypeError(
      `llm(): ${path}.${key}: must be a non-negative integer`,
    );
  }
  return v;
}

// Only primitives: z.literal compares by identity, so an object or
// array value could never match.
function literals(values: unknown[], path: string): z.ZodTypeAny {
  const options = values.map((v) => {
    if (v !== null && typeof v === "object") {
      throw new TypeError(
        `llm(): ${path}: only string, number, boolean or null values are supported`,
      );
    }
    return z.literal(v as z.Primitive);
  }) as z.ZodTypeAny[];
  return anyOf(options);
}

function anyOf(options: z.ZodTypeAny[]): z.ZodTypeAny {
  const [a, b, ...rest] = options;
  if (!a) return z.never();
  if (!b) return a;
  return z.union([a, b, ...rest]);
}
