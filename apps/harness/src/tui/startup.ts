// The screen while the session opens (spec §15): the lines startup
// prints — an MCP login's `log in at <url>`, its timeout and skip
// warnings — under a "starting…" line. Focused, so Ctrl+C reaches
// it: it quits before the session exists (Esc does not), and
// open()'s pending MCP logins and handshakes end with it.

import {
  type Component,
  matchesKey,
  wrapTextWithAnsi,
} from "@earendil-works/pi-tui";
import { dim, red, sanitize } from "./style.ts";

export interface Notice {
  text: string;
  error: boolean; // a warning: red
}

export class StartupScreen implements Component {
  readonly lines: Notice[] = [];
  readonly #quit: () => void;
  #quitting = false;

  constructor(quit: () => void) {
    this.#quit = quit;
  }

  add(text: string, error = false): void {
    this.lines.push({ text: sanitize(text), error });
  }

  handleInput(data: string): void {
    if (this.#quitting) return;
    if (!matchesKey(data, "ctrl+c")) return;
    this.#quitting = true;
    this.#quit();
  }

  render(width: number): string[] {
    const w = Math.max(1, width - 2);
    const status = this.#quitting
      ? "quitting…"
      : "starting… (Ctrl+C quits)";
    return [
      "",
      ...this.lines.flatMap(({ text, error }) =>
        wrapTextWithAnsi(error ? red(text) : text, w),
      ),
      dim(status),
    ].map((line) => ` ${line}`);
  }

  invalidate(): void {}
}
