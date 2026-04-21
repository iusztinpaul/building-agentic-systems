import { z } from "zod";
import type { Tool } from "./types";

const schema = z.object({
  command: z.string().describe("Shell command to execute via bash -c"),
  timeout_ms: z
    .number()
    .int()
    .positive()
    .default(120_000)
    .describe("Timeout in milliseconds (default 120000)"),
});

export const bashTool: Tool<z.infer<typeof schema>> = {
  name: "bash",
  description:
    "Execute a shell command via bash -c. Destructive — avoid for read-only operations (prefer read/glob/grep). Returns combined stdout/stderr with exit code.",
  schema,
  isReadOnly: false,
  isDestructive: true,
  async call({ command, timeout_ms }, ctx) {
    const proc = Bun.spawn(["bash", "-c", command], {
      cwd: ctx.cwd,
      stdout: "pipe",
      stderr: "pipe",
    });
    const timer = setTimeout(() => proc.kill(), timeout_ms);
    const onAbort = () => proc.kill();
    ctx.signal.addEventListener("abort", onAbort, { once: true });

    try {
      const [stdout, stderr] = await Promise.all([
        new Response(proc.stdout).text(),
        new Response(proc.stderr).text(),
      ]);
      const exitCode = await proc.exited;
      const parts = [
        stdout && `--- stdout ---\n${stdout.trimEnd()}`,
        stderr && `--- stderr ---\n${stderr.trimEnd()}`,
        `--- exit: ${exitCode} ---`,
      ].filter(Boolean);
      return { content: parts.join("\n"), isError: exitCode !== 0 };
    } finally {
      clearTimeout(timer);
      ctx.signal.removeEventListener("abort", onAbort);
    }
  },
};
