// Agent Skills (spec §9): a skill is a directory holding SKILL.md,
// whose YAML frontmatter has `name`, `description` and optionally
// `disable-model-invocation: true`. Roots, later wins on a name
// clash: ~/.tree-agent/skills, ~/.agents/skills, then — in a trusted
// cwd only — every .agents/skills from the git root down to cwd (the
// nearest dir is read last, so it wins) and <cwd>/.tree-agent/skills.
// Symlinks are followed. Problems become diagnostics, never throws.

import { existsSync } from "node:fs";
import { readdir, readFile, realpath } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { userPaths } from "../config/paths.ts";

export interface Skill {
  name: string;
  description: string;
  location: string; // absolute path of SKILL.md, as found
  body: string; // SKILL.md after the frontmatter, trimmed
  disableModelInvocation: boolean; // hidden from the prompt listing
}

export interface DiscoverOptions {
  cwd: string;
  trusted: boolean; // project roots are read only when true
  env?: NodeJS.ProcessEnv;
}

export interface Discovered {
  skills: Skill[]; // by name
  diagnostics: string[]; // "<file>: <problem>", shown once
}

export type DiscoverSkills = (o: DiscoverOptions) => Promise<Discovered>;

// The Agent Skills standard's name rule (also what /skill: routes).
const NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const NAME_MAX = 64;

// The roots in search order, each directory once.
export async function skillRoots(o: DiscoverOptions): Promise<string[]> {
  const env = o.env ?? process.env;
  const roots = [
    userPaths(env).skills,
    join(env.HOME || homedir(), ".agents", "skills"),
  ];
  if (o.trusted) {
    const cwd = resolve(o.cwd);
    for (const dir of gitRootToCwd(cwd)) {
      roots.push(join(dir, ".agents", "skills"));
    }
    roots.push(join(cwd, ".tree-agent", "skills"));
  }
  const seen = new Set<string>();
  const out: string[] = [];
  for (const root of roots) {
    const real = await realpath(root).catch(() => resolve(root));
    if (seen.has(real)) continue;
    seen.add(real);
    out.push(root);
  }
  return out;
}

export const discoverSkills: DiscoverSkills = async (o) => {
  const byName = new Map<string, Skill>();
  const diagnostics: string[] = [];
  for (const root of await skillRoots(o)) {
    const inRoot = new Map<string, { location: string; real: string }>();
    for (const entry of await entries(root, diagnostics)) {
      const location = join(root, entry, "SKILL.md");
      const text = await readSkillFile(location, diagnostics);
      if (text === undefined) continue;
      const parsed = parseSkill(text, location);
      if (typeof parsed === "string") {
        diagnostics.push(`${location}: ${parsed}`);
        continue;
      }
      const real = await realpath(location).catch(() => location);
      const twin = inRoot.get(parsed.name);
      if (twin && twin.real !== real) {
        diagnostics.push(
          `${location}: duplicate skill name "${parsed.name}" ` +
            `(also ${twin.location}); this one wins`,
        );
      }
      inRoot.set(parsed.name, { location, real });
      byName.set(parsed.name, parsed); // a later root overrides
    }
  }
  const skills = [...byName.values()].sort((a, b) =>
    a.name < b.name ? -1 : 1,
  );
  return { skills, diagnostics };
};

// A Skill, or the reason the file is not one.
export function parseSkill(text: string, location: string): Skill | string {
  let fm: Frontmatter;
  try {
    fm = parseFrontmatter(text);
  } catch (err) {
    return `bad frontmatter: ${(err as Error).message}`;
  }
  const name = fm.fields.get("name");
  if (!name) return "missing name";
  if (!NAME.test(name) || name.length > NAME_MAX) {
    return (
      `bad name "${name}": lowercase letters, digits and single ` +
      `hyphens, at most ${NAME_MAX} chars`
    );
  }
  const description = fm.fields.get("description");
  if (!description) return "missing description";
  const disable = fm.fields.get("disable-model-invocation") ?? "";
  return {
    name,
    description,
    location,
    body: fm.body,
    disableModelInvocation: /^(true|True|TRUE)$/.test(disable),
  };
}

// The user message of `/skill:<name> [args]`: `# <SKILL.md path>`
// (as context files), then the body with each `$ARGUMENTS` replaced
// by the args, or the body, a blank line and the args. Empty content
// (nothing to send) is "", header or not.
export function skillMessage(skill: Skill, args: string): string {
  const trimmed = args.trim();
  const content = skill.body.includes("$ARGUMENTS")
    ? skill.body.replaceAll("$ARGUMENTS", () => trimmed)
    : [skill.body, trimmed].filter(Boolean).join("\n\n");
  return content ? `# ${skill.location}\n${content}` : "";
}

// "/skill:<name> [args]" → its parts; anything else → undefined. The
// prefix is case-insensitive and the name may be empty, so "/skill:"
// and "/Skill:Foo" reach the exact-name lookup and its error line.
export function parseSkillCommand(
  text: string,
): { name: string; args: string } | undefined {
  const match = /^\/skill:(\S*)(?:\s+([\s\S]*))?$/i.exec(text.trim());
  if (!match) return undefined;
  return { name: match[1] ?? "", args: (match[2] ?? "").trim() };
}

// Git root first, cwd last; no git root → cwd alone.
function gitRootToCwd(cwd: string): string[] {
  const dirs: string[] = [];
  for (let dir = cwd; ; dir = dirname(dir)) {
    dirs.unshift(dir);
    if (existsSync(join(dir, ".git"))) return dirs;
    if (dirname(dir) === dir) return [cwd];
  }
}

// A root's candidate entries, sorted; a missing root is no entries.
async function entries(
  root: string,
  diagnostics: string[],
): Promise<string[]> {
  try {
    const names = await readdir(root);
    return names.filter((n) => !n.startsWith(".")).sort();
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code !== "ENOENT" && code !== "ENOTDIR") {
      diagnostics.push(`${root}: ${(err as Error).message}`);
    }
    return [];
  }
}

// Undefined when the entry is not a skill dir (no SKILL.md, a file).
async function readSkillFile(
  location: string,
  diagnostics: string[],
): Promise<string | undefined> {
  try {
    return await readFile(location, "utf8");
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code !== "ENOENT" && code !== "ENOTDIR") {
      diagnostics.push(`${location}: ${(err as Error).message}`);
    }
    return undefined;
  }
}

interface Frontmatter {
  fields: Map<string, string>; // the three keys this module reads
  body: string;
}

const WANTED = ["name", "description", "disable-model-invocation"];
// A top-level key: "…", '…' or plain (up to the first ": "), then
// the value. Odd keys (`x.y`, `"name"`) are keys like any other.
const KEY =
  /^(?:"((?:[^"\\]|\\.)*)"|'((?:[^']|'')*)'|([^\s"'#].*?))[ \t]*:(?:[ \t]+(.*))?$/;
// A column-0 block-sequence item ("- Bash"), as PyYAML dumps lists.
const LIST_ITEM = /^-(\s|$)/;

// The YAML subset SKILL.md files use: top-level `key: value` lines,
// plain, '…' and "…" scalars, | and > block scalars. Indented, blank
// and column-0 "- item" lines continue the key above (the values of
// ignored keys are skipped whole). Throws with the line number on a
// line that is no key, continuation, list item or comment.
export function parseFrontmatter(text: string): Frontmatter {
  const lines = text.replace(/^﻿/, "").split(/\r?\n/);
  if (lines[0]?.trimEnd() !== "---") {
    throw new Error("the file must start with a --- line");
  }
  const end = lines.findIndex((l, i) => i > 0 && l.trimEnd() === "---");
  if (end === -1) throw new Error("no closing --- line");
  const fields = new Map<string, string>();
  let i = 1;
  while (i < end) {
    const line = lines[i] ?? "";
    if (!line.trim() || line.startsWith("#") || LIST_ITEM.test(line)) {
      i++;
      continue;
    }
    const key = /^\s/.test(line) ? null : KEY.exec(line.trimEnd());
    if (!key) throw new Error(`line ${i + 1}: expected "key: value"`);
    let next = i + 1;
    while (next < end && isContinuation(lines[next] ?? "")) next++;
    const name = keyName(key);
    const head = key[4] ?? "";
    if (WANTED.includes(name)) {
      const rest = lines.slice(i + 1, next);
      fields.set(name, scalar(head.trim(), rest, i + 1));
    }
    i = next;
  }
  return {
    fields,
    body: lines
      .slice(end + 1)
      .join("\n")
      .trim(),
  };
}

function isContinuation(line: string): boolean {
  return /^(\s|$)/.test(line) || LIST_ITEM.test(line);
}

// The key as YAML reads it: quotes removed, escapes decoded.
function keyName(key: RegExpExecArray): string {
  const [, double, single, plain] = key;
  if (single !== undefined) return single.replaceAll("''", "'");
  if (plain !== undefined) return plain;
  try {
    return JSON.parse(`"${double ?? ""}"`) as string;
  } catch {
    return double ?? ""; // an escape JSON does not know: an odd key
  }
}

function scalar(head: string, rest: string[], lineNo: number): string {
  if (/^[|>][+-]?\d?$/.test(head)) return block(head[0] === ">", rest);
  const joined = [head, ...rest.map((l) => l.trim())]
    .filter(Boolean)
    .join(" ");
  if (head.startsWith('"')) {
    const m = /^"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$/.exec(joined);
    try {
      if (m) return (JSON.parse(`"${m[1]}"`) as string).trim();
    } catch {
      // an escape JSON does not know: reported below
    }
    throw new Error(`line ${lineNo}: bad "double-quoted" value`);
  }
  if (head.startsWith("'")) {
    const m = /^'((?:[^']|'')*)'\s*(?:#.*)?$/.exec(joined);
    if (!m) throw new Error(`line ${lineNo}: bad 'single-quoted' value`);
    return (m[1] ?? "").replaceAll("''", "'").trim();
  }
  return joined.replace(/\s+#.*$/, "").trim();
}

// | keeps the line breaks; > folds lines into one, blank → "\n".
function block(folded: boolean, rest: string[]): string {
  const indents = rest
    .filter((l) => l.trim())
    .map((l) => l.length - l.trimStart().length);
  const cut = Math.min(...indents, Number.POSITIVE_INFINITY);
  const lines = rest.map((l) => (l.trim() ? l.slice(cut) : ""));
  if (!folded) return lines.join("\n").trim();
  return lines
    .join("\n")
    .replace(/([^\n])\n(?=[^\n])/g, "$1 ")
    .trim();
}
