---
id: 141-voyage-4-and-modal-e2e-threshold-repin
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Live e2e: reset → index → query on `voyage-4` with a threshold re-pin; really deploy, smoke-test and stop both Modal models

Tags: `e2e`, `config`, `modal`, `docs`
Depends on: #134, #135, #136, #137, #138, #139, #140
Blocks: —
Implements: ADR-009 — Decision 7 (migration run) and Decision 8 (threshold re-pin protocol; amends ADR-008 §4)

## Scope

Follow `.agents/skills/run-pipelines-e2e/SKILL.md`, LOCAL env only (`make env-status` → local),
default `memory.mode: graphrag` so dedup + resolution scores exist.

**Part 1 — voyage-4 on the local pipelines (one re-embed total).**
1. With the existing local corpus (if the DB is empty, first ingest ≥ 3 documents with
   `make memory-run-pipeline`): `make memory-reset-embeddings` (dry run; note the count) →
   `make memory-reset-embeddings CONFIRM=yes` → `make memory-run-indexing-pipeline`.
   Verify with `mongosh`: every child chunk and entity node of the user has `embedding` of length
   1024; 0 children carry `viz`; `make memory-visualize-embeddings` prints the stale warning;
   then `make memory-run-clustering-pipeline` clears it.
2. Ingest ONE new document through the full pipeline so the inline path (roles + dedup +
   resolution) runs on voyage-4.
3. Re-pin, recording REAL scores in `## Log`:
   - `query.min_vector_score` (0.75): the ADR-008 §4 two-query protocol — nonsense query
     `"zxqv plorb wumbus"` must answer `nothing_found`, one on-topic query `found`; record both
     `top=` values from the `vector leg: … (top=…)` INFO line. If either fails, move the default to
     the nearest 0.05 step that passes both.
   - `extraction.resolution.semantic_threshold` (0.80), `extraction.dedup.auto_merge_threshold`
     (0.95) / `flag_threshold` (0.85): record the logged similarity for ≥ 1 true-duplicate pair and
     ≥ 1 distinct-but-related pair from the step-2 run (raise log level if the scores are DEBUG).
     Change a value ONLY if a true duplicate scores below its bar or a distinct pair at/above
     `flag_threshold`; nearest 0.05 step; otherwise state "unchanged — evidence: …".
   - Any change goes in `configs/default.yaml` + the matching Pydantic default + its test +
     `frozen_config.yaml`, with a `# re-pinned on voyage-4, 2026-09 — tasks/141 Log` comment.
4. **Part 2 — Modal, for real** (the human's workspace; tokens already in `.env`):
   `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano` → `…-test` → record the log
   lines; same for `MODEL=Qwen/Qwen3-Embedding-0.6B`; then `…-stop` BOTH and confirm with
   `uv --directory apps/memory run modal app list` that neither `ep-*` app is running. Record cold
   start seconds and the image build outcome for each engine (this is where the CUDA 13.0.2 /
   wheel compatibility and the GPU string are proven). If the orphaned Modal secret
   `vllm-embedding-api-key` exists, note it for the human to delete.
5. **Docs** — `.agents/skills/run-pipelines-e2e/SKILL.md`: a short "After changing the embedding
   model" step (reset → indexing → clustering, the dry-run line, `CONFIRM=yes`) and a "Modal
   embedding models" step (`MODEL=` deploy/test/stop, always stop after). README: make sure the
   sections from #134/#137/#140 read as one story. Per the project rule, REMOVE now-wrong sentences
   rather than adding caveats.

## Out of scope
- A pipeline run with `provider: modal`. Tuning beyond the pin (Chapter 7 evals own the knobs).
- Running anything against prod; migrating prod users (README documents the per-user loop).
- Editing `docs/glossary.md` / `docs/adrs/` (PA-owned): if a default changes, write
  `PA: glossary "Retrieval outcome" + ADR-009 Consequences need <knob> old→new` in `## Log`.

## Acceptance Criteria

- [ ] `## Log` records: dry-run count, reset count, `Embedded N nodes` with N == reset count (minus any logged Voyage-400 skips), and the `mongosh` count of rows with a 1024-length vector.
- [ ] `## Log` records both `min_vector_score` pin queries with their `top=` scores and the FINAL value; the nonsense query answers `nothing_found`, the on-topic query `found`, at that value.
- [ ] `## Log` records ≥ 1 true-duplicate and ≥ 1 distinct-pair similarity on voyage-4 and, per knob (`semantic_threshold`, `auto_merge_threshold`, `flag_threshold`), either `unchanged — evidence` or `old → new` with the YAML/default/test diff in the same commit.
- [ ] `make memory-visualize-embeddings` output starts with the stale-map warning after indexing and does not after clustering — both first lines pasted in `## Log`.
- [ ] Both Modal smoke tests pass through proxy auth: `## Log` has, per model, `health 200`, `2 embeddings, 1024 dims`, `unauthenticated health → 401`, cold-start seconds.
- [ ] `modal app list` output pasted in `## Log` shows `ep-voyage-4-nano` and `ep-qwen3-embedding-0-6b` as stopped.
- [ ] Every "SWE must verify" item from #138-#140 is answered in `## Log` with its source (GPU string, vLLM pin, SGLang install spec, CUDA base image, `get_url` async form, Qwen3 prompt bytes, Qwen3 Matryoshka support).
- [ ] `grep -c "memory-reset-embeddings" .agents/skills/run-pipelines-e2e/SKILL.md` ≥ 1 and `grep -c "memory-deploy-embedding-model" .agents/skills/run-pipelines-e2e/SKILL.md` ≥ 1.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green after any threshold change.
- [ ] [HUMAN] Confirms in the Modal dashboard that no `ep-*` container is billing, and removes `MODAL_EMBEDDING_API_KEY` from `.env` / `.env.prod`.

## User Stories

### Story: Operator migrates the local memory to voyage-4 and trusts "nothing found" again
1. Runs reset (dry, then `CONFIRM=yes`), indexing, clustering.
2. `make memory-query-graph QUERY="zxqv plorb wumbus"` → nothing found; `QUERY="how does the coordinator shard documents?"` → ranked parents.
3. The log line `vector leg: … kept at min_vector_score=<final> (top=…)` matches the values in this task's Log.

### Story: Operator proves both engines serve a model end to end
1. Deploys, tests and stops `voyageai/voyage-4-nano` (vLLM), then `Qwen/Qwen3-Embedding-0.6B` (SGLang).
2. Each smoke test logs 1024-d vectors through the proxy token and a 401 without it.
3. `modal app list` shows both stopped — nothing keeps billing.

### Story: The next engineer reads why a threshold moved
1. Opens `configs/default.yaml`, sees `# re-pinned on voyage-4, 2026-09 — tasks/141 Log`.
2. The Log holds the two pairs and their scores — no value was moved by feel.

---

Blocked by: #134, #135, #136, #137, #138, #139, #140

## Log

### [PA] 2026-09-19 17:43 — Grooming

**Summary**
The feature's live acceptance: one re-embed on voyage-4 with roles, an evidence-backed threshold re-pin, and both Modal engines really deployed, smoke-tested and stopped.

**Key decisions**
- Re-pin, not tune: same two-query protocol and 0.05 steps as `tasks/done/125`; dedup/resolution knobs move only on a recorded counter-example.
- Roles (#136) land before the reset so the corpus is re-embedded exactly once.
- Glossary/ADR edits stay with the PA; the SWE flags a changed default in the Log.

**Dependencies**
- All of #134-#140.

**User stories**
- 3 stories: migration + pin, both engines proven, auditable threshold change.

Ready for implementation.
