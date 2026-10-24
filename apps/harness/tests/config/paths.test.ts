import { homedir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { userPaths } from "../../src/config/paths.ts";

describe("userPaths", () => {
  it("resolves every path under TREE_AGENT_HOME when it is set", () => {
    const paths = userPaths({ TREE_AGENT_HOME: "/tmp/x" });

    expect(paths.home).toBe("/tmp/x");
    for (const path of Object.values(paths)) {
      expect(path === "/tmp/x" || path.startsWith("/tmp/x/")).toBe(true);
    }
    expect(paths.settings).toBe("/tmp/x/settings.json");
    expect(paths.sessions).toBe("/tmp/x/sessions");
  });

  it("defaults to ~/.tree-agent", () => {
    const paths = userPaths({});

    expect(paths.home).toBe(join(homedir(), ".tree-agent"));
    expect(paths.mcp).toBe(join(homedir(), ".tree-agent", "mcp.json"));
  });

  it("makes a relative TREE_AGENT_HOME absolute", () => {
    expect(userPaths({ TREE_AGENT_HOME: "rel/home" }).home).toBe(join(process.cwd(), "rel/home"));
  });
});
