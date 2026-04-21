import { randomUUID } from "node:crypto";
import type { GoogleGenAI } from "@google/genai";
import { Box, Static, Text, useApp, useInput } from "ink";
import type React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { loop } from "./agent/loop";
import type { Message } from "./messages";
import { type Rule, evaluateRules, suggestPattern } from "./permissions/policy";
import { PermissionPrompt } from "./permissions/prompt";
import type { SessionStore } from "./session/store";
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

interface PendingPermission {
  toolName: string;
  input: Record<string, unknown>;
  suggestedPattern: string;
  resolve: (decision: "allow" | "deny") => void;
}

export interface AppProps {
  client: GoogleGenAI;
  systemPrompt: string;
  tools: AnyTool[];
  toolContext: ToolContext;
  // Optional — when provided, every message the loop appends is persisted to disk
  // and permission decisions are logged as events.
  session?: SessionStore;
  // Prior history to preload (resume/continue). Rendered as opaque context — only
  // the plain user + assistant text lines are turned back into UI messages.
  initialHistory?: Message[];
}

function uiMessagesFromHistory(history: Message[]): UiMessage[] {
  const out: UiMessage[] = [];
  for (const m of history) {
    if (m.role === "user") {
      if (typeof m.content === "string") {
        out.push({ id: randomUUID(), kind: "user", text: m.content });
      } else {
        // Array form = tool_result blocks coming back from a previous turn.
        // Fill in the matching pending tool block (if the tool_use preceded it).
        for (const b of m.content) {
          if (b.type === "tool_result") {
            const target = out.find((u) => u.kind === "tool" && u.id === b.tool_use_id);
            if (target && target.kind === "tool") {
              target.result = b.content;
              target.isError = b.is_error;
            }
          }
        }
      }
      continue;
    }
    if (m.role === "assistant" && Array.isArray(m.content)) {
      const text = m.content
        .filter((b) => b.type === "text")
        .map((b) => (b as { text: string }).text)
        .join("");
      if (text) out.push({ id: randomUUID(), kind: "assistant", text, inProgress: false });
      for (const b of m.content) {
        if (b.type === "tool_use") {
          out.push({ id: b.id, kind: "tool", name: b.name, input: b.input });
        }
      }
    }
  }
  return out;
}

export function App({
  client,
  systemPrompt,
  tools,
  toolContext,
  session,
  initialHistory,
}: AppProps): React.ReactElement {
  const [messages, setMessages] = useState<UiMessage[]>(() =>
    initialHistory ? uiMessagesFromHistory(initialHistory) : [],
  );
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);
  const [pending, setPending] = useState<PendingPermission | null>(null);
  const historyRef = useRef<Message[]>(initialHistory ? [...initialHistory] : []);
  const rulesRef = useRef<Rule[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const pendingRef = useRef<PendingPermission | null>(null);
  const { exit } = useApp();

  useInput((input_, key) => {
    if ((key.ctrl && input_ === "c") || key.escape) {
      if (pendingRef.current) {
        pendingRef.current.resolve("deny");
        pendingRef.current = null;
        setPending(null);
        return;
      }
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

  const askPermission = useCallback(
    async (toolName: string, toolInput: Record<string, unknown>, tool: AnyTool) => {
      if (!tool.isDestructive) return "allow" as const;
      const rulesDecision = evaluateRules(toolName, toolInput, rulesRef.current);
      if (rulesDecision !== "ask") {
        session?.appendEvent("permission", {
          tool: toolName,
          input: toolInput,
          decision: rulesDecision,
          source: "rule",
        });
        return rulesDecision;
      }
      return new Promise<"allow" | "deny">((resolve) => {
        const prompt: PendingPermission = {
          toolName,
          input: toolInput,
          suggestedPattern: suggestPattern(toolName, toolInput),
          resolve,
        };
        pendingRef.current = prompt;
        setPending(prompt);
      });
    },
    [session],
  );

  const handleDecision = useCallback(
    (decision: "allow" | "deny", pattern?: string) => {
      const current = pendingRef.current;
      if (!current) return;
      if (pattern) {
        rulesRef.current.push({ pattern, decision: "allow" });
      }
      session?.appendEvent("permission", {
        tool: current.toolName,
        input: current.input,
        decision,
        pattern: pattern ?? null,
        source: pattern ? "user-pattern" : "user-once",
      });
      current.resolve(decision);
      pendingRef.current = null;
      setPending(null);
    },
    [session],
  );

  const submit = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || thinking) return;

      setInput("");
      const userId = randomUUID();
      setMessages((prev) => [...prev, { id: userId, kind: "user", text: trimmed }]);
      const userMessage: Message = { role: "user", content: trimmed };
      historyRef.current = [...historyRef.current, userMessage];
      session?.appendMessage(userMessage);
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
          permission: askPermission,
          onMessage: (m) => session?.appendMessage(m),
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
            currentAssistantId = null;
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
    [client, systemPrompt, tools, toolContext, thinking, askPermission, session],
  );

  return (
    <Box flexDirection="column">
      <Box marginBottom={1}>
        <Text color="green" bold>
          Tree
        </Text>
        <Text dimColor>
          {" — Your Rooted Personal Assistant. Ctrl+C to cancel / exit."}
          {session ? ` session: ${session.sessionId.slice(0, 8)}` : ""}
        </Text>
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
      {pending && (
        <PermissionPrompt
          toolName={pending.toolName}
          input={pending.input}
          suggestedPattern={pending.suggestedPattern}
          onDecide={handleDecision}
        />
      )}
      {thinking && !pending && <Spinner />}
      <Input value={input} onChange={setInput} onSubmit={submit} disabled={thinking} />
    </Box>
  );
}
