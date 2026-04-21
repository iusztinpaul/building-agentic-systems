// Sub-agent registry + spawner factory. A sub-agent is a recursive call into the
// same `loop()` with a narrowed tool set and fresh conversation context. The
// single teaching point: reusing the loop is the whole point — no separate
// runtime, no separate prompt machinery.

import type { GoogleGenAI } from "@google/genai";
import type { Message } from "../messages";
import { subagentSessionPath } from "../session/paths";
import { SessionStore } from "../session/store";
import type { AnyTool } from "../tools/registry";
import type { SpawnSubagent, SpawnSubagentRequest, SpawnSubagentResult } from "../tools/types";
import { type PermissionCheck, loop } from "./loop";

export type SubagentType = "general" | "explore" | "plan";

export const MAX_DEPTH = 2;
export const MAX_TOOL_CALLS_PER_SUBAGENT = 30;
export const SUBAGENT_TIMEOUT_MS = 5 * 60 * 1000;

interface SubagentConfig {
  description: string;
  systemPromptSuffix: string;
  toolFilter: (tool: AnyTool) => boolean;
  canSpawn: boolean;
}

const REGISTRY: Record<SubagentType, SubagentConfig> = {
  general: {
    description: "Full tool access. Can recurse one level (depth ≤ 2).",
    systemPromptSuffix:
      " You are a general-purpose sub-agent. Focus only on the single task you were given. Return the final answer as plain text.",
    toolFilter: () => true,
    canSpawn: true,
  },
  explore: {
    description: "Read-only code exploration — read / glob / grep / todo and read-only MCP tools.",
    systemPromptSuffix:
      " You are a read-only exploration sub-agent. Use read, glob, grep, and todo to investigate the codebase. Do not write, edit, or execute shells. Return a concise summary.",
    toolFilter: (t) => {
      if (t.name === "task") return false;
      if (["read", "glob", "grep", "todo"].includes(t.name)) return true;
      if (t.name.startsWith("mcp__") && t.isReadOnly) return true;
      return false;
    },
    canSpawn: false,
  },
  plan: {
    description: "Design-only — read / glob / grep. Analyze and return a plan.",
    systemPromptSuffix:
      " You are a planning sub-agent. Read and analyze code only. Do not write, edit, or execute anything. Return a concise plan.",
    toolFilter: (t) => ["read", "glob", "grep"].includes(t.name),
    canSpawn: false,
  },
};

export const SUBAGENT_TYPES = Object.keys(REGISTRY) as SubagentType[];

export function describeSubagent(type: SubagentType): string {
  return REGISTRY[type].description;
}

export type SubagentProgressEvent =
  | {
      kind: "start";
      subagentId: string;
      type: SubagentType;
      description: string;
      prompt: string;
      depth: number;
    }
  | { kind: "assistant_text"; subagentId: string; type: SubagentType; text: string }
  | {
      kind: "tool_use";
      subagentId: string;
      type: SubagentType;
      id: string;
      name: string;
      input: Record<string, unknown>;
    }
  | {
      kind: "tool_result";
      subagentId: string;
      type: SubagentType;
      id: string;
      name: string;
      content: string;
      isError?: boolean;
    }
  | { kind: "end"; subagentId: string; type: SubagentType; result: SpawnSubagentResult };

export interface SpawnDeps {
  client: GoogleGenAI;
  baseSystemPrompt: string;
  allTools: AnyTool[];
  parentSessionId: string;
  cwd: string;
  permission?: PermissionCheck;
  onProgress?: (ev: SubagentProgressEvent) => void;
}

function newId(): string {
  // Short id — enough uniqueness for a single session, readable in tool banners.
  return `sub_${Math.random().toString(36).slice(2, 10)}`;
}

// Build a spawner closure. The same closure is re-bound at each depth level so
// sub-agents receive their own fresh (but still bounded) recursion gate.
export function makeSpawnSubagent(deps: SpawnDeps): SpawnSubagent {
  const spawn: SpawnSubagent = async (req: SpawnSubagentRequest) => {
    const subagentId = newId();
    const depth = req.parentDepth + 1;
    const startedAt = Date.now();

    const emit = (ev: SubagentProgressEvent): void => deps.onProgress?.(ev);

    if (depth > MAX_DEPTH) {
      const result: SpawnSubagentResult = {
        summary: `(refused) sub-agent depth cap reached: max ${MAX_DEPTH}.`,
        tool_uses: 0,
        duration_ms: 0,
        subagent_id: subagentId,
        stopped_reason: "depth_exceeded",
      };
      emit({ kind: "end", subagentId, type: req.type, result });
      return result;
    }

    const config = REGISTRY[req.type];
    if (!config) {
      const result: SpawnSubagentResult = {
        summary: `(refused) unknown subagent_type: ${req.type}`,
        tool_uses: 0,
        duration_ms: 0,
        subagent_id: subagentId,
        stopped_reason: "error",
      };
      emit({ kind: "end", subagentId, type: req.type, result });
      return result;
    }

    // Filter tools for this type + strip `task` unless the type can spawn AND
    // we still have depth budget.
    const tools = deps.allTools.filter((t) => {
      if (t.name === "task") return config.canSpawn && depth < MAX_DEPTH;
      return config.toolFilter(t);
    });

    const sessionFile = subagentSessionPath(deps.cwd, deps.parentSessionId, subagentId);
    const store = new SessionStore(sessionFile, subagentId, deps.cwd, true);
    store.appendEvent("subagent_start", {
      type: req.type,
      description: req.description,
      parentSessionId: deps.parentSessionId,
      depth,
    });
    const userMessage: Message = { role: "user", content: req.prompt };
    store.appendMessage(userMessage);

    emit({
      kind: "start",
      subagentId,
      type: req.type,
      description: req.description,
      prompt: req.prompt,
      depth,
    });

    const subAbort = new AbortController();
    const timeout = setTimeout(() => subAbort.abort("timeout"), SUBAGENT_TIMEOUT_MS);
    const parentAbortHandler = () => subAbort.abort("parent");
    req.parentSignal.addEventListener("abort", parentAbortHandler, { once: true });

    let toolUses = 0;
    let summary = "";
    let stoppedReason: SpawnSubagentResult["stopped_reason"] = "end_turn";

    try {
      for await (const ev of loop({
        client: deps.client,
        messages: [userMessage],
        systemPrompt: deps.baseSystemPrompt + config.systemPromptSuffix,
        tools,
        toolContext: {
          cwd: deps.cwd,
          signal: subAbort.signal,
          mcpServers: req.mcpServers,
          depth,
          spawnSubagent: spawn,
        },
        permission: deps.permission,
        onMessage: (m) => store.appendMessage(m),
      })) {
        if (ev.type === "assistant_text") {
          summary += ev.text;
          emit({ kind: "assistant_text", subagentId, type: req.type, text: ev.text });
        } else if (ev.type === "tool_use") {
          toolUses += 1;
          emit({
            kind: "tool_use",
            subagentId,
            type: req.type,
            id: ev.id,
            name: ev.name,
            input: ev.input,
          });
          if (toolUses > MAX_TOOL_CALLS_PER_SUBAGENT) {
            subAbort.abort("max-tool-calls");
          }
        } else if (ev.type === "tool_result") {
          emit({
            kind: "tool_result",
            subagentId,
            type: req.type,
            id: ev.id,
            name: ev.name,
            content: ev.content,
            isError: ev.isError,
          });
        } else if (ev.type === "done") {
          stoppedReason = ev.reason;
        } else if (ev.type === "error") {
          stoppedReason = "error";
          summary += `\n[error] ${ev.message}`;
        }
      }
    } catch (err) {
      const reason = String(subAbort.signal.reason ?? "");
      if (reason === "timeout") stoppedReason = "timeout";
      else if (reason === "max-tool-calls") stoppedReason = "max_tool_calls";
      else stoppedReason = "error";
      if (!summary) {
        summary = `[aborted] ${reason || (err instanceof Error ? err.message : String(err))}`;
      }
    } finally {
      clearTimeout(timeout);
      req.parentSignal.removeEventListener("abort", parentAbortHandler);
    }

    const result: SpawnSubagentResult = {
      summary: summary.trim() || "(no output)",
      tool_uses: toolUses,
      duration_ms: Date.now() - startedAt,
      subagent_id: subagentId,
      stopped_reason: stoppedReason,
    };
    store.appendEvent("subagent_end", result);
    emit({ kind: "end", subagentId, type: req.type, result });
    return result;
  };

  return spawn;
}
