// The MCP login's token file (spec §10.2.1): one JSON file per HTTP
// server, $TREE_AGENT_HOME/mcp-auth/<server>.json, mode 0600 in a
// 0700 directory, written atomically (a temp file, then rename).
// `tokens: null` means "registered, not logged in". A file whose
// `url` is not the configured one is ignored, so a retargeted server
// never reuses a token. The file holds secrets: the only log line is
// the path of one found with a mode other than 0600 (then tightened).

import { createHash } from "node:crypto";
import {
  chmod,
  mkdir,
  readFile,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { dirname, join } from "node:path";
import { z } from "zod";
import { userPaths } from "../config/paths.ts";
import { log as defaultLog, type Logger } from "../log.ts";

const Tokens = z.object({
  access_token: z.string(),
  refresh_token: z.string().nullable(),
  expires_at: z.string().nullable(), // ISO 8601 UTC
  scope: z.string().nullable(),
});
const Client = z.object({
  client_id: z.string(),
  client_secret: z.string().nullable(),
  redirect_uri: z.string(), // http://127.0.0.1:<port>/callback
  registered_at: z.string(),
});
export const AuthFile = z.object({
  url: z.string(),
  issuer: z.string(),
  endpoints: z.object({
    authorization: z.string(),
    token: z.string(),
    registration: z.string(),
  }),
  resource: z.string(),
  client: Client,
  tokens: Tokens.nullable(),
});

export type AuthFile = z.infer<typeof AuthFile>;
export type AuthTokens = z.infer<typeof Tokens>;

// The mcp.json key itself when it is all [A-Za-z0-9._-]; otherwise
// every other character → `_`, then `~` and the first 8 hex digits
// of sha256(key). `~` is outside the plain alphabet, so `a/b`
// (a_b~<hash>.json) never collides with `a_b` (a_b.json).
export function authFilePath(
  env: NodeJS.ProcessEnv,
  server: string,
): string {
  const plain = /^[A-Za-z0-9._-]+$/.test(server);
  const hash = createHash("sha256").update(server).digest("hex");
  const key = plain
    ? server
    : `${server.replace(/[^A-Za-z0-9._-]/gu, "_")}~${hash.slice(0, 8)}`;
  return join(userPaths(env).mcpAuth, `${key}.json`);
}

// undefined: no file, an unreadable one, or one for another url. A
// file whose mode is not 0600 is tightened to 0600, with one warning.
export async function readAuthFile(
  file: string,
  url: string,
  log: Logger = defaultLog,
): Promise<AuthFile | undefined> {
  let raw: unknown;
  try {
    const { mode } = await stat(file);
    if ((mode & 0o777) !== 0o600) {
      const was = (mode & 0o777).toString(8).padStart(4, "0");
      await chmod(file, 0o600);
      log.warn(`${file}: mode ${was}, not 0600; restored 0600`);
    }
    raw = JSON.parse(await readFile(file, "utf8"));
  } catch {
    return undefined;
  }
  const parsed = AuthFile.safeParse(raw);
  return parsed.success && parsed.data.url === url
    ? parsed.data
    : undefined;
}

export async function writeAuthFile(
  file: string,
  data: AuthFile,
): Promise<void> {
  const dir = dirname(file);
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await chmod(dir, 0o700);
  const tmp = `${file}.${process.pid}.tmp`;
  await rm(tmp, { force: true }); // mode applies on creation only
  const text = `${JSON.stringify(data, null, 2)}\n`;
  await writeFile(tmp, text, { mode: 0o600, flag: "wx" });
  await rename(tmp, file);
}

// true when a file was there to delete.
export async function deleteAuthFile(file: string): Promise<boolean> {
  try {
    await rm(file);
    return true;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw err;
  }
}
