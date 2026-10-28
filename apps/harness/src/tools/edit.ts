// edit: replace one exact, unique occurrence of oldText.

import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { z } from "zod";
import type { ToolDefinition } from "./registry.ts";

const Params = z.object({
  path: z.string().min(1).describe("File path, relative to cwd"),
  oldText: z
    .string()
    .min(1)
    .describe("Exact text to replace; must occur exactly once"),
  newText: z.string().describe("Replacement text"),
});

export function editTool(): ToolDefinition<typeof Params> {
  return {
    name: "edit",
    description:
      "Replace one exact occurrence of oldText with newText in a " +
      "file. oldText must match exactly once, whitespace included.",
    promptSnippet: "replace one exact, unique text span in a file",
    parameters: Params,
    exposure: "direct",
    isReadOnly: false,
    async execute({ path, oldText, newText }, ctx) {
      const file = resolve(ctx.cwd, path);
      const text = await readFile(file, "utf8");
      const count = text.split(oldText).length - 1;
      if (count !== 1) {
        const fix =
          count === 0
            ? "read the file and copy the text exactly"
            : "include more surrounding text to make it unique";
        return {
          content: `oldText matched ${count} times in ${path}; ${fix}`,
          isError: true,
        };
      }
      const at = text.indexOf(oldText);
      const next =
        text.slice(0, at) + newText + text.slice(at + oldText.length);
      await writeFile(file, next);
      return { content: `Edited ${path}` };
    },
  };
}
