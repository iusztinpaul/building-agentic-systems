import type { GoogleGenAI } from "@google/genai";

// Fake Gemini client for loop + sub-agent tests. Replaces the only method our
// production code touches: `client.models.generateContentStream(...)`. Each
// scripted turn is consumed by one call; the fake throws a loud error if the
// loop calls past the end of the script so tests fail fast instead of hanging.
//
// Chunk shape matches what src/client.ts iterates:
//   { candidates: [{ content: { parts: [ { text } | { functionCall: { name, args } } ] } }] }

export interface FakeTurn {
  // If a string, emitted as a single chunk. If an array, each entry is a
  // separate chunk (simulates streaming word-by-word / line-by-line).
  text?: string | string[];
  // Emitted together as a single chunk after the text chunks (real Gemini
  // tends to batch function calls at the end of a turn).
  functionCalls?: Array<{ name: string; args?: Record<string, unknown> }>;
  // If set, generateContentStream rejects with an Error(error) for this turn.
  error?: string;
}

export interface FakeGeminiHandle {
  client: GoogleGenAI;
  calls: Array<{ model: string; contents: unknown; config: unknown }>;
}

interface StreamChunk {
  candidates: Array<{
    content: {
      parts: Array<
        { text: string } | { functionCall: { name: string; args: Record<string, unknown> } }
      >;
    };
  }>;
}

function* toChunks(turn: FakeTurn): Generator<StreamChunk> {
  if (turn.text !== undefined) {
    const parts = typeof turn.text === "string" ? [turn.text] : turn.text;
    for (const t of parts) {
      if (!t) continue;
      yield { candidates: [{ content: { parts: [{ text: t }] } }] };
    }
  }
  if (turn.functionCalls && turn.functionCalls.length > 0) {
    yield {
      candidates: [
        {
          content: {
            parts: turn.functionCalls.map((fc) => ({
              functionCall: { name: fc.name, args: fc.args ?? {} },
            })),
          },
        },
      ],
    };
  }
}

function toAsyncIterable(turn: FakeTurn): AsyncIterable<StreamChunk> {
  const buffered = Array.from(toChunks(turn));
  return {
    async *[Symbol.asyncIterator]() {
      for (const c of buffered) yield c;
    },
  };
}

export function makeFakeGeminiClient(opts: { turns: FakeTurn[] }): FakeGeminiHandle {
  const remaining = [...opts.turns];
  const calls: FakeGeminiHandle["calls"] = [];

  const fake = {
    models: {
      async generateContentStream(params: {
        model: string;
        contents: unknown;
        config?: unknown;
      }): Promise<AsyncIterable<StreamChunk>> {
        calls.push({ model: params.model, contents: params.contents, config: params.config });
        const turn = remaining.shift();
        if (!turn) {
          throw new Error(
            "fake Gemini ran out of scripted turns — expected fewer generateContentStream calls",
          );
        }
        if (turn.error) throw new Error(turn.error);
        return toAsyncIterable(turn);
      },
    },
  };

  return { client: fake as unknown as GoogleGenAI, calls };
}
