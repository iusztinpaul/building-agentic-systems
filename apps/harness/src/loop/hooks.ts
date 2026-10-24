// How every product concern attaches to the loop (spec §6): the loop
// calls these hooks at fixed points and knows nothing else about
// permissions, compaction, prompts or the TUI.

import type { Provider } from "../provider/types.ts";
import type { ToolRegistry, ToolResult } from "../tools/registry.ts";
import type { AgentEvent, Context, Message, ToolCall } from "../types.ts";

export type GateResult = { run: true } | { run: false; reason: string };

export interface LoopHooks {
  // Before each model call: compaction, prompt refresh.
  prepareTurn?(ctx: Context, turn: number): Promise<Context>;
  // The permission gate; a block is answered to the model.
  beforeToolCall?(call: ToolCall): Promise<GateResult>;
  // Truncation, rewriting.
  afterToolCall?(call: ToolCall, result: ToolResult): Promise<ToolResult>;
  // Drained after each turn's tool results, before the next call.
  getSteeringMessages?(): Message[];
  // Drained only when the loop would otherwise end.
  getFollowUpMessages?(): Message[];
  emit(event: AgentEvent): void;
}

export interface LoopConfig {
  provider: Provider;
  model: string;
  tools: ToolRegistry;
  hooks: LoopHooks;
  maxTurns: number;
  signal: AbortSignal;
  cwd: string; // the tools' working directory
  depth?: number; // ToolContext.depth; 0 (the main loop) by default
}
