import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { editTool } from "../../src/tools/edit.ts";
import { toolCtx, useTmpDir } from "../helpers/tmp.ts";

const dir = useTmpDir("edit");

function setup(content: string): string {
  const path = join(dir(), "notes.md");
  writeFileSync(path, content);
  return path;
}

const edit = (oldText: string, newText: string) =>
  editTool().execute({ path: "notes.md", oldText, newText }, toolCtx(dir()));

describe("edit", () => {
  it("replaces the one exact match", async () => {
    const path = setup("- a\n- b\n");

    const result = await edit("- b\n", "- b\n- c\n");

    expect(result).toEqual({ content: "Edited notes.md" });
    expect(readFileSync(path, "utf8")).toBe("- a\n- b\n- c\n");
  });

  it("errors with the count when there is no match, leaving the file alone", async () => {
    const path = setup("- a\n");

    const result = await edit("- z", "- y");

    expect(result.isError).toBe(true);
    expect(result.content).toMatch(/^oldText matched 0 times in notes\.md/);
    expect(readFileSync(path, "utf8")).toBe("- a\n");
  });

  it("errors with the count on two matches", async () => {
    const path = setup("x\nx\n");

    const result = await edit("x", "y");

    expect(result.isError).toBe(true);
    expect(result.content).toMatch(/^oldText matched 2 times in notes\.md; include more/);
    expect(readFileSync(path, "utf8")).toBe("x\nx\n");
  });

  it("inserts newText literally, `$&` patterns included", async () => {
    const path = setup("price: X\n");

    await edit("X", "$& and $1");

    expect(readFileSync(path, "utf8")).toBe("price: $& and $1\n");
  });
});
