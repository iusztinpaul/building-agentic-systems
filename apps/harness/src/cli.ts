#!/usr/bin/env node
// tree-agent entry. For now one streamed request, no tools: the
// prompt goes in, the model's text comes out on stdout. Task 204
// replaces this with the full mode matrix (spec §15).

import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import {
  checkDecisionModel,
  loadSettings,
  requireApiKey,
} from "./config/settings.ts";
import { OpenRouterProvider } from "./provider/openrouter.ts";
import type { Provider } from "./provider/types.ts";

const USAGE = 'usage: tree-agent [flags] "<prompt>"  (--model <id>)';
const SYSTEM = "You are tree-agent, a concise, helpful assistant.";

interface Out {
  write(text: string): unknown;
}

export interface CliDeps {
  env: NodeJS.ProcessEnv;
  stdout: Out;
  stderr: Out;
  createProvider?: (apiKey: string) => Provider;
}

// Returns the process exit code: 0 done, 1 any error.
export async function main(argv: string[], deps: CliDeps): Promise<number> {
  const { env, stdout, stderr } = deps;
  let args: ReturnType<typeof parseCli>;
  try {
    args = parseCli(argv);
  } catch (err) {
    stderr.write(`${(err as Error).message}\n${USAGE}\n`);
    return 1;
  }
  if (!args.prompt) {
    stderr.write(`${USAGE}\n`);
    return 1;
  }

  try {
    const apiKey = requireApiKey(env);
    const settings = await loadSettings({
      env,
      flags: { model: args.model },
    });
    const provider = (deps.createProvider ?? openRouter)(apiKey);
    await checkDecisionModel(provider, settings);

    const request = {
      model: settings.model,
      context: {
        system: SYSTEM,
        messages: [{ role: "user" as const, content: args.prompt }],
      },
      tools: [],
    };
    let text = "";
    const signal = new AbortController().signal;
    for await (const event of provider.stream(request, signal)) {
      if (event.type === "text_delta") {
        text += event.text;
        stdout.write(event.text);
      } else if (event.type === "error") {
        stderr.write(`${event.message}\n`);
        return 1;
      } else if (event.type === "done") {
        if (text && !text.endsWith("\n")) stdout.write("\n");
        return event.message.stopReason === "error" ? 1 : 0;
      }
    }
    stderr.write("stream ended without a result\n");
    return 1;
  } catch (err) {
    stderr.write(`${(err as Error).message}\n`);
    return 1;
  }
}

function parseCli(argv: string[]): { prompt: string; model?: string } {
  const { values, positionals } = parseArgs({
    args: argv,
    options: { model: { type: "string" } },
    allowPositionals: true,
  });
  return { prompt: positionals.join(" ").trim(), model: values.model };
}

function openRouter(apiKey: string): Provider {
  return new OpenRouterProvider({ apiKey });
}

// Run only when executed (node src/cli.ts, dist/cli.js, the bin
// symlink), not when a test imports main().
function isEntryPoint(): boolean {
  const script = process.argv[1];
  if (!script) return false;
  try {
    return realpathSync(script) === fileURLToPath(import.meta.url);
  } catch {
    return false;
  }
}

if (isEntryPoint()) {
  process.exitCode = await main(process.argv.slice(2), {
    env: process.env,
    stdout: process.stdout,
    stderr: process.stderr,
  });
}
