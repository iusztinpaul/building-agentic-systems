import { stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { describe, expect, it } from "vitest";
import { primaryArgument, ToolCard } from "../../src/tui/tool-card.ts";
import type { ToolCall } from "../../src/types.ts";

const call = (name: string, args: Record<string, unknown>): ToolCall => ({
  id: "c1",
  name,
  arguments: args,
});
const plain = (lines: string[]) => lines.map((l) => stripTerminalSequences(l));

describe("primaryArgument", () => {
  it("shows a path relative to the cwd, whether the model sent it absolute or relative", () => {
    expect(primaryArgument(call("read", { path: "/repo/src/a.ts" }), "/repo")).toBe("src/a.ts");
    expect(primaryArgument(call("read", { path: "src/a.ts" }), "/repo")).toBe("src/a.ts");
    expect(primaryArgument(call("read", { path: "/repo" }), "/repo")).toBe(".");
  });

  it("keeps a path outside the cwd as the model wrote it", () => {
    expect(primaryArgument(call("read", { path: "/etc/hosts" }), "/repo")).toBe("/etc/hosts");
  });

  it("shows the first line of a bash command, else the first string argument", () => {
    expect(primaryArgument(call("bash", { command: "ls\npwd" }), "/r")).toBe("ls");
    expect(primaryArgument(call("x", { n: 1, q: "find me" }), "/r")).toBe("find me");
    expect(primaryArgument(call("x", { n: 1 }), "/r")).toBe('{"n":1}');
  });
});

describe("ToolCard", () => {
  it("renders name and relative path, then a collapsed result that expand() opens", () => {
    const card = new ToolCard(call("read", { path: "/repo/notes.md" }), "/repo");
    expect(plain(card.render(80))).toEqual([" … read notes.md"]);

    card.setResult("line 1\nline 2\nline 3", false);
    expect(plain(card.render(80))).toEqual([" ✓ read notes.md", "    line 1 (+2 lines, Tab)"]);

    card.expanded = true;
    expect(plain(card.render(80))).toEqual([
      " ✓ read notes.md",
      "    line 1",
      "    line 2",
      "    line 3",
    ]);
  });

  it("a codemode card shows the description, the script collapsed under it, then the result", () => {
    const card = new ToolCard(
      call("codemode", {
        script: "const a = await tools.read({ path: 'x' });\nreturn a.lines;",
        description: "read x and count its lines",
      }),
      "/r",
    );
    expect(plain(card.render(80))).toEqual([
      " … codemode read x and count its lines",
      "    const a = await tools.read({ path: 'x' }); (+1 lines, Tab)",
    ]);

    card.setResult("12", false);
    card.expanded = true;
    expect(plain(card.render(80))).toEqual([
      " ✓ codemode read x and count its lines",
      "    const a = await tools.read({ path: 'x' });",
      "    return a.lines;",
      "    12",
    ]);
  });

  it("marks an error result", () => {
    const card = new ToolCard(call("bash", { command: "false" }), "/r");
    card.setResult("Command exited with code 1", true);

    expect(plain(card.render(80))[0]).toBe(" ✗ bash false");
  });

  it("never renders a line wider than the terminal", () => {
    const card = new ToolCard(call("bash", { command: "x".repeat(300) }), "/r");
    card.setResult(`${"y".repeat(300)}\nmore`, false);

    for (const width of [10, 40, 80]) {
      for (const expanded of [false, true]) {
        card.expanded = expanded;
        for (const line of card.render(width))
          expect(visibleWidth(line)).toBeLessThanOrEqual(width);
      }
    }
  });

  it("strips escape sequences and control characters from tool output", () => {
    const card = new ToolCard(call("bash", { command: "x" }), "/r");
    card.setResult("\x1b[2J\x1b[31mred\x1b[0m\tend\x07\r\nnext", false);
    card.expanded = true;

    const raw = card.render(80).join("\n");
    expect(raw).not.toContain("\x1b[2J");
    expect(raw).not.toContain("\x07");
    expect(plain(card.render(80)).slice(1)).toEqual(["    red  end", "    next"]);
    const hostile = new ToolCard(call("bash", { command: "\x1b[2Jls" }), "/r");
    expect(hostile.render(80).join("")).not.toContain("\x1b[2J");
  });
});
