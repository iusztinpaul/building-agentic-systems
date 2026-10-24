import { expect, test } from "bun:test";

// Keeps `bun test`, `tsc` and Biome green until the rewrite adds real tests.
test("harness entry exists", () => {
  expect(Bun.file(new URL("../../src/index.ts", import.meta.url)).size).toBeGreaterThan(0);
});
