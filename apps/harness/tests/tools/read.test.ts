import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { readTool } from "../../src/tools/read.ts";
import { toolCtx, useTmpDir } from "../helpers/tmp.ts";

const dir = useTmpDir("read");
const numbered = (n: number) => Array.from({ length: n }, (_, i) => `line ${i + 1}`);

function file(name: string, content: string): string {
  const path = join(dir(), name);
  writeFileSync(path, content);
  return path;
}

const read = (params: { path: string; offset?: number; limit?: number }) =>
  readTool().execute(params, toolCtx(dir()));

describe("read", () => {
  it("returns a small file whole, with structured {path, lines}", async () => {
    const path = file("a.txt", "one\ntwo\n");

    const result = await read({ path: "a.txt" });

    expect(result).toEqual({ content: "one\ntwo", structured: { path, lines: ["one", "two"] } });
  });

  it("answers 2 000 lines of a 3 000-line file plus a paging hint", async () => {
    file("big.txt", `${numbered(3000).join("\n")}\n`);

    const result = await read({ path: "big.txt" });

    const lines = result.content.split("\n");
    expect(lines.slice(0, 2000)).toEqual(numbered(2000));
    expect(lines.at(-1)).toBe("[Showing lines 1-2000 of 3000. Use offset=2001 to continue.]");
    expect((result.structured as { lines: string[] }).lines).toHaveLength(2000);
  });

  it("pages with offset and limit", async () => {
    file("big.txt", numbered(3000).join("\n"));

    const next = await read({ path: "big.txt", offset: 2001 });
    const slice = await read({ path: "big.txt", offset: 10, limit: 3 });

    expect(next.content.split("\n")).toEqual(numbered(3000).slice(2000));
    expect(slice.content).toBe(
      "line 10\nline 11\nline 12\n\n[Showing lines 10-12 of 3000. Use offset=13 to continue.]",
    );
  });

  it("caps a page at 50 KB even under 2 000 lines", async () => {
    const long = "x".repeat(1000);
    file("wide.txt", Array.from({ length: 100 }, () => long).join("\n"));

    const result = await read({ path: "wide.txt" });

    expect(Buffer.byteLength(result.content)).toBeLessThan(52 * 1024);
    expect(result.content).toMatch(/\[Showing lines 1-51 of 100\. Use offset=52 to continue\.\]$/);
  });

  it("cuts a single line over 50 KB by bytes and says how much was cut and how to read on", async () => {
    // 3-byte characters: a UTF-16 slice would keep 150 KB.
    const path = file("min.js", `${"€".repeat(70_000)}\nnext`);

    const result = await read({ path: "min.js" });

    const [line, , notice, , hint] = result.content.split("\n");
    expect(Buffer.byteLength(line ?? "")).toBe(51_198);
    expect(line).toBe("€".repeat(17_066));
    expect(notice).toBe(
      "[Line 1 cut: showing the first 51198 of 210000 bytes. offset/limit page by line; " +
        `read the rest of this line with bash, e.g. tail -c +51199 'min.js' | head -c 51200.]`,
    );
    expect(hint).toBe("[Showing lines 1-1 of 2. Use offset=2 to continue.]");
    expect(Buffer.byteLength(result.content)).toBeLessThan(51.5 * 1024);
    expect(result.structured).toEqual({ path, lines: [line] });
  });

  it("says (empty file) for an empty file", async () => {
    const path = file("empty.txt", "");

    expect(await read({ path: "empty.txt" })).toEqual({
      content: "(empty file)",
      structured: { path, lines: [] },
    });
  });

  it("errors on an offset past the end of the file", async () => {
    file("a.txt", "one\ntwo\n");

    const result = await read({ path: "a.txt", offset: 5 });

    expect(result).toEqual({
      content: "offset 5 is past the end of a.txt (2 lines)",
      isError: true,
    });
  });

  it("resolves the path against cwd and accepts absolute paths", async () => {
    mkdirSync(join(dir(), "sub"));
    const abs = file("sub/b.txt", "bee");

    expect((await read({ path: "sub/b.txt" })).content).toBe("bee");
    expect((await read({ path: abs })).content).toBe("bee");
  });

  it("is a read-only direct tool", () => {
    expect(readTool()).toMatchObject({ name: "read", exposure: "direct", isReadOnly: true });
  });
});
