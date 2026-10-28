import { readdirSync, readFileSync, realpathSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { bashTool, MAX_SPILL_BYTES } from "../../src/tools/bash.ts";
import { ToolRegistry } from "../../src/tools/registry.ts";
import { gone, toolCtx, useTmpDir } from "../helpers/tmp.ts";

const dir = useTmpDir("bash");

function bash(command: string, timeout?: number, env: NodeJS.ProcessEnv = process.env) {
  const tmpDir = join(dir(), "home", "tmp");
  return bashTool({ tmpDir, env }).execute({ command, timeout }, toolCtx(dir()));
}

// The one file under <home>/tmp, or "" if none was written.
function spillFile(): string {
  const tmp = join(dir(), "home", "tmp");
  const [saved] = readdirSync(tmp);
  return saved ? join(tmp, saved) : "";
}

const LIMIT = 51.5 * 1024;

describe("bash", () => {
  it("runs in cwd and returns output with structured {exitCode, stdout, stderr}", async () => {
    const result = await bash("pwd -P; echo oops >&2");

    const cwd = realpathSync(dir());
    expect(result.isError).toBe(false);
    expect(result.content).toContain(cwd);
    expect(result.content).toContain("oops");
    expect(result.structured).toEqual({
      exitCode: 0,
      stdout: `${cwd}\n`,
      stderr: "oops\n",
    });
  });

  it("inherits the given env", async () => {
    const result = await bash("echo $TREE_TEST_VALUE", undefined, {
      ...process.env,
      TREE_TEST_VALUE: "from-env",
    });

    expect(result.content).toBe("from-env\n");
  });

  it("marks a non-zero exit as an error and names the code", async () => {
    const result = await bash("echo nope; exit 3");

    expect(result.isError).toBe(true);
    expect(result.content).toBe("nope\n\nCommand exited with code 3");
    expect(result.structured).toMatchObject({ exitCode: 3 });
  });

  it("says (no output) for a silent command", async () => {
    expect((await bash("true")).content).toBe("(no output)");
  });

  it("spills output over 50 KB to <home>/tmp/<id>.txt and ends with that path", async () => {
    const result = await bash("for i in $(seq 1 20000); do echo line-$i; done");

    const tmp = join(dir(), "home", "tmp");
    const [saved] = readdirSync(tmp);
    expect(saved).toMatch(/^[0-9a-f-]{36}\.txt$/);
    const path = join(tmp, saved ?? "");
    expect(result.content.endsWith(`Full output: ${path}`)).toBe(true);
    expect(result.content.startsWith("line-1\n")).toBe(true);
    expect(result.content).toContain("line-20000\n");
    expect(result.content).toMatch(/\[\.\.\. \d+ bytes omitted \.\.\.\]/);
    expect(Buffer.byteLength(result.content)).toBeLessThan(51 * 1024);
    const full = readFileSync(path, "utf8");
    expect(full.split("\n")).toHaveLength(20001);
    const { stdout } = result.structured as { stdout: string };
    expect(Buffer.byteLength(stdout)).toBeLessThan(LIMIT);
    expect(stdout.startsWith("line-1\n")).toBe(true);
    expect(stdout.endsWith("line-20000\n")).toBe(true);
    expect(stdout).toMatch(/\[\.\.\. \d+ bytes omitted \.\.\.\]/);
  });

  it("streams an 80 MB output to the file and keeps the result and memory bounded", async () => {
    const result = await bash("head -c 80000000 /dev/zero | tr '\\0' a");

    const path = spillFile();
    expect(result.isError).toBe(false);
    expect(Buffer.byteLength(result.content)).toBeLessThanOrEqual(LIMIT);
    expect(result.content.endsWith(`Full output: ${path}`)).toBe(true);
    expect(result.content).toContain("[... 79948800 bytes omitted ...]");
    expect(statSync(path).size).toBe(80_000_000);
    const { stdout, stderr } = result.structured as { stdout: string; stderr: string };
    expect(Buffer.byteLength(stdout)).toBeLessThanOrEqual(LIMIT);
    expect(stderr).toBe("");
  }, 30_000);

  it("stops the file at maxSpillBytes and says so", async () => {
    const tmpDir = join(dir(), "home", "tmp");
    const tool = bashTool({ tmpDir, maxSpillBytes: 200_000 });

    const result = await tool.execute(
      { command: "head -c 300000 /dev/zero | tr '\\0' b" },
      toolCtx(dir()),
    );

    const path = spillFile();
    expect(statSync(path).size).toBe(200_000);
    expect(result.content).toContain("Only the first 200000 bytes were saved.");
    expect(result.content.endsWith(`Full output: ${path}`)).toBe(true);
    expect(Buffer.byteLength(result.content)).toBeLessThanOrEqual(LIMIT);
    expect(MAX_SPILL_BYTES).toBe(100 * 1024 * 1024);
  });

  it("kills an endless 'yes' at the timeout with a bounded error result", async () => {
    const started = Date.now();

    const result = await bash("yes", 1);

    expect(Date.now() - started).toBeLessThan(5000);
    expect(result.isError).toBe(true);
    expect(result.content.startsWith("Command timed out after 1s\ny\ny\n")).toBe(true);
    expect(Buffer.byteLength(result.content)).toBeLessThanOrEqual(LIMIT);
    expect(statSync(spillFile()).size).toBeLessThanOrEqual(MAX_SPILL_BYTES);
  }, 15_000);

  it("keeps the head and tail and says why when the output file cannot be written", async () => {
    const blocker = join(dir(), "not-a-dir");
    writeFileSync(blocker, "");
    const tool = bashTool({ tmpDir: join(blocker, "tmp") });
    const registry = new ToolRegistry();
    registry.register(tool);

    const result = await registry.execute(
      { id: "1", name: "bash", arguments: { command: "seq 1 40000" } },
      toolCtx(dir()),
    );

    expect(result.isError).toBe(false);
    expect(result.content).toMatch(/^1\n2\n3\n/);
    expect(result.content).toContain("bytes omitted");
    expect(result.content).toMatch(/\n40000\n\n\ncould not save full output: (ENOTDIR|EEXIST)/);
    expect(result.content).not.toContain("Full output:");
  });

  it("never splits a multi-byte character at the head or tail cut", async () => {
    const result = await bash(`node -e "process.stdout.write('€'.repeat(100000))"`);

    expect(result.content).not.toContain("\uFFFD");
    expect(result.content).toMatch(
      /^€+\n\n\[\.\.\. \d+ bytes omitted \.\.\.\]\n\n€+\n\nFull output: /,
    );
    expect(result.structured).toMatchObject({ stdout: expect.not.stringContaining("\uFFFD") });
  });

  it("rejects a timeout over 3600 s or under 1 s as an invalid-arguments result", async () => {
    const registry = new ToolRegistry();
    registry.register(bashTool({ tmpDir: join(dir(), "tmp") }));
    const call = (timeout: number) =>
      registry.execute(
        { id: "1", name: "bash", arguments: { command: "true", timeout } },
        toolCtx(dir()),
      );

    const huge = await call(1e10);
    const tiny = await call(0.5);

    expect(huge.isError).toBe(true);
    expect(huge.content).toMatch(/^Invalid arguments for bash:\n {2}- timeout: .*3600/);
    expect(tiny.content).toMatch(/^Invalid arguments for bash:\n {2}- timeout: .*1/);
  });

  it("kills the command after the timeout and says so", async () => {
    const started = Date.now();

    const result = await bash("echo started; sleep 5", 0.3);

    expect(Date.now() - started).toBeLessThan(3000);
    expect(result.isError).toBe(true);
    expect(result.content).toBe("Command timed out after 0.3s\nstarted\n");
  });

  it("kills the command when the run is aborted", async () => {
    const controller = new AbortController();
    const tool = bashTool({ tmpDir: join(dir(), "tmp") });
    setTimeout(() => controller.abort(), 100);

    const result = await tool.execute({ command: "sleep 5" }, toolCtx(dir(), controller.signal));

    expect(result).toMatchObject({ isError: true, content: "Command aborted\n(no output)" });
  });

  it("leaves no orphan when aborted: the backgrounded child dies with the group", async () => {
    const controller = new AbortController();
    const tool = bashTool({ tmpDir: join(dir(), "tmp") });
    const pidFile = join(dir(), "pid");
    setTimeout(() => controller.abort(), 300);

    await tool.execute(
      { command: `sleep 30 & echo $! > '${pidFile}'; wait` },
      toolCtx(dir(), controller.signal),
    );

    expect(await gone(Number(readFileSync(pidFile, "utf8")))).toBe(true);
  });

  it("defaults the timeout to 120 s and is a non-read-only direct tool", () => {
    const tool = bashTool({ tmpDir: "/tmp/x" });

    expect(tool).toMatchObject({ name: "bash", exposure: "direct", isReadOnly: false });
    expect(tool.parameters.shape.timeout.description).toContain("default 120");
  });
});
