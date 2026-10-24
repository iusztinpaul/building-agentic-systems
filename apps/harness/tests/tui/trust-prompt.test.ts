import { stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { describe, expect, it } from "vitest";
import { TrustPrompt } from "../../src/tui/trust-prompt.ts";

function prompt() {
  const answers: boolean[] = [];
  const p = new TrustPrompt("/work/repo", (trust) => answers.push(trust));
  return { p, answers };
}

describe("TrustPrompt", () => {
  it("asks about the cwd", () => {
    const text = prompt().p.render(80).map(stripTerminalSequences).join("\n");

    expect(text).toContain("Trust this directory? /work/repo");
    expect(text).toContain("(y/n)");
  });

  it.each([
    ["y", true],
    ["Y", true],
    ["n", false],
    ["N", false],
    ["\x1b", false],
    ["\x03", false],
  ])("answers %j with %s, once", (key, trust) => {
    const { p, answers } = prompt();

    p.handleInput(key);
    p.handleInput("y");

    expect(answers).toEqual([trust]);
  });

  it("ignores other keys", () => {
    const { p, answers } = prompt();

    p.handleInput("x");
    p.handleInput("\r");

    expect(answers).toEqual([]);
  });

  it("fits narrow terminals", () => {
    for (const line of prompt().p.render(20)) expect(visibleWidth(line)).toBeLessThanOrEqual(20);
  });
});
