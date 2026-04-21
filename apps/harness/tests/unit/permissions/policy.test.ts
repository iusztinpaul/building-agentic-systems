import { describe, expect, test } from "bun:test";
import {
  type Rule,
  evaluateRules,
  matchesRule,
  suggestPattern,
} from "../../../src/permissions/policy";

describe("matchesRule", () => {
  test.each<[string, Rule, string, Record<string, unknown>, boolean]>([
    ["bare tool name, any input", { pattern: "bash", decision: "allow" }, "bash", {}, true],
    ["bare tool name, wrong tool", { pattern: "bash", decision: "allow" }, "read", {}, false],
    [
      "bash prefix matches command",
      { pattern: "bash:git ", decision: "allow" },
      "bash",
      { command: "git status" },
      true,
    ],
    [
      "bash prefix mismatch",
      { pattern: "bash:git ", decision: "allow" },
      "bash",
      { command: "npm install" },
      false,
    ],
    [
      "file_path prefix matches path",
      { pattern: "edit:/repo/src/", decision: "allow" },
      "edit",
      { file_path: "/repo/src/foo.ts" },
      true,
    ],
    [
      "file_path prefix mismatch",
      { pattern: "edit:/repo/src/", decision: "allow" },
      "edit",
      { file_path: "/other/path.ts" },
      false,
    ],
    [
      "tool name with prefix but missing input field",
      { pattern: "bash:git ", decision: "allow" },
      "bash",
      {},
      false,
    ],
  ])("%s", (_label, rule, tool, input, expected) => {
    expect(matchesRule(rule, tool, input)).toBe(expected);
  });
});

describe("evaluateRules", () => {
  test("first matching rule wins (allow before deny)", () => {
    const rules: Rule[] = [
      { pattern: "bash:git ", decision: "allow" },
      { pattern: "bash", decision: "deny" },
    ];
    expect(evaluateRules("bash", { command: "git status" }, rules)).toBe("allow");
  });

  test("first matching rule wins (deny before allow)", () => {
    const rules: Rule[] = [
      { pattern: "bash", decision: "deny" },
      { pattern: "bash:git ", decision: "allow" },
    ];
    expect(evaluateRules("bash", { command: "git status" }, rules)).toBe("deny");
  });

  test("no match falls through to ask", () => {
    const rules: Rule[] = [{ pattern: "edit", decision: "allow" }];
    expect(evaluateRules("bash", {}, rules)).toBe("ask");
  });

  test("empty rule list falls through to ask", () => {
    expect(evaluateRules("bash", {}, [])).toBe("ask");
  });
});

describe("suggestPattern", () => {
  test("bash pulls out the first token of command", () => {
    expect(suggestPattern("bash", { command: "git status --short" })).toBe("bash:git ");
  });

  test("bash with empty command falls back to bare tool name", () => {
    expect(suggestPattern("bash", { command: "" })).toBe("bash");
  });

  test("edit uses parent directory of file_path", () => {
    expect(suggestPattern("edit", { file_path: "/repo/src/foo.ts" })).toBe("edit:/repo/src/");
  });

  test("write with filename-only input falls back to bare name", () => {
    expect(suggestPattern("write", { file_path: "README.md" })).toBe("write");
  });

  test("tool without recognized input falls back to bare name", () => {
    expect(suggestPattern("todo", { action: "list" })).toBe("todo");
  });
});
