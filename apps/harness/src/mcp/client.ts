// The hand-rolled MCP client (spec §10.2): `initialize` (protocol
// 2025-06-18, capabilities {}), `notifications/initialized`, then
// `tools/list` (following `nextCursor`) and `tools/call` on demand.
// Resources and prompts are never requested. It connects lazily: the
// first listTools()/callTool() runs the handshake, and a call after
// the transport dropped (a crashed child, an expired HTTP session)
// reconnects and re-initialises first. An HTTP server without a
// configured Authorization header gets an MCP login (auth.ts): its
// 401 ends the first handshake, the login runs outside the
// `initialize` timer, and the handshake is retried once. close(), or
// the caller's `signal`, ends a handshake in flight at once.

import { z } from "zod";
import { log as defaultLog, type Logger } from "../log.ts";
import {
  hasAuthorization,
  type LoginUi,
  McpAuth,
  McpAuthRequired,
} from "./auth.ts";
import { authFilePath } from "./auth-store.ts";
import { loadMcpConfig, type McpServer } from "./config.ts";
import {
  JsonRpcPeer,
  JsonRpcTimeoutError,
  type Transport,
} from "./jsonrpc.ts";
import { HttpTransport } from "./transport-http.ts";
import { StdioTransport } from "./transport-stdio.ts";

export const PROTOCOL_VERSION = "2025-06-18";
const CLIENT_INFO = { name: "tree-agent", version: "0.1.0" };
// The hosted server's 60 s cold-start window; a silent server costs
// startup this. settings.mcp.initializeTimeoutMs (same default)
// overrides it.
export const INITIALIZE_TIMEOUT_MS = 60_000;
const INITIALIZE_HINT = " (settings.json: mcp.initializeTimeoutMs)";
// With the repeated-cursor guard: a server minting endless cursors.
export const MAX_TOOL_PAGES = 100;

const Tool = z.object({
  name: z.string(),
  description: z.string().optional(),
  inputSchema: z.record(z.unknown()).default({ type: "object" }),
  // Untrusted hints (readOnlyHint, …); 209 reads `=== true`.
  annotations: z.record(z.unknown()).optional(),
});
const ListResult = z.object({
  tools: Tool.array(),
  nextCursor: z.string().nullish(),
});
const Content = z.object({ type: z.string() }).passthrough();
const CallResult = z.object({
  content: Content.array().default([]),
  structuredContent: z.record(z.unknown()).optional(),
  isError: z.boolean().default(false),
});
const InitializeResult = z.object({ protocolVersion: z.string() });

export type McpTool = z.infer<typeof Tool>;
export type McpContent = z.infer<typeof Content>;
export type McpCallResult = z.infer<typeof CallResult>;

export interface McpClientOptions {
  cwd?: string; // a stdio child's working directory
  // A stdio child's base env; TREE_AGENT_HOME locates mcp-auth/.
  env?: NodeJS.ProcessEnv;
  timeoutMs?: number; // per call, default 60 s
  initializeTimeoutMs?: number; // default INITIALIZE_TIMEOUT_MS
  log?: Logger;
  fetch?: typeof fetch;
  // May log in interactively (TTYs, or --mcp-login). Absent: a
  // server that needs a login is skipped with one warning.
  login?: LoginUi;
  freshLogin?: boolean; // --mcp-login: ignore the cached tokens
  // The TUI's Ctrl+C at startup: a handshake or login in flight
  // rejects now, its child or HTTP session dropped; later calls too.
  signal?: AbortSignal;
}

export class McpClient {
  readonly name: string;
  readonly server: McpServer;
  readonly #o: McpClientOptions;
  readonly #log: Logger;
  readonly #label: string;
  readonly #auth: McpAuth | undefined;
  readonly #closing = new AbortController(); // ends a login wait
  readonly #stop: AbortSignal; // #closing or the caller's signal
  #teardown: Promise<void> | undefined; // a stopped handshake's close
  #peer: JsonRpcPeer | undefined;
  #transport: Transport | undefined;
  #connecting: Promise<JsonRpcPeer> | undefined;
  #tools: McpTool[] = [];
  #closed = false;
  #loggedIn = false;

  constructor(
    name: string,
    server: McpServer,
    options: McpClientOptions = {},
  ) {
    this.name = name;
    this.server = server;
    this.#o = options;
    this.#log = options.log ?? defaultLog;
    this.#label = `mcp "${name}": `;
    this.#stop = options.signal
      ? AbortSignal.any([options.signal, this.#closing.signal])
      : this.#closing.signal;
    if ("url" in server && !hasAuthorization(server.headers)) {
      this.#auth = new McpAuth({
        name,
        url: server.url,
        file: authFilePath(options.env ?? process.env, name),
        fetch: options.fetch,
        log: this.#log,
        fresh: options.freshLogin,
      });
    }
  }

  get connected(): boolean {
    return this.#peer !== undefined;
  }

  // A login stored its tokens, whatever the handshake after it did.
  get loggedIn(): boolean {
    return this.#loggedIn;
  }

  // The last successful listTools().
  get tools(): readonly McpTool[] {
    return this.#tools;
  }

  // Every page, each tool once (the first occurrence wins).
  async listTools(): Promise<McpTool[]> {
    const peer = await this.#connect();
    const byName = new Map<string, McpTool>();
    const cursors = new Set<string>();
    let cursor: string | undefined;
    let pages = 0;
    do {
      if (pages++ === MAX_TOOL_PAGES) {
        this.#log.warn(
          `${this.#label}tools/list stopped after ${MAX_TOOL_PAGES} pages`,
        );
        break;
      }
      const params = cursor === undefined ? undefined : { cursor };
      const raw = await peer.request("tools/list", params);
      const page = this.#parse(ListResult, raw, "tools/list");
      for (const tool of page.tools) {
        if (!byName.has(tool.name)) byName.set(tool.name, tool);
      }
      cursor = page.nextCursor ?? undefined;
      if (cursor !== undefined && cursors.has(cursor)) {
        this.#log.warn(
          `${this.#label}tools/list repeated cursor ${cursor}`,
        );
        break;
      }
      if (cursor !== undefined) cursors.add(cursor);
    } while (cursor !== undefined);
    this.#tools = [...byName.values()];
    return this.#tools;
  }

  // The raw result; `isError: true` is a result, not a throw.
  async callTool(
    name: string,
    args: Record<string, unknown> = {},
  ): Promise<McpCallResult> {
    const peer = await this.#connect();
    const raw = await peer.request("tools/call", { name, arguments: args });
    return this.#parse(CallResult, raw, `tools/call ${name}`);
  }

  async close(): Promise<void> {
    this.#closed = true;
    this.#closing.abort();
    const transport = this.#transport;
    this.#peer?.close(new Error(`${this.#label}closed`));
    this.#peer = undefined;
    this.#transport = undefined;
    await Promise.all([transport?.close(), this.#teardown]);
  }

  #connect(): Promise<JsonRpcPeer> {
    if (this.#stop.aborted) return Promise.reject(this.#stopped());
    if (this.#peer) return Promise.resolve(this.#peer);
    this.#connecting ??= this.#handshake().finally(() => {
      this.#connecting = undefined;
    });
    return this.#connecting;
  }

  // One retry after a login; a second 401 is a plain error.
  async #handshake(): Promise<JsonRpcPeer> {
    try {
      return await this.#attempt();
    } catch (err) {
      if (!(err instanceof McpAuthRequired) || !this.#auth) throw err;
      await this.#login(this.#auth, err.challenge);
      this.#loggedIn = true;
    }
    try {
      return await this.#attempt();
    } catch (err) {
      if (!(err instanceof McpAuthRequired)) throw err;
      throw new Error(`${this.#label}HTTP 401 after the login`);
    }
  }

  // Outside any JSON-RPC timer: bounded by the login's own.
  async #login(auth: McpAuth, challenge: string): Promise<void> {
    const ui = this.#o.login;
    if (!ui) {
      throw new Error(
        `${this.#label}needs a login; run tree-agent --mcp-login ` +
          `${this.name} once, then re-run`,
      );
    }
    const signal = ui.signal
      ? AbortSignal.any([ui.signal, this.#stop])
      : this.#stop;
    await auth.login(challenge, { ...ui, signal });
  }

  async #attempt(): Promise<JsonRpcPeer> {
    if (this.#stop.aborted) throw this.#stopped(); // after a login
    const transport = this.#createTransport();
    const peer = new JsonRpcPeer((m) => transport.send(m), {
      timeoutMs: this.#o.timeoutMs,
      log: this.#log,
      label: this.#label,
    });
    // Not awaited: a child deaf to EOF is SIGTERMed only after 1 s.
    const stop = () => {
      peer.close(this.#stopped());
      if (this.#peer === peer) {
        this.#peer = undefined;
        this.#transport = undefined;
      }
      this.#teardown = transport.close();
    };
    let dropped: Error | undefined;
    await transport.start({
      onMessage: (message) => peer.receive(message),
      onClose: (error) => {
        this.#stop.removeEventListener("abort", stop);
        dropped = error;
        peer.close(error);
        if (this.#peer !== peer) return;
        this.#peer = undefined;
        this.#transport = undefined;
      },
    });
    this.#stop.addEventListener("abort", stop, { once: true });
    if (this.#stop.aborted) stop(); // during start()
    try {
      const raw = await peer
        .request(
          "initialize",
          {
            protocolVersion: PROTOCOL_VERSION,
            capabilities: {},
            clientInfo: CLIENT_INFO,
          },
          {
            timeoutMs: this.#o.initializeTimeoutMs ?? INITIALIZE_TIMEOUT_MS,
          },
        )
        .catch((err: Error) => {
          // Name the knob that raises the wait.
          if (err instanceof JsonRpcTimeoutError) {
            throw new JsonRpcTimeoutError(err.message + INITIALIZE_HINT);
          }
          throw err;
        });
      const { protocolVersion } = this.#parse(
        InitializeResult,
        raw,
        "initialize",
      );
      if (protocolVersion !== PROTOCOL_VERSION) {
        this.#log.warn(
          `${this.#label}server speaks ${protocolVersion}, ` +
            `not ${PROTOCOL_VERSION}; continuing`,
        );
      }
      transport.setProtocolVersion?.(protocolVersion);
      await peer.notify("notifications/initialized");
      if (dropped) throw dropped;
      if (this.#stop.aborted) throw this.#stopped();
    } catch (err) {
      this.#stop.removeEventListener("abort", stop);
      peer.close(err as Error);
      if (!this.#stop.aborted) await transport.close(); // else stop()
      throw err;
    }
    this.#peer = peer;
    this.#transport = transport;
    return peer;
  }

  #stopped(): Error {
    const why = this.#closed ? "closed" : "connect cancelled";
    return new Error(`${this.#label}${why}`);
  }

  #createTransport(): Transport {
    const { server } = this;
    const common = { label: this.#label, log: this.#log };
    if ("command" in server) {
      return new StdioTransport({
        ...common,
        command: server.command,
        args: server.args,
        env: server.env,
        cwd: this.#o.cwd,
        baseEnv: this.#o.env,
      });
    }
    return new HttpTransport({
      ...common,
      url: server.url,
      headers: server.headers,
      fetch: this.#o.fetch,
      auth: this.#auth,
    });
  }

  #parse<T>(
    schema: z.ZodType<T, z.ZodTypeDef, unknown>,
    raw: unknown,
    what: string,
  ): T {
    const result = schema.safeParse(raw);
    if (result.success) return result.data;
    const issue = result.error.issues[0];
    const where = issue?.path.join(".") || "result";
    throw new Error(
      `${this.#label}${what}: unexpected ${where}: ${issue?.message}`,
    );
  }
}

export interface StartMcpOptions
  extends Pick<
    McpClientOptions,
    "initializeTimeoutMs" | "log" | "login" | "signal"
  > {
  env: NodeJS.ProcessEnv;
  cwd: string;
  trusted: boolean; // the project .tree-agent/mcp.json too
}

export type StartMcp = (options: StartMcpOptions) => Promise<McpClient[]>;

// Startup: read the config (a bad file throws), connect every server
// and list its tools. A server that fails to start is warned about
// and kept: its next call retries the handshake.
export const startMcp: StartMcp = async ({ trusted, ...options }) => {
  const servers = await loadMcpConfig({
    env: options.env,
    projectDir: trusted ? options.cwd : undefined,
  });
  return connectMcpServers(servers, options);
};

export async function connectMcpServers(
  servers: Record<string, McpServer>,
  options: McpClientOptions = {},
): Promise<McpClient[]> {
  const log = options.log ?? defaultLog;
  const count = Object.keys(servers).length;
  if (count > 0) log.info(`connecting to ${count} MCP server(s)…`);
  return Promise.all(
    Object.entries(servers).map(async ([name, server]) => {
      const client = new McpClient(name, server, options);
      try {
        await client.listTools();
      } catch (err) {
        const message = (err as Error).message;
        const label = `mcp "${name}": `;
        log.warn(message.startsWith(label) ? message : label + message);
      }
      return client;
    }),
  );
}
