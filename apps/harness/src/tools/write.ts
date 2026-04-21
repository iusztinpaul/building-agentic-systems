import { resolve } from "node:path";
import { z } from "zod";
import type { Tool } from "./types";

const schema = z.object({
  file_path: z.string().describe("Absolute or cwd-relative file path"),
  content: z.string().describe("New file contents (overwrites existing)"),
});

export const writeTool: Tool<z.infer<typeof schema>> = {
  name: "write",
  description: "Create or overwrite a file with the given content. Destructive.",
  schema,
  isReadOnly: false,
  isDestructive: true,
  async call({ file_path, content }, ctx) {
    const abs = resolve(ctx.cwd, file_path);
    await Bun.write(abs, content);
    return { content: `Wrote ${content.length} chars to ${abs}` };
  },
};
