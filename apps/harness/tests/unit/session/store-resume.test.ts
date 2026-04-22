import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { loadSession } from "../../../src/session/resume";
import { SessionStore } from "../../../src/session/store";
import { mkTmpDir } from "../../helpers/tmp-fs";

// End-to-end: write messages + events through SessionStore, then parse them
// back with loadSession. This is the handshake the REPL does on --resume.

function makeStore(cwd: string, sessionId = "sess-1") {
  const path = join(cwd, `${sessionId}.jsonl`);
  return { store: new SessionStore(path, sessionId, cwd, true), path };
}

describe("SessionStore → loadSession roundtrip", () => {
  test("user + assistant text messages roundtrip intact", () => {
    const cwd = mkTmpDir();
    const { store, path } = makeStore(cwd);

    store.appendMessage({ role: "user", content: "hello" });
    store.appendMessage({
      role: "assistant",
      content: [{ type: "text", text: "hi there" }],
    });

    const { messages, sessionId } = loadSession(path);
    expect(sessionId).toBe("sess-1");
    expect(messages).toHaveLength(2);
    expect(messages[0]).toEqual({ role: "user", content: "hello" });
    expect(messages[1]).toEqual({
      role: "assistant",
      content: [{ type: "text", text: "hi there" }],
    });
  });

  test("tool_use + tool_result blocks roundtrip intact", () => {
    const cwd = mkTmpDir();
    const { store, path } = makeStore(cwd);

    store.appendMessage({ role: "user", content: "do the thing" });
    store.appendMessage({
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "call_1",
          name: "glob",
          input: { pattern: "**/*.ts" },
        },
      ],
    });
    store.appendMessage({
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "call_1",
          tool_name: "glob",
          content: "a.ts\nb.ts",
        },
      ],
    });

    const { messages } = loadSession(path);
    expect(messages).toHaveLength(3);
    const assistant = messages[1];
    expect(assistant?.role).toBe("assistant");
    expect(Array.isArray(assistant?.content)).toBe(true);
    const assistantContent = assistant?.content as unknown[];
    expect(assistantContent[0]).toMatchObject({
      type: "tool_use",
      name: "glob",
      input: { pattern: "**/*.ts" },
    });
    const toolResult = messages[2];
    expect(toolResult?.role).toBe("user");
    const toolResultContent = toolResult?.content as unknown[];
    expect(toolResultContent[0]).toMatchObject({
      type: "tool_result",
      tool_use_id: "call_1",
      tool_name: "glob",
    });
  });

  test("appendEvent writes a non-message line that loadSession ignores", () => {
    const cwd = mkTmpDir();
    const { store, path } = makeStore(cwd);
    store.appendMessage({ role: "user", content: "hi" });
    store.appendEvent("permission", { decision: "allow" });

    const raw = readFileSync(path, "utf-8");
    expect(raw).toContain('"kind":"event"');
    expect(raw).toContain('"name":"permission"');

    const { messages } = loadSession(path);
    expect(messages).toHaveLength(1);
    expect(messages[0]?.content).toBe("hi");
  });

  test("resumed store writes a second meta + a 'resumed' event", () => {
    const cwd = mkTmpDir();
    const { store, path } = makeStore(cwd);
    store.appendMessage({ role: "user", content: "first" });

    // Reopen with fresh=false to simulate --resume / --continue.
    new SessionStore(path, "sess-1", cwd, false);

    const raw = readFileSync(path, "utf-8");
    const metaLines = raw.split("\n").filter((l) => l.includes('"kind":"meta"'));
    expect(metaLines.length).toBe(2);
    expect(raw).toContain('"name":"resumed"');
  });
});
