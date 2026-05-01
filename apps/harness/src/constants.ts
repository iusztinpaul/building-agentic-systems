// Module-level constants with zero project imports.
//
// Why this file exists: there's a circular import chain between client.ts and
// tools/registry.ts (client → tools/registry → tools/task → agent/subagents →
// agent/loop → client). On Bun + Linux, that cycle reliably triggers a TDZ
// error ("Cannot access 'DEFAULT_MODEL' before initialization") when streamText
// is evaluated mid-cycle, because the `const DEFAULT_MODEL = ...` initializer
// in client.ts hasn't run yet by the time loop.ts re-enters streamText.
//
// Hoisting DEFAULT_MODEL into a leaf module (no project imports of its own)
// removes it from the cycle: every other module on the cycle can import it
// without re-entering client.ts, so it's always initialized before any
// consumer touches it. The underlying cycle still exists and should be
// cleaned up later, but this closes the immediate TDZ window.
export const DEFAULT_MODEL = "gemini-2.5-flash";
