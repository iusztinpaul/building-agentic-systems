// The streamable-HTTP transport (MCP 2025-06-18): every frame is one
// POST to `url` with `Accept: application/json, text/event-stream`
// and the configured headers. A JSON body is one frame; an SSE body
// carries frames as `data:` events, read with the provider's parser.
// `Mcp-Session-Id` is kept when returned and echoed on every later
// request, `MCP-Protocol-Version` once negotiated. A 404 on a session
// means it expired: the transport counts as dropped.

import { log as defaultLog, type Logger } from "../log.ts";
import { parseSse } from "../provider/sse.ts";
import type {
  JsonRpcMessage,
  Transport,
  TransportHandlers,
} from "./jsonrpc.ts";

export const ACCEPT = "application/json, text/event-stream";
const DELETE_TIMEOUT_MS = 2_000;

export interface HttpOptions {
  url: string;
  headers: Record<string, string>;
  label?: string;
  log?: Logger;
  fetch?: typeof fetch;
}

export class HttpTransport implements Transport {
  readonly #o: HttpOptions;
  readonly #log: Logger;
  readonly #label: string;
  readonly #fetch: typeof fetch;
  readonly #abort = new AbortController();
  #handlers: TransportHandlers | undefined;
  #sessionId: string | undefined;
  #protocolVersion: string | undefined;

  constructor(options: HttpOptions) {
    this.#o = options;
    this.#log = options.log ?? defaultLog;
    this.#label = options.label ?? "";
    this.#fetch = options.fetch ?? fetch;
  }

  get sessionId(): string | undefined {
    return this.#sessionId;
  }

  async start(handlers: TransportHandlers): Promise<void> {
    this.#handlers = handlers;
  }

  setProtocolVersion(version: string): void {
    this.#protocolVersion = version;
  }

  async send(message: JsonRpcMessage): Promise<void> {
    if (this.#abort.signal.aborted) {
      throw new Error(`${this.#label}not connected`);
    }
    const headers = this.#headers();
    headers.set("content-type", "application/json");
    headers.set("accept", ACCEPT);
    if (this.#protocolVersion) {
      headers.set("mcp-protocol-version", this.#protocolVersion);
    }
    const res = await this.#post(headers, JSON.stringify(message));
    const session = res.headers.get("mcp-session-id");
    if (session) this.#sessionId = session;
    if (res.status === 404 && headers.has("mcp-session-id")) {
      await res.body?.cancel();
      this.#sessionId = undefined;
      const error = new Error(`${this.#label}session expired (HTTP 404)`);
      this.#handlers?.onClose(error);
      throw error;
    }
    if (!res.ok) {
      const text = (await res.text().catch(() => "")).slice(0, 200);
      const detail = text ? `: ${text}` : "";
      throw new Error(`${this.#label}HTTP ${res.status}${detail}`);
    }
    if (!res.body) return;
    const type = res.headers.get("content-type") ?? "";
    if (type.includes("text/event-stream")) {
      for await (const data of parseSse(res.body)) this.#deliver(data);
      return;
    }
    const text = await res.text(); // 202 Accepted: empty
    if (text.trim()) this.#deliver(text);
  }

  // Ends the session politely (DELETE, ignored on failure) and stops
  // reading every open response.
  async close(): Promise<void> {
    if (this.#abort.signal.aborted) return;
    this.#abort.abort();
    if (!this.#sessionId) return;
    try {
      const res = await this.#fetch(this.#o.url, {
        method: "DELETE",
        headers: this.#headers(),
        signal: AbortSignal.timeout(DELETE_TIMEOUT_MS),
      });
      await res.body?.cancel();
    } catch (err) {
      this.#log.debug(`${this.#label}DELETE: ${(err as Error).message}`);
    }
    this.#sessionId = undefined;
  }

  // The configured headers plus the session id; Headers normalises
  // case, so a configured `Accept` cannot be sent twice.
  #headers(): Headers {
    const headers = new Headers(this.#o.headers);
    if (this.#sessionId) headers.set("mcp-session-id", this.#sessionId);
    return headers;
  }

  async #post(headers: Headers, body: string): Promise<Response> {
    try {
      return await this.#fetch(this.#o.url, {
        method: "POST",
        headers,
        body,
        signal: this.#abort.signal,
      });
    } catch (err) {
      const cause = (err as { cause?: { message?: string } }).cause;
      const why = cause?.message ?? (err as Error).message;
      throw new Error(`${this.#label}POST ${this.#o.url}: ${why}`);
    }
  }

  #deliver(text: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      this.#log.warn(`${this.#label}not JSON: ${text.slice(0, 200)}`);
      return;
    }
    this.#handlers?.onMessage(parsed); // 2025-06-18 has no batches
  }
}
