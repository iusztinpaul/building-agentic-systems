import { stripTerminalSequences } from "@earendil-works/pi-tui";
import { describe, expect, it, vi } from "vitest";
import { StartupScreen } from "../../src/tui/startup.ts";

const shown = (screen: StartupScreen, width = 60) =>
  screen.render(width).map((l) => stripTerminalSequences(l).trim());

describe("StartupScreen", () => {
  it("shows each added line, wrapped to the width, above the starting line", () => {
    const screen = new StartupScreen(() => {});
    const url = `http://127.0.0.1:5/oauth2/authorize?${"x".repeat(80)}`;

    screen.add(`mcp "tree-memory": log in at ${url}`);

    const lines = shown(screen);
    expect(lines.at(-1)).toBe("starting… (Ctrl+C quits)");
    expect(lines.join("")).toContain(url.slice(0, 40));
    expect(screen.render(60).every((l) => stripTerminalSequences(l).length <= 60)).toBe(true);
  });

  it("Ctrl+C quits once and the screen says so", () => {
    const quit = vi.fn();
    const screen = new StartupScreen(quit);

    screen.handleInput("\x03");
    screen.handleInput("\x03");

    expect(quit).toHaveBeenCalledTimes(1);
    expect(shown(screen).at(-1)).toBe("quitting…");
  });

  it("shows a warning in red, a login line plain", () => {
    const screen = new StartupScreen(() => {});

    screen.add('mcp "x": log in at http://127.0.0.1:5/a');
    screen.add('mcp "x": login timed out after 5 min', true);

    const [, url, warning] = screen.render(80);
    expect(url).not.toContain("\x1b[31m");
    expect(warning).toContain("\x1b[31m");
  });

  it("ignores other keys, Esc included (it never quits the program)", () => {
    const quit = vi.fn();
    const screen = new StartupScreen(quit);

    screen.handleInput("\x1b");
    screen.handleInput("y");
    screen.handleInput("\r");

    expect(quit).not.toHaveBeenCalled();
  });

  it("drops escape sequences from a line (a server's text is untrusted)", () => {
    const screen = new StartupScreen(() => {});

    screen.add('mcp "x": \x1b[2Jneeds a login\x07');

    expect(screen.lines).toEqual([{ text: 'mcp "x": needs a login', error: false }]);
  });
});
