// bash: run a command in cwd with the inherited env. Memory stays
// bounded: each stream keeps only its first and last 25 KB, and once
// the output passes 50 KB it streams to ~/.tree-agent/tmp/<id>.txt
// as it arrives (up to 100 MB); the path is appended to the result.
// If that file cannot be written, the result keeps the head and tail
// and says why the full output was not saved.

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { createWriteStream, mkdirSync, type WriteStream } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import type { ToolDefinition, ToolResult } from "./registry.ts";
import { HeadTailBuffer, MAX_OUTPUT_BYTES } from "./truncate.ts";

export const DEFAULT_TIMEOUT_S = 120;
export const MAX_TIMEOUT_S = 3_600;
// The spill file stops growing here, so a runaway command (`yes`)
// cannot fill the disk before its timeout.
export const MAX_SPILL_BYTES = 100 * 1024 * 1024;

const Params = z.object({
  command: z.string().min(1).describe("The bash command to run"),
  timeout: z
    .number()
    .min(1)
    .max(MAX_TIMEOUT_S)
    .optional()
    .describe(
      `Timeout in seconds (default ${DEFAULT_TIMEOUT_S}, max ${MAX_TIMEOUT_S})`,
    ),
});

export interface BashOptions {
  tmpDir: string; // where full over-limit output is saved
  env?: NodeJS.ProcessEnv; // inherited by the command
  maxSpillBytes?: number; // default MAX_SPILL_BYTES
}

export function bashTool(
  options: BashOptions,
): ToolDefinition<typeof Params> {
  return {
    name: "bash",
    description:
      "Run a bash command in the working directory. Returns stdout " +
      "and stderr; output over 50 KB is cut to its first and last " +
      "25 KB and saved in full (up to 100 MB) to a file whose path " +
      "is appended. " +
      "The structured stdout and stderr are cut the same way.",
    promptSnippet: "run a shell command (tests, git, search, builds)",
    parameters: Params,
    exposure: "direct",
    isReadOnly: false,
    async execute({ command, timeout = DEFAULT_TIMEOUT_S }, ctx) {
      const run = await spawnBash(command, {
        cwd: ctx.cwd,
        env: options.env ?? process.env,
        timeoutMs: timeout * 1000,
        signal: ctx.signal,
        tmpDir: options.tmpDir,
        maxSpillBytes: options.maxSpillBytes ?? MAX_SPILL_BYTES,
      });
      return toResult(run, timeout);
    },
  };
}

interface Run {
  output: HeadTailBuffer; // stdout and stderr interleaved
  stdout: HeadTailBuffer;
  stderr: HeadTailBuffer;
  file?: string; // the full output, once it passed 50 KB
  fileCapped?: number; // the file stopped at this many bytes
  fileError?: string; // why the full output could not be saved
  exitCode: number | null;
  timedOut: boolean;
  aborted: boolean;
}

// Rejects (never throws from a handler) on a spawn or pipe error,
// after killing the command. A spill-file error only stops the spill.
function spawnBash(
  command: string,
  o: {
    cwd: string;
    env: NodeJS.ProcessEnv;
    timeoutMs: number;
    signal: AbortSignal;
    tmpDir: string;
    maxSpillBytes: number;
  },
): Promise<Run> {
  return new Promise((done, fail) => {
    // Own process group, so a kill reaches the command's children.
    const child = spawn("bash", ["-c", command], {
      cwd: o.cwd,
      env: o.env,
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const run: Run = {
      output: new HeadTailBuffer(),
      stdout: new HeadTailBuffer(),
      stderr: new HeadTailBuffer(),
      exitCode: null,
      timedOut: false,
      aborted: false,
    };
    let spill: WriteStream | undefined;
    let spilled = 0;
    let paused = false;
    let failed = false;
    let closed = false;
    const kill = () => {
      try {
        if (child.pid) process.kill(-child.pid, "SIGKILL");
      } catch {
        // already gone
      }
    };
    const cleanup = () => {
      clearTimeout(timer);
      o.signal.removeEventListener("abort", onAbort);
    };
    const onError = (err: Error) => {
      if (failed) return;
      failed = true;
      cleanup();
      kill();
      spill?.destroy();
      fail(err);
    };
    const timer = setTimeout(() => {
      run.timedOut = true;
      kill();
    }, o.timeoutMs);
    const onAbort = () => {
      run.aborted = true;
      kill();
    };
    o.signal.addEventListener("abort", onAbort, { once: true });
    if (o.signal.aborted) onAbort();

    const resume = () => {
      paused = false;
      child.stdout.resume();
      child.stderr.resume();
    };
    const onSpillError = (err: Error) => {
      if (run.fileError) return;
      run.fileError = err.message;
      run.file = undefined;
      spill?.destroy();
      if (paused) resume();
      if (closed) done(run);
    };
    // Write to the spill file with backpressure: pause both pipes
    // until the file catches up, so the queue stays bounded.
    const toFile = (bytes: Buffer) => {
      if (!spill || run.fileCapped || run.fileError) return;
      if (spilled + bytes.length > o.maxSpillBytes) {
        bytes = bytes.subarray(0, o.maxSpillBytes - spilled);
        run.fileCapped = o.maxSpillBytes;
      }
      spilled += bytes.length;
      if (!spill.write(bytes) && !paused) {
        paused = true;
        child.stdout.pause();
        child.stderr.pause();
        spill.once("drain", resume);
      }
    };
    const onData = (stream: HeadTailBuffer) => (chunk: Buffer) => {
      if (failed) return;
      try {
        const over = run.output.total + chunk.length > MAX_OUTPUT_BYTES;
        if (!spill && !run.fileError && over) {
          // Nothing dropped yet: the buffer still holds every byte.
          mkdirSync(o.tmpDir, { recursive: true });
          run.file = join(o.tmpDir, `${randomUUID()}.txt`);
          spill = createWriteStream(run.file);
          spill.on("error", onSpillError);
          toFile(run.output.bytes());
        }
        toFile(chunk);
      } catch (err) {
        onSpillError(err as Error);
      }
      run.output.push(chunk);
      stream.push(chunk);
    };
    child.stdout.on("data", onData(run.stdout));
    child.stderr.on("data", onData(run.stderr));
    child.on("error", onError);
    child.on("close", (code) => {
      if (failed) return;
      cleanup();
      closed = true;
      run.exitCode = code;
      // end() hands a final-flush error to its callback.
      if (spill && !run.fileError) {
        spill.end((err?: Error | null) =>
          err ? onSpillError(err) : done(run),
        );
      } else done(run);
    });
  });
}

function toResult(run: Run, timeoutS: number): ToolResult {
  let content = run.output.render() || "(no output)";
  const { exitCode } = run;
  const structured = {
    exitCode,
    stdout: run.stdout.render(),
    stderr: run.stderr.render(),
  };
  const isError = run.timedOut || run.aborted || exitCode !== 0;
  if (run.timedOut || run.aborted) {
    const why = run.timedOut
      ? `Command timed out after ${timeoutS}s`
      : "Command aborted";
    content = `${why}\n${content}`;
  } else if (exitCode !== 0) {
    content = `${content.trimEnd()}\n\nCommand exited with code ${exitCode}`;
  }
  if (run.fileCapped && run.file) {
    content += `\n\nOnly the first ${run.fileCapped} bytes were saved.`;
  }
  if (run.file) content += `\n\nFull output: ${run.file}`;
  if (run.fileError) {
    content += `\n\ncould not save full output: ${run.fileError}`;
  }
  return { content, isError, structured };
}
