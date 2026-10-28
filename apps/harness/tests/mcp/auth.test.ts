import { createHash } from "node:crypto";
import { once } from "node:events";
import { readFileSync, statSync } from "node:fs";
import { Agent, createServer, request, type Server } from "node:http";
import { type AddressInfo, connect, type Socket } from "node:net";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createLogger, type Logger } from "../../src/log.ts";
import {
  authorizeUrl,
  challengeOf,
  discover,
  hasAuthorization,
  LOGIN_TIMEOUT_MS,
  type LoginUi,
  McpAuth,
} from "../../src/mcp/auth.ts";
import { type FakeOAuth, startFakeOAuth } from "../helpers/oauth-http.ts";
import { useTmpDir } from "../helpers/tmp.ts";

const tmp = useTmpDir("mcp-auth");
let fake: FakeOAuth;
const servers: Server[] = [];

beforeEach(async () => {
  fake = await startFakeOAuth();
});
afterEach(async () => {
  vi.useRealTimers();
  for (const s of servers.splice(0)) s.close();
  await fake.close();
});

const file = () => join(tmp(), "mcp-auth", "fake.json");
const stored = () => JSON.parse(readFileSync(file(), "utf8"));
const challenge = () =>
  `Bearer realm="FastMCP", resource_metadata="${fake.base}/.well-known/oauth-protected-resource"`;
const sha256 = (text: string) => createHash("sha256").update(text).digest("base64url");

function sink(): { log: Logger; lines: string[] } {
  const lines: string[] = [];
  return { log: createLogger("debug", { write: (t: string) => lines.push(t) }), lines };
}

function auth(log = sink().log, fresh = false): McpAuth {
  return new McpAuth({ name: "fake", url: fake.url, file: file(), fetch: fake.fetch, log, fresh });
}

// A UI whose "browser" completes the login (or does `act` instead).
function browser(act?: (url: string) => Promise<unknown>) {
  const lines: string[] = [];
  const visits: Promise<unknown>[] = [];
  const ui: LoginUi = {
    notify: (line) => lines.push(line),
    open: (url) => {
      visits.push(act ? act(url) : fake.browse(url));
    },
  };
  return { ui, lines, visits };
}

async function until(ok: () => boolean): Promise<void> {
  for (let i = 0; i < 2_000 && !ok(); i++) await new Promise((r) => setImmediate(r));
  expect(ok()).toBe(true);
}

async function loggedIn(log?: Logger): Promise<McpAuth> {
  const a = auth(log);
  await a.login(challenge(), browser().ui);
  return a;
}

describe("discover (RFC 9728 → RFC 8414)", () => {
  it("follows the 401's resource_metadata, then the AS metadata at the issuer", async () => {
    const found = await discover(fake.fetch, fake.url, challenge());

    expect(fake.wellKnown).toEqual([
      "/.well-known/oauth-protected-resource",
      "/.well-known/oauth-authorization-server",
    ]);
    expect(found).toEqual({
      resource: fake.base,
      issuer: fake.base,
      endpoints: {
        authorization: `${fake.base}/oauth2/authorize`,
        token: `${fake.base}/oauth2/token`,
        registration: `${fake.base}/oauth2/register`,
      },
      scope: "email offline_access openid profile",
    });
  });

  it("falls back to the origin's /.well-known/oauth-protected-resource without resource_metadata", async () => {
    const seen: string[] = [];
    const spy = ((input: string | URL | Request, init?: RequestInit) => {
      seen.push(String(input));
      return fake.fetch(input, init);
    }) as typeof fetch;

    await discover(spy, fake.url, 'Bearer realm="FastMCP"');

    expect(seen[0]).toBe(`${fake.base}/.well-known/oauth-protected-resource`);
  });

  it("takes the PRM scopes over the AS's, and omits scope when neither has any", async () => {
    await fake.close();
    fake = await startFakeOAuth({ prmScopes: ["mcp:read"] });
    expect((await discover(fake.fetch, fake.url, challenge())).scope).toBe("mcp:read");

    await fake.close();
    fake = await startFakeOAuth();
    fake.noScopes();
    expect(await discover(fake.fetch, fake.url, challenge())).not.toHaveProperty("scope");
  });

  it("refuses an AS without dynamic registration, a PRM for another origin and plain-http endpoints", async () => {
    fake.noRegistration();
    await expect(discover(fake.fetch, fake.url, challenge())).rejects.toThrow(
      "no dynamic client registration",
    );

    await expect(discover(fake.fetch, "https://127.0.0.2:1/mcp", challenge())).rejects.toThrow(
      `resource ${fake.base} is not https://127.0.0.2:1/mcp`,
    );

    const { fetch: remote } = remoteAs("https://as.example", "http://as.example/authorize");
    await expect(discover(remote, "https://mcp.example/mcp", "")).rejects.toThrow(
      "http://as.example/authorize: OAuth needs https (or a loopback address) for authorization endpoints",
    );
  });

  it("refuses a plain-http authorization server before fetching its metadata", async () => {
    const { fetch: remote, seen } = remoteAs("http://as.example", "https://as.example/authorize");

    await expect(discover(remote, "https://mcp.example/mcp", "")).rejects.toThrow(
      "http://as.example: OAuth needs https (or a loopback address) for the authorization server",
    );
    expect(seen).toEqual(["https://mcp.example/.well-known/oauth-protected-resource"]);
  });

  it("refuses a plain-http MCP server or resource metadata URL before any request", async () => {
    const { fetch: remote, seen } = remoteAs("https://as.example", "https://as.example/authorize");

    await expect(discover(remote, "http://mcp.example/mcp", "")).rejects.toThrow(
      "http://mcp.example/mcp: OAuth needs https (or a loopback address) for the MCP server",
    );
    const named =
      'Bearer resource_metadata="http://mcp.example/.well-known/oauth-protected-resource"';
    await expect(discover(remote, "https://mcp.example/mcp", named)).rejects.toThrow(
      "http://mcp.example/.well-known/oauth-protected-resource: OAuth needs https (or a loopback address) for the resource metadata",
    );
    expect(seen).toEqual([]);
  });

  it("names the URL when the metadata cannot be fetched", async () => {
    const down = (async () => {
      throw new TypeError("fetch failed");
    }) as typeof fetch;

    await expect(discover(down, "https://mcp.example/mcp", "")).rejects.toThrow(
      "GET https://mcp.example/.well-known/oauth-protected-resource: fetch failed",
    );
  });

  it("refuses AS metadata whose issuer is not the authorization server (RFC 8414 §3.3)", async () => {
    fake.badIssuer();

    await expect(discover(fake.fetch, fake.url, challenge())).rejects.toThrow(
      `${fake.base}/.well-known/oauth-authorization-server: issuer https://evil.example is not ${fake.base}`,
    );
  });
});

// A PRM on https://mcp.example naming `as`, whose metadata lists `authorize`.
function remoteAs(as: string, authorize: string): { fetch: typeof fetch; seen: string[] } {
  const seen: string[] = [];
  const remote = (async (input: string | URL | Request) => {
    const url = String(input);
    seen.push(url);
    if (url.endsWith("oauth-protected-resource")) {
      return Response.json({ resource: "https://mcp.example", authorization_servers: [as] });
    }
    return Response.json({
      issuer: as,
      authorization_endpoint: authorize,
      token_endpoint: `${as}/token`,
      registration_endpoint: `${as}/register`,
      code_challenge_methods_supported: ["S256"],
    });
  }) as typeof fetch;
  return { fetch: remote, seen };
}

describe("authorizeUrl", () => {
  it("carries every parameter, the S256 challenge of the verifier and the PRM resource verbatim", () => {
    const found = {
      resource: "https://tree-memory.fastmcp.app",
      issuer: "https://tree-memory.fastmcp.app",
      endpoints: {
        authorization: "https://tree-memory.fastmcp.app/oauth2/authorize",
        token: "t",
        registration: "r",
      },
      scope: "openid profile email offline_access",
    };
    const client = {
      client_id: "c-1",
      client_secret: null,
      redirect_uri: "http://127.0.0.1:51823/callback",
      registered_at: "x",
    };

    const url = authorizeUrl(found, client, "the-verifier", "the-state");

    expect(url).toBe(
      "https://tree-memory.fastmcp.app/oauth2/authorize?response_type=code&client_id=c-1" +
        "&redirect_uri=http%3A%2F%2F127.0.0.1%3A51823%2Fcallback" +
        `&code_challenge=${sha256("the-verifier")}&code_challenge_method=S256&state=the-state` +
        "&resource=https%3A%2F%2Ftree-memory.fastmcp.app&scope=openid+profile+email+offline_access",
    );
    expect(challengeOf("the-verifier")).toBe(sha256("the-verifier"));
  });
});

describe("McpAuth.login against the fake AS", () => {
  it("registers a public loopback client, prints the URL, exchanges the code and stores the file", async () => {
    const { ui, lines, visits } = browser();
    const a = auth();

    await a.login(challenge(), ui);

    expect(fake.registrations).toEqual([
      {
        client_name: "tree-agent",
        redirect_uris: [expect.stringMatching(/^http:\/\/127\.0\.0\.1:\d+\/callback$/)],
        grant_types: ["authorization_code", "refresh_token"],
        response_types: ["code"],
        token_endpoint_auth_method: "none",
      },
    ]);
    const redirect = fake.registrations[0].redirect_uris[0];
    const [q] = fake.authorizations;
    expect(q).toEqual({
      response_type: "code",
      client_id: "client-1",
      redirect_uri: redirect,
      code_challenge: expect.any(String),
      code_challenge_method: "S256",
      state: expect.stringMatching(/^[\w-]{43}$/),
      resource: fake.base,
      scope: "email offline_access openid profile",
    });
    expect(lines).toEqual([
      `mcp "fake": log in at ${fake.base}/oauth2/authorize?${new URLSearchParams(q)}`,
    ]);
    const [exchange] = fake.tokenRequests;
    expect(exchange).toEqual({
      grant_type: "authorization_code",
      code: expect.stringMatching(/^code-/),
      redirect_uri: redirect,
      code_verifier: expect.stringMatching(/^[\w-]{86}$/),
      client_id: "client-1",
      resource: fake.base,
    });
    expect(sha256(exchange?.code_verifier ?? "")).toBe(q?.code_challenge);
    const page = (await visits[0]) as Response;
    expect(page.status).toBe(200);
    expect(await page.text()).toContain(
      "tree-agent: you are logged in to fake. You can close this tab.",
    );
    const saved = stored();
    expect(saved).toEqual({
      url: fake.url,
      issuer: fake.base,
      endpoints: {
        authorization: `${fake.base}/oauth2/authorize`,
        token: `${fake.base}/oauth2/token`,
        registration: `${fake.base}/oauth2/register`,
      },
      resource: fake.base,
      client: {
        client_id: "client-1",
        client_secret: null,
        redirect_uri: redirect,
        registered_at: expect.stringMatching(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/),
      },
      tokens: {
        access_token: fake.issued[0],
        refresh_token: expect.stringMatching(/^rt-/),
        expires_at: expect.any(String),
        scope: "openid profile email offline_access",
      },
    });
    const ttl = Date.parse(saved.tokens.expires_at) - Date.now();
    expect(ttl).toBeGreaterThan(3_590_000);
    expect(ttl).toBeLessThanOrEqual(3_600_000);
    expect(statSync(file()).mode & 0o777).toBe(0o600);
    expect(statSync(join(tmp(), "mcp-auth")).mode & 0o777).toBe(0o700);
    expect(await a.token()).toBe(fake.issued[0]);
  });

  it("accepts expires_in as a string, and an answer without one expires in 1 h", async () => {
    for (const value of ["3600", undefined]) {
      fake.expiresIn(value);

      await auth(undefined, true).login(challenge(), browser().ui);

      const ttl = Date.parse(stored().tokens.expires_at) - Date.now();
      expect(ttl).toBeGreaterThan(3_590_000);
      expect(ttl).toBeLessThanOrEqual(3_600_000);
      expect(stored().tokens.access_token).toBe(fake.issued.at(-1));
    }
  });

  it("a token answer it cannot read names the offending field", async () => {
    fake.expiresIn("soon");

    await expect(auth().login(challenge(), browser().ui)).rejects.toThrow(
      'mcp "fake": token request: unexpected expires_in',
    );
  });

  it("writes the registration with tokens: null before the browser step", async () => {
    let atBrowser: unknown;
    const { ui } = browser(async (url) => {
      atBrowser = stored();
      return fake.browse(url);
    });

    await auth().login(challenge(), ui);

    expect(atBrowser).toMatchObject({ client: { client_id: "client-1" }, tokens: null });
    expect(stored().tokens).not.toBeNull();
  });

  it("a wrong state is answered 400 and the wait goes on; the right one completes", async () => {
    let bad: Response | undefined;
    const { ui } = browser(async (url) => {
      bad = await fake.browse(url, (location) => location.searchParams.set("state", "forged"));
      return fake.browse(url);
    });

    await auth().login(challenge(), ui);

    expect(bad?.status).toBe(400);
    expect(fake.tokenRequests.map((r) => r.grant_type)).toEqual(["authorization_code"]);
    expect(stored().tokens.access_token).toBe(fake.issued[0]);
  });

  // server.close() alone (Node >= 19) drops only idle sockets; one
  // mid-request (a favicon fetch still arriving) needs closeAllConnections.
  it("a browser's keep-alive and mid-request sockets are closed with the loopback server", async () => {
    const agent = new Agent({ keepAlive: true });
    const sockets: Socket[] = [];
    const closed: Promise<unknown>[] = [];
    const track = (s: Socket) => {
      sockets.push(s);
      closed.push(once(s, "close"));
    };
    let port = 0;
    const { ui } = browser(async (url) => {
      const first = await fake.fetch(url, { redirect: "manual" });
      const location = new URL(first.headers.get("location") ?? "");
      port = Number(location.port);
      const partial = connect(port, "127.0.0.1");
      partial.on("error", () => {});
      track(partial);
      await once(partial, "connect");
      partial.write("GET /favicon.ico HTTP/1.1\r\nhost: 127.0.0.1\r\n");
      await new Promise<void>((resolve, reject) => {
        const req = request(location, { agent }, (res) => {
          res.resume();
          res.on("end", resolve);
        });
        req.on("socket", track);
        req.on("error", reject);
        req.end();
      });
    });

    await auth().login(challenge(), ui);

    let timer: NodeJS.Timeout | undefined;
    const bound = new Promise((resolve) => {
      timer = setTimeout(() => resolve("still open 1 s after login"), 1_000);
    });
    expect(await Promise.race([Promise.all(closed).then(() => "closed"), bound])).toBe("closed");
    clearTimeout(timer);
    expect(sockets).toHaveLength(2);
    const refused = connect(port, "127.0.0.1");
    const [err] = (await once(refused, "error")) as NodeJS.ErrnoException[];
    expect(err?.code).toBe("ECONNREFUSED");
    agent.destroy();
  });

  it("an error= redirect ends the wait with the error_description", async () => {
    fake.denyNext();
    const { ui } = browser();

    await expect(auth().login(challenge(), ui)).rejects.toThrow(
      'mcp "fake": login failed: The user declined',
    );
    expect(stored().tokens).toBeNull();
  });

  it("an exchange answered invalid_client deletes the file, so the next login registers again", async () => {
    const { ui } = browser((url) => fake.browse(url, () => fake.rejectClient()));

    await expect(auth().login(challenge(), ui)).rejects.toThrow(
      'mcp "fake": token request failed: invalid_client: unknown client',
    );

    expect(() => stored()).toThrow("ENOENT");
    await auth().login(challenge(), browser().ui);
    expect(fake.registrations).toHaveLength(2);
  });

  it("re-login rebinds the persisted port with the same client: no second registration", async () => {
    // A login aborted at the browser step (the fake browser never comes).
    const first = auth();
    const signal = AbortSignal.timeout(300);
    await expect(first.login(challenge(), { notify: () => {}, signal })).rejects.toThrow(
      'mcp "fake": login cancelled',
    );
    const kept = stored();
    expect(kept.tokens).toBeNull();

    await auth().login(challenge(), browser().ui);

    expect(fake.registrations).toHaveLength(1);
    expect(fake.authorizations.map((q) => q.redirect_uri)).toEqual([kept.client.redirect_uri]);
    expect(stored().client).toEqual(kept.client);
    expect(stored().tokens.access_token).toBe(fake.issued[0]);
  });

  it("re-registers on a fresh port when the persisted port is taken (EADDRINUSE)", async () => {
    await loggedIn();
    const port = Number(new URL(stored().client.redirect_uri).port);
    const squatter = createServer();
    servers.push(squatter);
    await new Promise<void>((resolve) => squatter.listen(port, "127.0.0.1", resolve));

    await auth(undefined, true).login(challenge(), browser().ui);

    expect(fake.registrations).toHaveLength(2);
    const now = Number(new URL(stored().client.redirect_uri).port);
    expect(now).not.toBe(port);
    expect((squatter.address() as AddressInfo).port).toBe(port);
    expect(stored().client.client_id).toBe("client-2");
  });

  it("is bounded by LOGIN_TIMEOUT_MS: pending at 299 999 ms, the exact error at 300 000 ms", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const lines: string[] = [];
    let error: Error | undefined;

    const pending = auth()
      .login(challenge(), { notify: (l) => lines.push(l) })
      .catch((e: Error) => {
        error = e;
      });
    await until(() => lines.length === 1);
    await vi.advanceTimersByTimeAsync(LOGIN_TIMEOUT_MS - 1);
    const early = error;
    await vi.advanceTimersByTimeAsync(1);
    await pending;

    expect(LOGIN_TIMEOUT_MS).toBe(300_000);
    expect(early).toBeUndefined();
    expect(error?.message).toBe(
      'mcp "fake": login timed out after 5 min; run tree-agent --mcp-login fake to log in',
    );
    expect(stored()).toMatchObject({ client: { client_id: "client-1" }, tokens: null });
  });
});

describe("McpAuth.token / refresh", () => {
  it("no file: no token, no refresh, no request", async () => {
    const a = auth();

    expect(await a.token()).toBeUndefined();
    expect(await a.refresh()).toBeUndefined();
    expect(fake.tokenRequests).toEqual([]);
  });

  it("refresh sends refresh_token, client_id and resource, and stores the new tokens", async () => {
    const a = await loggedIn();
    const old = stored().tokens.refresh_token;

    const fresh = await a.refresh();

    expect(fresh).toBe(fake.issued[1]);
    expect(fake.tokenRequests[1]).toEqual({
      grant_type: "refresh_token",
      refresh_token: old,
      client_id: "client-1",
      resource: fake.base,
    });
    expect(stored().tokens.access_token).toBe(fresh);
    expect(stored().tokens.refresh_token).not.toBe(old);
  });

  it("a refresh answer without refresh_token keeps the old one", async () => {
    const a = await loggedIn();
    const old = stored().tokens.refresh_token;
    fake.omitRefreshToken();

    await a.refresh();

    expect(stored().tokens.refresh_token).toBe(old);
    expect(stored().tokens.access_token).toBe(fake.issued[1]);
  });

  it("an expired access token is refreshed before it is sent", async () => {
    await loggedIn();
    const data = stored();
    data.tokens.expires_at = new Date(Date.now() - 1000).toISOString();
    const { writeAuthFile } = await import("../../src/mcp/auth-store.ts");
    await writeAuthFile(file(), data);

    expect(await auth().token()).toBe(fake.issued[1]);
    expect(fake.tokenRequests.at(-1)?.grant_type).toBe("refresh_token");
  });

  it("a rejected refresh answers undefined and logs why, without any token", async () => {
    const { log, lines } = sink();
    const a = await loggedIn(log);
    fake.rejectRefresh();

    expect(await a.refresh()).toBeUndefined();
    expect(lines.join("")).toContain(
      'debug: mcp "fake": refresh failed: token request failed: invalid_grant: refresh token expired',
    );
  });

  it("a refresh answered invalid_client deletes the file; the next login registers again", async () => {
    const a = await loggedIn();
    fake.rejectClient();

    expect(await a.refresh()).toBeUndefined();
    expect(() => stored()).toThrow("ENOENT");

    await a.login(challenge(), browser().ui);
    expect(fake.registrations).toHaveLength(2);
    expect(stored().client.client_id).toBe("client-2");
  });

  it("concurrent refreshes share one token request", async () => {
    const a = await loggedIn();

    const [x, y] = await Promise.all([a.refresh(), a.refresh()]);

    expect(x).toBe(y);
    expect(fake.tokenRequests.filter((r) => r.grant_type === "refresh_token")).toHaveLength(1);
  });

  it("fresh (--mcp-login) ignores the cached tokens", async () => {
    await loggedIn();

    const a = auth(undefined, true);

    expect(await a.token()).toBeUndefined();
    expect(await a.refresh()).toBeUndefined();
  });

  it("ignores a file written for another url", async () => {
    await loggedIn();
    const other = new McpAuth({
      name: "fake",
      url: `${fake.url}/v2`,
      file: file(),
      fetch: fake.fetch,
    });

    expect(await other.token()).toBeUndefined();
  });
});

describe("secrets never reach the log", () => {
  it("no access/refresh token, code or code_verifier in any line of a full flow at debug", async () => {
    const { log, lines } = sink();
    const a = await loggedIn(log);
    await a.refresh();
    fake.rejectRefresh();
    await a.refresh();

    const secrets = [
      ...fake.issued,
      ...fake.tokenRequests.flatMap((r) => [r.code, r.code_verifier, r.refresh_token]),
    ].filter(Boolean) as string[];
    expect(secrets.length).toBeGreaterThan(4);
    expect(lines.length).toBeGreaterThan(2);
    for (const secret of secrets) expect(lines.join("")).not.toContain(secret);
  });
});

describe("hasAuthorization", () => {
  it("matches the header name case-insensitively", () => {
    expect(hasAuthorization({ authorization: "x" })).toBe(true);
    expect(hasAuthorization({ AUTHORIZATION: "x" })).toBe(true);
    expect(hasAuthorization({ "X-Team": "tree" })).toBe(false);
  });
});
