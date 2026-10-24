---
id: 207-harness-skills-discovery-and-prompt-listing
status: pending
feature: tree-agent-harness
---

# M4 — Agent Skills: `SKILL.md` discovery in the spec's order, the budgeted `skills` prompt section (progressive disclosure), `disable-model-invocation`, and `/skill:<name> [args]`

Tags: `harness`, `skills`, `prompt`, `enhancement`
Depends on: 205 (TUI slash commands, trust dialog)
Blocks: —
Implements: ADR-016 §4 (progressive disclosure: skills listed by name + description, read on demand)

## Scope

1. **`src/skills/skills.ts`** — spec §9: a skill is a directory with `SKILL.md` carrying YAML frontmatter
   `name`, `description`, optional `disable-model-invocation: true`. Discovery order, later wins on a name
   clash: `~/.tree-agent/skills/*/`, `~/.agents/skills/*/`, every `.agents/skills/*/` from `cwd` up to the git
   root, `.tree-agent/skills/*/`; symlinks followed; project-level dirs are trust-gated (204). Diagnostics (bad
   frontmatter, duplicate names, missing `name`) are collected and shown once at startup (TUI notice / stderr
   headless), never thrown.
2. **Prompt section** — the §9 block verbatim (`<available_skills>` with `name`, `description`, `location`
   = absolute `SKILL.md` path); description cap 250 chars with `…`; whole-section cap
   `skills.listingBudgetRatio` (1 %) × the current model's `contextLength` — skills past the cap are dropped
   with one startup warning naming them; `disable-model-invocation` hides a skill from the section but keeps
   it invocable.
3. **`/skill:<name> [args]`** — TUI and headless (a prompt whose first token is `/skill:<name>`): injects the
   full `SKILL.md` body plus the args as the user message; an unknown name answers one line listing the known
   names.
4. `--no-skills` skips discovery and the section.
5. This repo's own `.agents/skills/` (13 skills, discovered when the repo root is trusted) is the real-world
   fixture: `tree-memory` carries `disable-model-invocation: true` and must be reachable only via
   `/skill:tree-memory`.

**Out of scope (intentional):** prompt templates, an extensions API (spec non-goals); skill-local scripts or
`allowed-tools` fields (the standard's optional keys beyond the three named are ignored, not rejected).

## Acceptance Criteria

- [ ] Discovery order and override: a skill `x` in `~/.tree-agent/skills` and in `.tree-agent/skills` lists
      once with the project path; the four roots are searched in the stated order; a symlinked skill dir is
      found; in an untrusted `cwd` only the two user-level roots are read.
- [ ] A 400-char description is listed as its first 249 chars + `…`; with a fake `contextLength` of 20 000
      (budget 200 tokens ≈ 800 chars) the listing stops at the budget and the dropped names appear in one
      warning; a `disable-model-invocation: true` skill is absent from the section and present for `/skill:`.
- [ ] `/skill:<name> arg1 arg2` on FakeProvider sends a user message equal to the `SKILL.md` body + the args;
      `/skill:nope` answers one line with the known names and sends nothing to the provider.
- [ ] Bad frontmatter in one skill does not stop discovery of the others; the diagnostic names the file.
- [ ] `--no-skills` → no `skills` section and no file reads under any skills root (spy on the walker).
- [ ] The section's exact wrapper text ("The following skills provide specialized instructions. …
      Resolve relative paths against the skill dir.") matches spec §9.
- [ ] [HUMAN] In this repo (trusted): `tree-agent --json "Use the tech-docs skill to tell me where the Prefect
      llms.txt index is"` shows a `read` `tool_call` whose path ends with `.agents/skills/tech-docs/SKILL.md`
      and an answer naming `https://docs.prefect.io/llms.txt`; `/skill:tree-memory what do I know about
      Modal?` in the TUI injects the skill body (the model then asks for the memory server, which is not yet
      connected — expected until 209).
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green.

## User Stories

### Story: The model picks a skill by its description
1. The user asks for authoritative MongoDB docs; the prompt lists `tech-docs` with its one-line description.
2. The model calls `read` on the skill's absolute `location`, follows its instructions, answers with the index URL.

### Story: A user invokes a hidden skill explicitly
1. `tree-memory` is `disable-model-invocation: true`; it is not in the listing.
2. `/skill:tree-memory what did I decide about Modal?` injects the full body + the question as the user turn.

### Story: Too many skills for the budget
1. A machine has 60 skills under `~/.agents/skills`; the model's 1 % budget fits 40.
2. The section holds 40; a single startup warning lists the 20 dropped names; nothing crashes.

### Story: A malformed skill
1. One `SKILL.md` has no `name`.
2. Startup shows one diagnostic naming the file; the other skills are listed as usual.

### Story: CI disables skills
1. `tree-agent --no-skills "…"`.
2. The recorded prompt has no `skills` section; no skills directory was read.

---

Blocked by: 205

## Log

### [PA] 2026-10-24 20:09 — Grooming

**Summary**
Agent Skills with progressive disclosure: discover `SKILL.md` files in the spec's order, list only name +
description + path under a 1 % budget, hide `disable-model-invocation` skills from the model, and let the user
inject any skill with `/skill:<name>`.

**Key decisions**
- This repo's `.agents/skills/` is the fixture for the human check; `tree-memory`'s
  `disable-model-invocation: true` is exactly the hidden-but-invocable case.
- Diagnostics are collected, never thrown — one bad skill must not take the harness down.
- Extra frontmatter keys from the standard are ignored, not errors (forward compatibility at zero cost).

**Dependencies**
- 205 — slash-command plumbing and the trust state the project roots depend on.

**User stories**
- 5 stories covering: model-picked skill, explicit hidden skill, the budget cut, a malformed skill, `--no-skills`.

Ready for implementation.
