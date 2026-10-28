// runLoop (spec §6): stream a turn, run its tool calls in order,
// repeat while the model calls tools or steering is queued; then
// re-enter for queued follow-ups. Continuation is driven by
// `toolCalls.length`, never by finish_reason (unreliable upstream).
// Every product concern attaches through LoopHooks.

import type { ToolResult } from "../tools/registry.ts";
import type {
  AgentEndReason,
  AssistantMessage,
  Context,
  Message,
  ToolCall,
} from "../types.ts";
import type { LoopConfig } from "./hooks.ts";

export const CUT_OFF = "Output was cut off. Continue.";
export const TRUNCATED =
  "Not run: arguments truncated (the output hit the length limit). " +
  "Call the tool again with complete arguments.";

export async function runLoop(
  ctx: Context,
  cfg: LoopConfig,
): Promise<Context> {
  const { hooks } = cfg;
  let pending: Message[] = hooks.getSteeringMessages?.() ?? [];
  for (;;) {
    let cutOff = false; // one "Continue." per run of length stops
    for (let turn = 1; ; turn++) {
      if (turn > cfg.maxTurns) {
        ctx.messages.push(...pending); // keep undelivered steering
        return end(ctx, cfg, "max_turns");
      }
      if (hooks.prepareTurn) ctx = await hooks.prepareTurn(ctx, turn);
      ctx.messages.push(...pending);
      pending = [];
      hooks.emit({ type: "turn_start" });
      const msg = await streamTurn(ctx, cfg);
      ctx.messages.push(msg);
      hooks.emit({ type: "turn_end", message: msg });
      if (msg.stopReason === "error" || msg.stopReason === "aborted") {
        return end(ctx, cfg, msg.stopReason);
      }
      const calls = msg.toolCalls;
      if (msg.stopReason === "length" && calls.length === 0) {
        if (cutOff) {
          hooks.emit({ type: "error", message: "output cut off twice" });
          return end(ctx, cfg, "error");
        }
        cutOff = true;
        pending = [{ role: "user", content: CUT_OFF }];
        continue;
      }
      cutOff = false;
      for (const call of calls) await runCall(call, msg, ctx, cfg);
      if (cfg.signal.aborted) return end(ctx, cfg, stopped(cfg));
      pending = hooks.getSteeringMessages?.() ?? [];
      if (calls.length === 0 && pending.length === 0) break;
    }
    pending = hooks.getFollowUpMessages?.() ?? [];
    if (pending.length === 0) return end(ctx, cfg, "completed");
  }
}

// One model call. Text deltas are emitted as they arrive; the `done`
// message carries the assembled tool calls.
async function streamTurn(
  ctx: Context,
  cfg: LoopConfig,
): Promise<AssistantMessage> {
  const request = {
    model: cfg.model,
    context: ctx,
    tools: cfg.tools.schemasForRequest(),
  };
  let text = "";
  try {
    for await (const ev of cfg.provider.stream(request, cfg.signal)) {
      if (ev.type === "text_delta") {
        text += ev.text;
        cfg.hooks.emit(ev);
      } else if (ev.type === "error") {
        return failed(cfg, text, ev.message);
      } else if (ev.type === "done") {
        if (ev.message.stopReason !== "error") return ev.message;
        return failed(
          cfg,
          ev.message.content,
          "the model stopped with an error",
        );
      }
    }
    return failed(cfg, text, "the stream ended without a result");
  } catch (err) {
    return failed(cfg, text, (err as Error).message);
  }
}

function failed(
  cfg: LoopConfig,
  text: string,
  message: string,
): AssistantMessage {
  cfg.hooks.emit({ type: "error", message });
  return {
    role: "assistant",
    content: text,
    toolCalls: [],
    stopReason: "error",
  };
}

// Sequential, in call order. Every call gets a tool message, even
// when it does not run, so the next request stays valid on the wire.
async function runCall(
  call: ToolCall,
  msg: AssistantMessage,
  ctx: Context,
  cfg: LoopConfig,
): Promise<void> {
  const { hooks } = cfg;
  hooks.emit({ type: "tool_call", call });
  let result = await execute(call, msg, cfg);
  if (hooks.afterToolCall) result = await hooks.afterToolCall(call, result);
  const { content } = result;
  const isError = result.isError ?? false;
  ctx.messages.push({
    role: "tool",
    toolCallId: call.id,
    name: call.name,
    content,
    isError,
  });
  hooks.emit({ type: "tool_result", callId: call.id, content, isError });
}

async function execute(
  call: ToolCall,
  msg: AssistantMessage,
  cfg: LoopConfig,
): Promise<ToolResult> {
  if (cfg.signal.aborted) return refuse("Not run: aborted.");
  // A length stop may have cut the arguments: never run them.
  if (msg.stopReason === "length") return refuse(TRUNCATED);
  if (call.parseError) return refuse(call.parseError);
  const gate = (await cfg.hooks.beforeToolCall?.(call)) ?? { run: true };
  // An abort while the gate waited (classifier, prompt) beats its
  // answer; a BLOCKED abort is the gate's own deny.
  if (stopped(cfg) === "aborted" && cfg.signal.aborted) {
    return refuse("Not run: aborted.");
  }
  if (!gate.run) return refuse(`Blocked: ${gate.reason}`);
  return cfg.tools.execute(call, {
    cwd: cfg.cwd,
    signal: cfg.signal,
    origin: "model",
    depth: cfg.depth ?? 0,
  });
}

function refuse(content: string): ToolResult {
  return { content, isError: true };
}

// A hook that aborts with reason BLOCKED (the headless fail-stop)
// ends the run `blocked`; any other abort is `aborted`.
export const BLOCKED = "blocked";
function stopped(cfg: LoopConfig): AgentEndReason {
  return cfg.signal.reason === BLOCKED ? "blocked" : "aborted";
}

function end(
  ctx: Context,
  cfg: LoopConfig,
  reason: AgentEndReason,
): Context {
  cfg.hooks.emit({ type: "agent_end", reason });
  return ctx;
}
