# Feature Plan: Voyage TEXT-embedding default + incremental scheduled "dream" consolidation

## Summary

Two coupled changes. **Part A:** re-introduce a dedicated Voyage *text* embedding
client (`/v1/embeddings`) and route the `voyage-3` family to it, then flip the YAML
default from `voyage-multimodal-3` to `voyage-3.5` (same 1024-d, so the vector-index
dim-guard stays satisfied; existing vectors become stale and the operator re-extracts).
**Part B:** add an async, Prefect-cron-scheduled "dream" pipeline that re-runs the
existing three-tier dedup across the whole knowledge graph to catch near-duplicate
nodes that parallel ingestion's inline write-time dedup missed. The dream is
**incremental** via a per-(user, job) watermark: it drives comparisons only from
nodes updated since the last successful run, but compares them against the full graph
(the two-set rule). Default behavior is the normal semantic + fuzzy dedup across all
node types; the LLM contradiction judge is behind a flag, OFF by default; the first
rollout default is `dry_run=True`.

This feature reuses, does not reinvent: `dedupe_entity` (read-only three-tier
decision), `review_duplicate(CONFIRM, reviewed_by="dream")` (idempotent merge applier),
the pending-SAME_AS upsert path, `resolve_supersessions` (LLM judge), and the existing
Prefect `serve(...)` deployment registration.

## Tasks (in order)

1. **#048** — Voyage TEXT embedding client + model-id routing + voyage-3.5 YAML default — re-add `VoyageTextEmbeddingModel` (`/v1/embeddings`, flat `{"input":[...]}` payload, `ExtractionError.status_code` discriminator); route `voyage-multimodal-*`→multimodal, everything else→text in `get_model.py`; flip `default.yaml` + `EmbeddingConfig` default to `voyage-3.5`/1024. *(No deps.)*
2. **#049** — Vector-space-swap migration runbook — document (not script) that the same-dim model swap leaves vectors stale and the dim-guard won't catch it; recovery is `RESET_ONTOLOGY=1` re-extraction. **Authors** the `[!CAUTION]` runbook section in `CLAUDE.md` (it is NOT already present in this worktree — see Open Questions). *(Depends on #048.)*
3. **#050** — `knowledge_graph_meta_state` collection + watermark helpers — one doc per `(user_id, job)` with `_id="{user_id}:{job}"`; `load_watermark` (missing⇒epoch) + `record_dream_run` (writes `last_run_at = run_start`, no-gap semantics). *(No deps; independent of #048.)*
4. **#051** — Dream consolidation flow (incremental sweep + auto-merge/flag + audit) — `dream_consolidation(user_id, *, dry_run=True)`; the two-set rule (driving set = `updated_at > last_run_at`; search space = full graph); `id1<id2` + skip-existing-SAME_AS; merge via `review_duplicate(CONFIRM)`, flag via pending edge; `DreamConfig` block; dry-run = report-only + no watermark advance. *(Depends on #048, #050.)*
5. **#052** — Supersession-judge flag + scheduled per-user fan-out deployment — `sweep_supersession` gated on `dream.enable_supersession_judge` (default false, no LLM when off); `dream_consolidation_all_users` fan-out over active users; register `dream-consolidation-etl` with `cron` in `orchestrator.py`; `make memory-run-dream-consolidation`. *(Depends on #051.)*
6. **#053** — End-to-end acceptance (incremental watermark proof) — parallel-ingest two near-duplicates for the Paul Iusztin user, run dream→assert collapse + watermark advance, run AGAIN→assert near-noop; adversarial paths (dry-run, rejected-pair respect, empty delta, idempotency, cap, no-LLM default); free-tier-safe (read-only sweep / fake embedder). *(Depends on #048–#052.)*

## Cross-task dependencies

- **#048** and **#050** are independent and may land in either order; BOTH precede **#051**.
- **#049** depends on **#048** (it documents the swap #048 introduces) and precedes the e2e (#053).
- **#051** depends on **#048** (the text client / default) and **#050** (the watermark substrate).
- **#052** depends on **#051** (it extends the flow + wires the deployment).
- **#053** depends on all of **#048–#052**.

## Out of scope (intentional)

- **Removing/refactoring the multimodal client** — #038's consolidation is only *partially* reverted; `VoyageMultimodalEmbeddingModel` stays for `voyage-multimodal-*` blocks. We keep BOTH clients.
- **A migration *script*** for the vector-space swap — the existing `RESET_ONTOLOGY=1` migration already does the re-extraction; #049 documents it, no new script.
- **Multi-hop transitive SAME_AS clustering** in one dream run — the sweep resolves single-hop per run and converges over successive runs (`get_same_as_cluster` is single-hop by design). Acceptable and documented; not expanded here.
- **Separate dream-specific dedup thresholds** — thresholds are reused from `extraction.dedup`; the dream does NOT introduce its own.
- **Re-embedding inside the dream sweep** — the sweep is read-only over stored vectors; it never calls Voyage. (Fixture creation in #053 may embed a tiny set.)
- **Image/multimodal inputs to the text client** — text-only; multimodal stays on its own client.
- **Auth / multi-user provisioning** — fan-out enumerates existing `User` docs; no new auth wiring.

## Key risks (captured in the task specs)

- **Concurrency with live ingestion.** A node ingested *during* a long dream run could be missed. Mitigated by writing `last_run_at = run_start` (the run's START), not completion time — such nodes are simply re-driven next run (idempotent overlap, never a gap). (#050, #051)
- **Transitive SAME_AS clusters.** A 3-way duplicate cluster resolves single-hop per run; it converges over successive nightly runs rather than in one pass. Documented as acceptable. (#051)
- **First run after the #048 migration is a full sweep.** Missing watermark ⇒ epoch ⇒ every node is in-delta. Expected and bounded by `max_pairs`. (#050, #051)
- **Free-tier Voyage limits (3 RPM / 10K TPM) for live e2e.** The dream sweep itself is embedding-read-only (zero Voyage calls); only fixture creation embeds. #053 keeps live embedding to a tiny set or uses a deterministic fake embedder. (#053)
- **Silent vector-space staleness after #048.** The dim-guard cannot detect a same-dimension model swap. The #049 runbook is the only signal; if operators miss it, search/dedup degrade quietly. (#049)
- **Payload-shape regression.** The text endpoint needs the flat `{"input":[...]}` shape; reusing the multimodal nested shape 400s. Covered by a #048 unit test asserting the exact body + URL.

## Operator-decision points to surface at the Step-3 approval gate

1. **`voyage-3.5` vs `voyage-3` as the new default.** The spec proposes `voyage-3.5` (1024-d, same dim as today). Confirm, or pick `voyage-3` / another 1024-d text model. (Anything that changes `dimensions` would break the dim-guard and require a vector-index rebuild — out of scope.) (#048)
2. **Cron schedule.** Default `0 4 * * *` (04:00 UTC daily). Confirm the time/zone, or change. (#052)
3. **Per-user fan-out vs single-loop flow.** The plan uses a parent fan-out flow (`dream_consolidation_all_users`) calling the per-user flow once per active `User`, keeping watermark + cost tenant-scoped. Confirm this over a single in-flow loop. (#052)
4. **First-rollout `dry_run` default.** The `dream.dry_run` YAML default is `True` (report-only, no writes, no watermark advance) for a safe first deploy. Confirm we want the scheduled cron to run in dry-run until manually flipped. (#051, #052)
5. **`enable_supersession_judge` default.** Default `False` (no LLM on the default path). Confirm the LLM contradiction judge stays opt-in. (#052)
6. **`max_pairs` cap.** Default `10000` pairs/run. Confirm this bound for the first-run full sweep. (#051)
7. **CLAUDE.md runbook authorship (discrepancy).** The feature spec assumes a `[!CAUTION]` vector-space runbook already exists in `CLAUDE.md`; it is **NOT present in this worktree** (only the #036 *dimension*-mismatch runbook exists, and that's on another branch). #049 will **author** it here. Confirm this is the intended CLAUDE.md, or point us at the canonical one to extend instead.

## Notes

- **Documentation discipline:** this project has neither `docs/adr/` nor
  `docs/glossary.md`, so no ADRs/glossary entries are produced. (#049 authors a
  `CLAUDE.md` runbook section, which is the project's documentation home.)
- **Task numbering:** the tracker's highest existing number is `047` (`046`
  was skipped); this feature uses `048`–`053`.
- **A correction baked into #048:** the feature spec frames `embed_in_batches` /
  `_sanitize_for_embedding` / `_embed_chunk_resilient` as resilience "on the
  client." In this codebase they live at `tree/memory/embedding_text.py` and wrap
  ANY `BaseEmbeddingModel`, so the new text client inherits them for free once it
  implements `embed()` + the `status_code` discriminator. #048 does NOT duplicate
  that layer; it adds a composition test instead.
