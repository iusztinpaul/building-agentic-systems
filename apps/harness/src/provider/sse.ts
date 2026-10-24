// A minimal server-sent-events reader: yields the `data` payload of
// every event. Shared by the OpenRouter provider and the MCP HTTP
// transport, so it knows nothing about JSON or chat completions.
//
// Rules: events are separated by a blank line; lines starting with
// ":" are comments (OpenRouter's `: OPENROUTER PROCESSING`
// keep-alives); `event:`/`id:`/`retry:` fields are ignored; several
// `data:` lines join with "\n"; `data: [DONE]` ends the stream; an
// unterminated event at EOF is dropped (WHATWG).

const DONE = "[DONE]";

export async function* parseSse(
  body: AsyncIterable<Uint8Array>,
): AsyncGenerator<string> {
  const decoder = new TextDecoder();
  let buffer = "";
  let data: string[] = [];

  for await (const chunk of body) {
    buffer += decoder.decode(chunk, { stream: true });
    let newline = buffer.indexOf("\n");
    while (newline !== -1) {
      const line = buffer.slice(0, newline).replace(/\r$/, "");
      buffer = buffer.slice(newline + 1);
      newline = buffer.indexOf("\n");

      if (line === "") {
        if (data.length === 0) continue;
        const payload = data.join("\n");
        data = [];
        if (payload === DONE) return;
        yield payload;
      } else if (!line.startsWith(":")) {
        const value = dataField(line);
        if (value !== undefined) data.push(value);
      }
    }
  }
}

function dataField(line: string): string | undefined {
  const colon = line.indexOf(":");
  const name = colon === -1 ? line : line.slice(0, colon);
  if (name !== "data") return undefined;
  if (colon === -1) return "";
  const value = line.slice(colon + 1);
  return value.startsWith(" ") ? value.slice(1) : value;
}
