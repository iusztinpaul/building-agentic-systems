import { spawnSync } from "node:child_process";
import { realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createLogger } from "../../src/log.ts";
import { handleSignal, StdioTransport } from "../../src/mcp/transport-stdio.ts";
import { gone } from "../helpers/tmp.ts";

const SERVER = fileURLToPath(new URL("../fixtures/fake-mcp-server.mjs", import.meta.url));

const open: StdioTransport[] = [];
// Process groups and grandchildren a test started: SIGKILLed after
// it, so a failing run leaves no grandchild behind.
const groups: number[] = [];
const strays: number[] = [];
afterEach(async () => {
  vi.restoreAllMocks();
  await Promise.all(open.splice(0).map((t) => t.close()));
  for (const pid of [...groups.splice(0).map((g) => -g), ...strays.splice(0)]) {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // already gone
    }
  }
});

// A `uv run`-like child: it spawns a grandchild that inherits the
// pipes (so stdout stays open after the child dies), reports the
// grandchild's pid as one frame, and exits on stdin EOF.
const SPAWNS_PIPE_HOLDER =
  "const g = require('node:child_process').spawn('sleep', ['30'], { stdio: 'inherit' });" +
  "console.log(JSON.stringify({ jsonrpc: '2.0', method: 'spawned', params: { pid: g.pid } }));" +
  "process.stdin.resume(); process.stdin.on('end', () => process.exit(0));";

// The same, but the grandchild leaves the group (setsid): no signal
// of close() reaches it, so only the bounded wait ends close().
const SPAWNS_ESCAPED_PIPE_HOLDER = SPAWNS_PIPE_HOLDER.replace(
  "{ stdio: 'inherit' }",
  "{ stdio: 'inherit', detached: true }",
);

// The pids left in process group `pgid` (pgrep -g), none when empty.
function inGroup(pgid: number): string {
  return spawnSync("pgrep", ["-g", String(pgid)], { encoding: "utf8" }).stdout.trim();
}

function transport(
  args: string[],
  extra: Partial<ConstructorParameters<typeof StdioTransport>[0]> = {},
) {
  const logged: string[] = [];
  const log = createLogger("debug", { write: (t: string) => logged.push(t) });
  const t = new StdioTransport({
    command: process.execPath,
    args,
    env: {},
    log,
    label: 'mcp "t": ',
    ...extra,
  });
  open.push(t);
  const messages: unknown[] = [];
  const closed: Error[] = [];
  const handlers = {
    onMessage: (m: unknown) => messages.push(m),
    onClose: (e: Error) => closed.push(e),
  };
  return { t, logged, messages, closed, handlers };
}

async function until(check: () => boolean, ms = 3000): Promise<void> {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error("timed out waiting");
    await new Promise((r) => setTimeout(r, 10));
  }
}

describe("StdioTransport", () => {
  it("writes one JSON frame per line and reads one per line", async () => {
    const { t, messages, handlers } = transport([SERVER]);
    await t.start(handlers);

    await t.send({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-06-18" },
    });
    await until(() => messages.length === 1);

    expect(messages[0]).toMatchObject({
      jsonrpc: "2.0",
      id: 1,
      result: { protocolVersion: "2025-06-18" },
    });
  });

  it("forwards the child's stderr to the logger at info, never to stdout", async () => {
    const stdout = vi.spyOn(process.stdout, "write");
    const { t, logged, handlers } = transport([SERVER]);
    await t.start(handlers);

    await t.send({ jsonrpc: "2.0", method: "notifications/initialized" });
    await until(() => logged.some((l) => l.includes("recv notifications/initialized")));

    expect(logged).toContain('[tree-agent] info: mcp "t": fake-mcp-server: ready\n');
    const printed = stdout.mock.calls.map(([chunk]) => String(chunk)).join("");
    expect(printed).not.toContain("fake-mcp-server");
    expect(printed).not.toContain("recv ");
  });

  it("merges env over the base env and runs in cwd", async () => {
    const script =
      "process.stdout.write(JSON.stringify({jsonrpc:'2.0',method:'env'," +
      "params:{a:process.env.A,b:process.env.B,cwd:process.cwd()}})+'\\n')";
    const cwd = realpathSync(tmpdir());
    const { t, messages, handlers } = transport(["-e", script], {
      env: { A: "from-config", B: "config-wins" },
      baseEnv: { B: "base", PATH: process.env.PATH },
      cwd,
    });
    await t.start(handlers);
    await until(() => messages.length === 1);

    expect(messages[0]).toMatchObject({ params: { a: "from-config", b: "config-wins", cwd } });
  });

  it("warns on a stdout line that is not JSON and keeps reading", async () => {
    const script =
      "console.log('hello there'); console.log(JSON.stringify({jsonrpc:'2.0',method:'x'}))";
    const { t, logged, messages, handlers } = transport(["-e", script]);
    await t.start(handlers);
    await until(() => messages.length === 1);

    expect(logged.join("")).toContain('warn: mcp "t": not JSON on stdout: hello there');
  });

  it("delivers a frame written just before exiting, then reports the drop", async () => {
    const script =
      "console.log(JSON.stringify({jsonrpc:'2.0',id:1,result:'last'})); process.exit(3)";
    const { t, messages, closed, logged, handlers } = transport(["-e", script]);
    await t.start(handlers);
    await until(() => closed.length === 1);

    expect(messages).toEqual([{ jsonrpc: "2.0", id: 1, result: "last" }]);
    expect(closed[0]?.message).toBe('mcp "t": server exited (code 3)');
    expect(logged.join("")).toContain('warn: mcp "t": server exited (code 3)');
    await expect(t.send({ jsonrpc: "2.0", method: "x" })).rejects.toThrow("not connected");
  });

  it("close() ends the child (stdin closed) and logs no crash", async () => {
    const { t, logged, closed, handlers } = transport([SERVER]);
    await t.start(handlers);
    const pid = t.pid as number;

    await t.close();

    expect(await gone(pid)).toBe(true);
    expect(closed).toHaveLength(1);
    expect(logged.join("")).not.toContain("server exited");
  });

  it("close() kills a child that ignores stdin EOF", async () => {
    const { t, handlers } = transport([
      "-e",
      "process.stdin.resume(); process.stdin.on('end', () => {}); setInterval(() => {}, 1000)",
    ]);
    await t.start(handlers);
    const pid = t.pid as number;

    await t.close();

    expect(await gone(pid)).toBe(true);
  });

  it("close() ends a grandchild that holds the pipes open and returns promptly", async () => {
    const { t, messages, handlers } = transport(["-e", SPAWNS_PIPE_HOLDER]);
    await t.start(handlers);
    const pid = t.pid as number;
    groups.push(pid);
    await until(() => messages.length === 1);
    const grandchild = (messages[0] as { params: { pid: number } }).params.pid;
    strays.push(grandchild);

    const started = Date.now();
    await t.close();

    expect(Date.now() - started).toBeLessThan(3000);
    expect(await gone(grandchild)).toBe(true);
    expect(inGroup(pid)).toBe("");
  });

  it("close() stops waiting ~1 s after the SIGKILL when a pipe outlives the group", async () => {
    const pipes = () => process.getActiveResourcesInfo().filter((r) => r === "PipeWrap").length;
    const before = pipes();
    const { t, messages, closed, handlers } = transport(["-e", SPAWNS_ESCAPED_PIPE_HOLDER]);
    await t.start(handlers);
    groups.push(t.pid as number);
    await until(() => messages.length === 1);
    strays.push((messages[0] as { params: { pid: number } }).params.pid);

    const started = Date.now();
    await t.close();

    expect(Date.now() - started).toBeGreaterThanOrEqual(3900);
    expect(Date.now() - started).toBeLessThan(6000);
    // The orphan still holds the pipes: the harness lets go of its
    // ends, so no handle keeps the process alive until it exits.
    await until(() => closed.length === 1, 1000);
    expect(pipes()).toBe(before);
  }, 10_000);

  it("names the command when it cannot be started", async () => {
    const { t, handlers } = transport([], { command: "/nonexistent/mcp-server" });

    await expect(t.start(handlers)).rejects.toThrow(
      'mcp "t": cannot start /nonexistent/mcp-server: spawn',
    );
  });
});

// A child that ignores stdin EOF: only a signal ends it.
const IGNORES_EOF =
  "process.stdin.resume(); process.stdin.on('end', () => {}); setInterval(() => {}, 1000)";

describe("handleSignal (the process SIGINT / SIGTERM hook)", () => {
  it("registers one prepended SIGINT and SIGTERM listener for every transport", async () => {
    const before = {
      int: process.listenerCount("SIGINT"),
      term: process.listenerCount("SIGTERM"),
    };
    for (let i = 0; i < 2; i++) {
      const { t, handlers } = transport([SERVER]);
      await t.start(handlers);
    }
    // The hook may already be in from an earlier test: never more than one.
    expect(process.listenerCount("SIGINT") - before.int).toBeLessThanOrEqual(1);
    expect(process.listenerCount("SIGTERM") - before.term).toBeLessThanOrEqual(1);
    expect(process.listenerCount("SIGINT")).toBeGreaterThanOrEqual(1);
  });

  it("alone (a second Ctrl+C), it kills an EOF-ignoring child, then exits 130", async () => {
    const { t, handlers } = transport(["-e", IGNORES_EOF]);
    await t.start(handlers);
    const pid = t.pid as number;
    const exit = vi.fn();

    await handleSignal("SIGINT", { listenerCount: () => 1, exit });

    expect(await gone(pid)).toBe(true);
    expect(exit).toHaveBeenCalledExactlyOnceWith(130);
  });

  it("alone, it also ends a pipe-holding grandchild before exiting", async () => {
    const { t, messages, handlers } = transport(["-e", SPAWNS_PIPE_HOLDER]);
    await t.start(handlers);
    const pid = t.pid as number;
    groups.push(pid);
    await until(() => messages.length === 1);
    const grandchild = (messages[0] as { params: { pid: number } }).params.pid;
    strays.push(grandchild);
    const exit = vi.fn();

    const started = Date.now();
    await handleSignal("SIGINT", { listenerCount: () => 1, exit });

    expect(Date.now() - started).toBeLessThan(3000);
    expect(await gone(grandchild)).toBe(true);
    expect(inGroup(pid)).toBe("");
    expect(exit).toHaveBeenCalledExactlyOnceWith(130);
  });

  it("SIGTERM alone exits 143", async () => {
    const { t, handlers } = transport(["-e", IGNORES_EOF]);
    await t.start(handlers);
    const exit = vi.fn();

    await handleSignal("SIGTERM", { listenerCount: () => 1, exit });

    expect(await gone(t.pid as number)).toBe(true);
    expect(exit).toHaveBeenCalledExactlyOnceWith(143);
  });

  it("with another listener (the headless abort), it leaves the child and the process alone", async () => {
    const { t, handlers } = transport(["-e", IGNORES_EOF]);
    await t.start(handlers);
    const pid = t.pid as number;
    const exit = vi.fn();

    await handleSignal("SIGINT", { listenerCount: () => 2, exit });

    expect(exit).not.toHaveBeenCalled();
    expect(() => process.kill(pid, 0)).not.toThrow();
  });
});
