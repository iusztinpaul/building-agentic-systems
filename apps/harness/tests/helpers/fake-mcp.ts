import type { Client } from "@modelcontextprotocol/sdk/client/index.js";
import type { McpServer, McpToolDescriptor } from "../../src/mcp/client";

// Fake MCP server for tests. Exposes only the four methods production code
// touches (connect / listTools / callTool / close) and captures enough state
// that tests can assert on how the harness drove the SDK. The call sites cast
// the fake once (`as unknown as Client`); the rest of the code treats it as a
// full MCP Client.

export interface McpCallResult {
  content?: Array<{ type: string; text?: string; [k: string]: unknown }>;
  isError?: boolean;
  structuredContent?: unknown;
}

export interface FakeMcpOptions {
  name?: string;
  tools?: McpToolDescriptor[];
  // Pages returned in order from listTools. Last page has no nextCursor.
  // If absent, listTools returns a single page with `tools` (or empty).
  listPages?: McpToolDescriptor[][];
  // Scripted tool responses. Missing keys fall back to a plain text "(fake ok)".
  onCall?: (
    toolName: string,
    args: Record<string, unknown>,
  ) => Promise<McpCallResult> | McpCallResult;
  // Throw inside to exercise connect failure. Otherwise resolves.
  onConnect?: () => Promise<void> | void;
  // Throw inside to exercise close failure (connectMcpServer swallows it).
  onClose?: () => Promise<void> | void;
}

export interface FakeClientHandle {
  client: Client;
  calls: Array<{ name: string; arguments: Record<string, unknown> }>;
  connectedWith: { value: unknown };
  connectCount: { value: number };
  closed: { value: boolean };
  listToolsCalls: Array<{ cursor?: string } | undefined>;
}

export function makeFakeClient(opts: FakeMcpOptions = {}): FakeClientHandle {
  const calls: FakeClientHandle["calls"] = [];
  const connectedWith: FakeClientHandle["connectedWith"] = { value: undefined };
  const connectCount: FakeClientHandle["connectCount"] = { value: 0 };
  const closed: FakeClientHandle["closed"] = { value: false };
  const listToolsCalls: FakeClientHandle["listToolsCalls"] = [];

  const pages: McpToolDescriptor[][] = opts.listPages ?? (opts.tools ? [opts.tools] : [[]]);
  let pageIdx = 0;

  const fake = {
    async connect(transport: unknown): Promise<void> {
      connectCount.value += 1;
      connectedWith.value = transport;
      if (opts.onConnect) await opts.onConnect();
    },

    async listTools(params?: { cursor?: string }): Promise<{
      tools: Array<{ name: string; description?: string; inputSchema?: unknown }>;
      nextCursor?: string;
    }> {
      listToolsCalls.push(params);
      const page = pages[pageIdx] ?? [];
      const hasNext = pageIdx < pages.length - 1;
      pageIdx += 1;
      return {
        tools: page.map((t) => ({
          name: t.name,
          description: t.description || undefined,
          inputSchema: t.inputSchema,
        })),
        nextCursor: hasNext ? `cursor-${pageIdx}` : undefined,
      };
    },

    async callTool(params: {
      name: string;
      arguments: Record<string, unknown>;
    }): Promise<McpCallResult> {
      calls.push({ name: params.name, arguments: params.arguments });
      if (opts.onCall) return await opts.onCall(params.name, params.arguments);
      return { content: [{ type: "text", text: "(fake ok)" }] };
    },

    async close(): Promise<void> {
      closed.value = true;
      if (opts.onClose) await opts.onClose();
    },
  };

  return {
    client: fake as unknown as Client,
    calls,
    connectedWith,
    connectCount,
    closed,
    listToolsCalls,
  };
}

// Convenience wrapper for tests that operate on a built McpServer (adapter,
// callMcpTool). Skips the connect handshake since there's no transport.
export function makeFakeMcpServer(opts: FakeMcpOptions = {}): McpServer {
  const handle = makeFakeClient(opts);
  return {
    name: opts.name ?? "fake",
    client: handle.client,
    tools: opts.tools ?? [],
    close: async () => {
      await handle.client.close();
    },
  };
}
