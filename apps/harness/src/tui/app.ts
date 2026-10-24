// The TUI (spec §15): transcript, a spinner while a run is going,
// the prompt editor and the status bar, on pi-tui's main-screen
// renderer (the terminal keeps its scrollback). Components render
// AgentEvents and call AgentSession's public methods; nothing here
// is a capability headless mode lacks.

import {
  Container,
  Loader,
  type Terminal,
  type TUI,
  TuiMainScreen,
} from "@earendil-works/pi-tui";
import type { AgentSession } from "../session.ts";
import type { PermissionPrompter } from "../types.ts";
import { COMMANDS, runCommand } from "./commands.ts";
import { type InputAction, PromptInput, route } from "./input.ts";
import { tuiPrompter } from "./permission-prompt.ts";
import { StatusBar } from "./status-bar.ts";
import { cyan, dim } from "./style.ts";
import { Transcript } from "./transcript.ts";
import { TrustPrompt } from "./trust-prompt.ts";

export interface TuiOptions {
  terminal: Terminal;
  cwd: string;
  askTrust: boolean; // an untrusted cwd: ask before opening
  // Builds the session once trust is settled; `trust` is the user's
  // "y" (the caller records it in trusted.json). `prompter` asks
  // permission questions on this screen.
  open(trust: boolean, prompter: PermissionPrompter): Promise<AgentSession>;
  onStart?(tui: TUI): void; // tests read the screen through it
}

// Resolves with the exit code (0 on /exit or Ctrl+C when idle).
// Errors from open() propagate after the terminal is restored.
export async function runTui(o: TuiOptions): Promise<number> {
  const tui = new TuiMainScreen(o.terminal);
  tui.start();
  o.onStart?.(tui);
  try {
    const trust = o.askTrust ? await askTrust(tui, o.cwd) : false;
    const session = await o.open(trust, tuiPrompter(tui));
    return await new App(tui, session, o.cwd).done;
  } finally {
    tui.stop();
  }
}

function askTrust(tui: TUI, cwd: string): Promise<boolean> {
  return new Promise((resolve) => {
    const prompt = new TrustPrompt(cwd, (trust) => {
      tui.removeChild(prompt);
      tui.requestRender();
      resolve(trust);
    });
    tui.addChild(prompt);
    tui.setFocus(prompt);
    tui.requestRender();
  });
}

class App {
  readonly done: Promise<number>;
  readonly #tui: TUI;
  readonly #session: AgentSession;
  readonly #transcript: Transcript;
  readonly #spinner = new Container();
  readonly #status = new StatusBar();
  #loader: Loader | undefined;
  #run: Promise<unknown> | undefined;
  #exit: (code: number) => void = () => {};

  constructor(tui: TUI, session: AgentSession, cwd: string) {
    this.#tui = tui;
    this.#session = session;
    this.done = new Promise((resolve) => {
      this.#exit = resolve;
    });
    this.#transcript = new Transcript(cwd);
    const input = new PromptInput(
      tui,
      {
        submit: (text, alt) => this.#submit(text, alt),
        escape: () => session.abort(),
        tab: () => {
          this.#transcript.toggleToolResults();
          this.#render();
        },
        toggleMode: () => {
          const next = session.permissionMode === "ask" ? "auto" : "ask";
          session.setPermissionMode(next);
          this.#transcript.addNotice(`permission mode: ${next}`);
          this.#render();
        },
        interrupt: () => {
          if (session.running) session.abort();
          else void this.#quit();
        },
      },
      COMMANDS,
      cwd,
    );
    this.#transcript.load(session.messages);
    if (session.messages.length > 0) {
      this.#transcript.addNotice(`resumed session ${session.id}`);
    }
    for (const c of [
      this.#transcript,
      this.#spinner,
      input,
      this.#status,
    ]) {
      tui.addChild(c);
    }
    tui.setFocus(input);
    session.on((event) => {
      this.#transcript.apply(event);
      this.#render();
    });
    this.#render();
  }

  #submit(text: string, alt: boolean): void {
    const action = route(text, this.#session.running, alt);
    if (action) this.#act(action);
    this.#render();
  }

  #act(action: InputAction): void {
    const session = this.#session;
    switch (action.kind) {
      case "prompt":
        this.#transcript.addUser(action.text);
        this.#run = this.#prompt(action.text);
        break;
      case "steer":
        session.steer(action.text);
        this.#transcript.addUser(action.text, "steer");
        break;
      case "followUp":
        session.followUp(action.text);
        this.#transcript.addUser(action.text, "follow-up");
        break;
      case "command":
        void this.#command(action.name, action.arg);
        break;
    }
  }

  async #prompt(text: string): Promise<void> {
    this.#loader = new Loader(
      this.#tui,
      cyan,
      dim,
      "working… (Esc aborts, Enter steers, Alt+Enter queues)",
    );
    this.#spinner.addChild(this.#loader);
    try {
      await this.#session.prompt(text);
    } finally {
      this.#loader.stop();
      this.#spinner.clear();
      this.#render();
    }
  }

  async #command(name: string, arg: string): Promise<void> {
    if (name === "exit") return this.#quit();
    const result = await runCommand(this.#session, name, arg);
    if (result.cleared) this.#transcript.clear();
    for (const line of result.lines) {
      this.#transcript.addNotice(line, result.error);
    }
    this.#render();
  }

  // Ends a running prompt first, so its partial answer is saved.
  async #quit(): Promise<void> {
    this.#session.abort();
    await this.#run?.catch(() => {});
    this.#exit(0);
  }

  #render(): void {
    this.#status.set(this.#session.status());
    this.#tui.requestRender();
  }
}
