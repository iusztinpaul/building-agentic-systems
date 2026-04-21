// The agent loop — an async generator that drives a multi-turn tool-use conversation.
// Consumers (CLI in M2, Ink TUI at M3) receive typed events as they happen and own
// all rendering. The loop owns: stream consumption, tool dispatch, message bookkeeping,
// and termination. Distilled from Claude Code's src/query.ts.

import { randomUUID } from "node:crypto";
import type { GoogleGenAI } from "@google/genai";
import { streamText } from "../client";
import type { Message, ToolResultBlock, ToolUseBlock } from "../messages";
import type { AnyTool } from "../tools/registry";
import type { ToolContext } from "../tools/types";

export type LoopEvent =
  | { type: "assistant_text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: Record<string, unknown> }
  | { type: "tool_result"; id: string; name: string; content: string; isError?: boolean }
  // `messages` is the full accumulated history at turn end — interactive consumers
  // (Ink REPL) reuse it as the starting point for the next user turn.
  | { type: "done"; reason: "end_turn" | "max_iterations"; messages: Message[] }
  | { type: "error"; message: string };

export interface LoopOptions {
  client: GoogleGenAI;
  messages: Message[];
  systemPrompt: string;
  tools: AnyTool[];
  toolContext: ToolContext;
  maxIterations?: number;
  model?: string;
}

export async function* loop(opts: LoopOptions): AsyncGenerator<LoopEvent> {
  const { client, systemPrompt, tools, toolContext, model } = opts;
  const messages: Message[] = [...opts.messages];
  const registry = new Map(tools.map((t) => [t.name, t]));
  const maxIterations = opts.maxIterations ?? 10;

  for (let iter = 0; iter < maxIterations; iter++) {
    const pending: Array<{ id: string; name: string; args: Record<string, unknown> }> = [];
    const assistantText: string[] = [];

    try {
      for await (const ev of streamText(client, {
        messages,
        systemInstruction: systemPrompt,
        tools,
        model,
      })) {
        if (ev.type === "text_delta") {
          assistantText.push(ev.text);
          yield { type: "assistant_text", text: ev.text };
        } else if (ev.type === "function_call") {
          const id = `call_${randomUUID().slice(0, 8)}`;
          pending.push({ id, name: ev.name, args: ev.args });
          yield { type: "tool_use", id, name: ev.name, input: ev.args };
        }
        // "done" from streamText is per-turn; the loop decides whether to iterate.
      }
    } catch (err) {
      yield { type: "error", message: err instanceof Error ? err.message : String(err) };
      return;
    }

    if (pending.length === 0) {
      yield { type: "done", reason: "end_turn", messages };
      return;
    }

    // Persist the assistant turn (text + function calls) so the next streamText call
    // sees the full history, not just the original user prompt.
    const assistantBlocks: Array<{ type: "text"; text: string } | ToolUseBlock> = [];
    const joined = assistantText.join("");
    if (joined) assistantBlocks.push({ type: "text", text: joined });
    for (const c of pending) {
      assistantBlocks.push({ type: "tool_use", id: c.id, name: c.name, input: c.args });
    }
    messages.push({ role: "assistant", content: assistantBlocks });

    // Execute each tool and collect results.
    const results: ToolResultBlock[] = [];
    for (const call of pending) {
      const tool = registry.get(call.name);
      if (!tool) {
        const res = {
          type: "tool_result" as const,
          tool_use_id: call.id,
          tool_name: call.name,
          content: `Unknown tool: ${call.name}`,
          is_error: true,
        };
        results.push(res);
        yield {
          type: "tool_result",
          id: call.id,
          name: call.name,
          content: res.content,
          isError: true,
        };
        continue;
      }
      try {
        const parsed = tool.schema.parse(call.args);
        const out = await tool.call(parsed, toolContext);
        const res = {
          type: "tool_result" as const,
          tool_use_id: call.id,
          tool_name: call.name,
          content: out.content,
          is_error: out.isError,
        };
        results.push(res);
        yield {
          type: "tool_result",
          id: call.id,
          name: call.name,
          content: out.content,
          isError: out.isError,
        };
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        const res = {
          type: "tool_result" as const,
          tool_use_id: call.id,
          tool_name: call.name,
          content: msg,
          is_error: true,
        };
        results.push(res);
        yield { type: "tool_result", id: call.id, name: call.name, content: msg, isError: true };
      }
    }

    // Gemini expects tool results under role "user" with functionResponse parts
    // (client.ts handles the translation at the boundary).
    messages.push({ role: "user", content: results });
  }

  yield { type: "done", reason: "max_iterations", messages };
}
