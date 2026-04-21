import { Box, Text, useInput } from "ink";
import type React from "react";

export interface PermissionPromptProps {
  toolName: string;
  input: Record<string, unknown>;
  suggestedPattern: string;
  onDecide: (decision: "allow" | "deny", pattern?: string) => void;
}

const MAX_INPUT_DISPLAY = 240;

function displayInput(input: Record<string, unknown>): string {
  const json = JSON.stringify(input);
  if (json.length <= MAX_INPUT_DISPLAY) return json;
  return `${json.slice(0, MAX_INPUT_DISPLAY)}… (${json.length - MAX_INPUT_DISPLAY} more chars)`;
}

export function PermissionPrompt({
  toolName,
  input,
  suggestedPattern,
  onDecide,
}: PermissionPromptProps): React.ReactElement {
  useInput((chr, key) => {
    if (chr === "y") onDecide("allow");
    else if (chr === "a") onDecide("allow", suggestedPattern);
    else if (chr === "n" || key.escape) onDecide("deny");
  });

  return (
    <Box flexDirection="column" marginY={1} paddingX={2} borderStyle="round" borderColor="yellow">
      <Text color="yellow" bold>
        ⚠ permission required
      </Text>
      <Box marginTop={1}>
        <Text>
          <Text bold>{toolName}</Text>
          <Text dimColor> wants to run with:</Text>
        </Text>
      </Box>
      <Box marginLeft={2}>
        <Text dimColor>{displayInput(input)}</Text>
      </Box>
      <Box marginTop={1} flexDirection="column">
        <Text>
          <Text color="green">[y]</Text> allow once
        </Text>
        <Text>
          <Text color="green">[a]</Text> allow this pattern:{" "}
          <Text color="cyan">{suggestedPattern}</Text>
        </Text>
        <Text>
          <Text color="red">[n]</Text> deny (esc)
        </Text>
      </Box>
    </Box>
  );
}
