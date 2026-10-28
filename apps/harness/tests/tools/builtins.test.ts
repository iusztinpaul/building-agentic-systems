import { describe, expect, it } from "vitest";
import { builtinRegistry } from "../../src/tools/builtins.ts";

describe("builtinRegistry", () => {
  it("declares exactly the four built-ins, all direct, with no MCP servers", () => {
    const registry = builtinRegistry({ tmpDir: "/tmp/x" });

    expect(registry.schemasForRequest().map((s) => s.name)).toEqual([
      "read",
      "bash",
      "edit",
      "write",
    ]);
    expect(registry.list().every((t) => t.exposure === "direct")).toBe(true);
    expect(registry.get("tool_search")).toBeUndefined();
    expect(registry.get("codemode")).toBeUndefined();
  });

  it("marks only read as read-only", () => {
    const registry = builtinRegistry({ tmpDir: "/tmp/x" });

    expect(
      registry
        .list()
        .filter((t) => t.isReadOnly)
        .map((t) => t.name),
    ).toEqual(["read"]);
  });
});
