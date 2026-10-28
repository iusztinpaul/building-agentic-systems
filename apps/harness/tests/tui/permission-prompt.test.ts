import { stripTerminalSequences, TuiMainScreen } from "@earendil-works/pi-tui";
import { describe, expect, it } from "vitest";
import {
  MAX_SUMMARY_LINES,
  type PermissionAnswer,
  PermissionPrompt,
  tuiPrompter,
} from "../../src/tui/permission-prompt.ts";
import type { ToolCall } from "../../src/types.ts";
import { FakeTerminal, settle } from "../helpers/terminal.ts";

const write: ToolCall = { id: "w", name: "write", arguments: { path: "/r/src/a.ts" } };

function prompt(summary = "src/a.ts", reason?: string) {
  const answers: PermissionAnswer[] = [];
  const p = new PermissionPrompt(write, summary, reason, (a) => answers.push(a));
  const text = () => p.render(80).map(stripTerminalSequences).join("\n");
  return { p, answers, text };
}

describe("PermissionPrompt", () => {
  it("shows the tool, its primary argument and the keys", () => {
    const text = prompt().text();

    expect(text).toContain("Allow write?");
    expect(text).toContain("  src/a.ts");
    expect(text).toContain("y once · s this session · n deny");
    expect(text).not.toContain("classifier:");
  });

  it("says when the call comes from a codemode script", () => {
    const fromScript: ToolCall = { ...write, origin: "codemode" };
    const p = new PermissionPrompt(fromScript, "src/a.ts", undefined, () => {});

    expect(p.render(80).map(stripTerminalSequences).join("\n")).toContain(
      "Allow write (in a codemode script)?",
    );
  });

  it("shows the classifier's reason when there is one", () => {
    expect(prompt("~/.zshrc", "edits a shell rc file").text()).toContain(
      "classifier: edits a shell rc file",
    );
  });

  it("shows the first lines of a long summary and how many more there are", () => {
    const lines = Array.from({ length: MAX_SUMMARY_LINES + 5 }, (_, i) => `line ${i}`);

    const text = prompt(lines.join("\n")).text();

    expect(text).toContain(`line ${MAX_SUMMARY_LINES - 1}`);
    expect(text).not.toContain(`line ${MAX_SUMMARY_LINES}\n`);
    expect(text).toContain("… (+5 lines)");
  });

  it("strips terminal control sequences from the summary", () => {
    expect(prompt("rm x\x1b[2J\x07").text()).toContain("rm x");
    expect(prompt("rm x\x1b[2J").p.render(80).join("")).not.toContain("\x1b[2J");
  });

  it.each([
    ["y", "once"],
    ["Y", "once"],
    ["s", "session"],
    ["n", "deny"],
    ["\x1b", "deny"],
    ["\x03", "deny"],
  ] as const)("answers %j with %s, once", (key, answer) => {
    const { p, answers } = prompt();

    p.handleInput(key);
    p.handleInput("y");

    expect(answers).toEqual([answer]);
  });

  it("ignores other keys", () => {
    const { p, answers } = prompt();

    p.handleInput("x");
    p.handleInput("\r");

    expect(answers).toEqual([]);
  });
});

describe("tuiPrompter", () => {
  it("mounts the prompt with the focus, then removes it and gives the focus back", async () => {
    const terminal = new FakeTerminal();
    const tui = new TuiMainScreen(terminal);
    tui.start();
    const before = new PermissionPrompt(write, "", undefined, () => {});
    tui.addChild(before);
    tui.setFocus(before);

    const answer = tuiPrompter(tui).ask(write, "src/a.ts", "why");
    await settle();
    const screen = tui.render(80).map(stripTerminalSequences).join("\n");
    terminal.send("s");

    expect(screen).toContain("Allow write?");
    expect(screen).toContain("classifier: why");
    expect(await answer).toBe("session");
    expect(tui.getFocusedComponent()).toBe(before);
    expect(tui.children).toEqual([before]);
    tui.stop();
  });
});
