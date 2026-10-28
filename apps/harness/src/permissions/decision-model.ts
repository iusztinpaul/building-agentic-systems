// The `auto`-mode danger classifier (spec §11.1): two questions to a
// decision model through provider.decisions(). It sees the tool, its
// primary argument and input, the cwd and the user's last 3 messages,
// and answers DangerCheck. It only ever says "dangerous"; the gate
// turns that into a prompt, never a deny.

import { createHash } from "node:crypto";
import { Settings } from "../config/settings.ts";
import { withDeadline } from "../decision/decide.ts";
import {
  type DecisionQuestion,
  type DecisionsResponse,
  type Provider,
  ProviderError,
} from "../provider/types.ts";
import { cutBytes } from "../tools/truncate.ts";
import type { ToolCall } from "../types.ts";
import { primaryArgument } from "./summary.ts";

const REASON_MAX = 200;
export const CLASSIFIER_TIMEOUT_MS = 10_000;
const INPUT_CAP_BYTES = 2 * 1024;
const RISKS = ["low", "medium", "high"] as const;

export interface DangerCheck {
  dangerous: boolean;
  risk: (typeof RISKS)[number];
  reason: string; // one line, at most REASON_MAX characters
}

// Verbatim in spec §11.1 (a fenced json block the test compares).
export const DANGER_QUESTIONS = {
  dangerous: {
    type: "noul",
    instructions:
      "You are a safety classifier for a coding/personal agent. Is " +
      "this tool call dangerous? An action is dangerous when it " +
      "deletes or overwrites files outside cwd, touches .git " +
      "internals or shell rc files, exfiltrates secrets or " +
      "credentials, changes permissions, installs software, or has " +
      "an effect the user did not ask for. Reversible, cwd-scoped " +
      "actions that serve the user's last request are not " +
      "dangerous. When unsure, answer yes.",
    criteria: {
      true:
        "The call deletes or overwrites files outside cwd, touches " +
        ".git internals or shell rc files, reads or sends secrets or " +
        "credentials, changes permissions, installs software, or " +
        "does something the user's last messages did not ask for.",
      false:
        "The call stays inside cwd, is reversible, and serves the " +
        "user's last request.",
    },
  },
  risk: {
    type: "choice",
    instructions:
      "How severe is the worst plausible outcome of this tool call " +
      "if it runs as written?",
    criteria: {
      low:
        "Reversible and scoped to cwd: reading, creating or editing " +
        "project files, running tests or builds.",
      medium:
        "Hard to undo but contained: overwriting uncommitted work, " +
        "bulk edits, network calls that send project data, " +
        "installing packages into the project.",
      high:
        "Destructive or security-relevant: deleting or overwriting " +
        "files outside cwd, .git internals, shell rc files, " +
        "credentials, permission changes, system-wide installs, " +
        "force-pushes.",
    },
  },
} as const satisfies Record<string, DecisionQuestion>;

export interface DangerRequest {
  call: ToolCall;
  cwd: string;
  recentUserMessages: string[]; // the last 3, oldest first
}

export type Classifier = (
  req: DangerRequest,
  signal?: AbortSignal, // the run's: an abort cancels the request
) => Promise<DangerCheck>;

export interface ClassifierOptions {
  model?: string; // settings.decisionModel; default its default
  threshold?: number; // permissions.dangerThreshold; same
  cache: Map<string, DangerCheck>; // one per session
  timeoutMs?: number; // default CLASSIFIER_TIMEOUT_MS
  onCost?: (usd: number) => void; // each answered request's cost
}

// Rejects on a provider error, a missing or invalid answer or the
// timeout; the gate treats a rejection as dangerous. Only answers
// that parsed are cached.
export function dangerClassifier(
  provider: Provider,
  o: ClassifierOptions,
): Classifier {
  const defaults = Settings.parse({});
  const model = o.model ?? defaults.decisionModel;
  const threshold = o.threshold ?? defaults.permissions.dangerThreshold;
  return async (req, signal) => {
    const key = hash(req.call);
    const hit = o.cache.get(key);
    if (hit) return hit;
    const deadline = {
      name: "danger_check",
      timeoutMs: o.timeoutMs ?? CLASSIFIER_TIMEOUT_MS,
      signal,
    };
    const answer = await withDeadline(deadline, (s) =>
      provider.decisions(
        { model, state: dangerState(req), questions: DANGER_QUESTIONS },
        s,
      ),
    );
    o.onCost?.(answer.usage.cost);
    const check = toCheck(answer, threshold);
    o.cache.set(key, check);
    return check;
  };
}

export function dangerState(req: DangerRequest): Record<string, unknown> {
  const { call, cwd } = req;
  const cut = (text: string): string => cutBytes(text, INPUT_CAP_BYTES);
  return {
    tool: call.name,
    primary_argument: primaryArgument(call, cwd),
    input: cut(JSON.stringify(call.arguments)),
    cwd,
    recent_user_messages: req.recentUserMessages.slice(-3).map(cut),
  };
}

// Decision models give no text: the reason is generated, e.g.
// "decision model: p(dangerous)=0.95, risk high (0.98)".
function toCheck(
  answer: DecisionsResponse,
  threshold: number,
): DangerCheck {
  const { dangerous, risk } = answer.answers;
  const level =
    risk?.type === "choice" && RISKS.find((r) => r === risk.choice);
  if (dangerous?.type !== "noul" || risk?.type !== "choice" || !level) {
    const got = JSON.stringify(answer.answers);
    throw new ProviderError(
      "invalid_response",
      `danger_check: unexpected answers ${got}`,
    );
  }
  const p = dangerous.noul.toFixed(2);
  const conf = risk.confidence.toFixed(2);
  return {
    dangerous: dangerous.noul >= threshold,
    risk: level,
    reason: `decision model: p(dangerous)=${p}, risk ${level} (${conf})`,
  };
}

// The cache key: the same (tool, input) is classified once.
function hash(call: ToolCall): string {
  return createHash("sha256")
    .update(`${call.name}\n${JSON.stringify(call.arguments)}`)
    .digest("hex");
}

// At most REASON_MAX UTF-16 units, cut with "…" and never through a
// surrogate pair.
export function shortReason(text: string): string {
  if (text.length <= REASON_MAX) return text;
  let end = REASON_MAX - 1;
  const last = text.charCodeAt(end - 1);
  if (last >= 0xd800 && last <= 0xdbff) end--; // a high surrogate
  return `${text.slice(0, end)}…`;
}
