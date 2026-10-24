// The permission dialog (spec §15): the tool, its primary argument
// (the summary the gate built) and, in auto, the classifier's reason.
// Keys: y once, s for the rest of the session, n (or Esc, Ctrl+C)
// deny. PermissionPrompter mounts one per question and puts the focus
// back where it was.

import {
  type Component,
  matchesKey,
  type TUI,
  wrapTextWithAnsi,
} from "@earendil-works/pi-tui";
import type { PermissionPrompter, ToolCall } from "../types.ts";
import { bold, dim, sanitize, yellow } from "./style.ts";

export type PermissionAnswer = "once" | "session" | "deny";

// The summary can be a 2 KB script: show its head.
export const MAX_SUMMARY_LINES = 12;

const KEYS: Record<string, PermissionAnswer> = {
  y: "once",
  s: "session",
  n: "deny",
};

export class PermissionPrompt implements Component {
  readonly #call: ToolCall;
  readonly #summary: string;
  readonly #reason: string | undefined;
  readonly #answer: (answer: PermissionAnswer) => void;
  #answered = false;

  constructor(
    call: ToolCall,
    summary: string,
    reason: string | undefined,
    answer: (answer: PermissionAnswer) => void,
  ) {
    this.#call = call;
    this.#summary = summary;
    this.#reason = reason;
    this.#answer = answer;
  }

  handleInput(data: string): void {
    if (this.#answered) return;
    const cancel = matchesKey(data, "escape") || matchesKey(data, "ctrl+c");
    const answer = cancel ? "deny" : KEYS[data.toLowerCase()];
    if (!answer) return;
    this.#answered = true;
    this.#answer(answer);
  }

  render(width: number): string[] {
    const w = Math.max(1, width - 4);
    const all = sanitize(this.#summary).replace(/\t/g, "  ").split("\n");
    const shown = all.slice(0, MAX_SUMMARY_LINES);
    if (all.length > shown.length) {
      shown.push(`… (+${all.length - shown.length} lines)`);
    }
    const lines = [
      "",
      ...wrapTextWithAnsi(bold(`Allow ${this.#call.name}?`), w),
      ...shown.flatMap((l) => wrapTextWithAnsi(`  ${l}`, w)),
    ];
    if (this.#reason) {
      const reason = `classifier: ${sanitize(this.#reason)}`;
      lines.push(...wrapTextWithAnsi(yellow(reason), w));
    }
    lines.push(dim("y once · s this session · n deny"));
    return lines.map((line) => ` ${line}`);
  }

  invalidate(): void {}
}

// TUI's interface lacks the getter its implementations have.
export type FocusTui = TUI & { getFocusedComponent(): Component | null };

// Asks on the TUI's screen; one question at a time is the loop's
// order (tool calls run sequentially).
export function tuiPrompter(tui: FocusTui): PermissionPrompter {
  return {
    ask: (call, summary, reason) =>
      new Promise((resolve) => {
        const back = tui.getFocusedComponent();
        const prompt = new PermissionPrompt(
          call,
          summary,
          reason,
          (answer) => {
            tui.removeChild(prompt);
            tui.setFocus(back);
            tui.requestRender();
            resolve(answer);
          },
        );
        tui.addChild(prompt);
        tui.setFocus(prompt);
        tui.requestRender();
      }),
  };
}
