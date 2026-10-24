import { describe, expect, it } from "vitest";
import { cutBytes, HeadTailBuffer, headTail } from "../../src/tools/truncate.ts";

describe("headTail", () => {
  it("returns text within the cap unchanged", () => {
    expect(headTail("short", 10)).toBe("short");
  });

  it("keeps the first and last half of the cap around a marker", () => {
    expect(headTail("aaaaXXXXXXbbbb", 8)).toBe("aaaa\n\n[... 6 bytes omitted ...]\n\nbbbb");
  });

  it("moves both cuts to a character boundary and counts the dropped bytes", () => {
    // 7 x 3-byte chars = 21 bytes; halves of 4 split the 2nd and 6th char.
    expect(headTail("€€€€€€€", 8)).toBe("€\n\n[... 15 bytes omitted ...]\n\n€");
  });
});

describe("HeadTailBuffer", () => {
  const pushAll = (max: number, ...chunks: string[]) => {
    const buffer = new HeadTailBuffer(max);
    for (const c of chunks) buffer.push(Buffer.from(c));
    return buffer;
  };

  it("keeps everything while within the cap", () => {
    const buffer = pushAll(8, "aa", "aXXb", "bb");

    expect(buffer.complete).toBe(true);
    expect(buffer.render()).toBe("aaaXXbbb");
  });

  it("renders like headTail once chunks overflow, whatever the chunking", () => {
    const buffer = pushAll(8, "aaa", "aXXX", "X", "XXbb", "b", "b");

    expect(buffer.complete).toBe(false);
    expect(buffer.total).toBe(14);
    expect(buffer.render()).toBe(headTail("aaaaXXXXXXbbbb", 8));
  });

  it("renders without U+FFFD when a cut falls inside a character", () => {
    const buffer = pushAll(8, "€€€", "€€€€");

    expect(buffer.render()).toBe("€\n\n[... 15 bytes omitted ...]\n\n€");
  });
});

describe("cutBytes", () => {
  it("cuts by UTF-8 bytes without splitting a character", () => {
    expect(cutBytes("€€€", 7)).toBe("€€");
    expect(cutBytes("abc", 3)).toBe("abc");
  });
});
