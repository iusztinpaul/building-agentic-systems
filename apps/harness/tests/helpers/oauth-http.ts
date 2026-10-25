// An OAuth-protected streamable-HTTP MCP server AND its authorization
// server in one http.createServer on 127.0.0.1:0, shaped like the
// hosted tree-memory (task 219's probe of 2026-10-25). `/mcp` answers
// 401 + `WWW-Authenticate: Bearer realm="FastMCP", resource_metadata=…`
// until a valid bearer arrives, then behaves like mcp-http.ts's
// `/json`; `/open` is the same MCP server without auth. The AS checks
// what a real one would: the registered redirect_uri, the `resource`,
// and base64url(sha256(code_verifier)) === code_challenge (computed
// here, independently of src/). `fetch` reaches the same handler in
// memory (no sockets: fake-timer tests) and passes other URLs — the
// loopback redirect — to the real fetch. `browse()` plays the browser.

import { createHash } from "node:crypto";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { answer } from "./mcp-http.ts";

export type Query = Record<string, string>;

export interface FakeOAuth {
  base: string; // http://127.0.0.1:<port>, the PRM `resource`
  url: string; // the protected MCP endpoint, <base>/mcp
  openUrl: string; // an unprotected MCP endpoint, <base>/open
  fetch: typeof fetch;
  // biome-ignore lint/suspicious/noExplicitAny: assertions read arbitrary JSON
  registrations: any[];
  authorizations: Query[];
  tokenRequests: Query[];
  // biome-ignore lint/suspicious/noExplicitAny: JSON-RPC frames
  mcp: { authorization: string | null; body: any }[];
  wellKnown: string[]; // the metadata paths fetched, in order
  issued: string[]; // access tokens, in order
  expireAccessToken(): void; // every issued token 401s from now on
  rejectRefresh(): void;
  rejectClient(): void; // every registered client is invalid_client
  noRegistration(): void;
  noScopes(): void;
  noResourceMetadata(): void; // a bare `Bearer realm="FastMCP"`
  omitRefreshToken(): void; // refresh answers carry none
  rejectEveryToken(): void; // even a fresh token 401s
  denyNext(): void; // the next authorize redirects with error=
  expiresIn(value: unknown): void; // in token answers; undefined: omitted
  badIssuer(): void; // the AS metadata names https://evil.example
  // GET the authorize URL (no redirect following), then the loopback
  // Location (optionally tampered with): the callback's answer.
  browse(authorizeUrl: string, tamper?: (location: URL) => void): Promise<Response>;
  close(): Promise<void>;
}

const sha256 = (text: string) => createHash("sha256").update(text).digest("base64url");
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });

export async function startFakeOAuth(options: { prmScopes?: string[] } = {}): Promise<FakeOAuth> {
  let base = "";
  let n = 0;
  const flags = {
    refresh: true,
    registration: true,
    scopes: true,
    resourceMetadata: true,
    refreshToken: true,
    acceptTokens: true,
    deny: false,
    badIssuer: false,
  };
  let expiresIn: unknown = 3600;
  const valid = new Set<string>();
  const clients = new Map<string, string[]>(); // client_id → redirect_uris
  const rejected = new Set<string>();
  const codes = new Map<string, Query>();
  const refreshTokens = new Map<string, string>(); // token → client_id

  const issue = (client: string, refresh: boolean) => {
    n++;
    const access = `at-${n}-${client}`;
    valid.add(access);
    fake.issued.push(access);
    const body: Record<string, unknown> = {
      access_token: access,
      token_type: "Bearer",
      ...(expiresIn !== undefined && { expires_in: expiresIn }),
      scope: "openid profile email offline_access",
    };
    if (refresh) {
      body.refresh_token = `rt-${n}-${client}`;
      refreshTokens.set(body.refresh_token as string, client);
    }
    return json(200, body);
  };

  const token = (form: Query): Response => {
    fake.tokenRequests.push(form);
    const client = form.client_id ?? "";
    if (!clients.has(client) || rejected.has(client)) {
      return json(401, { error: "invalid_client", error_description: "unknown client" });
    }
    if (form.resource !== base) return json(400, { error: "invalid_target" });
    if (form.grant_type === "authorization_code") {
      const grant = codes.get(form.code ?? "");
      codes.delete(form.code ?? "");
      const ok =
        grant?.client_id === client &&
        grant.redirect_uri === form.redirect_uri &&
        grant.resource === form.resource &&
        sha256(form.code_verifier ?? "") === grant.code_challenge;
      if (!ok) return json(400, { error: "invalid_grant", error_description: "bad code" });
      return issue(client, true);
    }
    if (form.grant_type === "refresh_token") {
      if (!flags.refresh || refreshTokens.get(form.refresh_token ?? "") !== client) {
        return json(400, { error: "invalid_grant", error_description: "refresh token expired" });
      }
      return issue(client, flags.refreshToken);
    }
    return json(400, { error: "unsupported_grant_type" });
  };

  const authorize = (q: Query): Response => {
    fake.authorizations.push(q);
    const uris = clients.get(q.client_id ?? "");
    if (!uris || rejected.has(q.client_id ?? "") || !uris.includes(q.redirect_uri ?? "")) {
      return new Response("unknown client or redirect_uri", { status: 400 });
    }
    if (q.code_challenge_method !== "S256" || !q.code_challenge) {
      return new Response("PKCE S256 required", { status: 400 });
    }
    const location = new URL(q.redirect_uri as string);
    if (flags.deny) {
      flags.deny = false;
      location.searchParams.set("error", "access_denied");
      location.searchParams.set("error_description", "The user declined");
    } else {
      const code = `code-${++n}`;
      codes.set(code, q);
      location.searchParams.set("code", code);
    }
    location.searchParams.set("state", q.state ?? "");
    return new Response(null, { status: 302, headers: { location: location.href } });
  };

  const mcp = (req: Request, text: string, open: boolean): Response => {
    const body = text ? JSON.parse(text) : undefined;
    const authorization = req.headers.get("authorization");
    if (!open) fake.mcp.push({ authorization, body });
    const bearer = authorization?.startsWith("Bearer ") ? authorization.slice(7) : "";
    if (!open && !(flags.acceptTokens && valid.has(bearer))) {
      const meta = `${base}/.well-known/oauth-protected-resource`;
      const challenge = flags.resourceMetadata
        ? `Bearer realm="FastMCP", resource_metadata="${meta}"`
        : 'Bearer realm="FastMCP"';
      return json(
        401,
        { error: "invalid_request", error_description: "Bearer token required" },
        { "www-authenticate": challenge },
      );
    }
    if (body?.id === undefined) return new Response(null, { status: 202 });
    return json(200, { jsonrpc: "2.0", id: body.id, ...answer(body) });
  };

  const handle = async (req: Request): Promise<Response> => {
    const url = new URL(req.url);
    const text = req.method === "GET" ? "" : await req.text();
    switch (url.pathname) {
      case "/mcp":
      case "/open":
        return mcp(req, text, url.pathname === "/open");
      case "/.well-known/oauth-protected-resource":
        fake.wellKnown.push(url.pathname);
        return json(200, {
          resource: base,
          authorization_servers: [base],
          bearer_methods_supported: ["header"],
          ...(options.prmScopes && { scopes_supported: options.prmScopes }),
        });
      case "/.well-known/oauth-authorization-server":
        fake.wellKnown.push(url.pathname);
        return json(200, {
          issuer: flags.badIssuer ? "https://evil.example" : base,
          authorization_endpoint: `${base}/oauth2/authorize`,
          token_endpoint: `${base}/oauth2/token`,
          ...(flags.registration && { registration_endpoint: `${base}/oauth2/register` }),
          grant_types_supported: ["authorization_code", "refresh_token"],
          code_challenge_methods_supported: ["S256"],
          response_types_supported: ["code"],
          response_modes_supported: ["query"],
          token_endpoint_auth_methods_supported: ["none", "client_secret_post"],
          ...(flags.scopes && {
            scopes_supported: ["email", "offline_access", "openid", "profile"],
          }),
        });
      case "/oauth2/register": {
        const body = JSON.parse(text);
        fake.registrations.push(body);
        const client_id = `client-${fake.registrations.length}`;
        clients.set(client_id, body.redirect_uris ?? []);
        return json(201, { ...body, client_id, client_id_issued_at: 1 });
      }
      case "/oauth2/authorize":
        return authorize(Object.fromEntries(url.searchParams));
      case "/oauth2/token":
        return token(Object.fromEntries(new URLSearchParams(text)));
      default:
        return new Response("not found", { status: 404 });
    }
  };

  const server = createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const headers = new Headers();
    for (const name of ["authorization", "content-type", "accept"]) {
      const value = req.headers[name];
      if (typeof value === "string") headers.set(name, value);
    }
    const method = req.method ?? "GET";
    const request = new Request(`${base}${req.url}`, {
      method,
      headers,
      body: method === "GET" ? undefined : raw,
    });
    const response = await handle(request);
    res.writeHead(response.status, Object.fromEntries(response.headers));
    res.end(Buffer.from(await response.arrayBuffer()));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

  const inMemory = (async (input: string | URL | Request, init?: RequestInit) => {
    const request = new Request(input, init);
    return new URL(request.url).origin === base ? handle(request) : fetch(request);
  }) as typeof fetch;

  const fake: FakeOAuth = {
    base,
    url: `${base}/mcp`,
    openUrl: `${base}/open`,
    fetch: inMemory,
    registrations: [],
    authorizations: [],
    tokenRequests: [],
    mcp: [],
    wellKnown: [],
    issued: [],
    expireAccessToken: () => valid.clear(),
    rejectRefresh: () => {
      flags.refresh = false;
    },
    rejectClient: () => {
      for (const id of clients.keys()) rejected.add(id);
    },
    noRegistration: () => {
      flags.registration = false;
    },
    noScopes: () => {
      flags.scopes = false;
    },
    noResourceMetadata: () => {
      flags.resourceMetadata = false;
    },
    omitRefreshToken: () => {
      flags.refreshToken = false;
    },
    rejectEveryToken: () => {
      flags.acceptTokens = false;
    },
    denyNext: () => {
      flags.deny = true;
    },
    expiresIn: (value) => {
      expiresIn = value;
    },
    badIssuer: () => {
      flags.badIssuer = true;
    },
    browse: async (authorizeUrl, tamper) => {
      const first = await inMemory(authorizeUrl, { redirect: "manual" });
      const location = first.headers.get("location");
      if (first.status !== 302 || !location) return first;
      const target = new URL(location);
      tamper?.(target);
      return fetch(target);
    },
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
  return fake;
}
