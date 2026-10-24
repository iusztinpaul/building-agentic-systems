import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, test } from "vitest";

const CLI = fileURLToPath(new URL("../src/cli.ts", import.meta.url));

test("cli without a prompt prints usage to stderr and exits 1", () => {
  const result = spawnSync(process.execPath, [CLI], { encoding: "utf8" });

  expect(result.status).toBe(1);
  expect(result.stdout).toBe("");
  expect(result.stderr).toMatch(/^usage: tree-agent /m);
});
