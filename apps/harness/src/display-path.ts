// How a path argument is shown to the user, shared by the TUI's tool
// cards and the permission prompt. A leaf module (node:path only), so
// `src/tui` can import it without reaching `tools/` or `permissions/`.

import { isAbsolute, relative, resolve } from "node:path";

// Relative to cwd when inside it ("." for cwd itself), else as given.
export function displayPath(path: string, cwd: string): string {
  const rel = relative(cwd, resolve(cwd, path));
  if (rel === "") return ".";
  const outside = rel.startsWith("..") || isAbsolute(rel);
  return outside ? path : rel;
}
