import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { zodToJsonSchema } from "zod-to-json-schema";
import {
  CLASSIFIER_TIMEOUT_MS,
  DANGER_CHECK_SYSTEM,
  DangerCheck,
  type DangerRequest,
  dangerClassifier,
  dangerInput,
} from "../../src/permissions/decision-model.ts";
import { FakeProvider } from "../../src/provider/fake.ts";
import type { ToolCall } from "../../src/types.ts";

const SPEC = fileURLToPath(
  new URL("../../../../docs/notes/tree-agent-harness-spec.md", import.meta.url),
);

// The §11.1 system prompt as the spec writes it, in a code comment.
function specSystemPrompt(): string {
  const lines = readFileSync(SPEC, "utf8").split("\n");
  const start = lines.findIndex((l) => l.startsWith('// system: "'));
  const block: string[] = [];
  for (const line of lines.slice(start)) {
    block.push(line.replace(/^\/\/\s*(system: ")?/, "").trim());
    if (line.trimEnd().endsWith('"')) break;
  }
  return block.join(" ").replace(/"$/, "");
}

const rm: ToolCall = { id: "c1", name: "bash", arguments: { command: "rm -rf ~/.ssh" } };
const req = (call: ToolCall, recentUserMessages: string[] = ["tidy up"]): DangerRequest => ({
  call,
  cwd: "/work/repo",
  recentUserMessages,
});

const dangerous = { dangerous: true, risk: "high", reason: "deletes credentials outside cwd" };

describe("dangerClassifier", () => {
  it("sends the §11.1 system prompt verbatim on the decision model", async () => {
    const fake = new FakeProvider({ answers: [dangerous] });

    const verdict = await dangerClassifier(fake, { model: "test/haiku", cache: new Map() })(
      req(rm),
    );

    expect(verdict).toEqual(dangerous);
    const [sent] = fake.structuredRequests;
    expect(specSystemPrompt()).toMatch(/^You are a safety classifier .* dangerous: true\.$/);
    expect(sent?.system).toBe(specSystemPrompt());
    expect(DANGER_CHECK_SYSTEM).toBe(specSystemPrompt());
    expect(sent).toMatchObject({ model: "test/haiku", name: "danger_check" });
  });

  it("sends the §11.1 DangerCheck schema, with the 200-character limit stated", async () => {
    const fake = new FakeProvider({ answers: [dangerous] });
    await dangerClassifier(fake, { cache: new Map() })(req(rm));
    const sent = fake.structuredRequests[0]?.schema ?? DangerCheck;

    expect(zodToJsonSchema(sent, { target: "openAi" })).toMatchObject({
      properties: {
        dangerous: { type: "boolean" },
        risk: { type: "string", enum: ["low", "medium", "high"] },
        reason: {
          type: "string",
          maxLength: 200,
          description: "One sentence, at most 200 characters.",
        },
      },
      required: ["dangerous", "risk", "reason"],
    });
  });

  it("truncates an over-long reason to 200 characters instead of failing", async () => {
    const long = `${"x".repeat(250)}`;
    const fake = new FakeProvider({ answers: [{ ...dangerous, reason: long }] });

    const verdict = await dangerClassifier(fake, { cache: new Map() })(req(rm));

    expect(verdict.reason).toHaveLength(200);
    expect(verdict.reason).toBe(`${"x".repeat(199)}…`);
    expect(fake.structuredRequests).toHaveLength(1);
  });

  it("cuts an over-long emoji reason without splitting a surrogate pair, and it still parses", async () => {
    const long = "\u{1F600}".repeat(250); // 500 UTF-16 units
    const fake = new FakeProvider({ answers: [{ ...dangerous, reason: long }] });

    const verdict = await dangerClassifier(fake, { cache: new Map() })(req(rm));

    expect(verdict.reason.length).toBeLessThanOrEqual(200);
    expect(verdict.reason).toBe(`${"\u{1F600}".repeat(99)}…`);
  });

  it("cancels the request when the caller's signal aborts mid-flight", async () => {
    let seen: AbortSignal | undefined;
    const fake = new FakeProvider({
      answers: [
        (_req: unknown, signal?: AbortSignal) => {
          seen = signal;
          return new Promise(() => {}); // never answers
        },
      ],
    });
    const run = new AbortController();

    const pending = dangerClassifier(fake, { cache: new Map() })(req(rm), run.signal);
    run.abort(new Error("user aborted"));

    await expect(pending).rejects.toThrow("user aborted");
    expect(seen?.aborted).toBe(true);
  });

  it("classifies the same (tool, input) once per cache, whatever the conversation", async () => {
    const fake = new FakeProvider({ answers: [dangerous, { ...dangerous, dangerous: false }] });
    const classify = dangerClassifier(fake, { cache: new Map() });

    await classify(req(rm, ["one"]));
    const again = await classify(req({ ...rm, id: "c2" }, ["two"]));
    const other = await classify(req({ ...rm, arguments: { command: "ls" } }));

    expect(again).toEqual(dangerous);
    expect(other.dangerous).toBe(false);
    expect(fake.structuredRequests).toHaveLength(2);
  });

  it("rejects on a provider error and on a parse failure", async () => {
    const fake = new FakeProvider({ answers: [new Error("503"), { dangerous: "maybe" }] });
    const classify = dangerClassifier(fake, { cache: new Map() });

    await expect(classify(req(rm))).rejects.toThrow("503");
    await expect(classify(req(rm))).rejects.toThrow();
  });
});

describe("dangerClassifier timeout", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("gives up after 10 s", async () => {
    vi.useFakeTimers();
    const fake = new FakeProvider({ answers: [() => new Promise(() => {})] });
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
  });
});

describe("dangerInput", () => {
  it("names the tool, its primary argument, the input JSON, the cwd and the last 3 user messages", () => {
    const write: ToolCall = {
      id: "w",
      name: "write",
      arguments: { path: "/work/repo/src/a.ts", content: "x" },
    };

    const text = dangerInput(req(write, ["one", "two", "three", "four"]));

    expect(text.split("\n")).toEqual([
      "Tool: write",
      "Primary argument: src/a.ts",
      'Input (JSON): {"path":"/work/repo/src/a.ts","content":"x"}',
      "cwd: /work/repo",
      "The user's last messages, oldest first:",
      "1. two",
      "2. three",
      "3. four",
      "",
      "Answer dangerous, risk and reason (one sentence, at most 200 characters).",
    ]);
  });

  it("cuts the input JSON at 2 KB", () => {
    const big: ToolCall = {
      id: "b",
      name: "write",
      arguments: { path: "a", content: "y".repeat(5000) },
    };

    const line = dangerInput(req(big)).split("\n")[2] ?? "";

    expect(Buffer.byteLength(line.replace("Input (JSON): ", ""))).toBe(2048);
  });
});
