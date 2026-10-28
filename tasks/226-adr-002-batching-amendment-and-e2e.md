---
id: 226-adr-002-batching-amendment-and-e2e
status: pending
feature: data-batching
---

# Amend ADR-002 for uniform `data.batch_size` chunks and verify chunking end-to-end

Tags: `memory`, `data`, `docs`, `adr`, `e2e`
Depends on: 223, 224, 225
Blocks: —
Implements: ADR-002 amendment #078–#082 (refined).

## Problem

ADR-002's batch-grain amendment defines a Batch as "HuggingFace `batch_size`-chunk / one feed's
entries / the whole handed-in URL list" and promises a "small constant" task-run count per
shard. After tasks 223–225 a Batch is a `data.batch_size` chunk on every platform, in-flight I/O
is capped by `data.concurrency`, and task runs scale as phases × ⌈N / batch_size⌉.

## Scope

1. Append an amendment to `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md`
   (Status stays `Accepted`, same precedent as #078–#082): Batch = `data.batch_size` (500) chunk
   everywhere; `gather_isolated` caps in-flight work at `data.concurrency` (50); the task-run
   count is ≈ phases × ⌈N / 500⌉ (good: "100,000 Substack items → 200 chunks → ~400 task runs";
   bad: "scales well"); the YouTube Bright Data collection is per chunk, with the timing measured
   in task 224.
2. Grep `docs/` and `apps/memory` READMEs for the old wording and fix it.
3. E2E via the `run-pipelines-e2e` skill on the local env, with
   `TREE_DATA__BATCH_SIZE=3 TREE_DATA__CONCURRENCY=2` and a sources file holding ≥7 items of
   Substack and of Web (and ≥4 YouTube videos) — so chunking executes without paying for 500+ items.

## Acceptance criteria

- [ ] ADR-002 amendment present with the numbers above; no doc still describes "the whole
      handed-in URL list" as the batch.
- [ ] E2E run: Prefect shows `extract-web-batch` / `load-web-batch` / `load-substack-batch` /
      `load-youtube-batch` runs matching ⌈items / 3⌉ per platform; worker logs show ≤2 in-flight
      requests; Mongo document counts equal the sources' item counts (minus logged failures).
      Record commands, run ids and counts in the log.
- [ ] Re-running the same e2e inserts 0 new documents (idempotency).
- [ ] Cadence green (memory targets, local env).

## Log

### [Orchestrator] 2026-10-28 — Filed
