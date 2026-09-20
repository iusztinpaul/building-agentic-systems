---
id: 141-voyage-4-and-modal-e2e-threshold-repin
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Live e2e: reset -> index -> query on `voyage-4` with a threshold re-pin (in a separate local database); then four auto-routed Modal cycles — embedding -> endpoint, embedding -> vLLM App, LLM -> SGLang App, LLM -> endpoint — under our own `tree-` names, touching nothing that existed before

Tags: `e2e`, `config`, `modal`, `llm`, `docs`
Depends on: #134, #135, #136, #137, #138, #139, #140, #142, #143, #144, #145, #146, #147, #148, #149, #150, #151, #152
Blocks: —
Implements: ADR-009 — Decision 2 (auto-routing, proven live on four cycles), Decision 3 (the `tree-` namespace, the guard and H1, proven live), Decision 7 (migration run), Decision 8 (threshold re-pin protocol; amends ADR-008 §4), Decision 9 (the `HF_TOKEN` path on the Apps, checked live on public weights), Decision 10 (`ModalLLM` returns valid JSON from both routes) and Decision 11 (cold start waited out; one cold-again re-warm)

## Scope

**ORDER: this is the LAST task of the feature. Execution order of the rework: 143 -> 144 -> 145 -> 146 -> 147 -> 148 -> 149 -> 150 -> 151 -> 152 -> 141.**

**LLM CACHE CAVEAT (added 2026-09-20 19:30).** Until #151 has landed, the 30-day `INPUTS` cache of
`llm-extract-entities` (and the 90-day one of `summarise-cluster`) carries no LLM identity, so a document
already extracted under Gemini REPLAYS Gemini's JSON after `models.llm` is flipped to `modal` — a green run
in which the Modal LLM was never asked. #151 fixes this by construction (`llm_identity` in the cache key) and
is a hard dependency of this task: do not start before it is merged into this branch. Even so, every live LLM
cycle (4c, 4d — and any step you run THROUGH the pipeline with `TREE_MODELS__LLM__PROVIDER=modal`) must PROVE
the completion came from the Modal server and not from a cache hit: the run's own `ModalLLM ready: app=… served_model=…`
line, plus the Opik `modal` usage span of that call (`provider: modal`, `total_cost=0`, non-zero tokens). For a
pipeline-routed step additionally show that `llm-extract-entities` finished `Completed`, not `Cached`. The
direct `ModalLLM(...).generate_json(...)` client calls of 4c/4d bypass Prefect, so no cache sits in their path —
say so next to the proof rather than omitting it.

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
THIS is task 141: the ONLY place those commands run for real — and only the ones written below, only on
`tree-` / `ep-tree-` names.

Follow `.agents/skills/run-pipelines-e2e/SKILL.md`, LOCAL env only (`make env-status` -> local),
default `memory.mode: graphrag` so dedup + resolution scores exist.

**Part 1 — voyage-4 on the local pipelines, in a SEPARATE local database `tree_e2e_141`.**
The human's real local memory (database `tree`, 2423 embedded rows, shared with the `main` checkout) is NOT
reset, re-embedded or written by this task — the human runs the documented per-user reset after merge.
0. **Isolation.** The database-name knob is `MONGO_INITDB_DATABASE` (`MongoSettings.mongo_initdb_database` in
   `src/tree/config/settings.py`; `.env.example`; default `tree`). Agents never edit or read `.env`: pass
   `MONGO_INITDB_DATABASE=tree_e2e_141` as a MAKE COMMAND-LINE VARIABLE (after the target) on EVERY command of
   Part 1 — `make memory-serve-workflows MONGO_INITDB_DATABASE=tree_e2e_141` first of all (the flow reads it
   inside the serving process, same rule as `TREE_MEMORY__MODE`), then signup, pipelines, reset, query,
   visualize. Verified at grooming (GNU Make 3.81, the same `include .env` + `export` + `$(MAKE) -C` shape):
   `make <target> VAR=x` BEATS the included `.env` and travels into `apps/memory`; `VAR=x make <target>` does
   NOT (the include wins); pydantic-settings prefers the process env over the env file.
   Also pass `USER_IDENTIFIER=e2e-141@example.com` on every command (`.env` exports the human's
   `TREE_USER_IDENTIFIER`); create that user with
   `make memory-signup USER_IDENTIFIER=e2e-141@example.com MONGO_INITDB_DATABASE=tree_e2e_141` — the
   current-user pointer is the `sessions` collection of THAT database, so the human's stays put.
   If `docker ps` shows `tree-prefect-worker`, `docker stop` it for Part 1 and `docker start` it in cleanup
   (it executes main's code against `tree`).
   BEFORE anything else record, with `mongosh`, in database `tree`: the count of `memory` rows with a
   non-empty `embedding` (expected 2423) and the `documents` count. Re-count at the end of Part 1: both
   must be identical. After the first ingest, assert the document landed in `tree_e2e_141.documents`.
1. Fresh small corpus: ingest 3 documents, one run each,
   `make memory-run-pipeline MODE=online SOURCE="$PWD/docs/adrs/<file>" USER_IDENTIFIER=… MONGO_INITDB_DATABASE=…`
   for `002_pipeline_concurrency_and_voyage_rate_limiting.md`, `006_rag_graphrag_memory_modes.md`,
   `007_embedding_clusters_and_explicit_offline_phases.md`. Then the migration mechanics:
   `make memory-reset-embeddings` (dry run; note the count) ->
   `make memory-reset-embeddings CONFIRM=yes` -> `make memory-run-indexing-pipeline`.
   Verify with `mongosh` (database `tree_e2e_141`): every child chunk and entity node of the user has
   `embedding` of length 1024; 0 children carry `viz`; `make memory-visualize-embeddings` prints the stale
   warning; then `make memory-run-clustering-pipeline` clears it (if it is skipped below `min_cluster_size`,
   serve with `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5` as the e2e skill says).
2. Ingest ONE new document (`docs/adrs/008_mcp_tool_contract.md`, same command) so the inline path (roles +
   dedup + resolution) runs on voyage-4 against the 3 already there.
3. Re-pin, recording REAL scores in `## Log`:
   - `query.min_vector_score` (0.75): the ADR-008 §4 two-query protocol — nonsense query
     `"zxqv plorb wumbus"` must answer `nothing_found`, one on-topic query
     (`"how does the coordinator shard documents?"` — ADR-002 is in the corpus) `found`; record both
     `top=` values from the `vector leg: … (top=…)` INFO line. If either fails, move the default to
     the nearest 0.05 step that passes both.
   - `extraction.resolution.semantic_threshold` (0.80), `extraction.dedup.auto_merge_threshold`
     (0.95) / `flag_threshold` (0.85): record the logged similarity for >= 1 true-duplicate pair and
     >= 1 distinct-but-related pair from the step-2 run (raise log level if the scores are DEBUG).
     Change a value ONLY if a true duplicate scores below its bar or a distinct pair at/above
     `flag_threshold`; nearest 0.05 step; otherwise state "unchanged — evidence: …".
   - Any change goes in `configs/default.yaml` + the matching Pydantic default + its test +
     `frozen_config.yaml`, with a `# re-pinned on voyage-4, 2026-09 — tasks/141 Log` comment.
   - The pin is taken on THIS 4-document corpus; say so in the Log line (`corpus: 4 ADRs, tree_e2e_141`).
3b. Cleanup of Part 1: stop the serve process; `mongosh` -> `db.getSiblingDB("tree_e2e_141").dropDatabase()`
   (that literal name only); restart `tree-prefect-worker` if it was stopped; paste the two `tree` re-counts.

**Part 2 — Modal, for real** (the human's workspace; CLI token + Proxy token already configured).
FOUR cycles, the ones the human selected, each `deploy -> test -> probes -> stop` with NO `SERVING=`: the
driver decides the route and the `Routing …` line is the evidence. ONE model is live at a time.

**HARD RULES (the 2026-09-20 incident, see #143):** this task creates, tests and stops ONLY names that start
with `tree-` / `ep-tree-`. It NEVER calls, stops, recreates, deploys over or smoke-tests anything that existed
before the run; it NEVER passes `FORCE=yes`; if the guard refuses (exit 3) where this file does not expect it,
STOP and write the refusal in `## Log` — do not work around it. NOT ours and never touched: the human's own
endpoints `qwen3-embedding-0-6b`, `qwen3-embedding-8b`, `gpt-oss-120b`, `qwen3-6-35b-a3b-fp8` (no request is
sent to them — a request wakes a billed GPU), and the stopped accidental apps `ep-voyage-4-nano` /
`ep-qwen3-embedding-4b`. To check a command without running it use `DRY_RUN=yes` — never a fake `modal` on `PATH`.

**Cost note.** Four short cycles on small hardware: two A10 Apps (voyage-4-nano, LFM2.5-350M) and two managed
endpoints whose GPU Modal picks (A10 for Qwen3-Embedding-0.6B per its recipe; record what it picks for
Qwen3.5-0.8B). Each is up for its cold start + a few requests + <= 10 minutes; the cold-again proof adds ~8
GPU-LESS minutes (scaled to zero bills nothing) and one more cold start. Expect low single-digit dollars;
record per cycle the GPU and the minutes between the deploy and the stop. If any single cycle is up for more
than 30 minutes, stop it and write why.

4.0 **Baseline, before the first deploy.** Save `uv --directory apps/memory run modal app list --json` and
   `… modal endpoint list --json` to `<scratchpad>/baseline-apps.json` / `baseline-endpoints.json`, plus
   `… modal app history <description> --json` for every baseline app whose name starts with `ep-` but not
   `ep-tree-`. Paste a summary (name, id, state/status, latest version) in `## Log`, AND the two JSON key sets
   (this is the live confirmation #143 deferred). If a `tree-*` endpoint or a live `ep-tree-*` app is ALREADY
   there, stop and ask — it is not from this run.

**In EVERY cycle record:** the full `Routing …` line; Modal's verbatim refusal text when there is one, and
whether it arrived on stdout or stderr (the live check of #145's two substrings); the poller's own
`Warm: <url> answered HTTP 200 after <N>s` line from the `-test` run — THAT is the cycle's cold-start number
(#144); the GPU; deploy-to-stop minutes. Capture each deploy with `2>&1 | tee <scratchpad>/4x-deploy.log`.

**Wire-width probe — embedding cycles 4a and 4b, while the model is up, right after `-test`.** Two raw
`POST /v1/embeddings` calls for one text (ad-hoc `uv --directory apps/memory run python -c …` using
`resolve_server_url`, `modal_proxy_bearer`, `served_model_id` + an HTTP client; nothing committed): one WITHOUT
`dimensions`, one WITH `dimensions` = `512` for Qwen3 / `1024` for voyage-4-nano. ONE line per cycle:
`wire: <repo_id> via <endpoint|vLLM App> — no dimensions -> <len>; dimensions=<n> -> <len> | HTTP <status> "<error message>"`.
The second half is EVIDENCE ONLY (a 400 is a complete answer — the client never sends `dimensions`, ADR-009 §3;
Modal's 0.6B recipe sets no `is_matryoshka`, so a 400 is the expected answer on 4a). The first half is a GATE:
if `no dimensions` differs from the entry's `native_dimensions` (Qwen3 1024, voyage-4-nano 2048), the catalog
is wrong — fix `configs/default.yaml` + `test_seed_entries` + `frozen_config.yaml` in this task's commit and
write `PA: glossary "Modal catalog" + ADR-009 need native_dimensions <old -> new> for <repo_id>` in `## Log`.

4a. **(i) EMBEDDING -> ENDPOINT: `Qwen/Qwen3-Embedding-0.6B`** — OUR Dedicated endpoint
   `tree-qwen3-embedding-0-6b`, created next to, never instead of, the human's hand-made one.
   `make memory-deploy-model MODEL=Qwen/Qwen3-Embedding-0.6B` -> expect
   `Routing Qwen/Qwen3-Embedding-0.6B: Modal accepted it → Dedicated endpoint tree-qwen3-embedding-0-6b` ->
   paste `modal endpoint list --json` and `modal app list` -> `make memory-deploy-model-test MODEL=…` -> wire
   probe -> the guard's live proof: a SECOND `make memory-deploy-model MODEL=Qwen/Qwen3-Embedding-0.6B` while
   it is up is REFUSED (paste the line, exit 3; costs nothing; what Modal itself does on a duplicate `create`
   is NOT probed) -> `make memory-deploy-model-stop MODEL=…` (no `SERVING=`; expect the endpoint stop to
   succeed on the first try).
   Record the **H1 verdict**: is the Modal app named `ep-tree-qwen3-embedding-0-6b`, is its server class
   `Server`, did `resolve_server_url` find it? Also: whether `modal endpoint create` blocks until ready or
   returns while provisioning; that `modal endpoint stop -y <name>` accepts the NAME; whether `eu-west` is an
   accepted `--routing-region`; the served model id from `/v1/models`.
   **If H1 is false — fix it HERE, smallest step first, and log which one held:** (i) the app is some other
   pure function of `--name` -> correct `endpoint_name` / `app_name` in the config (+ `test_name_derivation`,
   the argv tests, a `PA:` glossary line); (ii) only if the app name is NOT derivable -> in
   `resolve_server_url`, read the URL from `modal endpoint list --json` (async subprocess, match on the
   endpoint name) with a unit test whose fixture is the REAL JSON pasted above. An explicit `url:` catalog
   field is NOT an option (a workspace-specific URL in committed YAML).
4b. **(ii) EMBEDDING -> vLLM APP: `voyageai/voyage-4-nano`.**
   `make memory-deploy-model MODEL=voyageai/voyage-4-nano` -> expect Modal's
   `'voyageai/voyage-4-nano' is not available for dedicated Endpoints.` (NOTHING is created), no catalog base on
   the Hub card, `Routing voyageai/voyage-4-nano: not in Modal's endpoint catalog, no catalog base → vLLM App`,
   then `Running: modal deploy deploy/modal_vllm_embedding.py` in the SAME command. The earlier plan's "try
   voyage-4-nano as an endpoint with custom weights" step no longer exists as a step: it IS the router's
   refusal, recorded here. Then `-test`; expected smoke lines `3 embeddings, 2048 dims`,
   `truncated 2048 -> 1024 dims client-side, norm=1.000`, `sanity@1024: …`; wire probe (`no dimensions -> 2048`).
   This is where vLLM `0.26.0` + CUDA 13.0.2 + the A10 string + `VoyageQwen3BidirectionalEmbedModel` are proven
   (#146); if the image or the engine fails, paste the error, fix the pin / flag in YAML (+ tests), redeploy,
   and say what moved.
   **Shared-space check (recorded, NO gate), while 4b is up:** embed the 3 fixed texts
   (`"how do I reset my password?"`, `"To reset your password, open Settings and choose Reset password."`,
   `"The Eiffel Tower is 330 metres tall."`) with the Voyage API model (`voyage-4`, 1024-d,
   `input_type="document"`, through `get_model`'s Voyage client) and with
   `ModalEmbeddingModel(proxy_token=modal_proxy_bearer(), model="voyageai/voyage-4-nano", dimensions=1024)`
   (`input_type="document"`; assert `len == 1024` on all three); record the 3 same-text cosines
   `cos(voyage-4 API @1024, nano @1024)` plus ONE cross-text contrast (`voyage-4` text 1 vs nano text 3). No
   threshold: nothing in the memory mixes the two models. If same-text is not clearly above cross-text, write
   `PA: ADR-009 Context "one shared embedding space" needs correction`.
   **HF token (App path), value-free — NEVER open, print or grep `.env`; never paste a token:** stream
   `uv --directory apps/memory run modal app logs ep-tree-voyage-4-nano` into `<scratchpad>/4b-app.log` until
   `HF_TOKEN set in container: …` appears, then interrupt. Then the leak check over EVERY captured file:

       uv --directory apps/memory run python -c "import sys, pathlib; from tree.config.settings import settings; t = settings.hf_token.get_secret_value(); sys.exit(2 if not t else 1 if any(t in pathlib.Path(p).read_text() for p in sys.argv[1:]) else 0)" <scratchpad>/4a-deploy.log <scratchpad>/4b-deploy.log <scratchpad>/4b-app.log <scratchpad>/4c-deploy.log <scratchpad>/4d-deploy.log; echo "leak-check exit=$?"

   (run it again at the end with all files present). `0` = the token occurs nowhere; `1` = LEAK -> stop, fix
   the redaction first and tell the human to rotate the token; `2` = no token configured. Record
   `HF token path (App): PROVEN (HF_TOKEN set in container: True, leak-check exit=0)` or
   `HF token path (App): NOT PROVEN LIVE — HF_TOKEN not set (container says False); unit evidence only`.
   The ENDPOINT half of the token path (`--custom-hf-token ***` on a custom-weights create) is NOT exercised
   live: no selected cycle is a custom-weights create. It stays unit-tested (#139 `TestHfToken`, #145
   `TestRouter` case 4) — say so in the Log line.
   **Cold-again proof (REQUIRED once, here; ~8 GPU-less minutes).** Still in 4b, ONE ad-hoc process (same
   snippet style, INFO logging on): build the `ModalEmbeddingModel`, `embed(["warm"], "document")`, then
   `await asyncio.sleep(420)` (the App's `scaledown_window` is 5 minutes), then `embed(["cold again"],
   "document")`. Expected log: `Cold again: ep-tree-voyage-4-nano answered HTTP 503 — re-warming once`, the
   `Still cold …` series, `Warm: … after <N>s`, `Warm again: …`, and a 1024-d vector back — ONE re-warm, ONE
   retry, no exception. If the second call is NOT cold (Modal kept the container), record
   `cold-again: not reproduced — container still warm after 420 s` and move on: that is not a failure.
   Then `make memory-deploy-model-stop MODEL=voyageai/voyage-4-nano` — expect the path-blind line
   `Not a Dedicated endpoint (…) — stopping the App instead.`
4c. **(iii) LLM -> SGLANG APP: `LiquidAI/LFM2.5-350M`.**
   `make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M` -> expect the refusal and
   `Routing LiquidAI/LFM2.5-350M: not in Modal's endpoint catalog, no catalog base → SGLang App` (its Hub base
   `LiquidAI/LFM2.5-350M-Base` is not in Modal's list), then `modal deploy deploy/modal_sglang_llm.py`. This is
   where the `lmsysorg/sglang:<tag>` image, `uv_pip_install` on it, LFM2 support and the in-container strict
   JSON warm-up are proven (#147); on failure paste the container log, fix tag / flags in YAML (+ tests),
   redeploy, say what moved — or, if SGLang cannot serve LFM2.5, follow #147's LOUD replacement procedure.
   `make memory-deploy-model-test MODEL=…` -> the six chat smoke lines. **Then the client check** (ad-hoc
   snippet, nothing committed):
   `ModalLLM(proxy_token=modal_proxy_bearer(), model="LiquidAI/LFM2.5-350M").generate_json("Reply with JSON facts about Tokyo.", schema=<the city_facts schema>)`
   -> paste the returned dict and assert `set(result) == {"city", "population"}` and
   `isinstance(result["population"], int)`; then the SAME call WITHOUT `schema` (JSON mode) -> paste what came
   back, or the `ExtractionError` — JSON mode on a 350M model is recorded, NOT gated. Include
   `<scratchpad>/4c-app.log` (`HF_TOKEN set in container: …`) in the leak check. Stop.
4d. **(iv) LLM -> ENDPOINT: `Qwen/Qwen3.5-0.8B`** (chosen over `google/gemma-3-1b-it`: ungated, Apache-2.0).
   `make memory-deploy-model MODEL=Qwen/Qwen3.5-0.8B` -> expect
   `Routing Qwen/Qwen3.5-0.8B: Modal accepted it → Dedicated endpoint tree-qwen3-5-0-8b` -> `-test` (the SAME
   chat smoke test — path-blind) -> the SAME two `ModalLLM` calls as 4c (strict schema asserted; JSON mode
   recorded — a thinking model may answer `Modal LLM returned an empty response` / `invalid JSON: <think>…`;
   paste it, it feeds ADR-009's "what would justify upgrading") -> record the GPU Modal picked and the served
   model id -> stop (endpoint stop on the first try).
4e. **Nothing of ours left running, nothing of theirs changed.** Paste `modal endpoint list --json` AND
   `modal app list --json`: no `tree-*` endpoint and no live `ep-tree-*` app. Compare with the 4.0 baseline:
   every baseline row is still there with the same id (`app_id` / `endpoint_id`) and the same `state`
   (`tasks`, and an endpoint's `status`, may differ — record them), and `modal app history` of every baseline
   `ep-*` app shows no version newer than the baseline's. Write one line:
   `baseline: <n> apps, <m> endpoints — all unchanged` or list each difference. If the orphaned Modal secret
   `vllm-embedding-api-key` or the old app `vllm-embedding-models` exists, note it for the human to delete.

5. **Docs** — `.agents/skills/run-pipelines-e2e/SKILL.md`: a short "After changing the embedding model" step
   (reset -> indexing -> clustering, the dry-run line, `CONFIRM=yes`) and a "Models on Modal" step
   (`make memory-deploy-model MODEL=` / `-test` / `-stop` for embeddings AND LLMs; the driver routes by itself
   and logs one `Routing …` line; `SERVING=endpoint|app` is an escape hatch only; always stop after;
   `HF_TOKEN` only for private or gated repos; a cold start is waited out by the poller, raise
   `TREE_MODAL__WARMUP_DEADLINE_S` for a slow first boot) that ENDS with this sentence, verbatim: "To see what
   a `make memory-deploy-model*` target would run WITHOUT deploying, add `DRY_RUN=yes`: it logs the redacted
   `modal` command and exits 0 without starting `modal`. That is the only safe way — a fake `modal` on `PATH`
   does NOT work under `make` / `uv run`, because `.venv/bin` comes first and the real CLI runs (on 2026-09-20
   this deployed over a live endpoint)." README: make sure the sections from #134/#137/#140/#145-#149 read as
   one story and match what 4a-4d actually showed. Per the project rule, REMOVE now-wrong sentences rather
   than adding caveats.

## Out of scope
- Resetting, re-embedding or writing the real local database `tree` (the human does it after merge).
- Any request to, stop of, or deploy over a Modal app or endpoint that is in the 4.0 baseline. `FORCE=yes`.
  `SERVING=` (the four cycles prove AUTO-routing; the escape hatch stays unit-tested).
- **The fine-tune -> custom-weights endpoint cycle** (e.g. `Octen/Octen-Embedding-0.6B`): the human did NOT
  select it. That route — HF lineage, the second `create`, the `not a servable checkpoint` fallback, the
  redacted `--custom-hf-token` — is UNIT-TESTED ONLY (#145 `TestRouter` cases 4-7, against today's exact live
  texts). Say so in `## Log`.
- A pipeline run with `provider: modal` for either model kind; a full extraction on a Modal LLM; any statement
  about extraction quality (a 350M model will not pass it — this task proves plumbing: valid JSON back).
- Tuning beyond the pin (Chapter 7 evals own the knobs). Running anything against prod.
- Dashboard-only settings of a Dedicated endpoint (min/max/buffer containers).
- Deploying a really gated or private model: the token path is proven on PUBLIC weights, App half only.
- Editing `docs/glossary.md` / `docs/adrs/` (PA-owned): if a default, a seed, H1, a pin or the token mechanism
  changes, write `PA: <doc> needs <old -> new>` in `## Log`.

## Acceptance Criteria

- [ ] Isolation: `## Log` has the two `tree` counts (embedded `memory` rows, `documents`) BEFORE and AFTER Part 1 and they are equal (expected 2423 embedded); every Part 1 command pasted in the Log carries `MONGO_INITDB_DATABASE=tree_e2e_141` and `USER_IDENTIFIER=e2e-141@example.com`; `tree_e2e_141` is dropped at the end.
- [ ] `## Log` records (database `tree_e2e_141`, corpus: 4 ADRs): dry-run count, reset count, `Embedded N nodes` with N == reset count (minus any logged Voyage-400 skips), and the `mongosh` count of rows with a 1024-length vector.
- [ ] `## Log` records both `min_vector_score` pin queries with their `top=` scores and the FINAL value; the nonsense query answers `nothing_found`, the on-topic query `found`, at that value.
- [ ] `## Log` records >= 1 true-duplicate and >= 1 distinct-pair similarity on voyage-4 and, per knob (`semantic_threshold`, `auto_merge_threshold`, `flag_threshold`), either `unchanged — evidence` or `old -> new` with the YAML/default/test diff in the same commit.
- [ ] `make memory-visualize-embeddings` output starts with the stale-map warning after indexing and does not after clustering — both first lines pasted in `## Log`.
- [ ] 4.0: `## Log` has the baseline summary (every app and endpoint: name, id, state/status, latest version of each non-`tree` `ep-*` app) and the two `list --json` key sets, taken BEFORE the first deploy.
- [ ] Routing, live: `## Log` has FOUR `Routing …` lines — 4a `→ Dedicated endpoint tree-qwen3-embedding-0-6b`, 4b `→ vLLM App`, 4c `→ SGLang App`, 4d `→ Dedicated endpoint tree-qwen3-5-0-8b` — and, for 4b and 4c, Modal's verbatim refusal containing `is not available for dedicated Endpoints` plus the stream it came on. No `SERVING=` and no `FORCE=yes` appears in any command of this Log.
- [ ] Guard, live: the refusal line `Refusing to deploy Qwen/Qwen3-Embedding-0.6B …: 'tree-qwen3-embedding-0-6b' already exists on Modal as a Dedicated endpoint. …` with exit code 3.
- [ ] 4a: the `tree-qwen3-embedding-0-6b` smoke lines (`health 200 after …s`, `served model id: …`, `3 embeddings, 1024 dims`, `sanity: cos(query, relevant)=… > cos(query, unrelated)=…`, `unauthenticated health -> 401`, `Smoke test passed`), the pasted `modal endpoint list --json`, `modal app list` showing `ep-tree-qwen3-embedding-0-6b`, and an explicit line `H1: TRUE` or `H1: FALSE -> fix (i)|(ii)` with the diff in the same commit and `make memory-tests` green.
- [ ] 4b: the vLLM App smoke lines (`3 embeddings, 2048 dims`, `truncated 2048 -> 1024 dims client-side`, `sanity@1024: …`), the vLLM version actually built, and the stop's `Not a Dedicated endpoint (…) — stopping the App instead.` line.
- [ ] Wire width: TWO `wire: …` lines (4a, 4b) — `no dimensions -> 1024` (Qwen3) and `-> 2048` (voyage-4-nano), each with the length or HTTP status + message for the `dimensions` call — or the catalog fix + `PA:` line in the same commit.
- [ ] Shared space: the 3 same-text cosines `cos(voyage-4 API @1024, nano @1024)` and the 1 cross-text contrast, labelled recorded-not-gated, and a `PA:` line only if same-text is not clearly above cross-text.
- [ ] Cold start: FOUR `Warm: … answered HTTP 200 after <N>s` lines (one per cycle, from the `-test` runs), each preceded by >= 1 `Still cold (HTTP 5xx)` line unless the server was already warm (say so).
- [ ] Cold again: either the sequence `Cold again: ep-tree-voyage-4-nano answered HTTP 503 — re-warming once` -> `Warm: …` -> `Warm again: …` with a 1024-d vector returned and exactly ONE `Cold again` line, or `cold-again: not reproduced — container still warm after 420 s`.
- [ ] 4c + 4d: for EACH LLM cycle the six chat smoke lines (incl. `strict JSON schema honoured: city=… population=…` and `unauthenticated health -> 401`), the dict returned by `ModalLLM.generate_json(…, schema=…)` with exactly the keys `city` / `population`, the JSON-mode result or its `ExtractionError` text (recorded, not gated), the `ModalLLM ready: app=ep-tree-… served_model=…` line, and for 4c the SGLang image tag that actually ran; for 4d the GPU Modal picked.
- [ ] Modal LLM really called (not a cache replay): for EACH of 4c and 4d, `## Log` pastes the `ModalLLM ready: app=ep-tree-… served_model=…` line of the run AND the Opik usage span of the same call showing `provider: modal`, `total_cost=0` and non-zero token counts, with one sentence stating whether the call went through Prefect (then also the `llm-extract-entities` task state `Completed`, never `Cached`) or was the direct client call (no cache in the path). `git log --oneline` of the branch shows #151's commit BEFORE any 4c/4d evidence.
- [ ] HF token: exactly one of `HF token path (App): PROVEN (HF_TOKEN set in container: True, leak-check exit=0)` or `HF token path (App): NOT PROVEN LIVE — HF_TOKEN not set (container says False); unit evidence only`, plus the sentence that the endpoint `--custom-hf-token` half is unit-tested only. `grep -c "hf_[A-Za-z0-9]\{20,\}" tasks/141-voyage-4-and-modal-e2e-threshold-repin.md` -> 0.
- [ ] 4e: `modal endpoint list --json` AND `modal app list --json` pasted in `## Log` show no `tree-*` endpoint and no live `ep-tree-*` app, and the line `baseline: <n> apps, <m> endpoints — all unchanged` (same ids, same states, no new version in any baseline `ep-*` app's history). Every `modal … stop` argv in this Log names a `tree-` / `ep-tree-` name.
- [ ] Cost: per cycle the GPU and deploy-to-stop minutes; no cycle above 30 minutes (or the reason).
- [ ] Every "SWE must verify" item left open by #138-#140, #142 and #143-#148 is answered in `## Log` with its source: GPU strings, the vLLM pin + CUDA base actually working, `VoyageQwen3BidirectionalEmbedModel` on that pin, the SGLang docker tag + `uv_pip_install` on it, LFM2 on SGLang, `speculative_model_path` optional, `json_object` on SGLang, async `get_url`, `modal endpoint stop` by name, `--name` -> app-name rule (H1), the two `list --json` key sets, whether `endpoint create` blocks, refusal on stdout vs stderr, Qwen3 prompt bytes, the wire width of each embedding seed and what each route answers to `dimensions`, `secrets=` on `@app.server`, local-vs-container `Secret.from_dict`, the engine subprocess inheriting `HF_TOKEN`.
- [ ] `grep -c "memory-reset-embeddings" .agents/skills/run-pipelines-e2e/SKILL.md` >= 1, `grep -c "memory-deploy-model" …` >= 1, `grep -c "Routing" …` >= 1, `grep -c "HF_TOKEN" …` >= 1, `grep -c "DRY_RUN=yes" …` >= 1, `grep -c "TREE_MODAL__WARMUP_DEADLINE_S" …` >= 1, and `grep -c "deploy-embedding-model" …` == 0.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green after any threshold, seed, pin or H1 change.
- [ ] [HUMAN] Confirms the hand-made `qwen3-embedding-0-6b` Dedicated Endpoint (overwritten by the accidental deploy of 2026-09-20) was restored in the dashboard and serves again — this task sent it no request. Says here if a smoke test of it is wanted.
- [ ] [HUMAN] After merge: runs reset -> indexing -> clustering on the real local memory (`tree`, 2423 embedded rows) per the README, and decides whether to delete the stopped accidental apps `ep-voyage-4-nano` and `ep-qwen3-embedding-4b`.
- [ ] [HUMAN] Confirms in the Modal dashboard (Endpoints AND Apps) that nothing of ours (`tree-*`) is billing, and removes `MODAL_EMBEDDING_API_KEY` from `.env` / `.env.prod`.
- [ ] [HUMAN] ONLY if the Log says `HF token path (App): NOT PROVEN LIVE`: either add a read-scope `HF_TOKEN` to `.env` and ask for a re-run of 4b's deploy + container line + leak check (one cold start, then stop), or accept the unit-test evidence — state which in the Log.

## User Stories

### Story: Operator migrates the local memory to voyage-4 and trusts "nothing found" again
1. With `MONGO_INITDB_DATABASE=tree_e2e_141` on every command: ingests 4 ADRs, runs reset (dry, then `CONFIRM=yes`), indexing, clustering. `tree` still holds its 2423 embedded rows.
2. `make memory-query-graph QUERY="zxqv plorb wumbus"` -> nothing found; `QUERY="how does the coordinator shard documents?"` -> ranked parents.
3. The log line `vector leg: … kept at min_vector_score=<final> (top=…)` matches the values in this task's Log.

### Story: Operator serves an embedding model and never picks a path
1. `make memory-deploy-model MODEL=Qwen/Qwen3-Embedding-0.6B`, `-test`, `-stop` — no `SERVING=` anywhere.
2. `Routing …: Modal accepted it → Dedicated endpoint tree-qwen3-embedding-0-6b`; the smoke test logs `3 embeddings, 1024 dims`, a sane ordering and a 401 without the token.
3. The hand-made `qwen3-embedding-0-6b` is exactly as in the baseline.

### Story: Modal refuses voyage-4-nano and the operator does nothing about it
1. `make memory-deploy-model MODEL=voyageai/voyage-4-nano`.
2. The refusal, then `→ vLLM App`, then the image build — one command. The smoke test shows `3 embeddings, 2048 dims` and `truncated 2048 -> 1024 dims client-side`.
3. `-stop` first tries the endpoint, says `Not a Dedicated endpoint (…)`, stops the App.

### Story: Operator serves a small custom LLM and gets JSON back
1. `make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M` -> `→ SGLang App`; `-test` prints `strict JSON schema honoured: city=Tokyo population=…`.
2. A two-line snippet with `ModalLLM(...).generate_json(…, schema=…)` returns `{"city": "Tokyo", "population": …}`.
3. The same two steps work unchanged for `Qwen/Qwen3.5-0.8B`, which Modal serves as a Dedicated endpoint — the client never learned the difference.

### Story: The test right after a deploy no longer fails in one second
1. `make memory-deploy-model-test` on a scaled-to-zero server.
2. `Warming …`, a series of `Still cold (HTTP 503) at … — 23s/600s`, then `Warm: … after 104s` — that number is the cold start on record.

### Story: The server went cold in the middle of a session
1. One process embeds, idles 7 minutes, embeds again.
2. ONE `Cold again: ep-tree-voyage-4-nano answered HTTP 503 — re-warming once`, one poll series, `Warm again`, a vector — no exception reached the caller.

### Story: The next engineer wonders whether the server could truncate for us
1. Opens this task's Log and finds `wire: Qwen/Qwen3-Embedding-0.6B via endpoint — no dimensions -> 1024; dimensions=512 -> HTTP 400 "…"` and `wire: voyageai/voyage-4-nano via vLLM App — no dimensions -> 2048; dimensions=1024 -> HTTP 400 "…"`.
2. Knows, per route, the native width and whether `dimensions` is honoured — and why ADR-009 §3 truncates client-side.
3. Also finds `cos(voyage-4 API @1024, nano @1024)` for three texts.

### Story: Operator with an `HF_TOKEN` in `.env` checks it never leaks
1. Every deploy of this task was captured to a scratchpad file; the two App containers logged `HF_TOKEN set in container: True`.
2. The leak check prints `leak-check exit=0`; the Log contains no token-shaped string.

### Story: The workspace owner's own Modal things survive our e2e
1. Before the run the workspace holds four hand-made endpoints and two stopped accidental apps; the baseline records them all.
2. Four cycles run under `tree-` names; a second deploy of a live name is refused with exit 3.
3. The Log ends with `baseline: <n> apps, <m> endpoints — all unchanged`.

### Story: The next engineer reads why a threshold moved
1. Opens `configs/default.yaml`, sees `# re-pinned on voyage-4, 2026-09 — tasks/141 Log`.
2. The Log holds the two pairs and their scores — no value was moved by feel.

---

Blocked by: #134, #135, #136, #137, #138, #139, #140, #142, #143, #144, #145, #146, #147, #148, #149, #150, #151, #152

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

### [PA] 2026-09-19 18:44 — Re-grooming (HF token)

**What changed and why**
- The human brought the optional `HF_TOKEN` into the feature. Both seeds are public, so a gated download cannot be proven without adding a model; the cheapest HONEST check rides on rounds that already exist: 4b is a custom-weights create, so the driver forwards the token whenever one is set -> assert the logged argv shows `--custom-hf-token ***` and the secret occurs in no captured output; 4c's container logs `HF_TOKEN set in container: True`, proving the locally built Modal Secret survives the in-container re-import. No extra deployment, no extra GPU time.
- Whether the human has a token is detected value-free (the redacted flag is or is not in the log). `.env` is never read; the leak check prints an exit code only; the Log must stay free of token-shaped strings (a grep criterion enforces it).
- No token -> the task still passes, records `NOT PROVEN LIVE`, and one conditional [HUMAN] criterion lets the owner choose between a 1-cold-start re-run and the unit evidence.
- "Gated models / `--custom-hf-token`" left Out of scope; what remains out is deploying a really gated model.

**Dependencies**
- Unchanged (#139 and #142 now also provide the token pieces).

**User stories**
- 6 stories: the previous 5 + token never leaks.

Ready for implementation.

### [PA] 2026-09-19 22:11 — Re-grooming (voyage-4-nano is natively 2048-d)

**What changed and why**
- FACT CORRECTION (Tester, #138 QA): voyage-4-nano serves 2048-d natively; 1024 is a Matryoshka truncation. Part 1 (the Voyage API `voyage-4` at 1024-d) is UNCHANGED — 1024 is the hosted API's default and the mongot index width.
- Expected smoke output is now per model: Qwen3 `3 embeddings, 1024 dims`; voyage-4-nano `3 embeddings, 2048 dims` + `truncated 2048 -> 1024 dims client-side` + `sanity@1024`.
- NEW wire-width probe in every round (with and without `dimensions`), numbers in the Log. Without `dimensions` it GATES the catalog's `native_dimensions` on the known-good paths — the catalog was wrong once from reading `hidden_size`; this is the live measurement the Tester asked for. With `dimensions` it is evidence only: the client never sends it (ADR-009 §3), so a 400 is a complete answer.
- The vector-equivalence check compares 1024-d with 1024-d (both clients built with `dimensions=1024`). 4b's likeliest outcome is now named: the base Qwen3 recipe has no projection head, so the endpoint answers 1024-d where the catalog says 2048 and the smoke test fails on length — a complete answer that keeps `serving: vllm`.
- NEW cheap shared-space check: `voyage-4` API @1024 vs nano truncated to 1024, three same-text cosines + one cross-text contrast, recorded without a gate (Voyage publishes no number and the memory never mixes the two models).

**Dependencies**
- Unchanged.

**User stories**
- 7 stories: the previous 6 + wire widths and the shared-space number on record.

Ready for implementation.

### [PA] 2026-09-20 11:20 — Re-grooming (name collision incident: `tree-` names, baseline, separate database) — drafted, never applied; merged into the 13:40 entry below

**What changed and why**
- INCIDENT (2026-09-20, #142's QA): two real `modal deploy` calls ran by accident. One put our SGLang script on app `ep-qwen3-embedding-0-6b` — the app of a Dedicated Endpoint the human made by hand on 2026-08-24 — and overwrote it (no rollback on the plan; restored by hand). This task AS WRITTEN would have done the same on purpose: 4a created an endpoint named `qwen3-embedding-0-6b` and then STOPPED it. #143 moves every name we create to `tree-<model>` / `ep-tree-<model>` and adds the guard + dry run; this task now depends on it (order 142 -> 143 -> 141).
- Recorded state of the workspace: `ep-voyage-4-nano` (v1, from the accidental vLLM deploy) exists as a STOPPED app and is left alone; the hand-made `qwen3-embedding-0-6b` endpoint is the human's and receives no request from this task.
- NEW 4.0 baseline + 4e comparison: every pre-existing app/endpoint row must be unchanged at the end (ids, states, app history). NEW hard rules: only `tree-` names, never `FORCE=yes`, a guard refusal ends the run. 4a now also proves the guard live (second deploy -> exit 3) instead of probing Modal's duplicate-`create` behaviour.
- 4a creates `tree-qwen3-embedding-0-6b` as OUR Dedicated Endpoint; all expected names, ACs and stories moved to the prefix (`ep-tree-voyage-4-nano`, `ep-tree-qwen3-embedding-0-6b`).
- Part 1 runs in a SEPARATE local database `tree_e2e_141` (knob `MONGO_INITDB_DATABASE`, passed as a make command-line variable — verified to beat `include .env`; a shell-prefix env var does not) on a fresh 4-ADR corpus; the human's 2423-row local memory is not reset (orchestrator default; the human had not answered). The re-pin is taken on that corpus and says so.
- The e2e skill gains the dry-run / PATH-shim sentence (given verbatim in step 5).
- Three [HUMAN] lines: endpoint restored; post-merge reset of the real memory + the fate of `ep-voyage-4-nano`; nothing of ours billing.

**Dependencies**
- All of #134-#140, #142, and #143 (names, guard, dry run).

**User stories**
- 8 stories: the previous 7 + the workspace owner's own Modal things survive.

Ready for implementation.

### [PA] 2026-09-20 13:40 — Re-grooming (third plan edit: auto-routing, LLMs, warm-up) — THIS is the version that ships

**What changed and why**
- The human removed the Serving path from configuration: the driver asks Modal first and otherwise deploys the App for the model's kind (vLLM = embeddings, SGLang = LLMs). Part 2 is therefore no longer "walk three paths with `SERVING=`" but FOUR AUTO-ROUTED cycles, exactly the ones the human selected: (i) `Qwen/Qwen3-Embedding-0.6B` -> endpoint, (ii) `voyageai/voyage-4-nano` -> vLLM App, (iii) `LiquidAI/LFM2.5-350M` -> SGLang App, (iv) `Qwen/Qwen3.5-0.8B` -> endpoint (picked over `google/gemma-3-1b-it`: ungated, Apache-2.0). No `SERVING=` anywhere; the `Routing …` line is the evidence.
- GONE: "voyage-4-nano as a Dedicated endpoint with custom weights" as a step (it is now the router's step-1 refusal, recorded in 4b), the cosine >= 0.99 endpoint-vs-script equivalence rule and the seed flip (there is no `serving:` to flip), and the SGLang EMBEDDING round (that mode was deleted in #147).
- NOT selected by the human, hence unit-tested only and said so in Out of scope: the fine-tune -> custom-weights endpoint cycle — which also means the endpoint half of the `HF_TOKEN` path (`--custom-hf-token ***`) is no longer exercised live; the App half still is (`HF_TOKEN set in container`, leak check over every captured file).
- NEW (LLMs, #147/#148): each LLM cycle runs the chat smoke test AND a strict-JSON-schema completion through the `ModalLLM` client (gated), plus a JSON-mode call (recorded, not gated — a 350M or a thinking model may not comply; that text feeds ADR-009).
- NEW (warm-up, #144): the cold start of every cycle is read off the poller's own `Warm: … after Ns` line; ONE required cold-again proof on the vLLM App (idle 420 s GPU-less, then one call -> one re-warm + one retry), with an honest "not reproduced" exit.
- KEPT from the 11:20 draft, nothing dropped: `tree-` names only, never `FORCE=yes`, the 4.0 BASELINE and the 4e unchanged-assertion, the guard's live proof (second deploy -> exit 3), Part 1 in the separate local database `tree_e2e_141` passed as a make command-line variable, the wire-width probe (now on the two embedding cycles), the shared-space check, the [HUMAN] lines (the second one now also names `ep-qwen3-embedding-4b`), the cost note (now explicit, with a 30-minute per-cycle cap), the verbatim dry-run sentence for the e2e skill (target names updated).
- The list of never-touched infrastructure now names all four of the human's endpoints and both stopped accidental apps.

**Dependencies**
- All of #134-#140, #142, and the rework #143-#150.

**User stories**
- 10 stories: migration + pin, embedding without picking a path, Modal refuses voyage, custom LLM returns JSON on both routes, the test waits out a cold start, cold again mid-session, wire widths, token never leaks, the owner's things survive, auditable threshold change.

Ready for implementation.

### [PA] 2026-09-20 19:30 — Re-grooming (two tasks inserted before this one; LLM cache caveat)

- `Depends on` / `Blocked by` gain **#151** (LLM identity in the `llm-extract-entities` / `summarise-cluster` cache key — found by #148, commit 22734df) and **#152** (pre-warm leak + provider-gated seams, two deploy-script static guards, `MODAL_SERVER_NAME` — from #147's and #149's QA, commit d3a9fe2); those follow-ups had been "routed to #150", which stays its three embedding-text items. New order: … 150 -> 151 -> 152 -> 141. Added one Scope paragraph (LLM cache caveat) and one AC ("Modal LLM really called"): with #151 the replay is impossible by construction, and this task still proves from the `ModalLLM ready:` line + the `modal` usage span (`total_cost=0`) that the Modal server answered. Nothing else in this task changed.
