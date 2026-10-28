import { describe, expect, it } from "vitest";
import { z } from "zod";
import { type Exposure, type ToolDefinition, ToolRegistry } from "../../src/tools/registry.ts";
import {
  rankTools,
  registerToolSearch,
  TOOL_SEARCH_DESCRIPTION,
  tokenize,
  toolSearchTool,
} from "../../src/tools/tool-search.ts";
import { toolCtx } from "../helpers/tmp.ts";

function tool(name: string, description: string, exposure: Exposure = "deferred"): ToolDefinition {
  return {
    name,
    description,
    promptSnippet: description.split("\n")[0] ?? "",
    parameters: z.object({}).passthrough(),
    exposure,
    isReadOnly: false,
    async execute() {
      return { content: name };
    },
  };
}

const MEMORY = [
  tool("mcp__m__search_memory", "Retrieve the passages that answer a question.", "direct"),
  tool(
    "mcp__m__visualize_memory_structure",
    "Show how the memory is organised: documents and their chunks.\nOptionally narrowed to a query.",
  ),
  tool("mcp__m__visualize_memory_embeddings", "Show a 2D map of the memory's topics."),
  tool("mcp__m__ingest_url", "Add web content to memory.", "codemode"),
];

function registry(...tools: ToolDefinition[]): ToolRegistry {
  const r = new ToolRegistry();
  for (const t of tools) r.register(t);
  return r;
}

const search = (r: ToolRegistry, args: Record<string, unknown>) =>
  r.execute({ id: "s", name: "tool_search", arguments: args }, toolCtx("/tmp"));

describe("registerToolSearch", () => {
  it("registers nothing with zero deferred tools", () => {
    const r = registry(tool("a", "A", "direct"), tool("b", "B", "codemode"));

    expect(registerToolSearch(r)).toBe(false);
    expect(r.get("tool_search")).toBeUndefined();
  });

  it("registers a direct, read-only tool_search with one deferred tool", () => {
    const r = registry(tool("a", "A", "deferred"));

    expect(registerToolSearch(r)).toBe(true);
    expect(r.get("tool_search")).toMatchObject({ exposure: "direct", isReadOnly: true });
  });

  it("sends the fixed §8.1 description, which names no tool", () => {
    const r = registry(...MEMORY);
    registerToolSearch(r);

    const schema = r.schemasForRequest().find((s) => s.name === "tool_search");
    expect(TOOL_SEARCH_DESCRIPTION).toBe(
      "Search tools that are not yet available by describing what you need. " +
        "Matching tools become callable on your next turn.",
    );
    expect(schema?.description).toBe(TOOL_SEARCH_DESCRIPTION);
    expect(schema?.parameters).toMatchObject({
      properties: { query: { type: "string" }, limit: { type: "integer" } },
      required: ["query"],
    });
  });
});

describe("tool_search", () => {
  it('"visualize memory" activates both visualize tools and answers name — first line', async () => {
    const r = registry(...MEMORY);
    registerToolSearch(r);

    const result = await search(r, { query: "visualize memory" });

    expect(result.content).toBe(
      [
        "mcp__m__visualize_memory_embeddings — Show a 2D map of the memory's topics.",
        "mcp__m__visualize_memory_structure — Show how the memory is organised: documents and their chunks.",
      ].join("\n"),
    );
    expect(r.activated().sort()).toEqual([
      "mcp__m__visualize_memory_embeddings",
      "mcp__m__visualize_memory_structure",
    ]);
    expect(r.schemasForRequest().map((s) => s.name)).toEqual([
      "mcp__m__search_memory",
      "mcp__m__visualize_memory_structure",
      "mcp__m__visualize_memory_embeddings",
      "tool_search",
    ]);
  });

  it("searches only deferred tools: direct and codemode ones are never returned", async () => {
    const r = registry(...MEMORY);
    registerToolSearch(r);

    const result = await search(r, { query: "ingest url search memory question" });

    expect(result.content).not.toContain("ingest_url");
    expect(result.content).not.toContain("search_memory");
    expect(r.activated()).not.toContain("mcp__m__ingest_url");
  });

  it("caps the answer at limit (default 5)", async () => {
    const many = Array.from({ length: 8 }, (_, i) => tool(`mcp__s__export_${i}`, "Export rows."));
    const r = registry(...many);
    registerToolSearch(r);

    expect((await search(r, { query: "export" })).content.split("\n")).toHaveLength(5);
    expect((await search(r, { query: "export", limit: 2 })).content.split("\n")).toHaveLength(2);
  });

  it("activates nothing when no tool matches", async () => {
    const r = registry(...MEMORY);
    registerToolSearch(r);

    const result = await search(r, { query: "kubernetes" });

    expect(result).toEqual({ content: 'No tools match "kubernetes".', structured: [] });
    expect(r.activated()).toEqual([]);
  });

  it("rejects an empty query and a limit outside 1..20", async () => {
    const r = registry(...MEMORY);
    r.register(toolSearchTool(r));

    expect((await search(r, { query: "" })).isError).toBe(true);
    expect((await search(r, { query: "x", limit: 0 })).isError).toBe(true);
    expect((await search(r, { query: "x", limit: 21 })).isError).toBe(true);
  });

  it("sees a deferred tool registered after tool_search (the index is the registry's current set)", async () => {
    const r = registry(tool("mcp__a__first", "First."));
    registerToolSearch(r);
    r.register(tool("mcp__b__late_tool", "Arrived later."));

    expect((await search(r, { query: "late" })).content).toBe("mcp__b__late_tool — Arrived later.");
  });
});

describe("rankTools (BM25)", () => {
  it("ranks a tool whose name matches above one whose description matches", () => {
    const byDescription = tool("mcp__s__dump_rows", "Export the table as a CSV file.");
    const byName = tool("mcp__s__export_table", "Write the rows somewhere useful for later.");

    expect(rankTools([byDescription, byName], "export", 5).map((t) => t.name)).toEqual([
      "mcp__s__export_table",
      "mcp__s__dump_rows",
    ]);
  });

  it("weighs rare terms above common ones (idf)", () => {
    const tools = [
      tool("mcp__s__a", "memory memory notes"),
      tool("mcp__s__b", "memory graph"),
      tool("mcp__s__c", "memory list"),
    ];

    expect(rankTools(tools, "memory graph", 5)[0]?.name).toBe("mcp__s__b");
  });

  it("keeps registration order on a tie and drops non-matching tools", () => {
    const tools = [
      tool("mcp__s__x", "same words"),
      tool("mcp__s__y", "same words"),
      tool("mcp__s__z", "other"),
    ];

    expect(rankTools(tools, "words", 5).map((t) => t.name)).toEqual(["mcp__s__x", "mcp__s__y"]);
  });

  it("returns nothing for an empty tool set or a query without terms", () => {
    expect(rankTools([], "x", 5)).toEqual([]);
    expect(rankTools([tool("mcp__s__x", "x")], "--- ___", 5)).toEqual([]);
  });

  it("tokenizes on [^a-z0-9]+ and _, lowercased", () => {
    expect(tokenize("mcp__tree_memory_local__Visualize-Memory 2D!")).toEqual([
      "mcp",
      "tree",
      "memory",
      "local",
      "visualize",
      "memory",
      "2d",
    ]);
  });
});
