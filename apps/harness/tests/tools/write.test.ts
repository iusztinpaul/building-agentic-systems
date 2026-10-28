import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { writeTool } from "../../src/tools/write.ts";
import { toolCtx, useTmpDir } from "../helpers/tmp.ts";

const dir = useTmpDir("write");

describe("write", () => {
  it("creates missing parent directories", async () => {
    const result = await writeTool().execute(
      { path: "a/b/c.md", content: "héllo" },
      toolCtx(dir()),
    );

    expect(result).toEqual({ content: "Wrote 6 bytes to a/b/c.md" });
    expect(readFileSync(join(dir(), "a/b/c.md"), "utf8")).toBe("héllo");
  });

  it("replaces an existing file", async () => {
    writeFileSync(join(dir(), "x.txt"), "old");

    await writeTool().execute({ path: "x.txt", content: "new" }, toolCtx(dir()));

    expect(readFileSync(join(dir(), "x.txt"), "utf8")).toBe("new");
  });
});
