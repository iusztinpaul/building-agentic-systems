# Setting Up the Memory Pipeline

Quick start tutorial on running the memory pipeline end-to-end, from Chapter 5, section `5.2.1` of the book.

The data pipeline from Chapter 2 needed nothing beyond Docker. The memory pipeline is different: it embeds every child chunk, so an embedding model is mandatory. We use Voyage AI's `voyage-4`, which you access through your MongoDB Atlas account. This tutorial walks you through getting the key, a light run on the free tier, and the full run.

## 1. Get a Voyage API Key From MongoDB Atlas

Voyage AI is part of MongoDB, so you create the key inside the same Atlas account you set up in Chapter 2. In your Atlas project, select **AI Models** in the navigation bar, click create, name the key, and copy it ([MongoDB docs](https://www.mongodb.com/docs/voyageai/management/api-keys/)). Atlas shows the key only once, so store it right away.

Add it to your `.env` (and `.env.prod` if you also run in the cloud):

```bash
VOYAGE_API_KEY=your-voyage-api-key
```

The key must come from Atlas, not from the standalone Voyage dashboard: our clients call `https://ai.mongodb.com/v1/embeddings`, which only accepts Atlas-issued keys.

If the local stack is already running, recreate the containers so the worker picks up the new key:

```bash
make env-local
make local-restart
```

## 2. A Light Run Without a Payment Method

Without a payment method, Voyage caps your key at **3 requests per minute and 10K tokens per minute**. That is enough for `sources/light.yaml` (the ~10 Substack articles from Chapter 2), but not for the full backfill.

Our pipelines respect that cap through `voyage_rpm` in [apps/memory/src/tree/config/default.yaml](../apps/memory/src/tree/config/default.yaml). The repository ships with the paid value active and the free one commented out. On a free key, swap them:

```yaml
concurrency:
  voyage_rpm: 3      # free tier, no payment method
  # voyage_rpm: 2000 # Tier 1, after adding a payment method
```

`voyage_rpm` drives a Prefect global concurrency limit called `voyage-embeddings` that every flow run shares. In the cloud, `make memory-deploy-prefect-setup-up` creates it from the YAML. Locally, nothing creates it for you, so create it once on your local Prefect server with the same numbers (3 slots, each freed after 20 s, so 3 / 60 = 0.05 slots per second):

```bash
PREFECT_API_URL=http://127.0.0.1:4200/api \
  uv --directory apps/memory run prefect gcl create voyage-embeddings \
  --limit 3 --slot-decay-per-second 0.05
```

The limit lives on the Prefect server, not in the worker image, so you don't need to rebuild anything. Now run the whole pipeline (data → ingestion → indexing) over the light sources:

```bash
make memory-signup USER_IDENTIFIER=pauliusztin@decodingai.com   # skip if you signed up in Chapter 2; use YOUR OWN email
make memory-run-pipeline SOURCE_FILE="sources/light.yaml"
```

Expect it to be slow. Voyage will answer some requests with HTTP `429`, which the pipeline retries automatically, so a `429` in the logs is not an error.

## 3. Recommended: Add a Payment Method

To run the full backfill, we recommend adding a payment method to your Atlas organization's billing. It will likely cost you nothing: Atlas gives **200 million free tokens per model**, and the full backfill below used about **3M tokens, roughly 1.5% of that allowance**. Past the free tokens, `voyage-4` costs $0.06 per million tokens, so even a full rebuild costs about $0.18.

What the payment method buys you is throughput. It moves your key to Tier 1 ([rate limits](https://www.mongodb.com/docs/voyageai/management/rate-limits/)):

| | Requests/minute | Tokens/minute |
| :-- | --: | --: |
| No payment method | 3 | 10K |
| Tier 1 (payment method added) | 2,000 | 8M |

After adding it, switch the YAML back to `voyage_rpm: 2000` and update the limits:

```bash
# Local Prefect server
PREFECT_API_URL=http://127.0.0.1:4200/api \
  uv --directory apps/memory run prefect gcl update voyage-embeddings \
  --limit 2000 --slot-decay-per-second 33.33

# Prefect Cloud (reads voyage_rpm from the YAML)
make memory-deploy-prefect-setup-up
```

For reference, with Tier 1 limits and 4 extraction shards on Prefect Cloud, the memory pipeline processed the 10,022 backfill documents in about 8 minutes, embedding 15,212 child chunks. On the free tier, the 10K tokens/minute cap alone would stretch those ~3M tokens over about 5 hours.

**Alternative: host your own embedding model.** If you prefer not to add a payment method, you can serve an open-source embedding model on Modal instead. We cover it in [Deploying Embedding Models on Modal](5_2_4_deploying_embedding_models_on_modal.md).

## 4. Running the Full Offline Pipeline

First, let's build the intuition and run the memory pipeline that wraps both phases as sequential steps:

```bash
make memory-run-memory-pipeline    # ingestion
make memory-run-indexing-pipeline  # indexing
```

Or run the whole offline pipeline end-to-end over the backfill sources:

```bash
make memory-run-pipeline SOURCE_FILE="sources/backfill.yaml"   # data -> ingestion -> indexing
```

The Make command triggers the same coordinator-worker pattern implemented with Prefect that we explained in detail in Chapter 2. Each shard processes N documents independently in parallel (`extraction.num_shards` in the YAML). This time the logic is ten times simpler, as each source is normalized to the same document structure. Thus, we have only one unified workflow.

Optionally, run it over the listen sources too (the RSS feeds the nightly cron polls):

```bash
make memory-run-pipeline SOURCE_FILE="sources/listen.yaml"
```

You don't have to run `listen.yaml` by hand: the scheduled offline pipeline loads it every night at 3 AM and carries it through ingestion and indexing.

To confirm the memory landed, run `make memory-check-db`. After the backfill, the `memory` collection should hold one row per document, parent chunk, and child chunk (35,270 rows in our run).

## 5. Visualizing the Memory

The best way to see the results of the ingestion is by visualizing all the clustered embeddings projected into a 2D space. First, we need to cluster the data:

```bash
make memory-run-clustering-pipeline
```

Next, to visualize it:

```bash
make memory-visualize-embeddings HULLS=true
```

This will pop up an interactive visualization in your browser, like the one in Figure 5.8:

![Figure 5.8 The embedding space after the offline run. Every dot is a child chunk; every hull is a cluster of chunks on the same topic.](assets/5_2_1_embedding_space.png)

Once you run the clustering pipeline, all the clusters are cached within MongoDB. Plus, we also compute them when running the scheduled offline pipeline each night automatically. Note that you can view the embeddings without clustering them, but this makes looking at them so much easier!

To see how the memory is organized instead (each document, its parent chunks, and their child chunks), render the structure view. Add `QUERY` to narrow it to the results of a search:

```bash
make memory-visualize-structure
make memory-visualize-structure QUERY="How does memory for AI agents work?"
```

For full setup instructions, see the repository's [README](../README.md) and [tutorials](.).
