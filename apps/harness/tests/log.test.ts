import { describe, expect, it } from "vitest";
import { createLogger } from "../src/log.ts";

function capture(level: string | undefined): string[] {
  const lines: string[] = [];
  const log = createLogger(level, { write: (text: string) => lines.push(text) });
  log.debug("d");
  log.info("i");
  log.warn("w");
  log.error("e");
  return lines;
}

describe("createLogger", () => {
  it("writes warn and error by default", () => {
    expect(capture(undefined)).toEqual(["[tree-agent] warn: w\n", "[tree-agent] error: e\n"]);
  });

  it("writes everything at debug and nothing at silent", () => {
    expect(capture("debug")).toHaveLength(4);
    expect(capture("silent")).toEqual([]);
  });

  it("falls back to warn for an unknown level", () => {
    expect(capture("loud")).toHaveLength(2);
  });
});
