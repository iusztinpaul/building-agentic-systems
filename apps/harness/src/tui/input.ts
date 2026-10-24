// The prompt editor (pi-tui's Editor: multiline with Shift+Enter,
// history on Up/Down) and the routing of what the user submits:
// idle → prompt; during a run Enter → steer, Alt+Enter → follow-up
// (spec §6); "/name args" → a slash command. Esc aborts the run.

import {
  CombinedAutocompleteProvider,
  type Component,
  Editor,
  type Focusable,
  matchesKey,
  type SlashCommand,
  type TUI,
} from "@earendil-works/pi-tui";
import { editorTheme } from "./style.ts";

export type InputAction =
  | { kind: "prompt" | "steer" | "followUp"; text: string }
  | { kind: "command"; name: string; arg: string };

// "/model x" is a command; "/usr/bin/env is odd" is a prompt.
const COMMAND = /^\/([a-z][a-z:-]*)(?:\s+([\s\S]*))?$/;

export function route(
  text: string,
  running: boolean,
  alt: boolean,
): InputAction | undefined {
  const trimmed = text.trim();
  if (!trimmed) return undefined;
  const command = COMMAND.exec(trimmed);
  if (command) {
    const [, name = "", arg = ""] = command;
    return { kind: "command", name, arg: arg.trim() };
  }
  if (!running) return { kind: "prompt", text: trimmed };
  return { kind: alt ? "followUp" : "steer", text: trimmed };
}

export interface InputHandlers {
  submit(text: string, alt: boolean): void;
  escape(): void; // abort the run
  tab(): void; // with an empty editor: expand tool results
  interrupt(): void; // Ctrl+C
}

export class PromptInput implements Component, Focusable {
  readonly editor: Editor;
  readonly #on: InputHandlers;
  #focused = false;

  constructor(
    tui: TUI,
    on: InputHandlers,
    commands: SlashCommand[],
    cwd: string,
  ) {
    this.#on = on;
    this.editor = new Editor(tui, editorTheme);
    this.editor.setAutocompleteProvider(
      new CombinedAutocompleteProvider(commands, cwd),
    );
    this.editor.onSubmit = (text) => this.#submit(text, false);
  }

  get focused(): boolean {
    return this.#focused;
  }

  set focused(value: boolean) {
    this.#focused = value;
    this.editor.focused = value;
  }

  handleInput(data: string): void {
    const menu = this.editor.isShowingAutocomplete();
    if (matchesKey(data, "alt+enter")) {
      // The Editor would insert a newline: Alt+Enter is ours.
      const text = this.editor.getExpandedText();
      this.editor.setText("");
      this.#submit(text, true);
    } else if (matchesKey(data, "escape") && !menu) {
      this.#on.escape();
    } else if (matchesKey(data, "ctrl+c")) {
      this.#on.interrupt();
    } else if (matchesKey(data, "tab") && !this.editor.getText()) {
      this.#on.tab();
    } else {
      this.editor.handleInput(data);
    }
  }

  render(width: number): string[] {
    return this.editor.render(width);
  }

  invalidate(): void {
    this.editor.invalidate();
  }

  #submit(text: string, alt: boolean): void {
    const trimmed = text.trim();
    if (!trimmed) return;
    this.editor.addToHistory(trimmed);
    this.#on.submit(trimmed, alt);
  }
}
