import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import {
  buildSystemPrompt,
  CHARS_PER_TOKEN,
  capDescription,
  cwdSection,
  DEFAULT_PREAMBLE,
  loadPreamble,
  MORE_TOOLS,
  mcpServersSection,
  RULES,
  rulesSection,
  SECTION_ORDER,
  SKILLS_INTRO,
  skillsSection,
  toolsSection,
} from "../../src/prompt/system-prompt.ts";
import type { Skill } from "../../src/skills/skills.ts";
import { builtinRegistry } from "../../src/tools/builtins.ts";
import { toolSearchTool } from "../../src/tools/tool-search.ts";
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

describe("toolsSection with tool_search", () => {
  it("adds the more-tools sentence when tool_search is declared, not otherwise", () => {
    const registry = builtinRegistry({ tmpDir: "/tmp/x" });
    const without = toolsSection(registry.declared());
    registry.register(toolSearchTool(registry));

    const withSearch = toolsSection(registry.declared());

    expect(without).not.toContain(MORE_TOOLS);
    expect(MORE_TOOLS).toBe("More tools are available; call `tool_search` to find them.");
    expect(withSearch.split("\n").slice(-2)).toEqual([
      "- tool_search: find more tools by describing what you need",
      MORE_TOOLS,
    ]);
  });
});

describe("mcpServersSection", () => {
  const server = (name: string, direct: number, deferred: number, codemode: number) => ({
    name,
    client: { connected: true },
    tools: direct + deferred + codemode,
    direct,
    deferred,
    codemode,
  });

  it("writes `- <name>: <N> tools; direct a, deferred b, codemode c` per server, keeping the config key", () => {
    expect(mcpServersSection([server("tree-memory-local", 1, 2, 8), server("gh", 0, 0, 30)])).toBe(
      [
        "- tree-memory-local: 11 tools; direct 1, deferred 2, codemode 8",
        "- gh: 30 tools; direct 0, deferred 0, codemode 30",
      ].join("\n"),
    );
  });

  it("is empty (the section is omitted) without servers", () => {
    expect(mcpServersSection([])).toBe("");
    expect(buildSystemPrompt({ ...ALL, mcp_servers: mcpServersSection([]) })).not.toContain(
      "<mcp_servers>",
    );
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

describe("skillsSection", () => {
  const skill = (name: string, o: Partial<Skill> = {}): Skill => ({
    name,
    description: `does ${name}`,
    location: `/abs/${name}/SKILL.md`,
    body: "",
    disableModelInvocation: false,
    ...o,
  });

  it("matches the spec §9 block: wrapper text, available_skills, one <skill> line each", () => {
    const spec = readFileSync(
      new URL("../../../../docs/notes/tree-agent-harness-spec.md", import.meta.url),
      "utf8",
    );
    const block = /## 9\. Skills[\s\S]*?```\n([\s\S]*?)\n```/.exec(spec)?.[1] ?? "";
    const [one, two, open, line, close] = block.split("\n");

    expect(SKILLS_INTRO).toBe(`${one}\n${two}`);
    expect(skillsSection([skill("a")], 1000).text).toBe(
      [
        SKILLS_INTRO,
        open,
        line
          ?.replace("<name>…</name>", "<name>a</name>")
          .replace("<description>…</description>", "<description>does a</description>")
          .replace("/abs/SKILL.md", "/abs/a/SKILL.md"),
        close,
      ].join("\n"),
    );
    expect(open).toBe("<available_skills>");
  });

  it("lists a 400-char description as its first 249 chars + …", () => {
    const long = "abcdefghij".repeat(40);

    const text = skillsSection([skill("a", { description: long })], 1000).text;

    expect(text).toContain(`<description>${long.slice(0, 249)}…</description>`);
    expect(capDescription(long)).toHaveLength(250);
    expect(capDescription("x".repeat(250))).toBe("x".repeat(250));
  });

  it("caps by code points, so an emoji at the cut is never split", () => {
    const capped = capDescription(`${"x".repeat(248)}😀😀😀`);

    expect(capped).toBe(`${"x".repeat(248)}😀…`);
  });

  it("puts a multi-line description on one line", () => {
    expect(capDescription("  one\n\n  two\tthree ")).toBe("one two three");
  });

  it("stops at the budget (200 tokens ≈ 800 chars) and returns every name after it as dropped", () => {
    const skills = Array.from({ length: 10 }, (_, i) => skill(`skill-${i}`));

    const { text, dropped } = skillsSection(skills, 200);

    expect(text.length).toBeLessThanOrEqual(200 * CHARS_PER_TOKEN);
    const listed = [...text.matchAll(/<name>([^<]+)<\/name>/g)].map((m) => m[1]);
    expect(listed.length).toBeGreaterThan(0);
    expect([...listed, ...dropped]).toEqual(skills.map((s) => s.name));
    const withOneMore = skillsSection(skills.slice(0, listed.length + 1), 10_000).text;
    expect(withOneMore.length).toBeGreaterThan(200 * CHARS_PER_TOKEN);
  });

  it("stops at the first skill that does not fit, even if a later, shorter one would", () => {
    const skills = [skill("a"), skill("long", { description: "x".repeat(240) }), skill("b")];
    const fitsAB = skillsSection([skill("a"), skill("b")], 10_000).text.length;

    const { text, dropped } = skillsSection(skills, Math.ceil(fitsAB / CHARS_PER_TOKEN));

    expect(text).toContain("<name>a</name>");
    expect(dropped).toEqual(["long", "b"]);
  });

  it("keeps a disable-model-invocation skill out of the listing and out of dropped", () => {
    const { text, dropped } = skillsSection(
      [skill("shown"), skill("hidden", { disableModelInvocation: true })],
      0,
    );

    expect(text).toBe("");
    expect(dropped).toEqual(["shown"]);
    expect(skillsSection([skill("hidden", { disableModelInvocation: true })], 1000)).toEqual({
      text: "",
      dropped: [],
    });
  });

  it("escapes &, < and > in the listing", () => {
    const text = skillsSection([skill("a", { description: "a <b> & c" })], 1000).text;

    expect(text).toContain("<description>a &lt;b&gt; &amp; c</description>");
  });
});
