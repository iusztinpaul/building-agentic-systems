// The codemode sandbox (spec §10.4, ADR-016 §9): one QuickJS wasm
// module, runtime and context per script, all dropped after it. A
// host function returns a QuickJS promise and, whenever one settles,
// the host pumps executePendingJobs(), so `await Promise.all([...])`
// fans out (Asyncify would suspend the whole VM and serialise it).
// The host enforces the limits: a deadline (interrupt handler + a
// host timer), a memory cap, a cap on in-flight host calls. Nothing
// else is exposed: no require, fetch, fs, or timers but setTimeout.

import {
  type EmscriptenModuleLoaderOptions,
  newQuickJSWASMModule,
  newVariant,
  type QuickJSContext,
  type QuickJSDeferredPromise,
  type QuickJSHandle,
  type QuickJSRuntime,
  RELEASE_SYNC,
} from "quickjs-emscripten";
import { log } from "../log.ts";

// Node has it; the ES2022 lib only types the namespace.
declare const WebAssembly: {
  Memory: new (d: {
    initial: number;
    maximum: number;
  }) => { buffer: ArrayBuffer };
};

export interface SandboxLimits {
  timeoutMs: number;
  memoryMb: number;
  concurrency: number; // async host calls in flight at once
}

// What an async host function gets besides its input.
export interface HostCall {
  signal: AbortSignal; // aborted when the script ends
  // Stops the deadline until release() is called: a permission
  // prompt is the user's time, not the script's.
  hold(): () => void;
}

// Strings across the boundary; the prelude does the JSON.
export type AsyncHostFn = (
  input: string,
  call: HostCall,
) => Promise<string>;
export type SyncHostFn = (input: string) => string;

export interface SandboxOptions extends SandboxLimits {
  script: string; // an async function body: `return` allowed
  // Runs before the script with `host` (the functions below) in
  // scope and defines the script's globals; `host` is gone after.
  prelude?: string;
  async?: Record<string, AsyncHostFn>;
  sync?: Record<string, SyncHostFn>;
  signal?: AbortSignal; // the run's: an abort stops the script
}

export interface SandboxResult {
  logs: string[];
  value?: string; // the returned value as JSON; none for undefined
  error?: string;
  aborted?: boolean; // stopped by `signal`
}

// QuickJS's own stack check fires before the host's stack overflows
// for ordinary recursion; a host RangeError (deep JSON) is caught
// and only costs this script's module.
const MAX_STACK_BYTES = 256 * 1024;
const MB = 1024 * 1024;
const PAGE = 64 * 1024; // a wasm page
const WASM_BASE_PAGES = 256; // the module's own 16 MB minimum
const MAX_TIMERS = 1_000;
const MAX_LOG_BYTES = 10 * 1024 * 1024; // what reaches the tmp file
export const LOGS_CUT = `[console output over ${MAX_LOG_BYTES} bytes dropped]`;

// console and the result's JSON live in the VM: one string crosses.
const BASE_PRELUDE = `
const fmt = (x) => {
  if (typeof x === "string") return x;
  if (x instanceof Error) return String(x);
  try {
    const s = JSON.stringify(x);
    return s === undefined ? String(x) : s;
  } catch {
    return String(x);
  }
};
const log = host.log;
const write = (...a) => log(a.map(fmt).join(" "));
globalThis.console = { log: write, info: write, warn: write, error: write };
`;

export async function runSandbox(
  options: SandboxOptions,
): Promise<SandboxResult> {
  // QuickJS's own count (setMemoryLimit) misses some reallocations
  // in this build; the wasm memory's maximum is the hard cap, with
  // room for the module's static data and stack.
  const maxPages =
    WASM_BASE_PAGES + Math.ceil((options.memoryMb * MB) / PAGE);
  const wasmMemory = new WebAssembly.Memory({
    initial: WASM_BASE_PAGES,
    maximum: maxPages,
  });
  // The module's stderr (a failed assertion while a huge heap is
  // freed) would land in the TUI's screen; the result is unaffected.
  const emscriptenModule: EmscriptenModuleLoaderOptions & {
    printErr?: (text: string) => void;
  } = { printErr: (text) => log.debug(`codemode sandbox: ${text}`) };
  const variant = newVariant(RELEASE_SYNC, {
    wasmMemory,
    emscriptenModule,
  });
  const module = await newQuickJSWASMModule(variant);
  const runtime = module.newRuntime();
  // Within a megabyte of the cap: the heap ran out at some point.
  const memoryFull = () =>
    wasmMemory.buffer.byteLength + MB > maxPages * PAGE;
  return new Script(
    runtime,
    runtime.newContext(),
    options,
    memoryFull,
  ).start();
}

interface Timer {
  timer: NodeJS.Timeout;
  fn: QuickJSHandle;
}

class Script {
  readonly #rt: QuickJSRuntime;
  readonly #vm: QuickJSContext;
  readonly #o: SandboxOptions;
  readonly #memoryFull: () => boolean;
  readonly #logs: string[] = [];
  #logBytes = 0;
  readonly #stop = new AbortController(); // host calls' signal
  readonly #deferreds = new Set<QuickJSDeferredPromise>();
  readonly #timers = new Map<number, Timer>();
  #timerId = 0;
  #main: QuickJSHandle | undefined; // the script's promise
  #calls = 0; // async host calls not yet settled
  #active = 0; // of which running (the rest wait for a slot)
  readonly #queue: (() => void)[] = [];
  #deadline = 0;
  #deadlineTimer: NodeJS.Timeout | undefined;
  #held = 0;
  #heldSince = 0;
  #sliceStart = 0; // when the VM was last entered
  #inVm = 0;
  #interrupted = false; // the deadline stopped the VM
  #crashed = false; // a host exception escaped the wasm
  #done = false;
  #disposed = false;
  #resolve: (result: SandboxResult) => void = () => {};
  readonly #onAbort = () =>
    this.#finish({ error: "aborted", aborted: true });

  constructor(
    rt: QuickJSRuntime,
    vm: QuickJSContext,
    o: SandboxOptions,
    memoryFull: () => boolean,
  ) {
    this.#rt = rt;
    this.#vm = vm;
    this.#o = o;
    this.#memoryFull = memoryFull;
  }

  start(): Promise<SandboxResult> {
    const done = new Promise<SandboxResult>((r) => {
      this.#resolve = r;
    });
    const { signal } = this.#o;
    if (signal?.aborted) {
      this.#onAbort();
      return done;
    }
    signal?.addEventListener("abort", this.#onAbort, { once: true });
    this.#rt.setMemoryLimit(this.#o.memoryMb * MB);
    this.#rt.setMaxStackSize(MAX_STACK_BYTES);
    this.#rt.setInterruptHandler(() => {
      if (this.#expired()) this.#interrupted = true;
      return this.#interrupted;
    });
    this.#deadline = Date.now() + this.#o.timeoutMs;
    this.#arm();
    this.#enter(() => {
      const failed = this.#install();
      if (failed) return this.#finish({ error: failed });
      const wrapped =
        `(async () => {\n${this.#o.script}\n})()` +
        ".then((v) => (v === undefined ? v : JSON.stringify(v)))";
      const result = this.#vm.evalCode(wrapped, "script.js");
      if (result.error) {
        this.#fail(result.error);
      } else {
        this.#main = result.value;
      }
    });
    this.#pump();
    return done;
  }

  // console, setTimeout, the host functions and the preludes; the
  // prelude's error, if any.
  #install(): string | undefined {
    const vm = this.#vm;
    const host = vm.newObject();
    vm.newFunction("log", (text) => {
      this.#log(vm.getString(text));
    }).consume((fn) => vm.setProp(host, "log", fn));
    for (const [name, fn] of Object.entries(this.#o.sync ?? {})) {
      vm.newFunction(name, (input) =>
        vm.newString(fn(vm.getString(input))),
      ).consume((handle) => vm.setProp(host, name, handle));
    }
    for (const [name, fn] of Object.entries(this.#o.async ?? {})) {
      vm.newFunction(name, (input) =>
        this.#callHost(fn, vm.getString(input)),
      ).consume((handle) => vm.setProp(host, name, handle));
    }
    vm.newFunction("setTimeout", (fn, ms) =>
      this.#setTimeout(fn, ms),
    ).consume((handle) => vm.setProp(vm.global, "setTimeout", handle));
    vm.newFunction("clearTimeout", (id) => {
      if (id && vm.typeof(id) === "number") {
        this.#clearTimer(vm.getNumber(id));
      }
    }).consume((handle) => vm.setProp(vm.global, "clearTimeout", handle));
    vm.setProp(vm.global, "__host", host);
    host.dispose();
    const prelude =
      "((host) => {\n" +
      `${BASE_PRELUDE}\n${this.#o.prelude ?? ""}\n` +
      "})(globalThis.__host);\ndelete globalThis.__host;";
    const result = vm.evalCode(prelude, "prelude.js");
    if (result.error) {
      return `the sandbox prelude failed: ${this.#describe(result.error)}`;
    }
    result.value.dispose();
    return undefined;
  }

  #log(text: string): void {
    if (this.#logBytes > MAX_LOG_BYTES) return;
    this.#logBytes += Buffer.byteLength(text) + 1;
    this.#logs.push(this.#logBytes > MAX_LOG_BYTES ? LOGS_CUT : text);
  }

  // Returns the promise at once; the host function starts after
  // the VM yields, and at most `concurrency` run at a time.
  #callHost(fn: AsyncHostFn, input: string): QuickJSHandle {
    const deferred = this.#vm.newPromise();
    this.#deferreds.add(deferred);
    this.#calls++;
    const call: HostCall = {
      signal: this.#stop.signal,
      hold: () => this.#hold(),
    };
    const run = () => {
      Promise.resolve()
        .then(() => fn(input, call))
        .then(
          (output) => this.#settle(deferred, { output }),
          (error: unknown) => this.#settle(deferred, { error }),
        )
        .finally(() => this.#release());
    };
    if (this.#active < this.#o.concurrency) {
      this.#active++;
      run();
    } else {
      this.#queue.push(run);
    }
    return deferred.handle;
  }

  #release(): void {
    this.#active--;
    const next = this.#done ? undefined : this.#queue.shift();
    if (next) {
      this.#active++;
      next();
    }
  }

  #settle(
    deferred: QuickJSDeferredPromise,
    outcome: { output: string } | { error: unknown },
  ): void {
    this.#calls--;
    this.#deferreds.delete(deferred);
    if (this.#done) return; // torn down: never resumes the script
    this.#enter(() => {
      const vm = this.#vm;
      if ("output" in outcome) {
        vm.newString(outcome.output).consume((h) => deferred.resolve(h));
      } else {
        const message = errorMessage(outcome.error);
        vm.newError(message).consume((h) => deferred.reject(h));
      }
      deferred.dispose();
    });
    this.#pump();
  }

  #setTimeout(
    fn: QuickJSHandle | undefined,
    ms: QuickJSHandle | undefined,
  ): QuickJSHandle {
    const vm = this.#vm;
    if (!fn || vm.typeof(fn) !== "function") {
      throw new TypeError("setTimeout needs a function");
    }
    if (this.#timers.size >= MAX_TIMERS) {
      throw new Error(`more than ${MAX_TIMERS} pending setTimeout calls`);
    }
    const delay = ms && vm.typeof(ms) === "number" ? vm.getNumber(ms) : 0;
    const id = ++this.#timerId;
    const timer = setTimeout(() => this.#fire(id), Math.max(0, delay));
    this.#timers.set(id, { timer, fn: fn.dup() });
    return vm.newNumber(id);
  }

  #clearTimer(id: number): void {
    const timer = this.#timers.get(id);
    if (!timer) return;
    this.#timers.delete(id);
    clearTimeout(timer.timer);
    timer.fn.dispose();
  }

  // An error thrown by a timer callback is logged, as a browser
  // would, and does not end the script.
  #fire(id: number): void {
    const timer = this.#timers.get(id);
    if (!timer || this.#done) return;
    this.#timers.delete(id);
    this.#enter(() => {
      const vm = this.#vm;
      const result = vm.callFunction(timer.fn, vm.undefined);
      timer.fn.dispose();
      if (result.error) {
        this.#log(`Uncaught ${this.#describe(result.error)}`);
      } else {
        result.value.dispose();
      }
    });
    this.#pump();
  }

  // Runs what a settle or a timer unblocked, then checks the script.
  #pump(): void {
    if (this.#done) return;
    this.#enter(() => {
      const jobs = this.#rt.executePendingJobs();
      if (jobs.error) this.#fail(jobs.error);
    });
    this.#check();
  }

  #check(): void {
    if (this.#done) return;
    if (this.#interrupted) {
      this.#finish({ error: this.#timedOut() });
      return;
    }
    const main = this.#main;
    if (!main) return;
    this.#enter(() => {
      const state = this.#vm.getPromiseState(main);
      if (state.type === "fulfilled") {
        const value = state.value.consume((h) => this.#vm.dump(h));
        const json = typeof value === "string" ? value : undefined;
        return this.#finish({ value: json });
      }
      if (state.type === "rejected") return this.#fail(state.error);
      const idle =
        this.#calls === 0 &&
        this.#timers.size === 0 &&
        !this.#rt.hasPendingJob();
      if (idle) {
        this.#finish({
          error: "the script awaits a promise that can never settle",
        });
      }
    });
  }

  #fail(error: QuickJSHandle): void {
    let text = this.#describe(error);
    // Out of memory with no room left for the error object, QuickJS
    // throws null; a `throw null` leaves the heap far from the cap.
    if (text === "null" && this.#memoryFull()) {
      text = "InternalError: out of memory";
    }
    const memory = text.includes("out of memory")
      ? ` (codemode.memoryMb = ${this.#o.memoryMb})`
      : "";
    this.#finish({
      error: this.#interrupted ? this.#timedOut() : `${text}${memory}`,
    });
  }

  // `Name: message` of a guest error; disposes the handle.
  #describe(handle: QuickJSHandle): string {
    const dumped: unknown = handle.consume((h) => this.#vm.dump(h));
    if (dumped && typeof dumped === "object" && "message" in dumped) {
      const { name, message } = dumped as {
        name?: unknown;
        message: unknown;
      };
      return `${String(name ?? "Error")}: ${String(message)}`;
    }
    return String(dumped);
  }

  #timedOut(): string {
    return (
      `the script timed out after ${this.#o.timeoutMs} ms ` +
      "(codemode.timeoutMs)"
    );
  }

  // Every entry into the VM: times the slice, turns a host exception
  // out of the wasm (a host stack overflow, an abort) into the
  // script's error, and tears down once the VM has returned.
  #enter<T>(fn: () => T): T | undefined {
    if (this.#crashed) return undefined;
    this.#sliceStart = Date.now();
    this.#inVm++;
    try {
      return fn();
    } catch (err) {
      this.#crashed = true;
      this.#finish({
        error: `the sandbox crashed: ${errorMessage(err)}`,
      });
      return undefined;
    } finally {
      this.#inVm--;
      if (this.#done && this.#inVm === 0) this.#dispose();
    }
  }

  // One synchronous slice may never outlast timeoutMs (it would
  // block the event loop the prompt needs); otherwise the deadline,
  // which stands still while a host call holds it.
  #expired(): boolean {
    const now = Date.now();
    if (now - this.#sliceStart > this.#o.timeoutMs) return true;
    return this.#held === 0 && now > this.#deadline;
  }

  #hold(): () => void {
    if (this.#held++ === 0) {
      this.#heldSince = Date.now();
      clearTimeout(this.#deadlineTimer);
    }
    let released = false;
    return () => {
      if (released) return;
      released = true;
      if (--this.#held > 0 || this.#done) return;
      this.#deadline += Date.now() - this.#heldSince;
      this.#arm();
    };
  }

  // The host side of the deadline: a script idle on a host promise
  // runs no code, so the interrupt handler alone would never fire.
  #arm(): void {
    clearTimeout(this.#deadlineTimer);
    const left = Math.max(0, this.#deadline - Date.now());
    this.#deadlineTimer = setTimeout(() => {
      this.#interrupted = true;
      this.#finish({ error: this.#timedOut() });
    }, left);
  }

  #finish(result: Omit<SandboxResult, "logs">): void {
    if (this.#done) return;
    this.#done = true;
    this.#o.signal?.removeEventListener("abort", this.#onAbort);
    clearTimeout(this.#deadlineTimer);
    this.#stop.abort();
    this.#resolve({ logs: this.#logs, ...result });
    if (this.#inVm === 0) this.#dispose();
  }

  // Frees every handle, then the context and the runtime. After a
  // crash the module is not touched again: it is garbage.
  #dispose(): void {
    if (this.#disposed) return;
    this.#disposed = true;
    for (const { timer } of this.#timers.values()) clearTimeout(timer);
    this.#queue.length = 0;
    if (this.#crashed) return;
    try {
      for (const { fn } of this.#timers.values()) fn.dispose();
      for (const deferred of this.#deferreds) deferred.dispose();
      this.#main?.dispose();
      this.#vm.dispose();
      this.#rt.dispose();
    } catch (err) {
      log.debug(`codemode sandbox dispose failed: ${errorMessage(err)}`);
    }
    this.#timers.clear();
    this.#deferreds.clear();
  }
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
