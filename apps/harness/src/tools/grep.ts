import { resolve } from "node:path";
import { z } from "zod";
import type { Tool } from "./types";

const schema = z.object({
  pattern: z.string().describe("Regex pattern (ripgrep-compatible)"),
  path: z
    .string()
    .optional()
    .describe("File or directory to search (absolute, or relative to cwd)"),
  glob: z
    .string()
    .optional()
    .describe("Filter files by glob, e.g. '*.ts'. Passed to ripgrep --glob."),
});

const MAX_LINES = 250;

export const grepTool: Tool<z.infer<typeof schema>> = {
  name: "grep",
  description:
    "Search file contents via ripgrep. Returns up to 250 matching lines prefixed with `path:lineno:`. Read-only.",
  schema,
  isReadOnly: true,
  isDestructive: false,
  async call({ pattern, path, glob }, ctx) {
    const target = path ? resolve(ctx.cwd, path) : ctx.cwd;
    const args = ["rg", "-n", "--no-heading", "--color=never"];
    if (glob) args.push("--glob", glob);
    args.push(pattern, target);

    const proc = Bun.spawn(args, { stdout: "pipe", stderr: "pipe", cwd: ctx.cwd });
    const onAbort = () => proc.kill();
    ctx.signal.addEventListener("abort", onAbort, { once: true });

    try {
      const stdout = await new Response(proc.stdout).text();
      await proc.exited;
      const lines = stdout.split("\n").filter(Boolean).slice(0, MAX_LINES);
      if (lines.length === 0) return { content: "(no matches)" };
      return { content: lines.join("\n") };
    } finally {
      ctx.signal.removeEventListener("abort", onAbort);
    }
  },
};
