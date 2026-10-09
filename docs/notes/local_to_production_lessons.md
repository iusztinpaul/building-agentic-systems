# Local → production: what the first prod memory backfill taught us

Retrospective of the 2026-10-02 → 2026-10-03 run that took the memory pipeline from "green on local
Docker" to "green on Prefect Cloud + Atlas M0". It took five prod runs. Each failure exposed a gap
between the two environments that no unit test and no local e2e run could see.

| Run | Code | Outcome |
|---|---|---|
| A — first backfill | YAML ignored (graphrag), list vectors | data OK; extraction `AutoReconnect` after 17,569 rows; flow **Completed** |
| B — retry of A | same | Gemini `API_KEY_INVALID` (graphrag LLM call); flow **Completed** |
| C — after the config fix | rag, list vectors, one `bulk_write` | `AutoReconnect` after 17,569 rows again; flow **Completed** |
| D — after binData + fail-loud | rag, binData, one `bulk_write` | `AutoReconnect`, **0 rows**; flow **Failed** with `PartialIngestError` |
| E — after batched loads | rag, binData, batched `bulk_write` | 37,557 rows, exit 0 |

Companion notes: `mongodb_vector_storage_and_quantization.md` (the storage arithmetic),
`prefect-execution-topologies.md` (how a managed run reaches a container),
`deployment-runbook.md` (the ordered bring-up).

---

## TL;DR

| # | Symptom on prod | Root cause | Fix | Lesson |
|---|---|---|---|---|
| 1 | CD red for hours, `offline-pipeline` deployment missing | Prefect Cloud API key expired → `401` on every CD run | New key in `.env.prod` + GitHub secret, `make memory-deploy-prefect` | A red CD is invisible unless someone looks; deployments silently go stale |
| 2 | Worker logs `Processing … in graphrag mode`, YAML says `rag` | Managed run does a NON-editable `pip install`; `default.yaml` lived outside the package, the loader fell back to code defaults SILENTLY | `8b24867`: `default.yaml` bundled into `tree.config`; a missing config file raises | Anything read relative to `__file__` must be package data; test from an installed wheel |
| 3 | Gemini `API_KEY_INVALID` mid-extraction | Only reached because of #2 (graphrag calls the LLM; rag does not) | Key still to rotate; irrelevant in rag | A wrong-mode run fails on dependencies the right mode never touches |
| 4 | Shard failed, flow **Completed**, `make` exit 0 | Per-user / per-shard isolation logged failures and then forgot them | `8b3034c` (+ `af445a7`): any partial ingest raises `PartialIngestError` after all phases | Isolation without aggregation hides failures |
| 5 | Load died at the same row count twice; diagnosed as the 512 MB cap | Partly real: `sample_mflix` 128 MB + 13 KB/vector arrays would overflow M0 | `f60f85d`: float32 `binData` (≈3.2× smaller); `sample_mflix` dropped (`0606a99`) | Worth doing — but it was NOT why the load failed |
| 6 | Run D: `AutoReconnect`, **0 rows written**, DB at 18 MB | ONE `bulk_write` of ~37k upserts (~200 MB) ran ~6 min on M0 until the server closed the connection | `29eae41`: ~500-op batches of whole documents, retried with backoff | Bound every write command; local Docker Mongo has no throughput limit |

Final prod state (run E, `cooperative-lion`, 2026-10-03 09:03 UTC, exit 0):

| | Value |
|---|---|
| `documents` | 12,122 (10,081 with content) |
| Documents in memory | 10,081 / 10,081 eligible |
| `memory` rows | 37,557 = 10,081 document + 10,170 parent + 17,306 child |
| Child embeddings | 17,306 `binData` float32, 0 pending; child row ≈ 5.7 KB |
| `vector_index` | READY, queryable; `make memory-search` top vector score 0.837 (same as local) |
| `tree` on disk | 260.7 MiB of the 512 MB M0 cap (data 135.5, storage 112.6, indexes 148.1 MiB) |
| CD | green again |

---

## 1. Credentials expire, and CD failures are silent

The Prefect Cloud API key had expired. Every CD run since the afternoon failed with
`401 Invalid authentication credentials`, so the `offline-pipeline` deployment introduced by ADR-007
was never registered on Prefect Cloud. Prod still had only the old `data-etl-*` deployments.

How the pieces actually move (verified in `orchestrator.py` + `.github/workflows/cd.yml`):

- **Code** reaches a run at run time. Each managed run clones the commit CD pinned — the last one
  that passed CI on `main` (`git_clone`, ~1 s; it cloned `main` itself until 2026-10-09), then
  `pip install ./apps/memory` (~75 s). A push to `main` is live on the next run only once its CD is
  green.
- **Deployment definitions** (names, schedules, parameters, env, pull steps — the pinned SHA
  included) reach Prefect Cloud ONLY through CD (`deploy/prefect_pipelines.py`, after CI passes on
  `main`). A red CD freezes them, and with them the code.

Lesson: check `gh run list -w cd.yml` before a prod run, and treat a red CD as a prod incident, not a
CI nuisance.

## 2. Non-editable installs break `__file__`-relative paths — silently

`app_config.py` resolved `configs/default.yaml` as `Path(__file__).parents[3] / "configs"`. In a
checkout or an editable install, that is `apps/memory/configs/`. After the managed run's plain
`pip install`, `__file__` sits in `site-packages`, the path points nowhere, and `load_app_config`
quietly returned `AppConfig()`. That put prod on the code default `graphrag`, plus every other knob
at its code default. The Docker `prefect-worker` had the same bug, because its image copied `src/`
and never `configs/`.

Fix (`8b24867`): the YAML moved into the package (`src/tree/config/default.yaml`), so hatch ships it
in the wheel, and a missing config file now raises `FileNotFoundError`.

Lessons:
- A config fallback that hides a missing file is worse than a crash. The crash costs one red run; the
  fallback cost a whole backfill in the wrong mode.
- Verify packaging the way prod installs it: build the wheel, unzip it outside the checkout, import,
  and read a value (`mode = rag`).
- The local e2e skill runs `make memory-serve-workflows` from the checkout, so it can never catch this
  class of bug.

## 3. A wrong mode fails on the wrong dependency

The graphrag run died on `Gemini … API_KEY_INVALID`. Rag never calls an LLM during ingest, so the
invalid key was a symptom of #2, not its own incident. Chasing the key first would have masked the
mode bug. Lesson: when prod fails on something the configured mode should never touch, suspect the
configuration before the dependency.

## 4. Failure isolation needs failure aggregation

Every level isolated failures correctly (per item, per shard, per user) and then dropped them. The
coordinator logged `extraction: … succeeded=0 failed=1` and returned normally. Prefect marked the run
**Completed**, and `make` printed `Done. Flow completed successfully.` with exit 0, over a backfill
that had loaded 4,376 of 10,081 documents.

Fix (`8b3034c`):
- Every phase still runs for every user and shard. Indexing still embeds whatever landed.
- After the last phase, `offline_pipeline` sums data shard failures, data item failures (counted
  through `gather_isolated`), extraction shard failures and per-user indexing/clustering exceptions.
- It then raises ONE `PartialIngestError` that names each failure. The run ends **Failed** and the CLI
  exits non-zero.
- `af445a7` makes the CLI print that error once instead of three times.

This is the change that made the rest of this note debuggable: run D failed loudly and named the
failing shard and its error.

Trade-off: a transient item failure (one feed timing out) now fails the nightly run. That matches the
"any partial ingest is an error" rule. Expect noisier alerts.

## 5. A plausible diagnosis is not a verified one

After runs A and C died at the same row count (17,569 rows), we blamed the M0 512 MB cap. The evidence
was real:
- `sample_mflix` held 128 MB.
- A 1024-dim vector stored as a BSON array of doubles costs 13.2 KB.
- The full corpus extrapolated to ~610 MB storage + ~120 MB indexes.

We shipped float32 `binData` (`f60f85d`, see the storage note) and dropped `sample_mflix`.

Run D still failed, with **0 rows** written and the database at **18 MB**. That one observation
falsified the cap theory in seconds. It was available before the fix: an `estimatedDocumentCount()`
right after run C, plus `db.stats()`, would have shown how much headroom was actually
left (~463 MB on disk in total, `sample_mflix` included — the load died below the cap,
not at it).

Lessons:
- Before building a fix, name the observation that would prove the hypothesis wrong, and check it.
- The storage work was still worth it. Prod ended at 260.7 MiB on disk; the same corpus as arrays
  projects past the cap. It was the right change for the wrong incident.

## 6. Managed free tiers throttle; local Docker does not

The real cause: `load_rag_rows` wrote every row of the run in ONE `bulk_write(ordered=False)`. That
was ~37k pipeline upserts and ~200 MB for the prod corpus. Local Docker Mongo absorbs that in
seconds; the local rehearsal of the full corpus finished in 4m49s. Atlas M0 throttles throughput, so
the single command ran ~6 minutes (19:29:09 → 19:35:35 in run C's task log) until the server closed
the connection. Prefect's task retries replayed the same giant command and failed the same way.

Fix (`29eae41`): rows go in ~500-op `bulk_write` batches (~3 MB each), each retried with backoff on
`AutoReconnect` / `NetworkTimeout`. Batches hold WHOLE documents. A document counts as ingested as
soon as ANY of its rows carries it in `sources`, so a batch that died halfway through a document
would mark it done with rows missing, and the next run would never pick it up again. Prod then loaded
steadily at ~8k rows/min: 11,657 → 19,804 → 28,231 → 36,229 → 37,557.

Lessons:
- Bound every write command, by op count and bytes, never by "the whole run". A command that is fast
  locally can be minutes long on a throttled tier.
- Batch boundaries must follow the unit of idempotency and of "done". Here that is the document, not
  the row.
- A rehearsal on local Docker proves correctness and size, not throughput. To prove throughput,
  rehearse against a tier with the same limits as prod (a scratch M0 cluster or a separate Atlas
  project).

## 7. Measure size on the target, not locally

| | Local (full corpus) | Atlas M0 (same corpus) |
|---|---|---|
| `memory` data | 116.6 MiB | 116.6 MiB |
| `memory` storage | 98.0 MiB | 97.0 MiB |
| `memory` indexes | 74.3 MiB | 145.8 MiB |
| `tree` on disk | ~197 MiB | 260.7 MiB |

Data and storage matched. Indexes on Atlas came out about twice the local size. The text index
dominates both, and the freshly built Atlas indexes are not compacted. The mongot vector index is not
in either number. Budget for the target's numbers, not the rehearsal's.

## 8. Smaller things

- **12,122 documents, 10,081 in memory.** The other 2,041 have no `content` (failed scrapes or
  transcripts at ingest time), so memory never selects them. This is a data-phase gap: re-scrape
  them, or prune them.
- **Prod-destructive commands stay human.** The agent permission classifier blocked every prod drop
  (`dropDatabase`, the mode reset) and the attempt to remove the reset's confirmation step. The human
  ran those commands. The reset guard ended up simpler (one `CONFIRM=yes` on every target, dry run by
  default, PROD warning in the dry run) without losing the dry run.
- **Run-time `git clone`** of the pinned SHA means a green push lands mid-backfill in every worker that
  starts after its CD re-pins the deployments. Hold merges to `main` if a long backfill must run one
  commit end to end.

---

## Pre-flight checklist for the next prod run

1. `make memory-deploy-prefect-setup-status` lists every deployment the command dispatches, each
   at `ref=commit:<sha7>` of the commit you expect, with no ORPHAN line. A green CD alone does not
   prove a deploy: CD skips (green) when its commit is no longer the tip of `main`.
2. Config comes from the package: the worker log's first lines show the expected `memory.mode`
   (`Processing N documents in <mode> mode`).
3. `make env-status` shows `prod`; `db.stats()` + `listDatabases` give headroom against the tier's cap
   (indexes included, at the target's size).
4. Use dry runs for anything destructive (`make memory-reset-mode`), and read the `env target:` line
   before `CONFIRM=yes`.
5. Watch the row count on prod while the run executes (`estimatedDocumentCount()` every minute):
   Prefect streams only the parent run's logs, and a steady climb is the batching working.
6. A non-zero exit or `PartialIngestError` is the run's final word. Re-run the memory pipeline: it
   picks up only pending documents.
