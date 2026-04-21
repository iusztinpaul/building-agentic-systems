import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import type { McpSpawn } from "./config";

// A connected MCP server + its discovered tool list. `close()` terminates the
// subprocess and frees the transport.

export interface McpToolDescriptor {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export interface McpServer {
  name: string;
  client: Client;
  tools: McpToolDescriptor[];
  close: () => Promise<void>;
}

const CLIENT_INFO = { name: "tree", version: "0.1.0" };

export async function connectMcpServer(name: string, spawn: McpSpawn): Promise<McpServer> {
  const transport = new StdioClientTransport({
    command: spawn.command,
    args: spawn.args,
    env: spawn.env,
    cwd: spawn.cwd,
    // Merge the server's stderr into our stderr so import errors / tracebacks
    // from the Python side surface in the harness terminal.
    stderr: "inherit",
  });

  const client = new Client(CLIENT_INFO);
  await client.connect(transport);

  const tools = await listAllTools(client);
  return {
    name,
    client,
    tools,
    close: async () => {
      try {
        await client.close();
      } catch {
        // ignore — server may already be gone
      }
    },
  };
}

async function listAllTools(client: Client): Promise<McpToolDescriptor[]> {
  const out: McpToolDescriptor[] = [];
  let cursor: string | undefined;
  do {
    const page = await client.listTools(cursor ? { cursor } : {});
    for (const t of page.tools) {
      out.push({
        name: t.name,
        description: t.description ?? "",
        inputSchema: (t.inputSchema ?? { type: "object" }) as Record<string, unknown>,
      });
    }
    cursor = page.nextCursor;
  } while (cursor);
  return out;
}

export async function callMcpTool(
  server: McpServer,
  toolName: string,
  args: Record<string, unknown>,
): Promise<{ content: string; isError: boolean }> {
  const result = await server.client.callTool({ name: toolName, arguments: args });

  // content is an array of parts; we flatten text parts and stringify anything else.
  const parts = (result.content ?? []) as Array<Record<string, unknown>>;
  const pieces: string[] = [];
  for (const p of parts) {
    if (p.type === "text" && typeof p.text === "string") {
      pieces.push(p.text);
    } else {
      pieces.push(JSON.stringify(p));
    }
  }

  // structuredContent (optional) is for machine consumption — we surface it only
  // when there was no textual content, so the LLM still gets something useful.
  if (pieces.length === 0 && result.structuredContent) {
    pieces.push(JSON.stringify(result.structuredContent));
  }

  return {
    content: pieces.join("\n") || "(no content returned)",
    isError: result.isError === true,
  };
}
