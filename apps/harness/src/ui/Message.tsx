import { Box, Text } from "ink";
import type React from "react";
import { Markdown } from "./Markdown";

export interface UserMessageProps {
  text: string;
}

export function UserMessage({ text }: UserMessageProps): React.ReactElement {
  return (
    <Box flexDirection="column" marginY={1}>
      <Text color="cyan" bold>
        &gt; you
      </Text>
      <Box marginLeft={2}>
        <Text>{text}</Text>
      </Box>
    </Box>
  );
}

export interface AssistantMessageProps {
  text: string;
  inProgress?: boolean;
}

export function AssistantMessage({ text, inProgress }: AssistantMessageProps): React.ReactElement {
  return (
    <Box flexDirection="column" marginY={1}>
      <Text color="green" bold>
        • tree{inProgress ? " …" : ""}
      </Text>
      <Box marginLeft={2}>
        <Markdown>{text}</Markdown>
      </Box>
    </Box>
  );
}

export interface ErrorMessageProps {
  text: string;
}

export function ErrorMessage({ text }: ErrorMessageProps): React.ReactElement {
  return (
    <Box flexDirection="column" marginY={1}>
      <Text color="red" bold>
        ! error
      </Text>
      <Box marginLeft={2}>
        <Text color="red">{text}</Text>
      </Box>
    </Box>
  );
}
