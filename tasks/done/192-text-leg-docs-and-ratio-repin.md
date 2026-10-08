---
id: 192-text-leg-docs-and-ratio-repin
status: done
feature: atlas-search-text-leg
---

# ADR-015's Status-line notes on ADR-008 / 012 / 013, the skill / README / runbook / docstring texts that still describe `$text` or `min_text_score`, and the live e2e that re-pins `query.text_min_match_ratio` (lowest 0.1 step separating four off-topic from six on-topic queries) with the evidence in `default.yaml`

Tags: `docs`, `adr`, `evals`, `config`, `e2e`
Depends on: 191
Blocks: —
Implements: ADR-015 §8 (the e2e re-pin) and its "Amendments to apply"

## Problem

After 191 the code has the ratio but the prose does not: ADR-008 §3, ADR-012 §1–§2 and ADR-013 §7 still
describe `$text`, `text_index` and `min_text_score` without a Status-line pointer to ADR-015; the
`tree-memory` skill's read chain 3 says "the text search hits ≥ `query.min_text_score`"; the runbook does
not say that a deploy must be followed by an indexing run before the text leg comes back. And `0.5` is a
guess — ADR-015 §8 pins it by the same on-topic vs off-topic protocol task 183 used, now with a rule that
CAN separate them.

## Scope

**Docs (no code besides the pinned value):**
1. **ADR Status-line notes** — exactly ADR-015's "Amendments to apply" list (ADR-008, ADR-012, ADR-013);
   body text untouched; no other ADR edited (ADR-006 line 183 and ADR-008 line 118 are diagram body
   text). **DONE by the PA (ADRs are PA-owned) — see the Log entry of 2026-10-08 23:50; the SWE skips
   this item.** The PA also appended one note to ADR-015's OWN Status line recording 191's K formula
   (`min(M, max(1, ceil(round(ratio × M, 9))))`) and the 64-term cap (`_MAX_QUERY_TERMS`), so the diff
   is four Status lines, not three.
2. `.agents/skills/tree-memory/SKILL.md` read chain 3 (line 54): "the text search hits ≥
   `query.min_text_score`" → "the text search keeps only rows matching at least
   `ceil(query.text_min_match_ratio × M)` of the question's M content words (`0.5` → half of them)"; the
   rest of the sentence (never threshold the returned `score`) stays.
3. `apps/memory/README.md`: the `search_memory` row (line 464) and the `query` key line (76, if 191 left
   anything stale) describe the ratio; the Embedding-reset paragraph (292, "search runs on the text leg")
   still holds — leave it. `docs/notes/deployment-runbook.md` step 3's indexing paragraph (lines ~70–74)
   gains one sentence: "Re-run it after ANY deploy that changes a search-index definition (ADR-015 added
   `text_search_index`): until the run, `search_memory` answers `search_mode: vector_only`." The same
   runbook's step 1 bullet (line ~25) says "Atlas Search indexes (`text_index`, `vector_index` on
   `memory`)" — `text_index` was never a mongot index and is gone since 191: rewrite as
   "(`vector_index`, `text_search_index` on `memory`)". This is a stale reference, NOT history — it is
   not on Scope 4's residual list.
4. Docstrings / comments that still describe the retired leg as CURRENT (`$text`, `textScore`,
   `text_index`, `min_text_score`) in `apps/memory/src` (`mcp/tools.py` if any; 191 already cleared
   `rag/types.py` and `rag/search.py` — zero hits, do not hunt there). Two-tier check, `docs/adrs` and
   `tasks/done` excluded. Both greps run with `-I` (skip binaries — otherwise `__pycache__/*.pyc` prints
   "Binary file … matches") and the pattern in SINGLE quotes with `[$]text`: double-quoted `\$text`
   reaches grep as `$text`, which ugrep (this machine's `grep`) and any ERE grep read as an end-of-line
   anchor, so the `$text` alternative silently matches nothing and the check passes vacuously.
   - (a) `grep -rnI 'min_text_score' apps/memory/src apps/memory/README.md .agents docs/notes tutorials
     docs/glossary.md` is EMPTY — no exceptions (191's AC already holds this for `apps/`, `.agents` and
     the glossary; this extends it to the README, notes and tutorials).
   - (b) `grep -rnI 'textScore\|text_index\b\|[$]text' apps/memory/src apps/memory/README.md .agents
     docs/notes tutorials docs/glossary.md | grep -v -e rag/indexing.py -e entities/memory.py -e
     docs/glossary.md -e .agents/skills/mongodb-` is EMPTY. The excluded paths are the residual set —
     mentions that name the retired index as HISTORY, or are not about Tree's leg at all, and must stay:
     - `apps/memory/src/tree/memory/rag/indexing.py`: the legacy-drop code — `_LEGACY_TEXT_INDEX_NAME`,
       `_drop_legacy_text_index`, and the module / `ensure_indexes` docstring sentences describing that
       drop. It must name `text_index` and `$text` to drop them.
     - `apps/memory/src/tree/entities/memory.py`: the `memory_indexes` docstring sentence saying
       `ensure_indexes` replaced the classic `$text` index and drops a leftover `text_index`.
     - `docs/glossary.md`: rows **`memory` collection** ("no classic `$text` index since ADR-015"),
       **Minimum match ratio** (the `textScore` bad example), **Retrieval outcome** (task 183's
       `textScore` eval) and **Text search index** ("Replaces the classic `$text` `text_index`").
       PA-owned; do not edit.
     - `.agents/skills/mongodb-*/SKILL.md`: vendored third-party MongoDB skills
       (`mongodb-search-and-ai` line 116, `mongodb-natural-language-querying` line 97) giving generic
       advice about the `$text` OPERATOR — not a description of Tree's text leg. Vendored; never edit.
     Every surviving hit in the Tree-owned residual files must read as history — "legacy", "classic",
     "retired", "replaced", past tense — never as the current leg. A hit that describes `$text` /
     `textScore` as what the text leg does TODAY is a defect even inside the residual set.
5. The PR description carries the runbook step verbatim ("run `make memory-run-indexing-pipeline`
   against prod right after merging; the text leg is `vector_only` until then").

**E2E and re-pin (run-pipelines-e2e skill, LOCAL env, the real local corpus; every number in the Log):**
6. `make env-status` → local; `make memory-serve-workflows &` from this checkout; `make
   memory-run-indexing-pipeline`; verify with `mongosh`: `$listSearchIndexes` = `vector_index` +
   `text_search_index`, `getIndexes()` without `text_index`.
7. N depends only on K, and K takes at most M distinct values per query — so do NOT run 10 ratios × 10
   queries (≈ 100 Voyage embeds under `voyage_rpm: 3`). Per query: compute M and the K each 0.1 step
   yields, run ONE `make memory-search QUERY="…"` per DISTINCT K (`TREE_QUERY__TEXT_MIN_MATCH_RATIO=r`
   with any r producing that K; one-word queries = one run; the 8-term query = eight), and read the
   `text leg: N candidate(s), M query term(s), min_should_match=K` line (INFO logs on) — or produce the
   N-per-K column with the exact `$search` pipeline in `mongosh` (no embedding) and confirm only the
   chosen step with `make memory-search`. Queries — off-topic ×4: "dark photon vector-like fermions"
   (task 183 measured a local text-leg top of 1.472 for it, so that paper IS most likely in the local
   corpus — expect to substitute another known-absent topic and name it), "recipe for sourdough bread",
   "zxqv plorb wumbus", "MongoDB Atlas Vector Search scalar binary quantization int8"; on-topic ×6:
   "context engineering", "structured outputs pydantic", "ReAct agent tool calling", "pydantic", "ReAct",
   "Gemini". Record a table `query → M, (K → N) per 0.1 step` (≈ 30 runs). One-word queries are K = 1 at
   every r — expected; they verify the leg still finds single terms.
8. Pick the LOWEST r at which every off-topic N == 0 and every on-topic N ≥ 1. Set it in `default.yaml`,
   `frozen_config.yaml` and `QueryConfig`'s default; rewrite the `default.yaml` comment in the
   `min_vector_score` evidence style (date, corpus size, the table's decisive rows, the chosen step, the
   `TREE_QUERY__…` override, "re-pin with the same protocol"). Show the fused top-5 for the quantization
   query before / after the pin: the physics paper must be absent, or the Log says why it legitimately
   stays (e.g. the vector leg keeps it). If NO step separates them (an off-topic query shares ≥ K terms
   at every r while an on-topic one loses its only hit), ship `0.5` unchanged, write the table and the
   conflict in the Log and the comment, and report it — do not force a value.
9. MCP smoke per the skill: serve `make memory-serve-mcp TRANSPORT=streamable-http`, call
   `search_memory("context engineering")` through the header-carrying `fastmcp.Client` snippet →
   `search_mode: hybrid`; then `drop_search_index("text_search_index")` by hand → the same call answers
   `vector_only` with the server WARN; re-run the indexing pipeline → `hybrid` again (this is the prod
   rollout window, rehearsed locally).

## Acceptance criteria

- [x] ADR-008, ADR-012 and ADR-013 Status lines carry ADR-015's notes verbatim; `git diff docs/adrs`
      touches only those three Status lines (plus ADR-015's own Status line, PA-applied, for 191's K
      formula and 64-term cap). *Evidence: PA log entry 2026-10-08 23:50 — `grep -F` of each note
      against its ADR → OK ×3; `git diff --numstat docs/adrs` → `1 1` on 008 / 012 / 013 / 015, every
      hunk `@@ -3 +3 @@`.*
- [x] Scope 4's grep (a) is empty; grep (b) is empty after the `grep -v` of the residual set (three
      Tree-owned files + the vendored `.agents/skills/mongodb-*` skills), and every hit inside the three
      Tree-owned files reads as history (legacy / classic / replaced), none as the current leg; the
      skill's read chain 3, the runbook step-3 sentence and the runbook step-1 bullet (`vector_index`,
      `text_search_index`) read as specified. *Evidence: PA log entry 2026-10-09 — amended (a) and (b)
      run on both `grep` (ugrep 7.8.4) and `/usr/bin/grep` (BSD 2.6.0) → 0 lines each; residual hits
      enumerated there with line numbers; skill line 54, runbook lines 25 and 75–76 quoted.*
- [x] The Log holds the `query × r → N` table for all 10 queries, M and K per query, the chosen step (or
      the "no step separates" finding), and the quantization top-5 before / after.
- [x] `default.yaml`, `frozen_config.yaml` and `QueryConfig` agree on the pinned value;
      `test_app_config.py`'s default assertion matches it; the `default.yaml` comment carries the
      evidence in the `min_vector_score` style.
- [x] The MCP smoke shows `hybrid` → `vector_only` (index dropped by hand, WARN naming it) → `hybrid`
      (after re-indexing), recorded in the Log.
- [x] The PR description names the post-merge prod step; `make memory-format-check`, `make
      memory-lint-check`, `make pre-commit`, `make memory-tests` green (env-status local).
- [ ] [HUMAN] Post-merge: run `make memory-run-indexing-pipeline` against prod (`make env-prod`), confirm
      in Atlas → Search & Vector Search that exactly `vector_index` and `text_search_index` exist (2 of
      the M0's 3), and that a `tree-memory` `search_memory` call from Claude Code answers
      `search_mode: hybrid`; evidence in the Log.

## User Stories

### Story: The model reads the skill and does not threshold the score
1. The model reads read chain 3: relevance is gated server-side — the vector bar and the K-of-M word rule
   — so it keeps `top_k=5` and never filters on the returned `score`.

### Story: An operator deploys and the text leg is briefly missing
1. Following the runbook, the operator merges, sees `search_mode: vector_only` in the first Horizon
   answer, runs `make memory-run-indexing-pipeline` against prod as the runbook sentence says, and the
   next answer is `hybrid`.

### Story: A reviewer traces the text leg's design
1. From ADR-008 §3's Status note they land on ADR-015 §2–§4; from ADR-012's note they learn
   `text_index` left the Beanie set and `ensure_indexes` owns both mongot indexes; from ADR-013 §7's
   note that `min_text_score` became `text_min_match_ratio`.

### Story: The evals chapter re-pins the ratio next quarter
1. They open `default.yaml`, read the comment's table excerpt and the protocol, re-run the 10 queries
   with `TREE_QUERY__TEXT_MIN_MATCH_RATIO=r make memory-search …`, and move the value with the same
   evidence shape.

### Story: The Tester reproduces the e2e
1. Following the Log's commands verbatim on the local corpus, the Tester gets the same N for the
   decisive rows (within corpus drift they note), and the same `hybrid → vector_only → hybrid` sequence
   on the MCP smoke.

---

Blocked by: 191

## Log

### [PA] 2026-10-08 19:30 — Grooming

**Summary**
Make every operator- and model-facing text describe the K-of-M text leg, apply ADR-015's Status-line
notes, and pin `query.text_min_match_ratio` with the live 10-query protocol — or report that no step
separates the sets.

**Key decisions**
- The re-pin is this task, not 191's: the value is evidence-owned (ADR-008 §4's rule for `min_vector_score`),
  and 191 must stay shippable with the provisional 0.5 even if the eval is inconclusive.
- "No separating step" is a valid outcome that ships 0.5 and reports — the human re-decides, the task
  does not force a number.
- The post-merge prod indexing run is `[HUMAN]` by necessity (Horizon deploys from `main`); the local MCP
  smoke rehearses the exact window.

**Dependencies**
- 191 — the ratio, the log line, the index drop and the `vector_only` behaviour this task documents and
  measures.

**User stories**
- 5 stories: the skill reader, the deploying operator, the reviewer's trail, the evals re-pin, the
  Tester's reproduction.

Ready for implementation.

### [PA] 2026-10-08 23:30 — Grooming fix (Scope 3, Scope 4 and the grep AC)

Scope 4's grep was unsatisfiable as written: `text_index` / `$text` MUST appear in `rag/indexing.py`'s
legacy-drop code (`_LEGACY_TEXT_INDEX_NAME`, `_drop_legacy_text_index`) and in the `memory_indexes` docstring
that explains the drop, and the glossary rows **Minimum match ratio** / **Retrieval outcome** / **Text search
index** legitimately keep `textScore` (bad example, task 183's eval) and "replaces the classic `$text`
`text_index`" as ADR-015 history. Rewritten as a two-tier check: (a) `min_text_score` → empty, no
exceptions; (b) `textScore\|text_index\b\|$text` → empty after `grep -v` of exactly those three files, with
the rule that every surviving hit reads as history, never as the current leg. The one hit that is NOT
history — `docs/notes/deployment-runbook.md` step 1 calling `text_index` an Atlas Search index — is now an
explicit Scope 3 item (→ `vector_index`, `text_search_index`). Also noted that 191 already cleared
`rag/types.py` / `rag/search.py` so the SWE does not hunt ghosts. AC bullet 2 updated to match.

### [PA] 2026-10-08 23:50 — Scope 1 applied (ADR Status-line notes)

ADRs are PA-owned, so the PA applied Scope 1; the SWE skips it. The three notes under ADR-015's
"Amendments to apply" were appended verbatim to the Status lines of ADR-008, ADR-012 and ADR-013 (joined
with " — ", the style of the earlier task-driven notes on those lines); body text untouched, no other ADR
edited. Verification: `grep -F` of each note (outer `append "…"` wrapper stripped) against its ADR → OK
×3; `git diff --numstat docs/adrs` → `1 1` on 008 / 012 / 013 / 015, every hunk `@@ -3 +3 @@`.

ADR-015 itself: judged the four facts that changed during 191 against the Decision text. (a) the 64-term
cap (`_MAX_QUERY_TERMS`, mongot's 1024 `maxClauseCount` ÷ 4 text paths) and (b) the K formula
`min(M, max(1, ceil(round(ratio × M, 9))))` contradict §2 / §3 as written → recorded as one note on
ADR-015's own Status line. (c) `IndexNotFound` (code 27) on the legacy drop = already gone and (d) an
absent `mappings.dynamic` = `false` are §1's "idempotent retirement" and §5's "server-echoed defaults are
not drift" applied — not recorded. Scope 1 and AC bullet 1 updated to name the ADR-015 line so the
Tester's `git diff docs/adrs` check matches the actual diff. Not committed — ships with 192's commit.

### [SWE] 2026-10-09 00:20 — Implementation

**Files modified**
- `apps/memory/src/tree/config/default.yaml` — `text_min_match_ratio: 0.5` kept (it IS the lowest separating step); comment rewritten in the `min_vector_score` evidence style (date, corpus size, decisive rows, window, override, re-pin protocol).
- `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — one-line "pinned live, 2026-10-09 — tasks/192 Log" note, value 0.5.
- `apps/memory/src/tree/config/app_config.py` — `QueryConfig` docstring: provisional → pinned live with the decisive row; default `Field(0.5, …)` unchanged.
- `apps/memory/README.md` — `query` key line (76): "re-pinned in tasks/192" → "pinned live in tasks/192's Log as the lowest 0.1 step separating 4 off-topic from 6 on-topic queries". Row 464 (`search_memory`) already described the ratio after 191 — verified, untouched.
- `apps/memory/src/tree/memory/rag/load.py` — module docstring: "tokenised by the `$text` index" → "by the **Text search index** (`name` is one of its text paths)".
- `apps/memory/src/tree/entities/clusters.py` — module docstring: "`$vectorSearch` / `$text` results" → "`$vectorSearch` / `$search` results".
- `docs/notes/deployment-runbook.md` — step 1 bullet → "(`vector_index`, `text_search_index` on `memory`)"; step 3 gains the Scope 3 sentence verbatim.
- `.agents/skills/tree-memory/SKILL.md` — NOT modified: 191 already wrote read chain 3 with Scope 2's wording verbatim (verified line 54).
- ADRs (Scope 1) — PA-applied, not touched by the SWE.
- No test changes: the pinned value did not move, so `test_app_config.py`'s `== 0.5` assertions (lines 714–718, 748) already match.

**Tests**
- Unit: 5283 passed, 0 failing — `make memory-tests` (env-status local).
- Integration: N/A — the project has none (AGENTS.md); e2e below.

**Acceptance criteria**
- [x] ADR Status lines — PA-applied (see 23:50 entry).
- [ ] Scope 4 greps — (a) EMPTY. (b) has exactly TWO hits left after the `grep -v`, both in vendored third-party MongoDB skills that give generic advice about the `$text` OPERATOR and do not describe Tree's leg: `.agents/skills/mongodb-search-and-ai/SKILL.md:116` ("NEVER recommend `$regex` or `$text` for search use cases") and `.agents/skills/mongodb-natural-language-querying/SKILL.md:97` ("Do not use `$text` without a text index"). Not edited (out of scope, vendored). AC needs a PA amendment: add `-e .agents/skills/mongodb-` to the `grep -v`. Every residual-set hit reads as history (indexing.py: "classic", "legacy", "Retire", "retired", "replaced"; entities/memory.py: "replaced the classic `$text` index … drops a leftover `text_index`"; glossary 35/39/50/57: "no classic `$text` index since ADR-015", "retired `textScore` bar", "task 183's `textScore` eval", "Replaces the classic `$text` `text_index`"). Skill chain 3 + runbook step 1/3 read as specified. Note: `grep -rn` also prints `__pycache__/*.pyc` "Binary file … matches" lines — use `grep -rnI`.
- [x] Table, M/K per query, chosen step, quantization top-5 before/after — below.
- [x] `default.yaml` / `frozen_config.yaml` / `QueryConfig` all 0.5; `test_app_config.py` asserts 0.5; evidence comment in `default.yaml`.
- [x] MCP smoke `hybrid` → `vector_only` (WARN) → `hybrid` — below.
- [x] PR-description text below ("PR description"); format/lint/pre-commit/tests green.
- [ ] [HUMAN] Post-merge prod indexing run — stays open.

**E2E — Scope 6 (indexes)**
Docker `tree-prefect-worker` stopped; a stale `make memory-serve-workflows` from 2026-10-08 23:50 (same worktree, earlier session) was killed per the skill ("kill it first and re-serve"); served from this worktree with `PREFECT_LOGGING_ROOT_LEVEL=INFO PREFECT_LOGGING_EXTRA_LOGGERS=tree make memory-serve-workflows`.
```
$ make memory-run-indexing-pipeline
Submitted flow run eb24ccb5-592c-4a1a-ab47-1fa19fa2ba71 … Done. Flow completed successfully.
(serve) Embedded 0 nodes in memory
(serve) Vector search index 'vector_index' already up-to-date (dimensions=1024, filters=['kind', 'merged_into', 'subtype', 'type', 'user_id'])
(serve) Search index 'text_search_index' already up-to-date (analyzer=lucene.english, text paths=['name', 'aliases', 'properties.content', 'properties.aliases'], filter paths={'user_id': 'objectId', 'kind': 'token', 'type': 'token', 'subtype': 'token'})
$ mongosh … --eval 'db.getSiblingDB("tree").memory.aggregate([{$listSearchIndexes:{}},{$project:{_id:0,name:1,type:1}}])' ; getIndexes()
[{"name":"vector_index","type":"vectorSearch"},{"name":"text_search_index","type":"search"}]
["_id_","user_kind_type_subtype","user_type_name","active_user","user_kind_source_node","user_kind_target_node"]   # no text_index
```

**E2E — Scope 7 (the N-per-K table)**
Corpus: local `tree` DB, user `paul.iusztin@example.com` (`6ac0f976f5896c3a79957359`), 14 documents / 27 parent / 372 child chunks — 10 decodingai.com articles (context engineering, workflows vs agents, structured outputs, 5 patterns, agent planning, tool calling, ReAct agents, agent memory, documents-to-text, agents roadmap), 2 Claude Code sessions, 2 example.com pages. No physics paper: "dark photon vector-like fermions" is genuinely off-topic here, so it was KEPT, not substituted (task 183's 1.472 was a different corpus).

Method: N per DISTINCT K via the REAL `tree.memory.rag.search._text_search` (exact `$search` pipeline: `user_id` + `kind: node` + `type: chunk` + `subtype: child` filter, parent `mustNot`), no embedding, `limit=100000` so N is uncapped; the ratio set on `app_config.query.text_min_match_ratio` per step. Script (run with `cd apps/memory && uv run --env-file ../../.env python ratio_table.py "<off|…>" "<on|…>"`):
```python
import asyncio, os, sys
from beanie import PydanticObjectId
from pymongo import AsyncMongoClient
from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.entities.memory import MEMORY_COLLECTION
from tree.memory.rag import search as s
RATIOS = [round(i / 10, 1) for i in range(11)]
async def main() -> None:
    client = AsyncMongoClient(settings.mongo.mongo_uri.get_secret_value(), tz_aware=True)
    db = client[settings.mongo.mongo_initdb_database]
    uid = PydanticObjectId((await db["users"].find_one({"identifier": os.environ["TREE_USER_IDENTIFIER"]}))["_id"])
    for q in sys.argv[1].split("|") + sys.argv[2].split("|"):
        m = len(s.query_terms(q)); n_by_k = {}
        for r in RATIOS:
            k = s.min_should_match(m, r)
            if k not in n_by_k:
                app_config.query.text_min_match_ratio = r
                rows = await s._text_search(db[MEMORY_COLLECTION], q, user_id=uid, limit=100000, node_filter={"type": "chunk", "subtype": "child"})
                n_by_k[k] = len(rows or [])
        print(q, s.query_terms(q), m, {r: (s.min_should_match(m, r), n_by_k[s.min_should_match(m, r)]) for r in RATIOS})
    await client.close()
asyncio.run(main())
```

Table — `query → M; per r: K→N` (N = text candidates, uncapped):

| query | terms (M) | 0.0 | 0.1 | 0.2 | 0.3 | 0.4 | **0.5** | 0.6 | 0.7 | 0.8 | 0.9 | 1.0 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| OFF "dark photon vector-like fermions" | dark, photon, vector, like, fermions (5) | K1→93 | K1→93 | K1→93 | K2→6 | K2→6 | **K3→0** | K3→0 | K4→0 | K4→0 | K5→0 | K5→0 |
| OFF "recipe for sourdough bread" | recipe, sourdough, bread (3) | K1→1 | K1→1 | K1→1 | K1→1 | K2→0 | **K2→0** | K2→0 | K3→0 | K3→0 | K3→0 | K3→0 |
| OFF "zxqv plorb wumbus" | zxqv, plorb, wumbus (3) | K1→0 | K1→0 | K1→0 | K1→0 | K2→0 | **K2→0** | K2→0 | K3→0 | K3→0 | K3→0 | K3→0 |
| OFF "MongoDB Atlas Vector Search scalar binary quantization int8" | mongodb, atlas, vector, search, scalar, binary, quantization, int8 (8) | K1→79 | K1→79 | K2→5 | K3→1 | K4→0 | **K4→0** | K5→0 | K6→0 | K7→0 | K8→0 | K8→0 |
| ON "context engineering" | context, engineering (2) | K1→145 | K1→145 | K1→145 | K1→145 | K1→145 | **K1→145** | K2→50 | K2→50 | K2→50 | K2→50 | K2→50 |
| ON "structured outputs pydantic" | structured, outputs, pydantic (3) | K1→140 | K1→140 | K1→140 | K1→140 | K2→58 | **K2→58** | K2→58 | K3→13 | K3→13 | K3→13 | K3→13 |
| ON "ReAct agent tool calling" | react, agent, tool, calling (4) | K1→304 | K1→304 | K1→304 | K2→183 | K2→183 | **K2→183** | K3→83 | K3→83 | K4→34 | K4→34 | K4→34 |
| ON "pydantic" | pydantic (1) | K1→19 | … | … | … | … | **K1→19** | … | … | … | … | K1→19 |
| ON "ReAct" | react (1) | K1→55 | … | … | … | … | **K1→55** | … | … | … | … | K1→55 |
| ON "Gemini" | gemini (1) | K1→37 | … | … | … | … | **K1→37** | … | … | … | … | K1→37 |

Near-miss rows, which terms matched: dark photon @0.4 → all 6 rows match only `vector` + `like` (`like` is not a Lucene stop word); sourdough @0.3 → 1 row on `recipe`; quantization @0.3 → 1 row on `vector` + `search` + `binary` (doc "Stop Converting Documents to Text").

**Chosen step: 0.5** — the LOWEST 0.1 step with every off-topic N == 0 and every on-topic N ≥ 1. Separating window: 0.5–1.0 (on-topic never reaches 0 at any step; fewest at 0.5 = "pydantic" 19, at 1.0 = "structured outputs pydantic" 13). 0.4 fails only on "dark photon vector-like fermions" (6). The provisional value stands, now with evidence.

**E2E — confirmation through `make memory-search`** (one Voyage embed each, serialised 21 s apart; INFO lines from the CLI; `TOP_K=100` → leg limit 400 so N is uncapped, except the two quantization runs at `TOP_K=5`):
```
$ TREE_QUERY__TEXT_MIN_MATCH_RATIO=0.5 make memory-search QUERY="dark photon vector-like fermions" TOP_K=100
vector leg: 372 candidate(s), 0 kept at min_vector_score=0.70 (top=0.587)
text leg: 0 candidate(s), 5 query term(s), min_should_match=3
No results.                                                        (outcome=nothing_found, search_mode=hybrid)
$ TREE_QUERY__TEXT_MIN_MATCH_RATIO=0.4 … "dark photon vector-like fermions"  → text leg: 6 candidate(s), 5 query term(s), min_should_match=2 → 3 parents
$ … 0.5 "recipe for sourdough bread"   → vector 0 kept (top=0.631); text leg: 0 candidate(s), 3 query term(s), min_should_match=2 → No results.
$ … 0.3 "recipe for sourdough bread"   → text leg: 1 candidate(s), 3 query term(s), min_should_match=1 → 1 parent
$ … 0.5 "zxqv plorb wumbus"            → vector 0 kept (top=0.600); text leg: 0 candidate(s), 3 query term(s), min_should_match=2 → No results.
$ … 0.5 "context engineering"          → vector 25 kept (top=0.773); text leg: 145 candidate(s), 2 query term(s), min_should_match=1 → 22 parents
$ … 0.5 "structured outputs pydantic"  → vector 31 kept (top=0.852); text leg: 58 candidate(s), 3 query term(s), min_should_match=2 → 18 parents
$ … 0.5 "ReAct agent tool calling"     → vector 129 kept (top=0.805); text leg: 183 candidate(s), 4 query term(s), min_should_match=2 → 24 parents
$ … 0.5 "pydantic"                     → vector 16 kept (top=0.821); text leg: 19 candidate(s), 1 query term(s), min_should_match=1 → 7 parents
$ … 0.5 "ReAct"                        → vector 5 kept (top=0.719); text leg: 55 candidate(s), 1 query term(s), min_should_match=1 → 18 parents
$ … 0.5 "Gemini"                       → vector 0 kept (top=0.697); text leg: 37 candidate(s), 1 query term(s), min_should_match=1 → 16 parents
```
Every N matches the table. Note "Gemini": the vector leg keeps nothing (top 0.697 < 0.70) — the text leg alone answers it, which a higher K could never break (M = 1).

**Quantization query, fused top-5 before / after** (`TOP_K=5`; "before" = r 0.3, the last step with a text candidate, since the value itself did not move):
```
$ TREE_QUERY__TEXT_MIN_MATCH_RATIO=0.3 make memory-search QUERY="MongoDB Atlas Vector Search scalar binary quantization int8" TOP_K=5
vector leg: 20 candidate(s), 0 kept at min_vector_score=0.70 (top=0.657)
text leg: 1 candidate(s), 8 query term(s), min_should_match=3
[0.016] Stop Converting Documents to Text. You're Doing It Wrong. — Comparing multiple images
$ TREE_QUERY__TEXT_MIN_MATCH_RATIO=0.5 make memory-search QUERY="…same…" TOP_K=5
vector leg: 20 candidate(s), 0 kept at min_vector_score=0.70 (top=0.657)
text leg: 0 candidate(s), 8 query term(s), min_should_match=4
No results.                                                        (outcome=nothing_found, search_mode=hybrid)
```
The physics paper is absent both times — it is not in this corpus. At the pin the query answers `nothing_found`; the vector leg contributes nothing (top 0.657).

**E2E — Scope 9 (MCP smoke)** — `FASTMCP_PORT=8765 make memory-serve-mcp TRANSPORT=streamable-http` (port 8000 is held by Docker Desktop locally), the skill's header-carrying `fastmcp.Client`, `search_memory("context engineering", top_k=5)`:
```
1) 8 tools | search_mode: hybrid | outcome: found | parents: 5
   server: text leg: 20 candidate(s), 2 query term(s), min_should_match=1
$ mongosh … --eval 'db.getSiblingDB("tree").memory.dropSearchIndex("text_search_index")'
   → $listSearchIndexes: [{"name":"vector_index","type":"vectorSearch"}]
2) search_mode: vector_only | outcome: found | parents: 4
   server: text leg: 0 candidate(s), 2 query term(s), min_should_match=1
   server WARNING: Search leg unavailable: search index 'text_search_index' absent; the query runs vector_only
$ make memory-run-indexing-pipeline   → (serve) Creating search index 'text_search_index' (type=search)... treating it as ready (local mongot)
3) search_mode: hybrid | outcome: found | parents: 5
   server: text leg: 20 candidate(s), 2 query term(s), min_should_match=1
```
Cleanup: MCP server and serve process killed, `docker start tree-prefect-worker`, `$listSearchIndexes` = `vector_index` + `text_search_index`.

**QA**
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
341 files already formatted / All checks passed!
$ make pre-commit   → prettier, ruff check, ruff format, biome check: Passed
$ make memory-tests → 5283 passed in 57.23s
```

**PR description** (for `/squid-review` to paste into the feature PR)
> **Post-merge step (required):** run `make memory-run-indexing-pipeline` against prod right after merging; the text leg is `vector_only` until then. (`make env-prod` first.) Horizon boots with `MCP_SKIP_INDEX_BOOTSTRAP=true`, so only that run creates `text_search_index` and drops the classic `text_index`; until it does, every `search_memory` answer carries `search_mode: vector_only`. Then confirm in Atlas → Search & Vector Search that exactly `vector_index` and `text_search_index` exist (2 of the M0's 3), and that a `search_memory` call answers `search_mode: hybrid`. Switch back with `make env-local`.
>
> `query.text_min_match_ratio` is pinned at `0.5` by the 10-query on-topic vs off-topic eval in `tasks/192`'s Log (lowest 0.1 step separating them on the local corpus).

**Notes**
- Glossary rows **Minimum match ratio** ("PROVISIONAL — re-pinned by the 10-query protocol in `tasks/192`'s Log") and **Retrieval outcome** still read as future tense; they are PA-owned — the PA may now cite the result (0.5, window 0.5–1.0). Same for ADR-015 §8 if it wants the outcome recorded.
- The corpus is small (372 children) and the margin is one step: at 0.4 one off-topic query keeps 6 rows on two generic words. A larger corpus can only raise off-topic N at a fixed K, so the re-pin protocol should be re-run as it grows (said in the `default.yaml` comment).
- Local mongot reports neither `status` nor `queryable`, so `ensure_indexes` treats a fresh index as ready immediately; the third MCP call ran ~25 s after the indexing run and was already `hybrid`.

### [PA] 2026-10-09 09:40 — Grooming fix (Scope 4 grep (b), AC 2 ticked, glossary rows cited the pin)

**Scope 4 amended.** (b) as written could not be emptied: two hits are vendored third-party MongoDB
skills giving generic advice about the `$text` OPERATOR, not Tree's leg (`.agents/skills/mongodb-search-and-ai/SKILL.md:116`,
`.agents/skills/mongodb-natural-language-querying/SKILL.md:97`) — added `-e .agents/skills/mongodb-` to
the `grep -v` and the skills to the residual list (never edit, vendored). Both greps now run `-rnI`
(`__pycache__/*.pyc` otherwise prints "Binary file … matches"). Also found while re-running: this
machine's `grep` is ugrep 7.8.4 (shell function), and the double-quoted `"…\|\$text"` reaches it as
`$text` = end-of-line anchor, so the `$text` alternative matched NOTHING there and (b) passed vacuously
(memory.py:345 surfaced only via `text_index\b`; the two skill hits did not surface at all). Pattern now
single-quoted with `[$]text`, literal on ugrep, BSD and GNU grep alike — verified to hit the two skill
lines on both `grep` and `/usr/bin/grep` before the `grep -v`. `docs/glossary.md:35` (**`memory`
collection**, "no classic `$text` index since ADR-015") added to the glossary residual rows — history.

**AC 2 evidence** — amended (a) and (b), run on `grep` (ugrep 7.8.4) AND `/usr/bin/grep` (BSD 2.6.0):
```
$ grep -rnI 'min_text_score' apps/memory/src apps/memory/README.md .agents docs/notes tutorials docs/glossary.md
(0 lines, both greps)
$ grep -rnI 'textScore\|text_index\b\|[$]text' apps/memory/src apps/memory/README.md .agents docs/notes tutorials docs/glossary.md \
    | grep -v -e rag/indexing.py -e entities/memory.py -e docs/glossary.md -e .agents/skills/mongodb-
(0 lines, both greps)
```
Residual hits, all read as history / not Tree's leg: `rag/indexing.py` 16, 60, 63, 366, 368, 417, 418,
421, 422, 427, 432, 441, 446, 578 ("classic", "legacy", "Retire", "retired", "replaced", the
`_LEGACY_TEXT_INDEX_NAME` / `_drop_legacy_text_index` identifiers); `entities/memory.py` 345 ("replaced
the classic `$text` index … drops a leftover `text_index`"); `docs/glossary.md` 35, 39, 50, 57 ("no
classic `$text` index since ADR-015", "retired `textScore` bar", "task 183's `textScore` eval",
"Replaces the classic `$text` `text_index`"); `mongodb-search-and-ai` 116 / `mongodb-natural-language-querying`
97 (generic operator advice). Non-grep half: `tree-memory/SKILL.md:54` carries Scope 2's sentence
verbatim ("keeps only rows matching at least `ceil(query.text_min_match_ratio × M)` … never threshold the
returned `score`"); runbook line 25 = "(`vector_index`, `text_search_index` on `memory`)"; runbook lines
75–76 = the Scope 3 sentence verbatim. AC 2 ticked.

**Glossary (PA-owned) updated** — rows **Minimum match ratio** and **Retrieval outcome** no longer say
PROVISIONAL / future tense: both now state the ratio was pinned live at `0.5` on 2026-10-09 (`tasks/192`'s
Log; local corpus 14 documents / 372 child chunks; lowest 0.1 step with all four off-topic N = 0 and all
six on-topic N ≥ 1; window 0.5–1.0; decisive row "dark photon vector-like fermions" 6 at 0.4 on `vector`
+ `like`, 0 at 0.5) and is re-pinned by the same protocol as the corpus grows. Good / Bad examples and the
`textScore` history sentences untouched; the `0.70` PROVISIONAL clause on **Retrieval outcome** is
`min_vector_score` — out of scope, untouched. ADR-015 §8 not edited (Accepted ADRs are not re-opened for
an outcome; the Status line already carries 191's note). Not committed — ships with 192's commit.

### [Tester] 2026-10-09 01:00 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green; env-status local)
- Unit tests: 5283 passed / 0 failed (`make memory-tests`, 57.24 s)
- Integration tests: N/A (the project has none, AGENTS.md)
- Warnings: 0 reported by pytest

**E2E adversarial pass**
- Happy path (table): re-ran the SWE's inline `ratio_table.py` against the local corpus (14 docs / 372 children) → all 10 rows reproduce exactly (dark photon K1→93, K2→6, K3→0; sourdough K1→1, K2→0; quantization K3→1, K4→0; context engineering K1→145, K2→50; structured outputs K2→58, K3→13; ReAct agent tool calling K2→183; pydantic 19, ReAct 55, Gemini 37). PASS.
- Independent cross-check (hand-written `$search` pipeline in mongosh, not the code under test): dark photon K=1/2/3 → 93/6/0; sourdough K=1/2 → 1/0. PASS (matches).
- `make memory-search` end-to-end (TOP_K=100): dark photon @0.4 → `text leg: 6 candidate(s), 5 query term(s), min_should_match=2` → 3 parents; @0.5 → `0 candidate(s) … min_should_match=3` → `nothing_found`; "context engineering" @0.5 → 145 candidates, K=1, 22 parents; "ReAct agent tool calling" @0.5 → 183, K=2, 24 parents. PASS.
- MCP smoke (FASTMCP_PORT=8765, header-carrying fastmcp.Client): `search_memory("context engineering")` → `hybrid found 5`; `dropSearchIndex("text_search_index")` → `$listSearchIndexes` = `vector_index`; same call → `vector_only found 4`, server logged at WARNING `Search leg unavailable: search index 'text_search_index' absent; the query runs vector_only`; `make memory-run-indexing-pipeline` → `Creating search index 'text_search_index'`; `$listSearchIndexes` = `vector_index,text_search_index`; same call → `hybrid found 5`. PASS.
- Break path 1 (blank input): `search_memory("")` → `error_type: invalid_input`, no crash. PASS.
- Break path 2 (near-miss off-topic, boundary of the pin): "vector-like particles" (M=3: vector, like, particles) → K=2 at 0.5, **6 text candidates → 3 parents, `found`** (`make memory-search`, vector top=0.610 < 0.70, so the text leg alone returns Stop Converting Documents / Context Engineering rows on "vector"+"like"). At 0.7 (K=3) → 0 → `nothing_found`. Also leaks at 0.5: "bread recipe" (M=2 → K=1) → 1 row; "vector-like" (M=2 → K=1) → 93 rows. Not leaking at 0.5: "how to bake sourdough bread at home" (M=4, K=2) → 0, "quantum field theory of dark matter" (M=5, K=3) → 0. See "Other issues".
- Break path 3 (state edge): dead text leg then recovery, and the concurrent-free repeated call — covered by the smoke above (hybrid → vector_only → hybrid, no stale state). PASS.

**Acceptance criteria**
- [x] PASS — ADR Status lines: `git diff --numstat docs/adrs` → `1 1` on 008 / 012 / 013 / 015; each note compared by eye with ADR-015 "Amendments to apply" (lines 180/181/182) → verbatim; body untouched.
- [x] PASS — Scope 4 greps run exactly as amended, on `grep` (ugrep 7.8.4 function) AND `/usr/bin/grep` (BSD): (a) 0 lines, (b) 0 lines after `grep -v`. Non-vacuity: (b) WITHOUT the `grep -v` → 21 hits on both greps (indexing.py ×14, entities/memory.py:345, glossary 35/39/50/57, the two vendored mongodb-* skill lines 116/97). Every Tree-owned residual hit reads as history (classic / legacy / retired / replaced). Skill line 54, runbook line 25 and lines 75-76 read as specified.
- [x] PASS — Log holds the 10-query table, M/K, chosen step 0.5, quantization before/after (physics paper absent: not in this corpus).
- [x] PASS — `default.yaml`, `frozen_config.yaml`, `QueryConfig` default all 0.5; `test_app_config.py` asserts 0.5 (passes in the 5283); `default.yaml` comment follows the `min_vector_score` evidence style.
- [x] PASS — MCP smoke recorded and reproduced independently (above).
- [x] PASS — PR-description text names the post-merge prod step verbatim ("run `make memory-run-indexing-pipeline` against prod right after merging; the text leg is `vector_only` until then").
- [ ] [HUMAN] Post-merge prod indexing run — awaiting human verification (correctly unchecked).

**Other issues found (not blocking the AC; the human should decide)**
- 0.5 is the protocol's lowest separating step but is not robust to short or two-generic-word off-topic phrasings, because K-of-M with M = 2 gives K = 1 and M = 3 gives K = 2: "vector-like particles" returns 3 parents as `found` at the shipped default; "bread recipe" / "vector-like" also leak. At 0.7 the 10 protocol queries still separate (on-topic fewest: "structured outputs pydantic" 13, "ReAct agent tool calling" 83, "context engineering" 50, one-word queries unchanged) and "vector-like particles" / "bread recipe" go to 0 (K=3 / K=2); "vector-like" alone keeps 6 rows at every r >= 0.6 (both terms really co-occur in 6 rows), so no ratio fixes it. Suggest a follow-up: either add near-miss off-topic queries to the protocol and pin higher, or accept 0.5 and say in the `default.yaml` comment that short generic-word queries can leak. The SWE already notes the one-step margin; this is the concrete near-miss.
- `tutorials/4_x_x_deploying_mcp_server.md:11` says "one indexing run" creates the Atlas Search indexes; true, but it does not carry the "re-run after a deploy that changes an index definition" sentence the runbook gained. Optional.
- `default.yaml` / `QueryConfig` docstring still say "PROVISIONAL" next to "pinned live", mirroring `min_vector_score`'s style; consistent, just noting.

**Cleanup**: MCP server (8765) and serve-workflows processes killed (0 left, port 8765 free), `docker start tree-prefect-worker` done, `$listSearchIndexes` = `vector_index,text_search_index`.

**VERDICT: PASS**

### [PA] 2026-10-09 00:40 — Acceptance Review (feature `atlas-search-text-leg`: 190, 191, 192; PR #47)

**VERDICT: ACCEPT**

Walked from the three user perspectives against the Tasks Plan's AC and stories; the Tester's evidence
re-read from the user's POV, plus three live `make memory-search` runs (local env, Voyage-spaced; the
Prefect worker was not touched — the CLI does not need it).

- **Claude Code user via the `tree-memory` skill / `search_memory`.** Story "full sentence, on-topic":
  `"How does the ReAct agent decide when to call a tool?"` → `text leg: 20 candidate(s), 5 query term(s),
  min_should_match=3`, hybrid, fused top-5 led by "Building Production ReAct Agents…" and "Tool Calling
  From Scratch…" — the story's promise holds. Off-topic 8-term query → `nothing_found`, hybrid (Tester).
  One-word `"Gemini"` answered by the text leg alone (vector top 0.697 < 0.70) — the leg still does its
  recall job. All-stop-word query → `hybrid`, no pipeline. Skill read chain 3 now states the K-of-M rule
  in the model's terms ("`0.5` → half of them") and keeps "never threshold the returned `score`";
  `rag.md`'s `vector_only` caveat line mirrors `text_only`. `search_memory`'s docstring, README row 464,
  `RetrievalOutcome` text all describe the ratio; no `min_text_score` / `$text` reaches a user anywhere.
- **Operator deploying / running the indexing pipeline.** First run creates `text_search_index` and
  drops the classic `text_index` once (logged, idempotent); second run is a no-op; drift heals; the M0
  cap error names the index and the fix. The deploy window reads as `search_mode: vector_only` with ONE
  WARN naming the absent index and the mode, and the runbook (step 1 bullet, step 3 sentence) and the PR
  body both say to run `make memory-run-indexing-pipeline` right after merging. MCP smoke
  `hybrid → vector_only → hybrid` reproduced by SWE and Tester.
- **graphrag seeds.** Verified by reading the shared `_text_search` (`node_filter={}` keeps entity rows,
  `mustNot` excludes parents unconditionally) and `TestGraphSeedFilter` / `test_graph_seed_search_shares_the_rule`;
  NOT run live (local mode is `rag`). ADR-015's Consequences record the entity-name trade-off (K = 1 only
  when the question has ≤ 2 content words at 0.5; entities still seed through the vector leg).
- **Documentation discipline.** ADR-015 present with the feature's seven decisions and diagram; glossary
  rows **Minimum match ratio** and **Text search index** added, **`memory` collection** / **Retrieval
  outcome** / **Search mode** amended; Status-line notes on ADR-008 / 012 / 013 verbatim; canonical terms
  used throughout code, YAML comments and copy.

**The judged finding — short near-miss off-topic queries leak at the pinned 0.5.** Live:
`"vector-like particles"` (M = 3, K = 2) → `text leg: 6 candidate(s)`, vector `0 kept (top=0.610)` →
**3 parents, `found`** ("Stop Converting Documents to Text", "How Does Memory for AI Agents Work?"), on
`vector` + `like` alone; `"bread recipe"` (M = 2, K = 1) → 1 row (Tester). At 0.7 the ten protocol
queries still separate (Tester) and the full-sentence ReAct story returns the identical top-2 at K = 4
(PA, live) — so on THIS corpus 0.7 costs nothing measurable. I still did not re-pin, and this is NOT a
rejection, because: (1) the pin followed the rule the human approved in ADR-015 §8 ("lowest 0.1 step
separating these ten queries") exactly, and moving it on one query the protocol never contained is a
post-hoc rule change, not an SWE defect; (2) it is a strict improvement over the retired `$text` leg,
where the same query returned 93 rows; (3) the real defence for `found`-with-irrelevant-passages is
unchanged — the skill cites only passages that answer, and the vector leg's 0.70 bar already excluded
every one of them; (4) `"vector-like"` alone keeps 6 rows at every ratio ≥ 0.6 (both terms co-occur), so
no ratio closes the class — only an enlarged protocol can say what is acceptable, and that is PA/evals
work. The corpus is 10 articles about agents, so "costs nothing" is thin evidence either way.

**What is missing for "documented":** the leak lives only in the PR body and the Tester's log; the two
places the next re-pinner reads — the `default.yaml` comment and the glossary **Minimum match ratio**
row — say "re-pin with the same 10-query protocol", which repeats the blind spot. Not edited here (192 is
committed and pushed; I was not asked to commit). Filed as a groomed follow-up instead:
`tasks/193-near-miss-queries-in-the-ratio-repin-protocol.md` (status pending) — enlarge ADR-015 §8's set
with ≥ 5 near-miss off-topic (M = 2 and M = 3) and ≥ 2 short on-topic queries, re-run 192's table, re-pin
by the unchanged rule, record the leak class in `default.yaml` / glossary, one ADR-015 Status-line note.
**If the human prefers to pin 0.7 now:** the evidence above supports it on this corpus; it would be a
one-value change in `default.yaml` / `frozen_config.yaml` / `QueryConfig` / `test_app_config.py` plus the
comment and glossary rows — route it through 193 so the enlarged protocol, not one query, owns the value.

**Non-blocking, noted once:** `tutorials/4_x_x_deploying_mcp_server.md:11` ("search returns nothing until
this run") is pre-existing first-deploy wording, still true when no index exists; "PROVISIONAL" next to
"pinned live" mirrors `min_vector_score`'s style. Both the Tester's and SWE's follow-ups (vector-leg
`node_filter` dict-merge, stale Docker worker image, served-flow INFO visibility) stand as recorded in
the PR body.

All acceptance criteria verified from the user's POV. Hand off to the PR Reviewer.
