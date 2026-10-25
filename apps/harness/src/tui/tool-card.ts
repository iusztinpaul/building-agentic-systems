// One tool call in the transcript: the tool's name and its primary
// argument, then the result, collapsed to its first line until the
// user expands it (Tab). A codemode card shows its description and,
// under it, the script, collapsed the same way. Task 211 nests
// llm() rows under codemode.

import { isAbsolute, relative, resolve } from "node:path";
import {
  type Component,
  truncateToWidth,
  visibleWidth,
  wrapTextWithAnsi,
} from "@earendil-works/pi-tui";
import type { ToolCall } from "../types.ts";
import { bold, dim, green, red, sanitize, yellow } from "./style.ts";

// What identifies the call at a glance: a path relative to the cwd,
// a command's first line, else the first string argument.
export function primaryArgument(call: ToolCall, cwd: string): string {
  const args = call.arguments;
  if (call.name === "codemode" && typeof args.description === "string") {
    return args.description;
  }
  if (typeof args.path === "string") {
    const rel = relative(cwd, resolve(cwd, args.path));
    if (rel === "") return ".";
    const outside = rel.startsWith("..") || isAbsolute(rel);
    return outside ? args.path : rel;
  }
  if (typeof args.command === "string") {
    return args.command.split("\n")[0] ?? "";
  }
  const first = Object.values(args).find((v) => typeof v === "string");
  return typeof first === "string" ? first : JSON.stringify(args);
}

export class ToolCard implements Component {
  readonly call: ToolCall;
  expanded = false;
  #arg: string;
  #script: string[] | undefined; // codemode only
  #result: { content: string; isError: boolean } | undefined;

  constructor(call: ToolCall, cwd: string) {
    this.call = call;
    this.#arg = sanitize(primaryArgument(call, cwd));
    const { script } = call.arguments;
    if (call.name === "codemode" && typeof script === "string") {
      this.#script = textLines(script);
    }
  }

  setResult(content: string, isError: boolean): void {
    this.#result = { content, isError };
  }

  render(width: number): string[] {
    const r = this.#result;
    const mark = !r ? yellow("…") : r.isError ? red("✗") : green("✓");
    const head = ` ${mark} ${bold(this.call.name)} ${this.#arg}`;
    const lines = [truncateToWidth(head, width)];
    if (this.#script) lines.push(...this.#block(this.#script, dim, width));
    if (!r) return lines;
    const style = r.isError ? red : dim;
    return [...lines, ...this.#block(textLines(r.content), style, width)];
  }

  // Indented under the head: every line when expanded, else the
  // first one and how many more.
  #block(
    body: string[],
    style: (text: string) => string,
    width: number,
  ): string[] {
    if (this.expanded) {
      return body.flatMap((line) =>
        wrapTextWithAnsi(line, Math.max(1, width - 4)).map((l) =>
          style(`    ${l}`),
        ),
      );
    }
    const more = body.length > 1 ? ` (+${body.length - 1} lines, Tab)` : "";
    const room = Math.max(1, width - visibleWidth(more));
    const first = truncateToWidth(`    ${style(body[0] ?? "")}`, room);
    return [truncateToWidth(first + dim(more), width)];
  }

  invalidate(): void {}
}

function textLines(text: string): string[] {
  return sanitize(text).replace(/\t/g, "  ").split("\n");
}
