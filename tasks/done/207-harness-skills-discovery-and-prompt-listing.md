---
id: 207-harness-skills-discovery-and-prompt-listing
status: done
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

- [x] Discovery order and override: a skill `x` in `~/.tree-agent/skills` and in `.tree-agent/skills` lists
      once with the project path; the four roots are searched in the stated order; a symlinked skill dir is
      found; in an untrusted `cwd` only the two user-level roots are read.
- [x] A 400-char description is listed as its first 249 chars + `…`; with a fake `contextLength` of 20 000
      (budget 200 tokens ≈ 800 chars) the listing stops at the budget and the dropped names appear in one
      warning; a `disable-model-invocation: true` skill is absent from the section and present for `/skill:`.
- [x] `/skill:<name> arg1 arg2` on FakeProvider sends a user message equal to the `SKILL.md` body + the args;
      `/skill:nope` answers one line with the known names and sends nothing to the provider.
- [x] Bad frontmatter in one skill does not stop discovery of the others; the diagnostic names the file.
- [x] `--no-skills` → no `skills` section and no file reads under any skills root (spy on the walker).
- [x] The section's exact wrapper text ("The following skills provide specialized instructions. …
      Resolve relative paths against the skill dir.") matches spec §9.
- [ ] [HUMAN] In this repo (trusted): `tree-agent --json "Use the tech-docs skill to tell me where the Prefect
      llms.txt index is"` shows a `read` `tool_call` whose path ends with `.agents/skills/tech-docs/SKILL.md`
      and an answer naming `https://docs.prefect.io/llms.txt`; `/skill:tree-memory what do I know about
      Modal?` in the TUI injects the skill body (the model then asks for the memory server, which is not yet
      connected — expected until 209).
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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

### [SWE] 2026-10-25 01:42 — Implementation

**Files added**
- `apps/harness/src/skills/skills.ts` — `discoverSkills({cwd, trusted, env}) → {skills, diagnostics}` (type `DiscoverSkills`), `skillRoots()`, `parseSkill()`, `parseFrontmatter()`, `skillMessage(skill, args)`, `parseSkillCommand(text)`. `Skill = {name, description, location, body, disableModelInvocation}`. Nothing throws and nothing logs: every problem is a `"<file>: <problem>"` string.
- `apps/harness/tests/skills/skills.test.ts` — roots order and trust gate, override, symlinks, diagnostics, the frontmatter subset, `/skill:` parsing, and this repo's 13 skills.

**Files changed**
- `src/prompt/system-prompt.ts` — `skillsSection(skills, budgetTokens) → {text, dropped}`, `capDescription`, `SKILLS_INTRO` (the §9 wrapper; the line break after "skill's file" is kept), `CHARS_PER_TOKEN = 4`, `DESCRIPTION_CAP = 250`. It escapes `& < >`.
- `src/session.ts` — new option `skills?: {budgetRatio, discover?} | null`. Absent or null means no discovery and no section, so every existing session test stays hermetic. `reload()` re-discovers (at startup and on `/reload`). The listing is computed once in `reload()` and again in `setModel()`, never per turn. New getters: `skills` (all, the hidden ones too), `skillNotices` (diagnostics prefixed `skills: `, plus at most one `skills: over the listing budget (N tokens), not listed: a, b`). `skillMessage(name, args)` throws `unknown skill "x"; known: a, b` (or `(none)`). `systemPrompt()` passes `skills:`.
- `src/cli.ts` — `--no-skills` (in USAGE, `CliArgs.skills`). Passes `{budgetRatio: settings.skills.listingBudgetRatio, discover: deps.discoverSkills}`, or `null`. `CliDeps.discoverSkills` is the walker seam (the same pattern as `createProvider`).
- `src/headless/print.ts` — writes each `skillNotices` line to stderr once, then checks the prompt. A prompt whose first token is `/skill:<name>` (argv, or stdin line 1) becomes the body + args. An unknown name writes one stderr line and returns exit 1 before anything is queued or sent.
- `src/tui/commands.ts` — a `skill:<name>` branch returns `{lines: [], prompt}`, and an unknown name is the one error line. `commandsFor(session)` adds one `skill:<name>` autocomplete entry per skill. `/help` gets a line `/skill:<name> [args] — run a skill (N): …`. `/reload` appends the skill notices.
- `src/tui/app.ts` — shows `skillNotices` as red notices at startup. A `prompt` result shows the typed `/skill:x args` as the user line and runs the body. While a run is going it is `steer(body)`, marked `(steer)`.
- `src/tui/input.ts` — the `COMMAND` regex was `[a-z][a-z:-]*` and is now `[a-z][a-z0-9:-]*`, because `/skill:run-pipelines-e2e` (a real fixture with a digit) was routed as a prompt.
- Tests: `tests/prompt/system-prompt.test.ts`, `tests/session.test.ts`, `tests/cli.test.ts` (`main (skills)`; `run()`/`spawnCli` now set `HOME` to the scratch home, because the real `~/.agents/skills` on this machine has 7 entries), `tests/tui/{commands,app,input}.test.ts`.
- No new dependency. The frontmatter parser is hand-written, so `package.json` and `pnpm-lock.yaml` are untouched.

**Bindings / decisions the spec leaves open**
- Roots, in order (later wins): `$TREE_AGENT_HOME|~/.tree-agent/skills`, `$HOME/.agents/skills` (`env.HOME || homedir()`). Then, trusted only: `.agents/skills` for each dir **from the git root down to cwd**, so the nearest one wins (the same direction as the `AGENTS.md` walk). Last comes `<cwd>/.tree-agent/skills`. The git root is the first ancestor with a `.git` entry (a dir or a worktree file). With no git root, only cwd is used, and parents are never walked. A directory reached by two roots (for example HOME is the git root) is read once (realpath dedupe).
- Inside a root, entries are sorted, dot entries are skipped, and only `<entry>/SKILL.md` is read. Symlinked dirs and roots are followed. A missing SKILL.md, a plain file or a dangling link is skipped quietly. EACCES and other read errors become diagnostics.
- Clash semantics: a later **root** overrides silently. Two dirs with the same `name` inside **one** root give the diagnostic `duplicate skill name "x" (also <other>); this one wins`, and the later dir wins. The same file reached via a symlink is not a duplicate.
- Skipped with a diagnostic: bad frontmatter (no `---` open or close, a top-level line that is not `key: value`, unclosed quotes), missing `name`, a `name` that breaks the Agent Skills rule (`^[a-z0-9]+(-[a-z0-9]+)*$`, ≤ 64), and missing or empty `description` (the listing needs it). Other keys (`metadata`, `allowed-tools`, `user-invocable`, `license`, …) are ignored together with their nested lines. `disable-model-invocation` is true only for `true|True|TRUE`.
- Parser subset: plain (with `#` comments and indented continuation lines), `'…'`, `"…"` (JSON escapes), and `|` / `>` with chomping indicators. This covers all 13 repo fixtures (0 diagnostics).
- Listing: sorted by name. The description is collapsed to one line, then capped at 250 code points (249 + `…`, so an emoji is never split). The budget is `floor(listingBudgetRatio × contextLength)` tokens at **4 chars/token**, the heuristic `provider/fake.ts` and the 900-token base-prompt test already use. The budget counts the whole section body, wrapper included. Skills are added until the next one would overflow, and that one and **every later one** are dropped (the listing stops; it never skips ahead). If nothing fits, the section is omitted.
- `/skill:` message = `SKILL.md` body (after the closing `---`, trimmed), then `\n\n` + args when args are given. It is spec-literal, with no header and no `$ARGUMENTS` substitution.

**Tests**
- Unit: 526 passed, 0 failed, 34 files (`make harness-tests`, live OpenRouter tests included). The suite had 454 before.
- Integration: N/A (none in this app).
- Mutation probes (files restored, sha1 verified): git walk reversed → 2 fail; hide filter removed → 5 fail; trust gate forced on → 3 fail; first-wins instead of later-wins → 4 fail; skip-and-continue budget → 1 fail (a test was added for this one after the first probe survived).

**Acceptance criteria**
- [x] Discovery order and override — `skills.test.ts > skillRoots > searches the four roots in the spec's order…`, `> reads only the two user-level roots in an untrusted cwd`, `discoverSkills > lists a skill in ~/.tree-agent/skills and .tree-agent/skills once, with the project path`, `> lets each later root win…`, `> follows a symlinked skill dir and a symlinked root`, `> reads no project root in an untrusted cwd`. Also `cli.test.ts > main (skills) > lists only the user-level skills in an untrusted cwd`.
- [x] 400-char description, 20 000 budget, hidden — `system-prompt.test.ts > skillsSection > lists a 400-char description as its first 249 chars + …`, `> stops at the budget…`, `> stops at the first skill that does not fit…`. `session.test.ts > AgentSession skills > with contextLength 20 000 stops the listing at 200 tokens and warns once with the dropped names` and `> hides a disable-model-invocation skill from the section and keeps it for /skill:`.
- [x] `/skill:` on FakeProvider — `cli.test.ts > main (skills) > /skill:<name> arg1 arg2 sends the SKILL.md body + the args…`, `> /skill: reaches a hidden skill, also as the first stdin line`, `> /skill:nope answers one line with the known names, sends nothing and exits 1`. TUI: `commands.test.ts > runCommand /skill:`, `app.test.ts > … sends /skill:x args as the SKILL.md body + args`, `> steers with the skill body…`.
- [x] Bad frontmatter — `skills.test.ts > keeps discovering after a bad skill and names the file in its diagnostic`, `cli.test.ts > prints each skill diagnostic once on stderr and still runs, stdout pure JSONL`, `app.test.ts` (startup notice).
- [x] `--no-skills` — `cli.test.ts > main (skills) > --no-skills: no skills section and the walker is never called` (a `vi.fn(discoverSkills)` injected through `CliDeps.discoverSkills`: 0 calls with the flag, 1 without). Also `session.test.ts > skills null (--no-skills): no walker call and no section`.
- [x] Wrapper text — `system-prompt.test.ts > skillsSection > matches the spec §9 block…` parses the §9 code block out of `docs/notes/tree-agent-harness-spec.md` and compares the two wrapper lines, `<available_skills>`, the indented `<skill>` line and the closing tag.
- [ ] [HUMAN] Repo `--json` tech-docs run: done live (evidence below), PASS on what it asks for. Still open for the human: `/skill:tree-memory what do I know about Modal?` **in the TUI**. The headless variant ran in a scratch copy (below).
- [x] Cadence green.

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
Checked 75 files … No fixes applied.        (lint-check)
pnpm exec tsc --noEmit                       (clean)
 Test Files  34 passed (34)
      Tests  526 passed (526)
biome check (harness)....Passed  (prettier/ruff Passed)

# [HUMAN] AC, repo root, trusted via --trust (writes nothing), fake HOME + TREE_AGENT_HOME,
# --permission-mode ask so anything but `read` would be denied (read-only guaranteed)
$ node apps/harness/src/cli.ts --trust --permission-mode ask --json "Use the tech-docs skill to tell me where the Prefect llms.txt index is"
exit=0, stderr empty
tool_call {"name": "read", "arguments": {"path": ".agents/skills/tech-docs/SKILL.md"}}
permission allow readOnly read
turn_end 'The Prefect `llms.txt` index is at https://docs.prefect.io/llms.txt. The skill (…) also lists a full reference at https://docs.prefect.io/llms-full.txt. …'
agent_end completed

# scratch cwd (copies of tree-memory + tech-docs, plus a bad/SKILL.md without name), fake HOME + TREE_AGENT_HOME
$ tree-agent --cwd work --trust "/skill:nope hi"
skills: …/work/.agents/skills/bad/SKILL.md: missing name
unknown skill "nope"; known: tech-docs, tree-memory
exit=1
$ tree-agent --cwd work --trust --no-skills "/skill:tree-memory hi"
unknown skill "tree-memory"; known: (none)
exit=1
$ tree-agent --cwd work --trust --permission-mode ask --json "/skill:tree-memory what do I know about Modal?"
first user message: 8668 chars, == SKILL.md body + "\n\nwhat do I know about Modal?" → True
tool_call bash `ls -a; find . -name "rag.md" -o -name "graphrag.md" -o -name "write.md" | head` → denied (ask, headless) → exit 2
```

**Notes for the Tester / PA**
- **PA follow-up (spec gap, seen live):** the `/skill:` message is spec-literal body + args, so a hidden skill's relative links (`tree-memory` → `rag.md`, `graphrag.md`, `write.md`) have no base dir. The listing's "Resolve relative paths against the skill dir" never reaches the model for a hidden skill, and the live model went looking with `find`. The body also contains Claude Code's `$ARGUMENTS` placeholder, which is not substituted (the args follow the body). Suggested fix for a later task: prepend one line `Skill dir: <dir>` (or substitute `$ARGUMENTS`), which needs the spec/AC wording changed first.
- In the repo run, the model passed a **relative** `read` path, not the absolute `location`. The AC ("ends with") holds, and `read` resolves it against cwd.
- `/skill:` in the TUI during a run is a steer (my choice, consistent with Enter). The transcript shows the typed command, while the JSONL and the provider get the full body.
- Headless: the skill check runs on the prompt only (argv, or stdin line 1). Later stdin lines starting with `/skill:` are sent literally.
- `/model` re-budgets the listing silently. The dropped warning appears only at startup and on `/reload`.
- After the repo run, the fake `TREE_AGENT_HOME` holds only `sessions/` (no `trusted.json`), and the repo has no `.tree-agent/`. `--trust` wrote nothing.
- The cross-root name clash is a **silent** override, and only a same-root clash is a diagnostic. This is a deliberate reading of the spec's "duplicate names": the AC wants "lists once with the project path", so overriding is the feature, not an error.
- `commandsFor(session)` is built once in the `App` constructor. `/skill:` autocomplete therefore does not pick up skills added by `/reload`, but the command itself does.
- `skills.test.ts > finds this repo's 13 skills…` is coupled to `.agents/skills/` on purpose. Adding or renaming a repo skill breaks it by design.
- `session.ts` is 481 lines. `agent-loop.ts` is untouched (180).
- Things to probe: a SKILL.md with a frontmatter style outside the subset (flow maps `{…}`, multi-line double-quoted strings: these become a diagnostic, not a crash); a skills root that is a file; a huge `~/.agents/skills` (60 skills) at 1 M context (no drop) vs a 20 k model; `/skill:` with a name that has uppercase letters (it routes as a prompt in the TUI, and headless answers unknown); autocomplete of `/skill:` in the TUI.

### [Tester] 2026-10-25 01:55 — QA

**Test summary**
- Format / lint / typecheck / pre-commit: PASS (`make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit`, exit 0; biome, prettier, ruff Passed)
- Unit tests: 526 passed / 0 failed, 34 files (no integration suite in this app)
- Warnings: 0
- `agent-loop.ts` 180 lines (<= 200); `grep -rln "provider/\|loop/\|tools/" apps/harness/src/tui` empty; `pnpm install --frozen-lockfile` "Already up to date"; `git status` shows no `.env*` and no spec-doc change (same 17 entries before and after my runs)
- Isolation: `HOME=/Users/pauliusztin pnpm exec vitest run tests/cli.test.ts tests/skills tests/session.test.ts` -> 157/157 passed with the real HOME (which holds 5 real skill dirs); the full cadence ran under the real HOME too

**E2E adversarial pass** (scratch drivers in `.../scratchpad/qa-207/p1.ts` .. `p6.ts`, fake HOME/TREE_AGENT_HOME, FakeProvider through `main()`, `runTui` with FakeTerminal, fs spy on fs and fs/promises)
- Happy path (live, read-only, repo root, `--trust --permission-mode ask --json`, fake HOME): exit 0, stderr empty, `tool_call read {"path":".agents/skills/tech-docs/SKILL.md"}`, answer contains `docs.prefect.io/llms.txt`, no `.tree-agent/` in the repo, fake home holds only `sessions/` -> PASS
- Discovery order: same `x` in all 6 roots (git root, mid, cwd `.agents`, user x2, `.tree-agent`) -> listed once, `<cwd>/.tree-agent/skills/x`. Removing roots one at a time -> next-later wins: cwd `.agents` -> mid -> git root -> `~/.agents` -> `~/.tree-agent` -> empty. Nested git (cwd 2 below root): nearest wins. PASS
- Symlinked skill dir and symlinked SKILL.md -> both found, no false duplicate. Symlink loops (`loop/loop`, self-link dir, `SKILL.md -> SKILL.md`, root symlinked to its parent) -> 1 ms, ELOOP diagnostics naming the file, no hang. Root that is a regular file / dir without SKILL.md / plain file in root -> silently skipped. `SKILL.md` as dir -> `EISDIR` diagnostic. chmod 000 -> `EACCES` diagnostic (restored afterwards). Same name twice in one root -> diagnostic, later dir wins. PASS
- Trust gate (fs spy, `main()`): untrusted cwd -> listed `[usr]`, 0 project-root paths touched, user root touched; `--trust` -> `[proj, pt, usr]`, 14 project-root paths touched, one stderr line `skills: <repo>/.agents/skills/bad/SKILL.md: missing name`. PASS
- Frontmatter (`parseSkill`): missing closing `---` -> diagnostic; CRLF, BOM, `"x"`, `'it''s'`, `>` folded, `|` literal, nested `metadata:` map, inline `[a, b]`, tabs -> parsed. `{a: b}` as description -> literal string; as name -> bad-name diagnostic. `Foo` and `foo bar` -> diagnostic (no crash), diagnostics name the file, other skills still discovered. `disable-model-invocation`: `true` and `"true"` -> hidden, `yes`/`false` -> not hidden. PASS
- **Frontmatter: unindented YAML block list under an ignored key -> skill rejected. FAIL** (see below)
- Budget: 400 x `a` -> 249 `a` + `…` (250 chars) PASS. ctx 20 000 with 12 skills -> section 774 chars (<= 800), 1 listed, one notice naming the other 11 PASS. ctx 0 / 1 / 100 / 1000 / 5000 / 15 000 (wrapper alone exceeds budget) -> no `<skills>`, no half-open `<available_skills>`, one notice PASS. 73 skills at 1 M -> 73 listed, 0 notices PASS. Wrapper lines byte-equal spec §9 (parsed from `docs/notes/tree-agent-harness-spec.md`) PASS. Hidden skill absent from section and from `dropped`, present in `session.skills` and for `/skill:` PASS
- `/skill:` message: body `BODY LINE 1\n\nBODY LINE 2` + `\n\n` + args is exact for no args, `a b`, quotes/backticks/`$HOME`, `héllo 日本語 🙂`, padded, multi-line args. Headless `/skill:run-pipelines-e2e <args>` recognised (digits). `/skill:nope arg` -> one stderr line `unknown skill "nope"; known: proj, pt, run-pipelines-e2e, usr`, exit 1, 0 provider requests, stdout empty (also with `--json`). TUI: `/skill:run-pipelines-e2e go` sent as body + `\n\ngo`; `/skill:nope x` -> error line, no request. Steer during a run: `tests/tui/app.test.ts > steers with the skill body when /skill: is sent during a run` (not driven live). PASS
- `--no-skills`: no `<skills>` section, fs spy saw 0 paths containing `skills`; `/skill:run-pipelines-e2e` with `--no-skills` -> `unknown skill "run-pipelines-e2e"; known: (none)`, exit 1, 0 requests, no crash. PASS
- `/reload`: skill added after start absent before, present after, and in the next prompt; TUI `/skill:added-later` works after reload; `/help` shows `(3)`. `/model` small (2000 ctx) -> section dropped, one notice, no crash; back to 1 M -> 73 listed again. PASS
- Mutation 1 (first-wins instead of later-wins): 4 tests fail. Mutation 2 (hidden filter dropped): 5 tests fail. Both files restored, sha1 identical. PASS

**Acceptance criteria**
- [x] PASS — Discovery order and override — `skills.test.ts > skillRoots/discoverSkills` (order, override, symlink, untrusted); my p1/p3 runs above
- [x] PASS — 400-char -> 249 + `…`; 20 000 ctx stops at budget with one warning; hidden skill absent/present for `/skill:` — p2 output above; `system-prompt.test.ts`, `session.test.ts > AgentSession skills`
- [x] PASS — `/skill:` on FakeProvider body + args; `/skill:nope` one line, nothing sent — p3/p4/p6 output above; `cli.test.ts > main (skills)`
- [x] PASS — Bad frontmatter does not stop the others; diagnostic names the file — p1 `misc`, p3 (`bad/SKILL.md: missing name`)
- [x] PASS — `--no-skills`: no section, no reads under any skills root — fs spy in p3 (0 skills paths)
- [x] PASS — Wrapper text matches spec §9 — p2 compare against the spec file
- [ ] [HUMAN] — repo `--json` tech-docs run reproduced by me (PASS on that half); TUI `/skill:tree-memory what do I know about Modal?` left for the human, not ticked
- [x] PASS — cadence green (526/526)

**Failure (1 issue)**
- FAIL — Scope: "the standard's optional keys beyond the three named are ignored, not rejected" (SWE log: other keys are "ignored together with their nested lines")
  Repro (`.../qa-207/p6.ts`, `main()` headless, FakeProvider): `~/.agents/skills/usr/SKILL.md` =
  `---\nname: usr\ndescription: d\nallowed-tools:\n- Bash\n---\nBODY`
  Expected: skill `usr` listed, `allowed-tools` ignored (unindented block sequences under a key are valid YAML; PyYAML's default dump emits exactly this form).
  Actual: stderr `skills: <home>/.agents/skills/usr/SKILL.md: bad frontmatter: line 5: expected "key: value"`; system prompt lists only `[ok]`; `/skill:usr` is unknown.
  Cause: `apps/harness/src/skills/skills.ts` `parseFrontmatter`, the continuation loop `while (next < end && /^(\s|$)/.test(lines[next] ?? "")) next++` (line ~213) swallows only indented/blank lines, so a column-0 `- Bash` falls through to `KEY.exec` (line ~210) and throws.
  Fix: also treat lines matching `/^-(\s|$)/` as continuation of the key above, and add a regression test in `tests/skills/skills.test.ts` with that exact fixture (and an assertion that the skill is listed).

**Other issues found (not blocking)**
- Headless bare `/skill:` (no name) is sent to the model literally as the prompt `/skill:`; the TUI answers `unknown skill ""`. Inconsistent, no crash.
- TUI `/skill:Tree-Memory hi` (uppercase) routes as a plain prompt and is sent to the model verbatim; headless answers unknown skill. A one-line notice would be kinder than a silent send.
- An empty-body skill with args produces the message `"\n\nq"`; with no args it sends an empty user message.
- `--no-skills` + `/skill:x` says `known: (none)`, which hides that skills are disabled.
- Other YAML forms outside the subset are rejected with a diagnostic: dotted/quoted top-level keys (`x.y: 1`, `"description": d`).
- `skillsSection` XML-escapes `&` in the `<location>`, so a path containing `&` is not directly readable by the model (rare).
- `/skill:` autocomplete is not refreshed after `/reload` (SWE already flagged); headless follow-up stdin lines beginning `/skill:` are sent literally (documented).

**VERDICT: FAIL** (1 issue; everything else PASS)

### [SWE] 2026-10-25 02:05 — Fixes (Tester QA 01:55)

**Files modified**
- `apps/harness/src/skills/skills.ts`
  - `parseFrontmatter`: a column-0 `- item` line (`/^-(\s|$)/`) now continues the key above, the same as an indented line. Nested indented lines under that item are skipped too. A stray list item with no key above is skipped like a comment.
  - `KEY` now accepts `"…"` / `'…'` quoted keys (unquoted, so `"description": d` maps to `description`) and plain keys up to the first `:` that is followed by whitespace or the end of the line (`x.y`, `a key`, `a:b`). `description: see http://x` still keys on `description`.
  - It still throws only on a missing `---` fence, an indented first key, a line that is no key, continuation, list item or comment (`just words`, an unclosed quoted key), or a broken quoted value of a read key.
  - `parseSkillCommand`: the `/skill:` prefix is case-insensitive and the name may be empty. `/skill:` gives `{name: "", …}`, which reaches the exact-name lookup.
  - `skillMessage`: `[body, args].filter(Boolean).join("\n\n")`, so an empty body gives the args alone.
- `apps/harness/src/session.ts`, `skillMessage`: when skills are absent or null, it throws `unknown skill "x"; skills are disabled (--no-skills)`. An empty result throws `skill "x" has an empty body and no args; nothing sent`. Both callers (TUI command, headless) go through it, so neither one needs a special case.
- `apps/harness/src/headless/print.ts`: **follow-up stdin lines starting with `/skill:` are now resolved like the first line** (the cheaper consistent option). Every line is resolved before the sink, the signals or the queue are set up. If any line has an unknown name, an empty message, or skills disabled, one stderr line is written, exit 1, and nothing is sent.
- `apps/harness/src/tui/input.ts`, `route()`: `parseSkillCommand` is checked before `COMMAND`. `/skill:Tree-Memory hi` and `/SKILL:x` route as the command `skill:<name as typed>`, so the exact-name lookup gives the unknown-skill notice. `COMMAND` itself is unchanged, so `/Foo bar` is still a prompt. `src/tui` still imports nothing from `provider/`, `loop/` or `tools/`.
- Unchanged: the §9 wrapper, the non-empty-body message format, `.agents/skills/`, `.env*`, the untracked spec doc.

**New tests**
- `tests/skills/skills.test.ts`
  - `discoverSkills > lists a skill whose ignored key holds an unindented block list (PyYAML's dump)`: the Tester's exact fixture; 0 diagnostics, the skill is listed.
  - `parseFrontmatter > skips unindented block lists, also with nested lines, under ignored keys`
  - `parseFrontmatter > accepts dotted, spaced and colon keys as ignored keys` (includes the `description: see http://x.y/z` guard)
  - `parseFrontmatter > maps quoted keys to the real key`
  - `parseFrontmatter > throws on a line that is no key` and `> throws on an unclosed quoted key`
  - `/skill: messages > is the args alone for an empty body, and empty with no args either`
  - parse cases `/skill:` → `{name: ""}`, `/skill: x`, `/Skill:Tree-Memory hi` (the old `/skill:` → `undefined` case is flipped)
- `tests/session.test.ts`
  - `skills %s: no walker call and no section` now expects `skills are disabled (--no-skills)`
  - `says known: (none) when skills are enabled and none were found`
  - `sends an empty-body skill's args alone and refuses an empty message`
- `tests/cli.test.ts > main (skills)`
  - `resolves a /skill: follow-up stdin line like the first line`
  - `%s answers one line, sends nothing and exits 1`, for an unknown follow-up, a bare `/skill:` argv and a bare `/skill:` stdin line
  - `--no-skills + /skill:x says skills are disabled`
- `tests/tui/input.test.ts`: `/skill:Tree-Memory hi`, `/SKILL:x` (running) and `/skill:` route as `skill:` commands.
- `tests/tui/commands.test.ts`: `/skill:Tree-Memory (exact-name lookup) answers one error line` and `/skill: …`.
- `tests/tui/app.test.ts`: `answers /skill:X (uppercase) with the unknown-skill notice and sends nothing`.
- Mutation probe: with the list-item continuation removed, `skips unindented block lists, also with nested lines…` fails, and the file was restored. The Tester's fixture alone is also covered by the stray-item skip, so both mechanisms are needed: the skip covers the `- Bash` line, and the continuation covers the indented lines under it.

**Tests**
- Unit: 548 passed, 0 failed, 34 files (`make harness-tests`, live OpenRouter included). Before: 526.
- Integration: N/A.

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
exit=0 · Checked 75 files … No fixes applied · tsc clean · Test Files 34 passed (34) · Tests 548 passed (548)
prettier / ruff check / ruff format / biome check (harness) Passed

$ node /private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/qa-207/p6.ts /private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/swe-207      # the Tester's repro (stdin: "/skill:ok first\n/skill:ok second\n")
0 "" [ '{"role":"user","content":"OKBODY\\n\\nfirst"}', '{"role":"user","content":"OKBODY\\n\\nsecond"}' ]
sys listed: [ 'ok', 'usr' ]          # usr is listed now; stderr empty; the follow-up /skill: is resolved

$ node /private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/swe-207/smoke.ts /private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/swe-207   # main() + FakeProvider, fake HOME/TREE_AGENT_HOME
["/skill:"]              → exit 1 | stderr "unknown skill \"\"; known: empty, odd\n" | sent []
["/skill:Odd","hi"]      → exit 1 | stderr "unknown skill \"Odd\"; known: empty, odd\n" | sent []
["/skill:odd","hi"]      → exit 0 | stderr "" | sent ["ODD\n\nhi"] | listed [ 'empty', 'odd' ]   # odd = "name"/x.y/'description'/- list keys
["/skill:empty"]         → exit 1 | stderr "skill \"empty\" has an empty body and no args; nothing sent\n" | sent []
["/skill:empty","q"]     → exit 0 | sent ["q"]
["--no-skills","/skill:odd"] → exit 1 | stderr "unknown skill \"odd\"; skills are disabled (--no-skills)\n" | sent []
```

**Notes**
- Still not handled (not asked for): `&` in `<location>` is XML-escaped, `/skill:` autocomplete is not refreshed after `/reload`, and JSON-style `"key":value` with no space after the colon is still a diagnostic.
- `[HUMAN]` box untouched.

### [Tester] 2026-10-25 02:20 — Re-QA (frontmatter fix)

**Test summary**
- Format / lint / typecheck / pre-commit: PASS (`make harness-lint-fix && harness-lint-check && harness-typecheck`: 75 files clean, tsc 0 errors; `make pre-commit` all hooks Passed)
- Harness tests: 548 passed / 0 failed, 34 files (the `[tree-agent] warn:` lines are expected app logs from negative-path tests, not runner warnings)
- `tests/cli.test.ts` with real HOME exported: 77 passed
- `agent-loop.ts` 180 lines; `grep -rln "provider/\|loop/\|tools/" apps/harness/src/tui` empty; `pnpm install --frozen-lockfile` "Already up to date"; `git status` 17 entries as before, no `.env*`, spec doc untouched (mtime 18:30 yesterday)

**FAIL #1 re-verified**
- `p6.ts` (stdin `/skill:ok first` + `/skill:ok second`, user skill with `allowed-tools:\n- Bash`): exit 0, stderr empty, both messages sent (`OKBODY\n\nfirst`, `OKBODY\n\nsecond`), `usr` and `ok` both listed. PASS
- `p1.ts` `unindentedList`: skill `a` listed, 0 diagnostics. PASS. Also `dotkey`, `quotedkey`, `nestedMeta`, `indentedList`, `flowlistInline` all parse.

**Parser break variants (`parseSkill`, LF and CRLF each; all no crash)**
- `allowed-tools:\n- Bash\ndescription: d` (name above): description read. PASS
- `description:\n- one\n- two` -> string `"- one - two"` (+ `disable-model-invocation: true` after it still read). Not a crash, not a diagnostic: odd but a string. Note only.
- nested list with indented sub-lines `- a\n  - sub: 1\n    more: 2`, list item with colon `- key: value`, `- name: evil` / `- description: evil2` under an ignored key: ignored, real name/description kept. PASS
- `- stray` and bare `-` before the first key: skipped. PASS
- ignored `notes: |` block whose lines look like `name: evil`: ignored. PASS
- `"name": x`, `'name': x` map; JSON-style `"name":x` / `foo:bar` / plain text line: rejected with `<file>/SKILL.md: bad frontmatter: line N: expected "key: value"` through `discoverSkills` (file named, sibling good skill with CRLF + list still loaded). PASS
- Timing: 5 000 lines 0.2-1.8 ms; 50 000 lines 2.7-12 ms (keys, list, block, comments); 5 000-line description continuation 0.8 ms; 100 KB single non-key line and 100 KB bad quoted key: 0.4 ms. No quadratic or regex blow-up. PASS
- Cosmetic (not blocking): a column-0 `- item` right after `description: >` / `|` lines is swallowed into the block and, for `>`, defeats the indent cut (`"hello   world - item"`). Invalid YAML anyway.

**Fold-ins (headless via `main`, FakeProvider; TUI via FakeTerminal + runTui)**
- bare `/skill:` -> `unknown skill ""; known: ...`, exit 1, 0 requests. PASS
- stdin 3 lines, line 2 `/skill:nope` -> one stderr line, exit 1, 0 requests; bad line 3 or follow-up `/skill:` likewise 0 requests. PASS
- `/skill:Tree-Memory`, `/SKILL:x`: prefix case-insensitive, name exact (`/SKILL:ok` and `/Skill:hid z` resolve, `Tree-Memory` -> unknown). PASS
- TUI: `/skill:Tree-Memory`, `/Skill:Tree-Memory hi`, `/skill:` -> one notice each (`unknown skill "Tree-Memory"; known: ...`), 0 requests, session alive, `/SKILL:x` and `/skill:listy z` (list frontmatter) sent bodies; `/Foo bar` goes to the model verbatim (headless and TUI). PASS
- `--no-skills` + `/skill:ok` / bare / follow-up: `unknown skill "ok"; skills are disabled (--no-skills)`, exit 1, 0 requests; no skills path touched. PASS
- Empty-body skill: `/skill:empty some args` sends `some args` alone (headless and TUI); no args -> `skill "empty" has an empty body and no args; nothing sent`, exit 1 / TUI notice, 0 requests. PASS
- Hidden skill: absent from `<available_skills>` (listed: empty, ok, tree-memory), invocable by name. CRLF stdin lines and extra spaces work.

**Regression**
- Discovery order `p1.ts`: all six roots same name -> `.tree-agent/skills` wins; removing roots one by one falls back in the documented order; untrusted -> only `~/.agents`. PASS
- Trust gate `p3.ts`: untrusted 0 project-root paths touched, `--trust` 14. PASS
- Budget `p2.ts`: 20 000 context -> 774-char section, 1 listed, ONE `skills: over the listing budget (200 tokens), not listed: ...` notice; tiny contexts drop the section; 73/73 at 1M; setModel and /reload re-evaluate. PASS
- §9 wrapper lines match spec byte-for-byte (`spec wrapper lines match: true true true`). PASS
- (`p2.ts` ends with an uncaught throw on its own old empty-body probe: that is the new intended behavior, not a defect.)

**Mutations (restored byte-identically; sha1 verified)**
- Remove list item from `isContinuation`: 1 test fails (`skips unindented block lists, also with nested lines, under ignored keys`). KILLED
- Remove `/i` from `parseSkillCommand`: 3 fail (`parses "/Skill:Tree-Memory hi"`, `route '/SKILL:x'`, `resolves a /skill: follow-up stdin line like the first line`). KILLED
- Remove the skill block from `route()` in `tui/input.ts`: 3 fail (two `route` cases + `answers /skill:X (uppercase) with the unknown-skill notice`). KILLED

**Other issues found**
- None blocking. `description:` as a block list yields `"- one - two"` rather than a diagnostic; acceptable.

[HUMAN] TUI `/skill:tree-memory what do I know about Modal?` box left unticked.

**VERDICT: PASS**
