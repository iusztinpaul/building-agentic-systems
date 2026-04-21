import { describe, expect, test } from "bun:test";
import {
  MCP_TOOL_PREFIX,
  classifyMcpTool,
  mcpServersToTools,
  mcpToolToHarnessTool,
} from "../../../src/mcp/adapter";
import type { McpServer, McpToolDescriptor } from "../../../src/mcp/client";
import { makeFakeMcpServer } from "../../helpers/fake-mcp";

describe("classifyMcpTool", () => {
  test.each<[string, boolean]>([
    ["ingest_url", true],
    ["ingest_file", true],
    ["ingest_conversation", true],
    ["write_value", true],
    ["create_document", true],
    ["delete_entry", true],
    ["update_user", true],
    ["upsert_record", true],
    ["remove_item", true],
    ["add_tag", true],
    ["set_config", true],
    ["append_log", true],
    ["push_metric", true],
    ["modify_schema", true],
    ["edit_inline", true],
  ])("%s is destructive", (name, destructive) => {
    const { isReadOnly, isDestructive } = classifyMcpTool(name);
    expect(isDestructive).toBe(destructive);
    expect(isReadOnly).toBe(!destructive);
  });

  test.each<[string]>([
    ["query_memory"],
    ["search_memory"],
    ["deep_search_memory"],
    ["list_things"],
    ["get_status"],
    ["read_file"],
  ])("%s is read-only", (name) => {
    const { isReadOnly, isDestructive } = classifyMcpTool(name);
    expect(isReadOnly).toBe(true);
    expect(isDestructive).toBe(false);
  });
});

describe("MCP_TOOL_PREFIX", () => {
  test("matches the Claude-Code convention", () => {
    expect(MCP_TOOL_PREFIX).toBe("mcp__");
  });
});

describe("mcpToolToHarnessTool", () => {
  const descriptor: McpToolDescriptor = {
    name: "search_memory",
    description: "Vector + text search with graph expansion",
    inputSchema: {
      type: "object",
      properties: { query: { type: "string" }, top_k: { type: "integer" } },
      required: ["query"],
    },
  };
  const server = makeFakeMcpServer({ name: "tree-memory", tools: [descriptor] });

  test("prefixes the tool name with mcp__<server>__", () => {
    const tool = mcpToolToHarnessTool(server, descriptor);
    expect(tool.name).toBe("mcp__tree-memory__search_memory");
  });

  test("preserves the server-provided JSON Schema as parametersJsonSchema", () => {
    const tool = mcpToolToHarnessTool(server, descriptor);
    expect(tool.parametersJsonSchema).toBe(descriptor.inputSchema);
  });

  test("classifies read-only by name", () => {
    const tool = mcpToolToHarnessTool(server, descriptor);
    expect(tool.isReadOnly).toBe(true);
    expect(tool.isDestructive).toBe(false);
  });

  test("classifies destructive by name", () => {
    const d: McpToolDescriptor = {
      name: "ingest_conversation",
      description: "",
      inputSchema: { type: "object" },
    };
    const tool = mcpToolToHarnessTool(server, d);
    expect(tool.isDestructive).toBe(true);
    expect(tool.isReadOnly).toBe(false);
  });

  test("falls back to a generated description when the server sends an empty one", () => {
    const d: McpToolDescriptor = {
      name: "foo",
      description: "",
      inputSchema: { type: "object" },
    };
    const tool = mcpToolToHarnessTool(server, d);
    expect(tool.description).toContain("foo");
    expect(tool.description).toContain("tree-memory");
  });

  test("call() dispatches to the server's callTool with the right args", async () => {
    const captured: {
      value: { name: string; args: Record<string, unknown> } | null;
    } = { value: null };
    const srv = makeFakeMcpServer({
      name: "tree-memory",
      tools: [descriptor],
      onCall: (name, args) => {
        captured.value = { name, args };
        return { content: [{ type: "text", text: "ok" }] };
      },
    });
    const tool = mcpToolToHarnessTool(srv, descriptor);
    const result = await tool.call(
      { query: "hi", top_k: 5 },
      { cwd: "/tmp", signal: new AbortController().signal },
    );
    expect(captured.value).not.toBeNull();
    expect(captured.value?.name).toBe("search_memory");
    expect(captured.value?.args).toEqual({ query: "hi", top_k: 5 });
    expect(result.content).toBe("ok");
    expect(result.isError).toBe(false);
  });
});

describe("mcpServersToTools", () => {
  test("empty iterable returns no tools", () => {
    const tools = mcpServersToTools([] as McpServer[]);
    expect(tools).toHaveLength(0);
  });

  test("flattens tools from a single server", () => {
    const server = makeFakeMcpServer({
      name: "a",
      tools: [
        { name: "t1", description: "", inputSchema: {} },
        { name: "t2", description: "", inputSchema: {} },
        { name: "t3", description: "", inputSchema: {} },
      ],
    });
    const tools = mcpServersToTools([server]);
    expect(tools.map((t) => t.name)).toEqual(["mcp__a__t1", "mcp__a__t2", "mcp__a__t3"]);
  });

  test("namespaces tools across multiple servers", () => {
    const a = makeFakeMcpServer({
      name: "first",
      tools: [{ name: "query_memory", description: "", inputSchema: {} }],
    });
    const b = makeFakeMcpServer({
      name: "second",
      tools: [{ name: "list_docs", description: "", inputSchema: {} }],
    });
    const tools = mcpServersToTools([a, b]);
    expect(tools.map((t) => t.name)).toEqual([
      "mcp__first__query_memory",
      "mcp__second__list_docs",
    ]);
  });
});
