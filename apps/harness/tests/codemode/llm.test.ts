import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import {
  DEFAULT_MAX_TOKENS,
  DEFAULT_SYSTEM,
  type LlmCallEvent,
  zodFromJsonSchema,
} from "../../src/codemode/llm.ts";
import { Settings } from "../../src/config/settings.ts";
import type { GateResult } from "../../src/loop/hooks.ts";
import { FakeProvider, type FakeScript, fakeDone, fakeModel } from "../../src/provider/fake.ts";
import { OpenRouterProvider } from "../../src/provider/openrouter.ts";
import type { ModelInfo, Provider, StreamEvent, TextRequest } from "../../src/provider/types.ts";
import { builtinRegistry } from "../../src/tools/builtins.ts";
import { CODEMODE, type CodemodeOptions, registerCodemode } from "../../src/tools/codemode.ts";
import type { ToolContext, ToolDefinition } from "../../src/tools/registry.ts";
import type { ToolCall, Usage } from "../../src/types.ts";
import { fakeFetch, jsonResponse } from "../helpers/http.ts";
import { toolCtx, useTmpDir } from "../helpers/tmp.ts";

const tmp = useTmpDir("llm");
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const DECISION = "test/decision";
const LLM = Settings.parse({}).codemode.llm;

// A codemode-only tool, so codemode registers; `delay` keeps it in flight.
function memoryTool(run: () => Promise<unknown> | unknown = () => []): ToolDefinition {
  return {
    name: "mcp__memory__search",
    description: "Search the memory.",
    promptSnippet: "search the memory",
    parameters: z.object({}).passthrough(),
    exposure: "codemode",
    isReadOnly: true,
    execute: async () => {
      const structured = await run();
      return { content: JSON.stringify(structured), structured };
    },
  };
}

interface Setup {
  provider?: Provider;
  script?: FakeScript;
  llm?: Partial<typeof LLM>;
  gate?: (call: ToolCall) => Promise<GateResult>;
  tool?: ToolDefinition;
  concurrency?: number;
}

function setup(o: Setup = {}) {
  const provider = o.provider ?? new FakeProvider(o.script);
  const registry = builtinRegistry({ tmpDir: join(tmp(), "tmp") });
  registry.register(o.tool ?? memoryTool());
  const gated: ToolCall[] = [];
  const calls: { event: LlmCallEvent; pricing: ModelInfo["pricing"] }[] = [];
  const options: CodemodeOptions = {
    inlineBudgetTokens: 3_000,
    timeoutMs: 5_000,
    memoryMb: 64,
    maxToolCalls: 50,
    outputMaxBytes: 20_000,
    concurrency: o.concurrency ?? 4,
    llm: { ...LLM, ...o.llm },
    provider,
    decisionModel: DECISION,
    onLlmCall: (event, pricing) => calls.push({ event, pricing }),
    tmpDir: join(tmp(), "tmp"),
    gate: async (call) => {
      gated.push(call);
      return o.gate ? o.gate(call) : { run: true };
    },
  };
  registerCodemode(registry, options);
  const run = (source: string, ctx: ToolContext = toolCtx(tmp())) =>
    registry.execute(
      { id: "cm", name: CODEMODE, arguments: { script: source, description: "t" } },
      ctx,
    );
  return { provider, registry, run, gated, calls };
}

// Counts completeText calls in flight (and anything else sharing `live`).
class SlowProvider extends FakeProvider {
  live = { now: 0, peak: 0 };
  readonly delayMs: number;

  constructor(script: FakeScript, delayMs = 20) {
    super(script);
    this.delayMs = delayMs;
  }

  override async completeText(req: TextRequest) {
    this.live.peak = Math.max(this.live.peak, ++this.live.now);
    await sleep(this.delayMs);
    this.live.now--;
    return super.completeText(req);
  }
}

const read = (id: string, path: string): ToolCall => ({ id, name: "read", arguments: { path } });
const toolTurn = (...calls: ToolCall[]): StreamEvent[] => [fakeDone("", calls)];
const textTurn = (text: string): StreamEvent[] => [{ type: "text_delta", text }, fakeDone(text)];

describe("llm() at depth 1", () => {
  it("reaches completeText with no tools, the default system, 2 048 tokens and the decision model", async () => {
    const t = setup({ script: { texts: ["an answer"] } });

    const result = await t.run('return await llm("x")');

    expect(result).toEqual({ content: '"an answer"', isError: false });
    expect((t.provider as FakeProvider).textRequests).toEqual([
      { model: DECISION, system: DEFAULT_SYSTEM, input: "x", maxOutputTokens: DEFAULT_MAX_TOKENS },
    ]);
    expect(DEFAULT_SYSTEM).toBe("You are a sub-task worker. Answer only what is asked.");
    expect(DEFAULT_MAX_TOKENS).toBe(2_048);
    expect((t.provider as FakeProvider).requests).toHaveLength(0); // no sub-loop stream
  });

  it("takes model, system and maxTokens from the call, and codemode.llm.model over decisionModel", async () => {
    const t = setup({ script: { texts: ["a", "b"] }, llm: { model: "cheap/model" } });

    await t.run(
      'await llm("x"); return await llm("y", {model: "other/m", system: "Be terse.", maxTokens: 64})',
    );

    expect((t.provider as FakeProvider).textRequests).toEqual([
      { model: "cheap/model", system: DEFAULT_SYSTEM, input: "x", maxOutputTokens: 2_048 },
      { model: "other/m", system: "Be terse.", input: "y", maxOutputTokens: 64 },
    ]);
  });

  it("clamps maxTokens to the model's output cap", async () => {
    const t = setup({
      script: { texts: ["a"], models: [fakeModel(DECISION, { maxOutputTokens: 100 })] },
    });

    await t.run('return await llm("x", {maxTokens: 5000})');

    expect((t.provider as FakeProvider).textRequests[0]?.maxOutputTokens).toBe(100);
  });

  it("with a schema reaches complete<T> and returns the parsed object", async () => {
    const t = setup({ script: { answers: [{ facts: ["a", "b"] }] } });

    const result = await t.run(`
      const answer = await llm("merge", { schema: { type: "object",
        properties: { facts: { type: "array", items: { type: "string" } } }, required: ["facts"] } });
      return answer.facts.length;`);

    expect(result).toEqual({ content: "2", isError: false });
    const [req] = (t.provider as FakeProvider).structuredRequests;
    expect(req).toMatchObject({
      model: DECISION,
      system: DEFAULT_SYSTEM,
      input: "merge",
      name: "llm",
    });
    expect((t.provider as FakeProvider).textRequests).toHaveLength(0);
  });

  it("a schema mismatch after complete's one retry throws inside the script with the parse message", async () => {
    const models = {
      data: [
        {
          id: DECISION,
          context_length: 1000,
          top_provider: {},
          pricing: { prompt: "0", completion: "0" },
        },
      ],
    };
    const http = fakeFetch((call) =>
      call.url.endsWith("/models")
        ? jsonResponse(models)
        : jsonResponse({
            choices: [{ message: { role: "assistant", content: '{"facts":"not a list"}' } }],
            usage: { prompt_tokens: 10, completion_tokens: 5 },
          }),
    );
    const t = setup({ provider: new OpenRouterProvider({ apiKey: "k", fetch: http.fetch }) });

    const result = await t.run(`
      try {
        await llm("merge", { schema: { type: "object",
          properties: { facts: { type: "array", items: { type: "string" } } }, required: ["facts"] } });
        return "parsed";
      } catch (e) { return e.message; }`);

    expect(JSON.parse(result.content)).toMatch(/^llm: facts: Expected array, received string/);
    const posts = http.calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(2); // the answer and the one retry
    expect(posts[0]?.body.response_format.json_schema.schema).toMatchObject({
      type: "object",
      required: ["facts"],
      additionalProperties: false,
    });
    // Both attempts are spent.
    expect(t.calls.map((c) => c.event.usage)).toEqual([{ inputTokens: 20, outputTokens: 10 }]);
  });

  it("ignores {tools: true} at depth 1: a plain completion, no sub-loop", async () => {
    const t = setup({ script: { texts: ["from the prompt"] } });

    const result = await t.run('return await llm("Summarise CHANGELOG.md", {tools: true})');

    expect(result).toEqual({ content: '"from the prompt"', isError: false });
    expect((t.provider as FakeProvider).requests).toHaveLength(0);
    expect(t.calls.map((c) => c.event.depth)).toEqual([1]);
  });

  it("rejects a prompt that is not a string, sending nothing", async () => {
    const t = setup({ script: { texts: ["never"] } });

    const result = await t.run(
      "try { await llm({ a: 1 }) } catch (e) { return e.name + ': ' + e.message }",
    );

    expect(JSON.parse(result.content)).toBe("TypeError: llm(prompt): prompt must be a string");
    expect((t.provider as FakeProvider).textRequests).toHaveLength(0);
  });

  it("rejects an unknown option by name", async () => {
    const t = setup();

    const result = await t.run(
      'try { await llm("x", {temperature: 0}) } catch (e) { return e.message }',
    );

    expect(JSON.parse(result.content)).toMatch(
      /^llm\(\): options: Unrecognized key\(s\) in object: 'temperature'/,
    );
  });
});

describe("llm() budgets", () => {
  it("the 17th call in one script throws naming maxCalls; the catch keeps the partial result", async () => {
    const t = setup({
      script: { texts: Array.from({ length: 17 }, (_, i) => `s${i}`) },
      llm: { maxCalls: 16 },
    });

    const result = await t.run(`
      const out = [];
      try { for (let i = 0; i < 100; i++) out.push(await llm("slice " + i)); }
      catch (e) { return { n: out.length, error: e.message }; }`);

    expect(JSON.parse(result.content)).toEqual({
      n: 16,
      error: "llm budget exceeded: maxCalls=16 per script (codemode.llm.maxCalls)",
    });
    expect((t.provider as FakeProvider).textRequests).toHaveLength(16);
  });

  it("the default maxCalls is 64: the 65th call throws naming it", async () => {
    const t = setup({ script: { texts: Array.from({ length: 65 }, (_, i) => `s${i}`) } });

    const result = await t.run(`
      let n = 0;
      try { for (let i = 0; i < 100; i++) { await llm("slice " + i); n++; } }
      catch (e) { return { n, error: e.message }; }`);

    expect(JSON.parse(result.content)).toEqual({
      n: 64,
      error: "llm budget exceeded: maxCalls=64 per script (codemode.llm.maxCalls)",
    });
  });

  it("the budget is per script: the next script starts at zero", async () => {
    const t = setup({ script: { texts: ["a", "b", "c"] }, llm: { maxCalls: 2 } });

    const first = await t.run(
      'await llm("1"); await llm("2"); try { await llm("3") } catch (e) { return e.message }',
    );
    const second = await t.run('return await llm("4")');

    expect(JSON.parse(first.content)).toMatch(/maxCalls=2/);
    expect(second.content).toBe('"c"');
  });

  it("a 200 001-char prompt throws naming maxInputChars and sends nothing", async () => {
    const t = setup({ script: { texts: ["never"] } });

    const result = await t.run(
      'try { await llm("x".repeat(200001)) } catch (e) { return e.message }',
    );

    expect(JSON.parse(result.content)).toBe(
      "llm budget exceeded: maxInputChars=200000 per call (this one has 200001; codemode.llm.maxInputChars)",
    );
    expect((t.provider as FakeProvider).textRequests).toHaveLength(0);
    expect(t.calls).toHaveLength(0);
  });

  it("scripted usage summing past 400 000 tokens throws naming maxTotalTokens", async () => {
    const usage: Usage = { inputTokens: 150_000, outputTokens: 60_000 };
    const t = setup({ script: { texts: ["a", "b", "c"].map((text) => ({ text, usage })) } });

    const result = await t.run(`
      const out = [];
      try { for (let i = 0; i < 3; i++) out.push(await llm("q" + i)); }
      catch (e) { return { out, error: e.message }; }`);

    expect(JSON.parse(result.content)).toEqual({
      out: ["a", "b"],
      error:
        "llm budget exceeded: maxTotalTokens=400000 per script (used 420000; codemode.llm.maxTotalTokens)",
    });
    expect((t.provider as FakeProvider).textRequests).toHaveLength(2);
  });

  it("Promise.all over 8 llm() calls keeps at most 4 in flight and answers all 8", async () => {
    const provider = new SlowProvider({ texts: Array.from({ length: 8 }, (_, i) => `a${i}`) });
    const t = setup({ provider });

    const result = await t.run(
      "return await Promise.all([0,1,2,3,4,5,6,7].map((i) => llm('q' + i)))",
    );

    expect(JSON.parse(result.content)).toEqual(["a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7"]);
    expect(provider.live.peak).toBe(4);
  });

  it("tools.x() and llm() share the one concurrency semaphore", async () => {
    const provider = new SlowProvider({ texts: ["a", "b", "c", "d"] });
    const tool = memoryTool(async () => {
      provider.live.peak = Math.max(provider.live.peak, ++provider.live.now);
      await sleep(20);
      provider.live.now--;
      return [];
    });
    const t = setup({ provider, tool });

    await t.run(`
      const s = () => tools.mcp__memory__search({});
      return await Promise.all([s(), llm("1"), s(), llm("2"), s(), llm("3"), s(), llm("4")]);`);

    expect(provider.live.peak).toBe(4);
  });

  it("never goes through the permission gate", async () => {
    const t = setup({ script: { texts: ["a"] } });

    await t.run('return await llm("x")');

    expect(t.gated).toEqual([]);
  });
});

describe("llm() at depth 2 (codemode.llm.depth: 2)", () => {
  it("a {tools: true} call runs a sub-loop that can read the file it was handed; codemode is not in its tools", async () => {
    writeFileSync(join(tmp(), "CHANGELOG.md"), "## 1.0\n- first release\n");
    const t = setup({
      llm: { depth: 2 },
      script: { streams: [toolTurn(read("r1", "CHANGELOG.md")), textTurn("One release: 1.0.")] },
    });

    const result = await t.run('return await llm("Summarise CHANGELOG.md", {tools: true})');

    expect(result).toEqual({ content: '"One release: 1.0."', isError: false });
    const fake = t.provider as FakeProvider;
    const names = fake.requests[0]?.tools.map((s) => s.name);
    expect(names).toEqual(["read", "bash", "edit", "write"]);
    expect(names).not.toContain(CODEMODE);
    expect(fake.requests[0]?.model).toBe(DECISION);
    expect(fake.requests[0]?.context).toEqual({
      system: DEFAULT_SYSTEM,
      messages: [{ role: "user", content: "Summarise CHANGELOG.md" }],
    });
    const toolMessage = fake.requests[1]?.context.messages[2];
    expect(toolMessage).toMatchObject({ role: "tool", name: "read", isError: false });
    expect(toolMessage?.role === "tool" && toolMessage.content).toContain("first release");
    expect(t.gated.map((c) => c.name)).toEqual(["read"]); // the same gate
    expect(t.calls.map((c) => c.event)).toEqual([
      {
        type: "llm_call",
        depth: 2,
        model: DECISION,
        usage: { inputTokens: 200, outputTokens: 40 },
      },
    ]);
  });

  it("a non-read-only tool inside the sub-loop goes through the gate, and a deny is answered to the sub-model", async () => {
    const write: ToolCall = { id: "w1", name: "write", arguments: { path: "x.md", content: "x" } };
    const t = setup({
      llm: { depth: 2 },
      gate: async (call) =>
        call.name === "write" ? { run: false, reason: "no writes" } : { run: true },
      script: { streams: [toolTurn(write), textTurn("could not write")] },
    });

    const result = await t.run('return await llm("write x.md", {tools: true})');

    expect(result.content).toBe('"could not write"');
    expect(t.gated).toEqual([write]);
    const answered = (t.provider as FakeProvider).requests[1]?.context.messages[2];
    expect(answered).toMatchObject({ role: "tool", content: "Blocked: no writes", isError: true });
  });

  it("the sub-loop stops at maxSubTurns and the call throws naming it", async () => {
    const t = setup({
      llm: { depth: 2, maxSubTurns: 2 },
      script: {
        streams: [toolTurn(read("r1", "a.md")), toolTurn(read("r2", "b.md")), textTurn("never")],
      },
    });

    const result = await t.run(
      'try { await llm("loop", {tools: true}) } catch (e) { return e.message }',
    );

    expect(JSON.parse(result.content)).toBe(
      "llm() sub-loop stopped at maxSubTurns=2 without an answer (codemode.llm.maxSubTurns)",
    );
    expect((t.provider as FakeProvider).requests).toHaveLength(2);
  });

  it("without {tools: true} a call stays a plain depth-1 completion", async () => {
    const t = setup({ llm: { depth: 2 }, script: { texts: ["plain"] } });

    const result = await t.run('return await llm("x")');

    expect(result.content).toBe('"plain"');
    expect((t.provider as FakeProvider).requests).toHaveLength(0);
    expect(t.calls.map((c) => c.event.depth)).toEqual([1]);
  });

  it("a call that would need depth 3 returns an error to the script", async () => {
    const t = setup({ llm: { depth: 2 }, script: { texts: ["never"] } });
    // A script already running one level down (as a nested codemode would).
    const nested = { ...toolCtx(tmp()), depth: 1 };

    const result = await t.run(
      'try { await llm("x", {tools: true}) } catch (e) { return e.message }',
      nested,
    );

    expect(JSON.parse(result.content)).toBe(
      "llm() at depth 3 is beyond codemode.llm.depth = 2; nested llm() calls are not supported",
    );
    expect((t.provider as FakeProvider).textRequests).toHaveLength(0);
  });

  it("with depth 1 configured, a script one level down cannot call llm() at all", async () => {
    const t = setup({ script: { texts: ["never"] } });

    const result = await t.run('try { await llm("x") } catch (e) { return e.message }', {
      ...toolCtx(tmp()),
      depth: 1,
    });

    expect(JSON.parse(result.content)).toMatch(
      /^llm\(\) at depth 2 is beyond codemode.llm.depth = 1/,
    );
  });

  it("schema and tools together are refused with a hint", async () => {
    const t = setup({ llm: { depth: 2 } });

    const result = await t.run(
      'try { await llm("x", {tools: true, schema: {type: "object"}}) } catch (e) { return e.message }',
    );

    expect(JSON.parse(result.content)).toMatch(/^llm\(\): schema and tools cannot be combined/);
  });
});

describe("llm_call events", () => {
  it("every call reports depth, model and usage, with the model's pricing", async () => {
    const pricing = { prompt: 0.000001, completion: 0.000002 };
    const t = setup({
      script: {
        texts: ["abcd"],
        answers: [{ ok: true }],
        models: [fakeModel(DECISION, { pricing }), fakeModel("other/m")],
      },
    });

    await t.run(`await llm("12345678"); return await llm("q", {model: "other/m",
      schema: {type: "object", properties: {ok: {type: "boolean"}}, required: ["ok"]}})`);

    expect(t.calls).toEqual([
      {
        event: {
          type: "llm_call",
          depth: 1,
          model: DECISION,
          usage: { inputTokens: 2, outputTokens: 1 },
        },
        pricing,
      },
      {
        event: {
          type: "llm_call",
          depth: 1,
          model: "other/m",
          usage: { inputTokens: 1, outputTokens: 3 },
        },
        pricing: fakeModel("other/m").pricing,
      },
    ]);
  });

  it("an unknown model fails before anything is sent and reports no call", async () => {
    const t = setup({ script: { texts: ["never"], models: [fakeModel(DECISION)] } });

    const result = await t.run(
      'try { await llm("x", {model: "nope/m"}) } catch (e) { return e.message }',
    );

    expect(JSON.parse(result.content)).toMatch(/unknown model "nope\/m"/);
    expect((t.provider as FakeProvider).textRequests).toHaveLength(0);
    expect(t.calls).toHaveLength(0);
  });

  it("a provider error still reports the call, without usage", async () => {
    const t = setup({ script: { texts: [new Error("upstream 500")] } });

    const result = await t.run('try { await llm("x") } catch (e) { return e.message }');

    expect(result.content).toBe('"upstream 500"');
    expect(t.calls.map((c) => c.event)).toEqual([{ type: "llm_call", depth: 1, model: DECISION }]);
  });
});

describe("zodFromJsonSchema", () => {
  it("covers the structured-output subset; optional properties may be null", () => {
    const schema = zodFromJsonSchema({
      type: "object",
      properties: {
        facts: { type: "array", items: { type: "string" } },
        n: { type: "integer" },
        kind: { enum: ["a", "b"] },
        note: { type: ["string", "null"] },
        extra: { type: "number" },
      },
      required: ["facts", "n", "kind", "note"],
    });

    expect(schema.parse({ facts: ["x"], n: 1, kind: "a", note: null, extra: null })).toEqual({
      facts: ["x"],
      n: 1,
      kind: "a",
      note: null,
      extra: null,
    });
    expect(schema.safeParse({ facts: "x", n: 1, kind: "a", note: null }).success).toBe(false);
    expect(schema.safeParse({ facts: [], n: 1.5, kind: "a", note: null }).success).toBe(false);
    expect(schema.safeParse({ facts: [], n: 1, kind: "c", note: null }).success).toBe(false);
  });

  it("forwards array, string and number constraints", () => {
    const schema = zodFromJsonSchema({
      type: "object",
      properties: {
        bullets: { type: "array", items: { type: "string" }, minItems: 1, maxItems: 2 },
        code: { type: "string", minLength: 2, maxLength: 3, pattern: "^[A-Z]+$" },
        score: { type: "number", minimum: 0, maximum: 1 },
        n: { type: "integer", exclusiveMinimum: 0, exclusiveMaximum: 10, multipleOf: 2 },
      },
      required: ["bullets", "code", "score", "n"],
    });
    const ok = { bullets: ["a"], code: "AB", score: 0.5, n: 4 };

    expect(schema.parse(ok)).toEqual(ok);
    for (const bad of [
      { bullets: [] },
      { bullets: ["a", "b", "c"] },
      { code: "A" },
      { code: "ABCD" },
      { code: "ab" },
      { score: -0.1 },
      { score: 1.1 },
      { n: 0 },
      { n: 10 },
      { n: 3 },
    ]) {
      expect(schema.safeParse({ ...ok, ...bad }).success, JSON.stringify(bad)).toBe(false);
    }
  });

  it("rejects a keyword it cannot represent by name; {} and $ref are any value", () => {
    const rejects: [unknown, string][] = [
      [{ type: "array", uniqueItems: true }, 'llm(): schema: "uniqueItems" is not supported'],
      [{ type: "string", format: "email" }, 'llm(): schema: "format" is not supported'],
      [{ allOf: [] }, 'llm(): schema: "allOf" is not supported'],
      [
        { type: "object", additionalProperties: { type: "string" } },
        "llm(): schema.additionalProperties: only false is supported",
      ],
      [
        JSON.parse('{"type":"object","properties":{"__proto__":{"type":"string"}}}'),
        'llm(): schema.properties: "__proto__" is not a supported name',
      ],
      [{ enum: [{ a: 1 }] }, "llm(): schema.enum: only string, number, boolean or null"],
      [{ type: "banana" }, 'llm(): schema.type: "banana" is not a JSON Schema type'],
      [{ maxItems: 2 }, 'llm(): schema: "maxItems" needs a "type"'],
      [
        { type: "object", properties: { xs: { type: "array", maxItems: -1 } } },
        "llm(): schema.properties.xs.maxItems: must be a non-negative integer",
      ],
      [{ type: "string", pattern: "(" }, "llm(): schema.pattern: Invalid regular expression"],
    ];
    for (const [schema, message] of rejects) {
      expect(() => zodFromJsonSchema(schema), JSON.stringify(schema)).toThrow(message);
    }
    expect(zodFromJsonSchema({}).parse({ any: 1 })).toEqual({ any: 1 });
    expect(zodFromJsonSchema({ $ref: "#/$defs/x" }).parse([1])).toEqual([1]);
    expect(zodFromJsonSchema({ type: "object", additionalProperties: false }).parse({})).toEqual(
      {},
    );
  });
});

describe("llm() schema constraints on the wire", () => {
  const models = {
    data: [
      {
        id: DECISION,
        context_length: 1000,
        top_provider: {},
        pricing: { prompt: "0", completion: "0" },
      },
    ],
  };
  const openRouter = (answer: string) => {
    const http = fakeFetch((call) =>
      call.url.endsWith("/models")
        ? jsonResponse(models)
        : jsonResponse({
            choices: [{ message: { role: "assistant", content: answer } }],
            usage: { prompt_tokens: 10, completion_tokens: 5 },
          }),
    );
    return {
      http,
      t: setup({ provider: new OpenRouterProvider({ apiKey: "k", fetch: http.fetch }) }),
    };
  };
  const SCRIPT = `
    try {
      const answer = await llm("merge", { schema: { type: "object",
        properties: { bullets: { type: "array", maxItems: 2 } }, required: ["bullets"] } });
      return answer.bullets;
    } catch (e) { return e.message; }`;

  it("maxItems reaches the provider and a 3-item answer fails the parse", async () => {
    const { http, t } = openRouter('{"bullets":["a","b","c"]}');

    const result = await t.run(SCRIPT);

    const posts = http.calls.filter((c) => c.method === "POST");
    expect(posts[0]?.body.response_format.json_schema.schema.properties.bullets).toMatchObject({
      type: "array",
      maxItems: 2,
    });
    expect(posts).toHaveLength(2); // the answer and the one retry
    expect(JSON.parse(result.content)).toMatch(/^llm: bullets: Array must contain at most 2/);
  });

  it("a 2-item answer parses", async () => {
    const { t } = openRouter('{"bullets":["a","b"]}');

    expect(JSON.parse((await t.run(SCRIPT)).content)).toEqual(["a", "b"]);
  });

  it("an unsupported keyword is an error to the script: nothing sent, no call counted", async () => {
    const t = setup({ script: { texts: ["fine"] }, llm: { maxCalls: 1 } });

    const result = await t.run(`
      let first;
      try { await llm("x", { schema: { type: "array", uniqueItems: true } }); }
      catch (e) { first = e.message; }
      return [first, await llm("y")];`);

    expect(JSON.parse(result.content)).toEqual([
      'llm(): schema: "uniqueItems" is not supported',
      "fine",
    ]);
    expect((t.provider as FakeProvider).structuredRequests).toHaveLength(0);
    expect(t.calls).toHaveLength(1);
  });
});
