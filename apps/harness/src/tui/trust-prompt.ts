// The first-run question for an untrusted cwd (spec §16): "y" lets
// the caller write it to trusted.json and load the project's
// AGENTS.md and .tree-agent/ files; "n" (or Esc) keeps this run to
// user-level config. Asked before the session exists.

import {
  type Component,
  matchesKey,
  wrapTextWithAnsi,
} from "@earendil-works/pi-tui";
import { bold, dim } from "./style.ts";

export class TrustPrompt implements Component {
  readonly #cwd: string;
  readonly #answer: (trust: boolean) => void;
  #answered = false;

  constructor(cwd: string, answer: (trust: boolean) => void) {
    this.#cwd = cwd;
    this.#answer = answer;
  }

  handleInput(data: string): void {
    if (this.#answered) return;
    const key = data.toLowerCase();
    const no =
      key === "n" ||
      matchesKey(data, "escape") ||
      matchesKey(data, "ctrl+c");
    if (key !== "y" && !no) return;
    this.#answered = true;
    this.#answer(key === "y");
  }

  render(width: number): string[] {
    const w = Math.max(1, width - 2);
    return [
      "",
      ...wrapTextWithAnsi(bold(`Trust this directory? ${this.#cwd}`), w),
      ...wrapTextWithAnsi(
        dim(
          "y: load its AGENTS.md and .tree-agent/ settings and " +
            "remember it · n: user-level config only",
        ),
        w,
      ),
      "(y/n)",
    ].map((line) => ` ${line}`);
  }

  invalidate(): void {}
}
