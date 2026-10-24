// Session persistence (spec §14): one JSONL file per session under
// <sessions>/--<cwd-slug>--/<timestamp>_<id>.jsonl, in pi's envelope
// {id, parentId, timestamp}, written linearly (parentId = the
// previous entry's id) with one appendFileSync per finalized event.
// The in-memory store keeps the same entries in arrays, for tests.

import { randomUUID } from "node:crypto";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  statSync,
} from "node:fs";
import { join, resolve } from "node:path";
import { log } from "../log.ts";
import type {
  Decision,
  DecisionSource,
  Message,
  ToolCall,
  Usage,
} from "../types.ts";

export interface Envelope {
  id: string;
  parentId: string | null;
  timestamp: string; // ISO 8601 UTC, e.g. 2026-10-24T12:00:00.000Z
}

export type EntryBody =
  | { type: "session"; version: 1; cwd: string; model: string }
  | { type: "message"; message: Message }
  | { type: "compaction"; summary: string; firstKeptEntryId: string }
  | {
      type: "permission";
      call: ToolCall;
      decision: Decision;
      source: DecisionSource;
      reason?: string;
    }
  | { type: "tool_activation"; names: string[] }
  | { type: "model_change"; model: string };

export type Entry = Envelope & EntryBody;

// What load() rebuilds from a session's entries.
export interface RestoredSession {
  id: string;
  cwd: string;
  model: string; // the header's, or the last model_change's
  messages: Message[]; // after the last compaction, summary first
  activated: string[];
  // Every assistant usage on the chain with the model that ran it,
  // so the session cost survives a resume.
  spend: { model: string; usage: Usage }[];
}

export interface SessionInfo {
  id: string;
  modified: number; // ms since the epoch
}

export interface SessionStore {
  // Starts a session: writes its `session` entry, returns its id.
  create(cwd: string, model: string): string;
  append(id: string, body: EntryBody): Entry;
  load(id: string): RestoredSession; // throws SessionNotFound
  list(cwd: string): SessionInfo[]; // newest first
}

export class SessionNotFound extends Error {
  override name = "SessionNotFound";
}

type Now = () => Date;

export const SessionStore = {
  inMemory: (now?: Now): SessionStore => new InMemorySessionStore(now),
  jsonl: (dir: string, now?: Now): SessionStore =>
    new JsonlSessionStore(dir, now),
};

// pi's directory name: "/Users/me/repo" → "--Users-me-repo--".
export function cwdSlug(cwd: string): string {
  const path = resolve(cwd).replace(/^[/\\]/, "");
  return `--${path.replace(/[/\\:]/g, "-")}--`;
}

// The compaction summary as the first context message (spec §13).
export function summaryMessage(summary: string): Message {
  return {
    role: "user",
    content: `<summary>\n${summary}\n</summary>`,
  };
}

// The chain the file means: the last entry and its ancestors, in
// order. Entries off that chain (a branch) are ignored.
export function ancestry(entries: Entry[]): Entry[] {
  const byId = new Map(entries.map((e) => [e.id, e]));
  const chain: Entry[] = [];
  const seen = new Set<string>();
  let entry = entries.at(-1);
  while (entry && !seen.has(entry.id)) {
    seen.add(entry.id);
    chain.push(entry);
    entry = entry.parentId ? byId.get(entry.parentId) : undefined;
  }
  return chain.reverse();
}

export function restore(id: string, entries: Entry[]): RestoredSession {
  const header = entries[0];
  if (header?.type !== "session") {
    throw new Error(`session ${id} has no session entry`);
  }
  let model = header.model;
  let kept: { entryId: string; message: Message }[] = [];
  let summary: Message | undefined;
  const activated = new Set<string>();
  const spend: RestoredSession["spend"] = [];
  const chain = ancestry(entries);
  if (chain[0] !== header) {
    log.warn(
      `session ${id}: the last entry's ancestry does not reach the session entry; earlier history is dropped`,
    );
  }
  for (const e of chain) {
    if (e.type === "message") {
      kept.push({ entryId: e.id, message: e.message });
      const { message: m } = e;
      if (m.role === "assistant" && m.usage) {
        spend.push({ model, usage: m.usage });
      }
    } else if (e.type === "compaction") {
      const from = kept.findIndex((k) => k.entryId === e.firstKeptEntryId);
      kept = from === -1 ? [] : kept.slice(from);
      summary = summaryMessage(e.summary);
    } else if (e.type === "tool_activation") {
      for (const name of e.names) activated.add(name);
    } else if (e.type === "model_change") {
      model = e.model;
    } else if (e.type !== "session" && e.type !== "permission") {
      log.warn(`session ${id}: skipping unknown entry type`);
    }
  }
  const messages = kept.map((k) => k.message);
  return {
    id,
    cwd: header.cwd,
    model,
    messages: summary ? [summary, ...messages] : messages,
    activated: [...activated],
    spend,
  };
}

// Entry ids are 8 hex chars, unique within their session (pi).
function entry(
  ids: Set<string>,
  parentId: string | null,
  body: EntryBody,
  now: Now,
): Entry {
  let id = randomUUID().slice(0, 8);
  while (ids.has(id)) id = randomUUID().slice(0, 8);
  ids.add(id);
  return { id, parentId, timestamp: now().toISOString(), ...body };
}

interface Open {
  ids: Set<string>;
  last: string | null; // the id the next entry points to
}

export class InMemorySessionStore implements SessionStore {
  readonly #now: Now;
  readonly #sessions = new Map<
    string,
    Open & { cwd: string; entries: Entry[]; modified: number }
  >();

  constructor(now: Now = () => new Date()) {
    this.#now = now;
  }

  create(cwd: string, model: string): string {
    const id = randomUUID();
    const dir = resolve(cwd);
    this.#sessions.set(id, {
      ids: new Set(),
      last: null,
      entries: [],
      cwd: dir,
      modified: 0,
    });
    this.append(id, { type: "session", version: 1, cwd: dir, model });
    return id;
  }

  append(id: string, body: EntryBody): Entry {
    const s = this.#get(id);
    const e = entry(s.ids, s.last, body, this.#now);
    s.entries.push(structuredClone(e));
    s.last = e.id;
    s.modified = this.#now().getTime();
    return e;
  }

  load(id: string): RestoredSession {
    return structuredClone(restore(id, this.#get(id).entries));
  }

  list(cwd: string): SessionInfo[] {
    const dir = resolve(cwd);
    return [...this.#sessions]
      .filter(([, s]) => s.cwd === dir)
      .map(([id, s]) => ({ id, modified: s.modified }))
      .reverse() // later create() first on a tie
      .sort((a, b) => b.modified - a.modified);
  }

  #get(id: string) {
    const s = this.#sessions.get(id);
    if (!s) throw new SessionNotFound(`no session ${id}`);
    return s;
  }
}

// needsNewline: the file ends without "\n" (a crash cut its last
// line), so the next append must end that line first.
type OpenFile = Open & { file: string; needsNewline: boolean };

export class JsonlSessionStore implements SessionStore {
  readonly #dir: string;
  readonly #now: Now;
  readonly #open = new Map<string, OpenFile>();

  constructor(dir: string, now: Now = () => new Date()) {
    this.#dir = dir;
    this.#now = now;
  }

  create(cwd: string, model: string): string {
    const id = randomUUID();
    const dir = join(this.#dir, cwdSlug(cwd));
    mkdirSync(dir, { recursive: true });
    // pi's file name: the ISO timestamp with ":" and "." as "-".
    const stamp = this.#now().toISOString().replace(/[:.]/g, "-");
    const file = join(dir, `${stamp}_${id}.jsonl`);
    this.#open.set(id, {
      file,
      ids: new Set(),
      last: null,
      needsNewline: false,
    });
    const header = { cwd: resolve(cwd), model };
    this.append(id, { type: "session", version: 1, ...header });
    return id;
  }

  append(id: string, body: EntryBody): Entry {
    const s = this.#open.get(id) ?? this.#reopen(id);
    const e = entry(s.ids, s.last, body, this.#now);
    const lead = s.needsNewline ? "\n" : "";
    appendFileSync(s.file, `${lead}${JSON.stringify(e)}\n`);
    s.needsNewline = false;
    s.last = e.id;
    return e;
  }

  load(id: string): RestoredSession {
    const file = this.#find(id);
    const text = readFileSync(file, "utf8");
    const entries = readEntries(file, text);
    // Read-only: cli.ts loads before trust and the TUI, so a cut
    // tail is only remembered here and mended by the next append.
    this.#open.set(id, {
      file,
      ids: new Set(entries.map((e) => e.id)),
      last: entries.at(-1)?.id ?? null,
      needsNewline: text.length > 0 && !text.endsWith("\n"),
    });
    return restore(id, entries);
  }

  list(cwd: string): SessionInfo[] {
    const dir = join(this.#dir, cwdSlug(cwd));
    if (!existsSync(dir)) return [];
    return readdirSync(dir)
      .filter((name) => /_[^_]+\.jsonl$/.test(name))
      .sort() // the timestamp prefix: oldest first, newest last
      .reverse()
      .map((name) => ({
        id: name.slice(name.lastIndexOf("_") + 1, -".jsonl".length),
        modified: statSync(join(dir, name)).mtimeMs,
      }))
      .sort((a, b) => b.modified - a.modified); // stable: ties keep
  }

  #reopen(id: string): OpenFile {
    this.load(id);
    return this.#open.get(id) as OpenFile;
  }

  #find(id: string): string {
    const name = `_${id}.jsonl`;
    if (id && existsSync(this.#dir)) {
      for (const slug of readdirSync(this.#dir)) {
        const dir = join(this.#dir, slug);
        if (!statSync(dir).isDirectory()) continue;
        const hit = readdirSync(dir).find((f) => f.endsWith(name));
        if (hit) return join(dir, hit);
      }
    }
    throw new SessionNotFound(`no session ${id}`);
  }
}

// One entry per line; a line that does not parse (e.g. cut by a
// crash) is skipped with a warning rather than failing the load.
function readEntries(file: string, text: string): Entry[] {
  const entries: Entry[] = [];
  const lines = text.split("\n");
  for (const [i, line] of lines.entries()) {
    if (!line.trim()) continue;
    try {
      const value = JSON.parse(line) as Entry;
      if (typeof value?.id === "string" && "type" in value) {
        entries.push(value);
        continue;
      }
    } catch {
      // fall through to the warning
    }
    log.warn(`${file}:${i + 1}: not a session entry; skipped`);
  }
  return entries;
}
