---
id: 141-voyage-4-and-modal-e2e-threshold-repin
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Live e2e: reset -> index -> query on `voyage-4` with a threshold re-pin; really deploy, smoke-test and stop all three Modal Serving paths

Tags: `e2e`, `config`, `modal`, `docs`
Depends on: #134, #135, #136, #137, #138, #139, #140, #142
Blocks: —
Implements: ADR-009 — Decision 2 (the Serving path ladder, proven live), Decision 7 (migration run) and Decision 8 (threshold re-pin protocol; amends ADR-008 §4)

## Scope

**ORDER: this is the LAST task of the feature — it runs AFTER #142 (execution order 138 -> 139 -> 140 -> 142 -> 141).**

Follow `.agents/skills/run-pipelines-e2e/SKILL.md`, LOCAL env only (`make env-status` -> local),
default `memory.mode: graphrag` so dedup + resolution scores exist.

**Part 1 — voyage-4 on the local pipelines (one re-embed total).**
1. With the existing local corpus (if the DB is empty, first ingest >= 3 documents with
   `make memory-run-pipeline`): `make memory-reset-embeddings` (dry run; note the count) ->
   `make memory-reset-embeddings CONFIRM=yes` -> `make memory-run-indexing-pipeline`.
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
     (0.95) / `flag_threshold` (0.85): record the logged similarity for >= 1 true-duplicate pair and
     >= 1 distinct-but-related pair from the step-2 run (raise log level if the scores are DEBUG).
     Change a value ONLY if a true duplicate scores below its bar or a distinct pair at/above
     `flag_threshold`; nearest 0.05 step; otherwise state "unchanged — evidence: …".
   - Any change goes in `configs/default.yaml` + the matching Pydantic default + its test +
     `frozen_config.yaml`, with a `# re-pinned on voyage-4, 2026-09 — tasks/141 Log` comment.

**Part 2 — Modal, for real** (the human's workspace; CLI token + Proxy token already configured).
ONE model is live at a time and every deployment is stopped right after its test — a Dedicated
endpoint and a fallback script of the same model share the app name `ep-<endpoint_name>`.
Steps 4a-4d, in THIS order (the endpoint path first: it proves H1, on which #139/#140 were built):

4a. **Qwen3 as a Dedicated endpoint (the default path).**
   `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B` -> paste
   `uv --directory apps/memory run modal endpoint list --json` and `… modal app list` -> `…-test` -> `…-stop`.
   Record the H1 verdict: is the Modal app named `ep-qwen3-embedding-0-6b`, is its server class
   `Server`, did `resolve_server_url` find it? Also record: whether `modal endpoint create` blocks
   until ready or returns while provisioning (and how long until `/health` is 200); the JSON field
   names of `modal endpoint list --json` (is the URL there?); that `modal endpoint stop -y <name>`
   accepts the NAME; what a second `create` with the same name does; whether `eu-west` is an
   accepted `--routing-region`; the served model id reported by `/v1/models`.
   **If H1 is false — fix it HERE, smallest step first, and log which one held:**
   (i) the app is named exactly `<--name>` or some other pure function of it -> correct
   `endpoint_name` / `app_name` in `ModalEmbeddingModelConfig` (+ `test_name_derivation`, the #139
   argv tests, the glossary note for the PA); (ii) only if the app name is NOT derivable from the
   name -> in `resolve_server_url`, for `entry.serving == "endpoint"`, read the URL from
   `modal endpoint list --json` (async subprocess, match on the endpoint name) with a unit test whose
   fixture is the REAL JSON pasted above; the driver's `test` command then gains `--serving`.
   An explicit `url:` catalog field is NOT an option (a workspace-specific URL in committed YAML).
4b. **voyage-4-nano as a Dedicated endpoint with custom weights (the experiment).**
   `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano SERVING=endpoint` -> `…-test`.
   If create or the smoke test fails, paste the error — that is a complete answer. If it passes,
   save reference vectors: with the real client
   (`ModalEmbeddingModel(proxy_token=modal_proxy_bearer(), model="voyageai/voyage-4-nano").embed(texts, None)`,
   an ad-hoc `uv --directory apps/memory run python -c …` snippet, nothing committed) embed the 3
   fixed texts `"how do I reset my password?"`, `"To reset your password, open Settings and choose Reset password."`,
   `"The Eiffel Tower is 330 metres tall."` and write them to the scratchpad. Then
   `…-stop MODEL=voyageai/voyage-4-nano SERVING=endpoint`.
4c. **voyage-4-nano through the vLLM fallback script (the known-good path, and the reference).**
   `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano SERVING=vllm` -> `…-test` -> embed
   the same 3 texts the same way -> `…-stop … SERVING=vllm`. Record cold-start seconds and the image
   build outcome (this is where the CUDA 13.0.2 / vLLM wheel compatibility and the GPU string are proven).
   **Decision rule for the seed:** flip `voyageai/voyage-4-nano` to `serving: endpoint` ONLY IF 4b's
   smoke test passed AND, for each of the 3 texts, `cosine(endpoint vector, vLLM-script vector) >= 0.99`
   (same weights under the right architecture agree to numerical noise; causal attention or a
   different pooling does not — and still passes a dimension check). Otherwise the YAML keeps
   `serving: vllm`. Either way the three cosines (or 4b's error) go in `## Log`; a flip also updates
   `test_seed_entries`, `frozen_config.yaml` and the YAML comment, and writes
   `PA: glossary "Serving path" + ADR-009 §2/Consequences need voyage-4-nano vllm->endpoint` in `## Log`.
4d. **Qwen3 through the SGLang fallback script** (no seed uses SGLang — this keeps it from being dead code).
   `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B SERVING=sglang` -> `…-test` ->
   `…-stop … SERVING=sglang`. Record cold start, the SGLang install spec and image build outcome. The
   YAML is NOT changed (Qwen3 stays `endpoint`).
4e. **Nothing left running.** Paste `modal endpoint list` AND `modal app list`: no endpoint and no
   `ep-*` app is live. If the orphaned Modal secret `vllm-embedding-api-key` or the old app
   `vllm-embedding-models` exists, note it for the human to delete.

5. **Docs** — `.agents/skills/run-pipelines-e2e/SKILL.md`: a short "After changing the embedding
   model" step (reset -> indexing -> clustering, the dry-run line, `CONFIRM=yes`) and a "Modal
   embedding models" step (`MODEL=` deploy/test/stop, `SERVING=` only to walk the ladder, pass the
   same `SERVING=` to stop, always stop after). README: make sure the sections from
   #134/#137/#140/#142 read as one story and match what 4a-4d actually showed. Per the project rule,
   REMOVE now-wrong sentences rather than adding caveats.

## Out of scope
- A pipeline run with `provider: modal`. Tuning beyond the pin (Chapter 7 evals own the knobs).
- Running anything against prod; migrating prod users (README documents the per-user loop).
- Dashboard-only settings of a Dedicated endpoint (min/max/buffer containers); gated models / `--custom-hf-token`.
- Editing `docs/glossary.md` / `docs/adrs/` (PA-owned): if a default, the voyage-4-nano Serving path
  or H1 changes, write `PA: <doc> needs <old -> new>` in `## Log`.

## Acceptance Criteria

- [ ] `## Log` records: dry-run count, reset count, `Embedded N nodes` with N == reset count (minus any logged Voyage-400 skips), and the `mongosh` count of rows with a 1024-length vector.
- [ ] `## Log` records both `min_vector_score` pin queries with their `top=` scores and the FINAL value; the nonsense query answers `nothing_found`, the on-topic query `found`, at that value.
- [ ] `## Log` records >= 1 true-duplicate and >= 1 distinct-pair similarity on voyage-4 and, per knob (`semantic_threshold`, `auto_merge_threshold`, `flag_threshold`), either `unchanged — evidence` or `old -> new` with the YAML/default/test diff in the same commit.
- [ ] `make memory-visualize-embeddings` output starts with the stale-map warning after indexing and does not after clustering — both first lines pasted in `## Log`.
- [ ] 4a: `## Log` has the Qwen3 Dedicated endpoint smoke lines (`health 200 after …s`, `served model id: …`, `3 embeddings, 1024 dims`, `sanity: cos(query, relevant)=… > cos(query, unrelated)=…`, `unauthenticated health -> 401`, `Smoke test passed`), the pasted `modal endpoint list --json`, and an explicit line `H1: TRUE` or `H1: FALSE -> fix (i)|(ii)` with the diff in the same commit and `make memory-tests` green.
- [ ] 4b + 4c: `## Log` has either the endpoint attempt's error output OR its smoke lines plus the three `cosine(endpoint, vllm)` values; the vLLM-script smoke lines with cold-start seconds; and one line `voyage-4-nano serving: vllm (kept) — reason` or `vllm -> endpoint — three cosines >= 0.99`. `configs/default.yaml` matches that line.
- [ ] 4d: `## Log` has the SGLang-script smoke lines for `Qwen/Qwen3-Embedding-0.6B` with `1024 dims` and cold-start seconds; `app_config.modal.embedding_models` still says `serving: endpoint` for Qwen3.
- [ ] 4e: `modal endpoint list` AND `modal app list` output pasted in `## Log` show no running endpoint and no running `ep-*` app.
- [ ] Every "SWE must verify" item from #138, #139, #140, #142 is answered in `## Log` with its source: GPU string, vLLM pin, SGLang install spec, CUDA base image, async `get_url` form, minimum modal version shipping `modal endpoint`, `modal endpoint stop` identifier and `modal app stop` confirmation flag, `--name` -> app-name rule, `modal endpoint list --json` fields, base-model ids Modal accepted for embeddings, whether voyage-4-nano deploys as custom weights over `Qwen/Qwen3-Embedding-0.6B`, Qwen3 prompt bytes, Qwen3 Matryoshka support.
- [ ] `grep -c "memory-reset-embeddings" .agents/skills/run-pipelines-e2e/SKILL.md` >= 1, `grep -c "memory-deploy-embedding-model" …` >= 1 and `grep -c "SERVING=" …` >= 1.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green after any threshold, seed or H1 change.
- [ ] [HUMAN] Confirms in the Modal dashboard (Endpoints AND Apps) that nothing is billing, and removes `MODAL_EMBEDDING_API_KEY` from `.env` / `.env.prod`.

## User Stories

### Story: Operator migrates the local memory to voyage-4 and trusts "nothing found" again
1. Runs reset (dry, then `CONFIRM=yes`), indexing, clustering.
2. `make memory-query-graph QUERY="zxqv plorb wumbus"` -> nothing found; `QUERY="how does the coordinator shard documents?"` -> ranked parents.
3. The log line `vector leg: … kept at min_vector_score=<final> (top=…)` matches the values in this task's Log.

### Story: Operator proves the easy path — a Dedicated endpoint with zero code of ours
1. `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B`, `…-test`, `…-stop`.
2. The smoke test logs 1024-d vectors through the proxy token, a sane relevant-vs-unrelated ordering and a 401 without the token.
3. `modal endpoint list` shows it stopped.

### Story: Operator finds out whether custom weights are enough for voyage-4-nano
1. Deploys it with `SERVING=endpoint` over base `Qwen/Qwen3-Embedding-0.6B`, tests, saves 3 vectors, stops.
2. Deploys it with `SERVING=vllm`, tests, embeds the same 3 texts, stops.
3. Reads three cosines in the Log: all >= 0.99 -> the YAML now says `serving: endpoint`; otherwise it still says `vllm`, and the Log says why.

### Story: Operator proves the SGLang fallback is not dead code
1. `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B SERVING=sglang`, `…-test`, `…-stop … SERVING=sglang`.
2. 1024-d vectors, 401 without the token; the YAML still says `endpoint`.

### Story: The next engineer reads why a threshold moved
1. Opens `configs/default.yaml`, sees `# re-pinned on voyage-4, 2026-09 — tasks/141 Log`.
2. The Log holds the two pairs and their scores — no value was moved by feel.

---

Blocked by: #134, #135, #136, #137, #138, #139, #140, #142

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

### [PA] 2026-09-19 18:23 — Re-grooming (plan edit: Dedicated endpoints first)

**What changed and why**
- Part 1 (voyage-4 migration + re-pin) is unchanged. Part 2 now proves THREE Serving paths with four deploy/test/stop rounds: Qwen3 as a Dedicated endpoint (the new default), voyage-4-nano as a Dedicated endpoint with custom weights (the human's "try this first"), voyage-4-nano via the vLLM script (known-good + reference), and Qwen3 via `SERVING=sglang` (no seed uses SGLang any more, and the human chose to keep that script).
- This task owns the proof of H1 (endpoint named `N` = app `ep-N`, class `Server`) and a bounded, ordered fix if it is false — #139/#140 were built on it without a live call.
- The voyage-4-nano seed flips to `endpoint` only on a vector-equivalence check against the vLLM script (cosine >= 0.99 on 3 texts): a wrong architecture passes a dimension check and may even pass the relevant-vs-unrelated sanity check.
- One model live at a time (shared app name) and `modal endpoint list` joins `modal app list` in the "nothing is billing" evidence.
- `Depends on` gains #142, which is numbered after this task but runs before it.

**Dependencies**
- All of #134-#140 and #142.

**User stories**
- 5 stories: migration + pin, endpoint proven, custom-weights verdict, SGLang fallback proven, auditable threshold change.

Ready for implementation.
