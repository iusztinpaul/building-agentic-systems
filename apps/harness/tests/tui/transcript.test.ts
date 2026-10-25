import { stripTerminalSequences } from "@earendil-works/pi-tui";
import { describe, expect, it } from "vitest";
import { Transcript } from "../../src/tui/transcript.ts";
import type { AgentEvent, AssistantMessage, Message } from "../../src/types.ts";

const screen = (t: Transcript, width = 80) =>
  t
    .render(width)
    .map((l) => stripTerminalSequences(l).trim())
    .filter(Boolean);

const assistant = (content: string, extra: Partial<AssistantMessage> = {}): AssistantMessage => ({
  role: "assistant",
  content,
  toolCalls: [],
  stopReason: "stop",
  ...extra,
});

function play(t: Transcript, events: AgentEvent[]): void {
  for (const e of events) t.apply(e);
}

describe("Transcript", () => {
  it("streams the assistant's text as it arrives", () => {
    const t = new Transcript("/repo");
    t.addUser("hi");
    play(t, [{ type: "turn_start" }, { type: "text_delta", text: "Hel" }]);
    expect(screen(t)).toEqual(["› hi", "Hel"]);

    play(t, [{ type: "text_delta", text: "lo" }]);
    expect(screen(t)).toEqual(["› hi", "Hello"]);
  });

  it("renders code blocks and bullets from markdown", () => {
    const t = new Transcript("/repo");
    const text = "Steps:\n\n- one\n- two\n\n```ts\nconst x = 1;\n```";
    play(t, [{ type: "turn_start" }, { type: "turn_end", message: assistant(text) }]);

    expect(screen(t)).toEqual(["Steps:", "- one", "- two", "```ts", "const x = 1;", "```"]);
    const raw = t.render(80).map((l) => l.trimEnd());
    expect(raw).toContain(" \x1b[36m- \x1b[39mone"); // a styled bullet
    expect(raw).toContain(" \x1b[2m```ts\x1b[22m"); // a dimmed fence
    expect(raw).toContain("   const x = 1;"); // indented code
  });

  it("keeps the partial text of an aborted turn and says so", () => {
    const t = new Transcript("/repo");
    play(t, [
      { type: "turn_start" },
      { type: "text_delta", text: "partial" },
      { type: "turn_end", message: assistant("partial", { stopReason: "aborted" }) },
      { type: "agent_end", reason: "aborted" },
    ]);

    expect(screen(t)).toEqual(["partial", "(aborted)"]);
  });

  it("nests llm_call rows under the running codemode card only", () => {
    const t = new Transcript("/repo");
    const cm = {
      id: "c1",
      name: "codemode",
      arguments: { script: "return 1", description: "map" },
    };
    const llm = (model: string): AgentEvent => ({
      type: "llm_call",
      depth: 1,
      model,
      usage: { inputTokens: 10, outputTokens: 2 },
    });
    play(t, [
      llm("before/any-card"), // no card: dropped
      { type: "tool_call", call: cm },
      llm("a/m"),
      llm("b/m"),
      { type: "tool_result", callId: "c1", content: "1", isError: false },
      llm("after/result"), // the card is done: dropped
    ]);
    t.toggleToolResults();

    expect(screen(t)).toEqual([
      "✓ codemode map",
      "return 1",
      "llm depth 1 · a/m · 10 in / 2 out",
      "llm depth 1 · b/m · 10 in / 2 out",
      "1",
    ]);
  });

  it("puts a card per tool call and fills in its result", () => {
    const t = new Transcript("/repo");
    const call = { id: "c1", name: "read", arguments: { path: "/repo/a.md" } };
    play(t, [
      { type: "turn_start" },
      { type: "turn_end", message: assistant("", { toolCalls: [call], stopReason: "tool_calls" }) },
      { type: "tool_call", call },
    ]);
    expect(screen(t)).toEqual(["… read a.md"]);

    play(t, [{ type: "tool_result", callId: "c1", content: "one\ntwo", isError: false }]);
    expect(screen(t)).toEqual(["✓ read a.md", "one (+1 lines, Tab)"]);

    t.toggleToolResults();
    expect(screen(t)).toEqual(["✓ read a.md", "one", "two"]);
  });

  it("shows a permission's reason and skips read-only allows (nothing was decided)", () => {
    const t = new Transcript("/repo");
    const call = { id: "c1", name: "edit", arguments: { path: "~/.zshrc" } };
    play(t, [
      { type: "permission", call, decision: "allow", source: "readOnly" },
      {
        type: "permission",
        call,
        decision: "deny",
        source: "user",
        reason: "denied by the user",
      },
    ]);

    expect(screen(t)).toEqual(["edit: deny (user) — denied by the user"]);
  });

  it("shows permission, activation, compaction and error events inline", () => {
    const t = new Transcript("/repo");
    const call = { id: "c1", name: "bash", arguments: { command: "rm x" } };
    play(t, [
      { type: "permission", call, decision: "deny", source: "user" },
      { type: "tool_activation", names: ["mcp__a__b"] },
      { type: "compaction", before: 900, after: 100 },
      { type: "error", message: "boom" },
      { type: "agent_end", reason: "max_turns" },
    ]);

    expect(screen(t)).toEqual([
      "bash: deny (user)",
      "activated: mcp__a__b",
      "compacted: 900 → 100 tokens",
      "boom",
      "run ended: max_turns",
    ]);
  });

  it("marks a steer and a follow-up typed during a run", () => {
    const t = new Transcript("/repo");
    t.addUser("skip the tests", "steer");
    t.addUser("summarise the diff", "follow-up");

    expect(screen(t)).toEqual(["› skip the tests (steer)", "› summarise the diff (follow-up)"]);
  });

  it("draws a resumed session's messages the way they streamed", () => {
    const call = { id: "c1", name: "bash", arguments: { command: "ls" } };
    const messages: Message[] = [
      { role: "user", content: "list files" },
      assistant("Listing.", { toolCalls: [call], stopReason: "tool_calls" }),
      { role: "tool", toolCallId: "c1", name: "bash", content: "a.md", isError: false },
      assistant("One file.", { stopReason: "aborted" }),
    ];
    const t = new Transcript("/repo");

    t.load(messages);

    expect(screen(t)).toEqual([
      "› list files",
      "Listing.",
      "✓ bash ls",
      "a.md",
      "One file.",
      "(aborted)",
    ]);
  });

  it("strips escape sequences from the model's text", () => {
    const t = new Transcript("/repo");
    play(t, [{ type: "turn_start" }, { type: "text_delta", text: "\x1b[2Jhi\x07" }]);
    t.addUser("\x1b]0;title\x07me");

    const raw = t.render(80).join("\n");
    expect(raw).not.toContain("\x1b[2J");
    expect(raw).not.toContain("\x1b]0;");
    expect(screen(t)).toEqual(["hi", "› me"]);
  });

  it("starts over on clear()", () => {
    const t = new Transcript("/repo");
    t.addUser("old");

    t.clear();

    expect(screen(t)).toEqual([]);
  });
});
