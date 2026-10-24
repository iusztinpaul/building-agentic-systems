// The one-line footer: model · permission mode · context gauge ·
// session cost · MCP servers · activated tools. Every number comes
// from AgentSession.status(), so headless could print the same.

import { type Component, truncateToWidth } from "@earendil-works/pi-tui";
import type { SessionStatus } from "../session.ts";
import { dim } from "./style.ts";

export function formatStatus(s: SessionStatus): string {
  const tokens = s.tokens ?? 0;
  const ratio = s.contextLength > 0 ? tokens / s.contextLength : 0;
  const gauge =
    `${compact(tokens)} / ${compact(s.contextLength)} ` +
    `(${Math.round(ratio * 100)} %)`;
  return [
    s.model,
    s.mode,
    gauge,
    `$${s.cost.toFixed(4)}`,
    `${s.mcpServers} MCP`,
    `${s.activatedTools} activated`,
  ].join(" · ");
}

// 950 → "950", 12_345 → "12.3k", 1_000_000 → "1M".
export function compact(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${trim(n / 1000)}k`;
  return `${trim(n / 1_000_000)}M`;
}

function trim(n: number): string {
  return n.toFixed(1).replace(/\.0$/, "");
}

export class StatusBar implements Component {
  #text = "";

  set(status: SessionStatus): void {
    this.#text = formatStatus(status);
  }

  render(width: number): string[] {
    return [truncateToWidth(dim(` ${this.#text}`), width)];
  }

  invalidate(): void {}
}
