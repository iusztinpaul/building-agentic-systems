// The conversation as it happens: user messages, the assistant's
// streamed text (markdown: code blocks, bullets), one tool card per
// call and one-line notices for everything else. It only renders
// AgentEvents; it never decides anything.

import { Container, Markdown, Text } from "@earendil-works/pi-tui";
import type { AgentEvent, Message, ToolCall } from "../types.ts";
import { cyan, dim, markdownTheme, red, sanitize } from "./style.ts";
import { ToolCard } from "./tool-card.ts";

export type UserNote = "steer" | "follow-up";

export class Transcript extends Container {
  readonly #cwd: string;
  readonly #cards = new Map<string, ToolCard>();
  #answer: Markdown | undefined; // the turn being streamed
  #text = "";
  #expanded = false;

  constructor(cwd: string) {
    super();
    this.#cwd = cwd;
  }

  addUser(text: string, note?: UserNote): void {
    const tag = note ? dim(` (${note})`) : "";
    const line = `${cyan("›")} ${sanitize(text)}${tag}`;
    this.addChild(new Text(line, 1, 0));
  }

  addNotice(text: string, error = false): void {
    const clean = sanitize(text);
    this.addChild(new Text(error ? red(clean) : dim(clean), 1, 0));
  }

  apply(event: AgentEvent): void {
    switch (event.type) {
      case "turn_start":
        this.#text = "";
        this.#answer = this.#markdown("");
        break;
      case "text_delta":
        this.#text += event.text;
        this.#answer?.setText(sanitize(this.#text));
        break;
      case "turn_end":
        this.#answer?.setText(sanitize(event.message.content));
        this.#answer = undefined;
        if (event.message.stopReason === "aborted") {
          this.addNotice("(aborted)");
        }
        break;
      case "tool_call":
        this.#card(event.call);
        break;
      case "tool_result":
        this.#cards
          .get(event.callId)
          ?.setResult(event.content, event.isError);
        break;
      case "permission":
        this.addNotice(
          `${event.call.name}: ${event.decision} (${event.source})`,
        );
        break;
      case "tool_activation":
        this.addNotice(`activated: ${event.names.join(", ")}`);
        break;
      case "compaction":
        this.addNotice(
          `compacted: ${event.before} → ${event.after} tokens`,
        );
        break;
      case "error":
        this.addNotice(event.message, true);
        break;
      case "agent_end":
        if (event.reason === "max_turns" || event.reason === "blocked") {
          this.addNotice(`run ended: ${event.reason}`);
        }
        break;
      case "llm_call": // rows under a codemode card: task 211
        break;
    }
  }

  // A resumed session's history, drawn the way it streamed.
  load(messages: readonly Message[]): void {
    for (const m of messages) {
      if (m.role === "user") this.addUser(m.content);
      else if (m.role === "tool") {
        this.#cards.get(m.toolCallId)?.setResult(m.content, m.isError);
      } else {
        if (m.content) this.#markdown(m.content);
        for (const call of m.toolCalls) this.#card(call);
        if (m.stopReason === "aborted") this.addNotice("(aborted)");
      }
    }
  }

  // Tab: every tool result open, or every one collapsed again.
  toggleToolResults(): void {
    this.#expanded = !this.#expanded;
    for (const card of this.#cards.values()) {
      card.expanded = this.#expanded;
    }
  }

  override clear(): void {
    super.clear();
    this.#cards.clear();
    this.#answer = undefined;
  }

  #markdown(text: string): Markdown {
    const md = new Markdown(sanitize(text), 1, 0, markdownTheme);
    this.addChild(md);
    return md;
  }

  #card(call: ToolCall): void {
    const card = new ToolCard(call, this.#cwd);
    card.expanded = this.#expanded;
    this.#cards.set(call.id, card);
    this.addChild(card);
  }
}
