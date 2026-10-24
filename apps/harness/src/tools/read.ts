// read: a page of a text file, at most 2 000 lines / 50 KB, with a
// hint telling the model where the next page starts.

import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { z } from "zod";
import type { ToolDefinition } from "./registry.ts";
import { cutBytes, MAX_OUTPUT_BYTES } from "./truncate.ts";

export const MAX_READ_LINES = 2_000;

const Params = z.object({
  path: z.string().min(1).describe("File path, relative to cwd"),
  offset: z
    .number()
    .int()
    .min(1)
    .optional()
    .describe("First line to read, 1-based (default 1)"),
  limit: z
    .number()
    .int()
    .min(1)
    .optional()
    .describe(`Max lines to read (default and max ${MAX_READ_LINES})`),
});

export function readTool(): ToolDefinition<typeof Params> {
  return {
    name: "read",
    description:
      "Read a text file. Returns at most 2000 lines or 50 KB per " +
      "call; use offset and limit to page through larger files.",
    promptSnippet: "read a file's contents (paged by offset/limit)",
    parameters: Params,
    exposure: "direct",
    isReadOnly: true,
    async execute({ path, offset = 1, limit }, ctx) {
      const file = resolve(ctx.cwd, path);
      const text = await readFile(file, "utf8");
      if (text === "") {
        return {
          content: "(empty file)",
          structured: { path: file, lines: [] },
        };
      }
      const all = text.split("\n");
      if (all.length > 1 && all.at(-1) === "") all.pop();
      if (offset > all.length) {
        return {
          content: `offset ${offset} is past the end of ${path} (${all.length} lines)`,
          isError: true,
        };
      }
      const max = Math.min(limit ?? MAX_READ_LINES, MAX_READ_LINES);
      const { lines, cut } = page(all.slice(offset - 1, offset - 1 + max));
      const last = offset + lines.length - 1;
      let content = lines.join("\n");
      if (cut) {
        content +=
          `\n\n[Line ${offset} cut: showing the first ${cut.shown} of ` +
          `${cut.total} bytes. offset/limit page by line; read the ` +
          `rest of this line with bash, e.g. ` +
          `tail -c +${cut.shown + 1} '${path}' | head -c ${MAX_OUTPUT_BYTES}.]`;
      }
      if (last < all.length) {
        content +=
          `\n\n[Showing lines ${offset}-${last} of ${all.length}. ` +
          `Use offset=${last + 1} to continue.]`;
      }
      return { content, structured: { path: file, lines } };
    },
  };
}

interface Page {
  lines: string[];
  cut?: { shown: number; total: number }; // bytes of a cut first line
}

// The longest prefix of `lines` within 50 KB (at least one line,
// itself cut at 50 KB).
function page(lines: string[]): Page {
  const out: string[] = [];
  let bytes = 0;
  for (const line of lines) {
    bytes += Buffer.byteLength(line, "utf8") + 1;
    if (bytes > MAX_OUTPUT_BYTES) break;
    out.push(line);
  }
  const [first] = lines;
  if (out.length > 0 || first === undefined) return { lines: out };
  const head = cutBytes(first, MAX_OUTPUT_BYTES);
  const shown = Buffer.byteLength(head, "utf8");
  const total = Buffer.byteLength(first, "utf8");
  return {
    lines: [head],
    cut: shown < total ? { shown, total } : undefined,
  };
}
