import { afterEach } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// mkTmpDir() returns a fresh temp directory that gets cleaned up at the end of
// the current test. No try/finally boilerplate at call sites — the cleanup runs
// from the bun:test `afterEach` we register the first time this module loads.

const pending = new Set<string>();

afterEach(() => {
  for (const dir of pending) {
    try {
      rmSync(dir, { recursive: true, force: true });
    } catch {
      // tests shouldn't fail because cleanup did
    }
  }
  pending.clear();
});

export function mkTmpDir(prefix = "tree-test-"): string {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  pending.add(dir);
  return dir;
}
