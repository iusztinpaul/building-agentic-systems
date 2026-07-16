import { matchesRule } from "../permissions/policy";
import type { HookConfig, HookDefinition, HookEvent } from "./config";

// Hook protocol: each hook is a shell command that reads a JSON context on stdin,
// writes optional JSON on stdout, and signals deny via a non-zero exit code.
//
//   { "decision": "block", "reason": "..." }   → deny (same as exit != 0)
//   { "prompt": "new prompt" }                  → UserPromptSubmit only: replace prompt
//   anything else                               → allow + observation
//
// Matchers re-use the permissions pattern syntax ("toolName" or "toolName:prefix")
// so users don't have to learn two DSLs. Missing matcher = match any.

const HOOK_TIMEOUT_MS = 5000;

export interface HookStdoutParsed {
  decision?: "block" | "allow";
  reason?: string;
  prompt?: string;
}

export interface HookRunResult {
  command: string;
  exitCode: number;
  parsed?: HookStdoutParsed;
  raw: string;
}

export function matchHooks(
  event: HookEvent,
  cfg: HookConfig,
  toolName?: string,
  input?: Record<string, unknown>,
): HookDefinition[] {
  const defs = cfg[event] ?? [];
  if (event === "UserPromptSubmit" || event === "Stop") return defs;
  if (!toolName) return [];
  return defs.filter((d) => {
    if (!d.matcher) return true;
    return matchesRule({ pattern: d.matcher, decision: "allow" }, toolName, input ?? {});
  });
}

export async function runHook(
  def: HookDefinition,
  context: Record<string, unknown>,
): Promise<HookRunResult> {
  const proc = Bun.spawn(["bash", "-c", def.command], {
    stdin: "pipe",
    stdout: "pipe",
    stderr: "inherit",
  });

  const stdin = proc.stdin as unknown as { write: (s: string) => void; end: () => void };
  stdin.write(`${JSON.stringify(context)}\n`);
  stdin.end();

  const timeout = setTimeout(() => proc.kill(), HOOK_TIMEOUT_MS);
  try {
    const raw = await new Response(proc.stdout).text();
    const exitCode = await proc.exited;
    let parsed: HookStdoutParsed | undefined;
    const trimmed = raw.trim();
    if (trimmed) {
      try {
        parsed = JSON.parse(trimmed) as HookStdoutParsed;
      } catch {
        // Non-JSON stdout is fine — treat as observation.
      }
    }
    return { command: def.command, exitCode, parsed, raw };
  } finally {
    clearTimeout(timeout);
  }
}

// Convenience: run every matching hook for an event in order, aggregate result.
// Returns the first blocking hook's reason (if any) + any prompt mutation seen.
export async function runMatchingHooks(
  event: HookEvent,
  cfg: HookConfig,
  context: Record<string, unknown>,
  opts: { toolName?: string; input?: Record<string, unknown> } = {},
): Promise<{
  blocked: boolean;
  reason?: string;
  modifiedPrompt?: string;
  fires: HookRunResult[];
}> {
  const defs = matchHooks(event, cfg, opts.toolName, opts.input);
  const fires: HookRunResult[] = [];
  let blocked = false;
  let reason: string | undefined;
  let modifiedPrompt: string | undefined;

  for (const d of defs) {
    const result = await runHook(d, { event, ...context });
    fires.push(result);
    if (result.exitCode !== 0 || result.parsed?.decision === "block") {
      blocked = true;
      reason = reason ?? result.parsed?.reason ?? `hook exit=${result.exitCode}`;
      break;
    }
    if (event === "UserPromptSubmit" && typeof result.parsed?.prompt === "string") {
      modifiedPrompt = result.parsed.prompt;
    }
  }

  return { blocked, reason, modifiedPrompt, fires };
}
