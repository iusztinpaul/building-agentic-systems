import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// settings.json shape:
//   {
//     "hooks": {
//       "PreToolUse": [ { "matcher": "bash:rm ", "command": "echo ..." }, ... ],
//       "PostToolUse": [...],
//       "UserPromptSubmit": [...],
//       "Stop": [...]
//     }
//   }
//
// Matcher uses the same pattern syntax as permissions: "toolName" or
// "toolName:prefix". UserPromptSubmit + Stop ignore the matcher.
//
// Project settings (./.tree/settings.json) override user settings
// (~/.tree/settings.json): their arrays are concatenated (project first), so
// project hooks run before user hooks for the same event.

export type HookEvent = "PreToolUse" | "PostToolUse" | "UserPromptSubmit" | "Stop";

export interface HookDefinition {
  matcher?: string;
  command: string;
}

export type HookConfig = Partial<Record<HookEvent, HookDefinition[]>>;

interface Settings {
  hooks?: HookConfig;
}

function readSettings(path: string): Settings | null {
  if (!existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, "utf-8")) as Settings;
  } catch (err) {
    console.error(
      `tree: ${path} is not valid JSON — ignoring. (${err instanceof Error ? err.message : String(err)})`,
    );
    return null;
  }
}

export function loadHooks(cwd: string): HookConfig {
  const user = readSettings(join(homedir(), ".tree", "settings.json"));
  const project = readSettings(join(cwd, ".tree", "settings.json"));

  const merged: HookConfig = {};
  const events: HookEvent[] = ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"];
  for (const ev of events) {
    const a = project?.hooks?.[ev] ?? [];
    const b = user?.hooks?.[ev] ?? [];
    if (a.length || b.length) merged[ev] = [...a, ...b];
  }
  return merged;
}
