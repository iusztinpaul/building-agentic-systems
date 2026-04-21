import { describe, expect, test } from "bun:test";
import { MCP_TOOL_PREFIX, classifyMcpTool } from "../../../src/mcp/adapter";

describe("classifyMcpTool", () => {
  test.each<[string, boolean]>([
    ["ingest_url", true],
    ["ingest_file", true],
    ["ingest_conversation", true],
    ["write_value", true],
    ["create_document", true],
    ["delete_entry", true],
    ["update_user", true],
    ["upsert_record", true],
    ["remove_item", true],
    ["add_tag", true],
    ["set_config", true],
    ["append_log", true],
    ["push_metric", true],
    ["modify_schema", true],
    ["edit_inline", true],
  ])("%s is destructive", (name, destructive) => {
    const { isReadOnly, isDestructive } = classifyMcpTool(name);
    expect(isDestructive).toBe(destructive);
    expect(isReadOnly).toBe(!destructive);
  });

  test.each<[string]>([
    ["query_memory"],
    ["search_memory"],
    ["deep_search_memory"],
    ["list_things"],
    ["get_status"],
    ["read_file"],
  ])("%s is read-only", (name) => {
    const { isReadOnly, isDestructive } = classifyMcpTool(name);
    expect(isReadOnly).toBe(true);
    expect(isDestructive).toBe(false);
  });
});

describe("MCP_TOOL_PREFIX", () => {
  test("matches the Claude-Code convention", () => {
    expect(MCP_TOOL_PREFIX).toBe("mcp__");
  });
});
