# Document intra-run concurrency model in ADR-002 §6

Status: done
Tags: `docs`, `adr`
Depends on: #057, #058, #059
Blocks: —

## Scope

Doc-only: add a new §6 "Intra-run concurrency" to
`docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` documenting the
shipped #057/#058/#059 intra-run knobs (`doc_concurrency` chunking fan-out,
`dedup_concurrency` parallel read-only dedupe, and `bulk_write` batching) —
behavior-preserving, no code change, no Voyage-budget impact.
