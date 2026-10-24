// One tool call in the transcript: the tool's name and its primary
// argument, then the result, collapsed to its first line until the
// user expands it (Tab). Task 211 nests llm() rows under codemode.

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
  #result: { content: string; isError: boolean } | undefined;

  constructor(call: ToolCall, cwd: string) {
    this.call = call;
    this.#arg = sanitize(primaryArgument(call, cwd));
  }

  setResult(content: string, isError: boolean): void {
    this.#result = { content, isError };
  }

  render(width: number): string[] {
    const r = this.#result;
    const mark = !r ? yellow("…") : r.isError ? red("✗") : green("✓");
    const head = ` ${mark} ${bold(this.call.name)} ${this.#arg}`;
    const lines = [truncateToWidth(head, width)];
    if (!r) return lines;
    const body = sanitize(r.content).replace(/\t/g, "  ").split("\n");
    const style = r.isError ? red : dim;
    if (this.expanded) {
      for (const line of body) {
        const wrapped = wrapTextWithAnsi(line, Math.max(1, width - 4));
        lines.push(...wrapped.map(style));
      }
      return lines.map((l, i) => (i === 0 ? l : `    ${l}`));
    }
    const more = body.length > 1 ? ` (+${body.length - 1} lines, Tab)` : "";
    const room = Math.max(1, width - visibleWidth(more));
    const first = truncateToWidth(`    ${style(body[0] ?? "")}`, room);
    return [...lines, truncateToWidth(first + dim(more), width)];
  }

  invalidate(): void {}
}
