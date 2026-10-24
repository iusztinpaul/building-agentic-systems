import { describe, expect, it } from "vitest";
import { z } from "zod";
import { type Exposure, type ToolDefinition, ToolRegistry } from "../../src/tools/registry.ts";
import { toolCtx } from "../helpers/tmp.ts";

function tool(name: string, exposure: Exposure = "direct"): ToolDefinition {
  return {
    name,
    description: `${name} description`,
    promptSnippet: `${name} snippet`,
    parameters: z.object({ path: z.string(), limit: z.number().optional() }),
    exposure,
    isReadOnly: false,
    async execute(params) {
      return { content: `${name} ran with ${JSON.stringify(params)}` };
    },
  };
}

function registry(...tools: ToolDefinition[]): ToolRegistry {
  const r = new ToolRegistry();
  for (const t of tools) r.register(t);
  return r;
}

const ctx = toolCtx("/tmp");

describe("ToolRegistry", () => {
  it("throws on a duplicate name", () => {
    const r = registry(tool("read"));

    expect(() => r.register(tool("read"))).toThrow(/"read" is already registered/);
  });

  it("lists tools in registration order, filtered by exposure", () => {
    const r = registry(
      tool("a"),
      tool("b", "deferred"),
      tool("c", "codemode"),
      tool("d", "hidden"),
      tool("e"),
    );

    expect(r.list().map((t) => t.name)).toEqual(["a", "b", "c", "d", "e"]);
    expect(r.list("direct").map((t) => t.name)).toEqual(["a", "e"]);
    expect(r.list("deferred").map((t) => t.name)).toEqual(["b"]);
    expect(r.list("codemode").map((t) => t.name)).toEqual(["c"]);
    expect(r.list("hidden").map((t) => t.name)).toEqual(["d"]);
  });

  it("sends only direct tools until a deferred one is activated", () => {
    const r = registry(tool("a"), tool("b", "deferred"), tool("c", "codemode"));

    expect(r.schemasForRequest().map((s) => s.name)).toEqual(["a"]);

    r.activate(["b"]);

    expect(r.activated()).toEqual(["b"]);
    expect(r.schemasForRequest().map((s) => s.name)).toEqual(["a", "b"]);
  });

  it("forgets every activation on deactivateAll() (a new session)", () => {
    const r = registry(tool("a"), tool("b", "deferred"));
    r.activate(["b"]);

    r.deactivateAll();

    expect(r.activated()).toEqual([]);
    expect(r.schemasForRequest().map((s) => s.name)).toEqual(["a"]);
  });

  it("never activates codemode, hidden, direct or unknown tools", () => {
    const r = registry(tool("a"), tool("c", "codemode"), tool("d", "hidden"));

    r.activate(["a", "c", "d", "nope"]);

    expect(r.activated()).toEqual([]);
    expect(r.declared().map((t) => t.name)).toEqual(["a"]);
  });

  it("converts parameters to a JSON Schema that keeps optional params optional", () => {
    const [schema] = registry(tool("a")).schemasForRequest();

    expect(schema).toEqual({
      name: "a",
      description: "a description",
      parameters: {
        type: "object",
        properties: { path: { type: "string" }, limit: { type: "number" } },
        required: ["path"],
        additionalProperties: false,
      },
    });
  });

  it("answers a missing required param with the zod issue list as an error result", async () => {
    const r = registry(tool("a"));

    const result = await r.execute({ id: "1", name: "a", arguments: { limit: "x" } }, ctx);

    expect(result).toEqual({
      content: [
        "Invalid arguments for a:",
        "  - path: Required",
        "  - limit: Expected number, received string",
      ].join("\n"),
      isError: true,
    });
  });

  it("runs a valid call with the parsed params", async () => {
    const r = registry(tool("a"));

    const result = await r.execute({ id: "1", name: "a", arguments: { path: "x" } }, ctx);

    expect(result).toEqual({ content: 'a ran with {"path":"x"}' });
  });

  it("turns a thrown error into an error result", async () => {
    const r = registry({
      ...tool("boom"),
      async execute() {
        throw new Error("ENOENT: no such file");
      },
    });

    const result = await r.execute({ id: "1", name: "boom", arguments: { path: "x" } }, ctx);

    expect(result).toEqual({ content: "ENOENT: no such file", isError: true });
  });

  it("turns an asynchronous rejection into an error result", async () => {
    const r = registry({
      ...tool("late"),
      execute: () =>
        new Promise((_, reject) => setTimeout(() => reject(new Error("late failure")), 10)),
    });

    const result = await r.execute({ id: "1", name: "late", arguments: { path: "x" } }, ctx);

    expect(result).toEqual({ content: "late failure", isError: true });
  });

  it("refuses unknown, hidden, codemode and unactivated deferred tools to the model", async () => {
    const r = registry(tool("b", "deferred"), tool("c", "codemode"), tool("d", "hidden"));

    for (const name of ["nope", "b", "c", "d"]) {
      const result = await r.execute({ id: "1", name, arguments: { path: "x" } }, ctx);
      expect(result).toEqual({ content: `Unknown tool: ${name}`, isError: true });
    }
    r.activate(["b"]);
    const activated = await r.execute({ id: "1", name: "b", arguments: { path: "x" } }, ctx);
    expect(activated.isError).toBeUndefined();
  });

  it("lets a codemode script call every tool except hidden ones", async () => {
    const r = registry(tool("b", "deferred"), tool("c", "codemode"), tool("d", "hidden"));
    const script = { ...ctx, origin: "codemode" as const };

    const results = await Promise.all(
      ["b", "c", "d"].map((name) => r.execute({ id: "1", name, arguments: { path: "x" } }, script)),
    );

    expect(results.map((res) => res.isError ?? false)).toEqual([false, false, true]);
  });
});
