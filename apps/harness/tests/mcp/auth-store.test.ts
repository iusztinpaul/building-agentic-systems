import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";
import { describe, expect, it } from "vitest";
import { createLogger } from "../../src/log.ts";
import {
  type AuthFile,
  authFilePath,
  deleteAuthFile,
  readAuthFile,
  writeAuthFile,
} from "../../src/mcp/auth-store.ts";
import { useTmpDir } from "../helpers/tmp.ts";

const tmp = useTmpDir("auth-store");
const URL_ = "https://tree-memory.fastmcp.app/mcp";

const sample = (over: Partial<AuthFile> = {}): AuthFile => ({
  url: URL_,
  issuer: "https://tree-memory.fastmcp.app",
  endpoints: {
    authorization: "https://tree-memory.fastmcp.app/oauth2/authorize",
    token: "https://tree-memory.fastmcp.app/oauth2/token",
    registration: "https://tree-memory.fastmcp.app/oauth2/register",
  },
  resource: "https://tree-memory.fastmcp.app",
  client: {
    client_id: "c-1",
    client_secret: null,
    redirect_uri: "http://127.0.0.1:51823/callback",
    registered_at: "2026-10-25T10:00:00.000Z",
  },
  tokens: {
    access_token: "at-1",
    refresh_token: "rt-1",
    expires_at: "2026-10-25T11:00:00.000Z",
    scope: "openid profile email offline_access",
  },
  ...over,
});
const mode = (path: string) => statSync(path).mode & 0o777;

describe("authFilePath", () => {
  const env = { TREE_AGENT_HOME: "/h" };
  const h8 = (key: string) => createHash("sha256").update(key).digest("hex").slice(0, 8);

  it("is $TREE_AGENT_HOME/mcp-auth/<key>.json for a key of [A-Za-z0-9._-]", () => {
    expect(authFilePath(env, "tree-memory")).toBe("/h/mcp-auth/tree-memory.json");
    expect(authFilePath(env, "a_b")).toBe("/h/mcp-auth/a_b.json");
  });

  it("any other key: [^A-Za-z0-9._-] → _, then ~ and 8 hex digits of its sha256", () => {
    expect(authFilePath(env, "a b/c:d.e_f")).toBe(
      `/h/mcp-auth/a_b_c_d.e_f~${h8("a b/c:d.e_f")}.json`,
    );
    expect(authFilePath(env, "../x")).toBe(`/h/mcp-auth/.._x~${h8("../x")}.json`);
  });

  it("distinct keys never share a file: a/b, a_b, a:b", () => {
    const files = ["a/b", "a_b", "a:b", "a_b~0"].map((key) => authFilePath(env, key));

    expect(new Set(files).size).toBe(4);
    for (const file of files) expect(file.startsWith("/h/mcp-auth/")).toBe(true);
    for (const file of files) expect(basename(file)).not.toContain("/");
  });
});

describe("writeAuthFile / readAuthFile", () => {
  it("writes the file 0600 in a 0700 dir and reads it back", async () => {
    const file = join(tmp(), "mcp-auth", "tree-memory.json");

    await writeAuthFile(file, sample());

    expect(mode(file)).toBe(0o600);
    expect(mode(join(tmp(), "mcp-auth"))).toBe(0o700);
    expect(await readAuthFile(file, URL_)).toEqual(sample());
    expect(JSON.parse(readFileSync(file, "utf8"))).toEqual(sample());
  });

  it("tightens a pre-existing directory to 0700 and leaves no temp file behind", async () => {
    const dir = join(tmp(), "mcp-auth");
    mkdirSync(dir, { mode: 0o755 });
    const file = join(dir, "s.json");
    writeFileSync(`${file}.${process.pid}.tmp`, "stale", { mode: 0o644 });

    await writeAuthFile(file, sample({ tokens: null }));
    await writeAuthFile(file, sample());

    expect(mode(dir)).toBe(0o700);
    expect(mode(file)).toBe(0o600);
    expect(readdirSync(dir)).toEqual(["s.json"]);
  });

  it("a file whose mode is not 0600 is read, restored to 0600 and named in one warning", async () => {
    const file = join(tmp(), "mcp-auth", "s.json");
    await writeAuthFile(file, sample());
    chmodSync(file, 0o644);
    const lines: string[] = [];
    const log = createLogger("debug", { write: (t: string) => lines.push(t) });

    expect(await readAuthFile(file, URL_, log)).toEqual(sample());

    expect(mode(file)).toBe(0o600);
    expect(lines).toEqual([`[tree-agent] warn: ${file}: mode 0644, not 0600; restored 0600\n`]);
    await readAuthFile(file, URL_, log);
    expect(lines).toHaveLength(1);
  });

  it("keeps tokens: null (registered, not logged in)", async () => {
    const file = join(tmp(), "mcp-auth", "s.json");

    await writeAuthFile(file, sample({ tokens: null }));

    expect((await readAuthFile(file, URL_))?.tokens).toBeNull();
  });

  it("ignores a file written for another url, a missing file and a corrupt one", async () => {
    const file = join(tmp(), "mcp-auth", "s.json");
    await writeAuthFile(file, sample({ url: "https://old.example/mcp" }));

    expect(await readAuthFile(file, URL_)).toBeUndefined();
    expect(await readAuthFile(join(tmp(), "none.json"), URL_)).toBeUndefined();
    writeFileSync(file, "{ not json");
    expect(await readAuthFile(file, URL_)).toBeUndefined();
    writeFileSync(file, JSON.stringify({ url: URL_ }));
    expect(await readAuthFile(file, URL_)).toBeUndefined();
  });
});

describe("deleteAuthFile", () => {
  it("answers true when it deleted the file and false when there was none", async () => {
    const file = join(tmp(), "mcp-auth", "s.json");
    await writeAuthFile(file, sample());

    expect(await deleteAuthFile(file)).toBe(true);
    expect(await deleteAuthFile(file)).toBe(false);
  });
});
