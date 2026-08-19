# Serving Locally

This is the tutorial used to run the MongoDB database and Prefect pipelines locally from Chapter 2, section `2.4 Deploying the Database & Pipelines` of the book.

## Serving the Prefect Deployments

Prefect's `run_deployment` function can only dispatch deployments registered with Prefect. `_active_deployment_specs` returns the list of registered Prefect flows, coupled with other metadata such as their names, tags, and cron rules. Transforming a flow into a Prefect deployment is as easy as calling `flow.to_deployment(...)`. The next step is to register the deployment in a place where it can be executed. Via the `serve()` command, you can easily transform any machine into a worker that executes the registered Prefect deployments. This is the equivalent of having a remote function that can execute the code within the flow.

In our use case, we will use the `serve()` logic just to start a local server that executes the registered Prefect deployments for development purposes. But you can just as well run it on any reserved machine you have access to, as long as it can reach the cloud Prefect control plane that orchestrates it.

**The local serve entrypoint, from [apps/memory/src/tree/orchestrator.py](../apps/memory/src/tree/orchestrator.py):**

```python
from prefect import serve


def build_deployments() -> list:
    return [
        spec.flow.to_deployment(
            name=spec.name, tags=spec.tags, schedules=spec.schedules()
        )
        for spec in _active_deployment_specs()
    ]


def serve_deployments(limit: int) -> None:
    configure_opik()
    serve(*build_deployments(), limit=limit)


if __name__ == "__main__":
    serve_deployments(app_config.concurrency.runner_global_limit)
```

Within the `_active_deployment_specs` function we load all our Prefect deployment specs and filter out optional deployments in case `app_config.prefect.deploy_optional = False`. We added this because Prefect's Cloud free tier gives you a maximum of 5 deployments. The always-on core set now spends all 5 slots, so no spec is optional today and the flag registers nothing extra — it is kept as the extension point for the next deployment that has to be gated. The function also takes an optional `groups` argument that narrows the set to whole pipelines (`data` or `memory`) by the identity tag every spec already carries; it is used by the cloud deploy path, while the local `serve()` registers everything.

Within `_DEPLOYMENT_SPECS` we define all your deployment specs, where a `_DeploymentSpec` is a wrapper over a Prefect flow, packaging important metadata such as the flow's name, tags and optional scheduling parameters. We scheduled the end-to-end `offline-pipeline` deployment to run every night at 03:00 UTC via the `"0 3 * * *"` cron job, and the `dream-consolidation-all-users` deployment an hour later at 04:00 UTC. Via the `TAGS_DATA_OFFLINE`/`TAGS_ONLINE_PIPELINE`/`TAGS_OFFLINE_PIPELINE` constants we flag the deployments as data-pipeline / memory-pipeline + offline/online, to easily filter the runs within Prefect.

**The topology registry, from [apps/memory/src/tree/orchestrator.py](../apps/memory/src/tree/orchestrator.py):**

```python
from prefect import Flow
from prefect.schedules import Cron


@dataclass(frozen=True)
class _DeploymentSpec:
    flow: Flow
    name: str
    entrypoint: str  # cloud only: the repo path the worker clones
    tags: list[str]
    cron: str | None = None
    schedule_parameters: dict[str, Any] = field(default_factory=dict)
    optional: bool = False  # beyond Prefect Cloud's free-tier 5


_DEPLOYMENT_SPECS: list[_DeploymentSpec] = [
    _DeploymentSpec(
        data_etl_worker,
        "data-etl-worker",
        "apps/memory/src/tree/data/offline_pipeline.py:data_etl_worker",
        TAGS_DATA_OFFLINE,
    ),
    _DeploymentSpec(
        memory_extract_etl_worker,
        "memory-extract-etl-worker",
        "apps/memory/src/tree/memory/extraction/"
        "pipeline.py:memory_extract_etl_worker",
        TAGS_EXTRACTION,
    ),
    _DeploymentSpec(
        online_pipeline,
        "online-pipeline",
        "apps/memory/src/tree/online.py:online_pipeline",
        TAGS_ONLINE_PIPELINE,
    ),
    _DeploymentSpec(
        offline_pipeline,
        "offline-pipeline",
        "apps/memory/src/tree/offline.py:offline_pipeline",
        TAGS_OFFLINE_PIPELINE,
        cron=_SCHEDULED_INGEST_CRON,
        schedule_parameters={"source_files": ["sources/listen.yaml"]},
    ),
    _DeploymentSpec(
        dream_consolidation_all_users,
        "dream-consolidation-all-users",
        "apps/memory/src/tree/memory/consolidation/"
        "dream.py:dream_consolidation_all_users",
        ["memory-pipeline", "dream", "consolidation"],
        cron=app_config.dream.cron,
    ),
]
```

The `data-etl-worker` and `memory-extract-etl-worker` deployments are the per-user workers fanned out by the coordinators, while `online-pipeline` and `offline-pipeline` are the end-to-end entrypoints that run data + memory in one go — they are also key at understanding the difference between the two execution models in practice. Note that `data_etl_coordinator` is *not* a deployment of its own: `offline_pipeline` calls it as an inline subflow, and memory indexing does the same within extraction. That's by design — it avoids a useless serialization/deserialization step plus a deployment slot. In this chapter we only exercise the data half (`data-etl-worker`, driven by the coordinator inside `offline-pipeline`); the memory deployments arrive in the next chapter.

Via the [apps/memory/configs/default.yaml](../apps/memory/configs/default.yaml) config, we can control `runner_global_limit: 6` which defines how many flow runs we can run concurrently at a time. A flow run is an invocation of any of the deployments we defined above. Meanwhile `deploy_optional: false` is used to turn off or on the optional deployments. Note that this limitation exists only when deploying to Prefect Cloud. As the Prefect engine is open-source, when deploying locally or on a self-managed solution, you don't have it.

## The Local Stack Behind `make local-start`

The whole local stack is baked into a single [docker-compose.yml](../docker-compose.yml) found at the repository root. Within it we have 5 services powered as different Docker images:

1. **mongodb** is the MongoDB document warehouse, a single-node replica set on port 27017, with a named volume so documents survive restarts.
2. **mongodb-init** is a one-shot container that after the replica set is online it creates the admin user, and exits.
3. **mongot** is MongoDB's search engine that provides the vector index, running along with the database. We will use it within the memory layer to index our data.
4. **prefect-server** is the Prefect control plane. It exposes the dashboard on port 4200 (accessible at http://127.0.0.1:4200/dashboard). It only orchestrates, it never executes the actual pipeline code.
5. **prefect-worker** is the executor, built by us as a new Docker image within [apps/memory/docker/Dockerfile](../apps/memory/docker/Dockerfile) that inherits from `python:3.14-slim`, installs our memory app within it via `uv`, and then kicks off with `uv run python -m tree.orchestrator` which serves all the Prefect deployments via the `serve()` Prefect function, we explained in listing 2.46.

![Figure 2.18 The deployments in Prefect's dashboard. Note the offline-pipeline scheduled every day at 03:00 AM UTC](assets/2_4_1_prefect_deployments.png)

You can kick off the entire local infrastructure by running `make local-start`, which will call Docker Compose to do the heavy lifting. Running this will kick off the Prefect deployments within the `prefect-worker` Docker container. In case you want to run it directly locally, to avoid rebuilding the Docker image, you can also start the Prefect worker by running `make memory-serve-workflows`, which will run `uv run python -m tree.orchestrator` directly on the host. When doing so, just make sure to turn off the `prefect-worker` Docker container by commenting it out within the `docker-compose.yml` file or manually shutting it down.

You can access Prefect's server dashboard by typing within your browser http://127.0.0.1:4200/dashboard. For inspecting MongoDB we recommend using either their [mongosh CLI](https://www.mongodb.com/try/download/shell) or their [MongoDB Compass GUI](https://www.mongodb.com/try/download/compass) by using this connection string `mongodb://tree:tree@localhost:27017/?directConnection=true&authSource=admin`. We personally use both. The CLI coupled with a coding agent like Claude Code or Codex and the GUI to manually inspect the data.

![Figure 2.19 Inspecting the documents collection from MongoDB Compass](assets/2_4_1_mongodb_compass_documents.png)

Now that we have everything up and running locally, continue by deploying them to the cloud following [this tutorial](./2_4_2_deploying_to_cloud.md).
