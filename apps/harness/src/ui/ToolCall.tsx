import { Box, Text } from "ink";
import type React from "react";

export interface ToolCallProps {
  name: string;
  input: Record<string, unknown>;
  result?: string;
  isError?: boolean;
  pending?: boolean;
}

const MAX_INPUT = 160;
const MAX_RESULT = 300;

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return `${s.slice(0, n)}… (${s.length - n} more chars)`;
}

export function ToolCall({
  name,
  input,
  result,
  isError,
  pending,
}: ToolCallProps): React.ReactElement {
  const inputLine = truncate(JSON.stringify(input), MAX_INPUT);
  return (
    <Box flexDirection="column" marginLeft={2}>
      <Text dimColor>
        <Text color={pending ? "yellow" : "blue"}>▪ {name}</Text>
        <Text> {inputLine}</Text>
      </Text>
      {result !== undefined && (
        <Text dimColor>
          {"  "}
          <Text color={isError ? "red" : "green"}>{isError ? "✗" : "✓"}</Text>{" "}
          {truncate(result.replace(/\n/g, " ⏎ "), MAX_RESULT)}
        </Text>
      )}
    </Box>
  );
}
