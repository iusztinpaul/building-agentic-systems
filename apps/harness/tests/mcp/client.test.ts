import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createLogger, type Logger } from "../../src/log.ts";
import {
  connectMcpServers,
  INITIALIZE_TIMEOUT_MS,
  MAX_TOOL_PAGES,
  McpClient,
  type McpClientOptions,
  PROTOCOL_VERSION,
  startMcp,
} from "../../src/mcp/client.ts";
import type { McpServer } from "../../src/mcp/config.ts";
import { type FakeMcpHttp, startFakeMcpHttp } from "../helpers/mcp-http.ts";
import { gone } from "../helpers/tmp.ts";

const SERVER = fileURLToPath(new URL("../fixtures/fake-mcp-server.mjs", import.meta.url));

let dir: string;
let marker: string;
let http: FakeMcpHttp | undefined;
const clients: McpClient[] = [];

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "tree-agent-mcp-client-"));
  marker = join(dir, "pids");
});

afterEach(async () => {
  vi.useRealTimers();
  // Kill every child even when an assertion failed.
  const pids = pidsOf(marker);
  await Promise.all(clients.splice(0).map((c) => c.close()));
  for (const pid of pids) expect(await gone(pid)).toBe(true);
  await http?.close();
  http = undefined;
  rmSync(dir, { recursive: true, force: true });
});

function pidsOf(file: string): number[] {
  try {
    return readFileSync(file, "utf8").trim().split("\n").filter(Boolean).map(Number);
  } catch {
    return [];
  }
}

function sink(): { log: Logger; lines: string[] } {
  const lines: string[] = [];
  return { log: createLogger("debug", { write: (t: string) => lines.push(t) }), lines };
}

function stdio(env: Record<string, string> = {}): McpServer {
  return {
    command: process.execPath,
    args: [SERVER],
    env: { FAKE_MCP_MARKER: marker, ...env },
    toolExposure: {},
  };
}

// Reads stdin, never answers, exits on EOF (so close() is quick).
const SILENT_STDIO: McpServer = {
  command: process.execPath,
  args: ["-e", "process.stdin.resume(); process.stdin.on('end', () => process.exit(0))"],
  env: {},
  toolExposure: {},
};
// An HTTP server that never answers: with fake timers, no real I/O.
const SILENT_HTTP: McpServer = { url: "http://silent.test/mcp", headers: {}, toolExposure: {} };
// Every request hangs until the transport aborts it (close()).
const hangingFetch = ((_url: unknown, init?: RequestInit) =>
  new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
  })) as typeof fetch;

function client(server: McpServer, options: McpClientOptions = {}): McpClient {
  const c = new McpClient("fake", server, options);
  clients.push(c);
  return c;
}

// The methods the fake server received, in order (it logs "recv <m>").
const received = (lines: string[]) =>
  lines.flatMap((l) => {
    const m = /recv (\S+)/.exec(l);
    return m?.[1] ? [m[1]] : [];
  });

describe("McpClient over stdio (fake-mcp-server.mjs)", () => {
  it("handshakes initialize → notifications/initialized → tools/list ×2 and lists both tools", async () => {
    const { log, lines } = sink();
    const c = client(stdio(), { log });

    const tools = await c.listTools();

    expect(received(lines)).toEqual([
      "initialize",
      "notifications/initialized",
      "tools/list",
      "tools/list",
    ]);
    expect(tools.map((t) => t.name)).toEqual(["echo", "read_note"]);
    expect(tools[1]?.annotations).toEqual({ readOnlyHint: true });
    expect(tools[0]?.annotations).toBeUndefined();
    expect(tools[1]?.inputSchema).toMatchObject({ required: ["id"] });
    expect(c.tools).toEqual(tools);
    expect(c.connected).toBe(true);
    expect(c.name).toBe("fake");
  });

  it("callTool returns the echoed args as text content", async () => {
    const c = client(stdio());

    const result = await c.callTool("echo", { query: "agents", n: 2 });

    expect(result).toEqual({
      content: [{ type: "text", text: '{"query":"agents","n":2}' }],
      isError: false,
    });
  });

  it("callTool returns structuredContent when the tool sends it", async () => {
    const c = client(stdio());

    const result = await c.callTool("read_note", { id: "n1" });

    expect(result.structuredContent).toEqual({ id: "n1", body: "note n1" });
  });

  it("a JSON-RPC error from tools/call rejects with the server's message", async () => {
    const c = client(stdio());

    await expect(c.callTool("nope")).rejects.toThrow("Unknown tool: nope");
  });

  it("answers a server→client request -32601 and the pending call still resolves", async () => {
    const c = client(stdio());

    const result = await c.callTool("echo", { probe: true });

    const answer = JSON.parse(String(result.content[0]?.text));
    expect(answer.probeAnswer).toEqual({
      jsonrpc: "2.0",
      id: "srv-1",
      error: { code: -32601, message: "Method not found: sampling/createMessage" },
    });
  });

  it("a call with no answer rejects after the per-call timeout", async () => {
    const c = client(stdio(), { timeoutMs: 200 });

    await expect(c.callTool("echo", { hang: true })).rejects.toThrow(
      'mcp "fake": tools/call timed out after 0.2 s',
    );
    await expect(c.callTool("echo", { after: "timeout" })).resolves.toMatchObject({
      isError: false,
    });
  });

  it("tools/list stops after 100 pages of endlessly new cursors, warning once", async () => {
    const { log, lines } = sink();
    const c = client(stdio({ FAKE_MCP_ENDLESS_PAGES: "1" }), { log });

    const tools = await c.listTools();

    expect(MAX_TOOL_PAGES).toBe(100);
    expect(tools).toHaveLength(100);
    expect(tools.at(-1)?.name).toBe("t100");
    expect(received(lines).filter((m) => m === "tools/list")).toHaveLength(100);
    const warns = lines.filter((l) => l.includes("warn:"));
    expect(warns).toEqual(['[tree-agent] warn: mcp "fake": tools/list stopped after 100 pages\n']);
  });

  it("child's stderr goes to the logger at info", async () => {
    const { log, lines } = sink();
    const c = client(stdio(), { log });

    await c.listTools();

    expect(lines).toContain('[tree-agent] info: mcp "fake": fake-mcp-server: ready\n');
  });

  it("close() ends the child and later calls reject", async () => {
    const c = client(stdio());
    await c.listTools();
    const [pid] = pidsOf(marker);

    await c.close();

    expect(await gone(pid as number)).toBe(true);
    expect(c.connected).toBe(false);
    await expect(c.callTool("echo")).rejects.toThrow('mcp "fake": closed');
  });

  it("a dropped child is respawned and re-initialised on the next callTool", async () => {
    const { log, lines } = sink();
    const c = client(stdio({ FAKE_MCP_EXIT_AFTER_CALL: "1" }), { log });

    await c.listTools();

    const first = await c.callTool("echo", { n: 1 });
    const [firstPid] = pidsOf(marker);
    expect(await gone(firstPid as number)).toBe(true);
    // `connected` flips on the child's "close", which may trail the pid's exit.
    for (let i = 0; i < 100 && c.connected; i++) await new Promise((r) => setTimeout(r, 10));
    expect(c.connected).toBe(false);
    const second = await c.callTool("echo", { n: 2 });

    expect(first.content[0]?.text).toBe('{"n":1}');
    expect(second.content[0]?.text).toBe('{"n":2}');
    expect(pidsOf(marker)).toHaveLength(2);
    expect(lines.filter((l) => l.includes("warn:"))).toEqual([
      '[tree-agent] warn: mcp "fake": server exited (code 0)\n',
    ]);
    expect(received(lines)).toEqual([
      "initialize",
      "notifications/initialized",
      "tools/list",
      "tools/list",
      "tools/call",
      "initialize",
      "notifications/initialized",
      "tools/call",
    ]);
  });

  it("two concurrent calls on a dropped client share one reconnect", async () => {
    const c = client(stdio());

    const [a, b] = await Promise.all([c.callTool("echo", { a: 1 }), c.callTool("echo", { b: 1 })]);

    expect(a.content[0]?.text).toBe('{"a":1}');
    expect(b.content[0]?.text).toBe('{"b":1}');
    expect(pidsOf(marker)).toHaveLength(1);
  });

  it("a command that cannot start rejects listTools with its name", async () => {
    const c = client(
      { command: join(dir, "missing-server"), args: [], env: {}, toolExposure: {} },
      { log: sink().log },
    );

    await expect(c.listTools()).rejects.toThrow(
      `mcp "fake": cannot start ${join(dir, "missing-server")}`,
    );
  });

  it("initialize carries protocol 2025-06-18, capabilities {} and clientInfo", async () => {
    // A server that writes what it received and exits.
    const script =
      "require('readline').createInterface({input:process.stdin}).once('line',l=>{" +
      `require('fs').writeFileSync(${JSON.stringify(join(dir, "init.json"))},l);process.exit(0)})`;
    const c = client(
      { command: process.execPath, args: ["-e", script], env: {}, toolExposure: {} },
      { log: sink().log },
    );

    await expect(c.listTools()).rejects.toThrow("server exited");

    const init = JSON.parse(readFileSync(join(dir, "init.json"), "utf8"));
    expect(PROTOCOL_VERSION).toBe("2025-06-18");
    expect(init).toEqual({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "tree-agent", version: "0.1.0" },
      },
    });
  });
});

describe("McpClient over streamable HTTP", () => {
  const httpServer = (path: "/json" | "/sse", headers: Record<string, string> = {}): McpServer => ({
    url: http?.url(path) ?? "",
    headers,
    toolExposure: {},
  });

  it("JSON responses: handshake, two pages, callTool", async () => {
    http = await startFakeMcpHttp();
    const c = client(httpServer("/json"));

    const tools = await c.listTools();
    const result = await c.callTool("echo", { q: "json" });

    expect(tools.map((t) => t.name)).toEqual(["echo", "read_note"]);
    expect(result.structuredContent).toEqual({ q: "json" });
    expect(http.requests.map((r) => r.body.method)).toEqual([
      "initialize",
      "notifications/initialized",
      "tools/list",
      "tools/list",
      "tools/call",
    ]);
    expect(http.requests[3]?.body.params).toEqual({ cursor: "p2" });
    expect(http.requests[2]?.body.params).toBeUndefined();
  });

  it("SSE responses with a session id: the id and the headers ride every request", async () => {
    http = await startFakeMcpHttp({ sessionId: "abc" });
    const c = client(httpServer("/sse", { Authorization: "Bearer tok" }));

    const result = await c.callTool("echo", { q: "sse" });

    expect(result.content[0]?.text).toBe('{"q":"sse"}');
    const posts = http.requests.filter((r) => r.method === "POST");
    expect(posts.map((r) => r.headers["mcp-session-id"])).toEqual([undefined, "abc", "abc"]);
    expect(posts.every((r) => r.headers.authorization === "Bearer tok")).toBe(true);
    expect(posts.every((r) => r.headers.accept === "application/json, text/event-stream")).toBe(
      true,
    );
    expect(posts.slice(1).every((r) => r.headers["mcp-protocol-version"] === "2025-06-18")).toBe(
      true,
    );
  });

  it("an expired session re-initialises on the next call", async () => {
    http = await startFakeMcpHttp({ sessionId: "abc" });
    const c = client(httpServer("/json"));
    await c.listTools();
    http.expire();

    await expect(c.callTool("echo", { n: 1 })).rejects.toThrow("session expired");
    const again = await c.callTool("echo", { n: 2 });

    expect(again.structuredContent).toEqual({ n: 2 });
    const methods = http.requests.map((r) => r.body?.method);
    expect(methods.slice(-3)).toEqual(["initialize", "notifications/initialized", "tools/call"]);
  });
});

describe("connectMcpServers / startMcp", () => {
  it("connects every server and lists its tools; a broken one is warned about and kept", async () => {
    const { log, lines } = sink();
    const servers = {
      good: stdio(),
      broken: { command: join(dir, "missing"), args: [], env: {}, toolExposure: {} },
    };

    const connected = await connectMcpServers(servers, { log });
    clients.push(...connected);

    expect(connected.map((c) => c.name)).toEqual(["good", "broken"]);
    expect(connected[0]?.tools.map((t) => t.name)).toEqual(["echo", "read_note"]);
    expect(connected[1]?.tools).toEqual([]);
    expect(lines.join("")).toContain(`warn: mcp "broken": cannot start ${join(dir, "missing")}`);
    expect(lines.filter((l) => l.includes("warn:"))).toHaveLength(1);
  });

  it("a server silent on initialize: an info line first, one warn after its own timeout, the client kept", async () => {
    const { log, lines } = sink();
    const connected = await connectMcpServers(
      { silent: SILENT_STDIO, good: stdio() },
      { log, timeoutMs: 60_000, initializeTimeoutMs: 200 },
    );
    clients.push(...connected);

    expect(lines[0]).toBe("[tree-agent] info: connecting to 2 MCP server(s)…\n");
    const warns = lines.filter((l) => l.includes("warn:"));
    expect(warns).toEqual([
      '[tree-agent] warn: mcp "silent": initialize timed out after 0.2 s (settings.json: mcp.initializeTimeoutMs)\n',
    ]);
    expect(connected.map((c) => [c.name, c.connected])).toEqual([
      ["silent", false],
      ["good", true],
    ]);
    expect(connected[0]?.tools).toEqual([]);
  });

  it("initialize waits 30 s by default: pending at 29 999 ms, a warn naming the setting at 30 000 ms", async () => {
    vi.useFakeTimers();
    const { log, lines } = sink();

    const pending = connectMcpServers({ silent: SILENT_HTTP }, { log, fetch: hangingFetch });
    await vi.advanceTimersByTimeAsync(INITIALIZE_TIMEOUT_MS - 1);
    const early = lines.filter((l) => l.includes("warn:"));
    await vi.advanceTimersByTimeAsync(1);
    const connected = await pending;
    clients.push(...connected);

    expect(INITIALIZE_TIMEOUT_MS).toBe(30_000);
    expect(early).toEqual([]);
    expect(lines.filter((l) => l.includes("warn:"))).toEqual([
      '[tree-agent] warn: mcp "silent": initialize timed out after 30 s (settings.json: mcp.initializeTimeoutMs)\n',
    ]);
    expect(connected[0]?.connected).toBe(false);
  });

  it("a custom initializeTimeoutMs is honoured: 45 s is still pending at 44 999 ms", async () => {
    vi.useFakeTimers();
    const { log, lines } = sink();
    const warns = () => lines.filter((l) => l.includes("warn:"));

    const pending = connectMcpServers(
      { silent: SILENT_HTTP },
      { log, fetch: hangingFetch, initializeTimeoutMs: 45_000 },
    );
    await vi.advanceTimersByTimeAsync(44_999);
    const early = warns();
    await vi.advanceTimersByTimeAsync(1);
    clients.push(...(await pending));

    expect(early).toEqual([]);
    expect(warns()).toEqual([
      '[tree-agent] warn: mcp "silent": initialize timed out after 45 s (settings.json: mcp.initializeTimeoutMs)\n',
    ]);
  });

  it("a tools/call timeout carries no settings hint (only initialize's wait is a setting)", async () => {
    const c = client(stdio(), { timeoutMs: 200 });

    await expect(c.callTool("echo", { hang: true })).rejects.toThrow(
      /^mcp "fake": tools\/call timed out after 0\.2 s$/,
    );
  });

  it("logs no connecting line when there is no server", async () => {
    const { log, lines } = sink();

    await expect(connectMcpServers({}, { log })).resolves.toEqual([]);

    expect(lines).toEqual([]);
  });

  it("startMcp reads the user mcp.json and spawns its stdio server in cwd", async () => {
    const home = join(dir, "home");
    const { mkdirSync } = await import("node:fs");
    mkdirSync(home);
    writeFileSync(join(home, "mcp.json"), JSON.stringify({ mcpServers: { fake: stdio() } }));

    const connected = await startMcp({
      env: { ...process.env, TREE_AGENT_HOME: home },
      cwd: dir,
      trusted: false,
    });
    clients.push(...connected);

    expect(connected.map((c) => c.name)).toEqual(["fake"]);
    expect(connected[0]?.tools).toHaveLength(2);
    expect(pidsOf(marker)).toHaveLength(1);
  });

  it("startMcp passes initializeTimeoutMs on to every client", async () => {
    const home = join(dir, "home");
    const { mkdirSync } = await import("node:fs");
    mkdirSync(home);
    writeFileSync(join(home, "mcp.json"), JSON.stringify({ mcpServers: { silent: SILENT_STDIO } }));
    const { log, lines } = sink();

    const connected = await startMcp({
      env: { ...process.env, TREE_AGENT_HOME: home },
      cwd: dir,
      trusted: false,
      initializeTimeoutMs: 200,
      log,
    });
    clients.push(...connected);

    expect(lines.filter((l) => l.includes("warn:"))).toEqual([
      '[tree-agent] warn: mcp "silent": initialize timed out after 0.2 s (settings.json: mcp.initializeTimeoutMs)\n',
    ]);
  });
});
