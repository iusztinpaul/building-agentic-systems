// Token estimation (spec §13): chars / 4, corrected by the ratio the
// last request showed (its usage.inputTokens over what we estimated
// for it). Good enough for an 80 % trigger; a tokenizer dependency
// buys nothing here.

import type { Context, Message } from "../types.ts";

export const CHARS_PER_TOKEN = 4;
// The correction never moves the estimate more than 2× either way:
// the tools array and a provider's tokenizer are both in the usage.
const MIN_RATIO = 0.5;
const MAX_RATIO = 2;

// What the provider reads of a message: its text, a tool call's name
// and JSON arguments, a tool result's name and content.
export function messageChars(m: Message): number {
  if (m.role === "user") return m.content.length;
  if (m.role === "tool") return m.name.length + m.content.length;
  let chars = m.content.length;
  for (const call of m.toolCalls) {
    chars += call.name.length + JSON.stringify(call.arguments).length;
  }
  return chars;
}

export function contextChars(ctx: Context): number {
  let chars = ctx.system.length;
  for (const m of ctx.messages) chars += messageChars(m);
  return chars;
}

export class TokenEstimator {
  #ratio = 1;
  // Messages are final once pushed: each is measured once, so the
  // status bar can ask on every streamed delta.
  readonly #chars = new WeakMap<Message, number>();

  get ratio(): number {
    return this.#ratio;
  }

  // `sent` is the context the provider answered with `inputTokens`.
  observe(sent: Context, inputTokens: number): void {
    const estimate = this.#contextChars(sent) / CHARS_PER_TOKEN;
    if (estimate <= 0 || !(inputTokens > 0)) return;
    const ratio = inputTokens / estimate;
    this.#ratio = Math.min(MAX_RATIO, Math.max(MIN_RATIO, ratio));
  }

  estimateTokens(ctx: Context): number {
    return this.#tokens(this.#contextChars(ctx));
  }

  messageTokens(m: Message): number {
    return this.#tokens(this.#measure(m));
  }

  reset(): void {
    this.#ratio = 1;
  }

  #contextChars(ctx: Context): number {
    let chars = ctx.system.length;
    for (const m of ctx.messages) chars += this.#measure(m);
    return chars;
  }

  #measure(m: Message): number {
    let chars = this.#chars.get(m);
    if (chars === undefined) {
      chars = messageChars(m);
      this.#chars.set(m, chars);
    }
    return chars;
  }

  #tokens(chars: number): number {
    return Math.ceil((chars / CHARS_PER_TOKEN) * this.#ratio);
  }
}
