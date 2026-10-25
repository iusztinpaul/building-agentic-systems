import { describe, expect, it } from "vitest";
import { jsonSink, textSink } from "../../src/headless/print.ts";
import type { AgentEvent, AssistantMessage } from "../../src/types.ts";

function sinks() {
  const out: string[] = [];
  const err: string[] = [];
  const text = textSink(
    { write: (t: string) => out.push(t) },
    { write: (t: string) => err.push(t) },
  );
  return { out, err, text };
}

const answer = (content: string, toolCalls: AssistantMessage["toolCalls"] = []): AgentEvent => ({
  type: "turn_end",
  message: { role: "assistant", content, toolCalls, stopReason: "stop" },
});

describe("textSink", () => {
  it("prints the text of each turn without tool calls to stdout", () => {
    const s = sinks();

    s.text({ type: "text_delta", text: "fo" });
    s.text(answer("four"));
    s.text(answer("six\n"));

    expect(s.out.join("")).toBe("four\nsix\n");
    expect(s.err).toEqual([]);
  });

  it("sends narration next to tool calls and tool activity to stderr", () => {
    const s = sinks();
    const call = { id: "c1", name: "write", arguments: { path: "notes.md", content: "x" } };

    s.text(answer("Let me write it.", [call]));
    s.text({ type: "tool_call", call });
    s.text({ type: "tool_result", callId: "c1", content: "Wrote 1 bytes", isError: false });
    s.text({
      type: "tool_result",
      callId: "c2",
      content: "oldText matched 0 times\nmore",
      isError: true,
    });

    expect(s.out).toEqual([]);
    expect(s.err).toEqual([
      "Let me write it.\n",
      '> write {"path":"notes.md","content":"x"}\n',
      "! oldText matched 0 times\n",
    ]);
  });

  it("names activated tools on stderr", () => {
    const s = sinks();

    s.text({ type: "tool_activation", names: ["mcp__m__a", "mcp__m__b"] });

    expect(s.out).toEqual([]);
    expect(s.err).toEqual(["activated: mcp__m__a, mcp__m__b\n"]);
  });

  it("prints errors and a non-completed end to stderr", () => {
    const s = sinks();

    s.text({ type: "error", message: "User not found." });
    s.text({ type: "agent_end", reason: "error" });
    s.text({ type: "agent_end", reason: "max_turns" });
    s.text({ type: "agent_end", reason: "completed" });

    expect(s.err).toEqual(["User not found.\n", "run ended: max_turns\n"]);
  });
});

describe("jsonSink", () => {
  it("writes one JSON line per event", () => {
    const out: string[] = [];
    const sink = jsonSink({ write: (t: string) => out.push(t) });

    sink({ type: "turn_start" });
    sink({ type: "text_delta", text: 'a "quoted"\nline' });
    sink({ type: "llm_call", depth: 1, model: "m", usage: { inputTokens: 3, outputTokens: 1 } });

    expect(out).toEqual([
      '{"type":"turn_start"}\n',
      '{"type":"text_delta","text":"a \\"quoted\\"\\nline"}\n',
      '{"type":"llm_call","depth":1,"model":"m","usage":{"inputTokens":3,"outputTokens":1}}\n',
    ]);
  });
});
