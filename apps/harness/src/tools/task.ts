import { z } from "zod";
import { SUBAGENT_TYPES, type SubagentType, describeSubagent } from "../agent/subagents";
import type { Tool } from "./types";

// The `task` tool spawns a sub-agent. A sub-agent is a recursive call into the
// same agent loop with a narrowed tool set and a fresh conversation — the
// canonical teaching example of reusing the loop as the whole abstraction.

const schema = z.object({
  subagent_type: z
    .enum(SUBAGENT_TYPES as [SubagentType, ...SubagentType[]])
    .describe(SUBAGENT_TYPES.map((t) => `${t}: ${describeSubagent(t)}`).join(" | ")),
  description: z.string().max(200).describe("Short description shown while the sub-agent runs"),
  prompt: z
    .string()
    .describe(
      "The full task for the sub-agent. It starts with no memory of this conversation — pass all needed context here.",
    ),
});

export const taskTool: Tool<z.infer<typeof schema>> = {
  name: "task",
  description: [
    "Spawn a sub-agent with a narrowed tool set and fresh context to handle a focused sub-task.",
    "Returns a string summary. Use for: focused investigation (explore), design work (plan),",
    "or delegating a multi-step task (general). Limits: depth ≤ 2, 5-min wall-clock, 30 tool",
    "calls per sub-agent.",
  ].join(" "),
  schema,
  // The tool itself is not destructive — any destructive work happens inside the
  // sub-agent's tool calls, which go through the same permission gate.
  isReadOnly: false,
  isDestructive: false,
  async call({ subagent_type, description, prompt }, ctx) {
    if (!ctx.spawnSubagent) {
      return {
        content: "task: sub-agents are not available in this context.",
        isError: true,
      };
    }
    const result = await ctx.spawnSubagent({
      type: subagent_type,
      description,
      prompt,
      parentDepth: ctx.depth ?? 0,
      parentSignal: ctx.signal,
      mcpServers: ctx.mcpServers,
    });
    const header = `subagent ${result.subagent_id} [${subagent_type}] finished (${result.stopped_reason}): ${result.tool_uses} tool_uses, ${(result.duration_ms / 1000).toFixed(1)}s`;
    return {
      content: `${header}\n\n${result.summary}`,
      isError: result.stopped_reason === "error",
    };
  },
};
