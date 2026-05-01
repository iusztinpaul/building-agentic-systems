// Leaf module for the Gemini tool-shape adapter.
//
// This file deliberately has zero imports of sibling tool modules (bash.ts,
// task.ts, etc.). It holds only:
//   - the `AnyTool` widening of Tool<unknown>
//   - the Gemini wire shape (GeminiFunctionDeclaration)
//   - the pure adapter `toGeminiTools`
//
// Why split it from registry.ts: `tools/registry.ts` imports every concrete
// tool, including `taskTool`, which in turn imports agent/subagents → loop →
// client. If client.ts imports anything from registry.ts, that closes a
// circular import chain that triggers TDZ errors on Bun + Linux (the `const`
// initializers in client.ts and loop.ts haven't run by the time mid-cycle
// re-entry hits them). Putting `AnyTool` + `toGeminiTools` here lets client.ts
// import them without ever touching registry.ts, breaking the cycle.

import { zodToJsonSchema } from "zod-to-json-schema";
import type { Tool } from "./types";

// biome-ignore lint/suspicious/noExplicitAny: tool registry is heterogeneous by design
export type AnyTool = Tool<any>;

// Gemini's `config.tools` shape: [{ functionDeclarations: [...] }]
export interface GeminiFunctionDeclaration {
  name: string;
  description: string;
  parametersJsonSchema: Record<string, unknown>;
}

export function toGeminiTools(
  tools: AnyTool[],
): Array<{ functionDeclarations: GeminiFunctionDeclaration[] }> {
  const functionDeclarations: GeminiFunctionDeclaration[] = tools.map((t) => ({
    name: t.name,
    description: t.description,
    // MCP tools provide a server-defined JSON Schema already; skip the zod
    // conversion in that case. For native tools, jsonSchema7 is the default;
    // OpenAPI 3.0 emits `exclusiveMinimum: boolean` which Gemini rejects.
    parametersJsonSchema:
      t.parametersJsonSchema ??
      (zodToJsonSchema(t.schema, { target: "jsonSchema7" }) as Record<string, unknown>),
  }));
  return [{ functionDeclarations }];
}
