// Core types shared by the provider, the loop and both front ends
// (spec §4). The decision types (spec §11) live here too, so the
// TUI's prompter needs nothing from the permission layer.

export type StopReason =
  | "stop"
  | "tool_calls"
  | "length"
  | "error"
  | "aborted";

export interface Usage {
  inputTokens: number;
  outputTokens: number;
  cachedTokens?: number;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  // Set when the streamed arguments were not a JSON object: the
  // loop answers the call with an isError result carrying this
  // message instead of running the tool. `arguments` is then {}.
  parseError?: string;
}

export type Message =
  | { role: "user"; content: string }
  | {
      role: "assistant";
      content: string;
      toolCalls: ToolCall[];
      stopReason: StopReason;
      usage?: Usage;
    }
  | {
      role: "tool";
      toolCallId: string;
      name: string;
      content: string;
      isError: boolean;
    };
// The system prompt is not a Message; it is rebuilt from sections
// every request.

export type AssistantMessage = Message & { role: "assistant" };

export interface Context {
  system: string;
  messages: Message[];
}

export type PermissionMode = "ask" | "auto";
export type Decision = "allow" | "deny";
export type DecisionSource =
  | "readOnly"
  | "session"
  | "user"
  | "classifier"
  | "headless";

// The TUI's dialog; headless runs have none (a prompt is a deny).
export interface PermissionPrompter {
  ask(
    call: ToolCall,
    summary: string,
    reason?: string,
  ): Promise<"once" | "session" | "deny">;
}

export type AgentEndReason =
  | "completed"
  | "max_turns"
  | "aborted"
  | "error"
  | "blocked";

export type AgentEvent =
  | { type: "turn_start" }
  | { type: "text_delta"; text: string }
  | { type: "tool_call"; call: ToolCall }
  | {
      type: "tool_result";
      callId: string;
      content: string;
      isError: boolean;
    }
  | {
      type: "permission";
      call: ToolCall;
      decision: Decision;
      source: DecisionSource;
      reason?: string;
    }
  | { type: "tool_activation"; names: string[] }
  // from the sandbox llm()
  | { type: "llm_call"; depth: number; model: string; usage?: Usage }
  | { type: "compaction"; before: number; after: number }
  | { type: "turn_end"; message: AssistantMessage }
  | { type: "agent_end"; reason: AgentEndReason }
  | { type: "error"; message: string };
