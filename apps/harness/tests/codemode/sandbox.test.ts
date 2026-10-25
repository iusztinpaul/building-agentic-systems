import { QuickJSRuntime } from "quickjs-emscripten";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  type AsyncHostFn,
  LOGS_CUT,
  runSandbox,
  type SandboxOptions,
} from "../../src/codemode/sandbox.ts";
import { log } from "../../src/log.ts";

const LIMITS = { timeoutMs: 2_000, memoryMb: 64, concurrency: 4 };
const run = (script: string, o: Partial<SandboxOptions> = {}) =>
  runSandbox({ ...LIMITS, script, ...o });

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const timeouts = () => process.getActiveResourcesInfo().filter((r) => r === "Timeout").length;

// `wait(ms)` in the script: a host promise settled after ms.
const waitHost = (onStart?: () => void) => ({
  prelude: "globalThis.wait = (ms) => host.wait(String(ms));",
  async: {
    wait: (async (input) => {
      onStart?.();
      await sleep(Number(input));
      return input;
    }) satisfies AsyncHostFn,
  },
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("runSandbox", () => {
  it("returns the console.log lines and the return value as JSON", async () => {
    const result = await run('console.log("one", {a: 1}); console.log(2); return {ok: [1, "x"]};');

    expect(result).toEqual({ logs: ['one {"a":1}', "2"], value: '{"ok":[1,"x"]}' });
  });

  it("has no value when the script returns nothing", async () => {
    expect(await run('console.log("only")')).toEqual({ logs: ["only"] });
  });

  it("exposes no fetch, require, llm, setInterval or host; setTimeout works", async () => {
    const result = await run(`
      await new Promise((r) => setTimeout(r, 20));
      return [typeof fetch, typeof require, typeof llm, typeof setInterval,
        typeof process, typeof host, typeof __host, typeof setTimeout];
    `);

    expect(JSON.parse(result.value ?? "")).toEqual([...Array(7).fill("undefined"), "function"]);
  });

  it("calling setInterval is a ReferenceError", async () => {
    const result = await run("setInterval(() => {}, 10)");

    expect(result.error).toBe("ReferenceError: 'setInterval' is not defined");
  });

  it("clearTimeout cancels a pending setTimeout", async () => {
    const result = await run(`
      let fired = false;
      const id = setTimeout(() => { fired = true; }, 10);
      clearTimeout(id);
      clearTimeout(); clearTimeout("x"); clearTimeout(999); // ignored, as in Node
      await new Promise((r) => setTimeout(r, 40));
      return fired;
    `);

    expect(result.value).toBe("false");
  });

  it("stops while (true) {} at timeoutMs within 1 s, disposes the runtime, and the next run works", async () => {
    const dispose = vi.spyOn(QuickJSRuntime.prototype, "dispose");
    const started = Date.now();

    const result = await run("while (true) {}", { timeoutMs: 200 });

    expect(Date.now() - started).toBeLessThan(1_000);
    expect(result.error).toBe("the script timed out after 200 ms (codemode.timeoutMs)");
    expect(dispose).toHaveBeenCalledTimes(1);
    expect((dispose.mock.contexts[0] as QuickJSRuntime).alive).toBe(false);
    expect(await run("return 1 + 1")).toEqual({ logs: [], value: "2" });
  });

  it("a try/catch around the loop cannot swallow the timeout", async () => {
    const result = await run('try { while (true) {} } catch { return "caught"; }', {
      timeoutMs: 200,
    });

    expect(result.error).toMatch(/timed out/);
  });

  it("times out a script idle on a host promise (the host timer, not the interrupt)", async () => {
    const started = Date.now();

    const result = await run("await wait(60000)", { ...waitHost(), timeoutMs: 200 });

    expect(result.error).toMatch(/timed out after 200 ms/);
    expect(Date.now() - started).toBeLessThan(1_000);
  });

  it("leaves no host timer behind after a timeout", async () => {
    const before = timeouts();

    await run("setTimeout(() => {}, 60000); setTimeout(() => {}, 60000); while (true) {}", {
      timeoutMs: 100,
    });

    expect(timeouts()).toBe(before);
  });

  it("an allocation loop hits memoryMb: an error result, the process lives, the next run works", async () => {
    const result = await run("const a = []; while (true) a.push(new Array(10000).fill(1));", {
      memoryMb: 16,
      timeoutMs: 10_000,
    });

    expect(result.error).toBe("InternalError: out of memory (codemode.memoryMb = 16)");
    expect(await run('return "alive"')).toEqual({ logs: [], value: '"alive"' });
  });

  it("an out-of-memory in a long .then chain names memoryMb; a `throw null` stays null", async () => {
    const chain = await run(
      "let p = Promise.resolve(0); for (let i = 0; i < 1e6; i++) p = p.then((v) => v + 1); return await p;",
      { timeoutMs: 20_000 },
    );

    expect(chain.error).toBe("InternalError: out of memory (codemode.memoryMb = 64)");
    expect((await run("throw null")).error).toBe("null");
  });

  it("a 3 MB returned string comes back whole; the module's stderr goes to log.debug", async () => {
    const debug = vi.spyOn(log, "debug").mockImplementation(() => {});
    const stderr = vi.spyOn(console, "error");

    const result = await run('return "x".repeat(3_000_000)');

    expect(result.value?.length).toBe(3_000_002);
    expect(result.error).toBeUndefined();
    const lines = debug.mock.calls.flat();
    expect(lines.some((line) => line.startsWith("codemode sandbox: Aborted("))).toBe(true);
    expect(stderr).not.toHaveBeenCalled();
  });

  it("deep recursion is a stack-overflow error, deep JSON a sandbox crash; neither kills the worker", async () => {
    const recursion = await run("function f() { return f() + 1; } return f();");
    const deepJson = await run('return JSON.parse("[".repeat(200000) + "]".repeat(200000));');

    expect(recursion.error).toBe("InternalError: stack overflow");
    expect(deepJson.error).toMatch(/^the sandbox crashed: /);
    expect(await run("return 3")).toEqual({ logs: [], value: "3" });
  });

  it("fans Promise.all out over host calls, at most `concurrency` in flight", async () => {
    let inFlight = 0;
    let peak = 0;
    const host: AsyncHostFn = async (input) => {
      peak = Math.max(peak, ++inFlight);
      await sleep(30);
      inFlight--;
      return input;
    };

    const result = await run(
      "return await Promise.all([1,2,3,4,5,6,7,8].map((n) => call(String(n))));",
      { prelude: "globalThis.call = (x) => host.call(x);", async: { call: host } },
    );

    expect(result.value).toBe('["1","2","3","4","5","6","7","8"]');
    expect(peak).toBe(4);
  });

  it("a held host call does not use up the deadline", async () => {
    const result = await run('await hold(); return "done";', {
      timeoutMs: 100,
      prelude: "globalThis.hold = () => host.hold('');",
      async: {
        hold: async (_input, call) => {
          const release = call.hold();
          await sleep(300); // e.g. the user reading a prompt
          release();
          return "";
        },
      },
    });

    expect(result).toEqual({ logs: [], value: '"done"' });
  });

  it("still stops a busy loop while another call holds the deadline", async () => {
    const started = Date.now();

    const result = await run("hold(); await wait(20); while (true) {}", {
      timeoutMs: 200,
      prelude: `${waitHost().prelude}\nglobalThis.hold = () => host.hold('');`,
      async: {
        ...waitHost().async,
        hold: (_input, call) => {
          call.hold();
          return new Promise<string>(() => {}); // never settles
        },
      },
    });

    expect(result.error).toMatch(/timed out/);
    expect(Date.now() - started).toBeLessThan(1_000);
  });

  it("an abort tears the script down before a catch can run", async () => {
    const controller = new AbortController();
    let started!: () => void;
    const begun = new Promise<void>((r) => {
      started = r;
    });

    const pending = run(
      'try { await wait(60000); } catch { console.log("caught"); } return "after";',
      { ...waitHost(() => started()), signal: controller.signal },
    );
    await begun;
    controller.abort("blocked");
    const result = await pending;

    expect(result).toEqual({ logs: [], error: "aborted", aborted: true });
  });

  it("an already-aborted signal runs nothing", async () => {
    const controller = new AbortController();
    controller.abort();

    expect(await run('console.log("x")', { signal: controller.signal })).toMatchObject({
      logs: [],
      aborted: true,
    });
  });

  it("reports a script awaiting a promise that can never settle at once", async () => {
    const started = Date.now();

    const result = await run("await new Promise(() => {});");

    expect(result.error).toBe("the script awaits a promise that can never settle");
    expect(Date.now() - started).toBeLessThan(500);
  });

  it("reports syntax errors, thrown errors and rejected host calls", async () => {
    expect((await run("return (")).error).toMatch(/^SyntaxError: /);
    expect((await run('throw new TypeError("bad")')).error).toBe("TypeError: bad");
    const failing = await run("try { await call('x'); } catch (e) { return e.message; }", {
      prelude: "globalThis.call = (x) => host.call(x);",
      async: {
        call: async () => {
          throw new Error("no such thing");
        },
      },
    });
    expect(failing.value).toBe('"no such thing"');
  });

  it("logs an error thrown in a setTimeout callback and goes on", async () => {
    const result = await run(`
      setTimeout(() => { throw new Error("late"); }, 0);
      await new Promise((r) => setTimeout(r, 20));
      return "ok";
    `);

    expect(result).toEqual({ logs: ["Uncaught Error: late"], value: '"ok"' });
  });

  it("caps captured console output at 10 MB", async () => {
    const result = await run(
      'const s = "x".repeat(1000); for (let i = 0; i < 12000; i++) console.log(s);',
    );

    expect(result.logs.length).toBeLessThan(12_000);
    expect(result.logs.at(-1)).toBe(LOGS_CUT);
  });

  it("a sync host function's throw is a catchable error", async () => {
    const result = await run("try { ping('x'); } catch (e) { return e.message; }", {
      prelude: "globalThis.ping = (x) => host.ping(x);",
      sync: {
        ping: () => {
          throw new Error("pong");
        },
      },
    });

    expect(result.value).toBe('"pong"');
  });
});
