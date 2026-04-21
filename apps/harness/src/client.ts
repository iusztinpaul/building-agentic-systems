import { GoogleGenAI } from "@google/genai";
import type { Message, StreamEvent, TextBlock } from "./messages";

export const DEFAULT_MODEL = "gemini-2.5-flash";

export function createClient(): GoogleGenAI {
  const apiKey = process.env.GOOGLE_API_KEY;
  if (!apiKey) {
    throw new Error(
      "GOOGLE_API_KEY is not set. Run via `make harness-run` from the repo root (it loads ../../.env), or export it yourself.",
    );
  }
  return new GoogleGenAI({ apiKey });
}

// Translate our neutral Message[] into Gemini's `contents` format.
// Gemini uses `model` for the assistant role; system prompts are passed
// separately via `config.systemInstruction`.
function toGeminiContents(
  messages: Message[],
): Array<{ role: "user" | "model"; parts: Array<{ text: string }> }> {
  return messages
    .filter((m) => m.role !== "system")
    .map((m) => {
      const text =
        typeof m.content === "string"
          ? m.content
          : m.content
              .filter((b): b is TextBlock => b.type === "text")
              .map((b) => b.text)
              .join("");
      return {
        role: m.role === "assistant" ? ("model" as const) : ("user" as const),
        parts: [{ text }],
      };
    });
}

export async function* streamText(
  client: GoogleGenAI,
  messages: Message[],
  systemInstruction?: string,
  model: string = DEFAULT_MODEL,
): AsyncGenerator<StreamEvent> {
  const response = await client.models.generateContentStream({
    model,
    contents: toGeminiContents(messages),
    config: systemInstruction ? { systemInstruction } : undefined,
  });

  for await (const chunk of response) {
    const text = chunk.text;
    if (text) {
      yield { type: "text_delta", text };
    }
  }
  yield { type: "done" };
}
