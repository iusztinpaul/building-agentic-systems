# Milestone 4 — Permissions + JSON Sessions

## Goal

Permission prompts for destructive tools; JSONL transcript writer; `--resume` / `--continue` flags. Merged because each alone is too small to teach.

## Depends on

M3 (Ink TUI — permission dialog renders inline in the message stream).

## Files

New:
- `src/permissions/policy.ts` — allow/deny/ask rule evaluation. Patterns like `Bash(git:*)`, `Edit(./src/**)`. ~80 lines.
- `src/permissions/prompt.tsx` — Ink dialog: allow once / allow pattern / deny. ~60 lines.
- `src/session/store.ts` — JSONL appender; one file per session. ~50 lines.
- `src/session/paths.ts` — `~/.tree/projects/<cwd-hash>/<session-id>.jsonl` resolver. ~25 lines.
- `src/session/resume.ts` — list recent sessions, pick most recent for cwd. ~50 lines.

Modified:
- `src/agent/loop.ts` — callout to `checkPermission(tool, input, ctx)` before destructive tools; append every message to the session writer.
- `src/index.ts` — argv gains `--resume` / `--continue`.

## New dependencies

None (Bun.file / fs / os built-ins cover it).

## LOC budget

+~400 (cumulative ~1200).

## Verification

1. `make harness-typecheck && make harness-lint` — pass.
2. Trigger a destructive `bash` (`rm -rf /tmp/foo`) — permission dialog fires.
3. Approving once: `~/.tree/projects/*/<session-id>.jsonl` contains `user` + `assistant` + `tool_use` + `tool_result` entries.
4. `make harness-run -- --resume` lists recent sessions with cwd + first prompt.
5. `make harness-run -- --continue PROMPT="follow up"` re-attaches to the most recent session and appends.

## Out of scope

- `settings.json` user/project merge — revisit in M7 alongside hook config.
- Bypass-permissions mode — add as flag in M7.
