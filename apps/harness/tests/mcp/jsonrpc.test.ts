import { afterEach, describe, expect, it, vi } from "vitest";
import { createLogger } from "../../src/log.ts";
import {
  CALL_TIMEOUT_MS,
  JsonRpcError,
  type JsonRpcMessage,
  JsonRpcPeer,
  METHOD_NOT_FOUND,
} from "../../src/mcp/jsonrpc.ts";

// A peer whose outbound frames land in `sent`.
function peer(options: { send?: (m: JsonRpcMessage) => Promise<void> } = {}) {
  const sent: JsonRpcMessage[] = [];
  const logged: string[] = [];
  const log = createLogger("debug", { write: (t: string) => logged.push(t) });
  const send =
    options.send ??
    (async (m: JsonRpcMessage) => {
      sent.push(m);
    });
  return { rpc: new JsonRpcPeer(send, { log }), sent, logged };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("JsonRpcPeer", () => {
  it("frames a request with a fresh numeric id and resolves on its response", async () => {
    const { rpc, sent } = peer();

    const call = rpc.request("tools/list", { cursor: "p2" });
    await Promise.resolve();
    rpc.receive({ jsonrpc: "2.0", id: 1, result: { tools: [] } });

    await expect(call).resolves.toEqual({ tools: [] });
    expect(sent).toEqual([
      { jsonrpc: "2.0", id: 1, method: "tools/list", params: { cursor: "p2" } },
    ]);
  });

  it("correlates out-of-order responses by id", async () => {
    const { rpc } = peer();

    const a = rpc.request("a");
    const b = rpc.request("b");
    rpc.receive({ jsonrpc: "2.0", id: 2, result: "B" });
    rpc.receive({ jsonrpc: "2.0", id: 1, result: "A" });

    await expect(a).resolves.toBe("A");
    await expect(b).resolves.toBe("B");
  });

  it("rejects with JsonRpcError on an error response", async () => {
    const { rpc } = peer();

    const call = rpc.request("tools/call");
    rpc.receive({
      jsonrpc: "2.0",
      id: 1,
      error: { code: -32602, message: "Unknown tool", data: { x: 1 } },
    });

    const err = await call.catch((e: unknown) => e);
    expect(err).toBeInstanceOf(JsonRpcError);
    expect(err).toMatchObject({ code: -32602, message: "Unknown tool", data: { x: 1 } });
  });

  it("sends a notification without an id and omits absent params", async () => {
    const { rpc, sent } = peer();

    await rpc.notify("notifications/initialized");

    expect(sent).toEqual([{ jsonrpc: "2.0", method: "notifications/initialized" }]);
  });

  it("answers a server→client request -32601 and leaves the pending call alone", async () => {
    const { rpc, sent } = peer();

    const call = rpc.request("tools/call");
    rpc.receive({ jsonrpc: "2.0", id: "s1", method: "sampling/createMessage", params: {} });
    rpc.receive({ jsonrpc: "2.0", id: 7, method: "roots/list" });
    rpc.receive({ jsonrpc: "2.0", id: 1, result: { ok: true } });

    await expect(call).resolves.toEqual({ ok: true });
    expect(sent.slice(1)).toEqual([
      {
        jsonrpc: "2.0",
        id: "s1",
        error: { code: METHOD_NOT_FOUND, message: "Method not found: sampling/createMessage" },
      },
      { jsonrpc: "2.0", id: 7, error: { code: -32601, message: "Method not found: roots/list" } },
    ]);
  });

  it("answers ping with an empty result", () => {
    const { rpc, sent } = peer();

    rpc.receive({ jsonrpc: "2.0", id: 3, method: "ping" });

    expect(sent).toEqual([{ jsonrpc: "2.0", id: 3, result: {} }]);
  });

  it("never answers a server notification", () => {
    const { rpc, sent } = peer();

    rpc.receive({ jsonrpc: "2.0", method: "notifications/tools/list_changed" });
    rpc.receive({ jsonrpc: "2.0", method: "notifications/message", params: { level: "info" } });

    expect(sent).toEqual([]);
  });

  it("warns and carries on for a non-object frame, a frame without id and an unknown id", async () => {
    const { rpc, logged } = peer();

    const call = rpc.request("x");
    rpc.receive("garbage");
    rpc.receive({ jsonrpc: "2.0", result: 1 });
    rpc.receive({ jsonrpc: "2.0", id: 99, result: 1 });
    rpc.receive({ jsonrpc: "2.0", id: 1, result: "ok" });

    await expect(call).resolves.toBe("ok");
    expect(logged.join("")).toContain("unknown id 99");
    expect(logged.filter((l) => l.includes("warn:"))).toHaveLength(3);
  });

  it("rejects a call with no answer after the 60 s timeout and sends notifications/cancelled", async () => {
    vi.useFakeTimers();
    const { rpc, sent } = peer();

    const call = rpc.request("tools/call", { name: "slow" });
    const outcome = call.then(
      () => "resolved",
      (e: Error) => e.message,
    );
    await vi.advanceTimersByTimeAsync(CALL_TIMEOUT_MS - 1);
    const early = await Promise.race([outcome, Promise.resolve("pending")]);
    await vi.advanceTimersByTimeAsync(1);

    expect(CALL_TIMEOUT_MS).toBe(60_000);
    expect(early).toBe("pending");
    await expect(outcome).resolves.toBe("tools/call timed out after 60 s");
    expect(sent.at(-1)).toEqual({
      jsonrpc: "2.0",
      method: "notifications/cancelled",
      params: { requestId: 1, reason: "timeout" },
    });
  });

  it("a per-call timeout (30 s) overrides the 60 s default", async () => {
    vi.useFakeTimers();
    const { rpc, sent } = peer();
    const PER_CALL_MS = 30_000;

    const init = rpc.request("initialize", {}, { timeoutMs: PER_CALL_MS });
    const initOutcome = init.then(
      () => "resolved",
      (e: Error) => e.message,
    );
    const call = rpc.request("tools/call", { name: "slow" });
    const callOutcome = call.then(
      () => "resolved",
      (e: Error) => e.message,
    );
    await vi.advanceTimersByTimeAsync(PER_CALL_MS - 1);
    const early = await Promise.race([initOutcome, Promise.resolve("pending")]);
    await vi.advanceTimersByTimeAsync(1);

    expect(early).toBe("pending");
    await expect(initOutcome).resolves.toBe("initialize timed out after 30 s");
    expect(sent.at(-1)).toEqual({
      jsonrpc: "2.0",
      method: "notifications/cancelled",
      params: { requestId: 1, reason: "timeout" },
    });
    const callEarly = await Promise.race([callOutcome, Promise.resolve("pending")]);
    expect(callEarly).toBe("pending");
    await vi.advanceTimersByTimeAsync(CALL_TIMEOUT_MS - PER_CALL_MS);
    await expect(callOutcome).resolves.toBe("tools/call timed out after 60 s");
  });

  it("a late answer after the timeout is ignored", async () => {
    vi.useFakeTimers();
    const { rpc, logged } = peer();

    const call = rpc.request("x").catch((e: Error) => e.message);
    await vi.advanceTimersByTimeAsync(CALL_TIMEOUT_MS);
    rpc.receive({ jsonrpc: "2.0", id: 1, result: "late" });

    await expect(call).resolves.toContain("timed out");
    expect(logged.join("")).toContain("unknown id 1");
  });

  it("close() rejects every pending call at once and every later call", async () => {
    vi.useFakeTimers();
    const { rpc } = peer();

    const a = rpc.request("a").catch((e: Error) => e.message);
    const b = rpc.request("b").catch((e: Error) => e.message);
    rpc.close(new Error("server exited (code 1)"));

    await expect(a).resolves.toBe("server exited (code 1)");
    await expect(b).resolves.toBe("server exited (code 1)");
    await expect(rpc.request("c")).rejects.toThrow("server exited");
    await expect(rpc.notify("n")).rejects.toThrow("server exited");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("a failing send rejects the call", async () => {
    const { rpc } = peer({ send: () => Promise.reject(new Error("EPIPE")) });

    await expect(rpc.request("x")).rejects.toThrow("EPIPE");
  });
});
