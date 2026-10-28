// Scratch directories for tests that touch the filesystem.

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach } from "vitest";

// A fresh directory per test, removed after it.
export function useTmpDir(prefix: string): () => string {
  let dir = "";
  afterEach(() => {
    if (dir) rmSync(dir, { recursive: true, force: true });
    dir = "";
  });
  return () => {
    dir ||= mkdtempSync(join(tmpdir(), `tree-agent-${prefix}-`));
    return dir;
  };
}

export function toolCtx(cwd: string, signal = new AbortController().signal) {
  return { cwd, signal, origin: "model" as const, depth: 0 };
}

// True once `pid` no longer exists (polls up to 2 s).
export async function gone(pid: number): Promise<boolean> {
  for (let i = 0; i < 40; i++) {
    try {
      process.kill(pid, 0);
    } catch {
      return true;
    }
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
}
