// Settings (spec §16): a zod schema whose defaults are the defaults.
// Layers merge defaults → ~/.tree-agent/settings.json →
// <cwd>/.tree-agent/settings.json (trusted cwd only) → CLI flags;
// later layers replace earlier ones key by key. Every object is
// strict, so a typo in settings.json fails loudly instead of being
// ignored.

import { readFile } from "node:fs/promises";
import { z } from "zod";
import type { Provider } from "../provider/types.ts";
import { projectPaths, userPaths } from "./paths.ts";

export const Settings = z
  .object({
    model: z.string().default("anthropic/claude-sonnet-5.5"),
    decisionModel: z.string().default("anthropic/claude-haiku-5.5"),
    // TUI only; headless is auto unless --permission-mode.
    permissions: z
      .object({ mode: z.enum(["ask", "auto"]).default("ask") })
      .strict()
      .default({}),
    compaction: z
      .object({
        triggerRatio: z.number().min(0.3).max(0.95).default(0.8),
        keepRecentTokens: z.number().default(20_000),
      })
      .strict()
      .default({}),
    codemode: z
      .object({
        // Sandbox limits (spec §10.4); 0 would hang or never run.
        inlineBudgetTokens: z.number().int().nonnegative().default(3_000),
        timeoutMs: z.number().int().positive().default(60_000),
        memoryMb: z.number().int().positive().default(256),
        maxToolCalls: z.number().int().positive().default(50),
        outputMaxBytes: z.number().int().positive().default(20_000),
        concurrency: z.number().int().positive().default(4),
        // llm() in a script (spec §10.6); model unset: decisionModel.
        llm: z
          .object({
            model: z.string().optional(),
            depth: z.union([z.literal(1), z.literal(2)]).default(1),
            maxCalls: z.number().int().positive().default(16),
            maxInputChars: z.number().int().positive().default(200_000),
            maxTotalTokens: z.number().int().positive().default(400_000),
            maxSubTurns: z.number().int().positive().default(8),
          })
          .strict()
          .default({}),
      })
      .strict()
      .default({}),
    contextFiles: z
      .object({ maxChars: z.number().default(40_000) })
      .strict()
      .default({}),
    skills: z
      .object({ listingBudgetRatio: z.number().default(0.01) })
      .strict()
      .default({}),
  })
  .strict();

export type Settings = z.infer<typeof Settings>;
// One layer as written in a settings.json or built from flags.
export type SettingsLayer = z.input<typeof Settings>;

export class SettingsError extends Error {
  override name = "SettingsError";
}

export interface LoadSettingsOptions {
  env?: NodeJS.ProcessEnv;
  flags?: SettingsLayer;
  // A trusted cwd: its .tree-agent/settings.json is a layer too.
  projectDir?: string;
}

export async function loadSettings(
  options: LoadSettingsOptions = {},
): Promise<Settings> {
  const user = await readLayer(userPaths(options.env).settings);
  const project = options.projectDir
    ? await readLayer(projectPaths(options.projectDir).settings)
    : {};
  const merged = merge(merge(user, project), options.flags ?? {});
  return validate(merged, "settings");
}

export function requireApiKey(
  env: NodeJS.ProcessEnv = process.env,
): string {
  const key = env.OPENROUTER_API_KEY?.trim();
  if (!key) {
    throw new SettingsError(
      "OPENROUTER_API_KEY is not set (add it to .env)",
    );
  }
  return key;
}

// Fail fast: the decision model must do structured outputs.
export async function checkDecisionModel(
  provider: Provider,
  settings: Settings,
): Promise<void> {
  const info = await provider.modelInfo(settings.decisionModel);
  if (!info.supported.has("structured_outputs")) {
    throw new SettingsError(
      `decisionModel ${info.id} does not support structured_outputs`,
    );
  }
}

async function readLayer(file: string): Promise<SettingsLayer> {
  let text: string;
  try {
    text = await readFile(file, "utf8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw err;
  }
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (err) {
    throw new SettingsError(
      `${file}: invalid JSON: ${(err as Error).message}`,
    );
  }
  validate(raw, file); // name the file that is wrong
  return raw as SettingsLayer;
}

function validate(raw: unknown, source: string): Settings {
  const result = Settings.safeParse(raw);
  if (result.success) return result.data;
  const issues = result.error.issues.map(
    (i) => `  - ${i.path.join(".") || "(root)"}: ${i.message}`,
  );
  throw new SettingsError(
    [`${source}: invalid settings`, ...issues].join("\n"),
  );
}

function merge(
  base: Record<string, unknown>,
  over: Record<string, unknown>,
): Record<string, unknown> {
  const out = { ...base };
  for (const [key, value] of Object.entries(over)) {
    if (value === undefined) continue; // an unset flag keeps the file
    const prev = out[key];
    out[key] =
      isObject(prev) && isObject(value) ? merge(prev, value) : value;
  }
  return out;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" && value !== null && !Array.isArray(value)
  );
}
