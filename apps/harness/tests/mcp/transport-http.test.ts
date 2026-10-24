import { afterEach, describe, expect, it } from "vitest";
import { createLogger } from "../../src/log.ts";
import type { JsonRpcMessage } from "../../src/mcp/jsonrpc.ts";
import { ACCEPT, HttpTransport } from "../../src/mcp/transport-http.ts";
import { fakeFetch, sseResponse } from "../helpers/http.ts";
import { type FakeMcpHttp, startFakeMcpHttp } from "../helpers/mcp-http.ts";

let server: FakeMcpHttp | undefined;
afterEach(async () => {
  await server?.close();
  server = undefined;
});

const INIT: JsonRpcMessage = {
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: { protocolVersion: "2025-06-18", capabilities: {} },
};
const call = (id: number): JsonRpcMessage => ({
  jsonrpc: "2.0",
  id,
  method: "tools/call",
  params: { name: "echo", arguments: { q: id } },
});

function transport(url: string, headers: Record<string, string> = {}, fetchFn?: typeof fetch) {
  const logged: string[] = [];
  const log = createLogger("debug", { write: (t: string) => logged.push(t) });
  const t = new HttpTransport({ url, headers, log, label: 'mcp "h": ', fetch: fetchFn });
  const messages: JsonRpcMessage[] = [];
  const closed: Error[] = [];
  const started = t.start({
    onMessage: (m) => messages.push(m as JsonRpcMessage),
    onClose: (e) => closed.push(e),
  });
  return { t, messages, closed, logged, started };
}

describe("HttpTransport against the in-process fake", () => {
  it("a JSON response is one frame", async () => {
    server = await startFakeMcpHttp();
    const { t, messages } = transport(server.url("/json"));

    await t.send(INIT);

    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({ id: 1, result: { protocolVersion: "2025-06-18" } });
  });

  it("an SSE response is read with the provider's parser, every frame delivered", async () => {
    server = await startFakeMcpHttp();
    const { t, messages } = transport(server.url("/sse"));

    await t.send(call(5));

    expect(messages).toEqual([
      { jsonrpc: "2.0", method: "notifications/message", params: { level: "info" } },
      {
        jsonrpc: "2.0",
        id: 5,
        result: { content: [{ type: "text", text: '{"q":5}' }], structuredContent: { q: 5 } },
      },
    ]);
  });

  it("every POST has Accept with both types, JSON content-type and the configured headers", async () => {
    server = await startFakeMcpHttp();
    const { t } = transport(server.url("/json"), {
      Authorization: "Bearer secret-1",
      "X-Team": "tree",
    });

    await t.send(INIT);
    await t.send({ jsonrpc: "2.0", method: "notifications/initialized" });
    await t.send(call(2));

    expect(server.requests).toHaveLength(3);
    for (const request of server.requests) {
      expect(request.method).toBe("POST");
      expect(request.headers.accept).toBe(ACCEPT);
      expect(request.headers.accept).toBe("application/json, text/event-stream");
      expect(request.headers["content-type"]).toBe("application/json");
      expect(request.headers.authorization).toBe("Bearer secret-1");
      expect(request.headers["x-team"]).toBe("tree");
    }
  });

  it("a configured Accept header is replaced, not duplicated", async () => {
    server = await startFakeMcpHttp();
    const { t } = transport(server.url("/json"), { accept: "text/plain" });

    await t.send(INIT);

    expect(server.requests[0]?.headers.accept).toBe(ACCEPT);
  });

  it("stores a returned Mcp-Session-Id and sends it on every later request", async () => {
    server = await startFakeMcpHttp({ sessionId: "abc" });
    const { t, messages } = transport(server.url("/sse"));

    await t.send(INIT);
    expect(t.sessionId).toBe("abc");
    await t.send({ jsonrpc: "2.0", method: "notifications/initialized" });
    await t.send(call(2));

    expect(server.requests[0]?.headers["mcp-session-id"]).toBeUndefined();
    expect(server.requests.slice(1).map((r) => r.headers["mcp-session-id"])).toEqual([
      "abc",
      "abc",
    ]);
    expect(messages.at(-1)).toMatchObject({ id: 2, result: { structuredContent: { q: 2 } } });
  });

  it("sends no Mcp-Session-Id when the server returns none", async () => {
    server = await startFakeMcpHttp();
    const { t } = transport(server.url("/json"));

    await t.send(INIT);
    await t.send(call(2));

    expect(t.sessionId).toBeUndefined();
    expect(server.requests.map((r) => r.headers["mcp-session-id"])).toEqual([undefined, undefined]);
  });

  it("sends MCP-Protocol-Version once it is set", async () => {
    server = await startFakeMcpHttp();
    const { t } = transport(server.url("/json"));

    await t.send(INIT);
    t.setProtocolVersion("2025-06-18");
    await t.send(call(2));

    expect(server.requests.map((r) => r.headers["mcp-protocol-version"])).toEqual([
      undefined,
      "2025-06-18",
    ]);
  });

  it("a notification gets 202 and no frame", async () => {
    server = await startFakeMcpHttp();
    const { t, messages } = transport(server.url("/json"));

    await t.send({ jsonrpc: "2.0", method: "notifications/initialized" });

    expect(messages).toEqual([]);
  });

  it("a 404 on a session drops the transport and forgets the session id", async () => {
    server = await startFakeMcpHttp({ sessionId: "abc" });
    const { t, closed } = transport(server.url("/json"));
    await t.send(INIT);
    server.expire();

    await expect(t.send(call(2))).rejects.toThrow('mcp "h": session expired (HTTP 404)');

    expect(closed.map((e) => e.message)).toEqual(['mcp "h": session expired (HTTP 404)']);
    expect(t.sessionId).toBeUndefined();
  });

  it("close() sends DELETE with the session id, and later sends fail", async () => {
    server = await startFakeMcpHttp({ sessionId: "abc" });
    const { t } = transport(server.url("/json"), { Authorization: "Bearer s" });
    await t.send(INIT);

    await t.close();

    const last = server.requests.at(-1);
    expect(last?.method).toBe("DELETE");
    expect(last?.headers["mcp-session-id"]).toBe("abc");
    expect(last?.headers.authorization).toBe("Bearer s");
    await expect(t.send(call(3))).rejects.toThrow("not connected");
  });

  it("an unreachable server names the URL", async () => {
    server = await startFakeMcpHttp();
    const url = server.url("/json");
    await server.close();
    server = undefined;
    const { t } = transport(url);

    await expect(t.send(INIT)).rejects.toThrow(`mcp "h": POST ${url}:`);
  });
});

describe("HttpTransport with a fake fetch", () => {
  it("an HTTP error names the status and the start of the body", async () => {
    const { fetch } = fakeFetch(() => new Response("bad things", { status: 500 }));
    const { t } = transport("https://x.dev/mcp", {}, fetch);

    await expect(t.send(INIT)).rejects.toThrow('mcp "h": HTTP 500: bad things');
  });

  it("an SSE body cut into 3-byte chunks still yields whole frames", async () => {
    const frame = { jsonrpc: "2.0", id: 1, result: { text: "héllo 🙂" } };
    const { fetch } = fakeFetch(() => sseResponse(`data: ${JSON.stringify(frame)}\n\n`, 3));
    const { t, messages } = transport("https://x.dev/mcp", {}, fetch);

    await t.send(INIT);

    expect(messages).toEqual([frame]);
  });

  it("warns on a body that is not JSON", async () => {
    const { fetch } = fakeFetch(
      () => new Response("<html>", { headers: { "content-type": "text/html" } }),
    );
    const { t, messages, logged } = transport("https://x.dev/mcp", {}, fetch);

    await t.send(INIT);

    expect(messages).toEqual([]);
    expect(logged.join("")).toContain('warn: mcp "h": not JSON: <html>');
  });
});
