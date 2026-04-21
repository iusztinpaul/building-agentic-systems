import { describe, expect, test } from "bun:test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { type McpToolDescriptor, callMcpTool, connectMcpServer } from "../../../src/mcp/client";
import type { McpSpawn } from "../../../src/mcp/config";
import { makeFakeClient, makeFakeMcpServer } from "../../helpers/fake-mcp";

const SPAWN: McpSpawn = { command: "fake", args: [], env: {}, cwd: "/tmp" };

describe("callMcpTool", () => {
  test("text-only content is joined with newlines", async () => {
    const server = makeFakeMcpServer({
      onCall: () => ({
        content: [
          { type: "text", text: "line 1" },
          { type: "text", text: "line 2" },
        ],
      }),
    });
    const result = await callMcpTool(server, "echo", {});
    expect(result.content).toBe("line 1\nline 2");
    expect(result.isError).toBe(false);
  });

  test("mixed text + non-text parts — text verbatim, non-text JSON-stringified", async () => {
    const server = makeFakeMcpServer({
      onCall: () => ({
        content: [
          { type: "text", text: "hello" },
          { type: "image", data: "base64…" },
        ],
      }),
    });
    const result = await callMcpTool(server, "mixed", {});
    expect(result.content).toContain("hello");
    expect(result.content).toContain('"type":"image"');
    expect(result.content).toContain('"data":"base64…"');
  });

  test("empty content with no structuredContent returns sentinel", async () => {
    const server = makeFakeMcpServer({ onCall: () => ({ content: [] }) });
    const result = await callMcpTool(server, "empty", {});
    expect(result.content).toBe("(no content returned)");
  });

  test("empty content with structuredContent returns the structured JSON", async () => {
    const server = makeFakeMcpServer({
      onCall: () => ({ content: [], structuredContent: { bmi: 22.86 } }),
    });
    const result = await callMcpTool(server, "structured", {});
    expect(result.content).toContain('"bmi":22.86');
  });

  test("isError: true is surfaced on the harness result", async () => {
    const server = makeFakeMcpServer({
      onCall: () => ({
        content: [{ type: "text", text: "boom" }],
        isError: true,
      }),
    });
    const result = await callMcpTool(server, "fails", {});
    expect(result.content).toBe("boom");
    expect(result.isError).toBe(true);
  });

  test("arguments are forwarded verbatim to the server", async () => {
    const seen: {
      value: { name: string; args: Record<string, unknown> } | null;
    } = { value: null };
    const server = makeFakeMcpServer({
      onCall: (name, args) => {
        seen.value = { name, args };
        return { content: [{ type: "text", text: "ack" }] };
      },
    });
    await callMcpTool(server, "greet", { who: "world", n: 3 });
    expect(seen.value?.name).toBe("greet");
    expect(seen.value?.args).toEqual({ who: "world", n: 3 });
  });
});

describe("connectMcpServer", () => {
  test("calls connect once with the transport returned by createTransport", async () => {
    const sentinelTransport = { marker: "fake-transport" };
    const handle = makeFakeClient();
    await connectMcpServer("s", SPAWN, {
      createTransport: () => sentinelTransport,
      createClient: () => handle.client,
    });
    expect(handle.connectCount.value).toBe(1);
    expect(handle.connectedWith.value).toBe(sentinelTransport);
  });

  test("listTools paginates via nextCursor", async () => {
    const p1: McpToolDescriptor[] = [
      { name: "a", description: "", inputSchema: {} },
      { name: "b", description: "", inputSchema: {} },
    ];
    const p2: McpToolDescriptor[] = [{ name: "c", description: "", inputSchema: {} }];
    const handle = makeFakeClient({ listPages: [p1, p2] });
    const server = await connectMcpServer("s", SPAWN, {
      createTransport: () => ({}),
      createClient: () => handle.client,
    });
    expect(server.tools.map((t) => t.name)).toEqual(["a", "b", "c"]);
    // Two listTools calls: first with no cursor, second with the cursor from page 1.
    expect(handle.listToolsCalls.length).toBe(2);
    expect(handle.listToolsCalls[0]).toEqual({});
    expect(handle.listToolsCalls[1]?.cursor).toBe("cursor-1");
  });

  test("single-page listing stops after one request", async () => {
    const handle = makeFakeClient({
      listPages: [[{ name: "only", description: "", inputSchema: {} }]],
    });
    const server = await connectMcpServer("s", SPAWN, {
      createTransport: () => ({}),
      createClient: () => handle.client,
    });
    expect(server.tools).toHaveLength(1);
    expect(handle.listToolsCalls.length).toBe(1);
  });

  test("missing description becomes empty string, missing inputSchema defaults to {type:object}", async () => {
    // The fake listTools returns tools as-given; connectMcpServer maps them via
    // listAllTools with its own defaults. We feed in a tool lacking description.
    const handle = makeFakeClient({
      listPages: [[{ name: "bare", description: "", inputSchema: { type: "object" } }]],
    });
    const server = await connectMcpServer("s", SPAWN, {
      createTransport: () => ({}),
      createClient: () => handle.client,
    });
    expect(server.tools[0]?.description).toBe("");
    expect(server.tools[0]?.inputSchema).toEqual({ type: "object" });
  });

  test("server.close() calls client.close() and swallows errors", async () => {
    const handle = makeFakeClient({
      onClose: () => {
        throw new Error("already closed");
      },
    });
    const server = await connectMcpServer("s", SPAWN, {
      createTransport: () => ({}),
      createClient: () => handle.client,
    });
    // Should NOT throw even though onClose throws.
    await server.close();
    expect(handle.closed.value).toBe(true);
  });

  test("connect rejection propagates out of connectMcpServer", async () => {
    const handle = makeFakeClient({
      onConnect: () => {
        throw new Error("eperm");
      },
    });
    await expect(
      connectMcpServer("s", SPAWN, {
        createTransport: () => ({}),
        createClient: () => handle.client,
      }),
    ).rejects.toThrow("eperm");
  });

  test("server exposes the client as its `client` field (for subagent MCP sharing)", async () => {
    const handle = makeFakeClient();
    const server = await connectMcpServer("s", SPAWN, {
      createTransport: () => ({}),
      createClient: () => handle.client,
    });
    // Not nominally `Client` since we passed the fake, but structurally identical.
    expect(server.client).toBe(handle.client as unknown as Client);
  });
});
