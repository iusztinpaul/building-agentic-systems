// The stdio transport: spawn `command args` (cwd = the session's,
// `env` merged over the base env), one JSON-RPC frame per line on
// stdin/stdout. The child's stderr goes to the harness log at info,
// never to the user's stdout. The child gets its own process group,
// so a signal also reaches what it started (`uv run` → python), and
// close() ends stdin, then escalates to SIGTERM and SIGKILL of the
// group; a harness exit or an unhandled SIGINT / SIGTERM kills every
// live group. No wait is unbounded: a pipe held by a process that
// left the group would otherwise keep "close" from ever firing, so
// close() then drops the harness's ends and the event loop drains.

import { type ChildProcess, spawn } from "node:child_process";
import { once } from "node:events";
import { constants } from "node:os";
import { createInterface, type Interface } from "node:readline";
import { log as defaultLog, type Logger } from "../log.ts";
import type {
  JsonRpcMessage,
  Transport,
  TransportHandlers,
} from "./jsonrpc.ts";

export interface StdioOptions {
  command: string;
  args: string[];
  env: Record<string, string>;
  cwd?: string;
  baseEnv?: NodeJS.ProcessEnv; // default process.env
  label?: string; // e.g. `mcp "tree-memory-local": `
  log?: Logger;
}

const TERM_AFTER_MS = 1_000; // stdin closed, still running
const KILL_AFTER_MS = 3_000;
const GIVE_UP_AFTER_KILL_MS = 1_000; // then stop waiting for "close"

// Spawned detached, the child leads its own group (pgid = pid), so
// the group outlives it while a grandchild still runs.
function signalGroup(child: ChildProcess, signal: NodeJS.Signals): void {
  try {
    if (child.pid) process.kill(-child.pid, signal);
  } catch {
    // the group is gone
  }
}

// Resolves on the child's "close" or after `ms`, whichever is first.
async function closedWithin(
  child: ChildProcess,
  ms: number,
): Promise<void> {
  let timer: NodeJS.Timeout | undefined;
  await Promise.race([
    once(child, "close").catch(() => {}),
    new Promise<void>((resolve) => {
      timer = setTimeout(resolve, ms);
    }),
  ]);
  clearTimeout(timer);
}

// One set of process hooks for all children (one per transport
// would trip MaxListeners): an exit SIGKILLs every live group, and a
// signal nobody else handles (a second Ctrl+C, after the headless
// abort's `once` is spent) ends them before the harness dies, since
// Node's default signal death fires no "exit" event.
const live = new Set<ChildProcess>();
let hooked = false;
function track(child: ChildProcess): void {
  live.add(child);
  child.once("close", () => live.delete(child));
  if (hooked) return;
  hooked = true;
  process.once("exit", () => {
    for (const each of live) signalGroup(each, "SIGKILL");
  });
  // Prepended: it must run while a headless `once` is still counted.
  for (const signal of SIGNALS) {
    process.prependListener(signal, () => void handleSignal(signal));
  }
}

const SIGNALS = ["SIGINT", "SIGTERM"] as const;
export type HarnessSignal = (typeof SIGNALS)[number];

export interface SignalTarget {
  listenerCount(signal: HarnessSignal): number;
  exit(code: number): void;
}

// The signal hook. Another listener (the headless abort) means the
// run winds down and closes its clients; alone, it does what Node's
// default would, after the children: SIGTERM, SIGKILL after the
// grace, then exit 128 + the signal number (130, 143).
export async function handleSignal(
  signal: HarnessSignal,
  target: SignalTarget = process,
): Promise<void> {
  if (target.listenerCount(signal) > 1) return;
  const children = [...live]; // none has fired "close" yet
  const grace = KILL_AFTER_MS - TERM_AFTER_MS;
  const closed = children.map((child) =>
    closedWithin(child, grace + GIVE_UP_AFTER_KILL_MS),
  );
  for (const child of children) signalGroup(child, "SIGTERM");
  const kill = setTimeout(() => {
    for (const child of children) signalGroup(child, "SIGKILL");
  }, grace);
  await Promise.all(closed);
  clearTimeout(kill);
  target.exit(128 + constants.signals[signal]);
}

export class StdioTransport implements Transport {
  readonly #o: StdioOptions;
  readonly #log: Logger;
  readonly #label: string;
  #child: ChildProcess | undefined;
  #readers: Interface[] = [];
  #closing = false;
  #closed = false; // "close" fired: the pipes are shut
  #closeDone: Promise<void> | undefined; // a second close() waits on it

  constructor(options: StdioOptions) {
    this.#o = options;
    this.#log = options.log ?? defaultLog;
    this.#label = options.label ?? "";
  }

  get pid(): number | undefined {
    return this.#child?.pid;
  }

  async start(handlers: TransportHandlers): Promise<void> {
    const { command, args, cwd } = this.#o;
    const env = { ...(this.#o.baseEnv ?? process.env), ...this.#o.env };
    const child = spawn(command, args, {
      cwd,
      env,
      detached: true, // own process group: see signalGroup
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.#child = child;
    // Before "spawn", a failure is start()'s rejection, said once.
    let spawned = false;
    child.on("error", (err) => {
      if (spawned) this.#log.warn(`${this.#label}${err.message}`);
    });
    child.stdin?.on("error", (err) =>
      this.#log.debug(`${this.#label}stdin: ${err.message}`),
    );
    this.#readers = [
      lines(child.stdout, (line) => this.#frame(line, handlers)),
      lines(child.stderr, (line) =>
        this.#log.info(`${this.#label}${line}`),
      ),
    ].filter((reader) => reader !== undefined);
    // "close", not "exit": stdout is drained first, so a reply
    // written just before exiting still reaches its call.
    child.once("close", (code, signal) => {
      this.#closed = true;
      const how = signal ? `signal ${signal}` : `code ${code}`;
      const error = new Error(`${this.#label}server exited (${how})`);
      if (spawned && !this.#closing) this.#log.warn(error.message);
      handlers.onClose(error);
    });
    try {
      await once(child, "spawn");
      spawned = true;
    } catch (err) {
      throw new Error(
        `${this.#label}cannot start ${command}: ${(err as Error).message}`,
      );
    }
    track(child);
  }

  async send(message: JsonRpcMessage): Promise<void> {
    const stdin = this.#child?.stdin;
    if (!stdin?.writable || this.#exited()) {
      throw new Error(`${this.#label}not connected`);
    }
    const line = `${JSON.stringify(message)}\n`;
    await new Promise<void>((resolve, reject) =>
      stdin.write(line, (err) => (err ? reject(err) : resolve())),
    );
  }

  close(): Promise<void> {
    if (!this.#child) return Promise.resolve(); // never started
    this.#closeDone ??= this.#close();
    return this.#closeDone;
  }

  async #close(): Promise<void> {
    this.#closing = true;
    const child = this.#child;
    // Not #exited(): the child may be gone while its group holds the
    // pipes. No pid: it never started.
    if (!child?.pid || this.#closed) return;
    const closed = closedWithin(
      child,
      KILL_AFTER_MS + GIVE_UP_AFTER_KILL_MS,
    );
    child.stdin?.end();
    const term = setTimeout(
      () => signalGroup(child, "SIGTERM"),
      TERM_AFTER_MS,
    );
    const kill = setTimeout(
      () => signalGroup(child, "SIGKILL"),
      KILL_AFTER_MS,
    );
    await closed;
    clearTimeout(term);
    clearTimeout(kill);
    if (this.#closed) return;
    // Given up: an orphan holds the pipes. Release our ends, or their
    // handles keep the harness alive until the orphan exits.
    for (const reader of this.#readers) reader.close();
    child.stdout?.destroy();
    child.stderr?.destroy();
  }

  #exited(): boolean {
    const child = this.#child;
    return !child || child.exitCode !== null || child.signalCode !== null;
  }

  #frame(line: string, handlers: TransportHandlers): void {
    if (!line.trim()) return;
    let message: unknown;
    try {
      message = JSON.parse(line);
    } catch {
      this.#log.warn(
        `${this.#label}not JSON on stdout: ${line.slice(0, 200)}`,
      );
      return;
    }
    handlers.onMessage(message);
  }
}

function lines(
  stream: NodeJS.ReadableStream | null,
  onLine: (line: string) => void,
): Interface | undefined {
  return stream
    ? createInterface({ input: stream }).on("line", onLine)
    : undefined;
}
