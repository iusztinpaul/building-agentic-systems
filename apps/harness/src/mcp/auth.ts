// The MCP login (spec §10.2.1): the MCP authorization flow
// (2025-06-18) for one streamable-HTTP server, by hand — no SDK
// (ADR-016 amendment 2026-10-25). On a 401: protected-resource
// metadata (RFC 9728, from WWW-Authenticate or the origin), the
// authorization server's metadata (RFC 8414), dynamic registration
// of a public client on a loopback redirect (RFC 7591), then an
// authorization-code grant with PKCE S256 (RFC 7636) and the
// `resource` indicator (RFC 8707). Tokens are refreshed silently.
// Token values, codes and the verifier are never logged.

import { spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { createServer, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { z } from "zod";
import { log as defaultLog, type Logger } from "../log.ts";
import {
  type AuthFile,
  type AuthTokens,
  deleteAuthFile,
  readAuthFile,
  writeAuthFile,
} from "./auth-store.ts";

export const LOGIN_TIMEOUT_MS = 300_000; // the interactive wait
const DEFAULT_EXPIRES_IN_S = 3_600; // a token answer without expires_in

// A 401 the flow may answer; `challenge` is its WWW-Authenticate.
export class McpAuthRequired extends Error {
  override name = "McpAuthRequired";
  readonly challenge: string;
  constructor(message: string, challenge: string) {
    super(message);
    this.challenge = challenge;
  }
}

// An OAuth error answer (`invalid_client`, `invalid_grant`, …).
export class OAuthError extends Error {
  override name = "OAuthError";
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

// How a login talks to the user. Absent: the run may not prompt.
export interface LoginUi {
  notify(line: string): void; // `mcp "<server>": log in at <url>`
  open?(url: string): void; // the browser; absent: the URL only
  timeoutMs?: number; // default LOGIN_TIMEOUT_MS
  signal?: AbortSignal; // Ctrl+C under --mcp-login
}

export interface McpAuthOptions {
  name: string; // the mcp.json key
  url: string;
  file: string; // authFilePath(env, name)
  fetch?: typeof fetch;
  log?: Logger;
  fresh?: boolean; // --mcp-login: cached tokens unused, client kept
}

const Prm = z.object({
  resource: z.string().url(),
  authorization_servers: z.string().url().array().min(1),
  scopes_supported: z.string().array().optional(),
});
const AsMeta = z.object({
  issuer: z.string(),
  authorization_endpoint: z.string().url(),
  token_endpoint: z.string().url(),
  registration_endpoint: z.string().url().optional(),
  code_challenge_methods_supported: z.string().array().default([]),
  scopes_supported: z.string().array().optional(),
});
const Registered = z.object({
  client_id: z.string(),
  client_secret: z.string().optional(),
});
const TokenAnswer = z.object({
  access_token: z.string(),
  refresh_token: z.string().optional(),
  // A string "3600" is tolerated; none: DEFAULT_EXPIRES_IN_S.
  expires_in: z.coerce
    .number()
    .int()
    .nonnegative()
    .default(DEFAULT_EXPIRES_IN_S),
  scope: z.string().optional(),
});
type TokenAnswer = z.infer<typeof TokenAnswer>;

interface Listening {
  server: Server; // the loopback redirect's listener
  client: AuthFile["client"];
}

export interface Discovery {
  resource: string; // the PRM `resource`, verbatim
  issuer: string;
  endpoints: AuthFile["endpoints"];
  scope?: string; // PRM scopes, else the AS's, space-joined
}

export class McpAuth {
  readonly #o: McpAuthOptions;
  readonly #fetch: typeof fetch;
  readonly #log: Logger;
  readonly #label: string;
  #data: AuthFile | undefined;
  #loaded = false;
  #refreshing: Promise<string | undefined> | undefined;

  constructor(options: McpAuthOptions) {
    this.#o = options;
    this.#fetch = options.fetch ?? fetch;
    this.#log = options.log ?? defaultLog;
    this.#label = `mcp "${options.name}": `;
  }

  // The bearer to send: the cached one, refreshed when expired.
  async token(): Promise<string | undefined> {
    const tokens = (await this.#load())?.tokens;
    if (!tokens) return undefined;
    const expires = tokens.expires_at ? Date.parse(tokens.expires_at) : NaN;
    return expires <= Date.now() ? this.refresh() : tokens.access_token;
  }

  // After a 401: the new access token, or undefined when there is
  // no refresh token or the refresh failed. Concurrent calls share.
  refresh(): Promise<string | undefined> {
    this.#refreshing ??= this.#refresh().finally(() => {
      this.#refreshing = undefined;
    });
    return this.#refreshing;
  }

  // The interactive flow, bounded by `ui.timeoutMs`.
  async login(challenge: string, ui: LoginUi): Promise<void> {
    const found = await discover(this.#fetch, this.#o.url, challenge);
    const { server, client } = await this.#client(found);
    try {
      const codeVerifier = verifier();
      const state = verifier(32);
      const url = authorizeUrl(found, client, codeVerifier, state);
      ui.notify(`${this.#label}log in at ${url}`);
      ui.open?.(url);
      const code = await this.#wait(server, state, ui);
      const answer = await tokenRequest(this.#fetch, found, client, {
        grant_type: "authorization_code",
        code,
        redirect_uri: client.redirect_uri,
        code_verifier: codeVerifier,
      }).catch(async (err: Error) => {
        await this.#forgetRejected(err);
        throw new Error(this.#label + err.message);
      });
      const base = this.#base(found, client);
      await this.#store({ ...base, tokens: tokens(answer) });
      this.#log.debug(`${this.#label}logged in`);
    } finally {
      close(server);
    }
  }

  async #load(): Promise<AuthFile | undefined> {
    if (this.#loaded) return this.#data;
    const data = await readAuthFile(this.#o.file, this.#o.url, this.#log);
    this.#loaded = true;
    this.#data = data && this.#o.fresh ? { ...data, tokens: null } : data;
    return this.#data;
  }

  async #store(data: AuthFile): Promise<void> {
    this.#data = data;
    await writeAuthFile(this.#o.file, data);
  }

  #base(
    found: Discovery,
    client: AuthFile["client"],
  ): Omit<AuthFile, "tokens"> {
    const { resource, issuer, endpoints } = found;
    return { url: this.#o.url, issuer, endpoints, resource, client };
  }

  async #refresh(): Promise<string | undefined> {
    const data = await this.#load();
    const refreshToken = data?.tokens?.refresh_token;
    if (!data || !refreshToken) return undefined;
    try {
      const answer = await tokenRequest(this.#fetch, data, data.client, {
        grant_type: "refresh_token",
        refresh_token: refreshToken,
      });
      await this.#store({ ...data, tokens: tokens(answer, refreshToken) });
      this.#log.debug(`${this.#label}refreshed the access token`);
      return answer.access_token;
    } catch (err) {
      await this.#forgetRejected(err as Error);
      const why = (err as Error).message;
      this.#log.debug(`${this.#label}refresh failed: ${why}`);
      return undefined;
    }
  }

  // The AS no longer knows the client: delete the file, so the next
  // login registers again (in this process or a later one).
  async #forgetRejected(err: Error): Promise<void> {
    if (!(err instanceof OAuthError) || err.code !== "invalid_client") {
      return;
    }
    this.#data = undefined;
    await deleteAuthFile(this.#o.file);
  }

  // The stored client on its persisted port; a new one (fresh port,
  // registration, written at once with tokens: null) when there is
  // none or the port is taken.
  async #client(found: Discovery): Promise<Listening> {
    const stored = (await this.#load())?.client;
    if (stored && this.#data?.issuer === found.issuer) {
      const port = Number(new URL(stored.redirect_uri).port);
      const server = await listen(port).catch(
        (err: NodeJS.ErrnoException) => {
          if (err.code === "EADDRINUSE") return undefined;
          throw err;
        },
      );
      if (server) return { server, client: stored };
    }
    const server = await listen(0);
    const { port } = server.address() as AddressInfo;
    const redirect = `http://127.0.0.1:${port}/callback`;
    try {
      const client = await register(this.#fetch, found, redirect);
      await this.#store({ ...this.#base(found, client), tokens: null });
      this.#log.debug(`${this.#label}registered a client`);
      return { server, client };
    } catch (err) {
      close(server);
      throw new Error(this.#label + (err as Error).message);
    }
  }

  // The loopback redirect: a wrong `state` is answered 400 and the
  // wait goes on; `error=` ends it; so do the timeout and the signal.
  #wait(server: Server, state: string, ui: LoginUi): Promise<string> {
    const { name } = this.#o;
    const ms = ui.timeoutMs ?? LOGIN_TIMEOUT_MS;
    const after =
      ms % 60_000 === 0 ? `${ms / 60_000} min` : `${ms / 1000} s`;
    return new Promise((resolve, reject) => {
      const done = (err: Error | undefined, code = ""): void => {
        clearTimeout(timer);
        ui.signal?.removeEventListener("abort", cancel);
        if (err) reject(err);
        else resolve(code);
      };
      const fail = (text: string) => done(new Error(this.#label + text));
      const timer = setTimeout(
        () =>
          fail(
            `login timed out after ${after}; run tree-agent ` +
              `--mcp-login ${name} to log in`,
          ),
        ms,
      );
      const cancel = () => fail("login cancelled");
      if (ui.signal?.aborted) return cancel();
      ui.signal?.addEventListener("abort", cancel, { once: true });
      server.on("request", (req, res) => {
        const url = new URL(req.url ?? "/", "http://127.0.0.1");
        const q = url.searchParams;
        if (url.pathname !== "/callback")
          return page(res, 404, "not found");
        if (q.get("state") !== state) {
          return page(res, 400, "this is not the login tree-agent started");
        }
        const error = q.get("error");
        if (error) {
          const why = q.get("error_description") ?? error;
          page(res, 400, `login failed: ${why}`);
          return fail(`login failed: ${why}`);
        }
        const code = q.get("code");
        if (!code) return page(res, 400, "no code in the redirect");
        page(
          res,
          200,
          `you are logged in to ${name}. You can close this tab.`,
        );
        done(undefined, code);
      });
    });
  }
}

// RFC 9728 then RFC 8414. The PRM must describe this server's
// origin; the AS (checked before it is fetched) and every endpoint
// must be HTTPS (or loopback); the AS metadata's `issuer` must be the
// AS URL (RFC 8414 §3.3).
export async function discover(
  fetchFn: typeof fetch,
  url: string,
  challenge: string,
): Promise<Discovery> {
  const named = /resource_metadata="?([^"\s,]+)/.exec(challenge)?.[1];
  const prmUrl =
    named ?? new URL("/.well-known/oauth-protected-resource", url).href;
  const prm = await getJson(fetchFn, prmUrl, Prm);
  if (new URL(prm.resource).origin !== new URL(url).origin) {
    throw new Error(`${prmUrl}: resource ${prm.resource} is not ${url}`);
  }
  const asUrl = prm.authorization_servers[0] as string;
  secure(asUrl, "the authorization server");
  const as = new URL(asUrl);
  const path = as.pathname.replace(/\/$/, "");
  const wellKnown = "/.well-known/oauth-authorization-server";
  const metaUrl = `${as.origin}${wellKnown}${path}`;
  const meta = await getJson(fetchFn, metaUrl, AsMeta);
  const bare = (u: string) => u.replace(/\/$/, "");
  if (bare(meta.issuer) !== bare(asUrl)) {
    throw new Error(`${metaUrl}: issuer ${meta.issuer} is not ${asUrl}`);
  }
  if (!meta.code_challenge_methods_supported.includes("S256")) {
    throw new Error(`${metaUrl}: no PKCE S256 support`);
  }
  if (!meta.registration_endpoint) {
    throw new Error(`${metaUrl}: no dynamic client registration`);
  }
  const endpoints = {
    authorization: meta.authorization_endpoint,
    token: meta.token_endpoint,
    registration: meta.registration_endpoint,
  };
  for (const endpoint of Object.values(endpoints)) {
    secure(endpoint, "authorization endpoints");
  }
  const scopes = prm.scopes_supported ?? meta.scopes_supported ?? [];
  return {
    resource: prm.resource,
    issuer: meta.issuer,
    endpoints,
    ...(scopes.length > 0 && { scope: scopes.join(" ") }),
  };
}

export function authorizeUrl(
  found: Discovery,
  client: AuthFile["client"],
  codeVerifier: string,
  state: string,
): string {
  const url = new URL(found.endpoints.authorization);
  const params = {
    response_type: "code",
    client_id: client.client_id,
    redirect_uri: client.redirect_uri,
    code_challenge: challengeOf(codeVerifier),
    code_challenge_method: "S256",
    state,
    resource: found.resource,
    ...(found.scope && { scope: found.scope }),
  };
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  return url.href;
}

// base64url(sha256(verifier)), RFC 7636 §4.2.
export function challengeOf(codeVerifier: string): string {
  return createHash("sha256").update(codeVerifier).digest("base64url");
}

// A configured Authorization header disables the flow.
export function hasAuthorization(headers: Record<string, string>): boolean {
  return Object.keys(headers).some(
    (k) => k.toLowerCase() === "authorization",
  );
}

// Best effort; the URL is always printed as well.
export function openBrowser(url: string): void {
  const [command, args] =
    process.platform === "darwin"
      ? ["open", [url]]
      : process.platform === "win32"
        ? ["cmd", ["/c", "start", '""', url.replaceAll("&", "^&")]]
        : ["xdg-open", [url]];
  try {
    const child = spawn(command, args, { detached: true, stdio: "ignore" });
    child.on("error", () => {});
    child.unref();
  } catch {
    // no browser: the printed URL remains
  }
}

function verifier(bytes = 64): string {
  return randomBytes(bytes).toString("base64url");
}

async function register(
  fetchFn: typeof fetch,
  found: Discovery,
  redirect: string,
): Promise<AuthFile["client"]> {
  const res = await fetchFn(found.endpoints.registration, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify({
      client_name: "tree-agent",
      redirect_uris: [redirect],
      grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"],
      token_endpoint_auth_method: "none",
    }),
  });
  if (!res.ok) throw await oauthError(res, "client registration");
  const body = Registered.parse(await res.json());
  return {
    client_id: body.client_id,
    client_secret: body.client_secret ?? null,
    redirect_uri: redirect,
    registered_at: new Date().toISOString(),
  };
}

// POST the token endpoint, form-encoded, `resource` always sent.
async function tokenRequest(
  fetchFn: typeof fetch,
  at: Pick<Discovery, "endpoints" | "resource">,
  client: AuthFile["client"],
  params: Record<string, string>,
): Promise<TokenAnswer> {
  const form = new URLSearchParams(params);
  form.set("client_id", client.client_id);
  form.set("resource", at.resource);
  if (client.client_secret) form.set("client_secret", client.client_secret);
  const res = await fetchFn(at.endpoints.token, {
    method: "POST",
    headers: {
      "content-type": "application/x-www-form-urlencoded",
      accept: "application/json",
    },
    body: form,
  });
  if (!res.ok) throw await oauthError(res, "token request");
  const parsed = TokenAnswer.safeParse(await res.json().catch(() => null));
  if (!parsed.success) {
    throw new Error(`token request: unexpected ${fields(parsed.error)}`);
  }
  return parsed.data;
}

function tokens(answer: TokenAnswer, refreshToken?: string): AuthTokens {
  const expires = Date.now() + answer.expires_in * 1000;
  return {
    access_token: answer.access_token,
    refresh_token: answer.refresh_token ?? refreshToken ?? null,
    expires_at: new Date(expires).toISOString(),
    scope: answer.scope ?? null,
  };
}

// `<what> failed: <error>: <description>` — never the request.
async function oauthError(res: Response, what: string): Promise<Error> {
  const body = (await res.json().catch(() => ({}))) as {
    error?: unknown;
    error_description?: unknown;
  };
  const code = typeof body.error === "string" ? body.error : "";
  const desc =
    typeof body.error_description === "string"
      ? `: ${body.error_description}`
      : "";
  const why = code ? `${code}${desc}` : `HTTP ${res.status}`;
  return new OAuthError(code, `${what} failed: ${why}`);
}

async function getJson<T>(
  fetchFn: typeof fetch,
  url: string,
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
): Promise<T> {
  let res: Response;
  try {
    res = await fetchFn(url, { headers: { accept: "application/json" } });
  } catch (err) {
    throw new Error(`GET ${url}: ${(err as Error).message}`);
  }
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  const parsed = schema.safeParse(await res.json().catch(() => null));
  if (parsed.success) return parsed.data;
  throw new Error(`${url}: unexpected ${fields(parsed.error)}`);
}

// The offending fields of a zod error: `expires_in`, `a.b, c`, `body`.
function fields(error: z.ZodError): string {
  const paths = error.issues.map((i) => i.path.join(".") || "body");
  return [...new Set(paths)].join(", ");
}

function secure(url: string, what: string): void {
  const { protocol, hostname } = new URL(url);
  const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(hostname);
  if (protocol !== "https:" && !loopback) {
    throw new Error(`${url}: ${what} must use https`);
  }
}

function listen(port: number): Promise<Server> {
  const server = createServer();
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => {
      server.off("error", reject);
      resolve(server);
    });
  });
}

// A browser's keep-alive socket must not hold the process open.
function close(server: Server): void {
  server.close();
  server.closeAllConnections();
}

function page(res: ServerResponse, status: number, text: string): void {
  const safe = text.replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
  res.writeHead(status, {
    "content-type": "text/html; charset=utf-8",
    connection: "close",
  });
  res.end(
    `<!doctype html><title>tree-agent</title><p>tree-agent: ${safe}</p>`,
  );
}
