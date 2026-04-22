import { describe, expect, test } from "bun:test";
import type { SessionSummary } from "../../../src/session/resume";
import { applySlashCommand, parseSlashCommand } from "../../../src/ui/slash";

describe("parseSlashCommand", () => {
  test("non-slash input returns null", () => {
    expect(parseSlashCommand("hello")).toBeNull();
  });

  test("/help with no args", () => {
    expect(parseSlashCommand("/help")).toEqual({ name: "help", rest: "" });
  });

  test("/clear with trailing tokens", () => {
    expect(parseSlashCommand("/clear some extra")).toEqual({
      name: "clear",
      rest: "some extra",
    });
  });

  test("leading whitespace after the slash is trimmed", () => {
    // rest preserves anything after the first space — trailing whitespace is kept
    // as-is because slash commands don't take free-form args in v1.
    const parsed = parseSlashCommand("/  help more");
    expect(parsed?.name).toBe("help");
    expect(parsed?.rest).toBe("more");
  });

  test("bare slash returns empty name", () => {
    expect(parseSlashCommand("/")).toEqual({ name: "", rest: "" });
  });
});

describe("applySlashCommand", () => {
  test("non-slash returns null", () => {
    const result = applySlashCommand("hello world", {
      cwd: "/tmp",
      clearHistory: () => {},
    });
    expect(result).toBeNull();
  });

  test("/help returns the command listing", () => {
    const result = applySlashCommand("/help", {
      cwd: "/tmp",
      clearHistory: () => {},
    });
    expect(result).not.toBeNull();
    expect(result?.info).toContain("/help");
    expect(result?.info).toContain("/clear");
    expect(result?.info).toContain("/resume");
  });

  test("/clear invokes clearHistory and reports back", () => {
    let called = 0;
    const result = applySlashCommand("/clear", {
      cwd: "/tmp",
      clearHistory: () => {
        called += 1;
      },
    });
    expect(called).toBe(1);
    expect(result?.info).toContain("cleared");
  });

  test("/resume with no prior sessions reports empty", () => {
    const result = applySlashCommand("/resume", {
      cwd: "/tmp",
      clearHistory: () => {},
      listSessions: () => [],
    });
    expect(result?.info).toContain("no sessions");
  });

  test("/resume renders session ids and first prompts", () => {
    const fakeSessions: SessionSummary[] = [
      {
        id: "abcdef12-3456-789a-bcde-f0123456789a",
        path: "/tmp/x.jsonl",
        cwd: "/tmp",
        firstPrompt: "hello world",
        startedAt: "2026-04-21T12:34:56.000Z",
        mtime: Date.now(),
      },
    ];
    const result = applySlashCommand("/resume", {
      cwd: "/tmp",
      clearHistory: () => {},
      listSessions: () => fakeSessions,
    });
    expect(result?.info).toContain("abcdef12");
    expect(result?.info).toContain("hello world");
  });

  test("unknown command reports a hint", () => {
    const result = applySlashCommand("/nonsense", {
      cwd: "/tmp",
      clearHistory: () => {},
    });
    expect(result?.info).toContain("unknown");
    expect(result?.info).toContain("/help");
  });
});
