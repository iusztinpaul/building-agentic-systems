# Milestone 7 — Hooks + Polish

## Goal

Shell-executed lifecycle hooks (`PreToolUse`/`PostToolUse`/`UserPromptSubmit`/`Stop`); slash commands (`/help`, `/clear`, `/resume`); error surfacing; README polish. Same exit-code + stdout-JSON contract as Claude Code.

## Depends on

M4 (permissions + session — hooks share `settings.json`); M3 (Ink — slash UI).

## Files

New:
- `src/hooks/runner.ts` — shell-exec a hook command with stdin = JSON context; parse stdout JSON; non-zero exit = deny. ~80 lines.
- `src/hooks/config.ts` — load hook definitions from `~/.tree/settings.json` (user) and `./.tree/settings.json` (project). ~40 lines.
- `src/ui/Slash.tsx` — inline slash-command menu + dispatcher for `/help`, `/clear`, `/resume`. ~60 lines.

Modified:
- `src/agent/loop.ts` — call `runHooks("PreToolUse", ctx)` before tool, `"PostToolUse"` after, `"UserPromptSubmit"` on each user turn, `"Stop"` on exit.
- `src/ui/Input.tsx` — route leading `/` to `<Slash/>`.
- `apps/harness/README.md` — full usage, config layout, extensibility notes.

## New dependencies

None (Bun subprocess is built-in).

## LOC budget

+~200 (cumulative ~2000).

## Verification

1. `./.tree/settings.json` with a `PreToolUse` hook blocking `bash(rm:*)` denies the tool call.
2. `/help` lists commands; `/clear` resets message state; `/resume` lists recent sessions and attaches.
3. Hook non-zero exit surfaces as a denied tool-result in the transcript.
4. `Stop` hook fires exactly once on normal exit and on `Ctrl+C`.

## Out of scope

- Skills (appendix feature) — explicitly deferred per `docs/harness-plan.md`.
- Remote hook endpoints — shell exec only.
