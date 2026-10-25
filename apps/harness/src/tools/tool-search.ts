// tool_search (spec §8.1): BM25 over the `deferred` tools' name and
// description; the top matches are activated, so they are in the
// `tools` array from the next request on. The index is built from
// the registry on every call (a few dozen short documents), so it is
// always the registry's current deferred set.

import { z } from "zod";
import type { ToolDefinition, ToolRegistry } from "./registry.ts";

export const TOOL_SEARCH = "tool_search";

// Lists no tool names, so it never grows with the servers.
export const TOOL_SEARCH_DESCRIPTION =
  "Search tools that are not yet available by describing what you " +
  "need. Matching tools become callable on your next turn.";

export const DEFAULT_LIMIT = 5;
const K1 = 1.2;
const B = 0.75;
// Name tokens count this many times: a tool named for the query
// ranks above one that only mentions it.
const NAME_WEIGHT = 2;

const Params = z.object({
  query: z.string().min(1).describe("What the tool should do"),
  limit: z
    .number()
    .int()
    .min(1)
    .max(20)
    .optional()
    .describe(`How many tools to return (default ${DEFAULT_LIMIT})`),
});

export function toolSearchTool(
  registry: ToolRegistry,
): ToolDefinition<typeof Params> {
  return {
    name: TOOL_SEARCH,
    description: TOOL_SEARCH_DESCRIPTION,
    promptSnippet: "find more tools by describing what you need",
    parameters: Params,
    exposure: "direct",
    isReadOnly: true,
    async execute({ query, limit = DEFAULT_LIMIT }) {
      const hits = rankTools(registry.list("deferred"), query, limit);
      if (hits.length === 0) {
        return { content: `No tools match "${query}".`, structured: [] };
      }
      registry.activate(hits.map((t) => t.name));
      // promptSnippet: an MCP tool's first description line.
      const found = hits.map((t) => ({
        name: t.name,
        description: t.promptSnippet,
      }));
      return {
        content: found
          .map((t) => `${t.name} — ${t.description}`)
          .join("\n"),
        structured: found,
      };
    },
  };
}

// Registered only when there is something to find.
export function registerToolSearch(registry: ToolRegistry): boolean {
  if (registry.list("deferred").length === 0) return false;
  registry.register(toolSearchTool(registry));
  return true;
}

// BM25 (k1 1.2, b 0.75), best first, ties in the given order; tools
// sharing no term with the query are left out.
export function rankTools(
  tools: readonly ToolDefinition[],
  query: string,
  limit: number,
): ToolDefinition[] {
  const docs = tools.map((t) => [
    ...Array<string[]>(NAME_WEIGHT).fill(tokenize(t.name)).flat(),
    ...tokenize(t.description),
  ]);
  const avgLength = docs.reduce((n, d) => n + d.length, 0) / docs.length;
  const df = new Map<string, number>();
  for (const doc of docs) {
    for (const term of new Set(doc)) df.set(term, (df.get(term) ?? 0) + 1);
  }
  const terms = [...new Set(tokenize(query))];
  return docs
    .map((doc, index) => {
      let score = 0;
      for (const term of terms) {
        const tf = doc.filter((t) => t === term).length;
        if (tf === 0) continue;
        const n = df.get(term) ?? 0;
        const idf = Math.log(1 + (docs.length - n + 0.5) / (n + 0.5));
        const norm = 1 - B + (B * doc.length) / (avgLength || 1);
        score += (idf * tf * (K1 + 1)) / (tf + K1 * norm);
      }
      return { index, score };
    })
    .filter((s) => s.score > 0)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .slice(0, limit)
    .flatMap((s) => tools[s.index] ?? []);
}

// Lowercased runs of [a-z0-9]; `_` and `-` split like spaces.
export function tokenize(text: string): string[] {
  return text
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
}
