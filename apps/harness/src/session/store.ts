import { appendFileSync } from "node:fs";
import { dirname } from "node:path";
import type { Message } from "../messages";
import { ensureDir } from "./paths";

// Each session is a JSONL file; each line is one of:
//   { kind: "meta",    ts, cwd, sessionId }
//   { kind: "message", ts, role, content }          // verbatim Message
//   { kind: "event",   ts, name, data }             // side events (permissions, errors, …)

export interface MetaLine {
  kind: "meta";
  ts: string;
  cwd: string;
  sessionId: string;
}
export interface MessageLine {
  kind: "message";
  ts: string;
  role: Message["role"];
  content: Message["content"];
}
export interface EventLine {
  kind: "event";
  ts: string;
  name: string;
  data: unknown;
}

export type SessionLine = MetaLine | MessageLine | EventLine;

export class SessionStore {
  readonly path: string;
  readonly sessionId: string;

  constructor(path: string, sessionId: string, cwd: string, fresh: boolean) {
    this.path = path;
    this.sessionId = sessionId;
    ensureDir(dirname(path));
    // Always append a meta line so resumed sessions get a visible restart marker.
    this.writeLine({ kind: "meta", ts: now(), cwd, sessionId });
    if (!fresh) this.writeLine({ kind: "event", ts: now(), name: "resumed", data: { sessionId } });
  }

  appendMessage(message: Message): void {
    this.writeLine({
      kind: "message",
      ts: now(),
      role: message.role,
      content: message.content,
    });
  }

  appendEvent(name: string, data: unknown): void {
    this.writeLine({ kind: "event", ts: now(), name, data });
  }

  private writeLine(obj: SessionLine): void {
    appendFileSync(this.path, `${JSON.stringify(obj)}\n`);
  }
}

function now(): string {
  return new Date().toISOString();
}
