import { describe, expect, it } from "vitest";
import { displayPath } from "../src/display-path.ts";

describe("displayPath", () => {
  it("is . for the cwd itself", () => {
    expect(displayPath("/w", "/w")).toBe(".");
    expect(displayPath("sub/../", "/w")).toBe(".");
  });

  it("is relative to cwd inside it, whether given absolute or relative", () => {
    expect(displayPath("/w/src/a.ts", "/w")).toBe("src/a.ts");
    expect(displayPath("./src/a.ts", "/w")).toBe("src/a.ts");
  });

  it("is the path as given outside cwd", () => {
    expect(displayPath("/tmp/x", "/w")).toBe("/tmp/x");
    expect(displayPath("../other/a.ts", "/w")).toBe("../other/a.ts");
  });
});
