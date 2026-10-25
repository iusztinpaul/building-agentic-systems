import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import type { Logger } from "../../src/log.ts";
import { type McpCallResult, McpClient, type McpTool } from "../../src/mcp/client.ts";
import type { McpServer } from "../../src/mcp/config.ts";
import {
  exposureFor,
  MAX_DESCRIPTION,
  mcpToolName,
  type RegistrableClient,
  registerMcpTools,
  serverSegment,
  TOOL_NAME,
  toToolResult,
} from "../../src/mcp/register.ts";
import { ToolRegistry } from "../../src/tools/registry.ts";
import { toolCtx, useTmpDir } from "../helpers/tmp.ts";

const tmp = useTmpDir("mcp-register");

const silent = (): Logger & { warns: string[] } => {
  const warns: string[] = [];
  return { debug() {}, info() {}, warn: (m) => warns.push(m), error() {}, warns };
};

function stub(
  name: string,
  tools: McpTool[],
  toolExposure: McpServer["toolExposure"] = {},
  answer: (tool: string, args: Record<string, unknown>) => McpCallResult = (tool, args) => ({
    content: [{ type: "text", text: JSON.stringify({ tool, args }) }],
    isError: false,
  }),
): RegistrableClient & { calls: [string, Record<string, unknown>][] } {
  const calls: [string, Record<string, unknown>][] = [];
  return {
    name,
    server: { command: "x", args: [], env: {}, toolExposure },
    tools,
    connected: true,
    calls,
    async callTool(tool, args = {}) {
      calls.push([tool, args]);
      return answer(tool, args);
    },
  };
}

const tool = (name: string, extra: Partial<McpTool> = {}): McpTool => ({
  name,
  description: `${name} does things.\nSecond line.`,
  inputSchema: { type: "object", properties: { q: { type: "string" } }, required: ["q"] },
  ...extra,
});

function register(...clients: RegistrableClient[]) {
  const registry = new ToolRegistry();
  const log = silent();
  const servers = registerMcpTools(registry, clients, { tmpDir: join(tmp(), "tmp"), log });
  return { registry, servers, log };
}

describe("MCP tool naming", () => {
  it("sanitises the server key: tree-memory-local registers mcp__tree_memory_local__<tool>", () => {
    const { registry } = register(
      stub("tree-memory-local", [tool("search_memory"), tool("visualize_memory_embeddings")]),
    );

    expect(registry.list().map((t) => t.name)).toEqual([
      "mcp__tree_memory_local__search_memory",
      "mcp__tree_memory_local__visualize_memory_embeddings",
    ]);
    expect("mcp__tree_memory_local__visualize_memory_embeddings").toHaveLength(51);
  });

  it("maps uppercase, dots and spaces in the server key to the regex", () => {
    expect(serverSegment("My.Server 2")).toBe("my_server_2");
    expect(mcpToolName("a-b", "c")).toBe("mcp__a_b__c");
  });

  it("collapses a run of other characters to one _ and trims the edges: a--b registers mcp__a_b__x", () => {
    expect(serverSegment("a--b")).toBe("a_b");
    expect(serverSegment("-a__b-")).toBe("a_b");
    const { registry } = register(stub("a--b", [tool("x")]));

    expect(registry.list().map((t) => t.name)).toEqual(["mcp__a_b__x"]);
  });

  it.each(["", "--", "é", "9lives"])(
    "rejects server key %j (no usable segment): one warn, 0 tools, still listed",
    (key) => {
      const { registry, servers, log } = register(stub(key, [tool("x"), tool("y")]));

      expect(registry.list()).toEqual([]);
      expect(servers).toMatchObject([{ name: key, tools: 0, direct: 0, deferred: 0, codemode: 0 }]);
      expect(log.warns).toEqual([
        `mcp "${key}": tools not registered: server key "${key}" has no usable name segment`,
      ]);
    },
  );

  it("skips an empty tool name with a warn, keeping the server's other tools", () => {
    const { registry, servers, log } = register(stub("srv", [tool(""), tool("ok")]));

    expect(registry.list().map((t) => t.name)).toEqual(["mcp__srv__ok"]);
    expect(servers[0]?.tools).toBe(1);
    expect(log.warns).toEqual(['mcp "srv": tool "" not registered: tool name is empty']);
  });

  it("two keys on one segment: the first keeps its tools, the second registers none, one warn naming both", () => {
    const { registry, servers, log } = register(
      stub("a-b", [tool("x")]),
      stub("A_B", [tool("x"), tool("y")]),
    );

    expect(registry.list().map((t) => t.name)).toEqual(["mcp__a_b__x"]);
    expect(servers.map((s) => [s.name, s.tools])).toEqual([
      ["a-b", 1],
      ["A_B", 0],
    ]);
    expect(log.warns).toEqual([
      'mcp "A_B": tools not registered: servers "a-b" and "A_B" both map to "a_b"',
    ]);
  });

  it("registers only names matching ^[a-z][a-z0-9_]*$ within 64 chars, warning about the rest", () => {
    const long = `t${"x".repeat(60)}`;
    const { registry, servers, log } = register(
      stub("srv", [tool("ok_tool"), tool(long), tool("Bad-Name"), tool("ok_tool")]),
    );

    expect(registry.list().map((t) => t.name)).toEqual(["mcp__srv__ok_tool"]);
    for (const t of registry.list()) {
      expect(t.name).toMatch(TOOL_NAME);
      expect(t.name.length).toBeLessThanOrEqual(64);
    }
    expect(servers[0]?.tools).toBe(1);
    expect(log.warns).toEqual([
      `mcp "srv": tool "${long}" not registered: "mcp__srv__${long}" is 71 chars, over the 64-char cap`,
      'mcp "srv": tool "Bad-Name" not registered: "mcp__srv__Bad-Name" does not match ^[a-z][a-z0-9_]*$',
      'mcp "srv": tool "ok_tool" not registered: tool "mcp__srv__ok_tool" is already registered',
    ]);
  });
});

describe("MCP tool exposure", () => {
  const globs = {
    "search_*": "direct",
    "get_*": "codemode",
    "admin_*": "deferred",
    "delete_*": "hidden",
  } as const;

  it("applies the first matching glob, codemode when none matches, and skips hidden tools", () => {
    const { registry, servers } = register(
      stub(
        "s",
        [
          tool("search_notes"),
          tool("get_note"),
          tool("admin_reindex"),
          tool("delete_entry"),
          tool("summarise"),
        ],
        globs,
      ),
    );

    const exposures = Object.fromEntries(registry.list().map((t) => [t.name, t.exposure]));
    expect(exposures).toEqual({
      mcp__s__search_notes: "direct",
      mcp__s__get_note: "codemode",
      mcp__s__admin_reindex: "deferred",
      mcp__s__summarise: "codemode",
    });
    expect(registry.get("mcp__s__delete_entry")).toBeUndefined();
    expect(servers[0]).toMatchObject({ name: "s", tools: 4, direct: 1, deferred: 1, codemode: 2 });
  });

  it("first match wins in declaration order", () => {
    expect(exposureFor("search_all", { "search_*": "direct", "*_all": "hidden" })).toBe("direct");
    expect(exposureFor("search_all", { "*_all": "hidden", "search_*": "direct" })).toBe("hidden");
  });

  it("anchors the glob and escapes regex characters", () => {
    expect(exposureFor("xsearch_a", { "search_*": "direct" })).toBe("codemode");
    expect(exposureFor("a.b", { "a.b": "direct" })).toBe("direct");
    expect(exposureFor("axb", { "a.b": "direct" })).toBe("codemode");
    expect(exposureFor("anything", { "*": "deferred" })).toBe("deferred");
  });

  it("a hidden tool is not callable from codemode either", async () => {
    const { registry } = register(stub("s", [tool("delete_entry")], globs));

    const result = await registry.execute(
      { id: "c", name: "mcp__s__delete_entry", arguments: {} },
      { ...toolCtx(tmp()), origin: "codemode" },
    );

    expect(result).toEqual({ content: "Unknown tool: mcp__s__delete_entry", isError: true });
  });
});

describe("MCP tool definitions", () => {
  it("readOnlyHint: true → isReadOnly; absent or truthy-but-not-true → not", () => {
    const { registry } = register(
      stub("s", [
        tool("a", { annotations: { readOnlyHint: true } }),
        tool("b"),
        tool("c", { annotations: { readOnlyHint: "true" } }),
      ]),
    );

    expect(registry.list().map((t) => t.isReadOnly)).toEqual([true, false, false]);
  });

  it("cuts a 2 100-char description at 2 048 and takes the first line as the prompt snippet", () => {
    const { registry } = register(
      stub("s", [tool("long", { description: `First line.\n${"d".repeat(2_088)}` })]),
    );
    const t = registry.get("mcp__s__long");

    expect(MAX_DESCRIPTION).toBe(2_048);
    expect(t?.description).toHaveLength(2_048);
    expect(t?.promptSnippet).toBe("First line.");
  });

  it("falls back to a snippet naming the tool and server without a description", () => {
    const { registry } = register(stub("s", [tool("bare", { description: undefined })]));

    expect(registry.get("mcp__s__bare")).toMatchObject({
      description: "",
      promptSnippet: "bare (MCP server s)",
    });
  });

  it("sends the server's inputSchema to the provider, without $schema", () => {
    const { registry } = register(
      stub(
        "s",
        [
          tool("search_x", {
            inputSchema: {
              $schema: "http://json-schema.org/draft-07/schema#",
              type: "object",
              properties: { query: { type: "string" }, top_k: { type: "integer" } },
              required: ["query"],
            },
          }),
        ],
        { "search_*": "direct" },
      ),
    );

    expect(registry.schemasForRequest()).toEqual([
      {
        name: "mcp__s__search_x",
        description: "search_x does things.\nSecond line.",
        parameters: {
          type: "object",
          properties: { query: { type: "string" }, top_k: { type: "integer" } },
          required: ["query"],
        },
      },
    ]);
  });

  it("passes any object to the server under the original tool name; a non-object is rejected", async () => {
    const client = stub("tree-memory-local", [tool("search_memory")], { "search_*": "direct" });
    const { registry } = register(client);

    const result = await registry.execute(
      { id: "c", name: "mcp__tree_memory_local__search_memory", arguments: { query: "x", n: 2 } },
      toolCtx(tmp()),
    );

    expect(client.calls).toEqual([["search_memory", { query: "x", n: 2 }]]);
    expect(result.structured).toEqual({ tool: "search_memory", args: { query: "x", n: 2 } });
    const bad = await registry.execute(
      {
        id: "d",
        name: "mcp__tree_memory_local__search_memory",
        arguments: "x" as unknown as Record<string, unknown>,
      },
      toolCtx(tmp()),
    );
    expect(bad.isError).toBe(true);
    expect(client.calls).toHaveLength(1);
  });

  it("a thrown client error (timeout, dropped server) is an isError result", async () => {
    const client = stub("s", [tool("t")], { "*": "direct" }, () => {
      throw new Error('mcp "s": tools/call t timed out after 60000 ms');
    });
    const { registry } = register(client);

    const result = await registry.execute(
      { id: "c", name: "mcp__s__t", arguments: {} },
      toolCtx(tmp()),
    );

    expect(result).toEqual({
      content: 'mcp "s": tools/call t timed out after 60000 ms',
      isError: true,
    });
  });

  it("reports a server with no tools as 0 / 0 / 0 / 0", () => {
    const { servers } = register(stub("down", []));

    expect(servers).toEqual([
      expect.objectContaining({ name: "down", tools: 0, direct: 0, deferred: 0, codemode: 0 }),
    ]);
  });
});

describe("MCP tool results", () => {
  const text = (t: string) => ({ type: "text", text: t });

  it("concatenates the text content with newlines and keeps isError", () => {
    const result = toToolResult({ content: [text("a"), text("b")], isError: true }, tmp());

    expect(result).toEqual({ content: "a\nb", isError: true, structured: "a\nb" });
  });

  it("structured: structuredContent when present, else the parsed text, else the text", () => {
    const withStructured = toToolResult(
      { content: [text('{"x":1}')], structuredContent: { y: 2 }, isError: false },
      tmp(),
    );
    const parsed = toToolResult({ content: [text('{"x":1}')], isError: false }, tmp());
    const plain = toToolResult({ content: [text("hello")], isError: false }, tmp());

    expect(withStructured.structured).toEqual({ y: 2 });
    expect(parsed.structured).toEqual({ x: 1 });
    expect(plain.structured).toBe("hello");
  });

  it("unwraps FastMCP's {result: <json string>} to the parsed value; anything else stays as is", () => {
    const structured = (structuredContent: Record<string, unknown>) =>
      toToolResult({ content: [text("t")], structuredContent, isError: false }, tmp()).structured;

    expect(structured({ result: '{"parents":[1]}' })).toEqual({ parents: [1] });
    expect(structured({ result: "null" })).toBeNull();
    expect(structured({ result: "plain text" })).toEqual({ result: "plain text" });
    expect(structured({ result: '{"a":1}', other: 1 })).toEqual({ result: '{"a":1}', other: 1 });
    expect(structured({ result: { a: 1 } })).toEqual({ result: { a: 1 } });
  });

  it("names non-text content and falls back to structuredContent or (no output)", () => {
    const image = toToolResult(
      {
        content: [{ type: "image", data: "…", mimeType: "image/png" }, text("caption")],
        isError: false,
      },
      tmp(),
    );
    const onlyStructured = toToolResult(
      { content: [], structuredContent: { a: 1 }, isError: false },
      tmp(),
    );
    const empty = toToolResult({ content: [], isError: false }, tmp());

    expect(image.content).toBe("[image content]\ncaption");
    expect(image.structured).toBe("caption");
    expect(onlyStructured.content).toBe('{"a":1}');
    expect(empty.content).toBe("(no output)");
  });

  it("a 25 KB result reaches the model middle-truncated with the tmp path, the full text on disk", async () => {
    const big = `HEAD${"m".repeat(25 * 1024)}TAIL`;
    const client = stub("s", [tool("dump")], { "*": "direct" }, () => ({
      content: [text(big)],
      isError: false,
    }));
    const { registry } = register(client);

    const result = await registry.execute(
      { id: "c", name: "mcp__s__dump", arguments: {} },
      toolCtx(tmp()),
    );

    expect(result.content.startsWith("HEAD")).toBe(true);
    expect(result.content).toMatch(/\[\.\.\. \d+ bytes omitted \.\.\.\]/);
    const path = /Full output: (.+\.txt)$/.exec(result.content)?.[1] ?? "";
    expect(path.startsWith(join(tmp(), "tmp"))).toBe(true);
    expect(result.content).toContain(`TAIL\n\nFull output: ${path}`);
    expect(Buffer.byteLength(result.content)).toBeLessThan(21 * 1024);
    expect(readFileSync(path, "utf8")).toBe(big);
    expect(result.structured).toBe(big);
  });

  it("names an unwritable tmp dir instead of throwing", () => {
    const blocked = join(tmp(), "file");
    writeFileSync(blocked, "x"); // a file where the directory should be

    const result = toToolResult(
      { content: [text("z".repeat(30 * 1024))], isError: false },
      blocked,
    );

    expect(result.content).toMatch(/could not save full output: /);
    expect(existsSync(join(blocked, "x"))).toBe(false);
  });
});

describe("MCP registration against the stdio fixture", () => {
  const SERVER = fileURLToPath(new URL("../fixtures/fake-mcp-server.mjs", import.meta.url));
  let client: McpClient | undefined;
  afterEach(async () => {
    await client?.close();
    client = undefined;
  });

  it("registers listed tools and calls them through the real client", async () => {
    client = new McpClient(
      "fake-server",
      {
        command: process.execPath,
        args: [SERVER],
        env: {},
        toolExposure: { echo: "direct", read_note: "deferred" },
      },
      { log: silent() },
    );
    await client.listTools();
    const { registry, servers } = register(client);

    expect(registry.list().map((t) => [t.name, t.exposure, t.isReadOnly])).toEqual([
      ["mcp__fake_server__echo", "direct", false],
      ["mcp__fake_server__read_note", "deferred", true],
    ]);
    expect(servers[0]).toMatchObject({ tools: 2, direct: 1, deferred: 1, codemode: 0 });
    expect(servers[0]?.client.connected).toBe(true);
    const note = await registry.execute(
      { id: "c", name: "mcp__fake_server__read_note", arguments: { id: "7" } },
      { ...toolCtx(tmp()), origin: "codemode" },
    );
    expect(note).toEqual({
      content: '{"id":"7","body":"note 7"}',
      isError: false,
      structured: { id: "7", body: "note 7" },
    });
  });

  it("a script gets the parsed JSON of a {result: <json string>} wrapper; the model gets the text as sent", async () => {
    client = new McpClient(
      "fake-server",
      { command: process.execPath, args: [SERVER], env: {}, toolExposure: {} },
      { log: silent() },
    );
    await client.listTools();
    const { registry } = register(client);
    const echo = (structured: unknown) =>
      registry.execute(
        { id: "c", name: "mcp__fake_server__echo", arguments: { structured } },
        { ...toolCtx(tmp()), origin: "codemode" },
      );

    const wrapped = { result: '{"parents":[{"id":"p1"}]}' };
    expect(await echo(wrapped)).toEqual({
      content: JSON.stringify(wrapped),
      isError: false,
      structured: { parents: [{ id: "p1" }] },
    });
    expect((await echo({ result: "plain text" })).structured).toEqual({ result: "plain text" });
    expect((await echo({ result: "{}", other: 1 })).structured).toEqual({ result: "{}", other: 1 });
  });
});
