# MongoDB bulk writes on Atlas M0 — batch by whole documents, retry each batch

Note on the fix that unblocked the first prod memory backfill (commit `29eae41`, "fix: Load rag rows
in retried batches of whole documents"). The broader retrospective is
`local_to_production_lessons.md`; the storage side is `mongodb_vector_storage_and_quantization.md`.

---

## TL;DR

| | Before | After |
|---|---|---|
| Write shape | ONE `bulk_write(ordered=False)` per run | ~500-op `bulk_write` batches, in sequence |
| Prod corpus (10,081 docs) | ~37.5k upserts, ~200 MB in one command | ~76 batches of ~3 MB |
| On Atlas M0 | ~6 min, then `AutoReconnect … connection closed`, **0 rows** | ~8k rows/min, **37,557 rows**, exit 0 |
| On a dropped connection | Prefect task retry replays the whole giant command | that batch retries (2 s, 4 s backoff), earlier batches stay |
| A failed batch leaves | an unknown subset of rows | whole documents written or whole documents pending |

---

## The failure

`load_rag_rows` (`apps/memory/src/tree/memory/rag/load.py`) flushed every document, parent and child
row of an extraction run in ONE `bulk_write(ordered=False)`. On local Docker Mongo that is fast: the
full-corpus rehearsal finished in 4m49s with 0 failures. On prod the same command:

- ran from 19:29:09 to 19:35:35 (run C's `load-rag-rows` task log), then failed with
  `AutoReconnect: …shard-00-01…: connection closed (configured timeouts: connectTimeoutMS: 20000.0ms)`;
- after the switch to float32 vectors (run D), failed the same way with **0 rows** in `memory` and the
  whole `tree` database at 18 MB, which ruled out the 512 MB storage cap;
- got replayed whole by Prefect's task retries (`Retries are exhausted`), so each retry hit the same
  wall.

Atlas M0 is a shared tier with throttled throughput. A single command that runs for minutes there is
at risk of being cut. Local Docker has no such limit, so no local test can reproduce this.

## The fix

Three pieces, all in `apps/memory/src/tree/memory/rag/load.py`:

1. **`batch_document_ops(documents_ops, batch_ops=LOAD_BATCH_OPS)`** packs the per-document op lists
   (one `build_rag_row_ops` list per document) into batches of about `LOAD_BATCH_OPS = 500` ops. It
   never splits a document. A document larger than the target gets a batch of its own.
2. **`_write_batch(collection, ops)`** is one `bulk_write(ordered=False)`, retried up to
   `LOAD_RETRIES = 3` times on `AutoReconnect` / `NetworkTimeout`, with backoff
   `LOAD_RETRY_BASE_DELAY_S × 2^(attempt-1)` (2 s, then 4 s). Each retry logs a warning. The last
   failure re-raises.
3. **`load_rag_rows(database=…, documents_ops=…)`** writes the batches in sequence, logs
   `load_rag_rows: batch N/M written (R rows so far)`, and returns the total rows written. The caller
   (`_load_rag_rows` in `memory/pipeline.py`) now passes one op list per document instead of one flat
   list.

### Why batches hold WHOLE documents

A document counts as ingested as soon as ANY `memory` row carries its id in `sources`; that is how
the coordinator resolves pending documents. If a batch boundary split a document and the second
batch failed, the document would look done with half its rows missing, and no later run would ever
pick it up. With whole-document batches, a failure leaves each document either fully written or
fully pending. Re-running the memory pipeline then picks up exactly the pending ones.

### Why retrying is safe

Every op is an upsert on a deterministic `_id` (`build_rag_row_id` over `source_uri` + position). A
batch the server partly applied before the connection dropped is rewritten on retry, not duplicated.

### Why ~500 ops

At ~5.7 KB per child row, 500 ops is ~3 MB per command: seconds on M0, small next to MongoDB's 48 MB
wire-message limit, and ~76 round-trips for the whole prod corpus. The value is a module constant,
not YAML config. Raise it on a dedicated tier if the round-trips ever dominate; lower it if a
throttled tier still drops connections.

## Evidence on prod

Run E (`cooperative-lion`, 2026-10-03), rows counted with `estimatedDocumentCount()` every minute
while it ran:

```
0 → 11,657 → 19,804 → 28,231 → 36,229 → 37,557
extraction: user_id=6abfe1e89094a64658b0e66f shards=1 succeeded=1 failed=0
Done. Flow completed successfully.   (make exit 0)
```

End state: 10,081 document + 10,170 parent + 17,306 child rows. All children carry `binData`
vectors, 0 are pending, `vector_index` is READY, and a prod `make memory-search` scores the top hit
at 0.837, the same as local.

## Tests

`apps/memory/tests/unit/memory/rag/test_load.py::TestLoadRagRows`:

- `test_a_small_run_is_one_unordered_bulk_write`: one document, one command, `ordered=False`.
- `test_a_large_run_is_written_in_batches_of_whole_documents`: five 3-op documents at
  `batch_ops=7` → batches `[6, 6, 3]`, never 7, because 7 would split a document.
- `test_a_dropped_batch_is_retried_then_written`: one `AutoReconnect`, then success → 2 calls, all
  rows counted.
- `test_a_batch_that_keeps_dropping_raises_after_the_earlier_ones`: batch 1 is written, batch 2
  drops `LOAD_RETRIES` times → raises after 1 + `LOAD_RETRIES` calls.
- `test_empty_op_list_skips_the_round_trip`: `bulk_write([])` raises, so nothing is called.

## Applying the pattern elsewhere

Any write path whose input grows with the corpus should follow the same three rules:

1. **Bound the command**, by op count and bytes, never by "the whole run".
2. **Batch on the unit of done.** Here that is the document, because `sources` defines "ingested".
3. **Retry only what is idempotent**, with backoff, and let the last failure propagate. The offline
   pipeline's `PartialIngestError` (`8b3034c`) then turns it into a Failed run instead of a quiet
   Completed.

Already bounded: the indexing backfill (`rag/indexing.py`, one `bulk_write` per embedding batch).
Not audited yet: the graphrag `apply_writes` path and the data-phase loads. They are smaller per run
today, but they grow with the corpus too.
