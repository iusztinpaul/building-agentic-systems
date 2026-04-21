import { Box, Text } from "ink";
import InkSpinner from "ink-spinner";
import type React from "react";
import type { SpawnSubagentResult } from "../tools/types";

// Matches the Ui-layer subagent state built in app.tsx.

export interface AgentProgressBlockState {
  subagentId: string;
  subagentType: "general" | "explore" | "plan";
  description: string;
  events: Array<
    | { kind: "tool_use"; id: string; name: string; input: Record<string, unknown> }
    | { kind: "tool_result"; id: string; content: string; isError?: boolean }
  >;
  assistantText: string;
  done: boolean;
  result?: SpawnSubagentResult;
}

const MAX_INPUT = 140;
const MAX_RESULT = 260;

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return `${s.slice(0, n)}… (${s.length - n} more chars)`;
}

export function AgentProgress({ state }: { state: AgentProgressBlockState }): React.ReactElement {
  const typeColor =
    state.subagentType === "explore" ? "cyan" : state.subagentType === "plan" ? "magenta" : "green";

  return (
    <Box
      flexDirection="column"
      marginY={1}
      marginLeft={2}
      borderStyle="single"
      borderColor="gray"
      paddingX={1}
    >
      <Box>
        <Text>
          <Text color={typeColor} bold>
            task[{state.subagentType}]
          </Text>
          <Text dimColor> {state.subagentId}: </Text>
          <Text>{state.description}</Text>
          {!state.done && (
            <Text color="yellow">
              {" "}
              <InkSpinner type="dots" />
            </Text>
          )}
        </Text>
      </Box>
      {state.events.length > 0 && (
        <Box flexDirection="column" marginTop={1}>
          {state.events.map((ev) => {
            if (ev.kind === "tool_use") {
              return (
                <Text key={`u:${ev.id}`} dimColor>
                  <Text color="blue">▪ {ev.name}</Text>
                  <Text> {truncate(JSON.stringify(ev.input), MAX_INPUT)}</Text>
                </Text>
              );
            }
            return (
              <Text key={`r:${ev.id}`} dimColor>
                {"  "}
                <Text color={ev.isError ? "red" : "green"}>{ev.isError ? "✗" : "✓"}</Text>{" "}
                {truncate(ev.content.replace(/\n/g, " ⏎ "), MAX_RESULT)}
              </Text>
            );
          })}
        </Box>
      )}
      {state.done && state.result && (
        <Box marginTop={1}>
          <Text dimColor>
            {`↳ ${state.result.stopped_reason} · ${state.result.tool_uses} tools · ${(state.result.duration_ms / 1000).toFixed(1)}s`}
          </Text>
        </Box>
      )}
    </Box>
  );
}
