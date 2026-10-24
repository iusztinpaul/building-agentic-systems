import { stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { describe, expect, it } from "vitest";
import type { SessionStatus } from "../../src/session.ts";
import { compact, formatStatus, StatusBar } from "../../src/tui/status-bar.ts";

const status: SessionStatus = {
  model: "anthropic/claude-sonnet-5.5",
  mode: "ask",
  tokens: 12_345,
  contextLength: 1_000_000,
  cost: 0.04213,
  mcpServers: 0,
  activatedTools: 2,
};

describe("formatStatus", () => {
  it("shows model · mode · tokens / context (NN %) · cost · MCP servers · activated tools", () => {
    expect(formatStatus(status)).toBe(
      "anthropic/claude-sonnet-5.5 · ask · 12.3k / 1M (1 %) · $0.0421 · 0 MCP · 2 activated",
    );
  });

  it("shows a zero gauge before the first answer", () => {
    expect(formatStatus({ ...status, tokens: undefined, cost: 0 })).toContain(
      "· 0 / 1M (0 %) · $0.0000 ·",
    );
  });

  it("rounds the percentage", () => {
    expect(formatStatus({ ...status, tokens: 160_000, contextLength: 200_000 })).toContain(
      "160k / 200k (80 %)",
    );
  });
});

describe("compact", () => {
  it.each([
    [0, "0"],
    [950, "950"],
    [1000, "1k"],
    [12_345, "12.3k"],
    [1_000_000, "1M"],
    [1_048_576, "1M"],
    [2_500_000, "2.5M"],
  ])("%d → %s", (n, text) => {
    expect(compact(n)).toBe(text);
  });
});

describe("StatusBar", () => {
  it("renders one line that fits the width", () => {
    const bar = new StatusBar();
    bar.set(status);

    const [line = ""] = bar.render(40);
    expect(bar.render(200).map(stripTerminalSequences)).toEqual([` ${formatStatus(status)}`]);
    expect(visibleWidth(line)).toBeLessThanOrEqual(40);
  });
});
