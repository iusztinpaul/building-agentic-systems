---
id: 219-harness-mcp-oauth-streamable-http
status: pending
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

- [ ] The human's exact entry `{"type": "http", "url": "https://tree-memory.fastmcp.app/mcp"}` parses;
      `type: "sse"`, or `type: "stdio"` with a `url`, fails with `type must be http or stdio` naming the server.
- [ ] Against the fake AS + protected server: the first 401 drives discovery (header `resource_metadata`, and
      the origin fallback when the header has none), registration (`token_endpoint_auth_method: "none"`, the
      loopback `redirect_uri`, both grant types), an authorize URL carrying `response_type=code`, `client_id`,
      `redirect_uri`, `code_challenge` = `base64url(sha256(verifier))`, `code_challenge_method=S256`, `state`,
      `resource` = the PRM `resource` verbatim and `scope` = the advertised scopes; a wrong `state` → 400 and
      the wait continues; the exchange sends `code_verifier` and `resource`; the retried handshake succeeds
      with `Authorization: Bearer <access_token>`; the file exists at `$TREE_AGENT_HOME/mcp-auth/<key>.json`
      with mode `0600` in a `0700` dir and the shape of Scope 6 — written once with `tokens: null` right after
      registration (a login aborted at the browser step leaves it so) and again with the tokens after the
      exchange.
- [ ] A later 401 (or `expires_at` passed) refreshes silently and retries once; a refresh answer without
      `refresh_token` keeps the old one; a failed refresh → the interactive flow (TTY) or the skip warning (no
      TTY); a second 401 after a fresh token → one error, no loop; a configured `Authorization` header → no
      discovery, no flow, today's `HTTP 401` error.
- [ ] The login runs outside `mcp.initializeTimeoutMs` (an authorize step longer than that timeout still ends
      in a connected client, with exactly two `initialize` requests), is bounded by `LOGIN_TIMEOUT_MS` (fake
      timers: pending at 299 999 ms, the exact timeout warning at 300 000 ms, the client kept, the other fake
      server connected and the session usable), and on re-login rebinds the persisted port, re-registering only
      on `EADDRINUSE` or `invalid_client`.
- [ ] TTY rule: headless with piped stdin (or a non-TTY stderr) and an un-logged-in server prints exactly one
      `mcp "<server>": needs a login; run tree-agent --mcp-login <server> once, then re-run`, answers the prompt,
      exits 0, and `--json` stdout is valid JSONL; with TTY stdin and stderr it prints `mcp "<server>": log in
      at <url>` and waits (the fake completes it).
- [ ] `tree-agent --mcp-login <server>` works with NO `OPENROUTER_API_KEY` in the environment: unknown server
      → `no MCP server "<server>" in mcp.json; known: …` exit 1; a stdio server → `"<server>" is not an HTTP
      server in mcp.json` exit 1; a static-header server → `… nothing to log in to` exit 0; success → the file
      and `logged in to "<server>" (<file>)` exit 0. `--mcp-logout <server>` deletes the file (`logged out of
      …`) or prints `not logged in to "<server>"`, exit 0 both. `--help` lists both.
- [ ] No token, `code_verifier` or `Authorization` value appears in any `log` call or stderr line (spy over the
      whole fake flow at `TREE_AGENT_LOG=debug`).
- [ ] Live, scratch `TREE_AGENT_HOME`, the hosted entry in its `mcp.json`: `tree-agent --mcp-login tree-memory`
      prints the real `https://tree-memory.fastmcp.app/oauth2/authorize?…` URL with the parameters of Scope 9
      (paste the URL with `client_id` and `state` redacted into the log), then Ctrl+C → exit 1, the file holds
      `client` and `tokens: null`, and a second `--mcp-login` shows the SAME `client_id` and port (no second
      registration); headless `echo "say ok" | tree-agent` with the same config → the one skip warning, `ok`,
      exit 0.
- [ ] [HUMAN] On the human's machine: `tree-agent --mcp-login tree-memory` opens the browser, the Horizon login
      completes, the tab shows "you are logged in", the command prints `logged in to "tree-memory" (…)` and
      exits 0; then headless `tree-agent "search my memory for agents"` (repo root, `--trust`) calls
      `mcp__tree_memory__search_memory` and answers with NO browser and no URL line; a third run after
      `--mcp-logout tree-memory` with piped stdin prints the skip warning instead.
- [ ] ADR-016 amendment (verbatim), spec §1/§2/§10.1/§10.2.1/§15/§17, the README sections (verbatim where
      given), the glossary row and the `tree-agent` row amendment are in the diff; the SWE's log records the
      context7 findings on `@modelcontextprotocol/sdk`'s auth module and the decision; `.tree-agent/mcp.json` is
      unchanged.
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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
