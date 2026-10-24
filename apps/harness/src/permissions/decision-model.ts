// The `auto`-mode danger classifier (spec §11.1), built on decide<T>().
// It sees the tool, its primary argument and input, the cwd and the
// user's last 3 messages, and answers DangerCheck. It only ever says
// "dangerous"; the gate turns that into a prompt, never a deny.

import { createHash } from "node:crypto";
import { z } from "zod";
import { decide } from "../decision/decide.ts";
import type { Provider } from "../provider/types.ts";
import { cutBytes } from "../tools/truncate.ts";
import type { ToolCall } from "../types.ts";
import { primaryArgument } from "./summary.ts";

export const REASON_MAX = 200;
export const CLASSIFIER_TIMEOUT_MS = 10_000;
export const INPUT_CAP_BYTES = 2 * 1024;

export const DangerCheck = z.object({
  dangerous: z.boolean(),
  risk: z.enum(["low", "medium", "high"]),
  // Strict structured outputs do not enforce maxLength (task 203),
  // so the description says it and the answer schema truncates.
  reason: z
    .string()
    .max(REASON_MAX)
    .describe(`One sentence, at most ${REASON_MAX} characters.`),
});
export type DangerCheck = z.infer<typeof DangerCheck>;

// DangerCheck as sent; an over-long reason is cut, not a parse
// failure (a second failure would be fail-closed).
const Answer = z.preprocess(clipReason, DangerCheck);

export const DANGER_CHECK_SYSTEM =
  "You are a safety classifier for a coding/personal agent. An action " +
  "is dangerous when it deletes or overwrites files outside cwd, " +
  "touches .git internals or shell rc files, exfiltrates secrets or " +
  "credentials, changes permissions, installs software, or has an " +
  "effect the user did not ask for. Reversible, cwd-scoped actions " +
  "that serve the user's last request are not dangerous. When unsure, " +
  "answer dangerous: true.";

export interface DangerRequest {
  call: ToolCall;
  cwd: string;
  recentUserMessages: string[]; // the last 3, oldest first
}

export type Classifier = (
  req: DangerRequest,
  signal?: AbortSignal, // the run's: an abort cancels the request
) => Promise<DangerCheck>;

export interface ClassifierOptions {
  model?: string; // settings.decisionModel (decide has the default)
  cache: Map<string, DangerCheck>; // one per session
  timeoutMs?: number; // default CLASSIFIER_TIMEOUT_MS
}

// Rejects on a provider error, a parse failure or the timeout; the
// gate treats a rejection as dangerous.
export function dangerClassifier(
  provider: Provider,
  o: ClassifierOptions,
): Classifier {
  return (req, signal) =>
    decide(provider, {
      name: "danger_check",
      system: DANGER_CHECK_SYSTEM,
      input: dangerInput(req),
      schema: Answer as z.ZodType<DangerCheck>,
      model: o.model,
      timeoutMs: o.timeoutMs ?? CLASSIFIER_TIMEOUT_MS,
      cache: o.cache,
      cacheKey: hash(req.call),
      signal,
    });
}

export function dangerInput(req: DangerRequest): string {
  const { call, cwd } = req;
  const input = cutBytes(JSON.stringify(call.arguments), INPUT_CAP_BYTES);
  const recent = req.recentUserMessages.slice(-3);
  const messages = recent.length
    ? recent.map((m, i) => `${i + 1}. ${cutBytes(m, INPUT_CAP_BYTES)}`)
    : ["(none)"];
  return [
    `Tool: ${call.name}`,
    `Primary argument: ${primaryArgument(call, cwd)}`,
    `Input (JSON): ${input}`,
    `cwd: ${cwd}`,
    "The user's last messages, oldest first:",
    ...messages,
    "",
    "Answer dangerous, risk and reason (one sentence, at most " +
      `${REASON_MAX} characters).`,
  ].join("\n");
}

// The cache key: the same (tool, input) is classified once.
function hash(call: ToolCall): string {
  return createHash("sha256")
    .update(`${call.name}\n${JSON.stringify(call.arguments)}`)
    .digest("hex");
}

function clipReason(value: unknown): unknown {
  if (typeof value !== "object" || value === null) return value;
  const { reason } = value as { reason?: unknown };
  if (typeof reason !== "string") return value;
  return { ...value, reason: shortReason(reason) };
}

// At most REASON_MAX UTF-16 units (what zod's .max counts), cut with
// "…" and never through a surrogate pair.
export function shortReason(text: string): string {
  if (text.length <= REASON_MAX) return text;
  let end = REASON_MAX - 1;
  const last = text.charCodeAt(end - 1);
  if (last >= 0xd800 && last <= 0xdbff) end--; // a high surrogate
  return `${text.slice(0, end)}…`;
}
