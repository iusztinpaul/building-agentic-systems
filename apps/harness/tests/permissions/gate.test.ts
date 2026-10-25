import { describe, expect, it } from "vitest";
import { z } from "zod";
import type { Classifier, DangerCheck } from "../../src/permissions/decision-model.ts";
import {
  type GateDeps,
  HEADLESS_ASK,
  PromptQueue,
  resolve,
  USER_DENIED,
} from "../../src/permissions/gate.ts";
import { builtinRegistry } from "../../src/tools/builtins.ts";
import type { ToolDefinition } from "../../src/tools/registry.ts";
import type { PermissionMode, ToolCall } from "../../src/types.ts";

type Answer = "once" | "session" | "deny";

const tools = builtinRegistry({ tmpDir: "/tmp/x" });
const tool = (name: string): ToolDefinition => {
  const t = tools.get(name);
  if (!t) throw new Error(name);
  return t;
};
const mcp = (isReadOnly: boolean): ToolDefinition => ({
  name: "mcp__fs__delete",
  description: "d",
  promptSnippet: "s",
  parameters: z.object({ path: z.string() }),
  exposure: "direct",
  isReadOnly,
  execute: async () => ({ content: "" }),
});

const calls: Record<string, ToolCall> = {
  read: { id: "r", name: "read", arguments: { path: "/work/a.md" } },
  bash: { id: "b", name: "bash", arguments: { command: "pnpm test" } },
  edit: { id: "e", name: "edit", arguments: { path: "/work/a.md", oldText: "a", newText: "b" } },
  write: { id: "w", name: "write", arguments: { path: "/work/src/x.ts", content: "x" } },
  mcp__fs__delete: { id: "m", name: "mcp__fs__delete", arguments: { path: "/" } },
};

// A recording prompter answering from a script, and a scripted
// classifier (a verdict, or an Error to reject with).
function deps(
  mode: PermissionMode,
  o: { answers?: Answer[]; verdicts?: (DangerCheck | Error)[]; headless?: boolean } = {},
) {
  const asked: { call: ToolCall; summary: string; reason?: string }[] = [];
  const classified: string[] = [];
  const answers = [...(o.answers ?? [])];
  const verdicts = [...(o.verdicts ?? [])];
  const classifier: Classifier = async (req) => {
    classified.push(req.call.name);
    const v = verdicts.shift();
    if (!v) throw new Error("no verdict scripted");
    if (v instanceof Error) throw v;
    return v;
  };
  const d: GateDeps = {
    mode,
    prompter: o.headless
      ? null
      : {
          ask: async (call, summary, reason) => {
            asked.push({ call, summary, ...(reason && { reason }) });
            return answers.shift() ?? "deny";
          },
        },
    classifier,
    sessionAllow: new Set(),
  };
  return { d, asked, classified };
}

const check = (name: string, d: GateDeps, recentUserMessages: string[] = []) =>
  resolve(
    {
      call: calls[name] as ToolCall,
      tool: name.startsWith("mcp__") ? mcp(false) : tool(name),
      cwd: "/work",
      recentUserMessages,
    },
    d,
  );

const safe: DangerCheck = { dangerous: false, risk: "low", reason: "cwd-scoped" };
const rcEdit: DangerCheck = { dangerous: true, risk: "high", reason: "edits a shell rc file" };

describe("resolve", () => {
  it.each(["ask", "auto"] as const)(
    "never prompts or classifies a read-only tool in %s",
    async (mode) => {
      const t = deps(mode, { headless: true });

      const read = await check("read", t.d);
      const ro = await resolve(
        {
          call: calls.mcp__fs__delete as ToolCall,
          tool: mcp(true),
          cwd: "/w",
          recentUserMessages: [],
        },
        t.d,
      );

      expect(read).toEqual({ decision: "allow", source: "readOnly" });
      expect(ro).toEqual({ decision: "allow", source: "readOnly" });
      expect(t.classified).toEqual([]);
    },
  );

  it.each(["bash", "edit", "write", "mcp__fs__delete"])("prompts on %s in ask", async (name) => {
    const t = deps("ask", { answers: ["once"] });

    const r = await check(name, t.d);

    expect(r).toEqual({ decision: "allow", source: "user" });
    expect(t.asked.map((a) => a.call.name)).toEqual([name]);
    expect(t.classified).toEqual([]);
  });

  it("passes the summary (primary argument) to the prompter", async () => {
    const t = deps("ask", { answers: ["once", "once"] });

    await check("bash", t.d);
    await check("write", t.d);

    expect(t.asked.map((a) => a.summary)).toEqual(["pnpm test", "src/x.ts"]);
  });

  it("asks again after `once`, but a `session` answer skips later prompts for that tool only", async () => {
    const t = deps("ask", { answers: ["once", "session", "once"] });

    await check("bash", t.d);
    await check("bash", t.d);
    const third = await check("bash", t.d);
    await check("write", t.d);

    expect(third).toEqual({ decision: "allow", source: "session" });
    expect(t.asked.map((a) => a.call.name)).toEqual(["bash", "bash", "write"]);
    expect([...t.d.sessionAllow]).toEqual(["bash"]);
  });

  it("denies with source user on `n`", async () => {
    const t = deps("ask", { answers: ["deny"] });

    expect(await check("edit", t.d)).toEqual({
      decision: "deny",
      source: "user",
      reason: USER_DENIED,
    });
  });

  it("denies headless in ask with source headless and no classifier call", async () => {
    const t = deps("ask", { headless: true });

    expect(await check("write", t.d)).toEqual({
      decision: "deny",
      source: "headless",
      reason: HEADLESS_ASK,
    });
    expect(t.classified).toEqual([]);
  });

  it("allows dangerous: false in auto without prompting, source classifier", async () => {
    const t = deps("auto", { verdicts: [safe] });

    expect(await check("bash", t.d)).toEqual({
      decision: "allow",
      source: "classifier",
      reason: "cwd-scoped",
    });
    expect(t.asked).toEqual([]);
  });

  it("prompts with the classifier's reason on dangerous: true; the user decides", async () => {
    const t = deps("auto", { verdicts: [rcEdit, rcEdit], answers: ["deny", "once"] });

    const denied = await check("edit", t.d);
    const allowed = await check("edit", t.d);

    expect(t.asked.map((a) => a.reason)).toEqual([
      "edits a shell rc file",
      "edits a shell rc file",
    ]);
    expect(denied).toEqual({ decision: "deny", source: "user", reason: USER_DENIED });
    expect(allowed).toEqual({ decision: "allow", source: "user", reason: "edits a shell rc file" });
  });

  it("denies a dangerous call headless with source headless and a `dangerous:` reason", async () => {
    const t = deps("auto", { headless: true, verdicts: [rcEdit] });

    expect(await check("edit", t.d)).toEqual({
      decision: "deny",
      source: "headless",
      reason: "dangerous: edits a shell rc file",
    });
  });

  it("fails closed: a classifier error or timeout prompts in the TUI and denies headless", async () => {
    const tui = deps("auto", { verdicts: [new Error("danger_check: no answer within 10000 ms")] });
    const headless = deps("auto", { headless: true, verdicts: [new Error("503")] });

    const prompted = await check("bash", tui.d);
    const denied = await check("bash", headless.d);

    expect(tui.asked).toHaveLength(1);
    expect(tui.asked[0]?.reason).toBe(
      "the safety classifier failed (danger_check: no answer within 10000 ms)",
    );
    expect(prompted.decision).toBe("deny"); // the scripted prompter has no answer left
    expect(denied).toEqual({
      decision: "deny",
      source: "headless",
      reason: "dangerous: the safety classifier failed (503)",
    });
  });

  it("collapses a classifier failure to one short line: a zod error's first issue", async () => {
    const zod = z.object({ dangerous: z.boolean(), risk: z.string() }).safeParse({});
    const headless = deps("auto", {
      headless: true,
      verdicts: [zod.success ? new Error() : zod.error, new Error(`a\n  b ${"x".repeat(300)}`)],
    });

    const first = await check("bash", headless.d);
    const second = await check("bash", headless.d);

    expect(first.reason).toBe("dangerous: the safety classifier failed (dangerous: Required)");
    expect(second.reason).not.toContain("\n");
    expect(second.reason).toMatch(/^dangerous: the safety classifier failed \(a b x+…$/);
    expect(second.reason?.length).toBe("dangerous: ".length + 200);
  });

  it("rejects without prompting when the run is aborted during the classifier call", async () => {
    const t = deps("auto", { answers: ["once"] });
    const run = new AbortController();
    t.d.signal = run.signal;
    t.d.classifier = (_req, signal) =>
      new Promise((_, reject) => {
        signal?.addEventListener("abort", () => reject(signal.reason));
      });

    const pending = check("write", t.d);
    run.abort(new Error("user aborted"));

    await expect(pending).rejects.toThrow("user aborted");
    expect(t.asked).toEqual([]);
  });

  it("skips the classifier for a tool allowed for the session in auto", async () => {
    const t = deps("auto", { verdicts: [rcEdit], answers: ["session"] });

    await check("bash", t.d);
    const second = await check("bash", t.d);

    expect(second).toEqual({ decision: "allow", source: "session" });
    expect(t.classified).toEqual(["bash"]);
  });

  it("gives the classifier the call, the cwd and the recent user messages", async () => {
    let seen: unknown;
    const t = deps("auto");
    t.d.classifier = async (req) => {
      seen = req;
      return safe;
    };

    await check("write", t.d, ["make x.ts"]);

    expect(seen).toMatchObject({
      call: calls.write,
      cwd: "/work",
      recentUserMessages: ["make x.ts"],
    });
  });
});

describe("resolve with a PromptQueue (parallel codemode calls)", () => {
  it("shows one dialog at a time, in arrival order, while classifier calls overlap", async () => {
    let open = 0;
    let peak = 0;
    let classifying = 0;
    let classifyPeak = 0;
    const order: string[] = [];
    const d: GateDeps = {
      mode: "auto",
      prompter: {
        ask: async (call) => {
          peak = Math.max(peak, ++open);
          order.push(call.id);
          await new Promise((r) => setTimeout(r, 10));
          open--;
          return "once";
        },
      },
      classifier: async () => {
        classifyPeak = Math.max(classifyPeak, ++classifying);
        await new Promise((r) => setTimeout(r, 10));
        classifying--;
        return rcEdit;
      },
      sessionAllow: new Set(),
      prompts: new PromptQueue(),
    };
    const req = (id: string) => ({
      call: { ...(calls.bash as ToolCall), id },
      tool: tool("bash"),
      cwd: "/work",
      recentUserMessages: [],
    });

    const answers = await Promise.all(["1", "2", "3"].map((id) => resolve(req(id), d)));

    expect(answers.map((a) => a.decision)).toEqual(["allow", "allow", "allow"]);
    expect(peak).toBe(1);
    expect(classifyPeak).toBe(3);
    expect(order).toEqual(["1", "2", "3"]);
  });

  it("a queued prompt of an aborted run is never shown", async () => {
    const controller = new AbortController();
    const asked: string[] = [];
    const d: GateDeps = {
      mode: "ask",
      prompter: {
        ask: async (call) => {
          asked.push(call.id);
          controller.abort(); // e.g. Esc while the first dialog is open
          return "once";
        },
      },
      classifier: async () => safe,
      sessionAllow: new Set(),
      signal: controller.signal,
      prompts: new PromptQueue(),
    };
    const req = (id: string) => ({
      call: { ...(calls.write as ToolCall), id },
      tool: tool("write"),
      cwd: "/work",
      recentUserMessages: [],
    });

    const [first, second] = await Promise.allSettled([resolve(req("a"), d), resolve(req("b"), d)]);

    expect(first).toMatchObject({ status: "fulfilled" });
    expect(second).toMatchObject({ status: "rejected" });
    expect(asked).toEqual(["a"]);
  });
});
