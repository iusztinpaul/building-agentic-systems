// Offline HTTP for provider tests: recorded SSE fixtures served
// through an injected fetch that records every request.

import { readFileSync } from "node:fs";

const FIXTURES = new URL("../fixtures/openrouter/", import.meta.url);

export function fixture(name: string): string {
  return readFileSync(new URL(name, FIXTURES), "utf8");
}

// The text as a byte stream, cut every `chunkSize` bytes (cuts may land
// inside a line or a multi-byte character, like a real socket).
export function byteStream(text: string, chunkSize = Number.POSITIVE_INFINITY) {
  const bytes = new TextEncoder().encode(text);
  return new ReadableStream<Uint8Array>({
    start(controller) {
      const step = Math.min(chunkSize, bytes.length || 1);
      for (let i = 0; i < bytes.length; i += step) {
        controller.enqueue(bytes.slice(i, i + step));
      }
      controller.close();
    },
  });
}

export function sseResponse(text: string, chunkSize?: number): Response {
  return new Response(byteStream(text, chunkSize), {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

// SSE text from chunk objects: one `data:` event each, then [DONE].
export function sse(...chunks: unknown[]): string {
  const events = chunks.map((c) => `data: ${JSON.stringify(c)}\n\n`);
  return `${events.join("")}data: [DONE]\n\n`;
}

export interface FetchCall {
  url: string;
  method: string;
  headers: Record<string, string>;
  // biome-ignore lint/suspicious/noExplicitAny: assertions read arbitrary JSON fields
  body: any;
  signal: AbortSignal | undefined;
}

export function fakeFetch(
  handler: (call: FetchCall, index: number) => Response | Promise<Response>,
): { fetch: typeof fetch; calls: FetchCall[] } {
  const calls: FetchCall[] = [];
  const fn = async (input: string | URL | Request, init: RequestInit = {}) => {
    const call: FetchCall = {
      url: String(input),
      method: init.method ?? "GET",
      headers: { ...(init.headers as Record<string, string>) },
      body: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
      signal: init.signal ?? undefined,
    };
    calls.push(call);
    return handler(call, calls.length - 1);
  };
  return { fetch: fn as typeof fetch, calls };
}

export async function collect<T>(iterable: AsyncIterable<T>): Promise<T[]> {
  const out: T[] = [];
  for await (const item of iterable) out.push(item);
  return out;
}
