// The one diagnostic channel: leveled lines on stderr. Users see
// the agent through AgentEvents; this is for operators debugging the
// harness (TREE_AGENT_LOG=debug|info|warn|error|silent, default warn).

const LEVELS = ["debug", "info", "warn", "error", "silent"] as const;
export type LogLevel = (typeof LEVELS)[number];

export interface Logger {
  debug(message: string): void;
  info(message: string): void;
  warn(message: string): void;
  error(message: string): void;
}

export function createLogger(
  level: string | undefined,
  out: { write(text: string): unknown } = process.stderr,
): Logger {
  const threshold = rank(level);
  const at =
    (name: LogLevel) =>
    (message: string): void => {
      if (rank(name) >= threshold) {
        out.write(`[tree-agent] ${name}: ${message}\n`);
      }
    };
  return {
    debug: at("debug"),
    info: at("info"),
    warn: at("warn"),
    error: at("error"),
  };
}

function rank(level: string | undefined): number {
  const index = LEVELS.indexOf(level as LogLevel);
  return index === -1 ? LEVELS.indexOf("warn") : index;
}

export const log: Logger = createLogger(process.env.TREE_AGENT_LOG);
