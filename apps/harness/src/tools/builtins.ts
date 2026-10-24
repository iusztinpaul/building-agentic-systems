// The four built-in tools (spec §8), all `direct`: with no MCP
// servers these are exactly the tools the model sees.

import { type BashOptions, bashTool } from "./bash.ts";
import { editTool } from "./edit.ts";
import { readTool } from "./read.ts";
import { ToolRegistry } from "./registry.ts";
import { writeTool } from "./write.ts";

export function builtinRegistry(options: BashOptions): ToolRegistry {
  const registry = new ToolRegistry();
  registry.register(readTool());
  registry.register(bashTool(options));
  registry.register(editTool());
  registry.register(writeTool());
  return registry;
}
