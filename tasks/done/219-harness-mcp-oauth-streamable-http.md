---
id: 219-harness-mcp-oauth-streamable-http
status: done
feature: tree-agent-harness
---

# MCP authorization for streamable-HTTP servers: the hosted `tree-memory` works from the plain `{"type": "http", "url": …}` entry — RFC 9728/8414 discovery, dynamic registration, PKCE S256 on a loopback redirect, token file under `mcp-auth/`, refresh, `--mcp-login` / `--mcp-logout`

Tags: `harness`, `mcp`, `auth`, `enhancement`
Depends on: None (202–218 ordered before it; 218 edits the same `cli.ts` / `settings.ts` / README / spec / ADR files, so this task lands after it)
Blocks: —
Implements: ADR-016 §3 as amended by this task (reverses the "OAuth for MCP" non-goal; one dated amendment, text below)

## Scope

The hosted memory server (`https://tree-memory.fastmcp.app/mcp`, Prefect Horizon / FastMCP Cloud) is OAuth-only:
there is no static client token (`.env.example` lines 52–58). The harness today can reach it only with a
hand-pasted `Authorization` header. The human's own client config (`.pi/mcp.json`) is the plain

```json
"tree-memory": { "type": "http", "url": "https://tree-memory.fastmcp.app/mcp" }
```

and this task makes exactly that entry work in `~/.tree-agent/mcp.json`: the standard MCP authorization flow
(OAuth 2.1), a browser login once, a cached token afterwards. This reverses the spec's and ADR-016's "OAuth
for MCP" non-goal; the ADR amendment, the spec and the README are edited in this task.

**Facts probed on 2026-10-25, unauthenticated (cite them; do not re-derive):**

- `POST https://tree-memory.fastmcp.app/mcp` without a token → HTTP 401, `WWW-Authenticate: Bearer
  realm="FastMCP", resource_metadata="https://tree-memory.fastmcp.app/.well-known/oauth-protected-resource"`,
  body `{"error":"invalid_request","error_description":"Bearer token required"}`.
- `GET https://tree-memory.fastmcp.app/.well-known/oauth-protected-resource` →
  `{"resource":"https://tree-memory.fastmcp.app","authorization_servers":["https://tree-memory.fastmcp.app"],
  "bearer_methods_supported":["header"]}`.
- `GET https://tree-memory.fastmcp.app/.well-known/oauth-authorization-server` → issuer
  `https://tree-memory.fastmcp.app`, `authorization_endpoint …/oauth2/authorize`, `token_endpoint
  …/oauth2/token`, `registration_endpoint …/oauth2/register` (dynamic client registration),
  `grant_types_supported: ["authorization_code","refresh_token"]`, `code_challenge_methods_supported: ["S256"]`,
  `response_types_supported: ["code"]`, `response_modes_supported: ["query"]`,
  `token_endpoint_auth_methods_supported: ["none","client_secret_post","client_secret_basic"]`,
  `scopes_supported: ["email","offline_access","openid","profile"]`. The upstream identity provider is Prefect
  Horizon (`auth.horizon.prefect.io`); the client only ever talks to the resource's own AS above.
- `apps/memory/src/tree/mcp/client.py` documents that a personal Horizon API key `fmcp_…` is reported to work
  as a plain bearer (`Authorization: Bearer fmcp_…`); `.env.example` says the Horizon key is a platform key,
  not a client credential. UNVERIFIED by this project's harness. The harness already sends static `headers`,
  so that path needs one hedged README sentence and no code.
- The protocol to implement is the MCP authorization spec,
  https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization — OAuth 2.1, protected-resource
  metadata (RFC 9728), AS metadata (RFC 8414), dynamic client registration (RFC 7591), PKCE S256 (RFC 7636),
  the `resource` indicator (RFC 8707). **The SWE reads it through the `tech-docs` skill (context7 first, then
  the `llms.txt` index) before coding**, as 208 did for transports.

1. **SDK or hand-rolled — decided here, recorded in the ADR amendment.** ADR-016's Consequences name "a
   second MCP client need (resources, OAuth) → revisit the SDK"; this is that revisit. **Recommendation:
   hand-roll (~300 lines, zero new dependencies), do not adopt `@modelcontextprotocol/sdk`.** Reasons: every
   step is stdlib — `node:crypto` (`randomBytes`, `createHash("sha256")` for S256), `node:http` for the
   loopback listener, `fetch` for metadata, registration and the token endpoint; the SDK's `auth()` helper
   still requires the caller to implement an `OAuthClientProvider` (token storage, redirect handling, browser
   open) of about the same size, is built to be driven by the SDK's own transport classes rather than a
   foreign one, and would put the SDK's whole dependency tree (expected to include its own `zod`, a PKCE
   helper, an SSE client and server-side helpers — the SWE confirms the actual list via context7) under a
   ~300-line transport the chapter lists as "no SDK, the wire is the lesson" (ADR-016 §3, spec §1 table). *Upgrade trigger:* a second OAuth-protected server whose AS quirk the
   flow cannot absorb in a few lines, or MCP resources/prompts joining the scope. The SWE confirms via
   context7 (a) whether the SDK's `client/auth` module is standalone-importable under Node type stripping and
   (b) what it pulls in, and records both findings in the implementation log; only a finding that contradicts
   the reasons above justifies the SDK, and then the ADR amendment text changes accordingly.

2. **`src/mcp/config.ts`** — the HTTP branch accepts `type: z.literal("http").optional()` and the stdio branch
   `type: z.literal("stdio").optional()` (today a `type` key is silently dropped by the non-strict object — the
   human's entry already parses; this makes `"type": "sse"` an error instead of a silent streamable-HTTP
   attempt). A `type` that does not match the branch → `type must be http or stdio` through the existing
   error path naming the server. The spec §10.1 listing and the README schema block gain the two lines.

3. **`src/mcp/auth.ts` (new; the flow)** — `McpAuth` for one HTTP server, created by `McpClient` for every
   HTTP server whose configured `headers` carry NO `Authorization` (a configured header wins: the `fmcp_`
   path and any other static token never trigger the flow, and a 401 is then today's plain `HTTP 401` error).
   Steps, each a small function the tests hit on its own:
   - **Discovery.** From the 401's `WWW-Authenticate` take `resource_metadata` (fallback when absent: the
     origin's `/.well-known/oauth-protected-resource`); GET it → `{resource, authorization_servers[0]}`; GET
     `<as>/.well-known/oauth-authorization-server` → endpoints, `code_challenge_methods_supported` (must
     include `S256`), `scopes_supported`, `registration_endpoint`.
   - **Registration** (RFC 7591) when the token file has no `client_id` for this `url`: POST
     `{client_name: "tree-agent", redirect_uris: ["http://127.0.0.1:<port>/callback"], grant_types:
     ["authorization_code", "refresh_token"], response_types: ["code"], token_endpoint_auth_method: "none"}`
     → `client_id` (+ `client_secret` if the AS insists; stored, sent with `client_secret_post` then). The
     port is chosen at registration by binding `127.0.0.1:0` and is PERSISTED with the `redirect_uri`; on
     every later login the listener rebinds that same port; only `EADDRINUSE`, or the AS rejecting the
     client (`invalid_client`), triggers a fresh port + re-registration. **The registration is written to the
     token file as soon as it is obtained — before the browser step — with `tokens: null`**; otherwise every
     timed-out or cancelled login would register one more client at the AS.
   - **Authorization** (PKCE S256, RFC 7636 + RFC 8707): `code_verifier` 64 random bytes base64url;
     `code_challenge = base64url(sha256(verifier))`; `state` 32 random bytes base64url; URL =
     `authorization_endpoint ? response_type=code & client_id & redirect_uri & code_challenge &
     code_challenge_method=S256 & state & resource=<PRM resource, verbatim: https://tree-memory.fastmcp.app>
     & scope=<PRM scopes_supported, else AS scopes_supported, joined by spaces; omitted when neither>` (with
     the hosted server: `openid profile email offline_access`, so a refresh token is issued). The loopback
     listener answers `/callback?code&state`: a mismatched `state` → 400 and the wait goes on; a matching one
     → a 200 HTML page "tree-agent: you are logged in to <server>. You can close this tab." and the
     listener closes. `error=` in the query → the wait fails with `error_description`.
   - **Token exchange**: POST `token_endpoint` (form-encoded) `grant_type=authorization_code, code,
     redirect_uri, client_id, code_verifier, resource` → `{access_token, refresh_token?, expires_in?, scope?}`.
     **Refresh**: `grant_type=refresh_token, refresh_token, client_id, resource`; a refreshed response that
     omits `refresh_token` keeps the old one.
   - **When each runs.** A cached, unexpired `access_token` is sent as `Authorization: Bearer …` on every
     request. A 401, or `expires_at` in the past, → silent refresh and one retry of the request; no refresh
     token, or the refresh fails → the interactive flow (below), subject to the TTY rule; a second 401 after
     a fresh token → a plain error (no loop).
   - **Browser.** When allowed to prompt: `spawn("open" | "xdg-open" | "cmd /c start", [url], {detached,
     stdio: "ignore"}).unref()` by platform, failure ignored; the URL is ALWAYS shown as well (one line on
     stderr: `mcp "<server>": log in at <url>` — the TUI is not drawn yet when `startMcp` runs, so stderr is
     the channel, as for today's startup warnings).
   - **Bound.** The interactive wait is `LOGIN_TIMEOUT_MS = 300_000` (5 min; `McpClientOptions.loginTimeoutMs`
     for tests, no settings key). On timeout: one warning `mcp "<server>": login timed out after 5 min; run
     tree-agent --mcp-login <server> to log in` and the server is kept un-connected like today's failed
     servers (its next call retries, under the same rules).

4. **Headless must never hang on a login URL (the TTY rule).** The interactive flow (URL + wait) runs only when
   stdin AND stderr are TTYs, or when the run IS `--mcp-login`. Otherwise (piped stdin, redirected stderr, CI)
   the server is skipped with exactly one warning: `mcp "<server>": needs a login; run tree-agent --mcp-login
   <server> once, then re-run` — exit code and the rest of the run unaffected (the other servers connect, the
   prompt runs, `--json` output stays valid JSONL because the warning is on stderr). `Stdin.isTTY` already
   flows through `CliDeps`; add stderr's TTY the same way (`CliDeps.stderrIsTTY?`, default `process.stderr.isTTY
   === true`) so `cli.test.ts` covers both branches. The TUI always has TTYs. `--no-mcp` is unaffected.

5. **Relative to `mcp.initializeTimeoutMs` (30 s).** The 401 arrives on the first POST — inside `initialize`'s
   JSON-RPC timer. The login must NOT count against it: `HttpTransport.send` turns a 401 whose
   `WWW-Authenticate` names a resource (and no configured `Authorization`) into a typed `McpAuthRequired`
   rejection; `McpClient.#handshake` catches it, runs `McpAuth.login()` outside any JSON-RPC timer (bounded by
   `LOGIN_TIMEOUT_MS`), then retries the handshake ONCE with a fresh `initialize` timer. `connectMcpServers`
   is already `Promise.all`, so the other servers connect meanwhile; the session opens when every server has
   connected, been skipped or failed (a user who is logging in is waiting on themselves). A mid-session 401
   (expired token) is handled inside `send`: refresh + one retry, silent; only a failed refresh surfaces as
   `McpAuthRequired`, which marks the transport dropped so the next call re-handshakes.

6. **Token file (`src/mcp/auth-store.ts` or inside `auth.ts`; the SWE decides, one vitest file per module).**
   `$TREE_AGENT_HOME/mcp-auth/<server-key>.json` where `<server-key>` is the mcp.json key with
   `[^A-Za-z0-9._-]` → `_`; the directory is created `0700`, the file written `0600` (atomic: write
   `<file>.tmp`, rename). Content, every date ISO 8601 UTC:

   ```json
   { "url": "https://tree-memory.fastmcp.app/mcp", "issuer": "https://tree-memory.fastmcp.app",
     "endpoints": { "authorization": "…", "token": "…", "registration": "…" },
     "resource": "https://tree-memory.fastmcp.app",
     "client": { "client_id": "…", "client_secret": null, "redirect_uri": "http://127.0.0.1:51823/callback",
                 "registered_at": "2026-10-25T10:00:00.000Z" },
     "tokens": { "access_token": "…", "refresh_token": "…", "expires_at": "2026-10-25T11:00:00.000Z",
                 "scope": "openid profile email offline_access" } }
   ```

   `tokens` is `null` between registration and the first successful exchange (a file with `client` and
   `tokens: null` means "registered, not logged in"; `--mcp-logout` deletes it whole). A file whose `url`
   differs from the configured one is ignored and overwritten after a fresh login (a retargeted server never
   reuses a token). Nothing is ever written to `mcp.json`, the repo or the logs: token
   values and `Authorization` header values are never logged (208's rule, extended).

7. **`src/cli.ts` — two flags, `parseArgs` style, two new `Mode` kinds, both resolved in `resolveMode` and
   returning BEFORE `requireApiKey` (no `OPENROUTER_API_KEY`, no model check, no session):**
   - `--mcp-login <server>`: reads `mcp.json` (user, plus project when the cwd is trusted or `--trust`), requires
     the server to be an HTTP one (`"<server>" is not an HTTP server in mcp.json` → exit 1; unknown name →
     `no MCP server "<server>" in mcp.json; known: …` → exit 1), runs the flow regardless of TTYs (prints the
     URL, opens the browser when a TTY), stores the file, prints `logged in to "<server>" (<file>)`, exit 0;
     a timeout, an `error=` callback or a token error → the message, exit 1. A server with a configured
     `Authorization` header → `"<server>" uses a static Authorization header; nothing to log in to`, exit 0.
   - `--mcp-logout <server>`: deletes the token file and prints `logged out of "<server>" (<file>)`; no file →
     `not logged in to "<server>"`; both exit 0. (One-liner, so it is in.)
   - USAGE gains the two lines; the README flag table gains two rows; `--help` still exits 0.

8. **Tests** — `tests/helpers/oauth-http.ts` (new, next to `mcp-http.ts`): one `http.createServer` on
   `127.0.0.1:0` playing BOTH the protected MCP server and its AS: `/mcp` answers 401 + the exact
   `WWW-Authenticate` above until a valid bearer arrives, then behaves like `startFakeMcpHttp`'s `/json`;
   `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server` serve the hosted
   server's metadata with the fake's own URLs; `/oauth2/register` records the body and answers a
   `client_id`; `/oauth2/authorize` records the query and 302-redirects to `redirect_uri?code=…&state=…` (the
   test plays the browser by fetching that URL with `redirect: "manual"` and then GETting the loopback
   `Location`); `/oauth2/token` verifies `grant_type`, `client_id`, `redirect_uri`, `resource` and
   `base64url(sha256(code_verifier)) === code_challenge`, answers `{access_token, refresh_token, expires_in}`;
   knobs: `expireAccessToken()`, `rejectRefresh()`, `rejectClient()`, `noRegistration()`, `noScopes()`.
   No live network in unit tests. Files and what they cover:
   - `tests/mcp/auth.test.ts` (and `auth-store.test.ts` if split): discovery from the header and the
     fallback; registration body (`token_endpoint_auth_method: "none"`, the loopback `redirect_uri`,
     both grant types); the authorize URL (every parameter above, `resource` = the PRM value, scopes from the
     PRM/AS, S256 challenge of the stored verifier); a wrong `state` → 400 and still waiting; the code
     exchange; `expires_at` computed from `expires_in`; refresh on 401 keeps the old `refresh_token` when the
     answer has none; a failed refresh → `McpAuthRequired`; the timeout (fake timers) → the exact warning; the
     port rebound on re-login, re-registration only on `EADDRINUSE` / `invalid_client`; the file is `0600` in a
     `0700` dir, atomic, ignored when `url` differs; no token value in any `log` call (spy).
   - `tests/mcp/transport-http.test.ts`: a configured `Authorization` header → no flow, 401 is a plain error;
     bearer attached once a token exists; 401 → refresh → one retry; second 401 → error, no loop.
   - `tests/mcp/client.test.ts`: the login runs outside the `initialize` timer (fake AS whose authorize step
     takes longer than `initializeTimeoutMs`, handshake still succeeds); the retry after login is one fresh
     `initialize`; the TTY rule's skip path (no login, one warning, the client kept).
   - `tests/mcp/config.test.ts`: the human's exact `.pi/mcp.json` entry parses; `type: "sse"` → `type must be
     http or stdio`; `type: "stdio"` with `url` → the same message.
   - `tests/cli.test.ts`: `--mcp-login` end to end against the fake (no `OPENROUTER_API_KEY` in `env`, exit 0,
     the file written, the `logged in` line); `--mcp-logout` both branches; headless with piped stdin and an
     un-logged-in server → one warning naming `--mcp-login`, the prompt still answered, exit 0; TTY stdin +
     stderr → the URL line and the wait (fake AS auto-completes); `--json` keeps stdout valid JSONL.
   - Mutation probes to report: PKCE verifier not hashed (1), `state` not checked (1), `resource` dropped
     from the token request (1), header precedence removed (1), skip rule removed (1), 0600 → 0644 (1).

9. **Live verification (agent-runnable, read-mostly, scratch `TREE_AGENT_HOME` only):** against the real
   hosted server, with the entry above in `$TREE_AGENT_HOME/mcp.json`, `tree-agent --mcp-login tree-memory`
   prints `mcp "tree-memory": log in at https://tree-memory.fastmcp.app/oauth2/authorize?…` with
   `response_type=code`, a `client_id` (dynamic registration against the real AS — this is ONE write, a
   client record; acceptable, and it is why the scratch `TREE_AGENT_HOME` is mandatory), `redirect_uri=
   http%3A%2F%2F127.0.0.1%3A<port>%2Fcallback`, `code_challenge_method=S256`, `resource=https%3A%2F%2F
   tree-memory.fastmcp.app`, `scope=openid+profile+email+offline_access` (order aside); the SWE/Tester then
   Ctrl+C (exit 1; the file exists with `client` filled and `tokens: null` — no bearer was issued; a second
   `--mcp-login` reuses that `client_id` and port instead of registering again). A headless `tree-agent "say ok"` with piped stdin and the same config →
   one `needs a login; run tree-agent --mcp-login tree-memory` warning, `ok`, exit 0. The browser login itself
   is `[HUMAN]`.

10. **Docs (edited in this task, with the wording below where it is given):**
    - `docs/adrs/016_tree_agent_harness.md`: append ONE section, verbatim (it follows 218's amendment):

      > ## Amendment (2026-10-25) — MCP authorization for streamable-HTTP servers is in scope; the client stays hand-rolled
      >
      > Amends §3 (the MCP client) and the non-goals line; ADR-008 §5 (MCP is the only harness↔memory boundary)
      > is confirmed, and the diagram's `mcp/client.ts … no OAuth` label reads `OAuth 2.1 for HTTP servers`.
      > **Context:** the hosted `tree-memory` server (Prefect Horizon / FastMCP Cloud) is OAuth-only — a 401
      > with `WWW-Authenticate … resource_metadata`, RFC 9728 protected-resource metadata, an RFC 8414 AS at the
      > same origin with dynamic registration, PKCE S256 and `authorization_code` + `refresh_token` — and the
      > owner runs it from the plain `{"type": "http", "url": …}` entry in other clients. "A second MCP client
      > need (resources, OAuth) → revisit the SDK" (Consequences) is therefore revisited. **Decision:** implement
      > the MCP authorization spec (2025-06-18) by hand in `mcp/auth.ts` (~300 lines, stdlib only: `crypto`,
      > `http` for the loopback redirect, `fetch`), not with `@modelcontextprotocol/sdk` — the SDK's `auth()`
      > still needs a caller-written `OAuthClientProvider` of comparable size, is shaped for the SDK's own
      > transports, and would hang its dependency tree under a transport the chapter lists as "no SDK". Tokens
      > and the registered client live in `$TREE_AGENT_HOME/mcp-auth/<server>.json` (0600), never in `mcp.json`
      > or the repo; a configured `Authorization` header always wins and disables the flow. The interactive
      > step (URL + browser + wait, 5 min) runs only on a TTY or under `tree-agent --mcp-login <server>`;
      > headless runs without a TTY skip the server with one warning, so CI never hangs. The login runs outside
      > the `initialize` timer. The hosted server is a user-level `~/.tree-agent/mcp.json` entry, NOT committed
      > to `.tree-agent/mcp.json`: a repo-root run by anyone not logged in would otherwise warn (or wait) every
      > time, and its tools would duplicate `tree-memory-local`'s. *Upgrade trigger:* a second OAuth server with
      > an AS quirk the flow cannot absorb, or MCP resources/prompts entering scope → the SDK.

    - `docs/notes/tree-agent-harness-spec.md`: §1 table "MCP client" row — "OAuth and SSE transport out of
      scope" → "OAuth 2.1 for streamable-HTTP servers (task 219); the legacy SSE transport out of scope";
      §2 non-goals — remove "OAuth for MCP"; §10.1 — the two `type` lines; a new §10.2.1 "Authorization
      (`mcp/auth.ts`)" with the flow, the TTY rule, the token file shape, `--mcp-login`/`--mcp-logout`, the
      `Authorization`-header precedence and the 5-minute bound; §15 CLI — the two flags; §17 — a `mcp/auth.ts`
      row listing the tests of Scope 8.
    - `apps/harness/README.md`: line 15 "Not built, on purpose" — drop "MCP OAuth"; the usage block and the
      flag table — the two flags; the Config table — `mcp-auth/<server>.json | OAuth tokens and client
      registration per HTTP MCP server (0600)`; the MCP schema block — the `type` lines; the "Timeouts"
      bullet — "no OAuth" → "OAuth 2.1 for HTTP servers (below)"; replace "### The hosted `tree-memory` server"
      with, verbatim:

      > ### The hosted `tree-memory` server
      >
      > The hosted server sits behind Prefect Horizon OAuth. Add it to your user file `~/.tree-agent/mcp.json`
      > (it is not committed to the repo's `.tree-agent/mcp.json`: a repo-root run by someone not logged in
      > would warn or wait every time, and its tools would duplicate `tree-memory-local`'s):
      >
      > ```json
      > { "mcpServers": { "tree-memory": { "type": "http", "url": "https://tree-memory.fastmcp.app/mcp",
      >   "toolExposure": { "search_memory": "direct", "visualize_*": "deferred" } } } }
      > ```
      >
      > `tree-agent` follows the MCP authorization flow: on the server's 401 it discovers the authorization
      > server, registers itself once, and opens your browser (printing the URL on stderr as well) for a
      > one-time Horizon login; the token and the registered client are stored in
      > `~/.tree-agent/mcp-auth/tree-memory.json` (mode 0600) and refreshed silently afterwards. Log in once
      > with `tree-agent --mcp-login tree-memory` (no `OPENROUTER_API_KEY` needed); every later run — TUI or
      > headless — reuses the token. Headless runs without a terminal (piped stdin, CI) never wait for a login:
      > a server that needs one is skipped with `mcp "tree-memory": needs a login; run tree-agent --mcp-login
      > tree-memory once, then re-run`. The interactive wait is bounded at 5 minutes. Log out with
      > `tree-agent --mcp-logout tree-memory` (it deletes the file). Its tools register as
      > `mcp__tree_memory__*`.
      >
      > A static token also works and disables the flow: `"headers": {"Authorization": "Bearer <token>"}`. A
      > personal Horizon API key (`fmcp_…`) is reported to work as that bearer (`apps/memory/src/tree/mcp/
      > client.py`); this is unverified from the harness — `.env.example` describes the key as a platform
      > credential, not a client one. Never commit a token.

      (If the Tester verifies the `fmcp_` path with the human's own key — never with a key read from
      `.env*` by an agent — the last paragraph drops "unverified".)
    - `docs/glossary.md`: add one row and amend one, verbatim:

      > | **MCP login** | The harness's OAuth 2.1 step for a streamable-HTTP MCP server (`mcp/auth.ts`, spec §10.2.1): on a 401 with `WWW-Authenticate … resource_metadata` it discovers the protected-resource and authorization-server metadata (RFC 9728/8414), registers a public client once (RFC 7591, `token_endpoint_auth_method: none`, a loopback `http://127.0.0.1:<port>/callback`), runs an authorization-code grant with PKCE S256 and the `resource` indicator (RFC 8707), stores `{client, tokens}` in `$TREE_AGENT_HOME/mcp-auth/<server>.json` (0600) and refreshes silently. Run on demand by `tree-agent --mcp-login <server>`; undone by `--mcp-logout <server>` (deletes the file). | Interactive (URL + browser + a 5-minute wait) only on a TTY or under `--mcp-login`; a headless run without a TTY skips the server with one warning naming the flag, so CI never hangs. A configured `Authorization` header disables it. Runs outside `mcp.initializeTimeoutMs`. Good: the hosted `tree-memory` from the plain `{"type": "http", "url": …}` entry in `~/.tree-agent/mcp.json`. Bad: a token pasted into a committed `mcp.json`. Hand-rolled, not the MCP SDK (ADR-016 amendment 2026-10-25). |

      and in the **tree-agent** row's Notes, "Talks to memory ONLY through the `tree-memory-local` MCP
      server declared in `.tree-agent/mcp.json`" → "Talks to memory ONLY through MCP servers declared in
      `mcp.json` — the committed `tree-memory-local` (stdio) and, user-level, the hosted `tree-memory` after an
      **MCP login**".
    - `.tree-agent/mcp.json` (committed): UNCHANGED — decision recorded in the amendment and the README.

**Out of scope (intentional):** the legacy HTTP+SSE transport; MCP resources/prompts; OAuth for stdio
servers (none needs it); a settings key for the login timeout (a constant; tests inject it); `client_credentials`
or device-code grants (the AS offers neither); a per-server `scope` override in `mcp.json` (the metadata
advertises what to ask); the fmcp_ static key as a first-class config field (`headers` covers it); a TUI
`/mcp login` command (the TUI is not drawn when servers connect; `--mcp-login` is the one path); committing
the hosted entry to `.tree-agent/mcp.json` (see the decision above).

**Constraints:** never print `OPENROUTER_API_KEY`, any token, `Authorization` header value or other `.env`
secret; never edit `.env`, `.env.prod`, `.env.target`; never modify `apps/memory`; live runs only in scratch
dirs with a fake `TREE_AGENT_HOME` (the real `~/.tree-agent/mcp-auth/` must not be written by an agent); CI
must not get `OPENROUTER_API_KEY`; the cadence is `make harness-lint-fix && make harness-lint-check && make
harness-typecheck && make harness-tests && make pre-commit`; Biome lineWidth 76 in `src/`, 100 in `tests/`;
`agent-loop.ts` stays ≤ 200 lines (180 today; untouched by this task); one vitest file per module; no new
runtime dependency unless the SDK finding of Scope 1 overturns the recommendation (then the ADR amendment
says so).

## Acceptance Criteria

- [x] The human's exact entry `{"type": "http", "url": "https://tree-memory.fastmcp.app/mcp"}` parses;
      `type: "sse"`, or `type: "stdio"` with a `url`, fails with `type must be http or stdio` naming the server.
- [x] Against the fake AS + protected server: the first 401 drives discovery (header `resource_metadata`, and
      the origin fallback when the header has none), registration (`token_endpoint_auth_method: "none"`, the
      loopback `redirect_uri`, both grant types), an authorize URL carrying `response_type=code`, `client_id`,
      `redirect_uri`, `code_challenge` = `base64url(sha256(verifier))`, `code_challenge_method=S256`, `state`,
      `resource` = the PRM `resource` verbatim and `scope` = the advertised scopes; a wrong `state` → 400 and
      the wait continues; the exchange sends `code_verifier` and `resource`; the retried handshake succeeds
      with `Authorization: Bearer <access_token>`; the file exists at `$TREE_AGENT_HOME/mcp-auth/<key>.json`
      with mode `0600` in a `0700` dir and the shape of Scope 6 — written once with `tokens: null` right after
      registration (a login aborted at the browser step leaves it so) and again with the tokens after the
      exchange.
- [x] A later 401 (or `expires_at` passed) refreshes silently and retries once; a refresh answer without
      `refresh_token` keeps the old one; a failed refresh → the interactive flow (TTY) or the skip warning (no
      TTY); a second 401 after a fresh token → one error, no loop; a configured `Authorization` header → no
      discovery, no flow, today's `HTTP 401` error.
- [x] The login runs outside `mcp.initializeTimeoutMs` (an authorize step longer than that timeout still ends
      in a connected client, with exactly two `initialize` requests), is bounded by `LOGIN_TIMEOUT_MS` (fake
      timers: pending at 299 999 ms, the exact timeout warning at 300 000 ms, the client kept, the other fake
      server connected and the session usable), and on re-login rebinds the persisted port, re-registering only
      on `EADDRINUSE` or `invalid_client`.
- [x] TTY rule: headless with piped stdin (or a non-TTY stderr) and an un-logged-in server prints exactly one
      `mcp "<server>": needs a login; run tree-agent --mcp-login <server> once, then re-run`, answers the prompt,
      exits 0, and `--json` stdout is valid JSONL; with TTY stdin and stderr it prints `mcp "<server>": log in
      at <url>` and waits (the fake completes it).
- [x] `tree-agent --mcp-login <server>` works with NO `OPENROUTER_API_KEY` in the environment: unknown server
      → `no MCP server "<server>" in mcp.json; known: …` exit 1; a stdio server → `"<server>" is not an HTTP
      server in mcp.json` exit 1; a static-header server → `… nothing to log in to` exit 0; success → the file
      and `logged in to "<server>" (<file>)` exit 0. `--mcp-logout <server>` deletes the file (`logged out of
      …`) or prints `not logged in to "<server>"`, exit 0 both. `--help` lists both.
- [x] No token, `code_verifier` or `Authorization` value appears in any `log` call or stderr line (spy over the
      whole fake flow at `TREE_AGENT_LOG=debug`).
- [x] Live, scratch `TREE_AGENT_HOME`, the hosted entry in its `mcp.json`: `tree-agent --mcp-login tree-memory`
      prints the real `https://tree-memory.fastmcp.app/oauth2/authorize?…` URL with the parameters of Scope 9
      (paste the URL with `client_id` and `state` redacted into the log), then Ctrl+C → exit 1, the file holds
      `client` and `tokens: null`, and a second `--mcp-login` shows the SAME `client_id` and port (no second
      registration); headless `echo "say ok" | tree-agent` with the same config → the one skip warning, `ok`,
      exit 0.
- [x] [HUMAN] On the human's machine: `tree-agent --mcp-login tree-memory` opens the browser, the Horizon login
      completes, the tab shows "you are logged in", the command prints `logged in to "tree-memory" (…)` and
      exits 0; then headless `tree-agent "search my memory for agents"` (repo root, `--trust`) calls
      `mcp__tree_memory__search_memory` and answers with NO browser and no URL line; a third run after
      `--mcp-logout tree-memory` with piped stdin prints the skip warning instead.
- [x] ADR-016 amendment (verbatim), spec §1/§2/§10.1/§10.2.1/§15/§17, the README sections (verbatim where
      given), the glossary row and the `tree-agent` row amendment are in the diff; the SWE's log records the
      context7 findings on `@modelcontextprotocol/sdk`'s auth module and the decision; `.tree-agent/mcp.json` is
      unchanged.
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green; no new runtime dependency; `agent-loop.ts` untouched.

## User Stories

### Story: First login from the TUI
1. The user adds the plain `tree-memory` entry to `~/.tree-agent/mcp.json` and runs `tree-agent` in a terminal.
2. On startup stderr shows `mcp "tree-memory": log in at https://tree-memory.fastmcp.app/oauth2/authorize?…`
   and the browser opens; the user logs into Horizon; the tab says they are logged in; the TUI opens with
   `mcp__tree_memory__search_memory` in the tool list and `/mcp` shows `tree-memory` connected.

### Story: Every later run is silent
1. The next day the user runs `tree-agent "what did I note about pricing?"`.
2. The cached token is sent (refreshed silently if expired); no URL, no browser; the answer cites memory.

### Story: CI never hangs
1. A pipeline runs `echo "run the tests" | tree-agent --trust` on a machine with the hosted entry configured
   and no token file.
2. One stderr line: `mcp "tree-memory": needs a login; run tree-agent --mcp-login tree-memory once, then
   re-run`; the run proceeds on the other tools and exits 0.

### Story: Logging in on purpose, without a model key
1. On a fresh machine the user runs `tree-agent --mcp-login tree-memory` before setting `OPENROUTER_API_KEY`.
2. The URL prints, the browser opens, the login completes, `logged in to "tree-memory"
   (~/.tree-agent/mcp-auth/tree-memory.json)` prints, exit 0.

### Story: The user declines
1. In a terminal the user sees the URL and does nothing.
2. After 5 minutes: `mcp "tree-memory": login timed out after 5 min; run tree-agent --mcp-login tree-memory to
   log in`; the session continues without that server. The token file keeps the registered client
   (`tokens: null`), so the next attempt does not register again.

### Story: Logging out
1. `tree-agent --mcp-logout tree-memory`.
2. `logged out of "tree-memory" (…/mcp-auth/tree-memory.json)`; the file is gone; the next TTY run asks again.

### Story: A static key instead
1. `"headers": {"Authorization": "Bearer fmcp_…"}` on the same entry.
2. No discovery, no browser, no token file; the server either works or answers today's plain `HTTP 401`.

---

Blocked by: (none)

## Log

### [PA] 2026-10-25 10:05 — Grooming

**Summary**
The standard MCP authorization flow for streamable-HTTP servers, so the hosted `tree-memory` works from the
plain `{"type": "http", "url": …}` entry: discovery, dynamic registration, PKCE S256 on a persisted loopback
port, a 0600 token file with refresh, `--mcp-login` / `--mcp-logout`, a TTY rule so headless never hangs, and
the ADR-016 reversal of the "OAuth for MCP" non-goal.

**Key decisions**
- Hand-rolled over `@modelcontextprotocol/sdk`: stdlib covers every step; the SDK's `auth()` still needs a
  caller-written provider of similar size and is shaped for its own transports; the chapter's "no SDK" stance
  holds. The SWE confirms via context7 and records the finding; the ADR amendment records the revisit.
- A configured `Authorization` header disables the flow (the `fmcp_` path, hedged as unverified in the README).
- The TTY rule (stdin AND stderr TTYs, or `--mcp-login`) is what keeps CI from hanging; the skip is one warning
  naming the flag. Both flags are `parseArgs` flags, consistent with `cli.ts`; they return before
  `requireApiKey`.
- The login runs in `McpClient.#handshake` after a typed `McpAuthRequired`, outside the `initialize` timer, with
  its own 5-minute constant (injectable for tests, no settings key).
- Registration persists `redirect_uri` with its port, written to the token file BEFORE the browser step
  (`tokens: null`); re-login rebinds it; re-registration only on `EADDRINUSE` or `invalid_client`. An aborted
  login therefore leaves a client-only file, never a second client at the AS.
- The SDK dependency list in Scope 1 is stated as expected, not verified; the SWE's context7 finding is what
  the log records.
- `type: "http" | "stdio"` is accepted and validated against the branch (a silent drop today would turn a
  `type: "sse"` typo into a wrong-transport attempt).
- The hosted entry is NOT committed to `.tree-agent/mcp.json` (every un-logged-in repo-root run would warn or
  wait; duplicate tool set with `tree-memory-local`); it is documented for `~/.tree-agent/mcp.json`.
- Glossary, ADR and README text are written here (PA-authored, verbatim) for the SWE to paste; the human
  scoped the docs into the task.

**Dependencies**
- None. Lands after 218 in NNN order (shared files: `cli.ts`, `settings.ts`, README, spec, ADR-016).

**User stories**
- 7 stories covering: first TUI login, a silent later run, CI skip, `--mcp-login` without a model key, the
  user declining (timeout), logout, a static key.

**Open questions**
- None blocking. For the human: whether the hosted AS accepts a loopback `redirect_uri` on a port other than
  the registered one (RFC 8252 §7.3) is unknown; the persisted-port design does not depend on it.

Ready for implementation.

### [SWE] 2026-10-25 09:40 — Implementation

**Docs read first (tech-docs skill).** context7 is not among this session's connected MCP servers, so the
fallback of the skill was used: the MCP authorization spec as markdown
(`https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization.md`), the npm registry
metadata of `@modelcontextprotocol/sdk@latest` and its tarball (inspected, never executed). The hosted
server's 401 / PRM / AS metadata were re-probed unauthenticated and match the Scope facts exactly
(`/.well-known/oauth-protected-resource/mcp` serves the same PRM; the AS also lists an
`introspection_endpoint`; the PRM carries no `scopes_supported`, so `scope` comes from the AS).

**SDK finding → decision: hand-rolled, recommendation confirmed.** `@modelcontextprotocol/sdk@1.32.2`:
(a) `client/auth.js` IS importable on its own (the `./*` export maps to compiled `dist/esm/*`, so type
stripping is irrelevant); it imports `pkce-challenge`, `zod/v4`, `../types.js`, `../shared/auth.js`,
`../shared/auth-utils.js`, `../server/auth/errors.js`, `../shared/transport.js`. Its `auth(provider, {serverUrl,
authorizationCode?, fetchFn?})` takes a `fetchFn`, so it is NOT strictly tied to the SDK's transports — the
"shaped for the SDK's own transports" reason is weaker than stated (it is driven by the caller calling it
twice, around a redirect). (b) Installing it pulls 17 runtime deps regardless of what is imported: `ajv`,
`ajv-formats`, `zod`, `cors`, `hono`, `@hono/node-server`, `jose`, `express` (5), `express-rate-limit`,
`raw-body`, `content-type`, `cross-spawn`, `eventsource`, `eventsource-parser`, `pkce-challenge`,
`json-schema-typed`, `zod-to-json-schema` (+ peer `@cfworker/json-schema`). `OAuthClientProvider` has 15
members, 7 required (`redirectUrl`, `clientMetadata`, `clientInformation`, `tokens`, `saveTokens`,
`redirectToAuthorization`, `saveCodeVerifier`/`codeVerifier`); the loopback listener, the browser, the
timeout and the TTY rule stay the caller's either way. The dependency tree and the provider size hold, so
the ADR amendment text was pasted verbatim. Note for PA: "~300 lines" in the amendment is low — the
Biome-formatted `auth.ts` is 521 lines and `auth-store.ts` 97 (about 300 of code without comments and
76-column wrapping); I did not edit the PA-authored wording.

**Files modified**
- `apps/harness/src/mcp/auth.ts` (new) — `McpAuth` (`token()`, `refresh()`, `login(challenge, ui)`), `discover`
  (RFC 9728 → RFC 8414; PRM `resource` must share the server's origin; every AS endpoint HTTPS or
  loopback; `S256` and a `registration_endpoint` required), `authorizeUrl`, `challengeOf`, registration,
  token requests (form-encoded, `resource` always sent), the loopback listener (wrong `state` → 400 and the
  wait goes on; `error=` ends it; timeout; AbortSignal), `McpAuthRequired`, `OAuthError`, `LOGIN_TIMEOUT_MS`,
  `hasAuthorization`, `openBrowser`.
- `apps/harness/src/mcp/auth-store.ts` (new) — token file path (`[^A-Za-z0-9._-]` → `_`), read (ignored when
  missing/corrupt/another `url`), atomic write (`<file>.<pid>.tmp` created `wx` 0600, then rename; dir 0700 +
  chmod), delete.
- `apps/harness/src/mcp/transport-http.ts` — `auth?: McpAuth`: bearer on every POST (and the session
  DELETE), 401 → one refresh + one retry, second 401 → plain `HTTP 401`, no token / failed refresh →
  `McpAuthRequired` + `onClose` (the next call re-handshakes).
- `apps/harness/src/mcp/client.ts` — `McpAuth` per HTTP server without a configured `Authorization`;
  `#handshake` = attempt → on `McpAuthRequired` the login (outside any JSON-RPC timer) → ONE retry; no
  `login` UI → `needs a login; run tree-agent --mcp-login <server> once, then re-run`; `close()` aborts a
  pending login wait; options `login?: LoginUi`, `freshLogin?`; `StartMcpOptions.login`.
- `apps/harness/src/mcp/config.ts` — `type: z.literal("http"|"stdio").optional()`; a mismatch →
  `type must be http or stdio` through the existing error path.
- `apps/harness/src/cli.ts` — `--mcp-login <server>` / `--mcp-logout <server>` (`McpAuthMode`, resolved in
  `resolveMode`, handled before `requireApiKey`); `CliDeps.stderrIsTTY?` and `CliDeps.openUrl?`; the
  startup `login` UI only when stdin AND stderr are TTYs; USAGE gains two lines.
- `apps/harness/src/config/paths.ts` — `UserPaths.mcpAuth` (`$TREE_AGENT_HOME/mcp-auth`).
- `apps/harness/tests/helpers/oauth-http.ts` (new) — one handler as both a real `127.0.0.1:0` server and
  an in-memory `fetch` (for fake timers): `/mcp` (401 + the hosted `WWW-Authenticate` until a valid bearer),
  `/open` (no auth), PRM, AS metadata, `/oauth2/register|authorize|token` (checks `redirect_uri`,
  `resource`, `sha256(code_verifier)` itself); knobs `expireAccessToken`, `rejectRefresh`, `rejectClient`,
  `noRegistration`, `noScopes`, `noResourceMetadata`, `omitRefreshToken`, `rejectEveryToken`, `denyNext`;
  `browse()` plays the browser. `tests/helpers/mcp-http.ts` exports `answer`.
- Tests: `tests/mcp/auth.test.ts` (new, 24), `tests/mcp/auth-store.test.ts` (new, 6),
  `tests/mcp/transport-http.test.ts` (+6), `tests/mcp/client.test.ts` (+9; `TREE_AGENT_HOME` stubbed per
  test so no HTTP client reads the real `~/.tree-agent/mcp-auth`), `tests/mcp/config.test.ts` (+5),
  `tests/cli.test.ts` (+16; `run()` defaults `stderrIsTTY: false`).
- Docs: `docs/adrs/016_tree_agent_harness.md` (amendment appended verbatim; the body, as for 218's
  amendment, untouched), `docs/notes/tree-agent-harness-spec.md` (§1 row, §2 non-goals, file layout, §10.1
  `type` lines, new §10.2.1, §15 flags, §17 `mcp/auth.ts` row), `apps/harness/README.md` (line 15, usage
  block, flag rows, Config row, schema block + `type` rule, Timeouts bullet, the hosted-server section
  verbatim), `docs/glossary.md` (**MCP login** row verbatim; `tree-agent` Notes amended verbatim).
  `.tree-agent/mcp.json` unchanged; `agent-loop.ts` untouched (180 lines); no dependency added.

**Token file format** (`$TREE_AGENT_HOME/mcp-auth/<key>.json`, 0600 in a 0700 dir): exactly Scope 6 —
`{url, issuer, endpoints{authorization, token, registration}, resource, client{client_id, client_secret|null,
redirect_uri, registered_at}, tokens{access_token, refresh_token|null, expires_at|null, scope|null}|null}`.

**Tests**
- Unit: 984 passing, 0 failing, 49 files (before: 918 / 47) — `make harness-tests` (live tests ran: the
  Makefile exports `.env`).
- Integration: N/A (the harness has none, AGENTS.md).

**Acceptance criteria**
- [x] entry parses / `type` mismatch — `tests/mcp/config.test.ts::parses the human's exact .pi/mcp.json
  entry…`, `::rejects a type that does not match the transport` (×4).
- [x] the fake-AS flow — `tests/mcp/auth.test.ts::discover…` (header + origin fallback, PRM/AS scopes, refusals),
  `::authorizeUrl…`, `::registers a public loopback client, prints the URL, exchanges the code and stores the
  file`, `::writes the registration with tokens: null before the browser step`, `::a wrong state is answered
  400 and the wait goes on`, `tests/mcp/client.test.ts::the login runs outside initialize's timer…` (retried
  handshake with the bearer).
- [x] refresh / no loop / header precedence — `auth.test.ts::McpAuth.token / refresh` (7),
  `transport-http.test.ts::HttpTransport with an MCP login` (6), `client.test.ts::a failed refresh
  mid-session…` (TTY branch), `::a failed refresh at startup without a TTY…` (skip branch), `::a 401 after the login is one error…`, `::a configured Authorization header…`.
- [x] outside the initialize timer (two `initialize`s), 5-minute bound with fake timers (both the
  `McpAuth` boundary and `connectMcpServers` with the other server connected and usable), port rebound,
  re-registration on `EADDRINUSE` / `invalid_client` — `client.test.ts`, `auth.test.ts::re-login rebinds…`,
  `::re-registers on a fresh port…`, `::an exchange answered invalid_client…`, `::a refresh answered
  invalid_client…`.
- [x] TTY rule — `cli.test.ts::headless, piped stdin, not logged in…`, `::headless --json with a non-TTY
  stderr…`, `::TTY stdin and stderr: the URL line, the wait…; a later run is silent`.
- [x] `--mcp-login` / `--mcp-logout` / `--help` — `cli.test.ts::main (MCP login)` (no key, TTY and non-TTY,
  unknown / stdio / static-header, trusted project file, `error=`, Ctrl+C, logout both branches).
- [x] no secret in logs — `auth.test.ts::secrets never reach the log`, `cli.test.ts::no token, code,
  code_verifier or Authorization value in any log call or stderr line` (spies on every `log` level over a
  login + a refreshing run at `TREE_AGENT_LOG=debug`).
- [x] live (below). - [ ] [HUMAN] the browser login. - [x] docs. - [x] cadence.

**Mutation probes** (each applied alone, `tests/mcp` + `tests/cli.test.ts`, 290 tests, then restored):
PKCE verifier not hashed → 28 failed; `state` not checked → 1 failed; `resource` dropped from the token
request → 27 failed; header precedence removed → 5 failed; skip rule removed (`login` UI always on) → 2
failed; 0600 → 0644 → 3 failed. All six killed.

**Evidence** (live: scratch `TREE_AGENT_HOME` and `HOME` under the session scratchpad; `client_id`,
`state`, `code_challenge` redacted)
```
$ env -i PATH=… HOME=<scratch>/fakehome TREE_AGENT_HOME=<scratch>/home \
    timeout -s INT 20 node src/cli.ts --mcp-login tree-memory </dev/null   # no TTY: no browser
mcp "tree-memory": log in at https://tree-memory.fastmcp.app/oauth2/authorize?response_type=code&client_id=<redacted>&redirect_uri=http%3A%2F%2F127.0.0.1%3A61160%2Fcallback&code_challenge=<redacted>&code_challenge_method=S256&state=<redacted>&resource=https%3A%2F%2Ftree-memory.fastmcp.app&scope=email+offline_access+openid+profile
mcp "tree-memory": login cancelled
$ ls -la <scratch>/home/mcp-auth   → drwx------ mcp-auth/, -rw------- tree-memory.json
  file: url, issuer, endpoints (…/oauth2/authorize|token|register), resource https://tree-memory.fastmcp.app,
        client {client_id <33 chars>, client_secret null, redirect_uri http://127.0.0.1:61160/callback,
        registered_at 2026-10-25T09:33:11.167Z}, tokens null
$ … timeout --preserve-status -s INT 10 node src/cli.ts --mcp-login tree-memory; echo exit=$?
mcp "tree-memory": log in at https://tree-memory.fastmcp.app/oauth2/authorize?…redirect_uri=http%3A%2F%2F127.0.0.1%3A61160%2Fcallback…
mcp "tree-memory": login cancelled
exit=1
SAME client_id and redirect_uri (port) as the first login; the URL carries the stored client_id
$ curl -o /dev/null -w '%{http_code} -> %{redirect_url}' "<the printed URL>"
302 -> https://horizon.prefect.io/tree-memory/authorize (query elided)   # the AS accepted the request
$ echo "say ok" | TREE_AGENT_HOME=<scratch>/home HOME=<scratch>/fakehome make -s -C apps/harness run; echo exit=$?
ok
[tree-agent] warn: mcp "tree-memory": needs a login; run tree-agent --mcp-login tree-memory once, then re-run
exit=0          # the token file was not touched (no registration in headless)
$ … node src/cli.ts --mcp-logout tree-memory  → logged out of "tree-memory" (<scratch>/home/mcp-auth/tree-memory.json), exit 0
$ … node src/cli.ts --mcp-logout tree-memory  → not logged in to "tree-memory", exit 0
$ … node src/cli.ts --mcp-login nope          → no MCP server "nope" in mcp.json; known: tree-memory, exit 1
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
Checked 114 files … No fixes applied. / tsc --noEmit clean / Test Files 49 passed (49), Tests 984 passed (984) / all hooks Passed
```
One dynamic registration was made at the real AS (the first live login); the second reused it. The last
live step was `--mcp-logout`, so the scratch token file is gone: re-running the Scope 9 sequence from that
home registers one more client. The recorded outputs are kept in the session scratchpad under `live/`
(`login1.err`, `login2.err`, `client1.txt`, `client2.txt` — these two hold the raw client_id, not a secret
but not for pasting — `headless.out`, `headless.err`).

**Notes**
- Deviations: (1) the `log in at <url>` line is written to the CLI's stderr directly, not through `log.warn`,
  so `TREE_AGENT_LOG=error|silent` can never hide the URL a user is waiting on; the skip and timeout
  warnings go through `connectMcpServers`' existing single `log.warn`. (2) `invalid_client` (refresh or
  exchange) deletes the token file instead of keeping an in-memory flag, so the next login registers again
  in this process or a later one; an exchange `invalid_client` ends that login with the error (the user
  re-runs), no automatic second browser round. (3) `--mcp-logout` does not read `mcp.json` (a server
  removed from the config can still be logged out of). (4) `--mcp-login` against a server that never
  answers 401 prints `"<server>" did not ask for a login`, exit 0, instead of a false `logged in`. (5) The
  callback pages are HTML with the server name / `error_description` HTML-escaped. (6) Added beyond the
  task: the PRM `resource` must share the MCP server's origin, and AS endpoints must be HTTPS or loopback
  (the MCP spec's "MUST be served over HTTPS"); `Authorization` is also sent on the session DELETE.
  (7) The test knob for the login bound is `LoginUi.timeoutMs` (passed through `McpClientOptions.login`),
  not a separate `McpClientOptions.loginTimeoutMs`. (8) ADR-016's body (the diagram's `mcp/client.ts … no
  OAuth` label, the non-goals line) is untouched, following 218's precedent: the appended amendment
  records both changes. (9) `CliDeps.openUrl` is a second new seam next to `stderrIsTTY`: tests play the browser through it, and
  the real opener is only ever called on a TTY.
- Finding (not fixed, out of scope): `runTui` calls `tui.start()` before `open()`, so in the TUI the URL
  line goes to stderr while the TUI already owns the screen and may be overdrawn; the browser still opens,
  and `--mcp-login` is the reliable path. A 401 mid-session whose refresh fails fails THAT call (the
  transport is dropped) and the next call runs the login, per Scope 5.
- Ctrl+C in a real terminal: `--mcp-login` installs SIGINT/SIGTERM handlers through `CliDeps.signals`, so
  it exits 1 (verified with `timeout --preserve-status -s INT`), not Node's 130.
- `[HUMAN]` remains: the browser login on the owner's machine, the authenticated `search_memory` run and
  the post-logout skip; and whether the Horizon AS accepts a loopback redirect on a port other than the
  registered one stays unknown (the design does not depend on it).

### [Tester] 2026-10-25 11:55 — QA

**VERDICT: PASS** (suite 984/984 green; live evidence confirmed; no AC fails; 5 non-blocking notes below)

**Test summary**
- Format / lint / typecheck / pre-commit: PASS (`make harness-format-check harness-lint-check harness-typecheck` exit 0, "Checked 114 files … No fixes applied", tsc clean; `make pre-commit` all hooks Passed, exit 0).
- Unit tests: `make harness-tests` → Test Files 49 passed (49), Tests 984 passed (984), exit 0 (live OpenRouter tests ran). Integration: N/A (the harness has none).
- Warnings: 0 (no "unhandled" / "warning" / "deprecat" line in the run).
- Untouched as required: `.tree-agent/mcp.json` and `apps/harness/src/loop/agent-loop.ts` (180 lines) absent from `git status`; no `package.json` / lockfile change (no new dependency); `apps/memory`, `.env*` and the untracked "Implementation Spec" untouched.
- Own mutation probes (restored, diff-verified): `state` check disabled → 1 failed (`a wrong state is answered 400…`); a retry loop on a second 401 added → 2 failed (`transport-http … a second 401 after a fresh token is one plain error: no loop`, `client … a 401 after the login is one error`).

**Live (hosted server, scratch `TREE_AGENT_HOME`/`HOME` under the session scratchpad `adv/`; no `.env` value used against it)**
- `POST /mcp` unauthenticated → 401 + `WWW-Authenticate: Bearer realm="FastMCP", resource_metadata="https://tree-memory.fastmcp.app/.well-known/oauth-protected-resource"`; PRM and AS metadata equal the task's probed facts. (Disclosure: this one unauthenticated 401 probe was a POST, not a GET; it is the same probe the task cites.)
- `node src/cli.ts --mcp-login tree-memory </dev/null`, `env -i`: `mcp "tree-memory": log in at https://tree-memory.fastmcp.app/oauth2/authorize?response_type=code&client_id=<redacted>&redirect_uri=http%3A%2F%2F127.0.0.1%3A62353%2Fcallback&code_challenge=<redacted>&code_challenge_method=S256&state=<redacted>&resource=https%3A%2F%2Ftree-memory.fastmcp.app&scope=email+offline_access+openid+profile`; SIGINT → `mcp "tree-memory": login cancelled`, exit 1; dir 700, file 600, `tokens: null`, redirect `http://127.0.0.1:62353/callback`.
- Second `--mcp-login`: same client_id (sha256 prefix fc33b19797 both times) and same port 62353, SIGINT → exit 1 (one registration at the real AS from this pass).
- `echo "say ok" | TREE_AGENT_HOME=<scratch> make -s -C apps/harness run` → stdout `ok`, stderr exactly one `[tree-agent] warn: mcp "tree-memory": needs a login; run tree-agent --mcp-login tree-memory once, then re-run`, exit 0, token file untouched.

**E2E adversarial pass** (own fake MCP server + AS with a control plane, `scratchpad/adv/fakeas.mts`; drivers `adv1-5.mts`, `cli2.sh`, `head1.sh`, `head2.sh`)
- Happy path, real CLI process against the fake: `--mcp-login fk` (piped stdin) prints the URL, a curl plays the browser, `logged in to "fk" (<file>)` exit 0, file 0600/dir 0700. Via a pty (`script`) with a stub `open`: `tree-agent "say ok"` prints the `log in at` line, calls the browser opener once, logs in, answers `ok`, exit 0; the second run is silent (no URL, opener not called, 1 registration in total). PASS.
- Callback with wrong `state`, missing `code`, and `error=` with a wrong `state`: 400, 400, 400, wait continues; the right redirect then completes (200, tokens stored). PASS. `error=`/`error_description` carrying `<img src=x onerror=…>"&` → HTML-escaped page (`&#60;img…&#62;&#34;&#38;`), message to stderr. PASS.
- Token endpoint 200 with no `access_token`, non-JSON 200, HTTP 500: each → `mcp "b": token request: unexpected answer` / `token request failed: HTTP 500`, exit path clean, `tokens` stays null. PASS. `expires_in` as the string `"3600"`: the same "unexpected answer" and the one-shot code is spent (see note 2). PASS with note.
- AS metadata with `http://as.example.invalid` endpoints: `…/oauth2/authorize: authorization endpoints must use https`, no registration request, no file written. PASS. PRM `resource` = `https://evil.example`: `…resource https://evil.example is not http://127.0.0.1:…/mcp`, no further request. PASS. PRM whose `authorization_servers[0]` is plain http non-loopback: `fetch failed` (see note 3). PASS with note.
- 401 on the retry after a successful refresh: requests `POST /mcp+bearer | POST /oauth2/token | POST /mcp+bearer`, then `mcp "srv": HTTP 401: {"error":"invalid_request"}`; no loop. PASS.
- Refresh answered `invalid_grant`: no UI → `needs a login; run tree-agent --mcp-login srv once, then re-run`; with a UI → one login, `registrationsAdded: 0`, tools listed. PASS.
- 8 and 20 concurrent `tools/call`s all 401 at once: all succeed; token requests 1 or 2 per burst (not N) (see note 1). PASS with note.
- Login longer than `initializeTimeoutMs` (real time, init 300 ms, login 1 500 ms, bound 3 000 ms): connected after 1 919 ms. Nobody completes (bound 700 ms): `login timed out after 0.7 s; run tree-agent --mcp-login srv to log in` after 709 ms, file `tokens: null`. PASS (the 300 000 ms fake-timer boundary is the SWE's `auth.test.ts::is bounded by LOGIN_TIMEOUT_MS` and a client test; both in the green suite).
- Crash between registration and token write: `--mcp-login` (piped stdin, so no browser), SIGKILL right after the URL line → `fk.json` exists, 0600 in a 0700 dir, `tokens: null`, client present, no `.tmp` left; the next `--mcp-login` printed a URL for the same client and port, completed, 1 registration in total. PASS.
- Token file whose `url` differs: ignored, the next login registers a new client (2 registrations), overwrites on success. PASS. Corrupt (truncated JSON) file: treated as absent → skip message, no crash. PASS.
- Token file 0644 in a 0755 dir: read and used silently (list tools works), mode stays 644 until the next write; the refresh rewrite restored 600 and dir 700. PASS with note 4.
- `--mcp-login` with piped stdin: URL on stderr, no browser, waits for the callback until completed/cancelled (as the task says). SIGINT exit 1 and SIGTERM exit 1, both `login cancelled`, process gone, port released. PASS.
- Headless, piped stdin, `--json`, un-logged-in fake: exit 0, stdout 4 lines all valid JSON, stderr exactly one `needs a login` warning, the fake saw one `POST /mcp` and no discovery/registration. Argv prompt with `</dev/null`: `ok`, one warning, exit 0, no `mcp-auth` dir created. PASS.
- Configured `Authorization` header + 401: `mcp "srv": HTTP 401: {…}`, requests `/mcp` only, no `mcp-auth` dir. PASS.
- Config: human entry parses; `type: "sse"`, `{type:"http", command}`, `{type:"stdio", url}`, `type: 3`, `type: "HTTP"` all → `<home>/mcp.json: server "s": type must be http or stdio`; `{type:"http", url, command}` → `set command (stdio) or url (HTTP), not both`. PASS.
- CLI args: `--mcp-login ""` → `--mcp-login needs a server name` + usage, exit 1; `--mcp-login` alone → parseArgs "argument missing" + usage, exit 1; both flags → `use --mcp-login or --mcp-logout, not both`, exit 1; stdio server → `"st" is not an HTTP server in mcp.json` exit 1; static header → `… nothing to log in to` exit 0; unknown → `no MCP server "nope" in mcp.json; known: fk, st, hdr` exit 1; `--mcp-logout '../../etc/passwd'` → `not logged in …` (key sanitised to `.._.._etc_passwd`, nothing outside `mcp-auth/` touched); logout twice → `logged out of …` then `not logged in to "fk"`, exit 0 both. PASS.
- Secret hygiene: spies over every log level in `adv3.mts` (login, refresh, invalid_grant, 8 concurrent refreshes) → no `at-*`/`rt-*`/`code-*` line; with `TREE_AGENT_LOG=debug` the real-process stderr, the pty typescripts, headless stdout/stderr and the session JSONLs under the scratch homes contain none of the token values held by any `mcp-auth` file (python scan, 0 hits) and no `code_verifier`/`Authorization` value. The authorize URL itself (client_id, challenge, state) is printed by design. PASS.

**Acceptance criteria**
- [x] PASS — human entry parses; `sse` / `stdio`+url → `type must be http or stdio` naming the server — `config.test.ts` (5 new) and the run above.
- [x] PASS — fake-AS flow (discovery header + fallback, registration body, authorize URL, wrong state 400, exchange, bearer, 0600/0700, `tokens: null` before the browser step) — `auth.test.ts` (24) green; `cli2.sh` shows the file 0600/0700 with `tokens: null` after SIGKILL at the browser step.
- [x] PASS — refresh/retry once/keeps old refresh_token/failed refresh → login or skip/no loop/static header — `auth.test.ts::McpAuth.token / refresh`, `transport-http.test.ts` (6), `client.test.ts`; re-proved above (`D`, `E`, `N`) and by mutation.
- [x] PASS — login outside `initializeTimeoutMs`, 5-minute bound (fake timers), port rebound, re-registration only on `EADDRINUSE`/`invalid_client` — `client.test.ts`, `auth.test.ts` (green); real-time re-run above.
- [x] PASS — TTY rule: piped/non-TTY stderr → one warning, answer, exit 0, `--json` valid; TTY → URL line + wait — `cli.test.ts` (green) and `head1.sh` / `head2.sh` above.
- [x] PASS — `--mcp-login` / `--mcp-logout` / `--help` without `OPENROUTER_API_KEY` (`env -i`) — messages and exit codes as specified (`cli2.sh`).
- [x] PASS — no token / verifier / `Authorization` value in any log call or stderr line — spy + scan above, `auth.test.ts::secrets never reach the log`, `cli.test.ts::no token, code, code_verifier or Authorization value…`.
- [x] PASS — live (see Live section; second login reuses client_id and port; headless skip warning, `ok`, exit 0).
- [x] [HUMAN] — browser login on the owner's machine: verified 2026-10-25 with the owner (see the orchestrator entry below).
- [x] PASS — docs: ADR-016 amendment, README hosted-server section and glossary rows are verbatim the task text (diff read); spec §1/§2/§10.1/§10.2.1/§15/§17 in the diff; the SWE log records the SDK findings; `.tree-agent/mcp.json` unchanged.
- [x] PASS — cadence green, no new runtime dependency, `agent-loop.ts` untouched.

**Other issues found (none blocks the verdict; the orchestrator decides)**
1. Concurrent 401s can refresh twice. `HttpTransport.#authorizedPost` (`transport-http.ts`, the `await auth.refresh()` after a 401) always asks for a refresh when `sent` is set. A call whose 401 arrives after another call's refresh has already finished starts a second token request (observed: 20 concurrent calls → 1 request in one run, 2 in three runs; every call still succeeded). Harmless against a stateless AS, but an AS that invalidates access tokens on refresh could 401 the first call's retry. Fix: before refreshing, compare `sent` with `auth`'s current access token and retry with it when it already differs.
2. `expires_in` as a string (`"3600"`, seen from some servers) fails the whole login with the opaque `token request: unexpected answer` after the one-shot code is spent (`auth.ts:81-86`, `z.number()`). Suggest `z.coerce.number()`.
3. `discover()` GETs `authorization_servers[0]`'s metadata before checking its scheme (the `secure()` check only covers the three endpoints afterwards), and a network failure there surfaces as a bare `mcp "x": fetch failed` / `read ECONNRESET` with no URL (`getJson` does not wrap `fetch` errors). The RFC 8414 `issuer` is also not compared with the AS URL. Suggest `secure(as.href)` first and a `${url}: ${cause}` wrapper.
4. A token file that is group/world readable (0644) is read silently; the 0600 is only re-established on the next write. A one-line warning (or a `chmod`) on read would match the "never leak" posture.
5. Server keys that sanitise to one file (`a/b` and `a_b`) share `mcp-auth/a_b.json`; their different `url`s make them ping-pong (each login overwrites the other's registration). Rare; a note in the README or a hash suffix would do.
- Not exercised (as instructed): the real browser login, `fmcp_` as bearer, and the TUI drawing over the URL line (the SWE's documented finding). The TTY branch was exercised with a pty plus a stub browser opener against the fake, not the real `open`.
- Follow-up scan (after the entry above): the 5 session JSONLs under the scratch homes, `scratchpad/suite.txt`, the pty typescripts and every real-process stdout/stderr capture contain no `at-N` / `rt-N` / `code-N` token value, `code_verifier`, `access_token`, `refresh_token` or `Bearer <value>` (0 hits). One scratch fake AS (pid 30139, from an aborted first driver script) is still running on a random loopback port; nothing else depends on it.

**VERDICT: PASS**

### [SWE] 2026-10-25 12:05 — Tester fold-ins
Folded in the Tester's issues 1–5, each with a test: `expires_in` coerced (missing → 1 h) and the unexpected-answer error names the field (`auth.test.ts`); a 401 on an already-replaced token retries with the new one, else joins McpAuth's in-flight refresh (`transport-http.test.ts`, 3 concurrent 401s → 1 token request); `discover()` checks the AS scheme before fetching, wraps network failures as `GET <url>: …` and refuses an `issuer` mismatch (`auth.test.ts`); a token file read with a mode other than 0600 is chmodded back with one warning, and a key outside `[A-Za-z0-9._-]` gets a `~<sha256[:8]>` suffix so `a/b` and `a_b` no longer share a file (`auth-store.test.ts`; spec §10.2.1 updated).

### [SWE] 2026-10-25 12:25 — Post-login handshake fix
`--mcp-login` now exits 0 with `logged in to "<server>" (<file>)` once the tokens are stored, turning a failed follow-up `initialize` (the hosted server cold-starting past 30 s) into one `logged in, but …` warning; it also honours `settings.json` `mcp.initializeTimeoutMs`, whose default rose 30 000 → 60 000 (Horizon's 60 s readiness window) — `tests/cli.test.ts` "stored tokens, then an initialize that never answers".

### [Orchestrator + HUMAN] 2026-10-25 12:40 — Hosted login verified against the real server
Scratch `TREE_AGENT_HOME`, `mcp.json` = the owner's exact `{"type": "http", "url": "https://tree-memory.fastmcp.app/mcp"}` entry. (1) `--mcp-login tree-memory` printed the authorize URL; the owner signed in to Horizon in Chrome and the tab ended on `127.0.0.1:63391/callback`; tokens were stored (0600). On the pre-fix build the follow-up `initialize` timed out at 30 s (hosted cold start), which is what motivated `3cf3f83`; on the fixed build a second login (auto-approved by the existing Horizon session) printed `logged in to "tree-memory" (…)` and exited 0 within 12 s with no warning. (2) Two headless `--json` runs reused the cached token with no browser and no URL line: `mcp__tree_memory__search_memory` was called through the hosted server (classifier `allow`, `p(dangerous)=0.04`), `agent_end completed`, exit 0; the hosted memory answered `nothing_found` for every query, so the owner's cloud memory looks empty (a memory-content matter, not a harness one). (3) `--mcp-logout tree-memory` printed `logged out of …`, exit 0, and a piped `say ok` run then printed `ok` with exactly one `needs a login; run tree-agent --mcp-login tree-memory once, then re-run` warning, exit 0.
