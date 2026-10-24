// AgentSession: what the TUI and the headless CLI both drive. It owns
// the context, the steering and follow-up queues and the hooks, and
// runs one runLoop per prompt(). Front ends never touch the context:
// mid-run input goes to a queue the loop drains at a turn boundary.

import { runLoop } from "./loop/agent-loop.ts";
import type { LoopHooks } from "./loop/hooks.ts";
import {
  type ContextFile,
  formatContextFiles,
  loadContextFiles,
} from "./prompt/context-files.ts";
import {
  buildSystemPrompt,
  cwdSection,
  loadPreamble,
  rulesSection,
  toolsSection,
} from "./prompt/system-prompt.ts";
import type { Provider } from "./provider/types.ts";
import type { ToolRegistry, ToolResult } from "./tools/registry.ts";
import { headTail } from "./tools/truncate.ts";
import type {
  AgentEndReason,
  AgentEvent,
  Context,
  Message,
} from "./types.ts";

// Where finished messages go. In memory for now; task 205 adds the
// JSONL store behind the same interface.
export interface SessionStore {
  append(message: Message): void;
  load(): Message[];
}

export class InMemorySessionStore implements SessionStore {
  readonly #messages: Message[] = [];

  append(message: Message): void {
    this.#messages.push(structuredClone(message));
  }

  load(): Message[] {
    return structuredClone(this.#messages);
  }
}

export interface SessionOptions {
  provider: Provider;
  model: string;
  tools: ToolRegistry;
  cwd: string; // absolute
  maxTurns: number;
  trusted: boolean; // project context files load only when true
  contextMaxChars: number;
  env?: NodeJS.ProcessEnv;
  store?: SessionStore;
  now?: () => Date;
}

type Listener = (event: AgentEvent) => void;

export class AgentSession {
  readonly #o: SessionOptions;
  readonly #store: SessionStore;
  readonly #listeners = new Set<Listener>();
  readonly #steering: Message[] = [];
  readonly #followUps: Message[] = [];
  #context: Context;
  #saved: number; // messages already in the store
  #preamble = "";
  #contextFiles: ContextFile[] = [];
  #run: AbortController | undefined;

  private constructor(o: SessionOptions) {
    this.#o = o;
    this.#store = o.store ?? new InMemorySessionStore();
    this.#context = { system: "", messages: this.#store.load() };
    this.#saved = this.#context.messages.length;
  }

  static async create(o: SessionOptions): Promise<AgentSession> {
    const session = new AgentSession(o);
    await session.reload();
    return session;
  }

  // Re-reads SYSTEM.md and the context files (session start, /reload).
  async reload(): Promise<void> {
    const { cwd, trusted, contextMaxChars: maxChars, env } = this.#o;
    this.#preamble = await loadPreamble(env);
    this.#contextFiles = await loadContextFiles({
      cwd,
      trusted,
      maxChars,
      env,
    });
  }

  get contextFiles(): readonly ContextFile[] {
    return this.#contextFiles;
  }

  get messages(): readonly Message[] {
    return this.#context.messages;
  }

  get running(): boolean {
    return this.#run !== undefined;
  }

  on(listener: Listener): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  // Runs the loop to its end. Queued steering (e.g. left over from an
  // aborted run) is delivered first, then `text`; queued follow-ups
  // run after it.
  async prompt(text: string): Promise<AgentEndReason> {
    if (this.#run) {
      throw new Error("a prompt is running; use steer() or followUp()");
    }
    const run = new AbortController();
    this.#run = run;
    this.#steering.push({ role: "user", content: text });
    let reason: AgentEndReason = "error";
    const off = this.on((e) => {
      if (e.type === "agent_end") reason = e.reason;
    });
    try {
      this.#context = await runLoop(this.#context, {
        provider: this.#o.provider,
        model: this.#o.model,
        tools: this.#o.tools,
        hooks: this.#hooks(),
        maxTurns: this.#o.maxTurns,
        signal: run.signal,
        cwd: this.#o.cwd,
      });
    } catch (err) {
      this.#emit({ type: "error", message: (err as Error).message });
      this.#emit({ type: "agent_end", reason: "error" });
    } finally {
      off();
      this.#run = undefined;
      this.#persist();
    }
    return reason;
  }

  steer(text: string): void {
    this.#steering.push({ role: "user", content: text });
  }

  followUp(text: string): void {
    this.#followUps.push({ role: "user", content: text });
  }

  // Ends the running prompt; queued messages stay queued.
  abort(): void {
    this.#run?.abort();
  }

  systemPrompt(): string {
    return buildSystemPrompt({
      preamble: this.#preamble,
      tools: toolsSection(this.#o.tools.declared()),
      rules: rulesSection(),
      project_context: formatContextFiles(this.#contextFiles),
      cwd: cwdSection(this.#o.cwd, (this.#o.now ?? now)()),
    });
  }

  #hooks(): LoopHooks {
    return {
      // Rebuilt every turn: activation changes the tools section.
      // Task 212 adds compaction here.
      prepareTurn: async (ctx) => ({ ...ctx, system: this.systemPrompt() }),
      // Pass-through: every call runs until task 206 replaces this
      // with the permission gate.
      beforeToolCall: async () => ({ run: true }),
      afterToolCall: async (_call, result) => capResult(result),
      getSteeringMessages: () => this.#steering.splice(0),
      getFollowUpMessages: () => this.#followUps.splice(0),
      emit: (event) => this.#emit(event),
    };
  }

  #emit(event: AgentEvent): void {
    for (const listener of this.#listeners) listener(event);
  }

  #persist(): void {
    const fresh = this.#context.messages.slice(this.#saved);
    for (const message of fresh) this.#store.append(message);
    this.#saved = this.#context.messages.length;
  }
}

// The truncation point: the built-in tools cap their own output at
// 50 KB; this backstop keeps head + tail of anything over 64 KB.
export const RESULT_CAP_BYTES = 64 * 1024;

function capResult(result: ToolResult): ToolResult {
  const content = headTail(result.content, RESULT_CAP_BYTES);
  return content === result.content ? result : { ...result, content };
}

function now(): Date {
  return new Date();
}
