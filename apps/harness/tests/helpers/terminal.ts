// A Terminal for driving the TUI without a TTY: keys go in through
// send(), and the screen is read back from the TUI's components.

import type { Terminal } from "@earendil-works/pi-tui";

export class FakeTerminal implements Terminal {
  readonly written: string[] = [];
  started = false;
  stopped = false;
  #onInput: ((data: string) => void) | undefined;

  start(onInput: (data: string) => void): void {
    this.started = true;
    this.#onInput = onInput;
  }

  stop(): void {
    this.stopped = true;
  }

  // Each call is one key (or one paste) as the terminal sends it.
  send(...keys: string[]): void {
    if (!this.#onInput) throw new Error("terminal not started");
    for (const key of keys) this.#onInput(key);
  }

  // Types `text` one character at a time.
  type(text: string): void {
    this.send(...text);
  }

  async drainInput(): Promise<void> {}
  write(data: string): void {
    this.written.push(data);
  }
  get columns(): number {
    return 100;
  }
  get rows(): number {
    return 40;
  }
  get kittyProtocolActive(): boolean {
    return false;
  }
  moveBy(): void {}
  hideCursor(): void {}
  showCursor(): void {}
  clearLine(): void {}
  clearFromCursor(): void {}
  clearScreen(): void {}
  setTitle(): void {}
  setProgress(): void {}
  setProgramStatus(): void {}
}

// Lets pending promise callbacks and nextTick renders run.
export async function settle(ms = 0): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}
