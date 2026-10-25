import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadMcpConfig, McpConfig, McpConfigError, parseMcpConfig } from "../../src/mcp/config.ts";

// Every path handed to node:fs or node:fs/promises, by any module the
// code under test imports: the `.mcp.json` spy.
const touched = vi.hoisted(() => [] as string[]);

function spyAll<T extends object>(real: T): T {
  const out: Record<string, unknown> = { ...(real as Record<string, unknown>) };
  for (const [key, value] of Object.entries(real)) {
    if (typeof value !== "function" || !/^[a-z]/.test(key)) continue;
    out[key] = (...args: unknown[]) => {
      touched.push(String(args[0]));
      return (value as (...a: unknown[]) => unknown)(...args);
    };
  }
  return out as T;
}

vi.mock("node:fs/promises", async (importOriginal) => {
  const real = await importOriginal<typeof import("node:fs/promises")>();
  const spied = spyAll(real);
  return { ...spied, default: spied };
});
vi.mock("node:fs", async (importOriginal) => {
  const real = await importOriginal<typeof import("node:fs")>();
  const spied = spyAll(real);
  return { ...spied, default: spied };
});

let home: string;
let cwd: string;

beforeEach(() => {
  const root = mkdtempSync(join(tmpdir(), "tree-agent-mcp-config-"));
  home = join(root, "home");
  cwd = join(root, "project");
  mkdirSync(home);
  mkdirSync(join(cwd, ".tree-agent"), { recursive: true });
});

afterEach(() => {
  rmSync(join(home, ".."), { recursive: true, force: true });
});

const env = () => ({ TREE_AGENT_HOME: home });
const write = (file: string, value: unknown) =>
  writeFileSync(file, typeof value === "string" ? value : JSON.stringify(value));
const userFile = () => join(home, "mcp.json");
const projectFile = () => join(cwd, ".tree-agent", "mcp.json");
// The fixtures above go through the spied fs too: record only the load.
const load = (options: Parameters<typeof loadMcpConfig>[0]) => {
  touched.length = 0;
  return loadMcpConfig(options);
};

describe("McpConfig schema (spec §10.1)", () => {
  it("parses a stdio server with the defaults filled in", () => {
    const parsed = McpConfig.parse({ mcpServers: { local: { command: "uv" } } });

    expect(parsed.mcpServers.local).toEqual({
      command: "uv",
      args: [],
      env: {},
      toolExposure: {},
    });
  });

  it("parses a streamable-HTTP server with headers and a toolExposure map", () => {
    const parsed = McpConfig.parse({
      mcpServers: {
        remote: {
          url: "https://tree-memory.fastmcp.app/mcp",
          headers: { Authorization: "Bearer t" },
          toolExposure: {
            "search_*": "direct",
            "get_*": "codemode",
            "admin_*": "deferred",
            "delete_*": "hidden",
          },
        },
      },
    });

    expect(parsed.mcpServers.remote).toEqual({
      url: "https://tree-memory.fastmcp.app/mcp",
      headers: { Authorization: "Bearer t" },
      toolExposure: {
        "search_*": "direct",
        "get_*": "codemode",
        "admin_*": "deferred",
        "delete_*": "hidden",
      },
    });
  });
});

describe("parseMcpConfig errors name the server and the key", () => {
  const parse = (servers: unknown) => () => parseMcpConfig({ mcpServers: servers }, "mcp.json");

  it("rejects a url without a scheme with the story's message", () => {
    expect(parse({ "tree-memory": { url: "tree-memory.fastmcp.app/mcp" } })).toThrow(
      'mcp.json: server "tree-memory": url must be a valid URL',
    );
  });

  it("rejects a server with both command and url", () => {
    expect(parse({ both: { command: "uv", url: "https://x.dev/mcp" } })).toThrow(
      'mcp.json: server "both": set command (stdio) or url (HTTP), not both',
    );
  });

  it("rejects an unknown toolExposure value, naming the glob", () => {
    expect(parse({ s: { command: "uv", toolExposure: { "search_*": "visible" } } })).toThrow(
      'mcp.json: server "s": toolExposure.search_* must be one of direct, deferred, codemode, hidden',
    );
  });

  it("rejects a non-string toolExposure value the same way", () => {
    expect(parse({ s: { command: "uv", toolExposure: { "get_*": 1 } } })).toThrow(
      'mcp.json: server "s": toolExposure.get_* must be one of direct, deferred, codemode, hidden',
    );
  });

  it("parses the human's exact .pi/mcp.json entry, keeping its type", () => {
    const entry = { type: "http", url: "https://tree-memory.fastmcp.app/mcp" };

    const parsed = parseMcpConfig({ mcpServers: { "tree-memory": entry } }, "mcp.json");

    expect(parsed.mcpServers["tree-memory"]).toEqual({ ...entry, headers: {}, toolExposure: {} });
    expect(
      parseMcpConfig({ mcpServers: { l: { type: "stdio", command: "uv" } } }, "mcp.json").mcpServers
        .l,
    ).toMatchObject({ type: "stdio", command: "uv" });
  });

  it.each([
    [{ type: "sse", url: "https://x.dev/sse" }],
    [{ type: "stdio", url: "https://x.dev/mcp" }],
    [{ type: "http", command: "uv" }],
    [{ type: 1, url: "https://x.dev/mcp" }],
  ])("rejects a type that does not match the transport: %j", (entry) => {
    expect(parse({ s: entry })).toThrow('mcp.json: server "s": type must be http or stdio');
  });

  it("rejects a server with neither command nor url", () => {
    expect(parse({ s: { args: ["x"] } })).toThrow(
      'mcp.json: server "s": needs command (stdio) or url (HTTP)',
    );
  });

  it("names a wrongly typed stdio key", () => {
    expect(parse({ s: { command: "uv", args: "run" } })).toThrow(
      'mcp.json: server "s": args must be an array',
    );
  });

  it("names a missing mcpServers key", () => {
    expect(() => parseMcpConfig({}, "mcp.json")).toThrow("mcp.json: mcpServers is required");
  });

  it("rejects a url whose scheme is not http or https", () => {
    expect(parse({ ftp: { url: "ftp://files.example.com/mcp" } })).toThrow(
      'mcp.json: server "ftp": url must start with http:// or https://',
    );
    expect(parse({ ok: { url: "HTTP://localhost:8000/mcp" } })).not.toThrow();
  });

  it("keeps a server named __proto__ (an own key, not the prototype)", () => {
    const raw = JSON.parse(
      '{"mcpServers": {"__proto__": {"command": "uv"}, "b": {"command": "x"}}}',
    );

    const { mcpServers } = parseMcpConfig(raw, "mcp.json");

    expect(Object.keys(mcpServers)).toEqual(["__proto__", "b"]);
    expect(Object.getOwnPropertyDescriptor(mcpServers, "__proto__")?.value).toMatchObject({
      command: "uv",
    });
  });

  it("rejects an empty server key", () => {
    expect(parse({ "": { command: "uv" } })).toThrow('mcp.json: server "": name must not be empty');
  });

  it("names a non-object top level or mcpServers", () => {
    expect(() => parseMcpConfig([], "mcp.json")).toThrow("mcp.json: must be an object");
    expect(() => parseMcpConfig({ mcpServers: [] }, "mcp.json")).toThrow(
      "mcp.json: mcpServers must be an object",
    );
  });

  it("throws McpConfigError", () => {
    expect(parse({ s: {} })).toThrow(McpConfigError);
  });
});

describe("loadMcpConfig", () => {
  it("returns no servers when neither file exists", async () => {
    await expect(load({ env: env(), projectDir: cwd })).resolves.toEqual({});
  });

  it("merges user then project by server name, the project entry winning whole", async () => {
    write(userFile(), {
      mcpServers: {
        shared: { command: "user-cmd", toolExposure: { "a*": "direct" } },
        userOnly: { url: "https://u.dev/mcp" },
      },
    });
    write(projectFile(), {
      mcpServers: { shared: { command: "project-cmd" }, projectOnly: { command: "p" } },
    });

    const servers = await load({ env: env(), projectDir: cwd });

    expect(Object.keys(servers)).toEqual(["shared", "userOnly", "projectOnly"]);
    expect(servers.shared).toEqual({ command: "project-cmd", args: [], env: {}, toolExposure: {} });
    expect(servers.userOnly).toMatchObject({ url: "https://u.dev/mcp" });
  });

  it("reads only the user file for an untrusted cwd (no projectDir)", async () => {
    write(userFile(), { mcpServers: { u: { command: "u" } } });
    write(projectFile(), { mcpServers: { p: { command: "p" } } });

    const servers = await load({ env: env() });

    expect(Object.keys(servers)).toEqual(["u"]);
    expect(touched.some((path) => path.startsWith(cwd))).toBe(false);
  });

  it("never opens the repo-root .mcp.json (fs spy)", async () => {
    write(join(cwd, ".mcp.json"), {
      mcpServers: { claude: { type: "http", url: "https://c.dev" } },
    });
    write(projectFile(), { mcpServers: { p: { command: "p" } } });

    const servers = await load({ env: env(), projectDir: cwd });

    expect(Object.keys(servers)).toEqual(["p"]);
    expect(touched).toEqual(expect.arrayContaining([userFile(), projectFile()]));
    expect(touched.filter((path) => path.endsWith(".mcp.json"))).toEqual([]);
    expect(touched.every((path) => path === userFile() || path === projectFile())).toBe(true);
  });

  it("only .mcp.json present → no servers", async () => {
    write(join(cwd, ".mcp.json"), { mcpServers: { claude: { command: "x" } } });

    await expect(load({ env: env(), projectDir: cwd })).resolves.toEqual({});
  });

  it("names the file in a validation error", async () => {
    write(projectFile(), { mcpServers: { "tree-memory": { url: "tree-memory.fastmcp.app/mcp" } } });

    await expect(load({ env: env(), projectDir: cwd })).rejects.toThrow(
      `${projectFile()}: server "tree-memory": url must be a valid URL`,
    );
  });

  it("names the file for an empty server key", async () => {
    write(userFile(), { mcpServers: { "": { command: "x" } } });

    await expect(load({ env: env() })).rejects.toThrow(
      `${userFile()}: server "": name must not be empty`,
    );
  });

  it("names the file on invalid JSON", async () => {
    write(userFile(), "{ nope");

    await expect(load({ env: env() })).rejects.toThrow(`${userFile()}: invalid JSON`);
  });
});

describe("the committed .tree-agent/mcp.json (repo root)", () => {
  const file = fileURLToPath(new URL("../../../../.tree-agent/mcp.json", import.meta.url));

  it("has tree-memory-local only: the .mcp.json command, search_memory direct, visualize_* deferred", () => {
    const { mcpServers } = parseMcpConfig(JSON.parse(readFileSync(file, "utf8")), file);

    expect(Object.keys(mcpServers)).toEqual(["tree-memory-local"]);
    expect(mcpServers["tree-memory-local"]).toEqual({
      command: "uv",
      args: [
        "--directory",
        "apps/memory",
        "run",
        "--env-file",
        "../../.env",
        "python",
        "scripts/serve_mcp.py",
      ],
      env: { ENV_FILE_PATH: "../../.env" },
      toolExposure: { search_memory: "direct", "visualize_*": "deferred" },
    });
  });
});
