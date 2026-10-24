import { describe, expect, it } from "vitest";
import { CLIPPED, displayPath, primaryArgument } from "../../src/permissions/summary.ts";
import type { ToolCall } from "../../src/types.ts";

const call = (name: string, args: Record<string, unknown>): ToolCall => ({
  id: "c",
  name,
  arguments: args,
});

describe("primaryArgument", () => {
  it("shows bash's whole command", () => {
    expect(primaryArgument(call("bash", { command: "pnpm test\npnpm lint" }), "/w")).toBe(
      "pnpm test\npnpm lint",
    );
  });

  it.each(["read", "edit", "write"])("shows %s's path relative to the cwd", (name) => {
    expect(
      primaryArgument(call(name, { path: "/work/repo/src/a.ts", content: "x" }), "/work/repo"),
    ).toBe("src/a.ts");
  });

  it("keeps a path outside the cwd as given", () => {
    expect(primaryArgument(call("write", { path: "/etc/hosts" }), "/work/repo")).toBe("/etc/hosts");
    expect(primaryArgument(call("edit", { path: "~/.zshrc" }), "/work/repo")).toBe("~/.zshrc");
  });

  it("shows codemode's description and the first 2 KB of its script", () => {
    const script = `${"a".repeat(2048)}TAIL`;

    const text = primaryArgument(call("codemode", { description: "count files", script }), "/w");

    expect(text).toBe(`count files\n${"a".repeat(2048)}${CLIPPED}`);
  });

  it("shows an MCP call's input JSON, cut at 2 KB", () => {
    const args = { query: "x".repeat(3000) };

    const text = primaryArgument(call("mcp__tree__search_memory", args), "/w");

    expect(text.endsWith(CLIPPED)).toBe(true);
    const body = text.slice(0, -CLIPPED.length);
    expect(Buffer.byteLength(body)).toBe(2048);
    expect(JSON.stringify(args).startsWith(body)).toBe(true);
  });

  it("shows a short MCP input JSON whole", () => {
    expect(primaryArgument(call("mcp__a__b", { id: 1 }), "/w")).toBe('{"id":1}');
  });

  it("never splits a UTF-8 character at the 2 KB cut", () => {
    const text = primaryArgument(call("bash", { command: `${"a".repeat(2047)}é` }), "/w");

    expect(text).toBe(`${"a".repeat(2047)}${CLIPPED}`);
  });
});

describe("displayPath", () => {
  it("is . for the cwd itself", () => {
    expect(displayPath("/w", "/w")).toBe(".");
    expect(displayPath("sub/../", "/w")).toBe(".");
  });
});
