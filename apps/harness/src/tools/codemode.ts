// codemode (spec §10.3): the model writes one script that calls many
// tools; only its console.log lines and its return value come back.
// Registered only when a codemode-exposed tool exists. Its provider
// description ends with the codemode tools as one-line declarations,
// in registry order, until codemode.inlineBudgetTokens (chars / 4);
// the rest are for searchTools().

import { randomUUID } from "node:crypto";
import { z } from "zod";
import { createBridge, firstLine, isRecord } from "../codemode/bridge.ts";
import { runSandbox } from "../codemode/sandbox.ts";
import type { Settings } from "../config/settings.ts";
import { BLOCKED } from "../loop/agent-loop.ts";
import type { GateResult } from "../loop/hooks.ts";
import type { ToolCall } from "../types.ts";
import {
  parametersOf,
  type ToolDefinition,
  type ToolRegistry,
} from "./registry.ts";
import { spillOver } from "./truncate.ts";

export const CODEMODE = "codemode";

// Task 211 adds the llm() sentence.
export const CODEMODE_PREAMBLE = [
  "Run a JavaScript (ES2020, async) script in a sandbox to call many " +
    "tools at once.",
  "`tools.<name>(args)` calls a tool and returns its structured result.",
  "`searchTools(query)` and `describeTool(name)` discover tools.",
  "Only console.log output and the returned value reach you; keep " +
    "data in variables.",
  "Available tools:",
].join("\n");

export type CodemodeLimits = Omit<Settings["codemode"], "llm">;

export interface CodemodeOptions extends CodemodeLimits {
  tmpDir: string; // where output over outputMaxBytes is saved
  // The session's permission gate, the same as the model's calls.
  gate: (call: ToolCall) => Promise<GateResult>;
}

const Params = z.object({
  script: z
    .string()
    .min(1)
    .describe("The body of an async function: await and return"),
  description: z
    .string()
    .min(1)
    .describe("One line on what the script does, shown to the user"),
});

export function codemodeTool(
  registry: ToolRegistry,
  o: CodemodeOptions,
): ToolDefinition<typeof Params> {
  return {
    name: CODEMODE,
    // A getter: it follows the codemode set (a server reconnects).
    get description() {
      return `${CODEMODE_PREAMBLE}\n${declarations(registry, o)}`;
    },
    // The `tools` prompt line: scripts reach the tools above too, and
    // nothing else tells the model what the built-ins return.
    promptSnippet:
      "run one JavaScript script that calls many tools at once; " +
      "every tool above is `tools.<name>(args)` in it too, where " +
      "read returns {path, lines: string[]}, bash {exitCode, stdout, " +
      "stderr} (a non-zero exit rejects), edit and write a status string",
    parameters: Params,
    exposure: "direct",
    isReadOnly: false,
    async execute({ script }, ctx) {
      const bridge = createBridge({
        registry,
        gate: o.gate,
        ctx,
        callId: `codemode-${randomUUID().slice(0, 8)}`,
        maxToolCalls: o.maxToolCalls,
        exclude: CODEMODE,
      });
      const result = await runSandbox({
        script,
        timeoutMs: o.timeoutMs,
        memoryMb: o.memoryMb,
        concurrency: o.concurrency,
        signal: ctx.signal,
        ...bridge,
      });
      const lines = [...result.logs];
      if (result.value !== undefined) lines.push(result.value);
      if (result.aborted) {
        const blocked = ctx.signal.reason === BLOCKED;
        lines.push(
          blocked
            ? "Error: the script was stopped: a tool call was blocked"
            : "Error: the script was stopped: the run was aborted",
        );
      } else if (result.error !== undefined) {
        // `TypeError: x` already names itself: no `Error: ` before it.
        const named = /^\w*Error: /.test(result.error);
        lines.push(named ? result.error : `Error: ${result.error}`);
      }
      const text = lines.join("\n") || "(no output)";
      return {
        content: spillOver(text, o.outputMaxBytes, o.tmpDir),
        isError: result.error !== undefined,
      };
    },
  };
}

// Only when a script would have something to call beyond the
// model's own tools.
export function registerCodemode(
  registry: ToolRegistry,
  o: CodemodeOptions,
): boolean {
  if (registry.list("codemode").length === 0) return false;
  registry.register(codemodeTool(registry, o));
  return true;
}

// `name(params: {…}): first line`, one per codemode tool, until the
// budget; then how many more searchTools() can find.
export function declarations(
  registry: ToolRegistry,
  o: Pick<CodemodeLimits, "inlineBudgetTokens">,
): string {
  const tools = registry.list("codemode");
  const budget = o.inlineBudgetTokens * 4; // chars
  const lines: string[] = [];
  let used = 0;
  for (const tool of tools) {
    const params = tsType(parametersOf(tool));
    const line = `${tool.name}(params: ${params}): ${firstLine(tool)}`;
    if (used + line.length + 1 > budget) break;
    used += line.length + 1;
    lines.push(line);
  }
  const rest = tools.length - lines.length;
  if (rest > 0) {
    lines.push(`(${rest} more; find them with searchTools(query))`);
  }
  return lines.join("\n");
}

// A JSON Schema as a short TypeScript-like type: objects two levels
// deep, then `object`.
export function tsType(schema: unknown, depth = 0): string {
  if (!isRecord(schema)) return "unknown";
  if (Array.isArray(schema.enum)) {
    return schema.enum.map((v) => JSON.stringify(v)).join(" | ");
  }
  if ("const" in schema) return JSON.stringify(schema.const);
  const union = schema.anyOf ?? schema.oneOf;
  if (Array.isArray(union)) {
    return union.map((s) => tsType(s, depth)).join(" | ");
  }
  const { type } = schema;
  if (Array.isArray(type)) {
    return type
      .map((t) => tsType({ ...schema, type: t }, depth))
      .join(" | ");
  }
  if (type === "string" || type === "boolean" || type === "null") {
    return type;
  }
  if (type === "number" || type === "integer") return "number";
  if (type === "array") {
    const item = tsType(schema.items, depth + 1);
    return item.includes(" | ") ? `(${item})[]` : `${item}[]`;
  }
  if (type === "object" || isRecord(schema.properties)) {
    return objectType(schema, depth);
  }
  return "unknown";
}

function objectType(
  schema: Record<string, unknown>,
  depth: number,
): string {
  const props = isRecord(schema.properties)
    ? Object.entries(schema.properties)
    : [];
  if (props.length === 0) return depth === 0 ? "{}" : "object";
  if (depth > 1) return "object";
  const required = new Set(
    Array.isArray(schema.required) ? schema.required : [],
  );
  const fields = props.map(([key, value]) => {
    const optional = required.has(key) ? "" : "?";
    return `${key}${optional}: ${tsType(value, depth + 1)}`;
  });
  return `{${fields.join(", ")}}`;
}
