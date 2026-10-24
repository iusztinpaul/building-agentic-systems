// write: create or overwrite a file, creating parent directories.

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { z } from "zod";
import type { ToolDefinition } from "./registry.ts";

const Params = z.object({
  path: z.string().min(1).describe("File path, relative to cwd"),
  content: z.string().describe("The whole new file content"),
});

export function writeTool(): ToolDefinition<typeof Params> {
  return {
    name: "write",
    description:
      "Write a file with the given content, replacing it if it " +
      "exists. Parent directories are created.",
    promptSnippet: "create a file or replace its whole content",
    parameters: Params,
    exposure: "direct",
    isReadOnly: false,
    async execute({ path, content }, ctx) {
      const file = resolve(ctx.cwd, path);
      await mkdir(dirname(file), { recursive: true });
      await writeFile(file, content);
      const bytes = Buffer.byteLength(content, "utf8");
      return { content: `Wrote ${bytes} bytes to ${path}` };
    },
  };
}
