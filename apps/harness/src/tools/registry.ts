// Tools and their exposure (spec §8). The registry is the one place
// that knows which tools the model may see and call: `direct` tools
// always, `deferred` ones only after activate(), `codemode` ones only
// from a codemode script, `hidden` ones never.

import type { z } from "zod";
import { zodToJsonSchema } from "zod-to-json-schema";
import type { JsonSchema, ToolSchema } from "../provider/types.ts";
import type { ToolCall } from "../types.ts";

export type Exposure = "direct" | "deferred" | "codemode" | "hidden";

export interface ToolContext {
  cwd: string;
  signal: AbortSignal;
  origin: "model" | "codemode";
  depth: number; // 0 = the main loop; llm() sub-loops count up
}

// The model only ever sees `content`; `structured` is the parsed
// value a codemode script receives.
export interface ToolResult {
  content: string;
  isError?: boolean;
  structured?: unknown;
}

export interface ToolDefinition<S extends z.AnyZodObject = z.AnyZodObject> {
  name: string; // ^[a-z][a-z0-9_]*$
  description: string; // the provider tools array
  promptSnippet: string; // one line in the `tools` prompt section
  parameters: S;
  exposure: Exposure;
  isReadOnly: boolean; // permission gate step 1 (task 206)
  // A method, so a ToolDefinition<{path}> fits a registry of any.
  execute(params: z.infer<S>, ctx: ToolContext): Promise<ToolResult>;
}

export class ToolRegistry {
  readonly #tools = new Map<string, ToolDefinition>();
  readonly #activated = new Set<string>();

  register(tool: ToolDefinition): void {
    if (this.#tools.has(tool.name)) {
      throw new Error(`tool "${tool.name}" is already registered`);
    }
    this.#tools.set(tool.name, tool);
  }

  get(name: string): ToolDefinition | undefined {
    return this.#tools.get(name);
  }

  list(exposure?: Exposure): ToolDefinition[] {
    const all = [...this.#tools.values()];
    return exposure ? all.filter((t) => t.exposure === exposure) : all;
  }

  // deferred → declared for the rest of the session; other names
  // are ignored (codemode and hidden tools never become declared).
  activate(names: string[]): void {
    for (const name of names) {
      if (this.get(name)?.exposure === "deferred") {
        this.#activated.add(name);
      }
    }
  }

  activated(): string[] {
    return [...this.#activated];
  }

  // What the model sees: direct ∪ activated, in registration order.
  declared(): ToolDefinition[] {
    return this.list().filter(
      (t) => t.exposure === "direct" || this.#activated.has(t.name),
    );
  }

  schemasForRequest(): ToolSchema[] {
    return this.declared().map((tool) => ({
      name: tool.name,
      description: tool.description,
      parameters: toJsonSchema(tool.parameters),
    }));
  }

  // Never throws: every failure is an isError result for the caller.
  async execute(call: ToolCall, ctx: ToolContext): Promise<ToolResult> {
    const tool = this.get(call.name);
    if (!tool || !this.#callable(tool, ctx.origin)) {
      return { content: `Unknown tool: ${call.name}`, isError: true };
    }
    const parsed = tool.parameters.safeParse(call.arguments);
    if (!parsed.success) {
      const issues = parsed.error.issues.map(
        (i) => `  - ${i.path.join(".") || "(root)"}: ${i.message}`,
      );
      return {
        content: [`Invalid arguments for ${tool.name}:`, ...issues].join(
          "\n",
        ),
        isError: true,
      };
    }
    try {
      return await tool.execute(parsed.data, ctx);
    } catch (err) {
      return { content: (err as Error).message, isError: true };
    }
  }

  #callable(tool: ToolDefinition, origin: ToolContext["origin"]) {
    if (tool.exposure === "hidden") return false;
    if (origin === "codemode") return true;
    return tool.exposure === "direct" || this.#activated.has(tool.name);
  }
}

// The default JSON Schema target keeps `.optional()` params optional
// (the openAi target used by complete<T> would make them required).
function toJsonSchema(schema: z.AnyZodObject): JsonSchema {
  const json = zodToJsonSchema(schema, {
    $refStrategy: "none",
  }) as JsonSchema;
  delete json.$schema;
  return json;
}
