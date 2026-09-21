---
id: 158-modal-llm-json-mode-only-and-smoke-test-parity
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# JSON mode is the ONLY `response_format` a Modal chat request carries: `ModalLLM` loses its unused `schema=` keyword, and the chat smoke test sends — and judges — exactly what the client sends

Tags: `memory`, `modal`, `llm`, `tests`, `docs`
Depends on: None (#153-#157 are done and on this branch)
Blocks: #141
Implements: ADR-009 — Decision 10, revision 7 ("JSON mode is the ONLY `response_format` …", the smoke-test sentence) and Consequences ("LLMs on Modal are plumbing-complete…"). Found live by #141 round 2, cycle 4d + the enum probe.

## Scope

**EXECUTION ORDER: 158 -> 141 round 3** (the "ROUND 3 AMENDMENT for #141" at the end of this file is #141's instruction; this task's SWE does NOT execute it).

**HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`.
Run test commands one at a time — two concurrent pytest runs contend for the local MongoDB/Prefect and stall.
Do not run the real memory pipelines against the local database (shared with the human's `main` checkout).
Every test here is mocked (the suite already sets `TREE_MODAL_DRY_RUN=1`); no test sleeps for real; NO live call of any kind in this task.

**Facts (`tasks/141` Log, `[SWE] 2026-09-21 21:20 — Round 2`, section 4d; and this codebase).**
- On a Dedicated endpoint a strict `json_schema` is accepted, compiled and DEGENERATE: `Qwen/Qwen3.5-0.8B` (thinking off) and `google/gemma-3-1b-it` both answered `{"city": "Tokyo", "population": 1400000000000…` until `finish_reason=length`; a strict `enum: ["ALPHA"]` probe answered `{` + whitespace to `length`. JSON mode (`json_object`) on the SAME endpoint answered `"population": 14000000`. On our SGLang App the strict schema works (`{"city":"Tokyo","population":37}`).
- **No caller passes `schema=`.** The four production call sites — `memory/graph/extraction.py:168`, `memory/graph/judge.py:130`, `memory/graph/nl_query.py:477`, `memory/clustering/summaries.py:110` — all call `generate_json(prompt, system=…)`: the schema is in the prompt and each caller validates the dict itself, exactly as under `GeminiLLM` (`response_mime_type="application/json"`, no schema). So the memory ALREADY sends JSON mode on both routes; the endpoint route is broken only for (a) the chat smoke test and (b) a keyword nobody uses.
- The parity invariant of #156 has a hole: `TestChatSmokeTest::test_wire_parity_between_client_and_smoke_test` compares the KNOBS only, so it never noticed that the smoke test POSTs `json_schema` while the client sends `json_object`.
- The client cannot learn its route: both routes share app name, class `Server`, URL shape and auth (ADR-009 §3); `existing_kind` is a CLI call and must never run in a pipeline. Hence ONE rule for both routes, no detection, no YAML knob.

**What to build**
1. `tree.models.modal_catalog`: ONE constant `CHAT_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}`, beside `chat_request_knobs`, with a comment naming ADR-009 §10 and the rule in one sentence (a path-blind client sends only what every route honours). Both senders import it.
2. `tree.models.modal_llm`:
   - DELETE the `schema` parameter of `ModalLLM.generate_json`, `_response_format`, `_SCHEMA_NAME`. The signature becomes exactly `BaseLLM`'s: `generate_json(self, prompt: str, *, system: str | None = None) -> dict[str, Any]`. The request sends `response_format=CHAT_RESPONSE_FORMAT`.
   - Module docstring, "JSON mode, not a schema": keep the SGLang `json_object` -> `{"type": "object"}` fact; REPLACE the sentence about the optional `schema=` with two sentences: strict `json_schema` is degenerate on a Dedicated endpoint (`tasks/141` round 2), and a path-blind client sends only what both routes honour. Remove the `schema` paragraph from `generate_json`'s docstring. Net result must be FEWER lines than today.
3. `tree.models.modal_server`:
   - `CITY_FACTS_SCHEMA` becomes the PLAIN schema object (`{"type": "object", "properties": {"city": {"type": "string"}, "population": {"type": "integer"}}, "required": ["city", "population"], "additionalProperties": False}`) — no `json_schema` wrapper, no `strict`.
   - `CHAT_SMOKE_PROMPT = "Reply with JSON facts about Tokyo. Answer with ONE JSON object matching this JSON Schema: " + json.dumps(CITY_FACTS_SCHEMA, separators=(",", ":"))` — the schema travels in the PROMPT, as every caller of `generate_json` has it.
   - `_chat` POSTs `"response_format": CHAT_RESPONSE_FORMAT`; everything else in the body (model, one user message, `temperature: 0`, `CHAT_SMOKE_MAX_TOKENS` overridden by `chat_request_knobs`) is unchanged.
   - **Gate = the client's three verdicts on a 200, nothing more:** empty content (unchanged messages, incl. the reasoning diagnosis), `chat completion is not valid JSON: '<excerpt>'`, `chat completion is not a JSON object: '<excerpt>'` (drop the clause "— the strict city_facts schema was ignored"). These stay `ModelError`.
   - **Recorded, NOT gated = whether the prompt's shape was followed.** After the gate: ONE INFO line `JSON mode honoured: keys=['city', 'population']` (the object's keys, sorted; replaces `strict JSON schema honoured: city=… population=…`). If the keys are not exactly `{city, population}`, or `city` is not a `str`, or `population` is not an `int` (a `bool` is not an int): ONE WARNING starting `prompt schema not followed (recorded, not gated): ` followed by the reason `_city_facts` gives today (`missing [...], unexpected [...]`, or the wrongly typed key and its type). The smoke test still PASSES. Why: JSON mode promises an object, never its keys; key-following is model quality, which ADR-009 §10 does not decide, and a gate on it would fail a deploy for a reason no redeploy fixes.
   - `ChatSmokeTestReport`: replace `city` / `population` with `keys: list[str]` (sorted) and `follows_prompt_schema: bool`, each with a `Field(description=…)`.
   - Docstrings of `chat_smoke_test`, `_chat`, `_city_facts` and the comment above `CHAT_SMOKE_PROMPT`: say JSON mode; remove every "strict"/"constrained decoding `ModalLLM` depends on" claim.
4. Docs the SWE owns (replace, do not append — each edit must not grow the file by more than 3 lines):
   - `apps/memory/README.md` ~L608-616 (the paragraph ending "Strict-schema completions are proven on the **App** path … not on the endpoint one."): rewrite its second half to the rule — every Modal chat request is JSON mode on both Serving paths; strict `json_schema` degenerates on a Dedicated endpoint (`tasks/141` round 2), so the client never sends it.
   - README ~L693-699: "one strict-JSON chat completion" -> "one JSON-mode chat completion"; the sample line `strict JSON schema honoured: city=Tokyo population=13960000` -> `JSON mode honoured: keys=['city', 'population']`.
   - README ~L620-623 ("a warm-up that is a chat completion under a strict JSON schema … because constrained JSON is exactly what the memory asks of it"): keep the warm-up fact, change the reason to "the same grammar backend the memory's JSON mode relies on".
   - `apps/memory/Makefile` `deploy-model-test` help text: "one /v1/chat/completions under a strict JSON schema" -> "one JSON-mode /v1/chat/completions (the request ModalLLM sends)".
   - `apps/memory/scripts/modal_model.py::test_command` docstring: "one strict-JSON chat completion" -> "one JSON-mode chat completion".
   - `.agents/skills/run-pipelines-e2e/SKILL.md` L93: "one strict-JSON chat completion for an LLM" -> "one JSON-mode chat completion for an LLM". Do not touch the verbatim `DRY_RUN=yes` closing sentence.
   - `apps/memory/deploy/modal_sglang_llm.py`, the comment above `WARMUP_PAYLOAD` ONLY: "AND the constrained decoding `ModalLLM` will ask for" -> "AND the grammar backend `ModalLLM`'s JSON mode relies on". The payload itself is Modal's, value for value, and is NOT touched.

**Out of scope (intentional)**
- Route detection in the client, or a `structured_output` / `response_format` YAML knob — rejected in ADR-009 §10 rev 7 (good/bad examples there).
- A `schema=` on `BaseLLM`, schema-in-prompt rendering inside `ModalLLM`, a `jsonschema` dependency, client-side schema validation: no caller has a schema to pass; ADR-009 "What would justify upgrading" records the shape it takes when one does.
- The SGLang App's in-container strict warm-up payload and `tests/unit/deploy/test_modal_deploy_scripts.py` (must stay green UNCHANGED).
- Any change to the seeds in `configs/default.yaml` (round 3 of #141 owns that, and only on live evidence).
- Skipping the endpoint attempt for LLMs in `modal_router` — a FOLLOW-UP that exists only if round 3 fails (see the amendment); do not pre-build it.
- `docs/adrs/*`, `docs/glossary.md` — PA-owned, already at revision 7.

## Acceptance Criteria

- [x] `tests/unit/models/test_modal_llm.py::TestGenerateJson::test_json_mode_is_the_only_response_format` (renamed from `test_json_mode_is_the_default_response_format`): the `create` kwargs carry `response_format == {"type": "json_object"}` and it `is`/equals `tree.models.modal_catalog.CHAT_RESPONSE_FORMAT`.
- [x] `tests/unit/models/test_modal_llm.py::TestBaseContract::test_generate_json_has_no_schema_keyword`: `list(inspect.signature(ModalLLM.generate_json).parameters) == ["self", "prompt", "system"]`, and `await llm.generate_json("hi", schema={})` raises `TypeError` before any request (`openai` stub records zero calls).
- [x] DELETED, not skipped: `TestGenerateJson::test_a_schema_upgrades_the_request_to_strict_json_schema` and `TestWireContract::test_a_schema_reaches_the_wire_as_strict_json_schema`. `TestWireContract::test_the_body_carries_the_model_messages_temperature_and_format` stays green and asserts `json_object` on the wire.
- [x] `tests/unit/models/test_modal_server.py::TestChatSmokeTest::test_it_posts_json_mode_with_the_schema_in_the_prompt` (replaces `test_it_posts_the_strict_city_facts_schema`): the POSTed body has `response_format == {"type": "json_object"}`, NO `json_schema` key anywhere in it, and its single user message contains the compact `json.dumps(CITY_FACTS_SCHEMA, separators=(",", ":"))`.
- [x] `::TestChatSmokeTest::test_wire_parity_between_client_and_smoke_test` — EXTENDED, for both parametrised seeds: `smoke_body["response_format"] == client_call["response_format"] == CHAT_RESPONSE_FORMAT`, in addition to the existing knob assertions.
- [x] `::TestChatSmokeTest::test_the_happy_path_logs_seven_lines_in_order`: line 5 of `_CHAT_LOG_LINES` is `JSON mode honoured: keys=['city', 'population']`; `report.keys == ["city", "population"]`, `report.follows_prompt_schema is True`; no WARNING record.
- [x] `::test_an_extra_key_is_recorded_not_gated`, `::test_a_missing_key_is_recorded_not_gated`, `::test_a_wrongly_typed_value_is_recorded_not_gated` (replace `test_an_extra_key_is_named`, `test_a_missing_key_is_named`, `test_a_wrongly_typed_value_fails`; keep the latter's parametrisation incl. `population: true`): the smoke test RETURNS, `follows_prompt_schema is False`, exactly ONE WARNING whose message starts `prompt schema not followed (recorded, not gated): ` and names the key; `Smoke test passed` is logged.
- [x] `::test_a_nested_object_of_the_models_own_shape_passes`: content `{"tokyo_facts": [{"city": "Tokyo", "population": 37}]}` (round 2's live LFM2.5 JSON-mode answer, shortened) -> passes, `keys == ["tokyo_facts"]`, one WARNING.
- [x] Still `ModelError`, unchanged tests green: `test_a_non_json_completion_fails_with_an_excerpt` (use the live digit run `{"city": "Tokyo", "population": 1400000000000000000000` as one input), `test_a_long_bad_completion_is_truncated`, `test_json_that_is_not_an_object_fails`, `test_an_empty_completion_fails`, both `test_an_empty_answer_*` tests.
- [x] `tests/unit/scripts/test_modal_model_script.py` builds `ChatSmokeTestReport` with the new fields and stays green.
- [x] `grep -c '"json_schema"' apps/memory/src/tree/models/modal_llm.py apps/memory/src/tree/models/modal_server.py apps/memory/src/tree/models/modal_catalog.py` -> `0`, `0`, `0` (no dict literal with that key is left; prose naming `json_schema` in backticks to say why it is NOT sent is fine).
- [x] `grep -c "strict JSON schema honoured" apps/memory/README.md apps/memory/src/tree/models/modal_server.py` -> `0` and `0`; `grep -c "one strict-JSON chat completion" apps/memory/README.md` -> `0`; `grep -ci "strict.json" apps/memory/Makefile apps/memory/scripts/modal_model.py .agents/skills/run-pipelines-e2e/SKILL.md` -> `0`, `0`, `0` (the README is deliberately not in this grep: its sentence about the SGLang App's strict in-container warm-up stays).
- [x] `git diff --stat -- apps/memory/deploy/modal_sglang_llm.py` shows a comment-only change (<= 2 lines); `tests/unit/deploy/test_modal_deploy_scripts.py` is NOT in the diff.
- [x] `git diff --numstat -- apps/memory/src/tree/models/modal_llm.py`: deletions > insertions.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (LOCAL env; one command at a time).

## User Stories

### Story: Operator smoke-tests an LLM that Modal serves as a Dedicated endpoint
1. `make memory-deploy-model MODEL=Qwen/Qwen3.5-0.8B`, then at once `make memory-deploy-model-test MODEL=Qwen/Qwen3.5-0.8B`.
2. After `Live: …` / `Warm: …` they read `chat knobs: max_tokens=4096 chat_template_kwargs={"enable_thinking": false}`, `chat completion: {"city": "Tokyo", "population": 14000000}`, `JSON mode honoured: keys=['city', 'population']`, `unauthenticated health -> 401`, `Smoke test passed`.
3. No digit run, no `finish_reason=length`: the request was JSON mode, the one the memory sends.

### Story: Operator smoke-tests the SGLang App and reads the same lines
1. `make memory-deploy-model-test MODEL=LiquidAI/LFM2.5-350M`.
2. The same five lines, `chat_template_kwargs={}`. The operator cannot tell the route from the output — and neither can the client.

### Story: A 350M model answers JSON of its own shape
1. The completion is `{"tokyo_facts": [{"city": "Tokyo", …}]}`.
2. Operator reads `JSON mode honoured: keys=['tokyo_facts']`, then ONE warning `prompt schema not followed (recorded, not gated): missing ['city', 'population'], unexpected ['tokyo_facts'] …`, then `Smoke test passed`.
3. They know the server works and the model is a weak instruction-follower — a quality call that is theirs (README: "the client guarantees the plumbing, not the quality").

### Story: A server that cannot produce JSON still fails the deploy check
1. The completion is `{"city": "Tokyo", "population": 14000000000000…` cut at `max_tokens`.
2. `chat completion is not valid JSON: '{"city": "Tokyo", "population": 1400000…'`, exit 1 — the same verdict `ModalLLM` would raise as `Modal LLM returned invalid JSON: …`.

### Story: The memory flips `models.llm` to Modal and nothing about the call changes per route
1. `models.llm: {provider: modal, model: Qwen/Qwen3.5-0.8B}`; the extraction worker calls `llm.generate_json(chunk, system=…)`.
2. The wire body carries `response_format: {"type": "json_object"}` plus the entry's knobs — byte-for-byte the same fields whether an endpoint or our App answers.
3. A wrong-shaped dict is filtered by `_parse_extraction`; invalid JSON is an `ExtractionError` and Prefect's task retry decides — as under Gemini.

### Story: An engineer tries to pass a schema
1. Writes `await ModalLLM(...).generate_json(prompt, schema=my_schema)`.
2. Gets `TypeError: … unexpected keyword argument 'schema'` at once — not a silent endless digit run on one of two routes.
3. Finds the reason and the upgrade path in ADR-009 §10 and "What would justify upgrading".

---

Blocked by: (none)

## ROUND 3 AMENDMENT for #141

*(PA, 2026-09-21. For the orchestrator to reference from `tasks/141-voyage-4-and-modal-e2e-threshold-repin.md`; where it disagrees with #141's Scope or its ROUND 2 AMENDMENT, THIS wins. Not executed by #158's SWE.)*

**ORDER: 158 -> 141 round 3.** Do not start before #158 is merged into this branch (`git log --oneline` must show it).

**Round 3 re-runs ONLY cycle 4d.** Everything else is PROVEN and NOT re-run: Part 1 (the `min_vector_score` 0.75 -> 0.70 re-pin; ADR-008 §4 and the glossary now carry it — the third `PA:` line is closed), 4a (round 1), 4b and 4c (round 2), the provisioning wait (#157), the timeout (#155), the knobs (#156), routing, stop, and the stopped-name re-create fact (now in ADR-009 Consequences). Do NOT deploy `Qwen/Qwen3-Embedding-0.6B`, `voyageai/voyage-4-nano` or `LiquidAI/LFM2.5-350M`. 4c's strict-schema evidence stands as history; its round-2 `JSON-MODE result` through `ModalLLM` already is the request the memory sends on the App route, so 4c needs no re-run under the new smoke test.

**STANDING SAFETY RULES — unchanged, in full:**
- **HARD SAFETY RULE:** No agent may run `make memory-deploy-*`, `scripts/modal_*.py` as a process, or any `modal …` command outside task 141; a PATH shim does NOT intercept under `make`/`uv run` because `.venv/bin` wins — use the mocked unit tests and `DRY_RUN=yes`. Task 141 is the ONLY place those commands run for real — and only the ones written here, only on `tree-` / `ep-tree-` names.
- **HARD RULES (the 2026-09-20 incident, see #143):** this task creates, tests and stops ONLY names that start with `tree-` / `ep-tree-`. It NEVER calls, stops, recreates, deploys over or smoke-tests anything that existed before the run; it NEVER passes `FORCE=yes`; if the guard refuses (exit 3) where this file does not expect it, STOP and write the refusal in `## Log` — do not work around it. NOT ours and never touched: the human's own endpoints `qwen3-embedding-0-6b`, `qwen3-embedding-8b`, `gpt-oss-120b`, `qwen3-6-35b-a3b-fp8` (no request is sent to them — a request wakes a billed GPU), and the stopped accidental apps `ep-voyage-4-nano` / `ep-qwen3-embedding-4b`. To check a command without running it use `DRY_RUN=yes` — never a fake `modal` on `PATH`.
- `make` targets only (`make memory-deploy-model` / `-test` / `-stop MODEL=…`); `DRY_RUN=yes` FIRST for every deploy and every stop; never `FORCE=yes`, never `SERVING=`; no `docker` command; no `dropDatabase`; never open, read or edit `.env`; LOCAL env (`make env-status` -> local). No pipeline run, no Prefect, no MongoDB write in this round.
- ONE model live at a time; a cycle is deploy -> stop in **<= 30 minutes** — past that, stop it and write why. No call may exceed 300 s: a `timed out after 300s` line is a complete, recordable answer.

**The cycle.**
0. Fresh 4.0 baseline (read-only `modal endpoint list --json` / `modal app list --json`): expect the human's 4 endpoints + 2 apps, plus possibly our STOPPED `ep-tree-*` apps. A LIVE `tree-*` / `ep-tree-*` thing = stop and ask.
1. `make memory-deploy-model MODEL=Qwen/Qwen3.5-0.8B DRY_RUN=yes`, then without `DRY_RUN`. Record the `Routing … → Dedicated endpoint tree-qwen3-5-0-8b` line and the `… is provisioning — …` line. Seed as committed: `chat_template_kwargs: {enable_thinking: false}`, `max_tokens: 4096`.
2. IMMEDIATELY `make memory-deploy-model-test MODEL=Qwen/Qwen3.5-0.8B`. **This is the FIRST thing round 3 checks, and it is the open question:** JSON mode with `enable_thinking: false` on THIS model is unproven (round 2 proved JSON mode on the endpoint only for `google/gemma-3-1b-it`). No hand-made probe before it — the smoke test IS the probe. Record `Provisioning:` / `Live:` / `Warm:`, then the chat lines: `chat knobs: max_tokens=4096 chat_template_kwargs={"enable_thinking": false}`, `chat completion: …`, `JSON mode honoured: keys=[…]`, any `prompt schema not followed (recorded, not gated): …` warning, `unauthenticated health -> 401`, `Smoke test passed`.
3. Client check — the same ad-hoc, uncommitted snippet method as round 2's 4c/4d client checks, no Prefect in the path (say so): `await ModalLLM(…, model="Qwen/Qwen3.5-0.8B").generate_json(CHAT_SMOKE_PROMPT)` with `CHAT_SMOKE_PROMPT` imported from `tree.models.modal_server`. **GATED:** a `dict` comes back (no `ExtractionError`), the `ModalLLM ready: app=ep-tree-qwen3-5-0-8b …` line, and the Opik `modal-generate-json` span with `provider: modal`, `model: Qwen/Qwen3.5-0.8B`, non-zero tokens. **RECORDED, not gated:** whether `set(result) == {"city", "population"}` and `population` is an `int` — paste the dict and one line `4d shape: exact | differs (<keys>)`.
4. `make memory-deploy-model-stop MODEL=Qwen/Qwen3.5-0.8B DRY_RUN=yes`, then for real. 4e: `baseline: <n> apps, <m> endpoints — all unchanged`, no live `tree-*` / `ep-tree-*`. Record deploy -> stop minutes.
5. ONE line: `4d answered in JSON mode: Qwen/Qwen3.5-0.8B (enable_thinking: false, 4096)`.

**If step 2 or 3 FAILS on `Qwen/Qwen3.5-0.8B`** (invalid JSON, empty content, or a 300 s timeout — paste it verbatim; do NOT try thinking-on, a 0.8B model reasoning for thousands of tokens per chunk is no use to the memory): that is a MODEL finding, not a route finding, because `google/gemma-3-1b-it` answered JSON mode on the same route in round 2. Stop the endpoint, then run the cycle ONCE more with the fallback seed **`google/gemma-3-1b-it`** exactly as round 2's rung 3 did (seed change in `configs/default.yaml` + `test_seed_entries` + `frozen_config.yaml`, in #141's commit, with a line `PA: ADR-009 §3/§10 + glossary "Modal catalog" — LLM endpoint seed Qwen/Qwen3.5-0.8B -> google/gemma-3-1b-it: <Qwen's failure verbatim>`). If Modal demands an `HF_TOKEN` or a licence for it, STOP — a `[HUMAN]` decision.

**If JSON mode ALSO fails on the fallback** (contradicting round 2's own evidence): STOP — no third cycle, no other model, no knob hunting. Stop the endpoint, REVERT the seed change, run 4e, leave the 4d box unticked, and write `PA: ADR-009 §10 — JSON mode fails on the Dedicated-endpoint route too: <both failures verbatim>`. The LLM endpoint route is then RECORDED as unsupported for JSON output and LLMs are served by the SGLang App only. That needs a router change, which is a FOLLOW-UP task the PA grooms THEN (next free NNN, ADR-009 revision 8) — NOT pre-built, NOT improvised in #141. Its shape, so the orchestrator knows what it is asking for:
  - `tree.models.modal_router`: an entry of kind `llm` skips the endpoint attempt (steps 1-2 of ADR-009 §2) and deploys `deploy/modal_sglang_llm.py` directly, with ONE line `Routing <repo_id>: Modal's LLM endpoints cannot return JSON (tasks/141 round 3) → SGLang App`; embeddings keep endpoint-first; `SERVING=endpoint` stays the one-command escape hatch.
  - Seeds: `Qwen/Qwen3.5-0.8B` either leaves `modal.llm_models` or gains `--reasoning-parser` in `extra_server_args` AND its knobs in the App's in-container warm-up (the upgrade path ADR-009 already names); glossary "Serving path" / "Modal catalog" and ADR-009 Decision 2 change with it.
  - One more live cycle (that seed -> SGLang App) in a round 4. The human's requirement "LLMs auto-route endpoint-first" would then be formally dropped for LLMs — surface it to the human before the follow-up ships.

### Round 3 acceptance criteria (replace round 2's unticked 4d box and the "4c + 4d six chat smoke lines" box; every other unticked box keeps its round-2 meaning)

- [ ] `git log --oneline` shows #158 BEFORE any round-3 evidence; the only `MODEL=` values in round-3 commands are `Qwen/Qwen3.5-0.8B` (and `google/gemma-3-1b-it` only after a pasted Qwen failure).
- [ ] 4d: the `Routing …` line, the `… is provisioning — …` line, >= 1 `Provisioning:` line, `Live: … after <N>s`, `Warm: …`, the chat smoke lines incl. `chat knobs: …` and `JSON mode honoured: keys=[…]`, `Smoke test passed`; NO `json_schema` request was sent by anything in the round.
- [ ] 4d client: the `ModalLLM ready:` line, the returned `dict` pasted, the `4d shape: …` line, the Opik `modal` span with non-zero tokens. (`total_estimated_cost: null` in Opik's read-back is accepted — the client sends `total_cost=0.0`; round 2 recorded this.)
- [ ] Every deploy and stop argv was dry-run first and names a `tree-` name; no `FORCE`, no `SERVING`, no `docker`, no `.env` read; deploy -> stop <= 30 minutes per cycle; no call above 300 s.
- [ ] 4e: `baseline: <n> apps, <m> endpoints — all unchanged`; no live `tree-*` / `ep-tree-*`.
- [ ] The line `4d answered in JSON mode: …` — OR the fallback's full cycle with its `PA:` seed line — OR the route-level `PA:` line with the seed change reverted and the box left open.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green if any file changed (a seed change only).
- [ ] [HUMAN] (carried over) Drops `tree_e2e_141`: `mongosh` -> `db.getSiblingDB("tree_e2e_141").dropDatabase()`.

## Log

### [PA] 2026-09-21 22:30 — Grooming

**Summary**
`ModalLLM` sends JSON mode and nothing else on both Serving paths (its unused `schema=` keyword is deleted), and the chat smoke test sends that same request from one shared constant, gates on the client's three verdicts and only records whether the prompt's shape was followed.

**Key decisions**
- ONE uniform rule over route detection: the client is path-blind by design (ADR-009 §3) and the only route oracle is a CLI call that must not run in a pipeline; a YAML knob would be configuration about something only Modal knows.
- DELETE `schema=` rather than re-implement it as prompt-rendering + `jsonschema` validation: `grep` finds zero callers — all four call sites embed the schema in the prompt and validate the dict themselves, as under Gemini. The memory already sends JSON mode; only the smoke test and a dead keyword were broken on the endpoint.
- The smoke test's gate is the client's gate (object back), not key-following: JSON mode never promised keys, and a 350M model's instruction-following is the quality question ADR-009 leaves to evals. The mismatch is a WARNING so the operator still sees it.
- Parity is now asserted on `response_format` too — the hole that let the smoke test drift from the client.
- Strict decoding stays only in the SGLang App's in-container warm-up (Modal's payload): an engine health check, untouched.
- Seeds unchanged: `Qwen/Qwen3.5-0.8B` stays until round 3 shows JSON mode fails on it; the fallback ladder is in the amendment.

**Dependencies**
- None.

**User stories**
- 6: endpoint smoke test, App smoke test (same lines), own-shape JSON recorded not gated, non-JSON still fails, the memory's call is route-identical, an engineer passing `schema=` fails loudly.

**Documentation updates (this grooming round)**
- ADR-009 -> revision 7: Decision 10 (JSON-mode-only rule with good/bad examples; smoke-test sentence), diagram (2 nodes), Consequences (keys not guaranteed; `endpoint create` over a STOPPED name succeeds with a new id; the `schema=` upgrade path).
- ADR-008 §4 + glossary "Retrieval outcome": `min_vector_score` 0.75 -> 0.70, re-pinned on voyage-4, thin margin stated (0.649 / 0.735), still provisional and evals-owned.
- Glossary: no new terms.

Ready for implementation.

### [SWE] 2026-09-21 23:40 — Implementation

**Files modified**
- `apps/memory/src/tree/models/modal_catalog.py` — ONE `CHAT_RESPONSE_FORMAT = {"type": "json_object"}` constant (comment: ADR-009 §10, the path-blind rule), imported by both chat senders.
- `apps/memory/src/tree/models/modal_llm.py` — `schema=`, `_response_format` and `_SCHEMA_NAME` DELETED; `generate_json(self, prompt, *, system=None)` is exactly `BaseLLM`'s signature and sends `response_format=CHAT_RESPONSE_FORMAT`; module + method docstrings rewritten (16 insertions / 42 deletions).
- `apps/memory/src/tree/models/modal_server.py` — `CITY_FACTS_SCHEMA` is the plain schema object; `CHAT_SMOKE_PROMPT` carries it as compact JSON in the PROMPT; `_chat` POSTs `CHAT_RESPONSE_FORMAT`; `_city_facts` split into `_json_object` (the gate: valid JSON + an object, `ModelError`) and `_prompt_schema_mismatch` (the reason, or `None` — recorded, never raised); `ChatSmokeTestReport` now carries `keys: list[str]` + `follows_prompt_schema: bool`.
- `apps/memory/README.md` (3 paragraphs), `apps/memory/Makefile` (`deploy-model-test` help), `apps/memory/scripts/modal_model.py` (`test_command` docstring), `.agents/skills/run-pipelines-e2e/SKILL.md` L93, `apps/memory/deploy/modal_sglang_llm.py` (the WARMUP_PAYLOAD comment ONLY — 2 lines, payload untouched).
- `apps/memory/tests/unit/models/test_modal_llm.py`, `.../test_modal_server.py`, `apps/memory/tests/unit/scripts/test_modal_model_script.py` — see below.

**Tests**
- Unit: 4056 passing, 0 failing (`make memory-tests`, LOCAL env) — the same total as the baseline: 3 nodes removed, 3 added (accounted below).
- Integration: N/A — this repo has no integration suite (CLAUDE.md); no live call of any kind was made in this task.

**Deleted, not skipped**
- `TestGenerateJson::test_a_schema_upgrades_the_request_to_strict_json_schema`
- `TestWireContract::test_a_schema_reaches_the_wire_as_strict_json_schema`
- `TestGenerateJson::test_the_seeded_thinking_model_sends_both_knobs` lost its `schema` parametrisation (`json-mode` / `strict-schema` -> ONE node).
Added in their place: `TestBaseContract::test_generate_json_has_no_schema_keyword`, `TestChatSmokeTest::test_a_nested_object_of_the_models_own_shape_passes`, and a second row on `test_a_non_json_completion_fails_with_an_excerpt` (`the-live-digit-run`). Net 0.

**Renamed (same behaviour, new verdict)**
- `test_json_mode_is_the_default_response_format` -> `test_json_mode_is_the_only_response_format` (also asserts `is CHAT_RESPONSE_FORMAT`).
- `test_it_posts_the_strict_city_facts_schema` -> `test_it_posts_json_mode_with_the_schema_in_the_prompt`.
- `test_a_missing_key_is_named` / `test_an_extra_key_is_named` / `test_a_wrongly_typed_value_fails` -> `test_a_missing_key_is_recorded_not_gated` / `test_an_extra_key_is_recorded_not_gated` / `test_a_wrongly_typed_value_is_recorded_not_gated` (parametrisation kept, incl. `population: true`).
- `test_wire_parity_between_client_and_smoke_test` EXTENDED: `smoke_body["response_format"] == client_call["response_format"] == CHAT_RESPONSE_FORMAT`, on both seeds.

**Acceptance criteria**
Every box above is ticked; the node ids in them all resolve (`--collect-only` on the 15 of them, run once). None is `[HUMAN]`.

**Evidence**
```
$ PYTEST_ADDOPTS="tests/unit/models/test_modal_llm.py -q -k '...'" make memory-tests   # RED, before the change
E       AssertionError: assert {'type': 'json_object'} is {'type': 'json_object'}
E       AssertionError: assert ['self', 'pro...em', 'schema'] == ['self', 'prompt', 'system']
$ PYTEST_ADDOPTS="tests/unit/models/test_modal_server.py -q -k TestChatSmokeTest" make memory-tests   # RED
10 failed, 26 passed, 22 deselected

$ make memory-tests
============================ 4056 passed in 58.45s =============================

$ make memory-format-check   # 313 files already formatted
$ make memory-lint-check     # All checks passed!
$ make pre-commit            # prettier / ruff check / ruff format / biome — Passed

# The operator's lines, driven in-process (no Modal, no network):
INFO  chat knobs: max_tokens=4096 chat_template_kwargs={}
INFO  chat completion: {"city": "Tokyo", "population": 13960000}
INFO  JSON mode honoured: keys=['city', 'population']
INFO  unauthenticated health -> 401
INFO  Smoke test passed
INFO  chat completion: {"tokyo_facts": [{"city": "Tokyo", "population": 37}]}
INFO  JSON mode honoured: keys=['tokyo_facts']
WARNING prompt schema not followed (recorded, not gated): missing ['city', 'population'], unexpected ['tokyo_facts']
INFO  Smoke test passed

$ grep -c '"json_schema"' src/tree/models/{modal_llm,modal_server,modal_catalog}.py   # 0 0 0
$ grep -ci "strict.json" apps/memory/Makefile apps/memory/scripts/modal_model.py .agents/skills/run-pipelines-e2e/SKILL.md   # 0 0 0
$ git diff --numstat -- apps/memory/src/tree/models/modal_llm.py                       # 16  42
$ git diff --stat   -- apps/memory/deploy/modal_sglang_llm.py                          # 2 insertions(+), 2 deletions(-)
```

**Notes**
- The SGLang fact in `modal_llm`'s module docstring was REWORDED, not dropped, and the rewording is forced by the `grep -c '"json_schema"' -> 0` AC: the old sentence stated it as `ResponseFormat.type` is `Literal[…, "json_schema"]` / `sampling_params["json_schema"] = '{"type": "object"}'`, i.e. twice with the double-quoted key. It now reads "a `json_object` request is compiled into the permissive `{"type": "object"}` grammar", with the same `protocol.py` v0.5.18 :230 / :1068-1069 citation.
- `CHAT_RESPONSE_FORMAT` is shared BY REFERENCE while `chat_request_knobs` (a few lines below it) deep-copies. Deliberate: the AC pins `create(...)["response_format"] is CHAT_RESPONSE_FORMAT`, so a copy is forbidden — and neither sender mutates it (`_chat` builds a NEW outer dict with `|`, the OpenAI SDK only serialises it), whereas every caller of `chat_request_knobs` merges into what it gets back.
- `_city_facts` was RENAMED rather than kept: its contract inverted (`(content: str) -> tuple[str, int]` that raises, to `(parsed: dict) -> str | None` that never raises), so it is now `_prompt_schema_mismatch`, with the two gate verdicts moved into `_json_object`. No AC or grep names the helper.
- The recorded WARNING carries the REASON only, no excerpt: the whole completion is already on the `chat completion:` line directly above it, and a second copy would double the bad-completion text in the log.
- No `modal` command, no `scripts/modal_model.py` process, no `make memory-deploy-model*` target and no request to any `*.modal.run` / `*.modal.direct` URL was run. Both suite rails (the `TREE_MODAL_DRY_RUN` CLI door and the SDK-door tripwire) are untouched and green.
- NOT RUN — the `run-pipelines-e2e` skill's real pipelines (orchestrator rail: no pipeline runs in this task) and any live deploy/smoke test (task 141 round 3 owns those).
- `tests/unit/deploy/test_modal_deploy_scripts.py` and the in-container `WARMUP_PAYLOAD` are byte-unchanged; the #155 timeout / `max_retries=0` behaviour and the #156 empty-answer diagnosis are untouched, with their tests still green.

### [Tester] 2026-09-21 23:59 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`: 313 files already formatted; `make memory-lint-check`: All checks passed!; `make pre-commit`: prettier / ruff check / ruff format / biome — Passed)
- Unit tests: `make memory-tests` run 3 times (one command at a time, per rule) — run 1: 4054 passed / 2 failed (`tests/unit/models/test_prewarm.py::test_first_failure_cancels_the_sibling`, `::test_a_sync_ensure_warm_does_not_leak_the_sibling`, an `asyncio.all_tasks()` leaked-task assertion racing a lingering `PyMongoProtocol.read()` task from another module); run 2: 4056 passed / 0 failed; run 3 (final, reported below): 4056 passed / 0 failed. `PYTEST_ADDOPTS="tests/unit/models/test_prewarm.py -q" make memory-tests` in isolation: 16 passed / 0 failed. Diagnosis: pre-existing cross-test asyncio-task-leak flake, order/timing-dependent, in a file task 158 never touches (not in `git diff`) — not a regression from this diff. Flagged below as a follow-up candidate, not a blocker.
- Integration tests: N/A — no integration suite in this repo (CLAUDE.md); no live pipeline run.
- Warnings: 0 (ruff, biome, prettier, pre-commit all clean; no new warnings in the 4056-test green run)

**E2E adversarial pass** (all mocked / 127.0.0.1-only, per the absolute rules — no `modal` command, no `scripts/modal_model.py` process, no `*.modal.run`/`*.modal.direct` request; `OPIK_TRACK_DISABLE=true` set to avoid polluting the production Opik project, matching `tests/conftest.py`'s own rule)
- Happy path: collected all 15 AC-named node ids (`PYTEST_ADDOPTS="--collect-only -q <14 explicit node ids + the whole test_modal_model_script.py file>" make memory-tests`) → all resolve, 112 tests collected, 0 errors (PASS).
- Break path 1 (mutation / state edge — "called twice, does any path mutate the shared `CHAT_RESPONSE_FORMAT` dict?"): custom script started a real `http.server` on `127.0.0.1:<ephemeral>`, monkeypatched only `resolve_server_url`/`poll_health`/`served_model_id` (same technique as the unit suite's own `server` fixture), and ran a REAL `ModalLLM.generate_json` (real `AsyncOpenAI` SDK) twice, then a REAL `chat_smoke_test` twice, against that socket. Result: `CHAT_RESPONSE_FORMAT` byte-identical and same `id()` before/after all 4 calls; both wire bodies carry `response_format == {"type": "json_object"}` with `"json_schema"` absent from the whole JSON-dumped body; client body and smoke body agree byte-for-byte on `response_format` (PASS — confirms RULE-ON #4: no code path mutates the shared constant; `MappingProxyType` would be cosmetic hardening, not a required fix).
- Break path 2 (parity invariant — "do the client and the smoke test agree on the SAME completion?"): battery of 14 inputs run through `modal_llm._parsed_object` vs `modal_server._json_object`+`_prompt_schema_mismatch` directly: `""`, `"   "` (whitespace-only), `null`, `[]`, `"str"`, `123`, `{"a":1}`, `{}`, nested own-shape `{"tokyo_facts":[...]}`, a `<think>...</think>{...}` prefix, a ```` ```json ```` fenced block, the live huge-digit-run, duplicate keys, `population: true`. All 14 agree on verdict category (pass / not-json / not-object) — 0 disagreements (PASS — neither side strips `<think>` blocks or markdown fences, both use plain `json.loads` + `isinstance(dict)`, both bound excerpts to 200 chars).
- Break path 3 (malformed/hostile inputs via the existing suite, re-verified): `test_a_non_json_completion_fails_with_an_excerpt` now carries 2 rows (`<think>` prefix AND the live digit run `{"city":"Tokyo","population":1400000000000000000000`) — both bounded to a 200-char excerpt; `test_a_wrongly_typed_value_is_recorded_not_gated` covers `population-string`, `population-bool` (`True` is an `int` in Python — the bool/int trap), `city-int` — all recorded-not-gated with exactly one WARNING and `Smoke test passed` still logged (PASS).
- Break path 4 (import hygiene / fresh interpreter, credential-free `env -i`): `env -i PATH=$PATH HOME=$HOME uv run python -c "import tree.models.get_model, tree.memory.pipeline, tree.mcp.server; ..."` → zero modules named `modal`, `tree.models.modal_llm*`, `tree.models.modal_embedding*` in `sys.modules` (PASS — MCP boot never imports Modal).
- Secret scan (counts only): `grep -c "logger\.\(info\|warning\|error\|debug\)" ... | grep -i "bearer|token"` on `modal_llm.py` and `modal_server.py` → 0 matches in both (no log line composes a bearer/token).

**Acceptance criteria**
- [x] PASS — `test_json_mode_is_the_only_response_format` — node id collects; asserts `response_format == {"type": "json_object"}` and `is CHAT_RESPONSE_FORMAT` (`tests/unit/models/test_modal_llm.py:731-745`)
- [x] PASS — `test_generate_json_has_no_schema_keyword` — node id collects; `inspect.signature` parameters `["self","prompt","system"]`, `TypeError` on `schema=`, `openai.calls == []` (`tests/unit/models/test_modal_llm.py:1405-1418`)
- [x] PASS — DELETED not skipped — `grep -rn "test_a_schema_upgrades_the_request_to_strict_json_schema|test_a_schema_reaches_the_wire_as_strict_json_schema|@pytest.mark.skip" tests/unit/models/` → 0 matches; `test_the_body_carries_the_model_messages_temperature_and_format` collects and green
- [x] PASS — `test_it_posts_json_mode_with_the_schema_in_the_prompt` — node id collects; body `response_format == {"type":"json_object"}`, `"json_schema" not in json.dumps(body)`, prompt contains compact `json.dumps(CITY_FACTS_SCHEMA, ...)` (`tests/unit/models/test_modal_server.py:757-787`)
- [x] PASS — `test_wire_parity_between_client_and_smoke_test` extended — node id collects; asserts `smoke_body["response_format"] == call["response_format"] == CHAT_RESPONSE_FORMAT` on both seeds, plus my own live wire check (break path 1) confirms byte-identity independently of the mock
- [x] PASS — `test_the_happy_path_logs_seven_lines_in_order` — node id collects; line 5 is `JSON mode honoured: keys=['city', 'population']`, `report.keys`/`follows_prompt_schema`, zero warnings via `_warnings(caplog)`
- [x] PASS — `test_an_extra_key_is_recorded_not_gated` / `test_a_missing_key_is_recorded_not_gated` / `test_a_wrongly_typed_value_is_recorded_not_gated` — all 3 node ids collect and green; RETURN (no raise), `follows_prompt_schema is False`, exactly one WARNING prefixed `prompt schema not followed (recorded, not gated): `, `Smoke test passed` still logged
- [x] PASS — `test_a_nested_object_of_the_models_own_shape_passes` — node id collects; `keys == ["tokyo_facts"]`, one warning naming `missing ['city', 'population'], unexpected ['tokyo_facts']`
- [x] PASS — unchanged `ModelError` tests — `test_a_non_json_completion_fails_with_an_excerpt` (now 2 rows incl. the live digit run), `test_a_long_bad_completion_is_truncated`, `test_json_that_is_not_an_object_fails`, `test_an_empty_completion_fails`, both `test_an_empty_answer_*` — all node ids collect and green
- [x] PASS — `tests/unit/scripts/test_modal_model_script.py` — whole file collects (112 tests), `ChatSmokeTestReport` fixture built with `keys=[...]`/`follows_prompt_schema=...` (`tests/unit/scripts/test_modal_model_script.py:185-192`), green
- [x] PASS — `grep -c '"json_schema"' modal_llm.py modal_server.py modal_catalog.py` → `0 0 0` (ran verbatim)
- [x] PASS — `grep -c "strict JSON schema honoured" README.md modal_server.py` → `0 0`; `grep -c "one strict-JSON chat completion" README.md` → `0`; `grep -ci "strict.json" Makefile modal_model.py SKILL.md` → `0 0 0` (ran verbatim)
- [x] PASS — `git diff --stat -- deploy/modal_sglang_llm.py` → `4 ++--` = 2 insertions/2 deletions, comment-only (verified by reading the diff: only the `WARMUP_PAYLOAD` comment's one sentence changed, payload untouched); `tests/unit/deploy/test_modal_deploy_scripts.py` absent from `git diff --stat` output
- [x] PASS — `git diff --numstat -- modal_llm.py` → `16  42` (deletions > insertions)
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` — all 4 run one at a time, all green in the final pass (see Test summary)

**Evidence**
```
$ git status --short   # before AND after the adversarial script — exactly 12 files, no scratch left in repo
 M .agents/skills/run-pipelines-e2e/SKILL.md
 M apps/memory/Makefile
 M apps/memory/README.md
 M apps/memory/deploy/modal_sglang_llm.py
 M apps/memory/scripts/modal_model.py
 M apps/memory/src/tree/models/modal_catalog.py
 M apps/memory/src/tree/models/modal_llm.py
 M apps/memory/src/tree/models/modal_server.py
 M apps/memory/tests/unit/models/test_modal_llm.py
 M apps/memory/tests/unit/models/test_modal_server.py
 M apps/memory/tests/unit/scripts/test_modal_model_script.py
 M tasks/158-modal-llm-json-mode-only-and-smoke-test-parity.md

$ make memory-tests   # final run
======================= 4056 passed in 63.31s (0:01:03) =========================

$ grep -c '"json_schema"' src/tree/models/{modal_llm,modal_server,modal_catalog}.py
modal_llm.py:0  modal_server.py:0  modal_catalog.py:0

$ env -i PATH="$PATH" HOME="$HOME" OPIK_TRACK_DISABLE=true uv run python <adversarial script, 127.0.0.1 only>
[PASS] CHAT_RESPONSE_FORMAT unmutated after 2 client calls before={'type': 'json_object'} after={'type': 'json_object'}
[PASS] client wire body carries json_object, no json_schema (call 1) ...
[PASS] CHAT_RESPONSE_FORMAT unmutated after 2 smoke-test calls before={'type': 'json_object'} after={'type': 'json_object'}
[PASS] parity invariant holds across the whole battery disagreements=[]
TOTAL: 8 checks, 0 failed
ALL ADVERSARIAL CHECKS PASSED
```

**Other issues found**
- Pre-existing flake in `tests/unit/models/test_prewarm.py` (2 of 3 full-suite runs green, 1 run had 2 order-dependent failures; 16/16 green in isolation) — not caused by this diff, not in `git diff`, worth a follow-up ticket to make the asyncio-task-leak assertion robust to a lingering `PyMongoProtocol.read()` task from a neighboring test.
- `logger.info("chat completion: %s", content)` in `modal_server.py` (pre-existing, untouched by this diff) logs the FULL completion unbounded — if a model runs a 4096-token digit run (exactly the live failure this task's tests pin), that whole payload lands in the log/terminal. The thing that matters more (the `ModelError`/`ExtractionError` message an operator or a caller might display elsewhere) IS bounded to 200 chars in both `modal_llm.py` and `modal_server.py`, and `reasoning_content` is only ever reported by LENGTH, never by text (#156 intact). This INFO line is a smoke-test-only, human-facing nit (not shipped in a pipeline's hot path) — flagging for the SWE/PA as a possible follow-up (e.g. bound it to `_CONTENT_EXCERPT` too), not a blocker for this task.
- The SGLang `protocol.py` v0.5.18 docstring claim in `modal_llm.py`'s module docstring was reworded to drop the literal `Literal[...]`/`sampling_params[...]` syntax (forced by the `grep -c '"json_schema"' -> 0` AC) in favour of a more general, equally-hedged "compiled into the permissive grammar" claim, same line citation. `sglang` is not installed/vendored in this repo, so the claim could not be checked against the pinned source directly; the rewording is strictly less specific than what it replaced, not a stronger unverifiable claim, so this is a PASS with note rather than a defect.
- My own QA script's first run (before I added `OPIK_TRACK_DISABLE=true`) printed `OPIK: Started logging traces to the "tree-memory" project` — a self-caught methodology gap (the repo's `tests/conftest.py` sets this env var for exactly this reason; my ad hoc script initially didn't). No `.env` was read (credential-free `env -i`), and the corrected re-run with the var set reproduced identical PASS results with no such line. Noting for the record, not attributable to the SWE's diff.

**VERDICT: PASS**
