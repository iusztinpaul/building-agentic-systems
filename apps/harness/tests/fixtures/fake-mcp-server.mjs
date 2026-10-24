#!/usr/bin/env node
// A stdio MCP server in plain Node for the client tests: one JSON-RPC
// frame per line. Two tools over two `tools/list` pages (`echo`, then
// `read_note` with `readOnlyHint: true` and `structuredContent`).
// It logs every received method to stderr ("recv <method>") and
// refuses `tools/list` before `notifications/initialized`.
//
// Knobs (env): FAKE_MCP_MARKER=<file> appends this pid per start;
// FAKE_MCP_EXIT_AFTER_CALL=1 exits after answering one tools/call;
// FAKE_MCP_ENDLESS_PAGES=1 answers every tools/list page with a new
// tool and a never-seen cursor.
// `echo` args: {hang: true} never answers; {probe: true} first sends
// a server→client request and answers with what the client replied.

import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";

const ECHO = {
  name: "echo",
  description: "Echo the arguments back as JSON text.",
  inputSchema: { type: "object", additionalProperties: true },
};
const READ_NOTE = {
  name: "read_note",
  description: "Read a note by id.",
  inputSchema: {
    type: "object",
    properties: { id: { type: "string" } },
    required: ["id"],
  },
  annotations: { readOnlyHint: true },
};

if (process.env.FAKE_MCP_MARKER) {
  appendFileSync(process.env.FAKE_MCP_MARKER, `${process.pid}\n`);
}
process.stderr.write("fake-mcp-server: ready\n");

let initialized = false;
const waiting = new Map(); // our request id → the tools/call id

const send = (frame) => process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", ...frame })}\n`);
const fail = (id, code, message) => send({ id, error: { code, message } });
const text = (value) => ({ content: [{ type: "text", text: JSON.stringify(value) }] });

function answerCall(id, result) {
  send({ id, result });
  if (process.env.FAKE_MCP_EXIT_AFTER_CALL === "1") process.exit(0);
}

function handle(message) {
  const { id, method, params } = message;
  if (method === undefined) {
    // a response to our probe
    const callId = waiting.get(id);
    waiting.delete(id);
    if (callId !== undefined) answerCall(callId, text({ probeAnswer: message }));
    return;
  }
  process.stderr.write(`recv ${method}\n`);
  if (method === "initialize") {
    send({
      id,
      result: {
        protocolVersion: params.protocolVersion,
        capabilities: { tools: {} },
        serverInfo: { name: "fake-mcp-server", version: "1.0.0" },
      },
    });
  } else if (method === "notifications/initialized") {
    initialized = true;
  } else if (method === "tools/list") {
    if (!initialized) return fail(id, -32002, "not initialized");
    const cursor = params?.cursor;
    if (process.env.FAKE_MCP_ENDLESS_PAGES === "1") {
      const n = cursor === undefined ? 1 : Number(cursor.slice(1)) + 1;
      send({ id, result: { tools: [{ ...ECHO, name: `t${n}` }], nextCursor: `c${n}` } });
    } else if (cursor === undefined) send({ id, result: { tools: [ECHO], nextCursor: "p2" } });
    else if (cursor === "p2") send({ id, result: { tools: [READ_NOTE] } });
    else fail(id, -32602, `bad cursor ${cursor}`);
  } else if (method === "tools/call") {
    const args = params.arguments ?? {};
    if (params.name === "echo") {
      if (args.hang) return;
      if (args.probe) {
        waiting.set("srv-1", id);
        send({ id: "srv-1", method: "sampling/createMessage", params: { messages: [] } });
        return;
      }
      answerCall(id, text(args));
    } else if (params.name === "read_note") {
      const note = { id: args.id, body: `note ${args.id}` };
      answerCall(id, { ...text(note), structuredContent: note });
    } else {
      fail(id, -32602, `Unknown tool: ${params.name}`);
    }
  } else if (id !== undefined) {
    fail(id, -32601, `Method not found: ${method}`);
  }
}

const lines = createInterface({ input: process.stdin });
lines.on("line", (line) => {
  if (line.trim()) handle(JSON.parse(line));
});
lines.on("close", () => process.exit(0));
