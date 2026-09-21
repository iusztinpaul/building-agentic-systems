---
id: 141-voyage-4-and-modal-e2e-threshold-repin
status: in-progress
feature: voyage-4-and-modal-embedding-catalog
---

# Live e2e: reset -> index -> query on `voyage-4` with a threshold re-pin (in a separate local database); then four auto-routed Modal cycles — embedding -> endpoint, embedding -> vLLM App, LLM -> SGLang App, LLM -> endpoint — under our own `tree-` names, touching nothing that existed before

Tags: `e2e`, `config`, `modal`, `llm`, `docs`
Depends on: #134, #135, #136, #137, #138, #139, #140, #142, #143, #144, #145, #146, #147, #148, #149, #150, #151, #152 (all done — round 1 ran on them); ROUND 2: #153, #154, #155, #156, #157
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

## ROUND 2 AMENDMENT (PA, 2026-09-21) — read this BEFORE the Scope above; where the two disagree, THIS wins

**ORDER: 153 -> 154 -> 155 -> 156 -> 157 -> 141 round 2.** Do not start before all five are merged into this branch
(`git log --oneline` must show them). The HARD SAFETY RULE and the HARD RULES above are unchanged and apply in full.

**Round 2 re-runs ONLY what round 1 left unproven.** PROVEN, evidence in the `[SWE] 2026-09-21 14:50` entry, NOT re-run:
the 4.0 baseline method, cycle **4a** in full (routing, smoke lines, wire probe `no dimensions -> 1024`, the guard
refusal with exit 3, **H1: TRUE**, stop by name), the four `Routing …` lines, the stderr finding, step 5's docs greps.
Do NOT deploy `Qwen/Qwen3-Embedding-0.6B` again. Still take a fresh 4.0 baseline first and finish with 4e: the expected
baseline is the human's 4 endpoints + 2 apps, plus possibly our two STOPPED apps `ep-tree-voyage-4-nano` /
`ep-tree-lfm2-5-350m` from round 1 (stopped = fine; a LIVE `tree-*` / `ep-tree-*` thing = stop and ask).

**R2-Part 2 — three cycles, in this order: 4b, 4c, 4d.** Each exactly as written above, plus:
- **Every `-test` is started IMMEDIATELY after its `deploy` returns** — no hand-polling of `modal endpoint list`. For 4d
  that exercises #157 live: record the `Endpoint tree-qwen3-5-0-8b is provisioning — …` deploy line, the
  `Provisioning: … — Ns/1800s` series and the `Live: … after <N>s` line. For 4b/4c record that NO `Provisioning` line
  appears. The `Warm: … after <N>s` line that follows is the cycle's cold-start number (three are owed: 4b, 4c, 4d; 4a's
  `after 0s` is on record with its reason).
- **4b (proves #153 + #154 live):** the container's first log line is `HF_TOKEN set in container: …` (not
  `JSONDecodeError`); the image build shows the new `ENV` steps (`HF_HOME`, `HF_HUB_CACHE`, the base64
  `EMBEDDING_DEPLOY_SPEC`) — so layers are rebuilt from that step; say whether the `vllm==0.26.0` layer was rebuilt or
  cached, and either way the ENGINE starting is now the proof of the pin + CUDA 13.0.2 +
  `VoyageQwen3BidirectionalEmbedModel`. Then everything 4b lists: smoke lines, wire probe (`no dimensions -> 2048`),
  the shared-space check, the cold-again proof (still required once, here), and — new, read-only, AFTER the first warm —
  `uv --directory apps/memory run modal volume ls huggingface-cache hub` showing `models--voyageai--voyage-4-nano`
  (weights are ON the Volume). Stop.
- **4c (proves #154 live):** no `cannot mount volume on non-empty path`; then everything 4c lists (six chat smoke lines
  incl. the new `chat knobs: max_tokens=4096 chat_template_kwargs={}` line, the `ModalLLM` strict-schema dict, the
  JSON-mode result recorded-not-gated, the Opik `modal` span, the SGLang tag that ran, `4c-app.log` with the container
  boolean). If SGLang cannot serve LFM2.5, #147's LOUD replacement procedure still applies. Stop.
- **4d (proves #155 + #156 live) — the LADDER, all inside ONE live cycle (the knobs are request fields: edit YAML,
  re-run `-test`, NO redeploy):**
  1. As seeded (`chat_template_kwargs: {enable_thinking: false}`, `max_tokens: 4096`): expect
     `strict JSON schema honoured: …` and the `ModalLLM` dict. Done.
  2. If the smoke test fails with `… no content (finish_reason=length, reasoning_content: N chars)`: the managed recipe
     ignored the kwarg. Set the seed to `chat_template_kwargs: {}` + `max_tokens: 8192` (thinking on, budget large
     enough for reasoning + JSON; the recipe's reasoning parser keeps the answer in `content`) and re-run `-test`.
  3. If that fails too: stop the endpoint, write `PA: ADR-009 §10 seed Qwen/Qwen3.5-0.8B cannot answer under strict
     JSON on a managed endpoint — <both messages verbatim>`, and run the cycle ONCE more with the fallback seed
     **`google/gemma-3-1b-it`** (non-thinking, in Modal's 44-id list; Modal serves its own snapshot on the endpoint
     route, so no `HF_TOKEN` of ours should be needed — if Modal demands one or a licence, STOP: that is a `[HUMAN]`
     decision, not a workaround). Every other small id in the list is a Qwen3.5 thinking sibling, so there is no
     second fallback. A seed change goes in `configs/default.yaml` + `test_seed_entries` + `frozen_config.yaml` in this
     task's commit with a `PA:` line (glossary "Modal catalog" names the seeds).
  Whichever rung held, record it in ONE line: `4d knobs that answered: <…>`. No call may exceed 300 s — a
  `timed out after 300s` line is a complete, recordable answer, not a hang to sit through.
  Also record, as a FACT for ADR-009 Consequences, what `modal endpoint create --name tree-qwen3-5-0-8b` answers now that
  a STOPPED endpoint of that name exists from round 1: success (expected) -> one line; anything else -> the router aborts
  as verdict `other`; paste it and STOP 4d (do not rename, do not `FORCE`).
- **Accepted as unrecordable (PA):** the GPU of a managed endpoint (no CLI column on modal 1.5.5) — write
  `GPU: Modal's choice, dashboard-only`; and `modal app list` never shows an endpoint's `ep-*` app — 4a's
  `modal app history ep-tree-qwen3-embedding-0-6b` evidence replaces that clause of the 4a criterion.
- **HF token line:** re-state it after 4b/4c now that the container boolean is actually observed (`settings.hf_token`
  was empty in round 1, so expect `NOT PROVEN LIVE — … (container says False)` — this time with the container line
  pasted). Leak check over every round-2 captured file.

**R2-Part 1 — isolation WITHOUT touching the human's containers (SUPERSEDES step 0's `docker stop tree-prefect-worker`
sentence; never stop, restart or exec into `tree-prefect-worker`).** The compose worker serves the same deployment names
against database `tree` through the Prefect server on port 4200; two runners on ONE server race for every run. So Part 1
uses its OWN throwaway Prefect server — no code change, nothing shared:
1. Start it in the background from the worktree, state kept out of `~/.prefect`:
   `PREFECT_HOME=<scratchpad>/prefect-141 uv --directory apps/memory run prefect server start --host 127.0.0.1 --port 4201`
   and wait for `curl -s http://127.0.0.1:4201/api/health` -> `true`.
2. EVERY Part 1 `make` command carries THREE command-line variables (after the target — the form verified to beat
   `include .env`): `MONGO_INITDB_DATABASE=tree_e2e_141 USER_IDENTIFIER=e2e-141@example.com PREFECT_API_URL=http://127.0.0.1:4201/api`
   — first of all `make memory-serve-workflows …`, then signup, pipelines, reset, query, visualize.
3. PROVE it before the first ingest: `PREFECT_API_URL=http://127.0.0.1:4201/api uv --directory apps/memory run prefect deployment ls`
   lists our deployments on 4201; after the first ingest the document is in `tree_e2e_141.documents`, the flow run is
   listed on 4201, and NO new flow run appeared on 4200 in that window (`prefect flow-run ls` against 4200, read-only).
   The fresh server has no `voyage-embeddings` limit: `rate_limit` is non-strict by design ("fresh dev boxes"), and 4
   documents sit far below the API limit — note the warning, do not create the limit on 4200.
4. The `tree` counts: the compose worker keeps running, so the HUMAN may legitimately write to `tree` during Part 1.
   Record both counts before/after (round 1: 2539 embedded `memory` rows, 2922 `documents`); if they differ, show that
   no `tree.documents` row created in the window has one of the four `docs/adrs/00{2,6,7,8}_*.md` worktree paths as
   its source — THAT is the invariant.
5. Cleanup: stop the serve process and the 4201 server; delete `<scratchpad>/prefect-141`. `tree_e2e_141` already exists
   EMPTY from round 1 (7 collections, 0 documents) — reuse it. If `dropDatabase` is denied again, do not work around
   it: the `[HUMAN]` line below covers it.
6. If step 1 is denied by the sandbox or port 4201 is taken (try 4202), STOP Part 1 and write so — the fallback is the
   `[HUMAN]` precondition below, never a `docker` command of yours.

### Round 2 acceptance criteria (in addition to every unticked box above)

- [ ] `git log --oneline` shows #153-#157 BEFORE any round-2 evidence; no round-2 command names `Qwen/Qwen3-Embedding-0.6B` in a `deploy`.
- [ ] 4b: container log starts with `HF_TOKEN set in container: …`; `modal volume ls huggingface-cache hub` lists `models--voyageai--voyage-4-nano`; all of 4b's original lines (smoke, wire `-> 2048`, shared-space cosines, cold-again sequence or its honest "not reproduced").
- [ ] 4c: no `cannot mount volume on non-empty path` anywhere in `4c-app.log`; the six chat smoke lines + `chat knobs: …`; the `ModalLLM` dict with exactly `city` / `population`; the Opik `modal` span.
- [ ] 4d: the deploy line `… is provisioning — …`, >= 1 `Provisioning: tree-qwen3-5-0-8b …` line, `Live: … after <N>s`, the line `4d knobs that answered: …` (or rung 3's `PA:` line + the fallback seed's full cycle), the `ModalLLM` dict, the Opik `modal` span, and the one-line fact about re-creating a stopped endpoint's name. No call ran longer than 300 s.
- [ ] Part 1: every pasted Part 1 command carries all THREE variables; the 4201 isolation proof (deployments on 4201, run on 4201, none on 4200, document in `tree_e2e_141`); no `docker` command appears in the round-2 Log.
- [ ] Every row of round 1's "SWE must verify" table that reads **NOT VERIFIED** / **NOT MEASURED** / **NOT PROVEN LIVE** is re-answered with its source (vLLM 0.26.0 + CUDA 13.0.2, `VoyageQwen3BidirectionalEmbedModel`, sgl-kernel on the A10, LFM2 on SGLang, `json_object` on SGLang, voyage-4-nano's wire width + `dimensions` answer, container half of `Secret.from_dict`, engine subprocess inheriting `HF_TOKEN`).
- [ ] 4e again at the very end: `baseline: <n> apps, <m> endpoints — all unchanged`, no live `tree-*` / `ep-tree-*`.
- [ ] [HUMAN] Drops the empty database `tree_e2e_141` (agents are denied `dropDatabase`): `mongosh` -> `db.getSiblingDB("tree_e2e_141").dropDatabase()` — after round 2's Part 1, or now if Part 1 is abandoned.
- [ ] [HUMAN] ONLY if the throwaway Prefect server (R2-Part 1 step 1) could not be started: stops the compose worker (`docker stop tree-prefect-worker`), CONFIRMS in this Log, lets Part 1 run against port 4200, and restarts it after (`docker start tree-prefect-worker`). No agent runs either command.

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
- [x] 4.0: `## Log` has the baseline summary (every app and endpoint: name, id, state/status, latest version of each non-`tree` `ep-*` app) and the two `list --json` key sets, taken BEFORE the first deploy.
- [x] Routing, live: `## Log` has FOUR `Routing …` lines — 4a `→ Dedicated endpoint tree-qwen3-embedding-0-6b`, 4b `→ vLLM App`, 4c `→ SGLang App`, 4d `→ Dedicated endpoint tree-qwen3-5-0-8b` — and, for 4b and 4c, Modal's verbatim refusal containing `is not available for dedicated Endpoints` plus the stream it came on. No `SERVING=` and no `FORCE=yes` appears in any command of this Log.
- [x] Guard, live: the refusal line `Refusing to deploy Qwen/Qwen3-Embedding-0.6B …: 'tree-qwen3-embedding-0-6b' already exists on Modal as a Dedicated endpoint. …` with exit code 3.
- [x] 4a: the `tree-qwen3-embedding-0-6b` smoke lines (`health 200 after …s`, `served model id: …`, `3 embeddings, 1024 dims`, `sanity: cos(query, relevant)=… > cos(query, unrelated)=…`, `unauthenticated health -> 401`, `Smoke test passed`), the pasted `modal endpoint list --json`, `modal app list` showing `ep-tree-qwen3-embedding-0-6b`, and an explicit line `H1: TRUE` or `H1: FALSE -> fix (i)|(ii)` with the diff in the same commit and `make memory-tests` green.
- [ ] 4b: the vLLM App smoke lines (`3 embeddings, 2048 dims`, `truncated 2048 -> 1024 dims client-side`, `sanity@1024: …`), the vLLM version actually built, and the stop's `Not a Dedicated endpoint (…) — stopping the App instead.` line.
- [ ] Wire width: TWO `wire: …` lines (4a, 4b) — `no dimensions -> 1024` (Qwen3) and `-> 2048` (voyage-4-nano), each with the length or HTTP status + message for the `dimensions` call — or the catalog fix + `PA:` line in the same commit.
- [ ] Shared space: the 3 same-text cosines `cos(voyage-4 API @1024, nano @1024)` and the 1 cross-text contrast, labelled recorded-not-gated, and a `PA:` line only if same-text is not clearly above cross-text.
- [ ] Cold start: FOUR `Warm: … answered HTTP 200 after <N>s` lines (one per cycle, from the `-test` runs), each preceded by >= 1 `Still cold (HTTP 5xx)` line unless the server was already warm (say so).
- [ ] Cold again: either the sequence `Cold again: ep-tree-voyage-4-nano answered HTTP 503 — re-warming once` -> `Warm: …` -> `Warm again: …` with a 1024-d vector returned and exactly ONE `Cold again` line, or `cold-again: not reproduced — container still warm after 420 s`.
- [ ] 4c + 4d: for EACH LLM cycle the six chat smoke lines (incl. `strict JSON schema honoured: city=… population=…` and `unauthenticated health -> 401`), the dict returned by `ModalLLM.generate_json(…, schema=…)` with exactly the keys `city` / `population`, the JSON-mode result or its `ExtractionError` text (recorded, not gated), the `ModalLLM ready: app=ep-tree-… served_model=…` line, and for 4c the SGLang image tag that actually ran; for 4d the GPU Modal picked.
- [ ] Modal LLM really called (not a cache replay): for EACH of 4c and 4d, `## Log` pastes the `ModalLLM ready: app=ep-tree-… served_model=…` line of the run AND the Opik usage span of the same call showing `provider: modal`, `total_cost=0` and non-zero token counts, with one sentence stating whether the call went through Prefect (then also the `llm-extract-entities` task state `Completed`, never `Cached`) or was the direct client call (no cache in the path). `git log --oneline` of the branch shows #151's commit BEFORE any 4c/4d evidence.
- [x] HF token: exactly one of `HF token path (App): PROVEN (HF_TOKEN set in container: True, leak-check exit=0)` or `HF token path (App): NOT PROVEN LIVE — HF_TOKEN not set (container says False); unit evidence only`, plus the sentence that the endpoint `--custom-hf-token` half is unit-tested only. `grep -c "hf_[A-Za-z0-9]\{20,\}" tasks/141-voyage-4-and-modal-e2e-threshold-repin.md` -> 0.
- [x] 4e: `modal endpoint list --json` AND `modal app list --json` pasted in `## Log` show no `tree-*` endpoint and no live `ep-tree-*` app, and the line `baseline: <n> apps, <m> endpoints — all unchanged` (same ids, same states, no new version in any baseline `ep-*` app's history). Every `modal … stop` argv in this Log names a `tree-` / `ep-tree-` name.
- [x] Cost: per cycle the GPU and deploy-to-stop minutes; no cycle above 30 minutes (or the reason).
- [x] Every "SWE must verify" item left open by #138-#140, #142 and #143-#148 is answered in `## Log` with its source: GPU strings, the vLLM pin + CUDA base actually working, `VoyageQwen3BidirectionalEmbedModel` on that pin, the SGLang docker tag + `uv_pip_install` on it, LFM2 on SGLang, `speculative_model_path` optional, `json_object` on SGLang, async `get_url`, `modal endpoint stop` by name, `--name` -> app-name rule (H1), the two `list --json` key sets, whether `endpoint create` blocks, refusal on stdout vs stderr, Qwen3 prompt bytes, the wire width of each embedding seed and what each route answers to `dimensions`, `secrets=` on `@app.server`, local-vs-container `Secret.from_dict`, the engine subprocess inheriting `HF_TOKEN`.
- [x] `grep -c "memory-reset-embeddings" .agents/skills/run-pipelines-e2e/SKILL.md` >= 1, `grep -c "memory-deploy-model" …` >= 1, `grep -c "Routing" …` >= 1, `grep -c "HF_TOKEN" …` >= 1, `grep -c "DRY_RUN=yes" …` >= 1, `grep -c "TREE_MODAL__WARMUP_DEADLINE_S" …` >= 1, and `grep -c "deploy-embedding-model" …` == 0.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green after any threshold, seed, pin or H1 change.
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

Blocked by: #153, #154, #155, #156, #157 (round 2; everything up to #152 is done)

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

### [SWE] 2026-09-21 14:50 — Implementation (live e2e, PARTIAL — two blockers, two real bugs found)

**Verdict up front.** Part 2 ran: all FOUR cycles were deployed, routed, probed and STOPPED under `tree-`
names only; the human's four endpoints and two apps are untouched. Two of the four cycles FAILED on real,
newly-discovered defects in the App path (not on the models) and one gated smoke test failed on a live
model behaviour. **Part 1 never started** — the sandbox refused the `docker stop tree-prefect-worker` the
task requires, and without it a dispatched run is a coin flip between this worktree and the human's real
`tree` database. NOTHING was written to any database. No `FORCE=yes`, no `SERVING=`, no command against a
name without the `tree-` prefix.

**Files modified**
- `.agents/skills/run-pipelines-e2e/SKILL.md` — new step 4 ("After changing the embedding model") and step 5
  ("Models on Modal"), ending with the verbatim `DRY_RUN=yes` sentence; cleanup renumbered to 6.
- `apps/memory/README.md` — one paragraph the live run showed was missing: an endpoint is `provisioning`
  after `create` returns, and a smoke test started before it is `live` fails in `resolve_server_url`, not on
  a 503 the poller would sit out.

No code, config, threshold, catalog seed or test was changed: nothing this run measured justified a re-pin
(Part 1 did not run) and both catalog facts it COULD check were already right.

**Tests**
- Unit: `make memory-tests` -> **3917 passed** in 56.85s, 0 failing.
- `make memory-format-fix` / `lint-fix` / `format-check` / `lint-check` / `make pre-commit` — all exit 0
  (`313 files already formatted`, `All checks passed!`, every hook Passed).
- Integration: N/A — neither app has an integration suite (deliberate, see AGENTS.md).

---

#### BLOCKER 1 (human) — Part 1 could not be run: `docker stop tree-prefect-worker` was denied

`docker-compose.yml:107-127` shows `tree-prefect-worker` runs `uv run python -m tree.orchestrator` — the SAME
`serve()` as `make memory-serve-workflows` — with `MONGO_INITDB_DATABASE=tree` and the MAIN checkout baked
into its image. Two serving processes register the same deployment names and BOTH poll for that deployment's
runs, so a run dispatched by `make memory-run-pipeline` is a coin flip between my worktree (`tree_e2e_141`)
and the human's real memory (`tree`, 2539 embedded rows). The task's step 0 therefore requires stopping it;
the sandbox policy refused (`docker stop tree-prefect-worker` -> denied, "Interfere With Workloads"), and I
did not work around it. **Part 1 was not started and no database was written.**

Needed from the human: permission for `docker stop tree-prefect-worker` / `docker start tree-prefect-worker`,
then a re-run of Part 1.

**Isolation was nevertheless PROVEN before anything else** (the differential probe, read-only):

```
$ make memory-whoami
6a8ea9579a7aeb13175955c8	paul@example.com	paul@example.com

$ make memory-whoami MONGO_INITDB_DATABASE=tree_e2e_141
Error: No current user is set. Run `signup` or `set-current` first.   (exit 1)
```

Different answers => the make COMMAND-LINE variable does reach pydantic-settings and switch the database,
exactly as the PA verified at grooming.

**The real database, read-only, BEFORE and AFTER the whole task:**

| | before | after |
|---|---|---|
| `tree.memory` rows with a non-empty `embedding` | **2539** | **2539** |
| `tree.documents` | **2922** | **2922** |

UNCHANGED. Note the task expected 2423 — the human's memory has grown since grooming (2026-09-20); 2539 is
today's number and the invariant that matters (before == after) holds.

`tree_e2e_141` **was created and is LEFT IN PLACE, empty**: the read-only `whoami` probe above connects and
bootstraps indexes, which creates the 7 collections `knowledge_graph_meta_state, users, memory,
memory_clusters, extraction_rejections, extraction_dropped_fields, documents` — **0 documents in each,
`dataSize` 0 bytes**. `db.getSiblingDB("tree_e2e_141").dropDatabase()` was DENIED by the sandbox
("Cloud Storage Mass Delete"), so the human should drop it (it costs nothing where it is).

#### BLOCKER 2 (PA) — the deploy spec cannot cross into a container intact. ADR-009 §3's transport is broken for any quoted value.

Found by 4b, diagnosed byte-exactly, NOT fixed (choosing a transport is an architectural decision and
ADR-009 §3 names the current one: "the resolved entry crosses into the container as ONE JSON env var baked
into the image").

The container crash-loops at module import:

```
File "/root/modal_vllm_embedding.py", line 98, in <module>
  SPEC = json.loads(os.environ["EMBEDDING_DEPLOY_SPEC"])
json.decoder.JSONDecodeError: Expecting ',' delimiter: line 1 column 331 (char 330)
Runner failed with exception: JSONDecodeError("Expecting ',' delimiter: line 1 column 331 (char 330)")
```

`deploy/modal_vllm_embedding.py:113` bakes the spec with `.env({… DEPLOY_SPEC_ENV: json.dumps(SPEC)})`.
`modal/_image.py:2821` (modal 1.5.5) renders that as a **Dockerfile** directive
`ENV {key}={shlex.quote(val)}`. `shlex.quote` is POSIX-SHELL quoting, but a Dockerfile `ENV` is parsed by
Docker, whose escape character is `\` — so every `\"` inside the value is UNESCAPED to `"` and the JSON is
destroyed. Proof, locally:

```
json.dumps(build_deploy_spec("voyageai/voyage-4-nano").model_dump())      -> parses OK
  the same string with \" -> "   -> Expecting ',' delimiter: line 1 column 331 (char 330)   <- the container's error
  the same string with \" -> \\" -> Expecting ',' delimiter: line 1 column 333 (char 332)
```

Only the stripped variant matches, character for character.

**Scope of the bug:** any entry whose `extra_server_args` carries a value containing a quote — i.e. exactly
the compact JSON ADR-009 §3 prescribes: `--pooler-config '{"pooling_type":"MEAN"}'` and
`--hf-overrides '{"architectures":["VoyageQwen3BidirectionalEmbedModel"]}'`. The CONTROL that isolates it:
`LiquidAI/LFM2.5-350M`'s spec has no quoted value, is byte-identical after the same unescaping, and its
container parsed the spec fine (it died later, of an unrelated cause — blocker 3).

**PA: ADR-009 §3 needs a decision on the deploy-spec transport.** I did not pick. The options:
- **A. base64 the spec** into the env var, decode in the container. Immune to every quoting layer; changes
  ADR-009 §3's sentence from "ONE JSON env var" to "one base64 env var" and makes the layer un-greppable.
- **B. pre-escape the backslashes** before `.env()` so Docker's unescaping restores the JSON. Keeps the ADR
  sentence literally true; depends on a Dockerfile unescaping layer we would be inferring, not reading.
- **C. a non-env transport** (e.g. a file added to the image). Most explicit, largest change.

**This defeats three "#141 verifies live" items by construction** — vLLM 0.26.0 on the CUDA 13.0.2 base
(#146 B.3, #142), `VoyageQwen3BidirectionalEmbedModel` on that pin, and the container half of
`Secret.from_dict` (#142 (b)) — none can be verified until the transport is fixed, and no amount of retrying
4b changes that.

#### BLOCKER 3 (PA) — the SGLang App cannot mount the shared weights Volume on its own image

Found by 4c, also NOT fixed (the mount path is a design choice; ADR-009 fixes the Volume, not the path).

```
Runner failed with exception: cannot mount volume on non-empty path: "/root/.cache/huggingface"
Function modal_sglang_llm.Server is crash-looping: containers are repeatedly failing to start.
```

Both App scripts mount `huggingface-cache` at `/root/.cache/huggingface`
(`deploy/modal_sglang_llm.py:164-166`, `deploy/modal_vllm_embedding.py:134-136`). The official
`lmsysorg/sglang:v0.5.18` image already has content there, so Modal refuses the mount; the
`nvidia/cuda:13.0.2-devel-ubuntu22.04` base does not, which is why only the SGLang script hits it.
The IMAGE itself is fine — `lmsysorg/sglang:v0.5.18` + `uv_pip_install(autoinference-utils==0.2.6)` built
and deployed (`Built image im-jtjusQsLZ25CsaavQuZCeo in 5.93s`, `✓ App deployed in 8.853s!`); it is the
CONTAINER that never starts.

**PA: ADR-009 §3 (App scripts) needs the shared-cache mount path decided** — e.g. mount at `/cache/huggingface`
and set `HF_HOME` to it (in the SGLang script only, or both for symmetry). Consequence: LFM2 on SGLang, the
in-container strict-JSON warm-up, `json_object` on SGLang and the SGLang half of the token path stay
UNVERIFIED until then.

---

### Part 2 — the four Modal cycles

#### 4.0 Baseline (read-only, BEFORE the first deploy)

`modal endpoint list --json` key set: `name, endpoint_id, status, created_at, created_by`
`modal app list --json` key set: `app_id, description, state, tasks, created_at, stopped_at`
(both confirm #143's claim, read off the live CLI on modal 1.5.5)

Endpoints — 4, all `status: live`, all `created_by: p-b-iusztin`:

| name | endpoint_id | status | created_at |
|---|---|---|---|
| qwen3-embedding-8b | ep-UDN0woLFq8dwNwMoHqK3A1 | live | 2026-09-20 11:05:47+03:00 |
| qwen3-embedding-0-6b | ep-AFm6anFYXobWj3ba0MxKOw | live | 2026-09-20 10:59:56+03:00 |
| gpt-oss-120b | ep-AZAaUPQk88Ko2i9xx5olsc | live | 2026-09-15 20:01:50+03:00 |
| qwen3-6-35b-a3b-fp8 | ep-nCsyPubmp2JsVUMkfPQR6E | live | 2026-09-14 13:22:55+03:00 |

Apps — 2, both `state: deployed`, `tasks: 0`, `stopped_at: null`:

| app_id | description | state | created_at |
|---|---|---|---|
| ap-opyKC4h4Z7RVMV3KaFbi8M | decode-sandbox-prod | deployed | 2026-09-09 15:09:03+03:00 |
| ap-v7ZtWBI4HIgrCiBF3IXZpI | decode-sandbox-local | deployed | 2026-09-11 00:20:41+03:00 |

No baseline app name starts with `ep-`, so no `modal app history` of a non-`tree` `ep-*` app was needed. No
`tree-*` endpoint and no `ep-tree-*` app existed. **FINDING:** `modal app list` (table AND `--json`) lists
NEITHER endpoint-backed `ep-*` apps NOR stopped apps — the human's four endpoints have apps that never appear
there, and neither do the two stopped accidental apps `ep-voyage-4-nano` / `ep-qwen3-embedding-4b` (so I can
say nothing about them; they are invisible to this CLI surface). That is precisely why the existence guard
reads the ENDPOINT list first; its app-list leg can never see a live endpoint's app.

#### The four `Routing …` lines (no `SERVING=`, no `FORCE=yes` anywhere in this run)

```
Routing Qwen/Qwen3-Embedding-0.6B: Modal accepted it → Dedicated endpoint tree-qwen3-embedding-0-6b
Routing voyageai/voyage-4-nano: not in Modal's endpoint catalog, no catalog base → vLLM App
Routing LiquidAI/LFM2.5-350M: not in Modal's endpoint catalog, no catalog base → SGLang App
Routing Qwen/Qwen3.5-0.8B: Modal accepted it → Dedicated endpoint tree-qwen3-5-0-8b
```

Modal's refusal, verbatim (first line of the Rich `╭─ Error ─╮` box), for the two App cycles:

```
'voyageai/voyage-4-nano' is not available for dedicated Endpoints.
'LiquidAI/LFM2.5-350M' is not available for dedicated Endpoints.
```

followed by `Models available for dedicated Endpoints:` and 44 bullets (which DID include
`Qwen/Qwen3-Embedding-0.6B`, `Qwen/Qwen3-Embedding-8B`, `Qwen/Qwen3.5-0.8B` and `google/gemma-3-1b-it`).
Nothing was created by either refusal. **Stream: stderr** — `modal/__main__.py:main` catches the exception
and calls `OutputManager.get().print_error(content)`, which at `modal/_output/rich.py:340-346` prints the
`Panel(title="Error")` on `self._stderr_console`, built at `:264` as `_make_console(stderr=True)`. This is
why `_log_modal_output` must join BOTH captured streams — it does. #145's two substrings match today's live
text unchanged.

Every cycle was DRY-RUN first and the name confirmed before the real command:
`tree-qwen3-embedding-0-6b`, `tree-voyage-4-nano`, `tree-lfm2-5-350m`, `tree-qwen3-5-0-8b` — and nothing else
was ever created.

#### 4a — `Qwen/Qwen3-Embedding-0.6B` -> Dedicated endpoint — **PASS**

`modal: ✓ Endpoint 'tree-qwen3-embedding-0-6b' (ep-cdhIRjCpSKKpCyOpUxMNg7) was created and started provisioning.`
`--routing-region eu-west` ACCEPTED. **`modal endpoint create` does NOT block**: it returned in 4 s with
`status: provisioning`; `provisioning -> live` took **2m15s** (bounded read-only poll on
`modal endpoint list --json`, 6 polls).

Smoke test:

```
Warming https://p-b-iusztin--ep-tree-qwen3-embedding-0-6b-server.eu-west.modal.direct/health — polling for up to 600s
Warm: https://…/health answered HTTP 200 after 0s
health 200 after 0.5s
served model id: Qwen/Qwen3-Embedding-0.6B
3 embeddings, 1024 dims
sanity: cos(query, relevant)=0.78 > cos(query, unrelated)=0.09
unauthenticated health -> 401
Smoke test passed
```

The two listings WHILE it was up.

`modal endpoint list --json` — ours added, the human's four untouched and still `live`:

```
tree-qwen3-embedding-0-6b  ep-cdhIRjCpSKKpCyOpUxMNg7  provisioning
qwen3-embedding-8b         ep-UDN0woLFq8dwNwMoHqK3A1  live
qwen3-embedding-0-6b       ep-AFm6anFYXobWj3ba0MxKOw  live
gpt-oss-120b               ep-AZAaUPQk88Ko2i9xx5olsc  live
qwen3-6-35b-a3b-fp8        ep-nCsyPubmp2JsVUMkfPQR6E  live
```

`modal app list` (both the table and `--json`) — only the two baseline apps:

```
ap-opyKC4h4Z7RVMV3KaFbi8M  decode-sandbox-prod   deployed
ap-v7ZtWBI4HIgrCiBF3IXZpI  decode-sandbox-local  deployed
```

`modal app list` does NOT show `ep-tree-qwen3-embedding-0-6b`, and this AC's clause expecting it rests on a
premise this run disproved: the command lists no endpoint-backed `ep-*` app at all — not ours, not any of
the human's four (see 4.0). The app's existence under the derived name is proven instead by
`modal app history ep-tree-qwen3-embedding-0-6b --json`, which RESOLVES that name, plus the URL
`resolve_server_url` returned for it. That is strictly stronger evidence than an `app list` row would have
been.

Wire probe (two raw `POST /v1/embeddings`, one text, nothing committed):

`wire: Qwen/Qwen3-Embedding-0.6B via endpoint — no dimensions -> 1024; dimensions=512 -> HTTP 400 "Model 'Qwen/Qwen3-Embedding-0.6B' does not support Matryoshka embeddings; dimensions must be unset (received dimensions=512)."`

GATE PASSES: the catalog's `native_dimensions: 1024` is the measured wire width — **no catalog fix needed**.
The 400 is the complete answer ADR-009 §3 predicted (Modal's 0.6B recipe sets no `is_matryoshka`), and the
client never sends `dimensions`.

Guard, live — a SECOND deploy while it was up:

```
Refusing to deploy Qwen/Qwen3-Embedding-0.6B via endpoint: 'tree-qwen3-embedding-0-6b' already exists on Modal as a Dedicated endpoint. Stop it first (make memory-deploy-model-stop MODEL=Qwen/Qwen3-Embedding-0.6B) or pass FORCE=yes to deploy over it.
```

`scripts/modal_model.py deploy` exit code **3** (via `make`: `make[1]: *** [deploy-model] Error 3`). It cost
one read-only `modal endpoint list --json` and no Modal mutation.

(The string `FORCE=yes` occurs in this task file only in the Scope/AC text and inside quoted Modal / guard
messages such as the refusal above — **no command of this run carried it**, and none carried `SERVING=`
either.)

**H1: TRUE.** The app IS `ep-tree-qwen3-embedding-0-6b` —
`modal app history ep-tree-qwen3-embedding-0-6b --json` -> `v1` 2026-09-21 13:56:03+03:00 and `v2` 13:56:10+03:00,
`deployed_by: p-b-iusztin`, client `1.5.1.dev13` (Modal itself deployed v2 seven seconds later, while
provisioning — no command of ours ran between 13:56:03 and 13:56:10) — and its server class is `Server`:
`resolve_server_url` = `modal.Server.from_name("ep-tree-qwen3-embedding-0-6b", "Server")` resolved
`https://p-b-iusztin--ep-tree-qwen3-embedding-0-6b-server.eu-west.modal.direct`. No fix (i) or (ii) was needed.

Stop: `modal endpoint stop -y tree-qwen3-embedding-0-6b` ->
`modal: ✓ Stopped Endpoint 'tree-qwen3-embedding-0-6b' in environment 'main' (ID: ep-cdhIRjCpSKKpCyOpUxMNg7).`
First try, exit 0 — **`modal endpoint stop` accepts the NAME**.

#### 4b — `voyageai/voyage-4-nano` -> vLLM App — **FAIL (blocker 2)**

Routing and refusal as above; `Running: modal deploy deploy/modal_vllm_embedding.py` in the SAME command.
Image: only `Step 0: FROM base`, `Step 1: ENV HF_XET_HIGH_PERFORMANCE=1`, `Step 2: ENV EMBEDDING_DEPLOY_SPEC=…`
were built (6.59 s) — the CUDA 13.0.2 + `vllm==0.26.0` + `autoinference-utils==0.2.6` layers came from Modal's
cache, so **this run did not rebuild them and cannot claim they work**. `✓ App deployed in 10.320s!`,
`✓ Created function Server.`, endpoint `https://p-b-iusztin--ep-tree-voyage-4-nano-server.eu-west.modal.direct`.

`-test` polled `Still cold (HTTP 503)` from 0s to 420s and never went warm; `modal app logs
ep-tree-voyage-4-nano` showed the container crash-looping (7 crashes in 100 s of log) with the
`JSONDecodeError` of blocker 2. I stopped it at 420 s rather than burn the full 600 s deadline on a crash loop.

NOT verified in 4b, therefore: the 2048-d wire width, the `dimensions` answer on this route, the
`3 embeddings, 2048 dims` / `truncated 2048 -> 1024 dims client-side` / `sanity@1024` smoke lines, the
shared-space cosines against the `voyage-4` API, the cold-again proof, and the `HF_TOKEN set in container:`
line (the crash is AT module import, before any container logging).

Stop, path-blind, worked exactly as designed:

```
Running: modal endpoint stop -y tree-voyage-4-nano
modal: Error: Endpoint 'tree-voyage-4-nano' not found in environment 'main'.
Not a Dedicated endpoint (Error: Endpoint 'tree-voyage-4-nano' not found in environment 'main'.) — stopping the App instead.
Running: modal app stop -y ep-tree-voyage-4-nano
```

#### 4c — `LiquidAI/LFM2.5-350M` -> SGLang App — **FAIL (blocker 3)**

Routing and refusal as above; `Running: modal deploy deploy/modal_sglang_llm.py`;
`Built image im-jtjusQsLZ25CsaavQuZCeo in 5.93s`; `✓ App deployed in 8.853s!`. The `LLM_DEPLOY_SPEC` parsed
fine in the container (no quoted value -> blocker 2 does not apply), which is what isolates blocker 2 to
quoted values. `-test` polled `Still cold (HTTP 503)` to 301 s while the container crash-looped on the
volume mount. Stopped with the same path-blind `Not a Dedicated endpoint (…) — stopping the App instead.`

The six chat smoke lines, the `ModalLLM` strict-schema and JSON-mode calls, and the SGLang image tag
*actually running* are therefore NOT on record for 4c.

#### 4d — `Qwen/Qwen3.5-0.8B` -> Dedicated endpoint — **deploy/route/stop PASS, chat smoke test FAIL (a live model finding)**

`modal: ✓ Endpoint 'tree-qwen3-5-0-8b' (ep-9kO3qTOKBybx76R5GBkmNB) was created and started provisioning.`
`provisioning -> live` took **9m25s** (28 polls) — four times the embedding endpoint's, which is the number
the README paragraph I added now records. **GPU: Modal's choice and NOT exposed by the CLI** —
`modal endpoint list --json` has no GPU column on modal 1.5.5, and a managed recipe's hardware is a
dashboard-only fact (ADR-009 already says so). NOT RECORDED, and not recordable without the dashboard.

```
Warm: https://p-b-iusztin--ep-tree-qwen3-5-0-8b-server.eu-west.modal.direct/health answered HTTP 200 after 2s
health 200 after 1.6s
served model id: Qwen/Qwen3.5-0.8B
the chat completion carried no content — the served model answered with an empty message
```

exit 1. The strict `city_facts` schema + `max_tokens=256` yielded an EMPTY `message.content`: consistent with
a thinking model spending its whole budget before emitting the JSON. This is the "what would justify
upgrading" evidence ADR-009 §10 asked for — per-entry `chat_template_kwargs` / `max_tokens` is the knob it
names, and this is the first live case for it.

The `ModalLLM` client check (ad-hoc, nothing committed) got as far as:

```
ModalLLM ready: app=ep-tree-qwen3-5-0-8b server=Server served_model=Qwen/Qwen3.5-0.8B url=https://p-b-iusztin--ep-tree-qwen3-5-0-8b-server.eu-west.modal.direct/v1
Retrying request to /chat/completions in 0.452048 seconds
```

and then HUNG for 13 minutes without returning — `ModalLLM.generate_json` sends no `max_tokens`, so the same
thinking loop runs unbounded to the context limit. I killed it at the 30-minute cycle cap (23m23s
deploy-to-stop) and stopped the endpoint. So: the `ModalLLM ready:` line IS on record (and it went through
NO Prefect, so no `llm-extract-entities` cache sat in its path — the completion could only have come from
the Modal server), but there is **no returned dict, no JSON-mode result and no Opik usage span**: the call
never completed. `git log --oneline` confirms #151 (`32a296d`, "LLM identity in the cache key of
llm-extract-entities and summarise-cluster") is on this branch BEFORE any of this evidence.

Stop: `modal: ✓ Stopped Endpoint 'tree-qwen3-5-0-8b' in environment 'main' (ID: ep-9kO3qTOKBybx76R5GBkmNB).`
First try, exit 0.

#### Cold start — only TWO `Warm:` lines exist, and here is the arithmetic

| cycle | `Warm:` line | preceded by `Still cold`? |
|---|---|---|
| 4a | `Warm: … answered HTTP 200 after 0s` | no — the endpoint was already warm because I waited for `live` before testing |
| 4b | none | 40+ `Still cold (HTTP 503) … 0s..420s`, never warm (crash loop) |
| 4c | none | 20+ `Still cold (HTTP 503) … 0s..301s`, never warm (crash loop) |
| 4d | `Warm: … answered HTTP 200 after 2s` | no — same reason as 4a |

The poller itself is proven working in all four (the `Still cold (HTTP 5xx) at <url> — Ns/600s` series in 4b
and 4c is exactly ADR-009 §11's contract, including the 5 s x 1.5 backoff capped at 15 s, visible in the
`0s, 6s, 13s, 25s, 40s, 55s, 70s, 86s, 101s, 116s, 131s…` stamps). What is NOT on record is four
`Warm:` lines, and no cold-again proof was possible (it lives on the vLLM App, which never served).

#### HF token

`HF token path (App): NOT PROVEN LIVE — HF_TOKEN not set (container says False); unit evidence only`

`settings.hf_token` is EMPTY — checked value-free before anything else (the task's own snippet with no file
arguments: exit 2 = no token configured). The leak check over every captured file
(`4a-deploy.log`, `4b-deploy.log`, `4b-app.log`, `4c-deploy.log`, `4c-app.log`, `4c-app2.log`,
`4d-deploy.log`) prints `leak-check exit=2` — no token configured, so nothing could leak. The check is
vacuous for the same reason on the files not listed (the `-test`, wire-probe and client captures): with no
token configured, no file can contain one. Strictly, the
container never reached the point of logging `HF_TOKEN set in container: …` either (blockers 2 and 3), so the
App half is doubly unproven. The ENDPOINT half of the token path (`--custom-hf-token ***` on a custom-weights
create) is **unit-tested only** — no selected cycle is a custom-weights create (#139 `TestHfToken`,
#145 `TestRouter` case 4). The conditional [HUMAN] criterion below therefore fires.

#### 4e — nothing of ours left running, nothing of theirs changed

Final `modal endpoint list --json` — the four baseline endpoints, same ids, all still `live`, and **no
`tree-*` endpoint**:

```
qwen3-embedding-8b    ep-UDN0woLFq8dwNwMoHqK3A1  live
qwen3-embedding-0-6b  ep-AFm6anFYXobWj3ba0MxKOw  live
gpt-oss-120b          ep-AZAaUPQk88Ko2i9xx5olsc  live
qwen3-6-35b-a3b-fp8   ep-nCsyPubmp2JsVUMkfPQR6E  live
```

Final `modal app list --json` — the two baseline apps unchanged (same ids, `deployed`, `tasks: 0`,
`stopped_at: null`) plus **two new rows, both ours, both `stopped`**:

```
ap-opyKC4h4Z7RVMV3KaFbi8M  decode-sandbox-prod   deployed  stopped_at=null
ap-v7ZtWBI4HIgrCiBF3IXZpI  decode-sandbox-local  deployed  stopped_at=null
ap-aYgIDQe2awiumbVi1WwyIO  ep-tree-lfm2-5-350m   stopped   stopped_at=2026-09-21 14:21:57+03:00
ap-MYMUtuVpim0sByBqAHwd29  ep-tree-voyage-4-nano stopped   stopped_at=2026-09-21 14:10:18+03:00
```

**`baseline: 2 apps, 4 endpoints — all unchanged`** (same ids, same states; the only additions are the two
STOPPED `ep-tree-*` apps this run created, which are not live and not billing). Every `modal … stop` argv in
this Log names a `tree-` / `ep-tree-` name and nothing else. The orphaned Modal secret
`vllm-embedding-api-key` and the old app `vllm-embedding-models` are not visible in `modal app list --json`
(which hides stopped apps) — the human should check the dashboard.

#### Cost — per cycle, GPU and deploy-to-stop

| cycle | model | route | GPU | deploy -> stop | outcome |
|---|---|---|---|---|---|
| 4a | Qwen/Qwen3-Embedding-0.6B | Dedicated endpoint | Modal's choice (not exposed by the CLI) | 10:55:59Z -> 10:59:34Z = **3m35s** | PASS |
| 4b | voyageai/voyage-4-nano | vLLM App | A10 (catalog) | 10:59:57Z -> 11:10:18Z = **10m21s** | FAIL (spec transport) |
| 4c | LiquidAI/LFM2.5-350M | SGLang App | A10 (catalog) | 11:12:34Z -> 11:21:57Z = **9m23s** | FAIL (volume mount) |
| 4d | Qwen/Qwen3.5-0.8B | Dedicated endpoint | Modal's choice (not exposed by the CLI) | 11:22:22Z -> 11:45:45Z = **23m23s** | deploy/route/stop PASS, chat smoke FAIL |

No cycle exceeded the 30-minute cap; ONE model was live at a time; every cycle was stopped, success or
failure. Both crash-looping Apps were stopped as soon as the loop was recognised rather than at the deadline.

#### Every "SWE must verify" / "#141 verifies live" item, answered

| item | verdict | source |
|---|---|---|
| GPU strings (`A10`, no `A10G`) | **OK** for the Apps — `gpu: A10` in both deploy specs, `✓ Created function Server.` with no GPU error | 4b/4c deploy logs |
| the GPU a managed endpoint picks | **NOT RECORDABLE via CLI** — `modal endpoint list --json` has no GPU column on modal 1.5.5; dashboard-only | 4a/4d listings |
| vLLM `0.26.0` pin + CUDA 13.0.2 base actually working | **NOT VERIFIED** — layers came from Modal's cache and the container never reached the engine (blocker 2) | 4b app log |
| `VoyageQwen3BidirectionalEmbedModel` on that pin | **NOT VERIFIED** (same) | 4b app log |
| SGLang docker tag + `uv_pip_install` on it | **IMAGE OK** (`lmsysorg/sglang:v0.5.18` + `autoinference-utils==0.2.6` built and deployed); **sgl-kernel / sm_86 NOT VERIFIED** — the container never started (blocker 3) | 4c deploy + app logs |
| LFM2 on SGLang | **NOT VERIFIED** (blocker 3) | 4c app log |
| `speculative_model_path` optional | not exercised live; unit evidence only (#147) | — |
| `json_object` on SGLang | **NOT VERIFIED** (blocker 3) | — |
| async `get_url` | **VERIFIED** — `resolve_server_url` used `get_url.aio()` and returned the URL without blocking in 4a and 4d | 4a smoke, 4d `ModalLLM ready:` |
| `modal endpoint stop` by NAME | **VERIFIED** — `modal endpoint stop -y tree-qwen3-embedding-0-6b` / `-y tree-qwen3-5-0-8b`, both `✓ Stopped Endpoint … (ID: ep-…)`, first try | 4a/4d stop logs |
| `--name` -> app-name rule (H1) | **VERIFIED TRUE** — app `ep-tree-qwen3-embedding-0-6b`, class `Server` | `modal app history` + resolved URL |
| the two `list --json` key sets | **VERIFIED** — endpoints `name, endpoint_id, status, created_at, created_by`; apps `app_id, description, state, tasks, created_at, stopped_at` | 4.0 baseline |
| whether `endpoint create` blocks | **VERIFIED: it does NOT** — returns in ~4 s with `status: provisioning`; `live` after 2m15s (0.6B) / 9m25s (0.8B) | 4a/4d |
| refusal on stdout vs stderr | **stderr** — `modal/__main__.py:main` -> `print_error` -> `modal/_output/rich.py:340-346`, `_stderr_console` (`:264`) | pinned client source |
| Qwen3 prompt bytes | sent as the catalog spells them; ranking held (0.78 > 0.09). Nothing ASSERTS the bytes — ADR-009 Consequences already says so | 4a smoke |
| wire width per embedding seed + the `dimensions` answer | **Qwen3 endpoint: 1024 native; `dimensions=512` -> HTTP 400 "does not support Matryoshka embeddings".** voyage-4-nano: **NOT MEASURED** (blocker 2) | 4a wire probe |
| `secrets=` on `@app.server` | **ACCEPTED** — both App deploys created the Server function with `secrets=[HF_SECRET]`, no error | 4b/4c deploy logs |
| local-vs-container `Secret.from_dict` | local half deployed; **container half NOT PROVEN LIVE** (blockers 2/3, and no token configured) | — |
| engine subprocess inheriting `HF_TOKEN` | **NOT PROVEN LIVE** — no engine ever started, no token configured | — |
| `--routing-region eu-west` accepted | **VERIFIED** — both endpoint creates succeeded with it | 4a/4d deploy logs |
| `modal app list` sees an endpoint's app | **NO** — it lists neither endpoint-backed `ep-*` apps nor stopped apps. The guard's endpoint-list leg is what covers that case | 4.0 + 4a listings |

#### Docs (step 5)

`.agents/skills/run-pipelines-e2e/SKILL.md` gained step 4 (After changing the embedding model — dry run,
`CONFIRM=yes`, reset -> indexing -> clustering) and step 5 (Models on Modal — the three targets for both
kinds, the one `Routing …` line, `SERVING=` as an escape hatch only, always stop after, `HF_TOKEN` only for
private/gated repos, the poller and `TREE_MODAL__WARMUP_DEADLINE_S`), ending with the verbatim `DRY_RUN=yes`
sentence the task specifies. Greps: `memory-reset-embeddings` 2, `memory-deploy-model` 4, `Routing` 1,
`HF_TOKEN` 1, `DRY_RUN=yes` 1, `TREE_MODAL__WARMUP_DEADLINE_S` 1, `deploy-embedding-model` **0**.

`apps/memory/README.md` gained the one paragraph this run proved was missing (endpoint `provisioning` vs
`live`, with the two measured times). Everything else in its Modal sections still matches what 4a/4d showed,
so nothing was removed. I deliberately did NOT rewrite the App-route sections around blockers 2 and 3: the
PA's decision will change that code, and a caveat today would be a wrong sentence tomorrow.

**Notes for the Tester and the PA**
- NOT RUN — Part 1 in full (isolation proof and both `tree` counts done; nothing else), because
  `docker stop tree-prefect-worker` was refused by the sandbox. Needs the human.
- NOT RUN — the threshold re-pin, so `configs/default.yaml`, the Pydantic defaults, their tests and
  `frozen_config.yaml` are untouched and `min_vector_score` stays 0.75 on voyage-3.5 evidence.
- `tree_e2e_141` exists and is EMPTY (7 collections, 0 documents, 0 bytes); the drop was refused by the
  sandbox. Safe to drop by hand.
- Three escalations are above: blocker 1 (human, permission), blockers 2 and 3 (PA, ADR-009 §3).
- Nothing was committed — the Tester goes first.

### [PA] 2026-09-21 16:10 — Re-grooming after round 1 (five fix tasks; round 2 = only what is unproven)

**What round 1 proved and what it could not**
- PROVEN, not re-run: baseline/4e method, cycle 4a in full (H1 TRUE, guard exit 3, stop by name, `eu-west`, wire 1024 + the 400 on `dimensions`), all four `Routing …` lines, refusals on STDERR (already joined since #147), `modal endpoint create` is asynchronous, the docs greps.
- FAILED on three real defects + one missing permission -> five tasks, order **153 -> 154 -> 155 -> 156 -> 157 -> 141 round 2**:
  - #153 (4b) the deploy spec crosses as BASE64 — the raw JSON lost every `\"` in the image build, so the default embedding model crashed at import.
  - #154 (4c) the weights Volume mounts at `/cache/huggingface` with `HF_HOME` + `HF_HUB_CACHE`, both scripts — the SGLang image ships a non-empty `/root/.cache/huggingface`.
  - #155 (4d) `modal.request_timeout_s: 300`, `max_retries=0`, and a TIMEOUT is not a cold answer (no re-warm loop) — the 13-minute hang.
  - #156 (4d) per-LLM-entry `max_tokens` + `chat_template_kwargs`, one helper for client and smoke test; `Qwen/Qwen3.5-0.8B` seeded `enable_thinking: false`.
  - #157 `-test` waits while our endpoint is `provisioning` (1800 s, fail-open); `deploy` says so; the guard's endpoint-first short-circuit pinned.
- F4 (`modal app list` sees neither an endpoint's app nor long-stopped apps): analysed, NO open collision class — the endpoint list is read first and is what protects; recorded in ADR-009 Consequences, pinned in #157.

**Decisions taken here (ADR-009 revision 6; glossary rows "Modal catalog" and "Warm gate" updated)**
- Part 1 no longer needs the human's container stopped: a throwaway Prefect server on port 4201 + `PREFECT_API_URL` as a third make command-line variable isolates the worktree's runner completely, with zero code change. A deployment-name suffix was rejected — `run_deployment` names are spelled in 5+ modules. The `[HUMAN]` stop/confirm/restart line remains only as the fallback.
- 4d gets an explicit three-rung ladder inside one live cycle (the knobs are request fields — no redeploy), with `google/gemma-3-1b-it` as the only non-thinking small fallback in Modal's list.
- Two clauses of round 1's criteria are accepted as unrecordable (endpoint GPU; `modal app list` row of an endpoint app) and replaced as written in the amendment.
- For the human: drop the empty `tree_e2e_141` database (agents were denied).

**Answers to the SWE's `PA:` escalations**
- BLOCKER 2 -> option A (base64), #153. BLOCKER 3 -> `/cache/huggingface` + `HF_HOME`, both scripts, #154. BLOCKER 1 -> no permission needed, see R2-Part 1.
- The README paragraph round 1 added ("Watch that column, then run `-test`") is rewritten by #157, not here.

**Dependencies**
- #153-#157.

Ready for round 2 once #157 is merged.
