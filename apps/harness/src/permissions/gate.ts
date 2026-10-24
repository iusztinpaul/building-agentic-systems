// The permission gate (spec §11), first match wins: read-only →
// allow; allowed for this session → allow; `ask` → prompt; `auto` →
// the classifier, prompting only on dangerous or failure (fail-
// closed). A prompt without a prompter (headless) is a deny. Calls
// from a codemode script take the same path: nothing here looks at
// where a call came from.

import { ZodError } from "zod";
import { log } from "../log.ts";
import type { ToolDefinition } from "../tools/registry.ts";
import type {
  Decision,
  DecisionSource,
  PermissionMode,
  PermissionPrompter,
  ToolCall,
} from "../types.ts";
import {
  type Classifier,
  type DangerCheck,
  shortReason,
} from "./decision-model.ts";
import { primaryArgument } from "./summary.ts";

export interface PermissionRequest {
  call: ToolCall;
  tool: ToolDefinition;
  cwd: string;
  recentUserMessages: string[];
}

export interface GateDeps {
  mode: PermissionMode;
  prompter: PermissionPrompter | null; // null: headless
  classifier: Classifier;
  sessionAllow: Set<string>; // tool names answered `session`
  signal?: AbortSignal; // the run's
}

export interface Resolution {
  decision: Decision;
  source: DecisionSource;
  // The classifier's reason, or why the call was denied.
  reason?: string;
}

export const HEADLESS_ASK =
  "needs approval (permission mode ask) and a headless run cannot " +
  "prompt";
export const USER_DENIED = "denied by the user";

// An abort during the classifier call rejects with the signal's
// reason: nothing was decided, so nothing is prompted.
export async function resolve(
  req: PermissionRequest,
  deps: GateDeps,
): Promise<Resolution> {
  const { tool } = req;
  if (tool.isReadOnly) return { decision: "allow", source: "readOnly" };
  if (deps.sessionAllow.has(tool.name)) {
    return { decision: "allow", source: "session" };
  }
  let reason: string | undefined;
  if (deps.mode === "auto") {
    const verdict = await classify(deps.classifier, req, deps.signal);
    if (!verdict.dangerous) {
      return {
        decision: "allow",
        source: "classifier",
        reason: verdict.reason,
      };
    }
    reason = verdict.reason;
  }
  if (!deps.prompter) {
    return {
      decision: "deny",
      source: "headless",
      reason: reason ? `dangerous: ${reason}` : HEADLESS_ASK,
    };
  }
  const summary = primaryArgument(req.call, req.cwd);
  const answer = await deps.prompter.ask(req.call, summary, reason);
  if (answer === "deny") {
    return { decision: "deny", source: "user", reason: USER_DENIED };
  }
  if (answer === "session") deps.sessionAllow.add(tool.name);
  return { decision: "allow", source: "user", reason };
}

// Fail-closed: an error, a parse failure or a timeout is dangerous.
async function classify(
  classifier: Classifier,
  req: PermissionRequest,
  signal: AbortSignal | undefined,
): Promise<DangerCheck> {
  try {
    return await classifier(req, signal);
  } catch (err) {
    if (signal?.aborted) throw err;
    const message = oneLine(err);
    log.info(`danger_check failed, treated as dangerous: ${message}`);
    return {
      dangerous: true,
      risk: "high",
      reason: shortReason(`the safety classifier failed (${message})`),
    };
  }
}

// A zod error's first issue, or the message, on one line.
function oneLine(err: unknown): string {
  const first = err instanceof ZodError ? err.issues[0] : undefined;
  const text = first
    ? `${first.path.join(".") || "(root)"}: ${first.message}`
    : String((err as Error)?.message ?? err);
  return text.replace(/\s+/g, " ").trim();
}
