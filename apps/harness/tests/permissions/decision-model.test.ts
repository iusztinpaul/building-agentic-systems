import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CLASSIFIER_TIMEOUT_MS,
  DANGER_QUESTIONS,
  type DangerCheck,
  type DangerRequest,
  dangerClassifier,
  dangerState,
} from "../../src/permissions/decision-model.ts";
import { FakeProvider } from "../../src/provider/fake.ts";
import type { ToolCall } from "../../src/types.ts";
import { verdict } from "../helpers/decisions.ts";

const SPEC = fileURLToPath(
  new URL("../../../../docs/notes/tree-agent-harness-spec.md", import.meta.url),
);

// The §11.1 question set as the spec writes it: its fenced json block.
function specQuestions(): unknown {
  const text = readFileSync(SPEC, "utf8");
  const section = text.slice(text.indexOf("### 11.1 "));
  const block = /```json\n([\s\S]*?)```/.exec(section)?.[1];
  if (!block) throw new Error("no json block in spec §11.1");
  return JSON.parse(block);
}

const rm: ToolCall = { id: "c1", name: "bash", arguments: { command: "rm -rf ~/.ssh" } };
const req = (call: ToolCall, recentUserMessages: string[] = ["tidy up"]): DangerRequest => ({
  call,
  cwd: "/work/repo",
  recentUserMessages,
});

describe("dangerClassifier", () => {
  it("sends DANGER_QUESTIONS — the spec §11.1 json block — and the state on the decision model", async () => {
    const fake = new FakeProvider({ decisions: [verdict(0.95, "high", { confidence: 0.98 })] });

    const check = await dangerClassifier(fake, { model: "test/decider", cache: new Map() })(
      req(rm, ["one", "two", "three", "four"]),
    );

    expect(DANGER_QUESTIONS).toEqual(specQuestions());
    expect(fake.decisionRequests).toEqual([
      {
        model: "test/decider",
        questions: DANGER_QUESTIONS,
        state: {
          tool: "bash",
          primary_argument: "rm -rf ~/.ssh",
          input: '{"command":"rm -rf ~/.ssh"}',
          cwd: "/work/repo",
          recent_user_messages: ["two", "three", "four"],
        },
      },
    ]);
    expect(check).toEqual({
      dangerous: true,
      risk: "high",
      reason: "decision model: p(dangerous)=0.95, risk high (0.98)",
    } satisfies DangerCheck);
  });

  it("defaults to the settings' decisionModel", async () => {
    const fake = new FakeProvider({ decisions: [verdict(0.02)] });

    await dangerClassifier(fake, { cache: new Map() })(req(rm));

    expect(fake.decisionRequests[0]?.model).toBe("typesafe/jev-1.14");
  });

  it.each([
    [0.29, undefined, false],
    [0.3, undefined, true],
    [0.45, 0.5, false],
    [0.5, 0.5, true],
  ])("p(dangerous) %d with threshold %s → dangerous %s", async (p, threshold, dangerous) => {
    const fake = new FakeProvider({ decisions: [verdict(p, "medium", { confidence: 0.6 })] });

    const check = await dangerClassifier(fake, { threshold, cache: new Map() })(req(rm));

    expect(check.dangerous).toBe(dangerous);
    expect(check.reason).toBe(`decision model: p(dangerous)=${p.toFixed(2)}, risk medium (0.60)`);
  });

  it("parses the recorded jev-1.14 answers into the generated reason", async () => {
    const recorded = (name: string) =>
      JSON.parse(
        readFileSync(new URL(`../fixtures/openrouter/${name}`, import.meta.url), "utf8"),
      ) as unknown;
    const fake = new FakeProvider({
      decisions: [recorded("decisions-rm-rf.json"), recorded("decisions-ls.json")],
    });
    const classify = dangerClassifier(fake, { cache: new Map() });

    const rmrf = await classify(req({ ...rm, arguments: { command: "rm -rf ~/Downloads/*" } }));
    const ls = await classify(req({ ...rm, arguments: { command: "ls -la" } }));

    expect(rmrf).toEqual({
      dangerous: true,
      risk: "high",
      reason: "decision model: p(dangerous)=0.87, risk high (0.97)",
    });
    expect(ls).toEqual({
      dangerous: false,
      risk: "low",
      reason: "decision model: p(dangerous)=0.02, risk low (1.00)",
    });
  });

  it("reports each answered request's usage.cost through onCost, a cache hit costs nothing", async () => {
    const costs: number[] = [];
    const fake = new FakeProvider({ decisions: [verdict(0.02, "low", { cost: 0.000027888 })] });
    const classify = dangerClassifier(fake, {
      cache: new Map(),
      onCost: (usd) => costs.push(usd),
    });

    await classify(req(rm));
    await classify(req({ ...rm, id: "c2" }));

    expect(costs).toEqual([0.000027888]);
  });

  it("classifies the same (tool, input) once per cache, whatever the conversation", async () => {
    const fake = new FakeProvider({ decisions: [verdict(0.95), verdict(0.02)] });
    const classify = dangerClassifier(fake, { cache: new Map() });

    const first = await classify(req(rm, ["one"]));
    const again = await classify(req({ ...rm, id: "c2" }, ["two"]));
    const other = await classify(req({ ...rm, arguments: { command: "ls" } }));

    expect(again).toEqual(first);
    expect(other.dangerous).toBe(false);
    expect(fake.decisionRequests).toHaveLength(2);
  });

  it("rejects on a provider error, a missing risk answer and an unknown risk level", async () => {
    const { risk: _, ...noRisk } = verdict(0.1).answers;
    const fake = new FakeProvider({
      decisions: [
        new Error("503"),
        { ...verdict(0.1), answers: noRisk },
        verdict(0.1, "extreme" as "high"),
      ],
    });
    const classify = dangerClassifier(fake, { cache: new Map() });

    await expect(classify(req(rm))).rejects.toThrow("503");
    await expect(classify(req(rm))).rejects.toThrow('decisions: no choice answer for "risk"');
    await expect(classify(req(rm))).rejects.toThrow(/danger_check: unexpected answers/);
    expect(fake.decisionRequests).toHaveLength(3); // nothing was cached
  });

  it("cancels the request when the caller's signal aborts mid-flight, and caches nothing", async () => {
    let seen: AbortSignal | undefined;
    const fake = new FakeProvider({
      decisions: [
        (_req: unknown, signal?: AbortSignal) => {
          seen = signal;
          return new Promise(() => {}); // never answers
        },
        verdict(0.02),
      ],
    });
    const cache = new Map<string, DangerCheck>();
    const classify = dangerClassifier(fake, { cache });
    const run = new AbortController();

    const pending = classify(req(rm), run.signal);
    run.abort(new Error("user aborted"));

    await expect(pending).rejects.toThrow("user aborted");
    expect(seen?.aborted).toBe(true);
    expect(cache.size).toBe(0);
    await expect(classify(req(rm))).resolves.toMatchObject({ dangerous: false });
    expect(fake.decisionRequests).toHaveLength(2);
  });
});

describe("dangerClassifier timeout", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("gives up after 10 s and cancels the request", async () => {
    vi.useFakeTimers();
    let seen: AbortSignal | undefined;
    const fake = new FakeProvider({
      decisions: [
        (_req: unknown, signal?: AbortSignal) => {
          seen = signal;
          return new Promise(() => {});
        },
      ],
    });
    let error: Error | undefined;
    const pending = dangerClassifier(fake, { cache: new Map() })(req(rm)).catch((err) => {
      error = err;
    });

    await vi.advanceTimersByTimeAsync(9_999);
    expect(error).toBeUndefined();
    await vi.advanceTimersByTimeAsync(1);
    await pending;

    expect(CLASSIFIER_TIMEOUT_MS).toBe(10_000);
    expect(error?.message).toBe("danger_check: no answer within 10000 ms");
    expect(seen?.aborted).toBe(true);
  });
});

describe("dangerState", () => {
  it("names the tool, its primary argument, the input JSON, the cwd and the last 3 user messages", () => {
    const write: ToolCall = {
      id: "w",
      name: "write",
      arguments: { path: "/work/repo/src/a.ts", content: "x" },
    };

    expect(dangerState(req(write, ["one", "two", "three", "four"]))).toEqual({
      tool: "write",
      primary_argument: "src/a.ts",
      input: '{"path":"/work/repo/src/a.ts","content":"x"}',
      cwd: "/work/repo",
      recent_user_messages: ["two", "three", "four"],
    });
  });

  it("cuts the input JSON and each message at 2 KB", () => {
    const big: ToolCall = {
      id: "b",
      name: "write",
      arguments: { path: "a", content: "y".repeat(5000) },
    };

    const state = dangerState(req(big, ["z".repeat(5000)]));

    expect(Buffer.byteLength(String(state.input))).toBe(2048);
    const [message = ""] = state.recent_user_messages as string[];
    expect(Buffer.byteLength(message)).toBe(2048);
  });
});
