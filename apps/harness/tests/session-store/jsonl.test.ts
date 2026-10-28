import { readdirSync, readFileSync, utimesSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { log } from "../../src/log.ts";
import {
  ancestry,
  cwdSlug,
  type Entry,
  type EntryBody,
  type Envelope,
  JsonlSessionStore,
  SessionNotFound,
  SessionStore,
  summaryMessage,
} from "../../src/session-store/jsonl.ts";
import type { Message } from "../../src/types.ts";
import { useTmpDir } from "../helpers/tmp.ts";

const root = useTmpDir("store");
const ISO_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/;

const user = (content: string): Message => ({ role: "user", content });
const answer = (content: string, inputTokens = 10): Message => ({
  role: "assistant",
  content,
  toolCalls: [],
  stopReason: "stop",
  usage: { inputTokens, outputTokens: 5 },
});

function lines(file: string): Entry[] {
  return readFileSync(file, "utf8")
    .trimEnd()
    .split("\n")
    .map((l) => JSON.parse(l) as Entry);
}

function onlyFile(dir: string): string {
  const slugDir = readdirSync(dir).map((d) => join(dir, d));
  expect(slugDir).toHaveLength(1);
  const files = readdirSync(slugDir[0] ?? "");
  expect(files).toHaveLength(1);
  return join(slugDir[0] ?? "", files[0] ?? "");
}

// A hand-written session file, as later tasks will write them.
function craft(
  dir: string,
  cwd: string,
  entries: (Omit<Envelope, "timestamp"> & EntryBody)[],
): string {
  const id = "crafted-0001";
  const file = join(dir, cwdSlug(cwd), `2026-10-24T12-00-00-000Z_${id}.jsonl`);
  const body = entries.map((e) => JSON.stringify({ timestamp: "2026-10-24T12:00:00.000Z", ...e }));
  writeFileSync(file, `${body.join("\n")}\n`, { flag: "w" });
  return id;
}

describe("JsonlSessionStore", () => {
  it("writes the session line first, chains every parentId to the line before, in UTC ISO", () => {
    const dir = join(root(), "sessions");
    const store = SessionStore.jsonl(dir);

    const id = store.create("/work/repo", "a/model");
    store.append(id, { type: "message", message: user("hi") });
    store.append(id, { type: "message", message: answer("hello") });
    store.append(id, { type: "model_change", model: "b/model" });

    const file = onlyFile(dir);
    const entries = lines(file);
    expect(entries[0]).toMatchObject({
      type: "session",
      version: 1,
      cwd: "/work/repo",
      model: "a/model",
      parentId: null,
    });
    for (let i = 1; i < entries.length; i++) {
      expect(entries[i]?.parentId).toBe(entries[i - 1]?.id);
    }
    for (const e of entries) expect(e.timestamp).toMatch(ISO_UTC);
    expect(new Set(entries.map((e) => e.id)).size).toBe(entries.length);
    expect(file.slice(dir.length)).toMatch(
      new RegExp(
        `^/--work-repo--/\\d{4}-\\d{2}-\\d{2}T\\d{2}-\\d{2}-\\d{2}-\\d{3}Z_${id}\\.jsonl$`,
      ),
    );
  });

  it("names the directory after the cwd the way pi does", () => {
    expect(cwdSlug("/Users/me/repo")).toBe("--Users-me-repo--");
  });

  it("stamps entries with the injected clock in UTC", () => {
    const dir = join(root(), "sessions");
    const now = () => new Date("2026-10-24T23:30:00-05:00");
    const store = SessionStore.jsonl(dir, now);

    store.create("/w", "m");

    const file = onlyFile(dir);
    expect(lines(file)[0]?.timestamp).toBe("2026-10-25T04:30:00.000Z");
    expect(file).toContain("/2026-10-25T04-30-00-000Z_");
  });

  it("rebuilds 6 messages in order, the activated set and the last model_change", () => {
    const dir = join(root(), "sessions");
    const store = SessionStore.jsonl(dir);
    const id = store.create("/w", "a/model");
    const messages = [user("1"), answer("2"), user("3"), answer("4"), user("5"), answer("6")];
    for (const [i, message] of messages.entries()) {
      store.append(id, { type: "message", message });
      if (i === 1) store.append(id, { type: "tool_activation", names: ["mcp__x__lookup"] });
      if (i === 3) store.append(id, { type: "model_change", model: "b/model" });
    }

    const restored = new JsonlSessionStore(dir).load(id);

    expect(restored.messages).toEqual(messages);
    expect(restored.activated).toEqual(["mcp__x__lookup"]);
    expect(restored.model).toBe("b/model");
    expect(restored.cwd).toBe("/w");
    expect(restored.spend.map((s) => s.model)).toEqual(["a/model", "a/model", "b/model"]);
  });

  it("restores llm_call usage as spend on its own model; one without usage costs nothing", () => {
    const dir = join(root(), "sessions");
    const store = SessionStore.jsonl(dir);
    const id = store.create("/w", "a/model");
    store.append(id, { type: "message", message: answer("1") });
    const usage = { inputTokens: 7, outputTokens: 3 };
    store.append(id, { type: "llm_call", depth: 1, model: "cheap/model", usage });
    store.append(id, { type: "llm_call", depth: 2, model: "cheap/model" });

    const restored = new JsonlSessionStore(dir).load(id);

    expect(restored.spend.map((s) => s.model)).toEqual(["a/model", "cheap/model"]);
    expect(restored.spend[1]?.usage).toEqual(usage);
    expect(restored.messages).toEqual([answer("1")]);
  });

  it("loads only the last entry's ancestry when a parentId skips an entry", () => {
    const dir = join(root(), "sessions");
    const store = SessionStore.jsonl(dir);
    store.create("/w", "m"); // makes the slug directory
    const id = craft(dir, "/w", [
      { id: "s", parentId: null, type: "session", version: 1, cwd: "/w", model: "m" },
      { id: "a", parentId: "s", type: "message", message: user("kept 1") },
      { id: "b", parentId: "a", type: "message", message: user("on a branch") },
      { id: "c", parentId: "a", type: "message", message: user("kept 2") },
    ]);

    const restored = store.load(id);

    expect(restored.messages.map((m) => m.content)).toEqual(["kept 1", "kept 2"]);
  });

  it("rebuilds [summary][tail] from the last compaction, keeping activations and permissions out", () => {
    const dir = join(root(), "sessions");
    const store = SessionStore.jsonl(dir);
    store.create("/w", "m");
    const call = { id: "t1", name: "bash", arguments: { command: "ls" } };
    const id = craft(dir, "/w", [
      { id: "s", parentId: null, type: "session", version: 1, cwd: "/w", model: "m" },
      { id: "a", parentId: "s", type: "message", message: user("old") },
      { id: "b", parentId: "a", type: "tool_activation", names: ["mcp__x__find"] },
      { id: "c", parentId: "b", type: "message", message: user("tail 1") },
      {
        id: "d",
        parentId: "c",
        type: "compaction",
        summary: "## Goal\nship",
        firstKeptEntryId: "c",
      },
      { id: "e", parentId: "d", type: "permission", call, decision: "allow", source: "user" },
      { id: "f", parentId: "e", type: "message", message: user("after") },
    ]);

    const restored = store.load(id);

    expect(restored.messages).toEqual([
      summaryMessage("## Goal\nship"),
      user("tail 1"),
      user("after"),
    ]);
    expect(restored.messages[0]?.content).toBe("<summary>\n## Goal\nship\n</summary>");
    expect(restored.activated).toEqual(["mcp__x__find"]);
  });

  it("warns, naming the id, when a compaction keeps from an unknown entry, and drops its tail", () => {
    const dir = join(root(), "sessions");
    const store = SessionStore.jsonl(dir);
    store.create("/w", "m");
    const id = craft(dir, "/w", [
      { id: "s", parentId: null, type: "session", version: 1, cwd: "/w", model: "m" },
      { id: "a", parentId: "s", type: "message", message: user("old") },
      {
        id: "d",
        parentId: "a",
        type: "compaction",
        summary: "## Goal\nship",
        firstKeptEntryId: "zz",
      },
      { id: "f", parentId: "d", type: "message", message: user("after") },
    ]);
    const warn = vi.spyOn(log, "warn").mockImplementation(() => {});

    try {
      const restored = store.load(id);

      expect(restored.messages).toEqual([summaryMessage("## Goal\nship"), user("after")]);
      expect(warn).toHaveBeenCalledTimes(1);
      expect(warn.mock.calls[0]?.[0]).toContain("unknown entry zz");
    } finally {
      warn.mockRestore();
    }
  });

  it("appends a resumed session to the same file, chained to its last line", () => {
    const dir = join(root(), "sessions");
    const id = SessionStore.jsonl(dir).create("/w", "m");

    const later = SessionStore.jsonl(dir);
    later.load(id);
    later.append(id, { type: "message", message: user("next day") });

    const entries = lines(onlyFile(dir));
    expect(entries).toHaveLength(2);
    expect(entries[1]?.parentId).toBe(entries[0]?.id);
  });

  it("skips a line that does not parse instead of failing the load", () => {
    const dir = join(root(), "sessions");
    const store = SessionStore.jsonl(dir);
    const id = store.create("/w", "m");
    store.append(id, { type: "message", message: user("ok") });
    writeFileSync(onlyFile(dir), '{"id":"cut-by-a-cra', { flag: "a" });

    expect(store.load(id).messages).toEqual([user("ok")]);
  });

  it("ends a crash-cut last line before the next append, so the appended entry survives", () => {
    const dir = join(root(), "sessions");
    const id = SessionStore.jsonl(dir).create("/w", "m");
    const writer = SessionStore.jsonl(dir);
    writer.append(id, { type: "message", message: user("one") });
    const intact = writer.append(id, { type: "message", message: user("two") });
    writeFileSync(onlyFile(dir), '{"type":"message","id":"deadbeef","parentId":"x","timesta', {
      flag: "a",
    });

    const resumed = SessionStore.jsonl(dir);
    resumed.load(id);
    const after = resumed.append(id, { type: "message", message: user("after the crash") });

    expect(after.parentId).toBe(intact.id);
    const reloaded = SessionStore.jsonl(dir).load(id);
    expect(reloaded.messages.map((m) => m.content)).toEqual(["one", "two", "after the crash"]);
  });

  it("does not write to a crash-cut file on load", () => {
    const dir = join(root(), "sessions");
    const id = SessionStore.jsonl(dir).create("/w", "m");
    const file = onlyFile(dir);
    writeFileSync(file, '{"id":"cut-by-a-cra', { flag: "a" });
    const before = readFileSync(file, "utf8");

    SessionStore.jsonl(dir).load(id);

    expect(readFileSync(file, "utf8")).toBe(before);
  });

  it("warns once when the last entry's ancestry does not reach the session entry", () => {
    const dir = join(root(), "sessions");
    const store = SessionStore.jsonl(dir);
    store.create("/w", "m");
    const id = craft(dir, "/w", [
      { id: "s", parentId: null, type: "session", version: 1, cwd: "/w", model: "m" },
      { id: "a", parentId: "s", type: "message", message: user("lost") },
      { id: "b", parentId: "gone", type: "message", message: user("orphan") },
    ]);
    const warn = vi.spyOn(log, "warn").mockImplementation(() => {});

    try {
      const restored = store.load(id);

      expect(restored.messages.map((m) => m.content)).toEqual(["orphan"]);
      expect(warn).toHaveBeenCalledTimes(1);
      expect(warn.mock.calls[0]?.[0]).toContain("does not reach the session entry");
    } finally {
      warn.mockRestore();
    }
  });

  it("does not warn when the ancestry reaches the session entry", () => {
    const dir = join(root(), "sessions");
    const store = SessionStore.jsonl(dir);
    const id = store.create("/w", "m");
    store.append(id, { type: "message", message: user("ok") });
    const warn = vi.spyOn(log, "warn").mockImplementation(() => {});

    try {
      store.load(id);

      expect(warn).not.toHaveBeenCalled();
    } finally {
      warn.mockRestore();
    }
  });

  it("lists this cwd's sessions newest first, and never another cwd's", () => {
    const dir = join(root(), "sessions");
    const store = SessionStore.jsonl(dir);
    const old = store.create("/w/repo", "m");
    const other = store.create("/w/other", "m");
    const recent = store.create("/w/repo", "m");
    const files = readdirSync(join(dir, cwdSlug("/w/repo")));
    const t = Date.now() / 1000;
    for (const f of files) {
      const when = f.includes(old) ? t - 3600 : t;
      utimesSync(join(dir, cwdSlug("/w/repo"), f), when, when);
    }

    expect(store.list("/w/repo").map((s) => s.id)).toEqual([recent, old]);
    expect(store.list("/w/other").map((s) => s.id)).toEqual([other]);
    expect(store.list("/w/none")).toEqual([]);
  });

  it("throws SessionNotFound for an unknown id", () => {
    const store = SessionStore.jsonl(join(root(), "sessions"));
    store.create("/w", "m");

    expect(() => store.load("nope")).toThrow(SessionNotFound);
    expect(() => store.load("nope")).toThrow("no session nope");
    expect(() => SessionStore.jsonl(join(root(), "empty")).load("x")).toThrow(SessionNotFound);
  });
});

describe("SessionStore.inMemory", () => {
  it("chains, restores and lists like the JSONL store", () => {
    const store = SessionStore.inMemory(() => new Date("2026-10-24T12:00:00Z"));
    const id = store.create("/w", "a/model");
    store.append(id, { type: "message", message: user("q") });
    store.append(id, { type: "model_change", model: "b/model" });
    const second = store.create("/w", "a/model");

    const restored = store.load(id);

    expect(restored).toMatchObject({ id, cwd: "/w", model: "b/model", messages: [user("q")] });
    expect(store.list("/w").map((s) => s.id)).toEqual([second, id]);
    expect(store.list("/elsewhere")).toEqual([]);
    expect(() => store.load("nope")).toThrow(SessionNotFound);
  });

  it("returns copies, so a caller cannot change what is stored", () => {
    const store = SessionStore.inMemory();
    const id = store.create("/w", "m");
    store.append(id, { type: "message", message: user("q") });

    store.load(id).messages.push(user("injected"));

    expect(store.load(id).messages).toEqual([user("q")]);
  });
});

describe("ancestry", () => {
  it("stops at a cycle instead of looping forever", () => {
    const e = (id: string, parentId: string): Entry => ({
      id,
      parentId,
      timestamp: "2026-10-24T12:00:00.000Z",
      type: "tool_activation",
      names: [],
    });

    expect(ancestry([e("a", "b"), e("b", "a")]).map((x) => x.id)).toEqual(["a", "b"]);
  });
});
