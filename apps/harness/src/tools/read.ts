import { resolve } from "node:path";
import { z } from "zod";
import type { Tool } from "./types";

const schema = z.object({
  file_path: z.string().describe("Absolute or cwd-relative file path"),
  offset: z.number().int().positive().optional().describe("1-based line to start at (default 1)"),
  limit: z.number().int().positive().optional().describe("Max lines to return (default 2000)"),
});

export const readTool: Tool<z.infer<typeof schema>> = {
  name: "read",
  description:
    "Read a text file and return its contents with 1-based line numbers (e.g. `  42→line text`). Read-only.",
  schema,
  isReadOnly: true,
  isDestructive: false,
  async call({ file_path, offset = 1, limit = 2000 }, ctx) {
    const abs = resolve(ctx.cwd, file_path);
    const text = await Bun.file(abs).text();
    const lines = text.split("\n");
    const start = offset - 1;
    const end = Math.min(lines.length, start + limit);
    const slice = lines.slice(start, end);
    const numbered = slice.map((l, i) => `${start + i + 1}→${l}`).join("\n");
    return { content: numbered || "(empty)" };
  },
};
