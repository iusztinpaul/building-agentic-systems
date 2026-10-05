# MCP Facade Skill — Dos and Don'ts

Lessons from rewriting `.agents/skills/tree-memory`, the skill that sits in front of Tree's MCP server.

An MCP server already ships a manual: every tool carries a description of what it takes and returns.

So a skill in front of it has exactly one job the server can't do.

It teaches the agent how to **chain** the tools.

Read → Read. Write → Write. Write → Read. Read → Write.

Everything else is duplication.

Below is what we got right and wrong, sorted by the four-step skill checklist: trigger, structure, steering, pruning.

---

## 1. Trigger

**Did OK**

- Kept it **user-invoked** (`disable-model-invocation: true`). Zero context load, and no "did the agent decide to load it?" unpredictability.
- Kept the description to one human-facing line. With no model invocation, a trigger list is wasted text.

**Did wrong / open trade-off**

- A facade has a hidden cost when user-invoked: the chain rules only reach the agent when you type `/tree-memory`. Ask "what do I know about X?" without the slash, and the agent uses the raw tools with no search budget and no provenance rule.
  - Decision for now: stay user-invoked. Revisit if the raw-tool behaviour starts hurting.
- We first filled `allowed-tools` with 27 spelled-out tool names (12 per server × 2 servers + 3 built-ins).
  - `allowed-tools` only pre-approves; it restricts nothing. With `defaultMode: auto` it bought nothing.
  - Good: no `allowed-tools` line; one `mcp__tree-memory` rule in settings if prompts ever bite.
  - Bad: a 27-name list that drifts every time the server adds a tool.

---

## 2. Structure

**Did OK**

- **Branched by memory mode.** The server registers different tools in `rag` and `graphrag`, so the mode is a natural branch.
  - `SKILL.md` finds the mode from one observable fact (`query_memory` present or absent), then points to `rag.md` or `graphrag.md`.
  - Each run loads only its own mode's chains.
- **Branched by intent — asymmetrically.** Write chains moved to `write.md` behind a pointer; read chains stayed in `SKILL.md`.
  - Why: the branch test is "inline what nearly every run needs, disclose what only some runs need".
  - Reads are the default branch, and write runs use them too: checking an ingest landed is a search, and the map after an ingest gives the map's fixed messages.
  - A `read.md` would make the commonest run load three files (`SKILL.md` → mode file → `read.md`) instead of two.
  - Good: a pure question loads `SKILL.md` + `rag.md` and never sees ingest receipts.
  - Bad: a symmetric `read.md` / `write.md` split for tidiness, paying an extra file on almost every run.
  - Trade-off accepted: `SKILL.md` mixes the router with the read chains, so it's less clean as a pure router.
- **Added a step spine with checkable completion:**
  1. Find the mode.
  2. Pick the chain.
  3. Run it until done.
     - A read is done when every claim carries its provenance, or you said "Not in memory" and named the searches.
     - A write is done when every source has its receipt reported.

**Did wrong**

- The first rewrite was a **per-tool manual**: signatures, defaults and return schemas for every tool.
  - That's a copy of the tool descriptions the agent already reads from the server: duplication, plus a second place to drift.
  - Good: "after `ingest_url`, an empty search means *not written yet*" — a cross-tool decision.
  - Bad: "`search_memory(query, top_k=10)` returns `{parents, outcome, search_mode}`" — already in the docstring.
- The original skill had **no completion criteria** outside "find the mode": all reference, no steps. That invites stopping too early.
- **Tests pinned skill text.** A docs-guard test required CLI commands (`make memory-search`, `make memory-visualize-structure`) to appear in `SKILL.md`.
  - That kept a no-op line alive, because the agent uses the MCP tools, not the CLI.
  - We removed `SKILL.md` from that test. Pin only what stops a real regression (e.g. the two-✅ rows in the mode table).

---

## 3. Steering

**Did OK**

- **"Provenance" as the leading word.** It replaced four scattered rules:
  - label web text as web;
  - group rag passages by document `source_uri`;
  - quote graph rows with their document;
  - name what was searched when answering "Not in memory".

  One word, one idea: every claim carries where it came from.
- Kept "chain" and "receipt" as consistent anchors across all files.
- Wrote rule headings as **positive targets**: "each retry a new angle", "relay the map's two answers verbatim", "offer the web, run it only on a yes". Bad examples stay, because the repo's AGENTS.md requires good-and-bad pairs. They now sit under positive rules instead of being the rule.

**Did wrong**

- The early version steered with prohibitions: "never a synonym", "never papered over", "do not work around a missing tool". Naming the elephant makes it more available.
- The search budget had a **loophole**: it counted only `search_memory`. In graphrag the agent could spend 3 calls there, 3 on `query_memory`, then a deep search.
  - Good: "at most 3 memory-reader calls, counted together".
  - Bad: "at most 3 `search_memory` calls" in a mode with three readers.

---

## 4. Pruning

**Did OK — no-ops and sediment removed**

- Removed "Present results human-readably": the agent does that by default.
- Removed "Count calls, never tokens": a leftover from an old failure; no agent budgets by tokens unprompted.
- Removed the "Role in a chain" table column, which restated the rules below it. It's now a short purpose per tool.
- Removed the duplicate "answer + picture" rule that lived in `SKILL.md`, `rag.md` and `graphrag.md` at once.
- Removed `session_uri` advice the agent couldn't act on (it has no session id to pass).

**Did wrong — drift that went unnoticed**

The skill had drifted from the server code. Every one of these was found only by reading the tool implementations:

- `episodes` as a node type — it doesn't exist; the closest type is `event`.
- Two "Memory is empty" / "No results" messages documented for both modes; they only exist in rag.
- "`Read` the deep-search files" — impossible against the cloud server, because the files live on the remote host.
- No warning that ingesting makes the topic map stale (ingest never clusters).
- No warning that `search_web(ingest=true)` only stores raw pages, unsearchable until the memory pipeline runs, while `ingest_url` makes a page searchable in one run.
  - This is the most expensive kind of drift for a facade: per-tool docs can't reveal it, because it lives *between* tools.

**Rule we're keeping:** before editing a facade skill, read the tool implementations, not just their docstrings. The chains are only as true as the code behind them.

---

## The checklist, condensed

| Step | Question to ask of an MCP facade skill |
|---|---|
| Trigger | Does the agent get the chain rules when the user doesn't type the slash command — and is that OK? |
| Structure | Does each mode/intent branch load only its own chains? Does every step end on a checkable condition? |
| Steering | Is there one leading word for the cross-cutting rule (here, *provenance*)? Are the rules positive targets? |
| Pruning | Is any line a copy of a tool description? Does each test pin a regression or just pin text? |


## Resources

More in: https://www.youtube.com/watch?v=UNzCG3lw6O0
