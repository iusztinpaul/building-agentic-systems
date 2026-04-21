// The registry holds tools with heterogeneous input types; a single type parameter
// can't capture that polymorphism, so we use `Tool<any>` as the wide type.

import { zodToJsonSchema } from "zod-to-json-schema";
import { bashTool } from "./bash";
import { editTool } from "./edit";
import { globTool } from "./glob";
import { grepTool } from "./grep";
import { readTool } from "./read";
import { todoTool } from "./todo";
import type { Tool } from "./types";
import { writeTool } from "./write";

// biome-ignore lint/suspicious/noExplicitAny: tool registry is heterogeneous by design
export type AnyTool = Tool<any>;

export const builtInTools: AnyTool[] = [
  bashTool,
  readTool,
  writeTool,
  editTool,
  globTool,
  grepTool,
  todoTool,
];

export function createRegistry(tools: AnyTool[] = builtInTools): Map<string, AnyTool> {
  const reg = new Map<string, AnyTool>();
  for (const t of tools) reg.set(t.name, t);
  return reg;
}

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
    // jsonSchema7 is the default; keep it explicit. OpenAPI 3.0 emits
    // `exclusiveMinimum: boolean` which Gemini rejects; draft-07 uses a number.
    parametersJsonSchema: zodToJsonSchema(t.schema, { target: "jsonSchema7" }) as Record<
      string,
      unknown
    >,
  }));
  return [{ functionDeclarations }];
}
