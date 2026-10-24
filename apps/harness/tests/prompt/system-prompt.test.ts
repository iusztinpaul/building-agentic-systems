import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import {
  buildSystemPrompt,
  cwdSection,
  DEFAULT_PREAMBLE,
  loadPreamble,
  RULES,
  rulesSection,
  SECTION_ORDER,
  toolsSection,
} from "../../src/prompt/system-prompt.ts";
import { builtinRegistry } from "../../src/tools/builtins.ts";
import { useTmpDir } from "../helpers/tmp.ts";

const home = useTmpDir("prompt-home");

const ALL = {
  preamble: "P",
  tools: "T",
  rules: "R",
  project_context: "C",
  skills: "S",
  mcp_servers: "M",
  cwd: "W",
};

describe("buildSystemPrompt", () => {
  it("joins the sections in the fixed order, tagged, with the preamble untagged", () => {
    expect(buildSystemPrompt(ALL)).toBe(
      [
        "P",
        "<tools>\nT\n</tools>",
        "<rules>\nR\n</rules>",
        "<project_context>\nC\n</project_context>",
        "<skills>\nS\n</skills>",
        "<mcp_servers>\nM\n</mcp_servers>",
        "<cwd>\nW\n</cwd>",
      ].join("\n\n"),
    );
    expect(SECTION_ORDER).toEqual([
      "preamble",
      "tools",
      "rules",
      "project_context",
      "skills",
      "mcp_servers",
      "cwd",
    ]);
  });

  it("keeps the order whatever order the sections object is built in", () => {
    const reversed = Object.fromEntries(Object.entries(ALL).reverse()) as typeof ALL;

    expect(buildSystemPrompt(reversed)).toBe(buildSystemPrompt(ALL));
  });

  it("omits absent and empty optional sections", () => {
    expect(
      buildSystemPrompt({ preamble: "P", tools: "T", rules: "R", project_context: "", cwd: "W" }),
    ).toBe("P\n\n<tools>\nT\n</tools>\n\n<rules>\nR\n</rules>\n\n<cwd>\nW\n</cwd>");
  });

  it("keeps the base prompt (preamble + 4 tool lines + rules) at or under 900 tokens", () => {
    const tools = toolsSection(builtinRegistry({ tmpDir: "/tmp/x" }).declared());
    const base = buildSystemPrompt({
      preamble: DEFAULT_PREAMBLE,
      tools,
      rules: rulesSection(),
      cwd: "",
    });

    expect(tools.split("\n")).toHaveLength(4);
    expect(Math.ceil(base.length / 4)).toBeLessThanOrEqual(900);
  });

  it("has at most 8 rules", () => {
    expect(RULES.length).toBeLessThanOrEqual(8);
    expect(
      rulesSection()
        .split("\n")
        .every((l) => l.startsWith("- ")),
    ).toBe(true);
  });
});

describe("toolsSection", () => {
  it("lists `- name: promptSnippet` per tool", () => {
    const tool = {
      name: "grep",
      description: "d",
      promptSnippet: "search files",
      parameters: z.object({}),
      exposure: "direct" as const,
      isReadOnly: true,
      execute: async () => ({ content: "" }),
    };

    expect(toolsSection([tool])).toBe("- grep: search files");
  });
});

describe("cwdSection", () => {
  it("carries the absolute path and today's date in UTC", () => {
    // 23:30 on Oct 24 in UTC-5 is already Oct 25 in UTC.
    const now = new Date("2026-10-24T23:30:00-05:00");

    expect(cwdSection("/repo/sub", now)).toBe(
      "Working directory: /repo/sub\nToday's date (UTC): 2026-10-25",
    );
  });
});

describe("loadPreamble", () => {
  it("defaults to the built-in preamble", async () => {
    await expect(loadPreamble({ TREE_AGENT_HOME: home() })).resolves.toBe(DEFAULT_PREAMBLE);
  });

  it("is replaced by ~/.tree-agent/SYSTEM.md", async () => {
    writeFileSync(join(home(), "SYSTEM.md"), "You are a pirate.\n");

    await expect(loadPreamble({ TREE_AGENT_HOME: home() })).resolves.toBe("You are a pirate.");
  });
});
