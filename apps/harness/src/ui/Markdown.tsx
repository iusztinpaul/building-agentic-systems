import { Text } from "ink";
import type React from "react";

// Tiny inline-markdown renderer. Handles **bold**, *italic*, `code` — the 90%
// case of what Gemini actually emits. Code fences and lists stay plain; a richer
// renderer lands in M7 polish if needed.

interface Token {
  kind: "text" | "bold" | "italic" | "code";
  value: string;
  pos: number; // byte offset in the input — used as React key
}

function tokenize(md: string): Token[] {
  const out: Token[] = [];
  const re = /\*\*([^*]+)\*\*|\*([^*]+)\*|`([^`]+)`/g;
  let last = 0;
  for (const m of md.matchAll(re)) {
    const idx = m.index ?? 0;
    if (idx > last) out.push({ kind: "text", value: md.slice(last, idx), pos: last });
    if (m[1] !== undefined) out.push({ kind: "bold", value: m[1], pos: idx });
    else if (m[2] !== undefined) out.push({ kind: "italic", value: m[2], pos: idx });
    else if (m[3] !== undefined) out.push({ kind: "code", value: m[3], pos: idx });
    last = idx + m[0].length;
  }
  if (last < md.length) out.push({ kind: "text", value: md.slice(last), pos: last });
  return out;
}

export function Markdown({ children }: { children: string }): React.ReactElement {
  const tokens = tokenize(children);
  return (
    <Text>
      {tokens.map((t) => {
        const key = `${t.pos}:${t.kind}`;
        if (t.kind === "bold")
          return (
            <Text key={key} bold>
              {t.value}
            </Text>
          );
        if (t.kind === "italic")
          return (
            <Text key={key} italic>
              {t.value}
            </Text>
          );
        if (t.kind === "code")
          return (
            <Text key={key} color="cyan">
              {t.value}
            </Text>
          );
        return <Text key={key}>{t.value}</Text>;
      })}
    </Text>
  );
}
