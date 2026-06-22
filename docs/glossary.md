# Glossary

The canonical vocabulary for **Tree**. When code, tests, docs, specs, or
conversation use a domain concept, use the term as it appears here — one name
per concept. Seeded from the data-pipeline sharding work; grows as the codebase
does. (Discipline borrowed from DDD's *ubiquitous language*.)

| Term | Definition | Notes |
|---|---|---|
| **Batch** | A streaming chunk of `batch_size` rows pulled *inside* one Worker run and processed concurrently under `asyncio.Semaphore(concurrency)`. | In-worker, in-process unit. Distinct from a **Window** (cross-run, one per Worker) — don't call a Window a "batch". |
| **Data Pipeline** | The ETL half that gathers raw source data and normalizes it into the `documents` collection. | Produces **Document**s only — there is NO index step. Distinct from the **Memory Pipeline**. |
| **Deployment** | A Prefect flow registered with the server / work pool and triggerable by name. | Capped at 5 on the Prefect free tier (`orchestrator.py::_DEPLOYMENT_SPECS`). Distinct from a **Worker** (a *run* of a Deployment). A "Docker" / "machine" is not a Deployment. |
| **Document** | A normalized source record in the `documents` collection; the Data Pipeline's output unit. | Deduped on `(user_id, source_uri)`. Distinct from a **Node** (the Memory Pipeline's unit). |
| **Memory Pipeline** | The ETL half that maps **Document**s into knowledge-graph Nodes/Edges in the `knowledge_graph` collection. | Distinct from the **Data Pipeline**. Still shards by document-**Shard** (`num_shards`). |
| **Orchestrator** | The single Prefect flow per pipeline that reads config, groups/partitions the work, and dispatches **Worker** runs at **depth-1** (never recursively). | One per pipeline (e.g. `data-etl-orchestrator`). Holds one admission slot while awaiting its Workers. Distinct from a **Worker**. |
| **Platform** | A source family used as the data Orchestrator's grouping key: `{SubstackRss, SubstackArticle}→substack`, `{YouTubeRss, YouTubeVideo}→youtube`, `Web→custom`, `HuggingFace→huggingface`. | One **Worker** run per Platform. Distinct from a **Source variant** (many variants → one Platform). |
| **Shard** | A contiguous, balanced partition of a work list (`tree.sharding._partition_into_shards`). | The Memory Pipeline shards **Document** ids; the Data Pipeline no longer shards by count (it groups by **Platform**). Distinct from a **Window** and a **Batch**. |
| **Source variant** | A discriminated `SourceEntry` config type (`SubstackRssSource`, `WebSource`, `HuggingFaceDatasetSource`, …). | The fine-grained config unit. Maps up to a **Platform** for grouping. |
| **Window** | A disjoint slice `[offset, offset + window_size)` of a HuggingFace dataset, ingested by exactly one **Worker** run. | Cross-run unit, computed by the Orchestrator from `num_workers`. Distinct from a **Batch** (in-worker). |
| **Worker** | A single Prefect flow *run* that executes one homogeneous unit of ingestion (a **Platform**'s sources, or one HF **Window**). | A run of the `data-etl-worker` **Deployment** — NOT a separate deployment, Docker, or machine. A Worker NEVER dispatches other Workers (that recursion can deadlock the admission limit). |
