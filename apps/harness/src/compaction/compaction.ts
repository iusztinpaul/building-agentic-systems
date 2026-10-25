// Compaction (spec §13): when the estimate reaches triggerRatio × the
// current model's contextLength, cut the messages at a turn boundary
// keeping ≈ keepRecentTokens of tail, summarise the head on the
// decision model (decide<T>(), merging any earlier summary) and go on
// with [system][summary][tail]. The session applies the result (one
// `compaction` entry, one event); this module owns the trigger, the
// cut, the summariser and the overflow retry.

import { z } from "zod";
import { decide } from "../decision/decide.ts";
import { log } from "../log.ts";
import { CONTEXT_LENGTH_EXCEEDED } from "../provider/openrouter.ts";
import type { Provider, StreamEvent } from "../provider/types.ts";
import { summaryMessage } from "../session-store/jsonl.ts";
import { headTail } from "../tools/truncate.ts";
import type { AgentEvent, Context, Message } from "../types.ts";
import { TokenEstimator } from "./estimate.ts";

export interface CompactionSettings {
  triggerRatio: number; // 0.3–0.95 (settings, --compact-at)
  keepRecentTokens: number;
}

// Consecutive failed summaries before compaction stops for the
// session (one `error` event, no loop of retries).
export const MAX_CONSECUTIVE_FAILURES = 3;
const SUMMARY_TIMEOUT_MS = 120_000;
// Each tool result enters the summariser input as head + tail of at
// most this many bytes (pi's serializer truncates them the same way).
export const TOOL_RESULT_BYTES = 2_000;

export const Summary = z.object({
  goal: z.string(),
  constraints: z.string().array(),
  progress: z.string().array(),
  decisions: z.string().array(),
  nextSteps: z.string().array(),
  // Paths, names and errors, verbatim.
  criticalContext: z.string().array(),
});
export type Summary = z.infer<typeof Summary>;

// pi's summarisation prompts (core/compaction), with its markdown
// sections as the Summary schema's fields.
const SUMMARY_SYSTEM =
  "You are a context summarization assistant. Your task is to read a " +
  "conversation between a user and an AI coding assistant, then " +
  "produce a structured summary following the exact format " +
  "specified. Do NOT continue the conversation. Do NOT respond to " +
  "any questions in the conversation. ONLY output the structured " +
  "summary.";

const FIELDS = [
  "- goal: what the user is trying to accomplish (several tasks " +
    "if the session covers several).",
  "- constraints: constraints, preferences and requirements the " +
    "user stated.",
  "- progress: what is done, what is in progress, what is blocked.",
  "- decisions: key decisions, each with its brief rationale.",
  "- nextSteps: an ordered list of what should happen next.",
  "- criticalContext: data, examples and references needed to " +
    "continue: exact file paths, function names and error messages.",
].join("\n");

const SUMMARY_PROMPT = [
  "The messages above are a conversation to summarize. Create a " +
    "structured context checkpoint summary that another LLM will use " +
    "to continue the work.",
  "",
  FIELDS,
  "",
  "Keep each item concise. Preserve exact file paths, function " +
    "names, and error messages.",
].join("\n");

export const UPDATE_PROMPT = [
  "The messages above are NEW conversation messages to incorporate " +
    "into the existing summary provided in <previous-summary> tags.",
  "",
  "Update the existing structured summary with new information. RULES:",
  "- PRESERVE all existing information from the previous summary",
  "- ADD new progress, decisions, and context from the new messages",
  "- UPDATE progress: mark items done when they were completed",
  "- UPDATE nextSteps based on what was accomplished",
  "- PRESERVE exact file paths, function names, and error messages",
  "- If something is no longer relevant, you may remove it",
  "",
  FIELDS,
].join("\n");

// The kept tail: ≈ keepRecentTokens, but at most half the trigger,
// so a small window (or keepRecentTokens ≥ the trigger) still frees
// room instead of compacting again on the next turn.
export function keepTokens(
  settings: CompactionSettings,
  contextLength: number,
): number {
  const half = Math.floor((settings.triggerRatio * contextLength) / 2);
  return Math.min(settings.keepRecentTokens, half);
}

// The index of the first kept message; 0 when there is nothing to
// summarise. Walk back until the tail holds `keep` tokens, then
// forward to the nearest `user` message, so an assistant tool_calls
// message never loses its `tool` results. With no user message ahead
// (one prompt, many tool turns) the nearest assistant message is the
// boundary instead; with neither, the last one before the walk point.
export function cutPoint(
  messages: readonly Message[],
  keep: number,
  tokensOf: (m: Message) => number,
): number {
  let walk = messages.length;
  let tail = 0;
  while (walk > 0 && tail < keep) {
    walk--;
    tail += tokensOf(messages[walk] as Message);
  }
  const boundary = (role: Message["role"]): number =>
    messages.findIndex((m, i) => i >= walk && m.role === role);
  let cut = boundary("user");
  if (cut === -1) cut = boundary("assistant");
  for (let i = walk - 1; cut === -1 && i >= 0; i--) {
    if (messages[i]?.role !== "tool") cut = i;
  }
  // A head of nothing, or of the previous summary alone, is no work.
  const floor = previousSummary(messages[0]) === undefined ? 1 : 2;
  return cut < floor ? 0 : cut;
}

// The rendered markdown inside a `<summary>` message, else undefined.
export function previousSummary(
  m: Message | undefined,
): string | undefined {
  if (m?.role !== "user") return undefined;
  const match = /^<summary>\n([\s\S]*)\n<\/summary>$/.exec(m.content);
  return match?.[1];
}

export function renderSummary(s: Summary): string {
  const list = (items: string[], numbered = false): string =>
    items.length === 0
      ? "- (none)"
      : items
          .map((item, i) => `${numbered ? `${i + 1}.` : "-"} ${item}`)
          .join("\n");
  return [
    `## Goal\n${s.goal}`,
    `## Constraints\n${list(s.constraints)}`,
    `## Progress\n${list(s.progress)}`,
    `## Decisions\n${list(s.decisions)}`,
    `## Next steps\n${list(s.nextSteps, true)}`,
    `## Critical context\n${list(s.criticalContext)}`,
  ].join("\n\n");
}

// pi's serializer: one tagged line per message, tool results cut.
function serializeConversation(messages: readonly Message[]): string {
  return messages
    .map((m) => {
      if (m.role === "user") return `[User]: ${m.content}`;
      if (m.role === "tool") {
        const content = headTail(m.content, TOOL_RESULT_BYTES);
        return `[Tool result ${m.name}]: ${content}`;
      }
      const calls = m.toolCalls.map(
        (c) => `${c.name}(${JSON.stringify(c.arguments)})`,
      );
      return [
        ...(m.content ? [`[Assistant]: ${m.content}`] : []),
        ...(calls.length
          ? [`[Assistant tool calls]: ${calls.join("; ")}`]
          : []),
      ].join("\n");
    })
    .join("\n\n");
}

export function summaryInput(
  head: readonly Message[],
  previous: string | undefined,
  instructions?: string,
): string {
  const parts = [
    `<conversation>\n${serializeConversation(head)}\n</conversation>`,
  ];
  if (previous !== undefined) {
    parts.push(`<previous-summary>\n${previous}\n</previous-summary>`);
  }
  parts.push(previous === undefined ? SUMMARY_PROMPT : UPDATE_PROMPT);
  if (instructions) parts.push(`Additional focus: ${instructions}`);
  return parts.join("\n\n");
}

export interface Compacted {
  cut: number; // messages[cut..] are the tail
  summary: string; // rendered markdown, without the tags
  before: number; // estimated tokens
  after: number;
}

export interface CompactorOptions {
  provider: Provider;
  decisionModel: string;
  settings: CompactionSettings;
  // The breaker's one `error` event.
  emit: (event: AgentEvent) => void;
}

// One per session: the estimator, the trigger and the breaker.
export class Compactor {
  readonly estimator = new TokenEstimator();
  readonly #o: CompactorOptions;
  #failures = 0; // consecutive failed summaries
  #warnedUnknownWindow = false;

  constructor(o: CompactorOptions) {
    this.#o = o;
  }

  // Tripped: automatic compaction (trigger, overflow) is off for the
  // session; a manual /compact still runs and re-arms it.
  get disabled(): boolean {
    return this.#failures >= MAX_CONSECUTIVE_FAILURES;
  }

  // An unknown window (contextLength 0) never triggers: a limit of 0
  // would summarise on every turn. Warned once per session.
  due(ctx: Context, contextLength: number): boolean {
    if (this.disabled) return false;
    if (!(contextLength > 0)) {
      if (!this.#warnedUnknownWindow) {
        log.warn(
          "the model's contextLength is unknown; automatic compaction is skipped",
        );
        this.#warnedUnknownWindow = true;
      }
      return false;
    }
    const limit = this.#o.settings.triggerRatio * contextLength;
    return this.estimator.estimateTokens(ctx) >= limit;
  }

  // Undefined when there is nothing to summarise. Rejects when the
  // summary fails; an abort (the signal) does not count as a failure.
  async compact(
    ctx: Context,
    contextLength: number,
    o: { instructions?: string; signal?: AbortSignal } = {},
  ): Promise<Compacted | undefined> {
    const { estimator } = this;
    const keep = keepTokens(this.#o.settings, contextLength);
    const tokensOf = (m: Message): number => estimator.messageTokens(m);
    const cut = cutPoint(ctx.messages, keep, tokensOf);
    if (cut === 0) return undefined;
    const before = estimator.estimateTokens(ctx);
    const previous = previousSummary(ctx.messages[0]);
    const head = ctx.messages.slice(previous === undefined ? 0 : 1, cut);
    let summary: string;
    try {
      const answer = await decide(this.#o.provider, {
        name: "compaction_summary",
        system: SUMMARY_SYSTEM,
        input: summaryInput(head, previous, o.instructions),
        schema: Summary,
        model: this.#o.decisionModel,
        timeoutMs: SUMMARY_TIMEOUT_MS,
        signal: o.signal,
      });
      summary = renderSummary(answer);
    } catch (err) {
      if (!o.signal?.aborted) this.#failed(err as Error);
      throw err;
    }
    this.#failures = 0;
    const after = estimator.estimateTokens({
      system: ctx.system,
      messages: [summaryMessage(summary), ...ctx.messages.slice(cut)],
    });
    return { cut, summary, before, after };
  }

  reset(): void {
    this.#failures = 0;
    this.estimator.reset();
  }

  #failed(err: Error): void {
    this.#failures++;
    const n = this.#failures;
    log.warn(`compaction summary failed (${n}): ${err.message}`);
    if (n !== MAX_CONSECUTIVE_FAILURES) return;
    this.#o.emit({
      type: "error",
      message:
        `compaction failed ${n} times in a row (${err.message}); ` +
        "automatic compaction is off for this session (/compact retries)",
    });
  }
}

// The overflow retry (spec §5, §13) without touching the loop: a
// provider whose stream(), when the request overflows the window
// (an `error` with code context_length_exceeded, or a `length` stop
// before any output), calls `recover` (compact the request's context
// in place) and, if it compacted, sends the request once more. A
// second overflow, or no recovery, is an `error` event.
export function retryOnOverflow(
  provider: Provider,
  recover: () => Promise<boolean>,
): Provider {
  return {
    modelInfo: (id) => provider.modelInfo(id),
    complete: (req, signal) => provider.complete(req, signal),
    completeText: (req, signal) => provider.completeText(req, signal),
    async *stream(req, signal) {
      for (let attempt = 1; ; attempt++) {
        let first = true;
        let overflow: StreamEvent | undefined;
        for await (const event of provider.stream(req, signal)) {
          if (first && isOverflow(event)) {
            overflow = event;
            break;
          }
          first = false;
          yield event;
        }
        if (!overflow) return;
        if (attempt === 1 && (await recover())) continue;
        yield signal.aborted ? ABORTED : overflowError(overflow);
        return;
      }
    },
  };
}

export function isOverflow(event: StreamEvent): boolean {
  if (event.type === "error") return event.code === CONTEXT_LENGTH_EXCEEDED;
  if (event.type !== "done") return false;
  const { stopReason, content, toolCalls } = event.message;
  return stopReason === "length" && !content && toolCalls.length === 0;
}

const ABORTED: StreamEvent = {
  type: "done",
  message: {
    role: "assistant",
    content: "",
    toolCalls: [],
    stopReason: "aborted",
  },
};

function overflowError(event: StreamEvent): StreamEvent {
  if (event.type === "error") return event;
  return {
    type: "error",
    code: CONTEXT_LENGTH_EXCEEDED,
    message: "the request exceeds the model's context length",
  };
}
