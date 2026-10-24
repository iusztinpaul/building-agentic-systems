import { TuiMainScreen } from "@earendil-works/pi-tui";
import { describe, expect, it } from "vitest";
import { type InputHandlers, PromptInput, route } from "../../src/tui/input.ts";
import { FakeTerminal } from "../helpers/terminal.ts";

describe("route", () => {
  it.each([
    { text: "fix it", running: false, alt: false, want: { kind: "prompt", text: "fix it" } },
    { text: "fix it", running: false, alt: true, want: { kind: "prompt", text: "fix it" } },
    { text: "use pnpm", running: true, alt: false, want: { kind: "steer", text: "use pnpm" } },
    { text: "then test", running: true, alt: true, want: { kind: "followUp", text: "then test" } },
    { text: "  padded  ", running: false, alt: false, want: { kind: "prompt", text: "padded" } },
    {
      text: "/model a/b",
      running: false,
      alt: false,
      want: { kind: "command", name: "model", arg: "a/b" },
    },
    { text: "/help", running: true, alt: false, want: { kind: "command", name: "help", arg: "" } },
    {
      text: "/skill:x go",
      running: false,
      alt: false,
      want: { kind: "command", name: "skill:x", arg: "go" },
    },
    {
      text: "/usr/bin/env is odd",
      running: false,
      alt: false,
      want: { kind: "prompt", text: "/usr/bin/env is odd" },
    },
  ])("$text (running $running, alt $alt) → $want.kind", ({ text, running, alt, want }) => {
    expect(route(text, running, alt)).toEqual(want);
  });

  it("ignores blank input", () => {
    expect(route("   \n ", false, false)).toBeUndefined();
  });
});

function setup() {
  const tui = new TuiMainScreen(new FakeTerminal());
  const calls: string[] = [];
  const on: InputHandlers = {
    submit: (text, alt) => calls.push(`${alt ? "alt" : "enter"}:${text}`),
    escape: () => calls.push("escape"),
    tab: () => calls.push("tab"),
    interrupt: () => calls.push("interrupt"),
  };
  const input = new PromptInput(tui, on, [{ name: "help" }], "/tmp");
  const type = (text: string) => {
    for (const ch of text) input.handleInput(ch);
  };
  return { input, calls, type };
}

describe("PromptInput", () => {
  it("submits on Enter and clears the editor", () => {
    const { input, calls, type } = setup();
    type("hello");

    input.handleInput("\r");

    expect(calls).toEqual(["enter:hello"]);
    expect(input.editor.getText()).toBe("");
  });

  it("inserts a newline on Shift+Enter and submits both lines", () => {
    const { input, calls, type } = setup();
    type("line 1");
    input.handleInput("\x1b[13;2u"); // Shift+Enter (kitty)
    type("line 2");

    input.handleInput("\r");

    expect(calls).toEqual(["enter:line 1\nline 2"]);
  });

  it("submits on Alt+Enter as a follow-up instead of inserting a newline", () => {
    const { input, calls, type } = setup();
    type("later");

    input.handleInput("\x1b\r"); // Alt+Enter (legacy)

    expect(calls).toEqual(["alt:later"]);
    expect(input.editor.getText()).toBe("");
  });

  it("does not submit empty input", () => {
    const { input, calls } = setup();

    input.handleInput("\r");
    input.handleInput("\x1b\r");

    expect(calls).toEqual([]);
  });

  it("sends Esc to escape(), Ctrl+C to interrupt() and Tab on an empty editor to tab()", () => {
    const { input, calls } = setup();

    input.handleInput("\x1b");
    input.handleInput("\x03");
    input.handleInput("\t");

    expect(calls).toEqual(["escape", "interrupt", "tab"]);
  });

  it("leaves Tab to the editor when there is text", () => {
    const { input, calls, type } = setup();
    type("abc");

    input.handleInput("\t");

    expect(calls).toEqual([]);
  });

  it("recalls earlier submissions with Up", () => {
    const { input, type } = setup();
    type("first");
    input.handleInput("\r");

    input.handleInput("\x1b[A"); // Up

    expect(input.editor.getText()).toBe("first");
  });
});
