// Trust (spec §16): project-level files (the cwd-ancestor AGENTS.md
// walk, .tree-agent/settings.json, later mcp.json and skills) load
// only for a trusted cwd. ~/.tree-agent/trusted.json is a JSON array
// of absolute directory paths; --trust or TREE_AGENT_TRUST=1 trusts
// any cwd for one run. Headless never prompts (the TUI dialog that
// writes trusted.json is task 205).

import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { z } from "zod";
import { log } from "../log.ts";
import { userPaths } from "./paths.ts";

const TrustedDirs = z.array(z.string());

export async function isTrusted(
  cwd: string,
  options: { env?: NodeJS.ProcessEnv; flag?: boolean } = {},
): Promise<boolean> {
  const env = options.env ?? process.env;
  if (options.flag || env.TREE_AGENT_TRUST === "1") return true;
  const dirs = await readTrusted(userPaths(env).trusted);
  const target = resolve(cwd);
  return dirs.some((dir) => resolve(dir) === target);
}

async function readTrusted(file: string): Promise<string[]> {
  let text: string;
  try {
    text = await readFile(file, "utf8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw err;
  }
  try {
    return TrustedDirs.parse(JSON.parse(text));
  } catch {
    log.warn(`${file}: not a JSON array of paths; trusting nothing`);
    return [];
  }
}
