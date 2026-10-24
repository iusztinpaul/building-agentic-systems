// Plain SGR styles and the two themes pi-tui's Editor and Markdown
// need. Basic 16-colour codes only: they follow the user's terminal
// palette, so there is no theme to configure.

import {
  type EditorTheme,
  type MarkdownTheme,
  stripTerminalSequences,
} from "@earendil-works/pi-tui";

const sgr =
  (on: number, off: number) =>
  (text: string): string =>
    `\x1b[${on}m${text}\x1b[${off}m`;

export const bold = sgr(1, 22);
export const dim = sgr(2, 22);
export const italic = sgr(3, 23);
export const underline = sgr(4, 24);
export const strike = sgr(9, 29);
export const red = sgr(31, 39);
export const green = sgr(32, 39);
export const yellow = sgr(33, 39);
export const cyan = sgr(36, 39);

export const markdownTheme: MarkdownTheme = {
  heading: (text) => bold(cyan(text)),
  link: underline,
  linkUrl: dim,
  code: yellow,
  codeBlock: (text) => text,
  codeBlockBorder: dim,
  quote: italic,
  quoteBorder: dim,
  hr: dim,
  listBullet: cyan,
  bold,
  italic,
  strikethrough: strike,
  underline,
};

export const editorTheme: EditorTheme = {
  borderColor: dim,
  selectList: {
    selectedPrefix: cyan,
    selectedText: bold,
    description: dim,
    scrollInfo: dim,
    noMatch: dim,
  },
};

// Model and tool text is untrusted: drop its escape sequences and
// control characters (tabs and newlines stay), so it cannot move the
// cursor, clear the screen or recolour what follows.
export function sanitize(text: string): string {
  return stripTerminalSequences(text)
    .replace(/\r\n?/g, "\n")
    .replace(/[^\P{Cc}\n\t]/gu, "");
}
