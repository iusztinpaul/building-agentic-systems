import { readFileSync, readdirSync, statSync } from "node:fs";
import { basename, join } from "node:path";
import type { Message } from "../messages";
import { sessionsDirFor } from "./paths";
import type { SessionLine } from "./store";

export interface SessionSummary {
  id: string;
  path: string;
  cwd: string;
  firstPrompt: string;
  startedAt: string;
  mtime: number;
}

export function listSessions(cwd: string, limit = 10): SessionSummary[] {
  const dir = sessionsDirFor(cwd);
  let entries: string[];
  try {
    entries = readdirSync(dir).filter((f) => f.endsWith(".jsonl"));
  } catch {
    return [];
  }

  const out: SessionSummary[] = [];
  for (const name of entries) {
    const path = join(dir, name);
    try {
      const summary = summarize(path, cwd);
      if (summary) out.push(summary);
    } catch {
      // skip unreadable / corrupt
    }
  }
  out.sort((a, b) => b.mtime - a.mtime);
  return out.slice(0, limit);
}

export function findMostRecent(cwd: string): SessionSummary | null {
  const [first] = listSessions(cwd, 1);
  return first ?? null;
}

export function loadSession(path: string): { messages: Message[]; sessionId: string | null } {
  const text = readFileSync(path, "utf-8");
  const messages: Message[] = [];
  let sessionId: string | null = null;
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    let obj: SessionLine;
    try {
      obj = JSON.parse(line);
    } catch {
      continue;
    }
    if (obj.kind === "meta") {
      sessionId = obj.sessionId;
    } else if (obj.kind === "message") {
      messages.push({ role: obj.role, content: obj.content });
    }
  }
  return { messages, sessionId };
}

function summarize(path: string, fallbackCwd: string): SessionSummary | null {
  const text = readFileSync(path, "utf-8");
  let cwd = fallbackCwd;
  let startedAt = "";
  let firstPrompt = "";

  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    let obj: SessionLine;
    try {
      obj = JSON.parse(line);
    } catch {
      continue;
    }
    if (obj.kind === "meta") {
      cwd = obj.cwd ?? cwd;
      startedAt = startedAt || obj.ts;
    } else if (obj.kind === "message" && obj.role === "user") {
      const raw = typeof obj.content === "string" ? obj.content : "";
      if (raw) {
        firstPrompt = raw;
        break;
      }
    }
  }
  if (!firstPrompt) return null;

  const stat = statSync(path);
  return {
    id: basename(path, ".jsonl"),
    path,
    cwd,
    firstPrompt: firstPrompt.slice(0, 80),
    startedAt: startedAt || new Date(stat.mtimeMs).toISOString(),
    mtime: stat.mtimeMs,
  };
}
