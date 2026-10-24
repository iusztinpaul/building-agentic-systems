// Trust (spec §16): project-level files (the cwd-ancestor AGENTS.md
// walk, .tree-agent/settings.json, later mcp.json and skills) load
// only for a trusted cwd. ~/.tree-agent/trusted.json is a JSON array
// of absolute directory paths; --trust or TREE_AGENT_TRUST=1 trusts
// any cwd for one run. Headless never prompts; the TUI asks once per
// untrusted cwd and a "y" is written here by trustDirectory().

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
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

// Adds `cwd` to trusted.json. A file that is not a JSON array of
// paths is left alone (it may be a hand edit gone wrong): throws.
export async function trustDirectory(
  cwd: string,
  env: NodeJS.ProcessEnv = process.env,
): Promise<void> {
  const file = userPaths(env).trusted;
  const dirs = await readTrusted(file, { strict: true });
  const target = resolve(cwd);
  if (dirs.some((dir) => resolve(dir) === target)) return;
  await mkdir(dirname(file), { recursive: true });
  const json = JSON.stringify([...dirs, target], null, 2);
  await writeFile(file, `${json}\n`);
}

async function readTrusted(
  file: string,
  { strict = false } = {},
): Promise<string[]> {
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
    if (strict) {
      throw new Error(`${file}: not a JSON array of paths; fix it`);
    }
    log.warn(`${file}: not a JSON array of paths; trusting nothing`);
    return [];
  }
}
