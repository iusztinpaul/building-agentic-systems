import { GoogleGenAI } from "@google/genai";
import { DEFAULT_MODEL } from "./constants";
import type { Message, StreamEvent } from "./messages";
// Import from the leaf "./tools/gemini" rather than "./tools/registry" — the
// registry pulls in every concrete tool, which closes the import cycle
// (registry → task → agent/subagents → agent/loop → client) that triggers TDZ
// errors on Bun + Linux. The leaf only depends on zod and tools/types.
import { type AnyTool, toGeminiTools } from "./tools/gemini";

// Re-export for backwards compatibility with anything importing DEFAULT_MODEL
// from "./client". The canonical home is ./constants — see that file for why.
export { DEFAULT_MODEL };

export function createClient(): GoogleGenAI {
  const apiKey = process.env.GOOGLE_API_KEY;
  if (!apiKey) {
    throw new Error(
      "GOOGLE_API_KEY is not set. Run via `make harness-run` from the repo root (loads ../../.env), or export it yourself.",
    );
  }
  return new GoogleGenAI({ apiKey });
}

type GeminiPart =
  | { text: string }
  | { functionCall: { name: string; args: Record<string, unknown> } }
  | { functionResponse: { name: string; response: Record<string, unknown> } };

type GeminiContent = { role: "user" | "model"; parts: GeminiPart[] };

// Translate neutral Message[] into Gemini's `contents` array. Skips system messages
// (they flow via config.systemInstruction). tool_use blocks land under role "model"
// as `functionCall` parts; tool_result blocks land under role "user" as
// `functionResponse` parts.
function toGeminiContents(messages: Message[]): GeminiContent[] {
  const out: GeminiContent[] = [];
  for (const m of messages) {
    if (m.role === "system") continue;
    const blocks =
      typeof m.content === "string" ? [{ type: "text" as const, text: m.content }] : m.content;

    const parts: GeminiPart[] = [];
    for (const b of blocks) {
      if (b.type === "text") {
        parts.push({ text: b.text });
      } else if (b.type === "tool_use") {
        parts.push({
          functionCall: { name: b.name, args: b.input ?? {} },
        });
      } else if (b.type === "tool_result") {
        const response: Record<string, unknown> = { result: b.content };
        if (b.is_error) response.error = true;
        parts.push({
          functionResponse: { name: b.tool_name, response },
        });
      }
    }

    const role: "user" | "model" = m.role === "assistant" ? "model" : "user";
    out.push({ role, parts });
  }
  return out;
}

export interface StreamTextOptions {
  messages: Message[];
  systemInstruction?: string;
  tools?: AnyTool[];
  model?: string;
}

export async function* streamText(
  client: GoogleGenAI,
  opts: StreamTextOptions,
): AsyncGenerator<StreamEvent> {
  const { messages, systemInstruction, tools, model = DEFAULT_MODEL } = opts;

  const config: Record<string, unknown> = {};
  if (systemInstruction) config.systemInstruction = systemInstruction;
  if (tools?.length) {
    config.tools = toGeminiTools(tools);
  }

  const response = await client.models.generateContentStream({
    model,
    contents: toGeminiContents(messages),
    config: Object.keys(config).length ? config : undefined,
  });

  // Iterate parts manually instead of using chunk.text / chunk.functionCalls getters,
  // which emit noisy "there are non-text parts" warnings when both coexist in a chunk.
  for await (const chunk of response) {
    const parts = chunk.candidates?.[0]?.content?.parts ?? [];
    for (const part of parts) {
      if (typeof part.text === "string" && part.text.length > 0) {
        yield { type: "text_delta", text: part.text };
      } else if (part.functionCall?.name) {
        yield {
          type: "function_call",
          name: part.functionCall.name,
          args: part.functionCall.args ?? {},
        };
      }
    }
  }
  yield { type: "done" };
}
