// Context files (spec §7, the memory feature): ~/.tree-agent/AGENTS.md,
// then — in a trusted cwd only — every ancestor of cwd from the root
// down to cwd, taking the first of AGENTS.md, CLAUDE.md per directory.
// Read at session start and on /reload.

import { readFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { userPaths } from "../config/paths.ts";
import { log } from "../log.ts";

export const CONTEXT_FILE_NAMES = ["AGENTS.md", "CLAUDE.md"];

export interface ContextFile {
  path: string; // absolute
  content: string; // possibly a truncated copy
}

export interface ContextFileOptions {
  cwd: string;
  trusted: boolean;
  maxChars: number; // per file; the total cap is 3×
  env?: NodeJS.ProcessEnv;
}

export async function loadContextFiles(
  o: ContextFileOptions,
): Promise<ContextFile[]> {
  const candidates = [[userPaths(o.env).agentsMd]];
  if (o.trusted) {
    for (const dir of ancestors(resolve(o.cwd))) {
      candidates.push(CONTEXT_FILE_NAMES.map((n) => join(dir, n)));
    }
  }
  const files: ContextFile[] = [];
  let budget = 3 * o.maxChars;
  for (const paths of candidates) {
    const found = await firstReadable(paths);
    if (!found) continue;
    if (budget <= 0) {
      log.warn(
        `context files over ${3 * o.maxChars} chars: ${found.path} dropped`,
      );
      continue;
    }
    const cap = Math.min(o.maxChars, budget);
    const content = truncate(found.path, found.content, cap);
    budget -= Math.min(found.content.length, cap);
    files.push({ path: found.path, content });
  }
  return files;
}

// Each file as `# <absolute path>\n<content>`.
export function formatContextFiles(files: ContextFile[]): string {
  return files.map((f) => `# ${f.path}\n${f.content.trim()}`).join("\n\n");
}

// Root first, cwd last.
function ancestors(dir: string): string[] {
  const out = [dir];
  while (dirname(dir) !== dir) {
    dir = dirname(dir);
    out.unshift(dir);
  }
  return out;
}

async function firstReadable(
  paths: string[],
): Promise<ContextFile | undefined> {
  for (const path of paths) {
    try {
      return { path, content: await readFile(path, "utf8") };
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code !== "ENOENT" && code !== "EISDIR") throw err;
    }
  }
  return undefined;
}

function truncate(path: string, content: string, cap: number): string {
  if (content.length <= cap) return content;
  return (
    `${content.slice(0, cap)}\n\n[truncated: showing ${cap} of ` +
    `${content.length} chars; read ${path} for the rest]`
  );
}
