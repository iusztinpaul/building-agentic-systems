// The registry holds tools with heterogeneous input types; a single type parameter
// can't capture that polymorphism, so we use `Tool<any>` as the wide type.
//
// `AnyTool`, `GeminiFunctionDeclaration`, and `toGeminiTools` live in ./gemini —
// a leaf module with no sibling-tool imports. Re-exported here for back-compat
// so existing `from "./tools/registry"` consumers keep working. New code that
// only needs the type or the adapter should import from "./tools/gemini"
// directly to avoid pulling in the full tool list (and the import cycle that
// comes with it: registry → task → agent/subagents → agent/loop → client).

import { bashTool } from "./bash";
import { editTool } from "./edit";
import { type AnyTool, type GeminiFunctionDeclaration, toGeminiTools } from "./gemini";
import { globTool } from "./glob";
import { grepTool } from "./grep";
import { readTool } from "./read";
import { taskTool } from "./task";
import { todoTool } from "./todo";
import { writeTool } from "./write";

export { type AnyTool, type GeminiFunctionDeclaration, toGeminiTools };

export const builtInTools: AnyTool[] = [
  bashTool,
  readTool,
  writeTool,
  editTool,
  globTool,
  grepTool,
  todoTool,
  taskTool,
];

export function createRegistry(tools: AnyTool[] = builtInTools): Map<string, AnyTool> {
  const reg = new Map<string, AnyTool>();
  for (const t of tools) reg.set(t.name, t);
  return reg;
}
