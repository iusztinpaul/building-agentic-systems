import { describe, expect, test } from "bun:test";
import {
  cwdHash,
  sessionPath,
  sessionsDirFor,
  subagentSessionPath,
} from "../../../src/session/paths";

describe("cwdHash", () => {
  test("is deterministic for the same cwd", () => {
    expect(cwdHash("/repo/foo")).toBe(cwdHash("/repo/foo"));
  });

  test("is different for different cwds", () => {
    expect(cwdHash("/repo/foo")).not.toBe(cwdHash("/repo/bar"));
  });

  test("is 12 hex chars", () => {
    expect(cwdHash("/any/path")).toMatch(/^[0-9a-f]{12}$/);
  });
});

describe("sessionPath", () => {
  test("lives under <tree>/projects/<hash>/<id>.jsonl", () => {
    const path = sessionPath("/repo/x", "abc-123");
    expect(path).toEndWith(`/projects/${cwdHash("/repo/x")}/abc-123.jsonl`);
  });
});

describe("subagentSessionPath", () => {
  test("nests the subagent id under the parent dir", () => {
    const path = subagentSessionPath("/repo/x", "parent-id", "sub-id");
    expect(path).toEndWith(`/projects/${cwdHash("/repo/x")}/parent-id/sub-id.jsonl`);
  });

  test("siblings of the parent jsonl: parent and subagent dir share the same prefix", () => {
    const parent = sessionPath("/repo/x", "parent-id");
    const sub = subagentSessionPath("/repo/x", "parent-id", "sub-id");
    expect(parent.replace(/\.jsonl$/, "")).toBe(sub.replace(/\/sub-id\.jsonl$/, ""));
  });

  test("sessionsDirFor matches the dirname of sessionPath", () => {
    const dir = sessionsDirFor("/repo/x");
    const path = sessionPath("/repo/x", "abc");
    expect(path.startsWith(`${dir}/`)).toBe(true);
  });
});
