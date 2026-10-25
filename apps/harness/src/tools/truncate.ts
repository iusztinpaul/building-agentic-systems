// Output caps shared by the tools and the session's afterToolCall.

import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export const MAX_OUTPUT_BYTES = 50 * 1024;

// `text` within maxBytes, or its head and tail with the full text
// saved to <tmpDir>/<id>.txt and the path appended (as bash does).
// A file that cannot be written is named as such, not thrown.
export function spillOver(
  text: string,
  maxBytes: number,
  tmpDir: string,
): string {
  const cut = headTail(text, maxBytes);
  if (cut === text) return text;
  try {
    mkdirSync(tmpDir, { recursive: true });
    const file = join(tmpDir, `${randomUUID()}.txt`);
    writeFileSync(file, text);
    return `${cut}\n\nFull output: ${file}`;
  } catch (err) {
    return `${cut}\n\ncould not save full output: ${(err as Error).message}`;
  }
}

// Keeps the first and last maxBytes / 2 of `text` around a marker
// naming how much was cut.
export function headTail(text: string, maxBytes: number): string {
  const bytes = Buffer.from(text, "utf8");
  if (bytes.length <= maxBytes) return text;
  const half = Math.floor(maxBytes / 2);
  return joinHeadTail(
    bytes.subarray(0, half),
    bytes.subarray(bytes.length - half),
    bytes.length - 2 * half,
  );
}

// Head and tail around the omission marker. Both cuts move to a
// character boundary (the bytes they drop count as omitted), so no
// U+FFFD appears next to the marker.
export function joinHeadTail(
  head: Buffer,
  tail: Buffer,
  omitted: number,
): string {
  const headEnd = charEnd(head);
  let tailStart = 0;
  while (tailStart < tail.length && isContinuation(tail[tailStart])) {
    tailStart++;
  }
  omitted += head.length - headEnd + tailStart;
  return [
    head.subarray(0, headEnd).toString("utf8"),
    `\n\n[... ${omitted} bytes omitted ...]\n\n`,
    tail.subarray(tailStart).toString("utf8"),
  ].join("");
}

// The longest prefix of `text` within maxBytes, never splitting a
// UTF-8 character.
export function cutBytes(text: string, maxBytes: number): string {
  const bytes = Buffer.from(text, "utf8");
  if (bytes.length <= maxBytes) return text;
  const prefix = bytes.subarray(0, maxBytes);
  return prefix.subarray(0, charEnd(prefix)).toString("utf8");
}

const isContinuation = (byte: number | undefined): boolean =>
  ((byte ?? 0) & 0xc0) === 0x80;

// Where `bytes` ends once a trailing incomplete character is dropped.
function charEnd(bytes: Buffer): number {
  let lead = bytes.length - 1;
  while (
    lead > 0 &&
    bytes.length - lead < 4 &&
    isContinuation(bytes[lead])
  ) {
    lead--;
  }
  const byte = bytes[lead] ?? 0;
  const size = byte >= 0xf0 ? 4 : byte >= 0xe0 ? 3 : byte >= 0xc0 ? 2 : 1;
  return lead + size > bytes.length ? lead : bytes.length;
}

// A byte stream kept as its first and last maxBytes / 2, so memory
// stays bounded however much is pushed.
export class HeadTailBuffer {
  readonly #half: number;
  #head: Buffer[] = [];
  #headBytes = 0;
  #tail: Buffer[] = [];
  #tailBytes = 0;
  total = 0;

  constructor(maxBytes: number = MAX_OUTPUT_BYTES) {
    this.#half = Math.floor(maxBytes / 2);
  }

  push(chunk: Buffer): void {
    this.total += chunk.length;
    const room = this.#half - this.#headBytes;
    if (room > 0) {
      const part = chunk.subarray(0, room);
      this.#head.push(part);
      this.#headBytes += part.length;
      chunk = chunk.subarray(part.length);
    }
    if (chunk.length === 0) return;
    this.#tail.push(chunk);
    this.#tailBytes += chunk.length;
    let first = this.#tail[0];
    while (first && this.#tailBytes > this.#half) {
      const drop = this.#tailBytes - this.#half;
      if (first.length <= drop) {
        this.#tail.shift();
        this.#tailBytes -= first.length;
      } else {
        this.#tail[0] = first.subarray(drop);
        this.#tailBytes -= drop;
      }
      first = this.#tail[0];
    }
  }

  // Every byte pushed so far, while nothing has been dropped.
  get complete(): boolean {
    return this.total === this.#headBytes + this.#tailBytes;
  }

  bytes(): Buffer {
    return Buffer.concat([...this.#head, ...this.#tail]);
  }

  render(): string {
    if (this.complete) return this.bytes().toString("utf8");
    return joinHeadTail(
      Buffer.concat(this.#head),
      Buffer.concat(this.#tail),
      this.total - this.#headBytes - this.#tailBytes,
    );
  }
}
