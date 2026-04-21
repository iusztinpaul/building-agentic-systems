#!/usr/bin/env bun
// Tiny in-repo MCP server used by the integration test. Registers three
// deterministic tools — `echo`, `adder`, `failing` — and serves over stdio.
// Run via `bun run tests/integration/fixtures/stub-mcp-server.ts`; the
// integration test spawns this exact path.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const server = new McpServer({ name: "stub", version: "0.0.1" });

server.registerTool(
  "echo",
  {
    description: "Echo the text argument verbatim",
    inputSchema: {
      text: z.string().describe("text to return"),
    },
  },
  async ({ text }) => ({
    content: [{ type: "text", text }],
  }),
);

server.registerTool(
  "adder",
  {
    description: "Add two numbers",
    inputSchema: {
      a: z.number(),
      b: z.number(),
    },
  },
  async ({ a, b }) => ({
    content: [{ type: "text", text: String(a + b) }],
  }),
);

server.registerTool(
  "failing",
  {
    description: "Always returns an error result",
    inputSchema: {},
  },
  async () => ({
    content: [{ type: "text", text: "this tool always fails" }],
    isError: true,
  }),
);

const transport = new StdioServerTransport();
await server.connect(transport);
