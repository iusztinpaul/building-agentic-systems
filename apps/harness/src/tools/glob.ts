import { resolve } from "node:path";
import { z } from "zod";
import type { Tool } from "./types";

const schema = z.object({
  pattern: z.string().describe("Glob pattern, e.g. 'src/**/*.ts' or '**/*.md'"),
  path: z.string().optional().describe("Base directory (absolute, or relative to cwd)"),
});

const MAX_RESULTS = 250;

export const globTool: Tool<z.infer<typeof schema>> = {
  name: "glob",
  description:
    "List files matching a glob pattern. Returns up to 250 paths (relative to the base dir). Read-only.",
  schema,
  isReadOnly: true,
  isDestructive: false,
  async call({ pattern, path }, ctx) {
    const glob = new Bun.Glob(pattern);
    const base = path ? resolve(ctx.cwd, path) : ctx.cwd;
    const results: string[] = [];
    for await (const file of glob.scan({ cwd: base, onlyFiles: false })) {
      results.push(file);
      if (results.length >= MAX_RESULTS) break;
    }
    if (results.length === 0) return { content: "(no matches)" };
    return { content: results.join("\n") };
  },
};
