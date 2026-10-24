// An in-process streamable-HTTP MCP server for the client tests, on
// 127.0.0.1 and port 0. `/json` answers each request with one JSON
// body; `/sse` answers with an SSE stream (a comment, a server
// notification, then the response). With `sessionId`, the initialize
// response carries `Mcp-Session-Id` and later requests without it
// get 400; expire() makes the session answer 404.

import { createServer, type IncomingHttpHeaders } from "node:http";
import type { AddressInfo } from "node:net";

export interface RecordedRequest {
  method: string;
  path: string;
  headers: IncomingHttpHeaders;
  // biome-ignore lint/suspicious/noExplicitAny: assertions read arbitrary JSON-RPC fields
  body: any;
}

export interface FakeMcpHttp {
  url(path: "/json" | "/sse"): string;
  requests: RecordedRequest[];
  expire(): void;
  close(): Promise<void>;
}

const TOOLS = [
  { name: "echo", description: "Echo the arguments back.", inputSchema: { type: "object" } },
  {
    name: "read_note",
    description: "Read a note.",
    inputSchema: { type: "object" },
    annotations: { readOnlyHint: true },
  },
];

// biome-ignore lint/suspicious/noExplicitAny: a JSON-RPC frame
function answer(message: any): Record<string, unknown> {
  const { method, params } = message;
  if (method === "initialize") {
    return {
      result: {
        protocolVersion: params.protocolVersion,
        capabilities: { tools: {} },
        serverInfo: { name: "fake-http", version: "1.0.0" },
      },
    };
  }
  if (method === "tools/list") {
    return params?.cursor === "p2"
      ? { result: { tools: [TOOLS[1]] } }
      : { result: { tools: [TOOLS[0]], nextCursor: "p2" } };
  }
  if (method === "tools/call") {
    const text = JSON.stringify(params.arguments ?? {});
    return { result: { content: [{ type: "text", text }], structuredContent: params.arguments } };
  }
  return { error: { code: -32601, message: `Method not found: ${method}` } };
}

export async function startFakeMcpHttp(options: { sessionId?: string } = {}): Promise<FakeMcpHttp> {
  const requests: RecordedRequest[] = [];
  let expired = false;
  const server = createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = raw ? JSON.parse(raw) : undefined;
    const path = req.url ?? "/";
    requests.push({ method: req.method ?? "", path, headers: req.headers, body });
    const session = options.sessionId;
    if (req.method === "DELETE") return res.writeHead(200).end();
    if (session && body?.method !== "initialize") {
      if (req.headers["mcp-session-id"] !== session) return res.writeHead(400).end("no session");
      if (expired) return res.writeHead(404).end("session expired");
    }
    if (body?.id === undefined) return res.writeHead(202).end(); // notification or response
    const frame = JSON.stringify({ jsonrpc: "2.0", id: body.id, ...answer(body) });
    const headers: Record<string, string> = {};
    if (session && body.method === "initialize") {
      headers["mcp-session-id"] = session;
      expired = false;
    }
    if (path === "/sse") {
      res.writeHead(200, { ...headers, "content-type": "text/event-stream" });
      res.write(": keep-alive\n\n");
      const note = { jsonrpc: "2.0", method: "notifications/message", params: { level: "info" } };
      res.write(`event: message\ndata: ${JSON.stringify(note)}\n\n`);
      res.end(`event: message\nid: 1\ndata: ${frame}\n\n`);
    } else {
      res.writeHead(200, { ...headers, "content-type": "application/json" }).end(frame);
    }
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return {
    url: (path) => `http://127.0.0.1:${port}${path}`,
    requests,
    expire: () => {
      expired = true;
    },
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
}
