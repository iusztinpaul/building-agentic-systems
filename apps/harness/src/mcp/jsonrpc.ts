// JSON-RPC 2.0 for the MCP client: numeric request ids, a pending
// map that correlates each response with its call, notifications in
// both directions, and a per-call timeout (60 s unless the call
// says). The client asks;
// a server→client request (sampling, roots, elicitation, …) is
// answered -32601 and otherwise ignored (spec §10.2), except `ping`,
// which MCP requires to be answered with `{}`.

import { log as defaultLog, type Logger } from "../log.ts";

export const CALL_TIMEOUT_MS = 60_000;
export const METHOD_NOT_FOUND = -32601;

export type JsonRpcId = number | string;
export type JsonRpcMessage = Record<string, unknown> & {
  jsonrpc: "2.0";
};

// What carries the frames: stdio or streamable HTTP.
export interface Transport {
  start(handlers: TransportHandlers): Promise<void>;
  send(message: JsonRpcMessage): Promise<void>;
  close(): Promise<void>;
  // HTTP sends it as MCP-Protocol-Version after initialize.
  setProtocolVersion?(version: string): void;
}

export interface TransportHandlers {
  onMessage(message: unknown): void;
  onClose(error: Error): void; // dropped: the peer must reconnect
}

// An `error` member of a response.
export class JsonRpcError extends Error {
  override name = "JsonRpcError";
  readonly code: number;
  readonly data: unknown;
  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.code = code;
    this.data = data;
  }
}

// A call the peer never answered within its timeout.
export class JsonRpcTimeoutError extends Error {
  override name = "JsonRpcTimeoutError";
}

interface Pending {
  resolve(result: unknown): void;
  reject(error: Error): void;
  timer: ReturnType<typeof setTimeout>;
}

export interface PeerOptions {
  timeoutMs?: number;
  log?: Logger;
  label?: string; // prefixes errors and log lines, e.g. `mcp "x": `
}

export class JsonRpcPeer {
  readonly #send: (message: JsonRpcMessage) => Promise<void>;
  readonly #timeoutMs: number;
  readonly #log: Logger;
  readonly #label: string;
  readonly #pending = new Map<JsonRpcId, Pending>();
  #nextId = 1;
  #closed: Error | undefined;

  constructor(
    send: (message: JsonRpcMessage) => Promise<void>,
    options: PeerOptions = {},
  ) {
    this.#send = send;
    this.#timeoutMs = options.timeoutMs ?? CALL_TIMEOUT_MS;
    this.#log = options.log ?? defaultLog;
    this.#label = options.label ?? "";
  }

  request(
    method: string,
    params?: unknown,
    { timeoutMs = this.#timeoutMs }: { timeoutMs?: number } = {},
  ): Promise<unknown> {
    if (this.#closed) return Promise.reject(this.#closed);
    const id = this.#nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const seconds = timeoutMs / 1000;
        this.#settle(
          id,
          new JsonRpcTimeoutError(
            `${this.#label}${method} timed out after ${seconds} s`,
          ),
        );
        const cancel = { requestId: id, reason: "timeout" };
        this.notify("notifications/cancelled", cancel).catch(() => {});
      }, timeoutMs);
      this.#pending.set(id, { resolve, reject, timer });
      this.#send(frame({ id, method, params })).catch((err: Error) =>
        this.#settle(id, err),
      );
    });
  }

  async notify(method: string, params?: unknown): Promise<void> {
    if (this.#closed) throw this.#closed;
    await this.#send(frame({ method, params }));
  }

  // One inbound frame from the transport. Never throws.
  receive(message: unknown): void {
    if (!isObject(message)) {
      this.#log.warn(`${this.#label}ignored a non-object frame`);
      return;
    }
    const { id, method } = message;
    if (typeof method === "string") {
      if (id === undefined || id === null) return; // a notification
      const answer =
        method === "ping"
          ? frame({ id, result: {} })
          : frame({
              id,
              error: {
                code: METHOD_NOT_FOUND,
                message: `Method not found: ${method}`,
              },
            });
      this.#send(answer).catch((err: Error) =>
        this.#log.warn(`${this.#label}answering ${method}: ${err.message}`),
      );
      return;
    }
    if (typeof id !== "number" && typeof id !== "string") {
      this.#log.warn(`${this.#label}ignored a frame without an id`);
      return;
    }
    if (!this.#pending.has(id)) {
      this.#log.warn(
        `${this.#label}ignored a response to unknown id ${id}`,
      );
      return;
    }
    const { error } = message;
    if (isObject(error)) {
      const code = typeof error.code === "number" ? error.code : 0;
      const text = String(error.message ?? "error");
      this.#settle(id, new JsonRpcError(code, text, error.data));
    } else {
      this.#settle(id, undefined, message.result);
    }
  }

  // The transport is gone: every pending call rejects now, and later
  // calls reject at once.
  close(error: Error): void {
    this.#closed ??= error;
    for (const id of [...this.#pending.keys()]) this.#settle(id, error);
  }

  #settle(id: JsonRpcId, error?: Error, result?: unknown): void {
    const pending = this.#pending.get(id);
    if (!pending) return;
    this.#pending.delete(id);
    clearTimeout(pending.timer);
    if (error) pending.reject(error);
    else pending.resolve(result);
  }
}

function frame(fields: Record<string, unknown>): JsonRpcMessage {
  const out: JsonRpcMessage = { jsonrpc: "2.0" };
  for (const [key, value] of Object.entries(fields)) {
    if (value !== undefined) out[key] = value;
  }
  return out;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" && value !== null && !Array.isArray(value)
  );
}
