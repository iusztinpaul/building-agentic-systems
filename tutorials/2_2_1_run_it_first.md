# Run It First

Quick start tutorial on running the whole data pipeline once, end-to-end, from Chapter 2, section `2.2.1 Run It First` of the book.

Before going through the code, let's run the whole data pipeline once to build up the intuition.

`sources/light.yaml` is a light config containing only ~10 Substack articles, with plain GET requests, no API keys, so the first run needs nothing beyond a simple Docker setup.

## Running the Pipeline

All four commands run from the repository root. Point the environment to the local infrastructure, start it (MongoDB, the Prefect server, and a worker that serves all pipelines), create your user (every document is tenant-scoped), and trigger the pipeline.

```bash
make env-local
make local-start
make memory-signup USER_IDENTIFIER=paul@example.com
make memory-run-data-pipeline SOURCE_FILE="sources/light.yaml"
```

We kick off the offline pipeline that runs the coordinator, which groups the ten Substack articles from `sources/light.yaml` into one shard running on one worker that ingests all of them as `Document` instances into the `documents` MongoDB collection.

Within the Prefect dashboard, the Runs tab, accessible at http://127.0.0.1:4200/dashboard, clearly shows how all the flow runs are triggered. The offline pipeline, coordinator, and worker deployments are generic layers that dispatch to whichever specialized ETL each shard needs. In this case, only the Substack ETL. If, among the sources, we had a YouTube link, we would have two specialized ETLs on top of the coordinator and worker: Substack and YouTube.

![The quick-start run in Prefect's dashboard](assets/2_2_1_prefect_dashboard.png)

## Inspecting the Data

On the database side, for a quick check, you can run `make memory-check-db`, which checks your connectivity and lists the number of documents in all your collections, such as `tree.documents: 227 docs`. Having 227 documents, instead of 10, makes sense because of the latent type ingesting references found within the Substack articles.

Still, for future steps, we recommend using either their [mongosh CLI](https://www.mongodb.com/try/download/shell) coupled with a coding agent like Claude Code or Codex or their [MongoDB Compass GUI](https://www.mongodb.com/try/download/compass) when manually inspecting the data. You can connect to the database using this connection string:

```
mongodb://tree:tree@localhost:27017/?directConnection=true&authSource=admin
```

![The quick-start run in MongoDB's Compass GUI](assets/2_2_1_mongodb_compass.png)

For full setup instructions, see the repository's [README](../README.md) and [tutorials](.).
