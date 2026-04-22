import { describe, expect, test } from "bun:test";
import type { HookConfig } from "../../../src/hooks/config";
import { matchHooks, runHook, runMatchingHooks } from "../../../src/hooks/runner";

describe("matchHooks", () => {
  test("returns all hooks for UserPromptSubmit regardless of matcher", () => {
    const cfg: HookConfig = {
      UserPromptSubmit: [{ command: "echo one" }, { matcher: "anything", command: "echo two" }],
    };
    expect(matchHooks("UserPromptSubmit", cfg)).toHaveLength(2);
  });

  test("returns all hooks for Stop regardless of matcher", () => {
    const cfg: HookConfig = { Stop: [{ command: "echo stop" }] };
    expect(matchHooks("Stop", cfg)).toHaveLength(1);
  });

  test("filters PreToolUse by matcher prefix", () => {
    const cfg: HookConfig = {
      PreToolUse: [
        { matcher: "bash:rm ", command: "echo block-rm" },
        { matcher: "bash:git ", command: "echo block-git" },
        { matcher: "edit", command: "echo block-edit" },
      ],
    };
    const fires = matchHooks("PreToolUse", cfg, "bash", { command: "rm -rf /tmp/x" });
    expect(fires.map((f) => f.command)).toEqual(["echo block-rm"]);
  });

  test("PreToolUse with no tool name matches nothing", () => {
    const cfg: HookConfig = { PreToolUse: [{ command: "echo never" }] };
    expect(matchHooks("PreToolUse", cfg)).toHaveLength(0);
  });

  test("missing event returns []", () => {
    expect(matchHooks("PreToolUse", {}, "bash", {})).toEqual([]);
  });
});

describe("runHook", () => {
  test("parses stdout JSON and surfaces exit 0", async () => {
    const result = await runHook(
      { command: `echo '{"decision":"block","reason":"nope"}'` },
      { event: "PreToolUse" },
    );
    expect(result.exitCode).toBe(0);
    expect(result.parsed).toEqual({ decision: "block", reason: "nope" });
  });

  test("non-zero exit is surfaced", async () => {
    const result = await runHook({ command: "exit 1" }, { event: "PreToolUse" });
    expect(result.exitCode).toBe(1);
    expect(result.parsed).toBeUndefined();
  });

  test("non-JSON stdout is treated as observation", async () => {
    const result = await runHook({ command: "echo hello" }, { event: "PostToolUse" });
    expect(result.exitCode).toBe(0);
    expect(result.parsed).toBeUndefined();
    expect(result.raw.trim()).toBe("hello");
  });
});

describe("runMatchingHooks", () => {
  test("returns blocked=true when first hook exits non-zero", async () => {
    const cfg: HookConfig = {
      PreToolUse: [
        { matcher: "bash", command: "exit 1" },
        { matcher: "bash", command: "echo never" },
      ],
    };
    const result = await runMatchingHooks(
      "PreToolUse",
      cfg,
      { tool: "bash", input: {} },
      { toolName: "bash", input: {} },
    );
    expect(result.blocked).toBe(true);
    expect(result.fires).toHaveLength(1); // short-circuits
  });

  test("returns blocked=true with parsed reason when stdout says block", async () => {
    const cfg: HookConfig = {
      PreToolUse: [
        {
          matcher: "bash:rm ",
          command: `echo '{"decision":"block","reason":"rm is disabled"}'`,
        },
      ],
    };
    const result = await runMatchingHooks(
      "PreToolUse",
      cfg,
      { tool: "bash", input: { command: "rm foo" } },
      { toolName: "bash", input: { command: "rm foo" } },
    );
    expect(result.blocked).toBe(true);
    expect(result.reason).toBe("rm is disabled");
  });

  test("captures modifiedPrompt on UserPromptSubmit", async () => {
    const cfg: HookConfig = {
      UserPromptSubmit: [{ command: `echo '{"prompt":"rewritten"}'` }],
    };
    const result = await runMatchingHooks("UserPromptSubmit", cfg, { prompt: "original" });
    expect(result.blocked).toBe(false);
    expect(result.modifiedPrompt).toBe("rewritten");
  });

  test("observation hooks do not block or modify", async () => {
    const cfg: HookConfig = {
      PostToolUse: [{ matcher: "bash", command: "echo observed" }],
    };
    const result = await runMatchingHooks(
      "PostToolUse",
      cfg,
      { tool: "bash", input: {}, result: { content: "", isError: false } },
      { toolName: "bash", input: {} },
    );
    expect(result.blocked).toBe(false);
    expect(result.modifiedPrompt).toBeUndefined();
    expect(result.fires).toHaveLength(1);
  });
});
