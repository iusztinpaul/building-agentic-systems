import { Box, Text } from "ink";
import TextInput from "ink-text-input";
import type React from "react";

export interface InputProps {
  value: string;
  onChange: (v: string) => void;
  onSubmit: (v: string) => void;
  disabled?: boolean;
  placeholder?: string;
}

export function Input({
  value,
  onChange,
  onSubmit,
  disabled,
  placeholder = "ask Tree something…",
}: InputProps): React.ReactElement {
  if (disabled) {
    // While the loop is running we show the typed prompt but don't accept new input.
    return (
      <Box>
        <Text color="gray">&gt; </Text>
        <Text dimColor>{value || placeholder}</Text>
      </Box>
    );
  }
  return (
    <Box>
      <Text color="cyan">&gt; </Text>
      <TextInput value={value} onChange={onChange} onSubmit={onSubmit} placeholder={placeholder} />
    </Box>
  );
}
