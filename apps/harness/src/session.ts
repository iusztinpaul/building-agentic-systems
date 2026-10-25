// AgentSession: what the TUI and the headless CLI both drive. It owns
// the context, the steering and follow-up queues and the hooks, and
// runs one runLoop per prompt(). Front ends never touch the context:
// mid-run input goes to a queue the loop drains at a turn boundary.
// Every finalized message is appended to the session store as the
// loop produces it, so an aborted or crashed run is on disk too.

import { log } from "./log.ts";
import { BLOCKED, runLoop } from "./loop/agent-loop.ts";
import type { GateResult, LoopHooks } from "./loop/hooks.ts";
import type { McpServerInfo } from "./mcp/register.ts";
import {
  type Classifier,
  type DangerCheck,
  dangerClassifier,
} from "./permissions/decision-model.ts";
import { resolve, USER_DENIED } from "./permissions/gate.ts";
import {
  type ContextFile,
  formatContextFiles,
  loadContextFiles,
} from "./prompt/context-files.ts";
import {
  buildSystemPrompt,
  cwdSection,
  loadPreamble,
  mcpServersSection,
  rulesSection,
  skillsSection,
  toolsSection,
} from "./prompt/system-prompt.ts";
import type { ModelInfo, Provider } from "./provider/types.ts";
import { type EntryBody, SessionStore } from "./session-store/jsonl.ts";
import {
  type DiscoverSkills,
  discoverSkills,
  type Skill,
  skillMessage,
} from "./skills/skills.ts";
import type { ToolRegistry, ToolResult } from "./tools/registry.ts";
import { headTail } from "./tools/truncate.ts";
import type {
  AgentEndReason,
  AgentEvent,
  Context,
  Message,
  PermissionMode,
  PermissionPrompter,
  ToolCall,
  Usage,
} from "./types.ts";

export interface SessionOptions {
  provider: Provider;
  model: string; // a resumed session keeps its recorded model
  tools: ToolRegistry;
  cwd: string; // absolute
  maxTurns: number;
  trusted: boolean; // project context files load only when true
  contextMaxChars: number;
  env?: NodeJS.ProcessEnv;
  store?: SessionStore; // default: SessionStore.inMemory()
  resume?: string; // a session id in `store`
  permissionMode?: PermissionMode; // default "ask"
  // The TUI's dialog; none (the default) is headless: a call that
  // needs a prompt is denied.
  prompter?: PermissionPrompter | null;
  // A headless deny ends the run `blocked` (stop, the default) or
  // only answers the model `Blocked: …` (continue).
  onBlocked?: "stop" | "continue";
  decisionModel?: string; // the auto classifier's; settings default
  // Absent or null (--no-skills): no discovery, no `skills` section.
  // The listing may take budgetRatio × the model's contextLength.
  skills?: { budgetRatio: number; discover?: DiscoverSkills } | null;
  // The configured MCP servers, their tools already in `tools`.
  mcpServers?: readonly McpServerInfo[];

  now?: () => Date;
}

// What the status bar shows; the same numbers in every front end.
export interface SessionStatus {
  model: string;
  mode: PermissionMode;
  // The last request's input tokens (usage.inputTokens) until the
  // estimator of task 212; undefined before the first answer.
  tokens: number | undefined;
  contextLength: number;
  cost: number; // USD, usage × the model's pricing
  mcpServers: number; // connected now
  activatedTools: number;
}

type Listener = (event: AgentEvent) => void;

export class AgentSession {
  readonly #o: SessionOptions;
  readonly #store: SessionStore;
  readonly #listeners = new Set<Listener>();
  readonly #steering: Message[] = [];
  readonly #followUps: Message[] = [];
  #id: string | undefined; // set when the first entry is written
  #model: string;
  #info: ModelInfo | undefined;
  #context: Context = { system: "", messages: [] };
  #saved = 0; // messages of #context already in the store
  #tokens: number | undefined;
  #cost = 0;
  #preamble = "";
  #contextFiles: ContextFile[] = [];
  #skills: Skill[] = [];
  #skillDiagnostics: string[] = [];
  #skillsListing = { text: "", dropped: [] as string[] };
  #run: AbortController | undefined;
  #mode: PermissionMode;
  // Per session, never persisted: cleared by clear().
  readonly #sessionAllow = new Set<string>();
  readonly #verdicts = new Map<string, DangerCheck>();
  readonly #classifier: Classifier;
  // Activations already saved and shown (restored ones included).
  readonly #announced = new Set<string>();

  private constructor(o: SessionOptions) {
    this.#o = o;
    this.#store = o.store ?? SessionStore.inMemory(o.now);
    this.#model = o.model;
    this.#mode = o.permissionMode ?? "ask";
    this.#classifier = dangerClassifier(o.provider, {
      model: o.decisionModel,
      cache: this.#verdicts,
    });
  }

  // Throws for an unknown `resume` id (SessionNotFound) or model.
  static async create(o: SessionOptions): Promise<AgentSession> {
    const session = new AgentSession(o);
    if (o.resume) await session.#restore(o.resume);
    session.#info = await o.provider.modelInfo(session.#model);
    await session.reload();
    return session;
  }

  async #restore(id: string): Promise<void> {
    const restored = this.#store.load(id);
    this.#id = id;
    this.#model = restored.model;
    this.#context = { system: "", messages: restored.messages };
    this.#saved = restored.messages.length;
    this.#o.tools.activate(restored.activated);
    for (const name of this.#o.tools.activated()) this.#announced.add(name);
    this.#tokens = lastInputTokens(restored.messages);
    const prices = new Map<string, ModelInfo["pricing"]>();
    for (const { model, usage } of restored.spend) {
      if (!prices.has(model)) {
        const info = await this.#o.provider.modelInfo(model).catch(() => {
          log.warn(`no pricing for ${model}; its turns cost 0`);
          return undefined;
        });
        prices.set(model, info?.pricing ?? { prompt: 0, completion: 0 });
      }
      this.#cost += costOf(usage, prices.get(model));
    }
  }

  // Re-reads SYSTEM.md, the context files and the skills (session
  // start, /reload).
  async reload(): Promise<void> {
    const { cwd, trusted, contextMaxChars: maxChars, env } = this.#o;
    this.#preamble = await loadPreamble(env);
    this.#contextFiles = await loadContextFiles({
      cwd,
      trusted,
      maxChars,
      env,
    });
    const skills = this.#o.skills;
    if (skills) {
      const discover = skills.discover ?? discoverSkills;
      const found = await discover({ cwd, trusted, env });
      this.#skills = found.skills;
      this.#skillDiagnostics = found.diagnostics;
    }
    this.#listSkills();
  }

  get contextFiles(): readonly ContextFile[] {
    return this.#contextFiles;
  }

  // Every discovered skill, the hidden ones too (all are invocable).
  get skills(): readonly Skill[] {
    return this.#skills;
  }

  // What the front end shows once at startup: discovery diagnostics
  // and, if the listing budget cut skills, one line naming them.
  get skillNotices(): string[] {
    const notices = this.#skillDiagnostics.map((d) => `skills: ${d}`);
    const { dropped } = this.#skillsListing;
    if (dropped.length > 0) {
      const tokens = this.#skillBudget();
      notices.push(
        `skills: over the listing budget (${tokens} tokens), ` +
          `not listed: ${dropped.join(", ")}`,
      );
    }
    return notices;
  }

  // The user message for `/skill:<name> [args]`. Throws one line for
  // an unknown name (with the known ones), for disabled skills and
  // for an empty message (an empty body and no args).
  skillMessage(name: string, args: string): string {
    if (!this.#o.skills) {
      throw new Error(
        `unknown skill "${name}"; skills are disabled (--no-skills)`,
      );
    }
    const skill = this.#skills.find((s) => s.name === name);
    if (!skill) {
      const known = this.#skills.map((s) => s.name).join(", ");
      throw new Error(
        `unknown skill "${name}"; known: ${known || "(none)"}`,
      );
    }
    const message = skillMessage(skill, args);
    if (!message) {
      throw new Error(
        `skill "${name}" has an empty body and no args; nothing sent`,
      );
    }
    return message;
  }

  // The listing follows the model: its contextLength is the budget.
  #listSkills(): void {
    this.#skillsListing = skillsSection(this.#skills, this.#skillBudget());
  }

  #skillBudget(): number {
    const ratio = this.#o.skills?.budgetRatio ?? 0;
    return Math.floor(ratio * (this.#info?.contextLength ?? 0));
  }

  get messages(): readonly Message[] {
    return this.#context.messages;
  }

  // The store's id for this session; undefined until something is
  // saved (an opened-and-closed TUI leaves no file behind).
  get id(): string | undefined {
    return this.#id;
  }

  get model(): string {
    return this.#model;
  }

  get trusted(): boolean {
    return this.#o.trusted;
  }

  status(): SessionStatus {
    return {
      model: this.#model,
      mode: this.#mode,
      tokens: this.#tokens,
      contextLength: this.#info?.contextLength ?? 0,
      cost: this.#cost,
      mcpServers: this.mcpServers.filter((s) => s.client.connected).length,
      activatedTools: this.#o.tools.activated().length,
    };
  }

  get mcpServers(): readonly McpServerInfo[] {
    return this.#o.mcpServers ?? [];
  }

  // What the model sees: direct ∪ activated tool names.
  toolNames(): string[] {
    return this.#o.tools.declared().map((tool) => tool.name);
  }

  // The next prompt() uses `model`. Throws for an unknown id (the
  // provider's model list) and while a prompt runs.
  async setModel(model: string): Promise<void> {
    this.#idle("switch the model");
    const info = await this.#o.provider.modelInfo(model);
    this.#model = info.id;
    this.#info = info;
    this.#listSkills();
    this.#append({ type: "model_change", model: info.id });
  }

  get permissionMode(): PermissionMode {
    return this.#mode;
  }

  // For the rest of this session, effective from the next call (also
  // mid-run). Never written to settings.json.
  setPermissionMode(mode: PermissionMode): void {
    this.#mode = mode;
  }

  // A new, empty session (a new file on its first entry). The model,
  // the permission mode and the context files stay.
  clear(): void {
    this.#idle("clear the session");
    this.#id = undefined;
    this.#context = { system: "", messages: [] };
    this.#saved = 0;
    this.#steering.length = 0;
    this.#followUps.length = 0;
    this.#o.tools.deactivateAll();
    this.#announced.clear();
    this.#tokens = undefined;
    this.#cost = 0;
    this.#sessionAllow.clear();
    this.#verdicts.clear();
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
        model: this.#model,
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
      skills: this.#skillsListing.text,
      mcp_servers: mcpServersSection(this.mcpServers),
      cwd: cwdSection(this.#o.cwd, (this.#o.now ?? now)()),
    });
  }

  #hooks(): LoopHooks {
    return {
      // Rebuilt every turn: activation changes the tools section.
      // Task 212 adds compaction here.
      // The returned context is the one the loop pushes to, so the
      // session keeps it to save each message as it lands.
      prepareTurn: async (ctx) => {
        this.#context = { ...ctx, system: this.systemPrompt() };
        return this.#context;
      },
      beforeToolCall: (call) => this.#gate(call),
      afterToolCall: async (_call, result) => {
        this.#announceActivation();
        return capResult(result);
      },
      getSteeringMessages: () => this.#steering.splice(0),
      getFollowUpMessages: () => this.#followUps.splice(0),
      emit: (event) => this.#emit(event),
    };
  }

  // Every resolution is saved, then emitted, before the call runs.
  // A tool the model cannot call (unknown, not activated, hidden) is
  // not gated: the registry answers it. An abort while resolving
  // records nothing: no decision was made (the loop answers it).
  async #gate(call: ToolCall): Promise<GateResult> {
    const tool = this.#o.tools.declared().find((t) => t.name === call.name);
    if (!tool) return { run: true };
    const users = this.#context.messages.flatMap((m) =>
      m.role === "user" ? [m.content] : [],
    );
    const signal = this.#run?.signal;
    const resolved = await resolve(
      { call, tool, cwd: this.#o.cwd, recentUserMessages: users.slice(-3) },
      {
        mode: this.#mode,
        prompter: this.#o.prompter ?? null,
        classifier: this.#classifier,
        sessionAllow: this.#sessionAllow,
        signal,
      },
    ).catch((err: unknown) => {
      if (signal?.aborted) return undefined;
      throw err;
    });
    if (!resolved || signal?.aborted)
      return { run: false, reason: "aborted" };
    const { decision, source, reason } = resolved;
    const why = reason === undefined ? {} : { reason };
    this.#persist();
    this.#append({ type: "permission", call, decision, source, ...why });
    this.#emit({ type: "permission", call, decision, source, ...why });
    if (decision === "allow") return { run: true };
    if (source === "headless" && this.#o.onBlocked !== "continue") {
      this.#run?.abort(BLOCKED); // the rest of the batch does not run
    }
    return { run: false, reason: reason ?? USER_DENIED };
  }

  // tool_search activated tools: saved (restored by --resume) and
  // shown once, before the result that reports them.
  #announceActivation(): void {
    const names = this.#o.tools
      .activated()
      .filter((name) => !this.#announced.has(name));
    if (names.length === 0) return;
    for (const name of names) this.#announced.add(name);
    this.#persist();
    this.#append({ type: "tool_activation", names });
    this.#emit({ type: "tool_activation", names });
  }

  // The loop pushes a message before the event that reports it
  // (user → turn_start, assistant → turn_end, tool → tool_result),
  // so saving here writes each one as soon as it is final, before
  // any listener sees the event.
  #emit(event: AgentEvent): void {
    this.#persist();
    if (event.type === "turn_end" && event.message.usage) {
      this.#tokens = event.message.usage.inputTokens;
      this.#cost += costOf(event.message.usage, this.#info?.pricing);
    }
    for (const listener of this.#listeners) listener(event);
  }

  #persist(): void {
    const fresh = this.#context.messages.slice(this.#saved);
    for (const message of fresh) {
      this.#append({ type: "message", message });
    }
    this.#saved = this.#context.messages.length;
  }

  #append(body: EntryBody): void {
    this.#id ??= this.#store.create(this.#o.cwd, this.#model);
    this.#store.append(this.#id, body);
  }

  #idle(action: string): void {
    if (this.#run) {
      throw new Error(`cannot ${action} while a prompt is running`);
    }
  }
}

function costOf(
  usage: Usage,
  pricing: ModelInfo["pricing"] | undefined,
): number {
  if (!pricing) return 0;
  return (
    usage.inputTokens * pricing.prompt +
    usage.outputTokens * pricing.completion
  );
}

function lastInputTokens(messages: Message[]): number | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m?.role === "assistant" && m.usage) return m.usage.inputTokens;
  }
  return undefined;
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
