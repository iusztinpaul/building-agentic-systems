// Permission policy — evaluate a tool call against a list of allow/deny rules.
//
// Pattern format is deliberately minimal for M4:
//   "toolName"           → match any call to that tool
//   "toolName:prefix"    → match if the tool's "argument string" starts with prefix
//                          (bash  → command; read/write/edit → file_path; glob/grep/todo ignored)
// First matching rule wins; no match ⇒ "ask".

export type Decision = "allow" | "deny" | "ask";

export interface Rule {
  pattern: string;
  decision: "allow" | "deny";
}

function argumentString(toolName: string, input: Record<string, unknown>): string {
  if (toolName === "bash") return typeof input.command === "string" ? input.command : "";
  if (typeof input.file_path === "string") return input.file_path;
  return "";
}

export function matchesRule(rule: Rule, toolName: string, input: Record<string, unknown>): boolean {
  const colon = rule.pattern.indexOf(":");
  const name = colon === -1 ? rule.pattern : rule.pattern.slice(0, colon);
  if (name !== toolName) return false;
  if (colon === -1) return true;
  const prefix = rule.pattern.slice(colon + 1);
  return argumentString(toolName, input).startsWith(prefix);
}

export function evaluateRules(
  toolName: string,
  input: Record<string, unknown>,
  rules: Rule[],
): Decision {
  for (const r of rules) {
    if (matchesRule(r, toolName, input)) return r.decision;
  }
  return "ask";
}

// Suggest an "allow pattern" for a tool call. Presented to the user in the
// permission dialog as the "allow similar" option.
//
//   bash  "git status"          → "bash:git "
//   bash  "npm install"         → "bash:npm "
//   edit  "/repo/src/foo.ts"    → "edit:/repo/src/"
//   write "relative/file.txt"   → "write:relative/"
//   read  "apps/memory/..."     → "read:apps/memory/"
export function suggestPattern(toolName: string, input: Record<string, unknown>): string {
  if (toolName === "bash" && typeof input.command === "string") {
    const first = input.command.trim().split(/\s+/)[0] ?? "";
    return first ? `bash:${first} ` : "bash";
  }
  if (typeof input.file_path === "string") {
    const p = input.file_path;
    const slash = p.lastIndexOf("/");
    const dir = slash >= 0 ? p.slice(0, slash + 1) : "";
    return dir ? `${toolName}:${dir}` : toolName;
  }
  return toolName;
}
