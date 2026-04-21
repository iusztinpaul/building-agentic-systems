import { randomUUID } from "node:crypto";
import type { GoogleGenAI } from "@google/genai";
import { Box, Static, Text, useApp, useInput } from "ink";
import type React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { loop } from "./agent/loop";
import type { Message } from "./messages";
import type { AnyTool } from "./tools/registry";
import type { ToolContext } from "./tools/types";
import { Input } from "./ui/Input";
import { AssistantMessage, ErrorMessage, UserMessage } from "./ui/Message";
import { Spinner } from "./ui/Spinner";
import { ToolCall } from "./ui/ToolCall";

// UI-layer message model. Distinct from the neutral Message / ContentBlock vocabulary
// in messages.ts: here we care about how things *render*, not how the model sees them.
type UiMessage =
  | { id: string; kind: "user"; text: string }
  | { id: string; kind: "assistant"; text: string; inProgress: boolean }
  | {
      id: string;
      kind: "tool";
      name: string;
      input: Record<string, unknown>;
      result?: string;
      isError?: boolean;
    }
  | { id: string; kind: "error"; text: string };

export interface AppProps {
  client: GoogleGenAI;
  systemPrompt: string;
  tools: AnyTool[];
  toolContext: ToolContext;
}

export function App({ client, systemPrompt, tools, toolContext }: AppProps): React.ReactElement {
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);
  const historyRef = useRef<Message[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const { exit } = useApp();

  // Ctrl+C: if thinking, cancel the current turn; otherwise exit.
  useInput((_, key) => {
    if (key.ctrl && !key.meta) return;
    // `useInput` returns key.ctrl as true for plain Ctrl-combinations. Guard narrower.
  });

  useInput((input_, key) => {
    if ((key.ctrl && input_ === "c") || key.escape) {
      if (thinking && abortRef.current) {
        abortRef.current.abort();
      } else {
        exit();
      }
    }
  });

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const submit = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || thinking) return;

      setInput("");
      const userId = randomUUID();
      setMessages((prev) => [...prev, { id: userId, kind: "user", text: trimmed }]);
      historyRef.current = [...historyRef.current, { role: "user", content: trimmed }];
      setThinking(true);

      const abort = new AbortController();
      abortRef.current = abort;
      let currentAssistantId: string | null = null;

      try {
        for await (const ev of loop({
          client,
          messages: historyRef.current,
          systemPrompt,
          tools,
          toolContext: { ...toolContext, signal: abort.signal },
        })) {
          if (ev.type === "assistant_text") {
            if (!currentAssistantId) {
              const id = randomUUID();
              currentAssistantId = id;
              setMessages((prev) => [
                ...prev,
                { id, kind: "assistant", text: ev.text, inProgress: true },
              ]);
            } else {
              const id = currentAssistantId;
              setMessages((prev) =>
                prev.map((m) =>
                  m.kind === "assistant" && m.id === id ? { ...m, text: m.text + ev.text } : m,
                ),
              );
            }
          } else if (ev.type === "tool_use") {
            currentAssistantId = null; // subsequent text is a new assistant block
            setMessages((prev) => [
              ...prev,
              { id: ev.id, kind: "tool", name: ev.name, input: ev.input },
            ]);
          } else if (ev.type === "tool_result") {
            setMessages((prev) =>
              prev.map((m) =>
                m.kind === "tool" && m.id === ev.id
                  ? { ...m, result: ev.content, isError: ev.isError }
                  : m,
              ),
            );
          } else if (ev.type === "done") {
            historyRef.current = ev.messages;
            const id = currentAssistantId;
            if (id) {
              setMessages((prev) =>
                prev.map((m) =>
                  m.kind === "assistant" && m.id === id ? { ...m, inProgress: false } : m,
                ),
              );
            }
            if (ev.reason === "max_iterations") {
              setMessages((prev) => [
                ...prev,
                { id: randomUUID(), kind: "error", text: "(hit max iterations — loop aborted)" },
              ]);
            }
          } else if (ev.type === "error") {
            setMessages((prev) => [...prev, { id: randomUUID(), kind: "error", text: ev.message }]);
          }
        }
      } finally {
        abortRef.current = null;
        setThinking(false);
      }
    },
    [client, systemPrompt, tools, toolContext, thinking],
  );

  return (
    <Box flexDirection="column">
      <Box marginBottom={1}>
        <Text color="green" bold>
          Tree
        </Text>
        <Text dimColor> — Your Rooted Personal Assistant. Ctrl+C to cancel / exit.</Text>
      </Box>
      <Static items={messages}>
        {(m) => {
          if (m.kind === "user") return <UserMessage key={m.id} text={m.text} />;
          if (m.kind === "assistant")
            return <AssistantMessage key={m.id} text={m.text} inProgress={m.inProgress} />;
          if (m.kind === "tool")
            return (
              <ToolCall
                key={m.id}
                name={m.name}
                input={m.input}
                result={m.result}
                isError={m.isError}
                pending={m.result === undefined}
              />
            );
          return <ErrorMessage key={m.id} text={m.text} />;
        }}
      </Static>
      {thinking && <Spinner />}
      <Input value={input} onChange={setInput} onSubmit={submit} disabled={thinking} />
    </Box>
  );
}
