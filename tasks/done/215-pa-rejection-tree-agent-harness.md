---
status: done
feature: tree-agent-harness
---

# [PA rejection] tree-agent harness — the `/skill:` message gets its skill-dir header, the `llm()` call budget covers the feature's own worked example, `--help` prints usage, and one `[HUMAN]` AC is reworded to match the shipped classifier

Tags: `rollup`, `pa-rejection`, `harness`
Refs: `tasks/done/202-harness-toolchain-node-pnpm-vitest-biome.md` … `tasks/done/213-harness-docs-readmes-and-stack-note.md` (the twelve tasks of `feature: tree-agent-harness`, PR #48, head b31d8ca)
Depends on: None
Blocks: —
Implements: ADR-016 (no change to any recorded decision — see "Documentation discipline" below)

## Scope

The feature PASSED automated QA (856 vitest tests, the live OpenRouter tests, the Testers' live
headless and TUI walkthroughs) and the PA's own spot-checks (headless `ok` run, an `ask`-mode write
exiting 2 with the `permission` event before the `tool_result`, the repo-root `--trust --json` memory
search calling `mcp__tree_memory_local__search_memory` with `source: "classifier"` / `allow`, README link
check clean, ADR-016 and the glossary present in the diff). It failed the user-perspective acceptance
review on four points, all in the two flagship paths the chapter sells — the memory skill and the RLM
example — plus one CLI-politeness gap and one stale acceptance criterion. The SWE fixes every issue
below in ONE coordinated pass, then hands back to the Tester (full pipeline re-runs from QA, harness
cadence: `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make
harness-tests && make pre-commit`).

The spec (`docs/notes/tree-agent-harness-spec.md`) and the harness README are edited IN THIS TASK with
the verbatim wording below — the spec is the contract and must agree with the code when this lands.
Nothing under `apps/memory` is touched (the stale harness lines in `apps/memory/README.md` are task 216).

**Documentation discipline.** No ADR change: ADR-016 §4 (skills: "name + description + path in the
prompt, the body on `read`") and §9 (`llm()` depth 1 by default, budgets) stay true; a default budget
value and a header line in a user message are not architectural decisions. No new glossary term.

## Acceptance Criteria

- [x] Issue 1 — `/skill:<name> [args]` sends `# <absolute path of SKILL.md>` as line 1, then the `SKILL.md`
      body; every literal `$ARGUMENTS` in the body is replaced by the trimmed args (an empty string when there
      are none); when the body has no `$ARGUMENTS`, the args follow the body after one blank line (as today).
      Unit: `skillMessage` on a body `Do it.` with args `arg1 arg2` → `# /abs/SKILL.md\nDo it.\n\narg1 arg2`;
      on a body `Run: $ARGUMENTS now` with args `q` → `# /abs/SKILL.md\nRun: q now` (nothing appended).
      Emptiness is decided BEFORE the header is added: an empty body with no args still throws `skill "<name>"
      has an empty body and no args; nothing sent`; an empty body with args `q` sends `# /abs/SKILL.md\nq`.
      Live (FakeProvider via `main`, this repo as a TRUSTED cwd — `--trust`, or the repo root listed in
      `trusted.json`, since `.agents/skills` is project-level): `/skill:tree-memory what do I know about
      Modal?` yields a user message whose first line ends with `.agents/skills/tree-memory/SKILL.md` and whose
      body line reads `The query or instruction to run is: what do I know about Modal?`.
- [x] Issue 1 docs — spec §9's sentence becomes: "`/skill:<name> [args]` (TUI and headless) injects, as one
      user message, `# <absolute SKILL.md path>` on the first line and then the full `SKILL.md` body with every
      `$ARGUMENTS` replaced by the args (appended after a blank line when the body has none), so relative
      links in the body resolve against the skill dir." README `## Skills` loses the sentence "The `/skill:`
      message carries no skill-directory header … spell out the path in the args if needed." and says the
      same as the spec. `tasks/done/207-…md` AC line "`/skill:<name> arg1 arg2` on FakeProvider sends a user
      message equal to the `SKILL.md` body + the args" is reworded to the new shape (the box stays ticked
      with a "(superseded by 215)" note).
- [x] Issue 2 — `codemode.llm.maxCalls` defaults to **64** in `src/config/settings.ts`; `Settings.parse({})`
      reports 64; the error text still names the configured value (`llm budget exceeded: maxCalls=64 per
      script (codemode.llm.maxCalls)` with defaults). The §10.6 worked-example test in `tests/session.test.ts`
      runs with the DEFAULT budget (the `maxCalls: 41` override is deleted) and still grows `ctx.messages` by
      exactly 2; the "17th call throws" test passes `llm: { maxCalls: 16 }` explicitly (or asserts the 65th
      with the default) — either way it still proves the cap and the `catch`.
- [x] Issue 2 docs — every "16" for `maxCalls` becomes 64: spec §16 listing (`maxCalls: z.number().default(64)`),
      spec §10.6 Budgets line (`maxCalls` 64 per script), README settings table row, README "`llm()`: depth and
      budgets" paragraph (`maxCalls` 64 … `llm budget exceeded: maxCalls=64 …`). The README worked-example
      paragraph that starts "**This script makes up to 41 `llm()` calls, above the default `maxCalls` of
      16**" and its `{ "codemode": { "llm": { "maxCalls": 48 } } }` block are DELETED and replaced by one
      sentence: "The example's 41 calls fit the default `maxCalls` (64); `maxTotalTokens` (400 000) and
      `timeoutMs` (60 000) are the cost and time bounds." `tasks/done/211-…md` AC "the 17th `llm()` in one
      script throws naming `maxCalls`" gets a "(default now 64 — task 215)" note; the box stays ticked.
- [x] Issue 3 — `tree-agent --help` and `tree-agent -h` print the usage text to **stdout** and exit **0**, in
      every mode (alone, with a prompt, with `--json`); an unknown option still prints its error + usage on
      stderr and exits 1; the usage text lists `--help`. `tests/cli.test.ts` covers `--help` → stdout + 0 and
      `--bogus` → stderr + 1. README `## CLI` line "There is no `--help` flag: an unknown option prints the
      error and the usage text and exits 1." becomes "`--help` / `-h` print the usage text and exit 0; an
      unknown option prints its error and the usage text on stderr and exits 1." Spec §15's `flags:` line
      gains `--help`.
- [x] Issue 4 — `tasks/done/206-…md` first `[HUMAN]` AC, the `auto` clause, reads verbatim: "In `auto`
      (`Shift+Tab`): `echo hi > note.txt` (inside the cwd) runs unprompted; `echo hi > /tmp/x` prompts with a
      reason naming a write outside the cwd (answer `n`); `rm -rf ~/.ssh` (same decoy `HOME`) prompts with a
      reason naming credentials/outside-cwd." No code change: the §11.1 prompt and the README `## Permissions`
      sentence ("a write to `/tmp` can be flagged and prompted; that is expected") are already right.
- [x] `grep -rn "maxCalls.*16\|16 per script" apps/harness/src apps/harness/README.md docs/notes/tree-agent-harness-spec.md`
      matches nothing; `grep -n "no skill-directory header\|no \`--help\` flag" apps/harness/README.md` matches nothing.
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green; `agent-loop.ts` still ≤ 200 lines; `grep -rn "apps/memory\|tree\.memory\|tree\.mcp"
      apps/harness/src` still matches nothing.
- [x] Tester re-runs the full QA suite and PASSES.
- [x] PA re-runs the acceptance review on the feature (`/squid-review` on PR #48) and ACCEPTS.

## Issues (detail)

### 1. `/skill:` sends a body that cannot find its own files — `apps/harness/src/skills/skills.ts:skillMessage`, `README.md:403`
- **What the user experiences (wrong):** `/skill:tree-memory what do I know about Modal?` — the repo's
  flagship memory skill — injects a body whose step 1 says "read [`rag.md`](rag.md)" and whose line 12 says
  "The query or instruction to run is: $ARGUMENTS", followed by the user's words. The model has no base
  directory for `rag.md` (the listing's "Resolve relative paths against the skill dir" never reaches a
  `disable-model-invocation` skill, which is not listed) and sees a literal `$ARGUMENTS`; the 207 live run
  shows it going `find`-ing. The README's remedy ("spell out the path in the args if needed") pushes the
  harness's job onto the user on the primary memory workflow.
- **What the spec / good UX implies (right):** the message should be self-locating, exactly as context
  files already are (`# <absolute path>\n<content>`, spec §7), and the Agent Skills `$ARGUMENTS` convention
  the repo's own skills use should be honoured.
- **Suggested fix:** in `skillMessage`, prepend `# ${skill.location}`; `body.includes("$ARGUMENTS")` →
  `replaceAll("$ARGUMENTS", args)` and do not append; else keep body + blank line + args. One function, the
  three test expectations in `tests/skills/skills.test.ts:456-462` and `tests/session.test.ts:977,1041`,
  the spec/README/207 sentences above.

### 2. The feature's own worked example fails with default settings — `src/config/settings.ts:51`, `README.md:341-366`, spec §10.6 / §16
- **What the user experiences (wrong):** a reader pastes the §10.6 map-reduce script (40 hits → 41 `llm()`
  calls) and the 17th call throws `llm budget exceeded: maxCalls=16`, failing the whole `Promise.all`.
  The 211 `[HUMAN]` demo (`limit 20` → 21 calls) needed the same override to pass. The README warns in
  bold and prints a `settings.json` override next to the example — an honest patch over a default that
  contradicts the one thing `llm()` is for.
- **What the spec / good UX implies (right):** the documented example runs as written. `maxCalls` is a loop
  guard, not the cost cap — `maxTotalTokens` (400 000) and `timeoutMs` (60 000) bound cost and time; 64
  covers the example (41) and the demo (21) with headroom and is still far under what 400 000 tokens allow.
- **Suggested fix:** `default(64)`; update the two tests named in the AC; replace the README override
  paragraph by the one-sentence note; align spec §16 and §10.6.

### 3. `tree-agent --help` is an error — `src/cli.ts:parseCli`, `README.md:61`
- **What the user experiences (wrong):** the first thing every CLI user types answers
  `Unknown option '--help'. To specify a positional argument starting with a '-', place it at the end of the
  command after '--', as in '-- "--help"` (Node's `parseArgs` text) and exits 1. The README documents this
  as "There is no `--help` flag".
- **What the spec / good UX implies (right):** `--help` is not forbidden by spec §15; usage on stdout,
  exit 0, is what a reader expects.
- **Suggested fix:** `help: { type: "boolean", short: "h", default: false }` in `parseArgs`; a
  `{ kind: "help" }` mode (or an early return in `main`) writing `USAGE` to `stdout` and returning 0;
  `--help` in the `USAGE` text; one test each way.

### 4. A `[HUMAN]` acceptance criterion contradicts the shipped classifier — `tasks/done/206-…md:63`
- **What the user experiences (wrong):** the human walking the 206 TUI AC is told `echo hi > /tmp/x` runs
  unprompted in `auto`; the verbatim §11.1 prompt calls a write outside the cwd dangerous and the PA
  reproduced it headless (`permission` → `deny`, `source: "headless"`, reason "overwrites a file at a path
  outside cwd"). The README already says a `/tmp` write may be prompted. The walkthrough would look like a
  bug.
- **What the spec / good UX implies (right):** the prompt is right (a write outside the cwd IS what the
  user should be asked about); the AC is wrong.
- **Suggested fix:** the verbatim replacement in the AC above; no code.

## User Stories

(Inherit from the original tasks — no new stories. Re-verify these pass after the fix:)

### Story: Reader runs the RLM worked example with a fresh install
1. User installs per the README, starts `make local-start`, runs `tree-agent` from the repo root and answers `y` to the trust prompt.
2. User pastes the README §"Worked example" request ("search my memory for 'pricing decisions' with top_k 40, summarise each hit with llm() in parallel, merge into one deduped list").
3. One `codemode` card appears with up to 41 `llm_call` rows nested under it; no `llm budget exceeded` error; the answer is the merged list; no `settings.json` was edited.

### Story: User queries memory through the skill in the TUI
1. User types `/skill:tree-memory what do I know about Modal?` and presses Enter.
2. The transcript shows the typed command; the JSONL `message` entry's first line is `# <repo>/.agents/skills/tree-memory/SKILL.md` and its body contains `The query or instruction to run is: what do I know about Modal?` with no `$ARGUMENTS`.
3. The model's first tool call is `read` on `<repo>/.agents/skills/tree-memory/rag.md` (or `graphrag.md`), then `mcp__tree_memory_local__search_memory`; no `find` or `bash` is used to locate the skill files.

### Story: New user asks the CLI for help
1. User runs `tree-agent --help`.
2. The usage text prints on stdout; the exit code is 0; nothing is written under `~/.tree-agent`.
3. User runs `tree-agent --bogus`; the error line and the usage text print on stderr; exit 1.

### Story: Human walks the 206 `auto` criterion
1. In the TUI, `Shift+Tab` to `auto`; ask for `echo hi > note.txt`: it runs without a prompt.
2. Ask for `echo hi > /tmp/x`: a permission dialog appears with a classifier reason naming a write outside the cwd; `n` denies and the model reports it was blocked.
3. Ask for `rm -rf ~/.ssh` under the decoy `HOME`: a dialog appears with a reason naming credentials or outside-cwd; `n`.

---

Blocked by: (none)

## Log

### [PA] 2026-10-25 05:35 — Grooming (rollup from acceptance review)

**Summary**
Four user-facing fixes in one pass: a self-locating `/skill:` message with `$ARGUMENTS` substitution, a
default `llm()` budget that covers the feature's own worked example, a working `--help`, and one stale
`[HUMAN]` AC reworded. Spec §9/§10.6/§15/§16 and the harness README move with the code.

**Key decisions**
- Header shape mirrors the context-file convention (`# <absolute path>`, spec §7) — one format, not a new `Skill dir:` label.
- `maxCalls` 64 rather than shrinking the example: the example is the chapter's listing and must run as printed; `maxTotalTokens` is the real cost cap.
- `--help` exits 0 on stdout; the unknown-option path is unchanged.
- The §11.1 classifier text stays; the 206 AC was wrong, not the gate.
- No ADR, no glossary change (nothing architectural; no new term).

**Dependencies**
- None (the feature's twelve tasks are done; this edits their output).

**User stories**
- 4 stories covering: the RLM example with defaults, the memory skill in the TUI, `--help`, the 206 `auto` walkthrough.

Ready for implementation.

### [SWE] 2026-10-25 06:40 — Implementation

**Files modified**
- `apps/harness/src/skills/skills.ts` — `skillMessage`: `# <location>` header line, every `$ARGUMENTS` replaced (function replacer, so `$$`/`$&` in args stay literal), else body + blank line + args; returns `""` when the content is empty, so the session's "empty body and no args" throw is unchanged (`session.ts` untouched).
- `apps/harness/src/config/settings.ts` — `codemode.llm.maxCalls` default 16 → 64.
- `apps/harness/src/cli.ts` — `help` option (`-h`), `Mode` `{ kind: "help" }` returned straight after parsing (wins alone, with a prompt, with `--json`, over later flag validation), `main` writes `USAGE` to stdout and returns 0 before any key/paths/settings work; `USAGE` gains `--help | -h (this text)`. An unknown option still throws inside `parseArgs` → stderr + 1.
- `apps/harness/README.md` — CLI line, usage block, `--help` flag row, exit-code 0 row, settings table 64, budgets paragraph 64, override paragraph + JSON block replaced by the supplied sentence, `## Skills` sentence per spec.
- `docs/notes/tree-agent-harness-spec.md` — §9 sentence, §10.6 Budgets 64, §15 `flags:` gains `--help | -h`, §16 `default(64)`.
- `tasks/done/206-…md` (auto clause verbatim), `tasks/done/207-…md` (AC reworded, "(superseded by 215)"), `tasks/done/211-…md` (AC "(default now 64 — task 215)").
- Tests: `tests/skills/skills.test.ts` (header + blank line + args; `$ARGUMENTS` substitution incl. repeated and empty args; `$` patterns literal; empty body → `# path\nq` / `""`), `tests/session.test.ts` (hidden-skill and empty-body expectations with the header; worked example on the default budget), `tests/codemode/llm.test.ts` (17th test passes `maxCalls: 16`; new "default maxCalls is 64: the 65th call throws naming it"), `tests/config/settings.test.ts` (DEFAULTS 64), `tests/cli.test.ts` (spawned `--help`/`-h`/with prompt/with `--json` → stdout + 0 + nothing written under the home; spawned `--bogus` → stderr + 1; `resolveMode` help rows; `/skill:` expectations with the header; new `/skill:tree-memory` on this repo via `main` + real `discoverSkills`), `tests/tui/app.test.ts`, `tests/tui/commands.test.ts` (header in the expected prompts).

**Tests**
- Unit (vitest, `make harness-tests`): 869 passing, 0 failing (was 856; red first: 27 failing for the right reasons before `src` changed).
- Integration: N/A (no suite by design).

**Acceptance criteria**
- [x] Issue 1 — `skills.test.ts > /skill: messages > *`, `session.test.ts > AgentSession skills > sends an empty-body skill's header and args…`, `cli.test.ts > main (skills) > /skill:tree-memory on this repo sends its SKILL.md path and substitutes $ARGUMENTS`.
- [x] Issue 1 docs — spec §9, README `## Skills`, 207 AC.
- [x] Issue 2 — `settings.test.ts > parses {} to the spec §16 defaults`, `llm.test.ts > the default maxCalls is 64…`, `> the 17th call…` (explicit 16), `session.test.ts > the §10.6 worked example grows the context by exactly 2…` (default budget).
- [x] Issue 2 docs — README table/budgets/worked example, spec §10.6/§16, 211 AC note.
- [x] Issue 3 — `cli.test.ts > tree-agent (spawned) > ["--help"]… / ["-h"]… / ["hi","--help"]… / ["--json","-h"]…`, `> prints an unknown option's error and the usage on stderr and exits 1`, `resolveMode` help rows; README CLI + spec §15.
- [x] Issue 4 — 206 AC reworded verbatim.
- [x] Greps: both match nothing; `agent-loop.ts` 180 lines; no `apps/memory|tree.memory|tree.mcp` in `apps/harness/src`.
- [x] Cadence green.

**Evidence**
```
$ make harness-tests
 Test Files  46 passed (46)
      Tests  869 passed (869)
$ make pre-commit
prettier....Passed  ruff check....Passed  ruff format....Passed  biome check (harness)....Passed

$ cd <scratch> && TREE_AGENT_HOME=<scratch-home> node apps/harness/src/cli.ts --help   (no OPENROUTER_API_KEY)
usage: tree-agent [flags]              (TUI, needs a terminal)
       ...
flags: --help | -h (this text)
       ...
exit=0   stderr bytes: 0   files under TREE_AGENT_HOME: 0
$ ... node apps/harness/src/cli.ts --bogus
Unknown option '--bogus'. ... (stderr) + usage (stderr)
exit=1   stdout bytes: 0

$ (repo root) main(["/skill:tree-memory what do I know about Modal?", "--trust", "--no-mcp"]) on FakeProvider
exit 0
# /Users/pauliusztin/.../building-agentic-systems/.agents/skills/tree-memory/SKILL.md
# Tree Memory

A facade over Tree's MCP server — ...

The query or instruction to run is: what do I know about Modal?
...
contains $ARGUMENTS: false
```

**Notes**
- Emptiness is decided on the content (after substitution, before the header): a body of only `$ARGUMENTS` with no args is also "empty body and no args; nothing sent".
- `--help` beats a later bad flag (`-h --max-turns 0` → help, exit 0); an unknown option anywhere still wins (parseArgs throws first), so `--help --bogus` → stderr + 1.
- Not run: a live OpenRouter `/skill:` run (the FakeProvider run via `main` covers the message shape; whether the model then `read`s `rag.md` is the [HUMAN]/PA story).
- `make harness-lint-check` reports 1 pre-existing warning (`tests/tui/app.test.ts:238` comma operator), untouched.
- The README worked-example story ("no `settings.json` edited") is covered by the session test on defaults; a live run is the PA's.

### [Tester] 2026-10-25 06:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make harness-lint-fix && harness-lint-check && harness-typecheck && harness-tests && pre-commit` exit 0; lint-fix produced no diff vs the pre-QA tree). Lint reports 1 pre-existing warning (`tests/tui/app.test.ts:238`, comma operator; not in this diff's hunks).
- Unit tests: 869 passed / 0 failed (46 files, includes the 2 live OpenRouter tests). No integration suite by design.
- Test-output warnings: none new (only the expected `[tree-agent] warn:` lines the tests assert on).
- Guards: `agent-loop.ts` = 180 lines (`apps/harness/src/loop/agent-loop.ts`), unchanged vs HEAD; `grep -rn "apps/memory\|tree\.memory\|tree\.mcp" apps/harness/src` empty; `pnpm install --frozen-lockfile` in `apps/harness` -> "Lockfile is up to date" / "Already up to date" (repo-root `pnpm` is broken by a missing corepack cache file, an environment issue unrelated to the diff); `git status`: no `.env*`, no `apps/memory`, the untracked spec doc and `tasks/216-...md` still untracked/intact, the PA entry is present in `tasks/done/213-...md`.

**E2E adversarial pass** (FakeProvider via `main`, scratch `TREE_AGENT_HOME`/HOME and project under `<scratchpad>/qa-215/`; scripts `skill.mts`, `tui.mts`, `repo.mts`, `set.mts`; `nofetch.mjs` made any `fetch` exit 99)
- Happy path: repo root, `main(["/skill:tree-memory what do I know about Modal?","--trust","--no-mcp"])` -> exit 0, line 1 `# /Users/.../building-agentic-systems/.agents/skills/tree-memory/SKILL.md` (absolute, no trailing whitespace), line 5 `The query or instruction to run is: what do I know about Modal?`, no `$ARGUMENTS` anywhere (PASS). Same result with `--cwd .` and `--cwd apps/..` (relative cwd still gives an absolute header).
- Break 1 (body with 0 / 1 / 3 `$ARGUMENTS`): `Do it.`+`arg1 arg2` -> `# <p>\nDo it.\n\narg1 arg2`; `Run: $ARGUMENTS now`+`q` -> `# <p>\nRun: q now` (nothing appended); 3 occurrences -> `a q b q c q`; no args on the 0-body -> `# <p>\nDo it.` (no trailing blank line); exactly one blank line between body and args (PASS).
- Break 2 (inside a code fence): `$ARGUMENTS` in a ``` block is replaced too (`` ```\nq\n``` ``). Documented behaviour: a plain `replaceAll`, no markdown awareness (PASS, note).
- Break 3 (hostile args): `a$&b $$ $1 \n \ é日本🙂 $\` $'` all literal; an arg of `$ARGUMENTS` stays literal (single pass, no recursion); an arg with an embedded newline is inserted verbatim (PASS).
- Break 4 (empty args): body only `$ARGUMENTS` + no args -> headless: stderr `skill "only" has an empty body and no args; nothing sent`, exit 1, provider never called; empty-body skill + no args -> same; TUI path (`runCommand`) -> `{lines:["skill \"only\" has an empty body and no args; nothing sent"], error:true}`, no prompt (one line, PASS). Empty body + args `q` -> `# <p>\nq` (PASS).
- Break 5 (not replaced): `$ARGUMENT` and `${ARGUMENTS}` stay literal (PASS). `$ARGUMENTSX` becomes `qX` (substring match; note below).
- Break 6 (symlinked skill dir): `proj/.agents/skills/sym -> realskills/sym` -> header uses the discovered path `.../proj/.agents/skills/sym/SKILL.md`, NOT the realpath (`location` is "as found", `skills.ts:18`). Relative links resolve through the symlink, so this is fine; documented (PASS).
- Break 7 (steer path / stdin): TUI steer test `tests/tui/app.test.ts` "steers with the skill body when /skill: is sent during a run" expects `# /skills/x/SKILL.md\n# X\nDo it.\n\nmore` (passes in the suite); stdin first-line `/skill:` and `/SKILL:zero q` (case-insensitive prefix) both carry the header (PASS).
- Break 8 (`--help`, spawned `node src/cli.ts` with `env -i`, no API key, fake `TREE_AGENT_HOME`, fetch trapped): `--help`, `-h`, `--help hi`, `hello -h`, `--json --help`, `-h --max-turns 0`, `--help --cwd /nonexistent`, `-h --permission-mode wrong`, `--resume nope -h` -> all exit 0, 10 stdout lines, 0 stderr bytes, 0 files/dirs created under the fake home, no `NETWORK-ATTEMPT`, no "key required" (PASS). `--help --bogus` and `--bogus --help` -> exit 1, stdout empty, stderr = `Unknown option '--bogus'...` + usage (PASS). `-- --help` -> treated as the positional prompt `--help`; with no key: `OPENROUTER_API_KEY is not set`, exit 1 (documented escape hatch, PASS). `--help | head -1` -> clean, no EPIPE. Usage text mentions `--help` once; README flag table has the `--help`, `-h` row; README exit-code row 0 mentions `--help` (PASS).
- Break 9 (`maxCalls`): `loadSettings` with `settings.json` `{}` -> 64; with `{"codemode":{"llm":{"maxCalls":16}}}` -> 16 (the explicit-16 test then proves the 17th throws); 65th default call throws `llm budget exceeded: maxCalls=64 per script (codemode.llm.maxCalls)` with `n: 64` (`llm.test.ts:257`). Worked-example test (`session.test.ts` ~1619) uses `codemode()` built from `Settings.parse({}).codemode.llm` -> no override (PASS).
- Mutation 1: remove the `replaceAll("$ARGUMENTS", ...)` -> 4 tests fail (`skills.test.ts` x3, `cli.test.ts > /skill:tree-memory on this repo...`). Variant: replacer function -> plain string `trimmed` -> 1 test fails (`keeps $ patterns in the args literal`). Restored byte-identically (`cmp`).
- Mutation 2: move the `--help` early return after `requireApiKey(env)` -> 4 spawned tests fail (`--help`, `-h`, `hi --help`, `--json -h`). Restored byte-identically (`cmp`); `git diff` equals the pre-QA diff.

**Acceptance criteria**
- [x] PASS — Issue 1 (header + `$ARGUMENTS` + append rule + emptiness before header): `skills.test.ts > /skill: messages > *` (header+blank+args, substitution, `$` literal, empty body), `session.test.ts` "sends an empty-body skill's header and args and refuses an empty message", `cli.test.ts:670` tree-memory on this repo; reproduced live above; `skills.ts:125-135`.
- [x] PASS — Issue 1 docs: spec line 397 (§9) and README line 401 (`## Skills`) carry the verbatim sentence; the "no skill-directory header" sentence is gone; `tasks/done/207` AC reworded with "(superseded by 215)", box still ticked.
- [x] PASS — Issue 2: `settings.ts:51` `default(64)`; `settings.test.ts` DEFAULTS 64; `llm.test.ts:257` (65th on defaults) and `:239` (17th with explicit 16); `session.test.ts` worked example with no override still grows `ctx.messages` by 2.
- [x] PASS — Issue 2 docs: spec §16 (line 709), §10.6 (line 486), README table (179), budgets paragraph (341), worked-example override paragraph + JSON block replaced by the exact sentence (364); `tasks/done/211` AC has "(default now 64 — task 215)". `grep maxCalls` over src, README, spec, docs/adrs, docs/glossary.md, root README: every default mention is 64; the only 16 is the explicit-16 test.
- [x] PASS — Issue 3: spawned tests in `cli.test.ts` (`--help`/`-h`/`hi --help`/`--json -h` -> stdout + 0; `--bogus` -> stderr + 1) plus the live runs above; README CLI line (61) is the verbatim sentence; spec §15 `flags:` line (678) gains `--help | -h`.
- [x] PASS — Issue 4: `tasks/done/206` first `[HUMAN]` AC auto clause matches the verbatim text; no code change for it.
- [x] PASS — Both acceptance greps (`maxCalls.*16\|16 per script`; `no skill-directory header\|no \`--help\` flag`) return nothing.
- [x] PASS — Cadence green; `agent-loop.ts` 180 <= 200; TUI/memory boundary grep empty.
- [x] PASS — Docs byte-equality re-run after the spec edit: `DANGER_CHECK_SYSTEM` (`decision-model.test.ts`, 10 pass), skills wrapper §9 (`system-prompt.test.ts`, 20 pass), `CODEMODE_PREAMBLE` vs spec §10.3 text (ad hoc script: `preamble byte-equal: true`).
- [ ] PA acceptance (`/squid-review` on PR #48): awaiting the PA; not a Tester item. The four `[HUMAN]` walkthroughs (live OpenRouter `/skill:` run, TUI, 206 `auto`) remain human/PA.

**Evidence**
```
$ make harness-tests            -> Test Files 46 passed (46) / Tests 869 passed (869)
$ make pre-commit               -> prettier, ruff check, ruff format, biome check (harness): Passed
$ env -i ... node src/cli.ts -h --max-turns 0   -> exit=0 stdout_lines=10 stderr_bytes=0 files=0
$ ... --help --bogus            -> exit=1 stdout_lines=0 stderr_bytes=735
$ preamble byte-equal: true
$ mutation 1 -> 4 failed | 865 passed; mutation 2 -> 4 failed | 865 passed; both restored (cmp clean)
```

**Other issues found** (none blocks)
- `cli.test.ts:670` reads the repo's real `.agents/skills/tree-memory/SKILL.md` and asserts the line `The query or instruction to run is: ...`; editing that skill's wording (or moving `$ARGUMENTS`) will fail a harness test. Intentional per the SWE; consider a looser assertion if the skill text changes often.
- `$ARGUMENTS` is replaced as a plain substring, so `$ARGUMENTSX` -> `<args>X`, and occurrences inside code fences are replaced. Matches the AC ("every literal `$ARGUMENTS`"); worth one README clause only if a skill ever needs the literal.
- No test covers `--help --bogus` (stderr, exit 1) or `-h --max-turns 0`; behaviour verified manually above and documented in the SWE notes.
- The symlinked skill header is the as-found path, not the realpath (see Break 6).

**VERDICT: PASS**

### [PA] 2026-10-25 05:58 — Acceptance Review (round 2, PR #48 head 9524e49)

**VERDICT: ACCEPT**

Re-verified each of the four 215 issues by running, not reading, plus two round-1 regression probes.
All acceptance criteria verified from the user's POV. Hand off to the PR Reviewer.

**Evidence** (scratch cwd + fake `TREE_AGENT_HOME`, `env -i`, no API key; FakeProvider via `main`)
```
$ make harness-tests                       -> Test Files 46 passed (46) / Tests 869 passed (869), exit 0
$ node <repo>/apps/harness/src/cli.ts --help      -> exit=0 stdout_lines=10 stderr_bytes=0 files_in_home=0
$ ... -h | hi --help | --json -h | -h --max-turns 0 -> exit=0 stdout_lines=10 stderr_bytes=0 (all four)
$ ... --bogus | --help --bogus             -> exit=1 stdout_bytes=0, stderr = Unknown option '--bogus'... + usage
$ main(["/skill:tree-memory what do I know about Modal?","--cwd",<repo>,"--trust","--no-mcp"])
  exit 0; line 1 = "# <repo>/.agents/skills/tree-memory/SKILL.md"; body line
  "The query or instruction to run is: what do I know about Modal?"; contains $ARGUMENTS = false
$ grep -rn maxCalls apps/harness/src README spec -> settings.ts default(64); README table 64, budgets 64,
  worked-example sentence "fit the default maxCalls (64)"; spec §10.6 64, §16 default(64); no "16 per script"
$ tasks/done/206 auto clause                -> verbatim per 215 Issue 4 (git show 9b94fc9)
regression 1: main(["hi","--trust","--no-mcp"])            -> exit 0, stdout "ok\n"
regression 2: main(["write a.md","--permission-mode","ask","--json"]) -> exit 2, a.md not written,
  --json: turn_start > turn_end > tool_call > permission(deny/headless) > tool_result > agent_end
guards: agent-loop.ts 180 lines; grep apps/memory|tree.memory|tree.mcp in apps/harness/src -> 0
```

**Non-blocking notes** (for the PR Reviewer; no product change needed)
- `apps/harness/README.md:93` flag-table cell says `--help` wins "whatever else is on the line"; an unknown option still wins (`--help --bogus` -> stderr, exit 1), as line 61 correctly states. One-cell wording overclaim.
- `tasks/done/206-…md:196` and `:243` are historical SWE notes that still describe the old "`/tmp/x` unprompted" expectation; the AC itself is now correct.

### [PR Reviewer] 2026-10-25 06:20 — Review (PR #48 head 9524e49)

**VERDICT: BLOCKERS**

Reviewed 184 files, +30 368 / −5 559 lines (every `apps/harness/src` and `tests` file, the config/meta files, ADR-016, the spec, the glossary, the READMEs). Blockers: 1; Nits: 16.

- BLOCKERS: filed rollup task `tasks/217-pr-review-rollup-tree-agent-harness.md`. The one Blocker is `[PA] [Documentation discipline]`: `docs/glossary.md` has no row for **Skill** (nor **Trusted directory**) although the diff adds `src/skills/`, the `skills` prompt section, `/skill:`, `--no-skills` and `skills.listingBudgetRatio`. Cure is a PA glossary pass (fold the three `[PA]` doc-drift Nits — glossary `llm()` "16 calls", spec §9 skills precedence wording, spec §16 missing `mcp.initializeTimeoutMs` — into the same pass). No code Blocker. Pipeline re-runs from PA grooming on the rollup; re-invoke me after PA ACCEPT + re-push.
- Verified green: `make harness-lint-check` (1 Biome warning, Nit 1), `make harness-typecheck`, `make harness-tests` (46 files / 869 tests), `pnpm install --frozen-lockfile --offline`; `agent-loop.ts` 180 lines / type-only imports; TUI import boundary; spec §9 / §10.3 / §11.1 texts byte-equal; README flag + settings tables match the code; sandbox globals, fail-closed gate, uncatchable headless deny, no `.mcp.json` read, trust gating, no secret logged, `.env.example` key name only; 20 KB MCP / codemode spill tested; JSONL crash-cut recovery, PromptQueue, abort-during-classifier tested.
