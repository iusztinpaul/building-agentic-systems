import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import { Settings } from "../../src/config/settings.ts";
import type { GateResult } from "../../src/loop/hooks.ts";
import { FakeProvider } from "../../src/provider/fake.ts";
import { builtinRegistry } from "../../src/tools/builtins.ts";
import {
  CODEMODE,
  CODEMODE_PREAMBLE,
  type CodemodeOptions,
  codemodeTool,
  declarations,
  registerCodemode,
  tsType,
} from "../../src/tools/codemode.ts";
import {
  type Exposure,
  type ToolContext,
  type ToolDefinition,
  ToolRegistry,
  type ToolResult,
} from "../../src/tools/registry.ts";
import type { ToolCall } from "../../src/types.ts";
import { toolCtx, useTmpDir } from "../helpers/tmp.ts";

const tmp = useTmpDir("codemode");
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function fake(
  name: string,
  exposure: Exposure = "codemode",
  run: (
    args: Record<string, unknown>,
    ctx: ToolContext,
  ) => Promise<ToolResult> | ToolResult = () => ({
    content: `${name} ran`,
  }),
  o: Partial<ToolDefinition> = {},
): ToolDefinition {
  return {
    name,
    description: `Does ${name}.\nMore detail.`,
    promptSnippet: `does ${name}`,
    parameters: z.object({}).passthrough(),
    exposure,
    isReadOnly: false,
    execute: async (args, ctx) => run(args, ctx),
    ...o,
  };
}

const allow = async (): Promise<GateResult> => ({ run: true });
const LLM = Settings.parse({}).codemode.llm;

function setup(tools: ToolDefinition[], o: Partial<CodemodeOptions> = {}) {
  const registry = new ToolRegistry();
  for (const t of tools) registry.register(t);
  const gated: ToolCall[] = [];
  const options: CodemodeOptions = {
    inlineBudgetTokens: 3_000,
    timeoutMs: 5_000,
    memoryMb: 64,
    maxToolCalls: 50,
    outputMaxBytes: 20_000,
    concurrency: 4,
    llm: LLM,
    provider: new FakeProvider(),
    decisionModel: "test/decision",
    tmpDir: join(tmp(), "tmp"),
    gate: async (call) => {
      gated.push(call);
      return allow();
    },
    ...o,
  };
  registerCodemode(registry, options);
  const script = (source: string, ctx: ToolContext = toolCtx(tmp())) =>
    registry.execute(
      { id: "cm", name: CODEMODE, arguments: { script: source, description: "test" } },
      ctx,
    );
  return { registry, script, gated, options };
}

describe("registerCodemode", () => {
  it("registers nothing with zero codemode tools, and a direct, non-read-only tool with one", () => {
    const none = setup([fake("a", "direct"), fake("b", "deferred")]);
    expect(none.registry.get(CODEMODE)).toBeUndefined();

    const one = setup([fake("a", "codemode")]);
    expect(one.registry.get(CODEMODE)).toMatchObject({ exposure: "direct", isReadOnly: false });
    expect(one.registry.schemasForRequest().map((s) => s.name)).toEqual([CODEMODE]);
  });

  it("describes the sandbox API, llm() included, and declares the codemode tools", () => {
    const t = setup([
      fake("mcp__m__get_doc", "codemode", undefined, {
        inputSchema: {
          type: "object",
          properties: { id: { type: "string" }, limit: { type: "integer" } },
          required: ["id"],
        },
      }),
      fake("direct_one", "direct"),
    ]);

    const description = t.registry.get(CODEMODE)?.description ?? "";

    expect(description).toBe(
      `${CODEMODE_PREAMBLE}\nmcp__m__get_doc(params: {id: string, limit?: number}): Does mcp__m__get_doc.`,
    );
    expect(description).toContain(
      "\n`llm(prompt, {schema?, model?, system?})` calls a language model and returns text or parsed JSON.\n",
    );
  });
});

describe("codemode scripts", () => {
  it("returns exactly the logs and the JSON return value; the tools' results stay in the script", async () => {
    const t = setup([
      fake("count", "codemode", () => ({ content: "SECRET-1", structured: { n: 1 } })),
      fake("label", "codemode", () => ({ content: "SECRET-2", structured: { label: "x" } })),
    ]);

    const result = await t.script(`
      const a = await tools.count();
      const b = await tools.label({ id: 7 });
      console.log("n is", a.n);
      console.log("label", b.label);
      return { ok: a.n === 1, label: b.label };
    `);

    expect(result).toEqual({
      content: 'n is 1\nlabel x\n{"ok":true,"label":"x"}',
      isError: false,
    });
  });

  it("a tool without structured gives the script its content", async () => {
    const t = setup([fake("plain")]);

    expect((await t.script("return await tools.plain()")).content).toBe('"plain ran"');
  });

  it("every tools.x() is a ToolCall with origin codemode through the gate, run with origin codemode", async () => {
    const origins: string[] = [];
    const t = setup([
      fake("probe", "codemode", (args, ctx) => {
        origins.push(ctx.origin);
        return { content: JSON.stringify(args) };
      }),
    ]);

    await t.script("await tools.probe({ q: 1 }); await tools.probe();");

    expect(t.gated).toEqual([
      {
        id: expect.stringMatching(/^codemode-.+\.1$/),
        name: "probe",
        arguments: { q: 1 },
        origin: "codemode",
      },
      {
        id: expect.stringMatching(/^codemode-.+\.2$/),
        name: "probe",
        arguments: {},
        origin: "codemode",
      },
    ]);
    expect(origins).toEqual(["codemode", "codemode"]);
  });

  it("a deny rejects with the reason and a try/catch continues", async () => {
    let ran = false;
    const t = setup(
      [
        fake("danger", "codemode", () => {
          ran = true;
          return { content: "x" };
        }),
        fake("safe"),
      ],
      {
        gate: async (call) =>
          call.name === "danger" ? { run: false, reason: "denied by the user" } : { run: true },
      },
    );

    const result = await t.script(`
      try { await tools.danger(); } catch (e) { console.log("skipped:", e.message); }
      return await tools.safe();
    `);

    expect(result.content).toBe('skipped: Blocked: denied by the user\n"safe ran"');
    expect(ran).toBe(false);
  });

  it("an isError tool result rejects with its content", async () => {
    const t = setup([fake("broken", "codemode", () => ({ content: "it broke", isError: true }))]);

    const result = await t.script("try { await tools.broken(); } catch (e) { return e.message; }");

    expect(result.content).toBe('"it broke"');
  });

  it("a script error is rendered once: `Error: boom`, a bare throw gets the prefix", async () => {
    const t = setup([fake("x")]);

    expect((await t.script('throw new Error("boom")')).content).toBe("Error: boom");
    expect((await t.script('throw "boom"')).content).toBe("Error: boom");
    expect((await t.script('throw new TypeError("bad")')).content).toBe("TypeError: bad");
  });

  it("a non-object argument rejects", async () => {
    const t = setup([fake("x")]);

    const result = await t.script('try { await tools.x("a"); } catch (e) { return e.message; }');

    expect(result.content).toBe('"tools.x() takes one object argument"');
  });

  it("8 parallel calls: at most 4 in flight, all 8 complete", async () => {
    let inFlight = 0;
    let peak = 0;
    const t = setup([
      fake("slow", "codemode", async (args) => {
        peak = Math.max(peak, ++inFlight);
        await sleep(30);
        inFlight--;
        return { content: "", structured: args.i };
      }),
    ]);

    const result = await t.script(
      "return await Promise.all([0,1,2,3,4,5,6,7].map((i) => tools.slow({ i })));",
    );

    expect(result.content).toBe("[0,1,2,3,4,5,6,7]");
    expect(peak).toBe(4);
  });

  it("the 51st tools.x() in one script rejects naming maxToolCalls", async () => {
    const t = setup([fake("tick", "codemode", () => ({ content: "", structured: 1 }))]);

    const result = await t.script(`
      let n = 0;
      for (let i = 0; i < 50; i++) n += await tools.tick();
      try { await tools.tick(); } catch (e) { return [n, e.message]; }
    `);

    expect(JSON.parse(result.content)).toEqual([
      50,
      "tool call limit reached: 50 per script (codemode.maxToolCalls)",
    ]);
    expect(t.gated).toHaveLength(50);
  });

  it("tools has codemode, direct and activated tools; not codemode itself, nor inactive deferred ones", async () => {
    const t = setup([
      fake("cm_tool"),
      fake("direct_tool", "direct"),
      fake("active", "deferred"),
      fake("inactive", "deferred"),
    ]);
    t.registry.activate(["active"]);

    const result = await t.script("return Object.keys(tools)");

    expect(JSON.parse(result.content)).toEqual(["cm_tool", "direct_tool", "active"]);
  });

  it("llm() is defined in a script (task 211); fetch and require still are not", async () => {
    const t = setup([fake("x")]);

    const result = await t.script("return [typeof llm, typeof fetch, typeof require]; ");

    expect(result.content).toBe('["function","undefined","undefined"]');
  });

  it("a timeout is an isError result naming it, and the next script runs", async () => {
    const t = setup([fake("x")], { timeoutMs: 200 });
    const started = Date.now();

    const result = await t.script('console.log("before"); while (true) {}');

    expect(Date.now() - started).toBeLessThan(1_000);
    expect(result).toEqual({
      content: "before\nError: the script timed out after 200 ms (codemode.timeoutMs)",
      isError: true,
    });
    expect(await t.script("return 1")).toEqual({ content: "1", isError: false });
  });

  it("a timed-out script aborts its running tool call's signal", async () => {
    let signal: AbortSignal | undefined;
    const t = setup(
      [
        fake("hang", "codemode", (_args, ctx) => {
          signal = ctx.signal;
          return new Promise(() => {});
        }),
      ],
      { timeoutMs: 150 },
    );

    const result = await t.script("await tools.hang()");

    expect(result.content).toMatch(/timed out/);
    expect(signal?.aborted).toBe(true);
  });

  it("a memory blow-up is an error result, never a crash", async () => {
    const t = setup([fake("x")], { memoryMb: 16 });

    const result = await t.script("const a = []; while (true) a.push(new Array(10000).fill(1));");

    expect(result).toEqual({
      content: "InternalError: out of memory (codemode.memoryMb = 16)",
      isError: true,
    });
  });

  it("middle-truncates a 100 KB output to 20 000 bytes and saves the full text to tmp", async () => {
    const t = setup([fake("x")]);

    const result = await t.script('return "a".repeat(50000) + "MIDDLE" + "z".repeat(50000);');

    const [shown, path] = result.content.split("\n\nFull output: ");
    expect(shown).toMatch(/^"a{9999}\n\n\[\.\.\. 80008 bytes omitted \.\.\.\]\n\nz{9999}"$/);
    expect(path).toMatch(new RegExp(`^${t.options.tmpDir}/.+\\.txt$`));
    const full = readFileSync(path ?? "", "utf8");
    expect(full).toHaveLength(100_008);
    expect(full).toContain("MIDDLE");
  });

  it("an empty run says so", async () => {
    const t = setup([fake("x")]);

    expect(await t.script("const a = 1;")).toEqual({ content: "(no output)", isError: false });
  });

  it("an aborted run stops the script and says so", async () => {
    const controller = new AbortController();
    const t = setup([
      fake("hang", "codemode", () => {
        controller.abort();
        return new Promise(() => {});
      }),
    ]);

    const result = await t.script(
      'try { await tools.hang(); } catch { console.log("caught"); }',
      toolCtx(tmp(), controller.signal),
    );

    expect(result).toEqual({
      content: "Error: the script was stopped: the run was aborted",
      isError: true,
    });
  });
});

describe("searchTools and describeTool", () => {
  const long = (i: number) =>
    fake(`mcp__m__tool_${String(i).padStart(2, "0")}`, "codemode", undefined, {
      description: `Tool number ${i} handles ${"widgets ".repeat(20)}`,
      inputSchema: {
        type: "object",
        properties: { query: { type: "string" }, limit: { type: "integer" } },
        required: ["query"],
      },
    });
  const embeddings = fake("mcp__m__visualize_memory_embeddings", "codemode", undefined, {
    description: "Show a 2D map of the memory's topics (clusters of chunk embeddings).",
  });

  it("inline declarations stop at the 3 000-token budget; searchTools finds one past it", async () => {
    const tools = [...Array.from({ length: 59 }, (_, i) => long(i)), embeddings];
    const t = setup(tools);

    const inline = declarations(t.registry, t.options);
    const lines = inline.split("\n");
    expect(inline.length).toBeLessThanOrEqual(3_000 * 4);
    expect(lines.length - 1).toBeLessThan(60);
    expect(lines.length - 1).toBeGreaterThan(40);
    expect(inline).not.toContain("visualize_memory_embeddings");
    expect(lines.at(-1)).toBe(`(${61 - lines.length} more; find them with searchTools(query))`);

    const found = await t.script('return searchTools("embeddings map");');
    expect(JSON.parse(found.content)[0]).toEqual({
      name: "mcp__m__visualize_memory_embeddings",
      description: "Show a 2D map of the memory's topics (clusters of chunk embeddings).",
    });
    expect(t.gated).toEqual([]); // search activates and gates nothing
    expect(t.registry.activated()).toEqual([]);
  });

  it("describeTool returns name, description and the JSON Schema; an unknown name throws", async () => {
    const t = setup([long(1)]);

    const result = await t.script(`
      const d = describeTool("mcp__m__tool_01");
      try { describeTool("nope"); } catch (e) { return [d, e.message]; }
    `);

    expect(JSON.parse(result.content)).toEqual([
      {
        name: "mcp__m__tool_01",
        description: long(1).description,
        parameters: {
          type: "object",
          properties: { query: { type: "string" }, limit: { type: "integer" } },
          required: ["query"],
        },
      },
      "unknown tool: nope",
    ]);
  });

  it("describeTool turns a built-in's zod parameters into JSON Schema", async () => {
    const t = setup([
      fake("x"),
      fake("typed", "direct", undefined, { parameters: z.object({ path: z.string() }) }),
    ]);

    const result = await t.script('return describeTool("typed").parameters;');

    expect(JSON.parse(result.content)).toMatchObject({
      type: "object",
      properties: { path: { type: "string" } },
      required: ["path"],
    });
  });
});

describe("tsType", () => {
  it("renders JSON Schema as a short TypeScript-like type", () => {
    expect(tsType({ type: "object" })).toBe("{}");
    expect(
      tsType({
        type: "object",
        properties: {
          q: { type: "string" },
          n: { type: "integer" },
          tags: { type: "array", items: { type: "string" } },
          mode: { enum: ["rag", "graph"] },
          when: { anyOf: [{ type: "string" }, { type: "null" }] },
          deep: { type: "object", properties: { a: { type: "object", properties: { b: {} } } } },
        },
        required: ["q"],
      }),
    ).toBe(
      '{q: string, n?: number, tags?: string[], mode?: "rag" | "graph", when?: string | null, deep?: {a?: object}}',
    );
    expect(tsType({ type: "array", items: { type: ["string", "number"] } })).toBe(
      "(string | number)[]",
    );
    expect(tsType(undefined)).toBe("unknown");
    expect(tsType({ const: 3 })).toBe("3");
  });
});

describe("codemodeTool", () => {
  it("its prompt line names the built-ins' result shapes, and scripts get exactly those", async () => {
    const registry = builtinRegistry({ tmpDir: join(tmp(), "tmp") });
    registry.register(fake("x"));
    registerCodemode(registry, { ...setup([]).options, gate: allow });
    writeFileSync(join(tmp(), "a.txt"), "one\ntwo\n");
    const script = `
      const r = await tools.read({path: "a.txt"});
      const b = await tools.bash({command: "echo hi"});
      const w = await tools.write({path: "b.txt", content: "x"});
      const e = await tools.edit({path: "b.txt", oldText: "x", newText: "y"});
      let failed = false;
      try { await tools.bash({command: "exit 3"}); } catch { failed = true; }
      return [Object.keys(r), r.lines, Object.keys(b).sort(), typeof w, typeof e, failed];
    `;

    const result = await registry.execute(
      { id: "cm", name: CODEMODE, arguments: { script, description: "shapes" } },
      toolCtx(tmp()),
    );

    expect(registry.get(CODEMODE)?.promptSnippet).toContain(
      "read returns {path, lines: string[]}, bash {exitCode, stdout, stderr} " +
        "(a non-zero exit rejects), edit and write a status string",
    );
    expect(JSON.parse(result.content)).toEqual([
      ["path", "lines"],
      ["one", "two"],
      ["exitCode", "stderr", "stdout"],
      "string",
      "string",
      true,
    ]);
  });

  it("is not read-only and takes script and description", () => {
    const tool = codemodeTool(new ToolRegistry(), {
      inlineBudgetTokens: 0,
      timeoutMs: 1,
      memoryMb: 1,
      maxToolCalls: 1,
      outputMaxBytes: 1,
      concurrency: 1,
      llm: LLM,
      provider: new FakeProvider(),
      decisionModel: "test/decision",
      tmpDir: "/tmp",
      gate: allow,
    });

    expect(tool.isReadOnly).toBe(false);
    expect(tool.parameters.safeParse({ script: "x" }).success).toBe(false);
    expect(tool.parameters.safeParse({ script: "x", description: "y" }).success).toBe(true);
  });
});
