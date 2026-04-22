import { resolve } from "node:path";
import { z } from "zod";
import type { Tool } from "./types";

const schema = z.object({
  file_path: z.string().describe("Absolute or cwd-relative file path"),
  old_string: z
    .string()
    .describe("Exact string to replace (include surrounding context for uniqueness)"),
  new_string: z.string().describe("Replacement string"),
  replace_all: z
    .boolean()
    .default(false)
    .describe("When true, replace every occurrence; otherwise old_string must be unique"),
});

export const editTool: Tool<z.infer<typeof schema>> = {
  name: "edit",
  description:
    "Replace `old_string` with `new_string` inside `file_path`. Destructive. Fails if old_string is not found, or appears multiple times without `replace_all=true`.",
  schema,
  isReadOnly: false,
  isDestructive: true,
  async call({ file_path, old_string, new_string, replace_all }, ctx) {
    const abs = resolve(ctx.cwd, file_path);
    const text = await Bun.file(abs).text();
    if (!text.includes(old_string)) {
      return {
        content: `old_string not found in ${abs}: ${JSON.stringify(old_string.slice(0, 80))}`,
        isError: true,
      };
    }
    const occurrences = text.split(old_string).length - 1;
    if (!replace_all && occurrences > 1) {
      return {
        content: `old_string appears ${occurrences} times — narrow the string or pass replace_all=true`,
        isError: true,
      };
    }
    const updated = replace_all
      ? text.split(old_string).join(new_string)
      : text.replace(old_string, new_string);
    await Bun.write(abs, updated);
    return {
      content: `Edited ${abs} (${occurrences} replacement${occurrences === 1 ? "" : "s"})`,
    };
  },
};
