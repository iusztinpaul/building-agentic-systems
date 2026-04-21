import { Box, Text } from "ink";
import InkSpinner from "ink-spinner";
import type React from "react";

export function Spinner({ label = "thinking" }: { label?: string }): React.ReactElement {
  return (
    <Box marginY={1} marginLeft={2}>
      <Text color="yellow">
        <InkSpinner type="dots" />
      </Text>
      <Text dimColor> {label}…</Text>
    </Box>
  );
}
