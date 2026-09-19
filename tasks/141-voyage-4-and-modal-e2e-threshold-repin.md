---
id: 141-voyage-4-and-modal-e2e-threshold-repin
status: pending
feature: voyage-4-and-modal-embedding-catalog
---

# Live e2e: reset -> index -> query on `voyage-4` with a threshold re-pin; really deploy, smoke-test and stop all three Modal Serving paths

Tags: `e2e`, `config`, `modal`, `docs`
Depends on: #134, #135, #136, #137, #138, #139, #140, #142
Blocks: —
Implements: ADR-009 — Decision 2 (the Serving path ladder, proven live), Decision 7 (migration run), Decision 8 (threshold re-pin protocol; amends ADR-008 §4) and Decision 9 (the `HF_TOKEN` path and its redaction, checked live on public weights)

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

**Wire-width probe — in EVERY round 4a-4d, while the model is up, right after `…-test`.** Two raw
`POST /v1/embeddings` calls for one text (ad-hoc `uv --directory apps/memory run python -c …` snippet using
`resolve_server_url`, `modal_proxy_bearer`, `served_model_id` + an HTTP client; nothing committed): one WITHOUT
`dimensions`, one WITH `dimensions` = `1024` for voyage-4-nano / `512` for Qwen3. Record ONE line per round:
`wire: <repo_id> via <endpoint|vllm|sglang> — no dimensions -> <len>; dimensions=<n> -> <len> | HTTP <status> "<error message>"`.
The second half is EVIDENCE ONLY (an HTTP 400 is an expected, complete answer — the client never sends
`dimensions`, ADR-009 §3); it decides whether server-side truncation is ever worth an upgrade. The first half
is a GATE on the known-good paths: if `no dimensions` on 4a, 4c or 4d differs from the entry's
`native_dimensions` (expected: Qwen3 1024, voyage-4-nano 2048), the catalog is wrong — fix
`configs/default.yaml` + `test_seed_entries` + `frozen_config.yaml` in this task's commit and write
`PA: glossary "Embedding catalog" + ADR-009 need native_dimensions <old -> new> for <repo_id>` in `## Log`.
On 4b a different width is simply the endpoint's verdict (see 4b).

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
4b. **voyage-4-nano as a Dedicated endpoint with custom weights (the experiment — and the only
   place the `HF_TOKEN` flag can be exercised live: both seeds are public, but this IS a
   custom-weights create, so #139's driver forwards the token whenever one is set).**
   Capture the deploy output to a scratchpad file (outside the repo), e.g.
   `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano SERVING=endpoint 2>&1 | tee <scratchpad>/4b-deploy.log`
   -> `…-test`. If create or the smoke test fails, paste the error — that is a complete answer. The
   LIKELIEST failure is `expected 2048 dims, got 1024`: the base Qwen3 recipe has no 1024->2048 projection
   head, so the endpoint answers the 1024-d hidden state — NOT a Matryoshka truncation of the real 2048-d
   vector, and not comparable to it. Run the wire-width probe even then (if `/health` is 200). If the smoke
   test passes, save reference vectors: with the real client
   (`ModalEmbeddingModel(proxy_token=modal_proxy_bearer(), model="voyageai/voyage-4-nano", dimensions=1024).embed(texts, None)`
   — `dimensions=1024` is the width the memory stores; the client truncates 2048 -> 1024 itself,
   an ad-hoc `uv --directory apps/memory run python -c …` snippet, nothing committed) embed the 3
   fixed texts `"how do I reset my password?"`, `"To reset your password, open Settings and choose Reset password."`,
   `"The Eiffel Tower is 330 metres tall."` and write them to the scratchpad. Then
   `…-stop MODEL=voyageai/voyage-4-nano SERVING=endpoint`.
   **HF token check (costs no extra deployment; NEVER open, print or grep `.env`; never paste a token):**
   - `grep -c -- "--custom-hf-token \*\*\*" <scratchpad>/4b-deploy.log`. `>= 1` means `HF_TOKEN` is
     set and was forwarded (a valid token is harmless on a public repo) -> run the leak check below
     and record `HF token path: PROVEN (endpoint argv redacted, leak-check exit=0)`.
     `0` means no `HF_TOKEN` is configured -> record
     `HF token path: NOT PROVEN LIVE — HF_TOKEN not set; unit evidence only (#139 TestHfToken)`
     and leave the [HUMAN] criterion below open. Neither outcome fails this task.
   - Leak check (value-free: it prints an exit code, never the secret; run it the same way as the
     reference-vector snippet so `Settings` sees the same environment):

         uv --directory apps/memory run python -c "import sys, pathlib; from tree.config.settings import settings; t = settings.hf_token.get_secret_value(); sys.exit(2 if not t else 1 if any(t in pathlib.Path(p).read_text() for p in sys.argv[1:]) else 0)" <scratchpad>/4b-deploy.log <scratchpad>/4c-app.log; echo "leak-check exit=$?"

     `0` = the token occurs in no captured file; `1` = LEAK -> stop, fix #139's redaction first and
     tell the human to rotate the token; `2` = no token visible to `Settings` (treat as NOT PROVEN).
     Paste deploy output into `## Log` only AFTER the leak check is 0.
4c. **voyage-4-nano through the vLLM fallback script (the known-good path, and the reference).**
   `make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano SERVING=vllm` -> `…-test` -> embed
   the same 3 texts the same way (`dimensions=1024`; assert `len == 1024` for all three before comparing) -> `…-stop … SERVING=vllm`.
   Expected smoke lines here: `3 embeddings, 2048 dims`, `truncated 2048 -> 1024 dims client-side, norm=1.000`, `sanity@1024: …`.
   **Shared-space check (recorded, NO gate), while 4c is up:** embed the 3 texts with the Voyage API model
   (`voyage-4`, 1024-d — the API default, `input_type="document"`) through `get_model`'s Voyage client and with
   voyage-4-nano truncated to 1024 (`input_type="document"`); record the 3 same-text cosines
   `cos(voyage-4 API @1024, nano @1024)` plus ONE cross-text contrast (`voyage-4` text 1 vs nano text 3).
   No threshold: Voyage publishes no numeric cross-model agreement, and nothing in the memory mixes the two
   models (one model per database, an Embedding reset on change), so a number here gates nothing. If the
   same-text cosines are not clearly above the cross-text one, write
   `PA: ADR-009 Context "one shared embedding space" needs correction` in `## Log`. Record cold-start seconds and the image
   build outcome (this is where the CUDA 13.0.2 / vLLM wheel compatibility and the GPU string are proven).
   While it is up, stream `uv --directory apps/memory run modal app logs ep-voyage-4-nano` into
   `<scratchpad>/4c-app.log` until the line `HF_TOKEN set in container: …` appears, then interrupt.
   With a token configured it must read `True` (the locally built Modal Secret reached the container
   although the in-container re-import builds an empty one — #142's verify item (b)); without one, `False`.
   Include that file in 4b's leak check.
   **Decision rule for the seed:** flip `voyageai/voyage-4-nano` to `serving: endpoint` ONLY IF 4b's
   smoke test passed (which now requires the endpoint to answer 2048-d natively) AND, for each of the 3
   texts, `cosine(endpoint vector, vLLM-script vector) >= 0.99` with BOTH vectors 1024-d (never compare
   different widths, and never a 1024-d hidden state against a truncated 2048-d vector)
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
   same `SERVING=` to stop, always stop after; `HF_TOKEN` only for private or gated repos). README: make sure the sections from
   #134/#137/#140/#142 read as one story and match what 4a-4d actually showed. Per the project rule,
   REMOVE now-wrong sentences rather than adding caveats.

## Out of scope
- A pipeline run with `provider: modal`. Tuning beyond the pin (Chapter 7 evals own the knobs).
- Running anything against prod; migrating prod users (README documents the per-user loop).
- Dashboard-only settings of a Dedicated endpoint (min/max/buffer containers).
- Deploying a really gated or private model: no seed is one, so the token path is proven on PUBLIC
  weights only (the flag is sent and redacted; the Secret reaches the container). That a token
  actually unlocks a gated download is Hugging Face's and Modal's contract, not re-proven here.
- Editing `docs/glossary.md` / `docs/adrs/` (PA-owned): if a default, the voyage-4-nano Serving path,
  H1 or the token mechanism (argv -> env var) changes, write `PA: <doc> needs <old -> new>` in `## Log`.

## Acceptance Criteria

- [ ] `## Log` records: dry-run count, reset count, `Embedded N nodes` with N == reset count (minus any logged Voyage-400 skips), and the `mongosh` count of rows with a 1024-length vector.
- [ ] `## Log` records both `min_vector_score` pin queries with their `top=` scores and the FINAL value; the nonsense query answers `nothing_found`, the on-topic query `found`, at that value.
- [ ] `## Log` records >= 1 true-duplicate and >= 1 distinct-pair similarity on voyage-4 and, per knob (`semantic_threshold`, `auto_merge_threshold`, `flag_threshold`), either `unchanged — evidence` or `old -> new` with the YAML/default/test diff in the same commit.
- [ ] `make memory-visualize-embeddings` output starts with the stale-map warning after indexing and does not after clustering — both first lines pasted in `## Log`.
- [ ] 4a: `## Log` has the Qwen3 Dedicated endpoint smoke lines (`health 200 after …s`, `served model id: …`, `3 embeddings, 1024 dims`, `sanity: cos(query, relevant)=… > cos(query, unrelated)=…`, `unauthenticated health -> 401`, `Smoke test passed`), the pasted `modal endpoint list --json`, and an explicit line `H1: TRUE` or `H1: FALSE -> fix (i)|(ii)` with the diff in the same commit and `make memory-tests` green.
- [ ] Wire width: `## Log` has FOUR `wire: …` lines (4a, 4b, 4c, 4d — 4b may read `not reachable: <error>`), each with the length without `dimensions` and the length or HTTP status + message with `dimensions`; the three known-good paths show `no dimensions -> 1024` (Qwen3, 4a and 4d) and `-> 2048` (voyage-4-nano, 4c), or the catalog fix + `PA:` line is in the same commit.
- [ ] 4b + 4c: `## Log` has either the endpoint attempt's error output (e.g. `expected 2048 dims, got 1024`) OR its smoke lines plus the three `cosine(endpoint, vllm)` values with the line `compared at 1024-d / 1024-d`; the vLLM-script smoke lines (`3 embeddings, 2048 dims`, `truncated 2048 -> 1024 dims client-side`, `sanity@1024: …`) with cold-start seconds; and one line `voyage-4-nano serving: vllm (kept) — reason` or `vllm -> endpoint — three cosines >= 0.99`. `configs/default.yaml` matches that line.
- [ ] Shared space: `## Log` has the 3 same-text cosines `cos(voyage-4 API @1024, nano @1024)` and the 1 cross-text contrast, labelled recorded-not-gated, and a `PA:` line only if same-text is not clearly above cross-text.
- [ ] HF token: `## Log` has exactly one of `HF token path: PROVEN (endpoint argv redacted, leak-check exit=0)` — together with the pasted `Running: modal endpoint create … --custom-hf-token ***` line, the `grep -c` result, `leak-check exit=0` and the 4c line `HF_TOKEN set in container: True` — or `HF token path: NOT PROVEN LIVE — HF_TOKEN not set; unit evidence only (#139 TestHfToken)` together with `HF_TOKEN set in container: False`. `grep -c "hf_[A-Za-z0-9]\{20,\}" tasks/141-voyage-4-and-modal-e2e-threshold-repin.md` -> 0 (no token-shaped string in this file).
- [ ] 4d: `## Log` has the SGLang-script smoke lines for `Qwen/Qwen3-Embedding-0.6B` with `3 embeddings, 1024 dims` (and no `truncated` line — Qwen3 lists no Matryoshka size) and cold-start seconds; `app_config.modal.embedding_models` still says `serving: endpoint` for Qwen3.
- [ ] 4e: `modal endpoint list` AND `modal app list` output pasted in `## Log` show no running endpoint and no running `ep-*` app.
- [ ] Every "SWE must verify" item from #138, #139, #140, #142 is answered in `## Log` with its source: GPU string, vLLM pin, SGLang install spec, CUDA base image, async `get_url` form, minimum modal version shipping `modal endpoint`, `modal endpoint stop` identifier and `modal app stop` confirmation flag, `--name` -> app-name rule, `modal endpoint list --json` fields, base-model ids Modal accepted for embeddings, whether voyage-4-nano deploys as custom weights over `Qwen/Qwen3-Embedding-0.6B`, Qwen3 prompt bytes, Qwen3 Matryoshka support, the wire width of each seed per Serving path and what each path answers to an OpenAI `dimensions` parameter, whether `--custom-hf-token` has an env-var form and whether it is only for `--custom-hf-repo`, `secrets=` on `@app.server`, local-vs-container `Secret.from_dict`, the engine subprocess inheriting `HF_TOKEN`.
- [ ] `grep -c "memory-reset-embeddings" .agents/skills/run-pipelines-e2e/SKILL.md` >= 1, `grep -c "memory-deploy-embedding-model" …` >= 1, `grep -c "SERVING=" …` >= 1 and `grep -c "HF_TOKEN" …` >= 1.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green after any threshold, seed or H1 change.
- [ ] [HUMAN] Confirms in the Modal dashboard (Endpoints AND Apps) that nothing is billing, and removes `MODAL_EMBEDDING_API_KEY` from `.env` / `.env.prod`.
- [ ] [HUMAN] ONLY if the Log says `HF token path: NOT PROVEN LIVE`: either add a read-scope `HF_TOKEN` to `.env` and ask for a re-run of 4b's deploy + leak check (one endpoint cold start, then stop), or accept the unit-test evidence — state which in the Log.

## User Stories

### Story: Operator migrates the local memory to voyage-4 and trusts "nothing found" again
1. Runs reset (dry, then `CONFIRM=yes`), indexing, clustering.
2. `make memory-query-graph QUERY="zxqv plorb wumbus"` -> nothing found; `QUERY="how does the coordinator shard documents?"` -> ranked parents.
3. The log line `vector leg: … kept at min_vector_score=<final> (top=…)` matches the values in this task's Log.

### Story: Operator proves the easy path — a Dedicated endpoint with zero code of ours
1. `make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B`, `…-test`, `…-stop`.
2. The smoke test logs `3 embeddings, 1024 dims` (Qwen3's native width) through the proxy token, a sane relevant-vs-unrelated ordering and a 401 without the token.
3. `modal endpoint list` shows it stopped.

### Story: Operator finds out whether custom weights are enough for voyage-4-nano
1. Deploys it with `SERVING=endpoint` over base `Qwen/Qwen3-Embedding-0.6B`, tests, saves 3 vectors, stops.
2. Deploys it with `SERVING=vllm`, tests (`3 embeddings, 2048 dims`, `truncated 2048 -> 1024 dims client-side`), embeds the same 3 texts at `dimensions=1024`, stops.
3. Reads the Log: either the endpoint's error (`expected 2048 dims, got 1024` — no projection head under the base recipe) or three cosines compared at 1024-d / 1024-d; all >= 0.99 -> the YAML now says `serving: endpoint`; otherwise it still says `vllm`, and the Log says why.

### Story: The next engineer wonders whether the server could truncate for us
1. Opens this task's Log and finds four lines like `wire: voyageai/voyage-4-nano via vllm — no dimensions -> 2048; dimensions=1024 -> HTTP 400 "…does not support matryoshka…"`.
2. Knows, per Serving path, the native width and whether `dimensions` is honoured — and why ADR-009 §3 truncates client-side.
3. Also finds `cos(voyage-4 API @1024, nano @1024)` for three texts: the shared-space claim, measured once at the width the memory uses.

### Story: Operator with an `HF_TOKEN` in `.env` checks it never leaks
1. Runs step 4b with the output captured to a scratchpad file.
2. `grep -c -- "--custom-hf-token \*\*\*"` on the capture prints `1`; the leak check prints `leak-check exit=0`.
3. During 4c, `modal app logs ep-voyage-4-nano` shows `HF_TOKEN set in container: True` — and no token value anywhere.

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
