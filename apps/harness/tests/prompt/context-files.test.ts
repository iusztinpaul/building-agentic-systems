import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { formatContextFiles, loadContextFiles } from "../../src/prompt/context-files.ts";
import { useTmpDir } from "../helpers/tmp.ts";

const root = useTmpDir("context");

// <root>/home is the fake ~/.tree-agent; <root>/repo/sub is the cwd.
function layout(files: Record<string, string>) {
  for (const [rel, content] of Object.entries(files)) {
    const path = join(root(), rel);
    mkdirSync(join(path, ".."), { recursive: true });
    writeFileSync(path, content);
  }
  mkdirSync(join(root(), "repo", "sub"), { recursive: true });
  mkdirSync(join(root(), "home"), { recursive: true });
  return { cwd: join(root(), "repo", "sub"), env: { TREE_AGENT_HOME: join(root(), "home") } };
}

// Only files under the scratch root (a real ancestor of tmpdir could hold one).
async function load(o: { cwd: string; env: NodeJS.ProcessEnv }, trusted = true, maxChars = 40_000) {
  const files = await loadContextFiles({ ...o, trusted, maxChars });
  return files.filter((f) => f.path.startsWith(root()));
}

describe("loadContextFiles", () => {
  it("orders ~/.tree-agent/AGENTS.md first, then ancestors root → cwd", async () => {
    const o = layout({
      "home/AGENTS.md": "user rules",
      "AGENTS.md": "root rules",
      "repo/AGENTS.md": "repo rules",
      "repo/sub/CLAUDE.md": "sub rules",
    });

    const files = await load(o);

    expect(files).toEqual([
      { path: join(root(), "home", "AGENTS.md"), content: "user rules" },
      { path: join(root(), "AGENTS.md"), content: "root rules" },
      { path: join(root(), "repo", "AGENTS.md"), content: "repo rules" },
      { path: join(root(), "repo", "sub", "CLAUDE.md"), content: "sub rules" },
    ]);
  });

  it("prefers AGENTS.md over CLAUDE.md in the same directory", async () => {
    const o = layout({ "repo/AGENTS.md": "agents", "repo/CLAUDE.md": "claude" });

    const files = await load(o);

    expect(files.map((f) => f.content)).toEqual(["agents"]);
  });

  it("loads only ~/.tree-agent/AGENTS.md in an untrusted cwd", async () => {
    const o = layout({ "home/AGENTS.md": "user rules", "repo/AGENTS.md": "repo rules" });

    const files = await loadContextFiles({ ...o, trusted: false, maxChars: 40_000 });

    expect(files).toEqual([{ path: join(root(), "home", "AGENTS.md"), content: "user rules" }]);
  });

  it("truncates a 50 000-char file to its first 40 000 chars plus a marker", async () => {
    const big = `${"a".repeat(40_000)}${"b".repeat(10_000)}`;
    const o = layout({ "repo/AGENTS.md": big });

    const [file] = await load(o);

    const kept = file?.content.slice(0, 40_000);
    const marker = file?.content.slice(40_000);
    expect(kept).toBe("a".repeat(40_000));
    expect(marker).toBe(
      `\n\n[truncated: showing 40000 of 50000 chars; read ${join(root(), "repo", "AGENTS.md")} for the rest]`,
    );
  });

  it("caps the total at 3× maxChars by cutting the file that crosses it", async () => {
    const o = layout({
      "home/AGENTS.md": "h".repeat(10),
      "AGENTS.md": "r".repeat(10),
      "repo/AGENTS.md": "p".repeat(10),
      "repo/sub/AGENTS.md": "s".repeat(10),
    });

    const files = await load(o, true, 12); // total cap 36

    expect(files.map((f) => f.content.split("\n")[0])).toEqual([
      "h".repeat(10),
      "r".repeat(10),
      "p".repeat(10),
      "s".repeat(6),
    ]);
    expect(files[3]?.content).toContain("[truncated: showing 6 of 10 chars;");
  });

  it("drops the files after the total cap is spent", async () => {
    const o = layout({
      "home/AGENTS.md": "h".repeat(10),
      "AGENTS.md": "r".repeat(10),
      "repo/AGENTS.md": "p".repeat(10),
      "repo/sub/AGENTS.md": "s".repeat(10),
    });

    const files = await load(o, true, 10); // total cap 30

    expect(files.map((f) => f.content)).toEqual(["h".repeat(10), "r".repeat(10), "p".repeat(10)]);
  });
});

describe("formatContextFiles", () => {
  it("injects each file as `# <absolute path>` then its content", () => {
    expect(
      formatContextFiles([
        { path: "/a/AGENTS.md", content: "one\n" },
        { path: "/a/b/CLAUDE.md", content: "two" },
      ]),
    ).toBe("# /a/AGENTS.md\none\n\n# /a/b/CLAUDE.md\ntwo");
  });
});
