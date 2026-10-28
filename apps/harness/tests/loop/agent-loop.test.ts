import { describe, expect, it } from "vitest";
import { z } from "zod";
import { BLOCKED, CUT_OFF, runLoop, TRUNCATED } from "../../src/loop/agent-loop.ts";
import type { LoopHooks } from "../../src/loop/hooks.ts";
import { FakeProvider, fakeDone } from "../../src/provider/fake.ts";
import type { StreamEvent } from "../../src/provider/types.ts";
import { ToolRegistry } from "../../src/tools/registry.ts";
import type { AgentEvent, Message, StopReason, ToolCall } from "../../src/types.ts";

const call = (id: string, text = id): ToolCall => ({ id, name: "echo", arguments: { text } });

function done(content: string, toolCalls: ToolCall[], stopReason: StopReason): StreamEvent {
  return { type: "done", message: { role: "assistant", content, toolCalls, stopReason } };
}

function setup(streams: StreamEvent[][], hooks: Partial<LoopHooks> = {}, maxTurns = 10) {
  const fake = new FakeProvider({ streams });
  const ran: string[] = [];
  const tools = new ToolRegistry();
  tools.register({
    name: "echo",
    description: "Echo text",
    promptSnippet: "echo text",
    parameters: z.object({ text: z.string() }),
    exposure: "direct",
    isReadOnly: true,
    async execute({ text }) {
      ran.push(text);
      return { content: `echo: ${text}`, structured: { text } };
    },
  });
  const events: AgentEvent[] = [];
  const controller = new AbortController();
  const cfg = {
    provider: fake,
    model: "test/model",
    tools,
    maxTurns,
    signal: controller.signal,
    cwd: process.cwd(),
    hooks: { emit: (e: AgentEvent) => events.push(e), ...hooks },
  };
  const ctx = { system: "sys", messages: [{ role: "user", content: "go" }] as Message[] };
  return { fake, ran, events, controller, cfg, ctx, run: () => runLoop(ctx, cfg) };
}

const ends = (events: AgentEvent[]) => events.filter((e) => e.type === "agent_end");
const lastMessages = (fake: FakeProvider, i: number) => fake.requests[i]?.context.messages ?? [];

describe("runLoop", () => {
  it("continues on a tool call even when finish_reason says stop", async () => {
    const t = setup([[done("", [call("c1")], "stop")], [fakeDone("all done")]]);

    const ctx = await t.run();

    expect(t.ran).toEqual(["c1"]);
    expect(t.fake.requests).toHaveLength(2);
    expect(lastMessages(t.fake, 1).at(-1)).toEqual({
      role: "tool",
      toolCallId: "c1",
      name: "echo",
      content: "echo: c1",
      isError: false,
    });
    expect(ctx.messages.at(-1)).toMatchObject({ role: "assistant", content: "all done" });
    expect(ends(t.events)).toEqual([{ type: "agent_end", reason: "completed" }]);
  });

  it("ends after one turn when the model calls no tool", async () => {
    const t = setup([[fakeDone("hi")]]);

    await t.run();

    expect(t.fake.requests).toHaveLength(1);
    expect(t.events.map((e) => e.type)).toEqual(["turn_start", "turn_end", "agent_end"]);
  });

  it("sends the registry's schemas on every request", async () => {
    const t = setup([[done("", [call("c1")], "tool_calls")], [fakeDone("ok")]]);

    await t.run();

    for (const req of t.fake.requests) {
      expect(req.tools.map((s) => s.name)).toEqual(["echo"]);
    }
  });

  it("fails every call with 'arguments truncated' on a length stop and runs none", async () => {
    const t = setup([[done("", [call("c1"), call("c2")], "length")], [fakeDone("retried")]]);

    await t.run();

    expect(t.ran).toEqual([]);
    const tools = lastMessages(t.fake, 1).filter((m) => m.role === "tool");
    expect(tools).toEqual([
      { role: "tool", toolCallId: "c1", name: "echo", content: TRUNCATED, isError: true },
      { role: "tool", toolCallId: "c2", name: "echo", content: TRUNCATED, isError: true },
    ]);
    expect(TRUNCATED).toContain("arguments truncated");
    expect(ends(t.events)).toEqual([{ type: "agent_end", reason: "completed" }]);
  });

  it("appends 'Output was cut off. Continue.' once, and a second length ends with error", async () => {
    const t = setup([[done("part one", [], "length")], [done("part two", [], "length")]]);

    await t.run();

    expect(t.fake.requests).toHaveLength(2);
    expect(lastMessages(t.fake, 1).at(-1)).toEqual({ role: "user", content: CUT_OFF });
    expect(CUT_OFF).toBe("Output was cut off. Continue.");
    expect(t.events.filter((e) => e.type === "error")).toHaveLength(1);
    expect(ends(t.events)).toEqual([{ type: "agent_end", reason: "error" }]);
  });

  it("allows a new 'Continue.' after a length stop is followed by a normal turn", async () => {
    const t = setup([
      [done("a", [], "length")],
      [done("", [call("c1")], "tool_calls")],
      [done("b", [], "length")],
      [fakeDone("end")],
    ]);

    await t.run();

    expect(t.fake.requests).toHaveLength(4);
    expect(lastMessages(t.fake, 3).at(-1)).toEqual({ role: "user", content: CUT_OFF });
    expect(ends(t.events)).toEqual([{ type: "agent_end", reason: "completed" }]);
  });

  it("answers a blocked call with 'Blocked: <reason>' as an error and keeps going", async () => {
    const t = setup([[done("", [call("c1")], "tool_calls")], [fakeDone("ok, another way")]], {
      beforeToolCall: async () => ({ run: false, reason: "not in CI" }),
    });

    await t.run();

    expect(t.ran).toEqual([]);
    expect(lastMessages(t.fake, 1).at(-1)).toEqual({
      role: "tool",
      toolCallId: "c1",
      name: "echo",
      content: "Blocked: not in CI",
      isError: true,
    });
    expect(ends(t.events)).toEqual([{ type: "agent_end", reason: "completed" }]);
  });

  it("answers a call whose arguments were invalid JSON with its parseError, without the gate", async () => {
    const gated: string[] = [];
    const bad: ToolCall = { id: "c1", name: "echo", arguments: {}, parseError: "invalid JSON" };
    const t = setup([[done("", [bad], "tool_calls")], [fakeDone("ok")]], {
      beforeToolCall: async (c) => {
        gated.push(c.id);
        return { run: true };
      },
    });

    await t.run();

    expect(t.ran).toEqual([]);
    expect(gated).toEqual([]);
    expect(lastMessages(t.fake, 1).at(-1)).toMatchObject({
      content: "invalid JSON",
      isError: true,
    });
  });

  it("passes every result through afterToolCall before it reaches the model", async () => {
    const t = setup([[done("", [call("c1")], "tool_calls")], [fakeDone("ok")]], {
      afterToolCall: async (_c, r) => ({ ...r, content: `${r.content} (checked)` }),
    });

    await t.run();

    expect(lastMessages(t.fake, 1).at(-1)).toMatchObject({ content: "echo: c1 (checked)" });
    expect(t.events).toContainEqual({
      type: "tool_result",
      callId: "c1",
      content: "echo: c1 (checked)",
      isError: false,
    });
  });

  it("runs calls sequentially in call order and emits tool_call before each result", async () => {
    const t = setup([[done("", [call("a"), call("b")], "tool_calls")], [fakeDone("ok")]]);

    await t.run();

    expect(t.ran).toEqual(["a", "b"]);
    expect(
      t.events
        .filter((e) => e.type === "tool_call" || e.type === "tool_result")
        .map((e) => (e.type === "tool_call" ? `call ${e.call.id}` : `result ${e.callId}`)),
    ).toEqual(["call a", "result a", "call b", "result b"]);
  });

  it("ends with agent_end(max_turns) after maxTurns model calls", async () => {
    const turn = () => [done("", [call("c")], "tool_calls")];
    const t = setup([turn(), turn(), turn()], {}, 2);

    await t.run();

    expect(t.fake.requests).toHaveLength(2);
    expect(ends(t.events)).toEqual([{ type: "agent_end", reason: "max_turns" }]);
  });

  it("keeps the partial text with stopReason aborted when aborted mid-stream", async () => {
    const t = setup([
      [{ type: "text_delta", text: "Hel" }, { type: "text_delta", text: "lo" }, fakeDone("Hello")],
    ]);
    t.cfg.hooks.emit = (e) => {
      t.events.push(e);
      if (e.type === "text_delta") t.controller.abort();
    };

    const ctx = await t.run();

    expect(ctx.messages.at(-1)).toEqual({
      role: "assistant",
      content: "Hel",
      toolCalls: [],
      stopReason: "aborted",
    });
    expect(ends(t.events)).toEqual([{ type: "agent_end", reason: "aborted" }]);
  });

  it("answers the remaining calls without running them when aborted during tools", async () => {
    const steering: Message[] = [];
    const t = setup([[done("", [call("a"), call("b")], "tool_calls")]], {
      getSteeringMessages: () => steering.splice(0),
    });
    t.cfg.hooks.emit = (e) => {
      t.events.push(e);
      if (e.type === "tool_result" && e.callId === "a") {
        steering.push({ role: "user", content: "later" });
        t.controller.abort();
      }
    };

    const ctx = await t.run();

    expect(t.ran).toEqual(["a"]);
    expect(ctx.messages.filter((m) => m.role === "tool").map((m) => m.content)).toEqual([
      "echo: a",
      "Not run: aborted.",
    ]);
    expect(steering).toHaveLength(1); // not drained
    expect(ends(t.events)).toEqual([{ type: "agent_end", reason: "aborted" }]);
  });

  it("ends with agent_end(blocked) when a gate aborts with BLOCKED, answering the rest unrun", async () => {
    const t = setup([[done("", [call("a"), call("b")], "tool_calls")], [fakeDone("never")]], {
      beforeToolCall: async () => {
        t.controller.abort(BLOCKED);
        return { run: false, reason: "dangerous: deletes ~/Downloads" };
      },
    });

    const ctx = await t.run();

    expect(t.ran).toEqual([]);
    expect(ctx.messages.filter((m) => m.role === "tool").map((m) => m.content)).toEqual([
      "Blocked: dangerous: deletes ~/Downloads",
      "Not run: aborted.",
    ]);
    expect(t.fake.requests).toHaveLength(1);
    expect(ends(t.events)).toEqual([{ type: "agent_end", reason: "blocked" }]);
  });

  it("answers Not run: aborted. when aborted while the gate waited, even if it then allows", async () => {
    const t = setup([[done("", [call("a"), call("b")], "tool_calls")], [fakeDone("never")]], {
      beforeToolCall: async () => {
        t.controller.abort(); // e.g. Esc while the classifier is asked
        return { run: true };
      },
    });

    const ctx = await t.run();

    expect(t.ran).toEqual([]);
    expect(ctx.messages.filter((m) => m.role === "tool").map((m) => m.content)).toEqual([
      "Not run: aborted.",
      "Not run: aborted.",
    ]);
    expect(ends(t.events)).toEqual([{ type: "agent_end", reason: "aborted" }]);
  });

  it("ends with error, emitting the message, when the stream ends with an error event", async () => {
    const t = setup([
      [
        { type: "text_delta", text: "par" },
        { type: "error", code: "server_error", message: "upstream died" },
      ],
    ]);

    const ctx = await t.run();

    expect(ctx.messages.at(-1)).toMatchObject({ content: "par", stopReason: "error" });
    expect(t.events.filter((e) => e.type === "error" || e.type === "agent_end")).toEqual([
      { type: "error", message: "upstream died" },
      { type: "agent_end", reason: "error" },
    ]);
  });

  it("delivers steering before the next model call, after the turn's tool results", async () => {
    const steering: Message[] = [];
    const t = setup([
      [{ type: "text_delta", text: "working" }, done("working", [call("c1")], "tool_calls")],
      [fakeDone("switched")],
    ]);
    t.cfg.hooks.getSteeringMessages = () => steering.splice(0);
    t.cfg.hooks.emit = (e) => {
      t.events.push(e);
      if (e.type === "text_delta") steering.push({ role: "user", content: "use pnpm, not npm" });
    };

    await t.run();

    expect(lastMessages(t.fake, 1).map((m) => m.role)).toEqual([
      "user",
      "assistant",
      "tool",
      "user",
    ]);
    expect(lastMessages(t.fake, 1).at(-1)).toEqual({ role: "user", content: "use pnpm, not npm" });
  });

  it("keeps the loop alive for steering after a turn without tool calls", async () => {
    const steering: Message[] = [];
    const t = setup([
      [{ type: "text_delta", text: "done?" }, fakeDone("done?")],
      [fakeDone("now with tests")],
    ]);
    t.cfg.hooks.getSteeringMessages = () => steering.splice(0);
    t.cfg.hooks.emit = (e) => {
      t.events.push(e);
      if (e.type === "text_delta") steering.push({ role: "user", content: "also tests" });
    };

    await t.run();

    expect(t.fake.requests).toHaveLength(2);
    expect(lastMessages(t.fake, 1).at(-1)).toEqual({ role: "user", content: "also tests" });
    expect(ends(t.events)).toHaveLength(1);
  });

  it("drains steering queued before the run into the first request", async () => {
    const steering: Message[] = [{ role: "user", content: "queued" }];
    const t = setup([[fakeDone("ok")]], { getSteeringMessages: () => steering.splice(0) });

    await t.run();

    expect(lastMessages(t.fake, 0).map((m) => m.content)).toEqual(["go", "queued"]);
  });

  it("re-enters for a follow-up only when agent_end would have fired", async () => {
    const followUps: Message[] = [{ role: "user", content: "then run the tests" }];
    const t = setup([[fakeDone("first answer")], [fakeDone("tests pass")]], {
      getFollowUpMessages: () => followUps.splice(0),
    });

    await t.run();

    expect(t.fake.requests).toHaveLength(2);
    expect(lastMessages(t.fake, 1).slice(-2)).toEqual([
      {
        role: "assistant",
        content: "first answer",
        toolCalls: [],
        stopReason: "stop",
        usage: expect.anything(),
      },
      { role: "user", content: "then run the tests" },
    ]);
    expect(t.events.map((e) => e.type)).toEqual([
      "turn_start",
      "turn_end",
      "turn_start",
      "turn_end",
      "agent_end",
    ]);
  });

  it("does not drain follow-ups while the model is still calling tools", async () => {
    const drained: number[] = [];
    const t = setup([[done("", [call("c1")], "tool_calls")], [fakeDone("ok")]], {
      getFollowUpMessages: () => {
        drained.push(t.fake.requests.length);
        return [];
      },
    });

    await t.run();

    expect(drained).toEqual([2]);
  });

  it("gives the model only the tool result's content, never `structured`", async () => {
    const t = setup([[done("", [call("c1")], "tool_calls")], [fakeDone("ok")]]);

    await t.run();

    const tool = lastMessages(t.fake, 1).at(-1);
    expect(tool).toEqual({
      role: "tool",
      toolCallId: "c1",
      name: "echo",
      content: "echo: c1",
      isError: false,
    });
    expect(tool).not.toHaveProperty("structured");
  });

  it("calls prepareTurn before every model call with the turn number", async () => {
    const turns: number[] = [];
    const t = setup([[done("", [call("c1")], "tool_calls")], [fakeDone("ok")]], {
      prepareTurn: async (ctx, turn) => {
        turns.push(turn);
        return { ...ctx, system: `sys ${turn}` };
      },
    });

    await t.run();

    expect(turns).toEqual([1, 2]);
    expect(t.fake.requests.map((r) => r.context.system)).toEqual(["sys 1", "sys 2"]);
  });
});
