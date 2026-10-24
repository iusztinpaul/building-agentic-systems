import { chmodSync, mkdirSync, symlinkSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { skillsSection } from "../../src/prompt/system-prompt.ts";
import {
  type DiscoverOptions,
  discoverSkills,
  parseFrontmatter,
  parseSkill,
  parseSkillCommand,
  type Skill,
  skillMessage,
  skillRoots,
} from "../../src/skills/skills.ts";
import { useTmpDir } from "../helpers/tmp.ts";

const root = useTmpDir("skills");

const REPO = fileURLToPath(new URL("../../../../", import.meta.url));

// A home (HOME and TREE_AGENT_HOME) and a git repo with cwd two levels down.
function layout() {
  const home = join(root(), "home");
  const repo = join(root(), "repo");
  const cwd = join(repo, "apps", "web");
  mkdirSync(join(repo, ".git"), { recursive: true });
  mkdirSync(cwd, { recursive: true });
  mkdirSync(home, { recursive: true });
  const env = { HOME: home, TREE_AGENT_HOME: join(home, ".tree-agent") };
  const roots = {
    treeAgentUser: join(home, ".tree-agent", "skills"),
    agentsUser: join(home, ".agents", "skills"),
    gitRoot: join(repo, ".agents", "skills"),
    mid: join(repo, "apps", ".agents", "skills"),
    cwdAgents: join(cwd, ".agents", "skills"),
    project: join(cwd, ".tree-agent", "skills"),
  };
  const opts = (trusted = true): DiscoverOptions => ({ cwd, trusted, env });
  return { home, repo, cwd, env, roots, opts };
}

function skill(
  dir: string,
  name: string,
  description = `does ${name}`,
  extra = "",
  body = `# ${name}\nSteps.`,
): string {
  mkdirSync(join(dir, name), { recursive: true });
  const file = join(dir, name, "SKILL.md");
  writeFileSync(file, `---\nname: ${name}\ndescription: ${description}\n${extra}---\n${body}\n`);
  return file;
}

const byName = (skills: Skill[]) => Object.fromEntries(skills.map((s) => [s.name, s]));

describe("skillRoots", () => {
  it("searches the four roots in the spec's order, the git walk from the root down to cwd", async () => {
    const t = layout();

    expect(await skillRoots(t.opts())).toEqual([
      t.roots.treeAgentUser,
      t.roots.agentsUser,
      t.roots.gitRoot,
      t.roots.mid,
      t.roots.cwdAgents,
      t.roots.project,
    ]);
  });

  it("reads only the two user-level roots in an untrusted cwd", async () => {
    const t = layout();

    expect(await skillRoots(t.opts(false))).toEqual([t.roots.treeAgentUser, t.roots.agentsUser]);
  });

  it("walks no further than cwd without a git root", async () => {
    const home = join(root(), "home");
    const cwd = join(root(), "plain", "dir");
    mkdirSync(cwd, { recursive: true });

    const roots = await skillRoots({ cwd, trusted: true, env: { HOME: home } });

    expect(roots.slice(2)).toEqual([
      join(cwd, ".agents", "skills"),
      join(cwd, ".tree-agent", "skills"),
    ]);
  });

  it("reads one directory once when two roots are the same place", async () => {
    // HOME is the git root: ~/.agents/skills is also the walk's first root.
    const t = layout();
    mkdirSync(t.roots.gitRoot, { recursive: true });

    const roots = await skillRoots({
      cwd: t.cwd,
      trusted: true,
      env: { HOME: t.repo, TREE_AGENT_HOME: t.env.TREE_AGENT_HOME },
    });

    expect(roots.filter((r) => r === t.roots.gitRoot)).toHaveLength(1);
  });
});

describe("discoverSkills", () => {
  it("lists a skill in ~/.tree-agent/skills and .tree-agent/skills once, with the project path", async () => {
    const t = layout();
    skill(t.roots.treeAgentUser, "x", "user copy");
    const project = skill(t.roots.project, "x", "project copy");

    const found = await discoverSkills(t.opts());

    expect(found.skills).toHaveLength(1);
    expect(found.skills[0]).toMatchObject({ location: project, description: "project copy" });
    expect(found.diagnostics).toEqual([]);
  });

  it("lets each later root win: user → ~/.agents → git root → … → cwd → .tree-agent", async () => {
    const t = layout();
    const order = Object.entries(t.roots);
    for (const [label, dir] of order) skill(dir, "same", label);
    for (const [label, dir] of order) skill(dir, `only-${label.toLowerCase()}`);

    const found = await discoverSkills(t.opts());

    expect(byName(found.skills).same?.description).toBe("project");
    expect(found.skills.map((s) => s.name)).toEqual([
      "only-agentsuser",
      "only-cwdagents",
      "only-gitroot",
      "only-mid",
      "only-project",
      "only-treeagentuser",
      "same",
    ]);
  });

  it("orders the git walk root → cwd, so the nearest .agents/skills wins", async () => {
    const t = layout();
    skill(t.roots.gitRoot, "near", "git root");
    const nearest = skill(t.roots.cwdAgents, "near", "cwd");

    const found = await discoverSkills(t.opts());

    expect(byName(found.skills).near?.location).toBe(nearest);
  });

  it("follows a symlinked skill dir and a symlinked root", async () => {
    const t = layout();
    const elsewhere = join(root(), "elsewhere");
    skill(elsewhere, "linked");
    mkdirSync(t.roots.agentsUser, { recursive: true });
    symlinkSync(join(elsewhere, "linked"), join(t.roots.agentsUser, "linked"));
    const shared = join(root(), "shared-skills");
    skill(shared, "via-root");
    mkdirSync(dirname(t.roots.treeAgentUser), { recursive: true });
    symlinkSync(shared, t.roots.treeAgentUser);

    const found = await discoverSkills(t.opts(false));

    expect(byName(found.skills).linked?.location).toBe(
      join(t.roots.agentsUser, "linked", "SKILL.md"),
    );
    expect(byName(found.skills)["via-root"]).toBeDefined();
    expect(found.diagnostics).toEqual([]);
  });

  it("reads no project root in an untrusted cwd", async () => {
    const t = layout();
    skill(t.roots.treeAgentUser, "a");
    skill(t.roots.agentsUser, "b");
    skill(t.roots.gitRoot, "c");
    skill(t.roots.cwdAgents, "d");
    skill(t.roots.project, "e");

    const found = await discoverSkills(t.opts(false));

    expect(found.skills.map((s) => s.name)).toEqual(["a", "b"]);
  });

  it("skips non-skill entries quietly: a file, a dir without SKILL.md, a dot dir, a broken link", async () => {
    const t = layout();
    skill(t.roots.agentsUser, "real");
    writeFileSync(join(t.roots.agentsUser, "README.md"), "not a skill");
    mkdirSync(join(t.roots.agentsUser, "empty"));
    skill(t.roots.agentsUser, ".hidden");
    symlinkSync(join(root(), "missing"), join(t.roots.agentsUser, "dangling"));

    const found = await discoverSkills(t.opts());

    expect(found.skills.map((s) => s.name)).toEqual(["real"]);
    expect(found.diagnostics).toEqual([]);
  });

  it("keeps discovering after a bad skill and names the file in its diagnostic", async () => {
    const t = layout();
    skill(t.roots.agentsUser, "alpha");
    const broken = join(t.roots.agentsUser, "broken", "SKILL.md");
    mkdirSync(dirname(broken));
    writeFileSync(broken, "---\nname: broken\nthis line is not yaml\n---\nbody\n");
    const nameless = join(t.roots.agentsUser, "nameless", "SKILL.md");
    mkdirSync(dirname(nameless));
    writeFileSync(nameless, "---\ndescription: no name here\n---\nbody\n");
    skill(t.roots.agentsUser, "omega");

    const found = await discoverSkills(t.opts());

    expect(found.skills.map((s) => s.name)).toEqual(["alpha", "omega"]);
    expect(found.diagnostics).toEqual([
      `${broken}: bad frontmatter: line 3: expected "key: value"`,
      `${nameless}: missing name`,
    ]);
  });

  it("lists a skill whose ignored key holds an unindented block list (PyYAML's dump)", async () => {
    const t = layout();
    const file = join(t.roots.agentsUser, "usr", "SKILL.md");
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, "---\nname: usr\ndescription: d\nallowed-tools:\n- Bash\n---\nBODY\n");

    const found = await discoverSkills(t.opts(false));

    expect(found.diagnostics).toEqual([]);
    expect(found.skills).toEqual([
      {
        name: "usr",
        description: "d",
        location: file,
        body: "BODY",
        disableModelInvocation: false,
      },
    ]);
  });

  it("reports an unreadable SKILL.md and goes on", async () => {
    const t = layout();
    const locked = skill(t.roots.agentsUser, "locked");
    skill(t.roots.agentsUser, "open");
    chmodSync(locked, 0o000);

    const found = await discoverSkills(t.opts());
    chmodSync(locked, 0o644);

    expect(found.skills.map((s) => s.name)).toEqual(["open"]);
    expect(found.diagnostics).toHaveLength(1);
    expect(found.diagnostics[0]).toMatch(new RegExp(`^${locked}: EACCES`));
  });

  it("reports two dirs with the same name in one root; the later dir wins", async () => {
    const t = layout();
    const first = join(t.roots.agentsUser, "a-dir", "SKILL.md");
    const second = join(t.roots.agentsUser, "b-dir", "SKILL.md");
    for (const [file, text] of [
      [first, "first"],
      [second, "second"],
    ] as const) {
      mkdirSync(dirname(file), { recursive: true });
      writeFileSync(file, `---\nname: dup\ndescription: ${text}\n---\n`);
    }

    const found = await discoverSkills(t.opts());

    expect(found.skills).toMatchObject([{ name: "dup", description: "second" }]);
    expect(found.diagnostics).toEqual([
      `${second}: duplicate skill name "dup" (also ${first}); this one wins`,
    ]);
  });

  it("does not call one skill reached through two roots a duplicate", async () => {
    const t = layout();
    skill(t.roots.gitRoot, "shared");
    mkdirSync(t.roots.agentsUser, { recursive: true });
    symlinkSync(join(t.roots.gitRoot, "shared"), join(t.roots.agentsUser, "shared"));

    const found = await discoverSkills(t.opts());

    expect(found.skills).toHaveLength(1);
    expect(found.diagnostics).toEqual([]);
  });

  it("finds this repo's 13 skills, 5 of them hidden from the model (trusted)", async () => {
    const empty = join(root(), "nobody");
    const env = { HOME: empty, TREE_AGENT_HOME: join(empty, ".tree-agent") };

    const found = await discoverSkills({ cwd: REPO, trusted: true, env });

    expect(found.diagnostics).toEqual([]);
    expect(found.skills).toHaveLength(13);
    const hidden = found.skills.filter((s) => s.disableModelInvocation).map((s) => s.name);
    expect(hidden).toEqual(["bright-data-mcp", "gws-docs", "scrape", "search", "tree-memory"]);
    const section = skillsSection(found.skills, 100_000).text;
    expect(section.match(/<skill>/g)).toHaveLength(8);
    expect(section).toContain(
      `<name>tech-docs</name><description>Look up authoritative documentation`,
    );
    expect(section).toContain(`<location>${join(REPO, ".agents/skills/tech-docs/SKILL.md")}`);
    expect(section).not.toContain("tree-memory");
    expect(byName(found.skills)["tree-memory"]?.body).toMatch(/^# Tree Memory\n/);
  });
});

describe("parseFrontmatter", () => {
  const fm = (lines: string[]) => parseFrontmatter(`---\n${lines.join("\n")}\n---\nthe body\n`);

  it("reads plain, quoted and commented values and returns the trimmed body", () => {
    const parsed = fm([
      "# a comment",
      "name: plain-name   # trailing comment",
      `description: "Say \\"hi\\": it's a test"`,
      "disable-model-invocation: true",
    ]);

    expect(Object.fromEntries(parsed.fields)).toEqual({
      name: "plain-name",
      description: `Say "hi": it's a test`,
      "disable-model-invocation": "true",
    });
    expect(parsed.body).toBe("the body");
  });

  it("reads 'single-quoted' values with '' escapes", () => {
    expect(fm(["description: 'it''s: fine'"]).fields.get("description")).toBe("it's: fine");
  });

  it("keeps | line breaks and folds > lines; the next key ends the block", () => {
    const literal = fm(["description: |", "  one", "", "  two", "name: n"]);
    const folded = fm(["description: >-", "  one", "  two", "name: n"]);

    expect(literal.fields.get("description")).toBe("one\n\ntwo");
    expect(folded.fields.get("description")).toBe("one two");
    expect(folded.fields.get("name")).toBe("n");
  });

  it("joins a plain value continued on indented lines", () => {
    expect(fm(["description: one", "  two"]).fields.get("description")).toBe("one two");
  });

  it("ignores other keys, nested maps and all", () => {
    const parsed = fm([
      "name: n",
      "metadata:",
      "  name: not-the-name",
      "  version: '1'",
      "allowed-tools: mcp__x__*",
      "description: d",
    ]);

    expect(Object.fromEntries(parsed.fields)).toEqual({ name: "n", description: "d" });
  });

  it("skips unindented block lists, also with nested lines, under ignored keys", () => {
    const parsed = fm([
      "name: n",
      "allowed-tools:",
      "- Bash",
      "- Read",
      "hooks:",
      "- matcher: Bash",
      "  command: echo hi",
      "-",
      "description: d",
    ]);

    expect(Object.fromEntries(parsed.fields)).toEqual({ name: "n", description: "d" });
  });

  it("accepts dotted, spaced and colon keys as ignored keys", () => {
    const parsed = fm(["x.y: 1", "a key: v", "a:b: c", "name: n", "description: see http://x.y/z"]);

    expect(Object.fromEntries(parsed.fields)).toEqual({
      name: "n",
      description: "see http://x.y/z",
    });
  });

  it("maps quoted keys to the real key", () => {
    const parsed = fm([
      `"description": d`,
      `'name': n`,
      `"disable-model-invocation" : true`,
      `"odd\\q": x`,
    ]);

    expect(Object.fromEntries(parsed.fields)).toEqual({
      description: "d",
      name: "n",
      "disable-model-invocation": "true",
    });
  });

  it("tolerates CRLF line endings and a BOM", () => {
    const parsed = parseFrontmatter("﻿---\r\nname: n\r\ndescription: d\r\n---\r\nbody\r\n");

    expect(Object.fromEntries(parsed.fields)).toEqual({ name: "n", description: "d" });
    expect(parsed.body).toBe("body");
  });

  it.each([
    ["no opening ---", "name: n\n", "the file must start with a --- line"],
    ["no closing ---", "---\nname: n\n", "no closing --- line"],
    ["an indented first key", "---\n  name: n\n---\n", 'line 2: expected "key: value"'],
    ["an unclosed double quote", '---\nname: "n\n---\n', 'line 2: bad "double-quoted" value'],
    ["an unclosed single quote", "---\nname: 'n\n---\n", "line 2: bad 'single-quoted' value"],
    ["a line that is no key", "---\nname: n\njust words\n---\n", 'line 3: expected "key: value"'],
    ["an unclosed quoted key", '---\n"name: n\n---\n', 'line 2: expected "key: value"'],
  ])("throws on %s", (_label, text, message) => {
    expect(() => parseFrontmatter(text)).toThrow(message);
  });
});

describe("parseSkill", () => {
  const text = (lines: string[]) => `---\n${lines.join("\n")}\n---\nBody.\n`;

  it("builds a Skill; disable-model-invocation is true only for true", () => {
    expect(
      parseSkill(text(["name: a", "description: d", "disable-model-invocation: true"]), "/s"),
    ).toEqual({
      name: "a",
      description: "d",
      location: "/s",
      body: "Body.",
      disableModelInvocation: true,
    });
    for (const value of ["false", "yes"]) {
      const parsed = parseSkill(
        text(["name: a", "description: d", `disable-model-invocation: ${value}`]),
        "/s",
      );
      expect(parsed).toMatchObject({ disableModelInvocation: false });
    }
  });

  it.each([
    [["description: d"], "missing name"],
    [["name: a"], "missing description"],
    [["name: a", 'description: ""'], "missing description"],
    [["name: Bad_Name", "description: d"], 'bad name "Bad_Name"'],
    [["name: a--b", "description: d"], 'bad name "a--b"'],
    [[`name: ${"a".repeat(65)}`, "description: d"], "at most 64 chars"],
  ])("rejects %j: %s", (lines, message) => {
    expect(parseSkill(text(lines), "/s")).toContain(message);
  });
});

describe("/skill: messages", () => {
  const s: Skill = {
    name: "x",
    description: "d",
    location: "/x/SKILL.md",
    body: "# X\nDo it.",
    disableModelInvocation: false,
  };

  it("is the body, a blank line, then the args", () => {
    expect(skillMessage(s, "arg1 arg2")).toBe("# X\nDo it.\n\narg1 arg2");
    expect(skillMessage(s, "  ")).toBe("# X\nDo it.");
  });

  it("is the args alone for an empty body, and empty with no args either", () => {
    expect(skillMessage({ ...s, body: "" }, "q")).toBe("q");
    expect(skillMessage({ ...s, body: "" }, "")).toBe("");
  });

  it.each([
    ["/skill:x", { name: "x", args: "" }],
    ["/skill:run-pipelines-e2e arg1 arg2", { name: "run-pipelines-e2e", args: "arg1 arg2" }],
    ["  /skill:x  line one\nline two ", { name: "x", args: "line one\nline two" }],
    ["/skill:", { name: "", args: "" }],
    ["/skill: x", { name: "", args: "x" }],
    ["/Skill:Tree-Memory hi", { name: "Tree-Memory", args: "hi" }],
    ["please /skill:x", undefined],
    ["/skills", undefined],
  ])("parses %j", (text, want) => {
    expect(parseSkillCommand(text)).toEqual(want);
  });
});
