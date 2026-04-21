import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { resolve } from "node:path";
import type { McpServer } from "../../src/mcp/client";
import { callMcpTool, connectMcpServer } from "../../src/mcp/client";
import type { McpSpawn } from "../../src/mcp/config";

// Full stdio round-trip against an in-repo MCP server. The fixture runs under
// `bun run`, so the test has no external dependency beyond bun itself — no
// MongoDB, no tree-memory, no network. We go through the production
// connectMcpServer path (no DI), so this also covers the StdioClientTransport
// wiring that the unit tests skip.

const STUB_PATH = resolve(import.meta.dir, "fixtures/stub-mcp-server.ts");

const SPAWN: McpSpawn = {
  command: "bun",
  args: ["run", STUB_PATH],
  env: process.env as Record<string, string>,
  cwd: process.cwd(),
};

let server: McpServer;

beforeAll(async () => {
  server = await connectMcpServer("stub", SPAWN);
});

afterAll(async () => {
  if (server) await server.close();
});

describe("connectMcpServer against the stub stdio server", () => {
  test("discovers all three stub tools via listTools", () => {
    const names = server.tools.map((t) => t.name).sort();
    expect(names).toEqual(["adder", "echo", "failing"]);
  });

  test("each tool has a description and an inputSchema", () => {
    for (const t of server.tools) {
      expect(typeof t.description).toBe("string");
      expect(t.inputSchema).toBeTruthy();
      expect((t.inputSchema as { type?: string }).type).toBe("object");
    }
  });

  test("callMcpTool round-trips a text result", async () => {
    const result = await callMcpTool(server, "echo", { text: "hi there" });
    expect(result.isError).toBe(false);
    expect(result.content).toBe("hi there");
  });

  test("callMcpTool surfaces a structured numeric response as text", async () => {
    const result = await callMcpTool(server, "adder", { a: 2, b: 3 });
    expect(result.isError).toBe(false);
    expect(result.content).toBe("5");
  });

  test("callMcpTool propagates isError: true", async () => {
    const result = await callMcpTool(server, "failing", {});
    expect(result.isError).toBe(true);
    expect(result.content).toContain("fails");
  });
});
