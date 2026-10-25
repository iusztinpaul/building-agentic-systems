// AgentSession: what the TUI and the headless CLI both drive. It owns
// the context, the steering and follow-up queues and the hooks, and
// runs one runLoop per prompt(). Front ends never touch the context:
// mid-run input goes to a queue the loop drains at a turn boundary.
// Every finalized message is appended to the session store as the
// loop produces it, so an aborted or crashed run is on disk too.

import type { LlmCallEvent } from "./codemode/llm.ts";
import {
  type CompactionSettings,
  Compactor,
  retryOnOverflow,
} from "./compaction/compaction.ts";
import { Settings } from "./config/settings.ts";
import { DEFAULT_MODEL } from "./decision/decide.ts";
import { log } from "./log.ts";
import { BLOCKED, runLoop } from "./loop/agent-loop.ts";
import type { GateResult, LoopHooks } from "./loop/hooks.ts";
import type { McpServerInfo } from "./mcp/register.ts";
import {
  type Classifier,
  type DangerCheck,
  dangerClassifier,
} from "./permissions/decision-model.ts";
import { PromptQueue, resolve, USER_DENIED } from "./permissions/gate.ts";
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
import {
  type Entry,
  type EntryBody,
  SessionStore,
  summaryMessage,
} from "./session-store/jsonl.ts";
import {
  type DiscoverSkills,
  discoverSkills,
  type Skill,
  skillMessage,
} from "./skills/skills.ts";
import { type CodemodeLimits, registerCodemode } from "./tools/codemode.ts";
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
  // Registers `codemode` (when a codemode tool exists) with this
  // session's gate, provider and cost; absent: no codemode.
  codemode?: CodemodeLimits & { tmpDir: string };
  // settings.compaction (triggerRatio from --compact-at too);
  // default: the settings defaults.
  compaction?: CompactionSettings;

  now?: () => Date;
}

// What the status bar shows; the same numbers in every front end.
export interface SessionStatus {
  model: string;
  mode: PermissionMode;
  // estimateTokens of the context (system prompt + messages), the
  // number the compaction trigger compares to triggerRatio × this.
  tokens: number;
  contextLength: number;
  cost: number; // USD, usage × pricing, llm() calls included
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
  // The `message` entry id of each of #context.messages (the summary
  // has none), for the compaction entry's firstKeptEntryId.
  #entryIds: (string | undefined)[] = [];
  readonly #compactor: Compactor;
  // The loop's provider: o.provider plus the overflow retry.
  readonly #provider: Provider;
  #cost = 0;
  #preamble = "";
  #contextFiles: ContextFile[] = [];
  #skills: Skill[] = [];
  #skillDiagnostics: string[] = [];
  #skillsListing = { text: "", dropped: [] as string[] };
  #run: AbortController | undefined;
  #ended = false; // the run's agent_end is out: nothing follows it
  #mode: PermissionMode;
  // Per session, never persisted: cleared by clear().
  readonly #sessionAllow = new Set<string>();
  readonly #verdicts = new Map<string, DangerCheck>();
  readonly #classifier: Classifier;
  readonly #prompts = new PromptQueue();
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
    this.#compactor = new Compactor({
      provider: o.provider,
      decisionModel: o.decisionModel ?? DEFAULT_MODEL,
      settings: o.compaction ?? Settings.parse({}).compaction,
      emit: (event) => this.#emit(event),
    });
    this.#provider = retryOnOverflow(o.provider, () => this.#recover());
  }

  // Throws for an unknown `resume` id (SessionNotFound) or model.
  static async create(o: SessionOptions): Promise<AgentSession> {
    const session = new AgentSession(o);
    if (o.codemode) {
      registerCodemode(o.tools, {
        ...o.codemode,
        gate: (call) => session.#gate(call),
        provider: o.provider,
        decisionModel: o.decisionModel ?? DEFAULT_MODEL,
        onLlmCall: (event, pricing) => session.#llmCall(event, pricing),
      });
    }
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
    this.#entryIds = restored.entryIds;
    this.#o.tools.activate(restored.activated);
    for (const name of this.#o.tools.activated()) this.#announced.add(name);
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
    const system = this.#context.system || this.systemPrompt();
    const { messages } = this.#context;
    return {
      model: this.#model,
      mode: this.#mode,
      tokens: this.#compactor.estimator.estimateTokens({
        system,
        messages,
      }),
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
    this.#entryIds = [];
    this.#compactor.reset();
    this.#steering.length = 0;
    this.#followUps.length = 0;
    this.#o.tools.deactivateAll();
    this.#announced.clear();
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
    this.#ended = false;
    this.#steering.push({ role: "user", content: text });
    let reason: AgentEndReason = "error";
    const off = this.on((e) => {
      if (e.type === "agent_end") reason = e.reason;
    });
    try {
      this.#context = await runLoop(this.#context, {
        provider: this.#provider,
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

  // /compact [instructions]: summarise now, with extra guidance for
  // the summariser. False when there is nothing to summarise (the
  // context fits in the kept tail); rejects when the summary fails.
  // While it runs, `running` is true and abort() cancels it.
  async compact(instructions?: string): Promise<boolean> {
    this.#idle("compact");
    const run = new AbortController();
    this.#run = run;
    try {
      return await this.#compact(instructions?.trim() || undefined);
    } finally {
      this.#run = undefined;
    }
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
      // Then the compaction trigger, against the current model.
      // The returned context is the one the loop pushes to, so the
      // session keeps it to save each message as it lands.
      prepareTurn: async (ctx) => {
        this.#context = { ...ctx, system: this.systemPrompt() };
        const contextLength = this.#info?.contextLength ?? 0;
        if (this.#compactor.due(this.#context, contextLength)) {
          await this.#compact().catch(() => false); // logged; go on
        }
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
  // A tool the caller cannot call (unknown, not activated, hidden) is
  // not gated: the registry answers it. A codemode script's calls
  // take the same path over the tools a script may call. An abort
  // while resolving records nothing: no decision was made.
  async #gate(call: ToolCall): Promise<GateResult> {
    const tools =
      call.origin === "codemode"
        ? this.#o.tools.scriptable()
        : this.#o.tools.declared();
    const tool = tools.find((t) => t.name === call.name);
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
        prompts: this.#prompts,
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

  // A script's llm() call: costed, saved (the cost survives a
  // resume) and shown under its codemode card. Never gated. A call
  // torn down by an abort can settle after agent_end: it is still
  // costed and saved, but not emitted (agent_end is a run's last).
  #llmCall(event: LlmCallEvent, pricing: ModelInfo["pricing"]): void {
    if (event.usage) this.#cost += costOf(event.usage, pricing);
    const { type, depth, model, usage } = event;
    this.#persist();
    this.#append({ type, depth, model, ...(usage && { usage }) });
    if (!this.#ended) this.#emit(event);
  }

  // Rewrites #context.messages in place (the loop holds the same
  // array) to [summary, ...tail]: every message is saved first, so the
  // tail's entries precede the compaction entry that points at them.
  async #compact(instructions?: string): Promise<boolean> {
    this.#persist();
    const result = await this.#compactor.compact(
      this.#context,
      this.#info?.contextLength ?? 0,
      { instructions, signal: this.#run?.signal },
    );
    if (!result) return false;
    const { cut, summary, before, after } = result;
    const { messages } = this.#context;
    const firstKeptEntryId = this.#entryIds[cut] ?? "";
    messages.splice(0, cut, summaryMessage(summary));
    this.#entryIds = [undefined, ...this.#entryIds.slice(cut)];
    this.#saved = messages.length;
    this.#append({ type: "compaction", summary, firstKeptEntryId });
    this.#emit({ type: "compaction", before, after });
    return true;
  }

  // A request over the window: compact once (if automatic compaction
  // is still on) and let retryOnOverflow send it again.
  async #recover(): Promise<boolean> {
    if (this.#compactor.disabled) return false;
    return this.#compact().catch(() => false);
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
    if (event.type === "agent_end") this.#ended = true;
    this.#persist();
    if (event.type === "turn_end" && event.message.usage) {
      const { usage } = event.message;
      // What was sent: everything before the answer just pushed.
      const { system, messages } = this.#context;
      const sent = { system, messages: messages.slice(0, -1) };
      this.#compactor.estimator.observe(sent, usage.inputTokens);
      this.#cost += costOf(usage, this.#info?.pricing);
    }
    for (const listener of this.#listeners) listener(event);
  }

  #persist(): void {
    const fresh = this.#context.messages.slice(this.#saved);
    for (const message of fresh) {
      this.#entryIds.push(this.#append({ type: "message", message }).id);
    }
    this.#saved = this.#context.messages.length;
  }

  #append(body: EntryBody): Entry {
    this.#id ??= this.#store.create(this.#o.cwd, this.#model);
    return this.#store.append(this.#id, body);
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
