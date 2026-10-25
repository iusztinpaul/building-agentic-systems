// What a permission prompt (and the classifier) shows for a call:
// its primary argument (spec §11). `bash` → the command; `read`,
// `edit`, `write` → the path relative to cwd; `codemode` → its
// description and the first 2 KB of the script; any other tool (MCP)
// → the input JSON, cut at 2 KB.

import { displayPath } from "../display-path.ts";
import { cutBytes } from "../tools/truncate.ts";
import type { ToolCall } from "../types.ts";

const SUMMARY_CAP_BYTES = 2 * 1024;
const PATH_TOOLS = new Set(["read", "edit", "write"]);

export function primaryArgument(call: ToolCall, cwd: string): string {
  const args = call.arguments;
  if (call.name === "bash" && typeof args.command === "string") {
    return clip(args.command);
  }
  if (PATH_TOOLS.has(call.name) && typeof args.path === "string") {
    return displayPath(args.path, cwd);
  }
  if (call.name === "codemode") {
    const description = String(args.description ?? "");
    const script = clip(String(args.script ?? ""));
    return `${description}\n${script}`;
  }
  return clip(JSON.stringify(args));
}

export const CLIPPED = " … (cut at 2 KB)";

function clip(text: string): string {
  const cut = cutBytes(text, SUMMARY_CAP_BYTES);
  return cut === text ? text : `${cut}${CLIPPED}`;
}
