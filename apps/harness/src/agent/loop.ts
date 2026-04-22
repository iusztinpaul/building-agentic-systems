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

export type PermissionCheck = (
  toolName: string,
  input: Record<string, unknown>,
  tool: AnyTool,
) => Promise<"allow" | "deny">;

export interface BeforeToolDecision {
  block: boolean;
  reason?: string;
}

export type BeforeToolHook = (
  toolName: string,
  input: Record<string, unknown>,
) => Promise<BeforeToolDecision>;

export type AfterToolHook = (
  toolName: string,
  input: Record<string, unknown>,
  result: { content: string; isError?: boolean },
) => Promise<void>;

export interface LoopOptions {
  client: GoogleGenAI;
  messages: Message[];
  systemPrompt: string;
  tools: AnyTool[];
  toolContext: ToolContext;
  maxIterations?: number;
  model?: string;
  // Called before any destructive tool is executed. Return "allow" or "deny".
  // If absent, destructive tools run freely (M2 behavior).
  permission?: PermissionCheck;
  // Called whenever the loop appends a message to its history (assistant turns + tool
  // results). Callers use this to persist sessions without waiting for `done`.
  onMessage?: (message: Message) => void;
  // M7 hooks — both optional. onBeforeTool runs before execution and can block;
  // onAfterTool runs after for observation only. The loop calls them for every tool,
  // including read-only ones (since PreToolUse/PostToolUse in Claude Code fire for
  // every tool). Consumers wire these to the shell-exec hook runner.
  onBeforeTool?: BeforeToolHook;
  onAfterTool?: AfterToolHook;
}

const PERMISSION_DENIED = "Permission denied by user.";

export async function* loop(opts: LoopOptions): AsyncGenerator<LoopEvent> {
  const {
    client,
    systemPrompt,
    tools,
    toolContext,
    model,
    permission,
    onMessage,
    onBeforeTool,
    onAfterTool,
  } = opts;
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

    // Persist the assistant turn (text + function calls) so both history and
    // the session log contain every assistant response, including the final
    // text-only turn that ends the conversation.
    const assistantBlocks: Array<{ type: "text"; text: string } | ToolUseBlock> = [];
    const joined = assistantText.join("");
    if (joined) assistantBlocks.push({ type: "text", text: joined });
    for (const c of pending) {
      assistantBlocks.push({ type: "tool_use", id: c.id, name: c.name, input: c.args });
    }
    if (assistantBlocks.length > 0) {
      const assistantMessage: Message = { role: "assistant", content: assistantBlocks };
      messages.push(assistantMessage);
      onMessage?.(assistantMessage);
    }

    if (pending.length === 0) {
      yield { type: "done", reason: "end_turn", messages };
      return;
    }

    // Execute each tool and collect results.
    const results: ToolResultBlock[] = [];
    for (const call of pending) {
      const tool = registry.get(call.name);
      if (!tool) {
        const res: ToolResultBlock = {
          type: "tool_result",
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

      // PreToolUse hook — fires for every tool (including read-only). A blocking
      // hook short-circuits before the permission gate.
      if (onBeforeTool) {
        const hookDecision = await onBeforeTool(call.name, call.args);
        if (hookDecision.block) {
          const reason = hookDecision.reason ?? "Blocked by PreToolUse hook.";
          const res: ToolResultBlock = {
            type: "tool_result",
            tool_use_id: call.id,
            tool_name: call.name,
            content: reason,
            is_error: true,
          };
          results.push(res);
          yield {
            type: "tool_result",
            id: call.id,
            name: call.name,
            content: reason,
            isError: true,
          };
          continue;
        }
      }

      // Permission gate — only applied to destructive tools. Read-only tools run freely.
      if (tool.isDestructive && permission) {
        const decision = await permission(call.name, call.args, tool);
        if (decision === "deny") {
          const res: ToolResultBlock = {
            type: "tool_result",
            tool_use_id: call.id,
            tool_name: call.name,
            content: PERMISSION_DENIED,
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
      }

      try {
        const parsed = tool.schema.parse(call.args);
        const out = await tool.call(parsed, toolContext);
        const res: ToolResultBlock = {
          type: "tool_result",
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
        // PostToolUse — fires after a successful call. Observation only; we don't
        // re-yield anything from its output.
        if (onAfterTool) {
          try {
            await onAfterTool(call.name, call.args, { content: out.content, isError: out.isError });
          } catch {
            // hooks shouldn't break the loop
          }
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        const res: ToolResultBlock = {
          type: "tool_result",
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
    const toolResultMessage: Message = { role: "user", content: results };
    messages.push(toolResultMessage);
    onMessage?.(toolResultMessage);
  }

  yield { type: "done", reason: "max_iterations", messages };
}
