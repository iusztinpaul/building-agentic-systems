import { createHash, randomUUID } from "node:crypto";
import { mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// Layout:
//   ~/.tree/projects/<cwd-hash>/<session-id>.jsonl
//
// cwd-hash keeps sessions scoped to the project they were recorded in without
// exposing the real path in file names; the `meta` entry in each jsonl carries
// the actual cwd for reconstruction.

export const treeDir = (): string => join(homedir(), ".tree");
export const projectsDir = (): string => join(treeDir(), "projects");

export function cwdHash(cwd: string): string {
  return createHash("sha256").update(cwd).digest("hex").slice(0, 12);
}

export function sessionsDirFor(cwd: string): string {
  return join(projectsDir(), cwdHash(cwd));
}

export function sessionPath(cwd: string, id: string): string {
  return join(sessionsDirFor(cwd), `${id}.jsonl`);
}

export function newSessionId(): string {
  return randomUUID();
}

export function ensureDir(path: string): void {
  mkdirSync(path, { recursive: true });
}
