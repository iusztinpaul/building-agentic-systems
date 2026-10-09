# Tree Memory — write chains

Every ingest tool answers a **receipt**: `duplicate`, `source_uri`, `document_id`, `flow_run_id`, `status`. The receipt is the write's provenance — report it as-is; it carries no counts.

## Write → Write

1. **Ingest directly — each ingest tool deduplicates on `source_uri` itself.** The receipt's `duplicate` answers "already known?".
   - Good: `ingest_url(url)` → `duplicate: true` → "Already in memory as `<source_uri>`."
   - Bad: `search_memory(<page title>)` first, then `ingest_url` anyway.
2. **Many sources → one ingest call per source, in parallel**, so every source gets its own receipt.
3. **Web to memory: `search_web` → `scrape_web` (≤5 URLs per call) → `ingest_url` on each page the user keeps.** Why `ingest_url` over `search_web(ingest=true)`: `ingest_url` runs ONE run that chunks, embeds (and in `graphrag` extracts), so the page becomes searchable; `ingest=true` only stores the raw pages, unsearchable until `make memory-run-memory-pipeline` processes pending documents, with no per-URL receipt.
   - Good: "keep the top two" → `ingest_url` × 2 → two receipts reported.
   - Bad: `search_web(..., ingest=true)`, then telling the user the pages are in memory.

   Use `ingest=true` only for a bulk queue the user accepts will wait for the memory pipeline — say so.
4. **Local file → `Read` it yourself, then `ingest_file` with its absolute path and text** — the server never opens the path.
5. **Mid-session "remember this" → `ingest_conversation`** with the relevant exchange. Whole sessions are persisted by the SessionEnd hook.

## Write → Read

1. **`duplicate: false` → memory is written out-of-band.** Report `source_uri` and `flow_run_id`. An empty search right after a submit means "not written yet".
2. **Every ingest makes the map stale.** Ingest never clusters, so the next `visualize_memory_embeddings` opens with the `don’t have a 2D embedding` line until the nightly offline pipeline (or `make memory-run-clustering-pipeline`) runs. Say so when the user asks for the map after adding content.
3. **`duplicate: true` but search still finds nothing → the earlier run stored the source and failed to process it.** A re-ingest deduplicates and skips processing; offer `make memory-run-memory-pipeline MODE=online SOURCE_URIS=<source_uri>`.
   - Good: "It's stored but was never indexed — `make memory-run-memory-pipeline MODE=online SOURCE_URIS=https://…` will finish it."
   - Bad: calling `ingest_url` again.
4. **graphrag only:** after several ingests, offer the duplicate-review loop in [`graphrag.md`](graphrag.md).
