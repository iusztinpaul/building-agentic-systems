// What a codemode script sees (spec §10.4–10.5): `tools.<name>()`,
// `searchTools()` and `describeTool()`. Every `tools.x()` is a
// ToolCall with origin "codemode" through the session's own gate
// (ADR-016 §5: one permission path), then registry.execute; the
// script gets `structured ?? content`. A deny rejects the promise;
// a headless deny also aborts the run, which tears the sandbox down
// before the rejection can reach a `catch`.

import type { GateResult } from "../loop/hooks.ts";
import {
  parametersOf,
  type ToolContext,
  type ToolDefinition,
  type ToolRegistry,
} from "../tools/registry.ts";
import { DEFAULT_LIMIT, rankTools } from "../tools/tool-search.ts";
import type { ToolCall } from "../types.ts";
import type { AsyncHostFn, SyncHostFn } from "./sandbox.ts";

export interface BridgeOptions {
  registry: ToolRegistry;
  gate: (call: ToolCall) => Promise<GateResult>;
  ctx: ToolContext; // the codemode call's
  callId: string; // prefix of the inner calls' ids
  maxToolCalls: number;
  exclude: string; // codemode itself: never nested
}

export interface Bridge {
  prelude: string;
  async: Record<string, AsyncHostFn>;
  sync: Record<string, SyncHostFn>;
}

export function createBridge(o: BridgeOptions): Bridge {
  const callable = o.registry
    .scriptable()
    .filter((tool) => tool.name !== o.exclude);
  const byName = new Map(callable.map((tool) => [tool.name, tool]));
  let calls = 0;

  const call: AsyncHostFn = async (input, host) => {
    const [name, args] = JSON.parse(input) as [string, unknown];
    if (++calls > o.maxToolCalls) {
      throw new Error(
        `tool call limit reached: ${o.maxToolCalls} per script ` +
          "(codemode.maxToolCalls)",
      );
    }
    if (!isRecord(args)) {
      throw new TypeError(`tools.${name}() takes one object argument`);
    }
    const toolCall: ToolCall = {
      id: `${o.callId}.${calls}`,
      name,
      arguments: args,
      origin: "codemode",
    };
    const release = host.hold(); // a prompt does not use up the time
    let gate: GateResult;
    try {
      gate = await o.gate(toolCall);
    } finally {
      release();
    }
    if (!gate.run) throw new Error(`Blocked: ${gate.reason}`);
    const result = await o.registry.execute(toolCall, {
      ...o.ctx,
      signal: host.signal,
      origin: "codemode",
    });
    if (result.isError) throw new Error(result.content);
    return JSON.stringify(result.structured ?? result.content) ?? "null";
  };

  const searchTools: SyncHostFn = (input) => {
    const [query, limit] = JSON.parse(input) as [string, unknown];
    const n =
      typeof limit === "number" && limit > 0 ? limit : DEFAULT_LIMIT;
    const hits = rankTools(o.registry.list("codemode"), query, n);
    return JSON.stringify(
      hits.map((tool) => ({
        name: tool.name,
        description: firstLine(tool),
      })),
    );
  };

  const describeTool: SyncHostFn = (input) => {
    const name = JSON.parse(input) as string;
    const tool = byName.get(name);
    if (!tool) throw new Error(`unknown tool: ${name}`);
    return JSON.stringify({
      name,
      description: tool.description,
      parameters: parametersOf(tool),
    });
  };

  return {
    prelude: prelude([...byName.keys()]),
    async: { call },
    sync: { searchTools, describeTool },
  };
}

// The script's globals, built in the VM from the host functions.
function prelude(names: string[]): string {
  return `
const call = host.call;
const search = host.searchTools;
const describe = host.describeTool;
const tool = (name) => async (args) =>
  JSON.parse(await call(JSON.stringify([name, args === undefined ? {} : args])));
globalThis.tools = Object.freeze(
  Object.fromEntries(${JSON.stringify(names)}.map((n) => [n, tool(n)])),
);
globalThis.searchTools = (query, limit) =>
  JSON.parse(search(JSON.stringify([String(query), limit])));
globalThis.describeTool = (name) =>
  JSON.parse(describe(JSON.stringify(String(name))));
`;
}

// The first non-empty line of a tool's description.
export function firstLine(tool: ToolDefinition): string {
  const line = tool.description
    .split("\n")
    .map((l) => l.trim())
    .find(Boolean);
  return line ?? tool.promptSnippet;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" && value !== null && !Array.isArray(value)
  );
}
