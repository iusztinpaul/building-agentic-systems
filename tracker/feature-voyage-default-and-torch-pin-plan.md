# Feature Plan: Voyage-3 default + torch pin + config-discipline migration (fresh-deploy fix)

## Summary

A fresh `/night`-style deploy of the memory app on Py3.14 + macOS arm64 crashes twice in a row: (1) `assert_settings_match_live_vector_index` hard-fails because `settings.embedding_dim=1024` (pinned to `voyage-3`) disagrees with the live mongot `vector_index` (built at 384 from the YAML's `MiniLM-L6-v2` dev override); (2) any path that touches torch shared memory raises `RuntimeError("no response from torch_shm_manager")` because torch 2.10.0's bundled `torch_shm_manager` binary SIGABRTs on Py3.14 + arm64.

This feature aligns the YAML embedding default with the production pin, pins a working torch version with a regression sentinel, documents the one-time mongot rebuild for upgrade operators, and proves both fixes end-to-end against the live HF arxiv ingestion.

**Operator revision (post-Step-3 approval feedback):** #034 expanded from "flip three YAML keys" to a project-wide config-discipline refactor. The operator's rule, now codified in `CLAUDE.md`: **YAML is the source of truth for behavior configuration; `.env` is exclusively for credentials and per-environment infrastructure endpoints.** `settings.py`'s `DedupConfig` (env-prefix `DEDUP_*`) and `embedding_provider/model/dim` fields are removed; their values move to the YAML schema in `app_config.py`. `.env.example` is trimmed to a credentials wallet. Ops keep an opt-in escape hatch via the existing `TREE_<SECTION>__<KEY>` env-override mechanism in `app_config._apply_env_overrides`, but `.env.example` does not document those keys. **#034 is now a substantial refactor** (touching every call site of `settings.dedup.*` and `settings.embedding_*`), not a three-line YAML edit — the headline acceptance criterion is exhaustive call-site migration.

## Tasks (in order)

1. **#034** — Migrate behavior config to YAML; keep `.env` for credentials and infra only — flips `apps/memory/configs/default.yaml`'s `models.embedding` block to `voyage / voyage-3 / 1024`, extends `extraction.dedup` with `supersession_candidate_cap`, **removes `DedupConfig` and `embedding_*` from `settings.py`**, rewrites every call site to read from `app_config`, trims `.env.example` to credentials + infra, and adds a `## Configuration` subsection to `CLAUDE.md` codifying the rule. Substantial refactor — see the task file for the full migration table.
2. **#035** — Pin a working torch version for Py3.14 + macOS arm64 — adds an explicit top-level `torch` pin in `apps/memory/pyproject.toml` (depth-first downgrade from 2.10.0 until `torch.tensor(0).share_memory_()` exits clean); regenerates `uv.lock`; adds a regression integration test. Escalates `USER ACTION REQUIRED` if no compatible torch version exists. **Independent of #034**; no overlap.
3. **#036** — Document the mongot vector-index rebuild path — depends on #034; adds a `CLAUDE.md` sub-section with the exact `mongosh dropSearchIndex("vector_index")` + re-trigger-indexing recipe. The grep anchor `Embedding dimension mismatch` is preserved verbatim across #034's refactor; the runbook quotes the full error string (now sourced from `app_config.models.embedding.dimensions` rather than `settings.embedding_dim`).
4. **#037** — End-to-end acceptance run — depends on #034, #035, #036; runs the full CLAUDE.md verification step 5 chain (serve → data-pipeline w/ HF arxiv live → extraction → indexing → query-graph) AND verifies the new config-discipline rule end-to-end (`.env` contains no behavior knobs; YAML drives the run; `TREE_` override works).

## Cross-task dependencies

- #034 must land before #036 (the runbook references the exact error string `assert_settings_match_live_vector_index` raises after #034's refactor; #034 changes the error message's left-hand side from `settings.embedding_dim` to `app_config.models.embedding.dimensions` but preserves the `Embedding dimension mismatch` anchor).
- #034 and #035 are independent and can land in either order, but BOTH must land before #037.
- #036 must land before #037 (the e2e acceptance walks the operator narrative, which includes the runbook reference for upgraders).

## Out of scope (intentional)

- **Refactoring `VoyageMultimodalEmbeddingModel`** — the existing code path already accepts `voyage-3` as the `model` string and routes it through the `/v1/multimodalembeddings` endpoint. Cleaning up the multimodal-vs-text endpoint naming is a separate hygiene task.
- **Removing `sentence-transformers` as a dependency** — sentence-transformers still provides a useful local-dev/mock provider. We only flip the YAML default; the provider stays for tests and overrides.
- **Bumping `requires-python`** — #035 explicitly forbids the SWE from silently downgrading Python; if no torch wheel exists for Py3.14 arm64, escalate to the human instead.
- **A Makefile target for "rebuild-vector-index"** — `ensure_indexes` already handles drop + recreate when it detects a dim mismatch, so re-running `make memory-run-memory-pipeline-indexing` is the canonical recipe. #036 documents that, no new target needed.
- **Modal-served voyage-3 (`ModalEmbeddingModel`)** — out of scope; the production cutover targets the direct Voyage AI HTTP API.
- **Migrating MongoDB connection settings into YAML** — the operator's rule explicitly classifies `MONGO_*` and `MONGOT_PORT` as infra endpoints. They stay in `.env`. Same for `PREFECT_PORT`, `PREFECT_API_URL`, `BRIGHTDATA_*_ZONE`.
- **Backporting old `DEDUP_*` env vars** — #034 retires the `DEDUP_` env_prefix entirely. Operators with `DEDUP_*` in their CI envrc files must migrate to `TREE_EXTRACTION__DEDUP__*`. This is a breaking change for ops, called out in the plan summary and the task log so the rollup PR description can cite it.
- **Documenting the `TREE_<SECTION>__<KEY>` override in `.env.example`** — explicitly NOT done; the `.env.example` stays a credentials wallet. The override path is documented in `CLAUDE.md` only.

## Operator-decision points (to highlight at Step 3 human-approval)

1. **Breaking change for ops who set `DEDUP_*` or `EMBEDDING_*` in their CI env.** After #034, those variables are silently ignored. Operators with `DEDUP_AUTO_MERGE_THRESHOLD=0.97` in their CI must migrate to `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.97`. Operators with `EMBEDDING_*` in their env must edit `apps/memory/configs/default.yaml` directly. The task log will paste a copy-pasteable migration table for the rollup PR description; the human should confirm they are aware of this break before approval. If there are operators in flight today using these env vars, the human may want to coordinate the cutover.
2. **Removal of `_warn_on_embedding_dim_mismatch`.** Today, on every app-config load, the codebase logs a WARNING if YAML disagrees with `settings.embedding_dim`. #034 removes that helper because the invariant now lives entirely at the indexing-layer assertion. The trade-off: WARNINGs at boot are noisier but earlier; the indexing-layer assertion crashes harder but later. The plan picks "later, harder" because (a) `.env` no longer carries the dim and (b) `assert_settings_match_live_vector_index` already exists and is exercised by tests. If the operator wants to retain the boot-time WARNING (in addition to the indexing assertion), say so at approval.
3. **Torch pin escalation risk (#035).** Unchanged from the previous round: if no published torch ships a working Py3.14 + macOS arm64 wheel that also satisfies `sentence-transformers>=4.0`, the SWE will STOP and surface four options to the human. Default assumption: a 2.x torch exists; if true, no escalation.
4. **Mongot rebuild affects existing data (#036).** Unchanged: any operator with a 384-d `vector_index` will need to drop + re-create it, which temporarily wipes `$vectorSearch` results until mongot reconverges (~30-90s). Documented but not gated.
5. **HF arxiv source stays live (#037).** Unchanged: per the operator's brief, the new branch starts with the HF arxiv source enabled in `default.yaml`. The acceptance demo requires it to ingest ≥1 doc end-to-end.

(No `docs/adr/` or `docs/glossary.md` discipline applies — this project uses `docs/adrs/` for ADRs and has no glossary file; the plan does not author new ADRs because the architectural decisions in play (`voyage-3` as the pinned model, YAML-as-config-source-of-truth) extend existing settled decisions rather than fork them. The "YAML for behavior config; `.env` for credentials and infra" rule is codified in `CLAUDE.md` per the operator's explicit ask, which is the project's rulebook for future contributors.)

## Open questions

(None — the operator's revision is unambiguous. Risks and the breaking-change for ops are documented in the operator-decision points above so the human can confirm at re-approval time.)

### [PM] 2026-05-19 — Acceptance Review

**VERDICT: ACCEPT**

Reviewed all four task done-files, the cross-task diff (`main..HEAD`), the
CLAUDE.md changes (Configuration rule + TMPDIR shim section + voyage-3
runbook), and the Tester's evidence from #037. Every operator-visible
AC verified from a fresh-deploy operator's POV:

- Both headline bugs dead with grep-discoverable runbooks (CLAUDE.md:107,
  118, 395). Error-text anchors preserved verbatim.
- Config discipline rule codified, `.env.example` is a credentials wallet.
- Voyage routing fix (#037 inline) is contract-preserving, bi-directional,
  live-API-verified, well-tested. The SWE's inline-fix-vs-rollup call was
  correct given the #037 happy-path AC depended on it.
- No dependency drift (`pyproject.toml`/`uv.lock` empty diff).
- Pre-existing SERP flakes are genuinely pre-existing (identical on main).

Non-blocking notes for PR description: feature-plan file staging,
stale `tree-prefect-worker` docker container, SERP-flake hardening,
override-surface ergonomics. None gate the squash-merge.

SWE may proceed to push / squash / PR Reviewer / On-Call gates.
