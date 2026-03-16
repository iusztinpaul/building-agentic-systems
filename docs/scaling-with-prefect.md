# Scaling Your GraphRAG Pipelines with Prefect: A Complete Guide

This guide uses the **Digital Twin / Personal Assistant** application as a concrete example — a GraphRAG system that ingests documents, extracts knowledge graphs via LLM, materializes them into a queryable graph, and serves semantic queries. All concepts apply to any Prefect-orchestrated application.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [How Prefect Works — The Three-Layer Model](#2-how-prefect-works--the-three-layer-model)
3. [The Prefect Server — Central Coordinator](#3-the-prefect-server--central-coordinator)
4. [Work Pools — The Distribution Hub](#4-work-pools--the-distribution-hub)
5. [Work Queues — Priority Within a Pool](#5-work-queues--priority-within-a-pool)
6. [Workers — The Execution Agents](#6-workers--the-execution-agents)
7. [Multiple Pipelines Per Worker](#7-multiple-pipelines-per-worker)
8. [Multiple Work Pools](#8-multiple-work-pools)
9. [How Work Pools Scale Up and Down](#9-how-work-pools-scale-up-and-down)
10. [End-to-End Architecture Diagram](#10-end-to-end-architecture-diagram)
11. [Step-by-Step Scaling Path](#11-step-by-step-scaling-path)
12. [End-to-End Example: 1 Million Records](#12-end-to-end-example-1-million-records)
13. [GPU vs CPU Work Pool Separation](#13-gpu-vs-cpu-work-pool-separation)

---

## 1. Architecture Overview

The application has three pipelines orchestrated by Prefect:

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  DATA PIPELINE          EXTRACTION PIPELINE    MATERIALIZATION  │
│                                                                 │
│  RSS Feed URL           Document content       KG log entries   │
│       │                      │                      │           │
│       ▼                      ▼                      ▼           │
│  fetch + parse          chunk (512 tokens)     aggregate/dedup  │
│       │                      │                      │           │
│       ▼                      ▼                      ▼           │
│  extract document       LLM extract (×5)       embed nodes     │
│       │                      │                      │           │
│       ▼                      ▼                      ▼           │
│  load to MongoDB        normalize + store      reverse edges   │
│                         to KG log              + indexes       │
│                                                                 │
│  Collection:            Collection:             Collection:     │
│  "documents"            "knowledge_graph_log"  "knowledge_graph"│
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Prefect flows defined in the codebase:**

| Flow | File | Purpose |
|------|------|---------|
| `ingest_substack_rss_feed` | `src/twin/data/substack/substack_rss_pipeline.py` | Ingest a single RSS feed |
| `ingest_substack_rss_feed_batch` | `src/twin/data/substack/substack_rss_pipeline.py` | Ingest multiple feeds via `asyncio.gather()` |
| `memory_extraction` | `src/twin/memory/extraction/pipeline.py` | Chunk documents, call LLM, build KG log |
| `memory_materialization` | `src/twin/memory/materialization/pipeline.py` | Aggregate logs, embed, index |

**How jobs are submitted today** (`scripts/run_data_pipeline.py`):

```python
async with get_client() as client:
    deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)
    flow_run = await client.create_flow_run_from_deployment(
        deployment_id=deployment.id,
        parameters={"feed_urls": feed_urls},
    )
```

---

## 2. How Prefect Works — The Three-Layer Model

Prefect separates the **"what to run"** from the **"where to run it"** through three layers:

```
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 1: ORCHESTRATION                                         │
│                                                                 │
│  Prefect Server or Prefect Cloud                                │
│                                                                 │
│  - Stores deployment definitions                                │
│  - Schedules and queues flow runs                               │
│  - Tracks run state (Scheduled → Pending → Running → Completed) │
│  - Enforces concurrency limits                                  │
│  - Serves the UI dashboard and REST API                         │
│  - NEVER executes your code                                     │
│                                                                 │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│  LAYER 2: WORK POOLS                                            │
│                                                                 │
│  Logical queues living inside the server's database             │
│                                                                 │
│  - Bridge between orchestration and infrastructure              │
│  - Typed: each pool has a type (docker, kubernetes, cloud-run)  │
│  - Hold scheduled flow runs until a worker claims them          │
│  - Enforce pool-level and queue-level concurrency limits        │
│  - Contain one or more priority queues                          │
│  - NOT separate processes — just database constructs            │
│                                                                 │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│  LAYER 3: WORKERS                                               │
│                                                                 │
│  Lightweight polling agents running on YOUR infrastructure      │
│                                                                 │
│  - Poll their assigned work pool every ~15 seconds              │
│  - Claim scheduled runs from the queue                          │
│  - Provision infrastructure (container, pod, process)           │
│  - Submit the flow run for execution                            │
│  - Multiple workers can poll the same pool (horizontal scaling) │
│  - Workers never talk to each other — only to the server        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

A **deployment** is the bridge between a flow and a work pool. It says: "this flow, with these defaults, should run on this work pool." When you call `create_flow_run_from_deployment`, the server creates a flow run in `Scheduled` state and places it in the work pool's queue. Workers pick it up from there.

---

## 3. The Prefect Server — Central Coordinator

The server is the single source of truth. It coordinates everything but executes nothing.

```
Your App (submit 1000 runs via API)
        │
        ▼
┌────────────────────────────────────────────────────┐
│                 PREFECT SERVER                      │
│                                                    │
│  What it does:                                     │
│  - Stores all state (deployments, runs, logs)      │
│  - Acts as the queue (holds Scheduled runs)        │
│  - Tracks run lifecycle:                           │
│      Scheduled → Pending → Running → Completed     │
│                                     → Failed       │
│                                     → Crashed      │
│  - Enforces concurrency limits (pool, queue,       │
│    global concurrency slots)                       │
│  - Serves the REST API (workers poll this)         │
│  - Serves the UI dashboard                         │
│  - Prevents two workers from claiming the same run │
│                                                    │
│  What it does NOT do:                              │
│  - Execute any flow code                           │
│  - Provision infrastructure                        │
│  - Manage containers or processes                  │
│                                                    │
│  ┌──────────────────────────────────────────────┐  │
│  │  Work Pool: "cpu-pool"                       │  │
│  │  ┌────────────────────────────────────────┐  │  │
│  │  │ 998 Scheduled  │  2 Running            │  │  │
│  │  └────────────────────────────────────────┘  │  │
│  └──────────────────────────────────────────────┘  │
└───────────────────┬────────────────────────────────┘
                    │  poll every ~15s
               ┌────┴─────┐
               ▼          ▼
            Worker A   Worker B
            (host-1)   (host-2)
```

**Self-hosted vs Cloud:**

- `prefect server start` — you run the server yourself (backed by PostgreSQL or SQLite). This is what `docker-compose.yml` does with the `twin-prefect-server` container.
- **Prefect Cloud** — hosted by Prefect, adds authentication, RBAC, audit logs, push automations, managed work pools.

In this application, the server runs as a Docker container and workers connect via `PREFECT_API_URL=http://prefect-server:4200/api`.

---

## 4. Work Pools — The Distribution Hub

Work pools are typed queues that determine **where** and **how** flow runs execute. They are database constructs inside the server — not separate processes.

Each deployment targets a specific pool:

```python
ingest_substack_rss_feed.deploy(
    name="ingest-substack-rss-feed-etl",
    work_pool_name="cpu-pool",       # ← targets this pool
    image="my-registry/twin:latest",
)
```

You can create as many work pools as needed:

```bash
prefect work-pool create "cpu-pool" --type docker
prefect work-pool create "gpu-pool" --type docker
prefect work-pool create "k8s-pool" --type kubernetes
```

**Work pool types determine what kind of infrastructure runs the flow:**

| Type | Infrastructure | Worker Required |
|------|---------------|-----------------|
| `process` | Local subprocess | Yes |
| `docker` | Docker container | Yes |
| `kubernetes` | K8s Job/Pod | Yes |
| `ecs:push` | AWS ECS Fargate | No (push) |
| `cloud-run:push` | GCP Cloud Run | No (push) |
| `azure-container-instance:push` | Azure ACI | No (push) |
| `prefect:managed` | Prefect Cloud infra | No (managed) |

**Base job templates:** Each pool defines default infrastructure config (CPU, memory, env vars, image). Individual deployments can override specific fields. This lets platform teams expose controlled interfaces while data engineers customize what they need.

---

## 5. Work Queues — Priority Within a Pool

Each work pool has a **default queue** created automatically. You can add more queues to differentiate by **priority** and **concurrency**:

```
Work Pool: "cpu-pool" (pool-level concurrency limit: 50)
│
├── Queue "critical"    (priority: 1, concurrency: 5)
├── Queue "standard"    (priority: 5, concurrency: 30)
└── Queue "backfill"    (priority: 10, concurrency: 15)
```

Workers drain queues in **priority order** (waterfall, not round-robin):
- All `critical` runs execute before any `standard` run
- All `standard` runs execute before any `backfill` run
- `backfill` only runs when there's remaining capacity

**Targeting a specific queue from a deployment:**

```python
memory_extraction.deploy(
    name="memory-extraction-etl",
    work_pool_name="cpu-pool",
    work_queue_name="critical",  # ← high priority
)
```

If you don't specify a queue, runs go to the `default` queue.

**The full hierarchy:**

```
Server
├── Work Pool A (type: docker, limit: 50)
│   ├── Queue "default"    (priority: 5)
│   ├── Queue "critical"   (priority: 1)
│   └── Queue "backfill"   (priority: 10)
├── Work Pool B (type: kubernetes, limit: 100)
│   └── Queue "default"
└── Work Pool C (type: cloud-run:push, limit: 200)
    └── Queue "default"
```

For most setups, the single default queue per pool is enough. Multiple queues become useful when you have mixed-priority workloads sharing the same infrastructure — e.g., real-time extraction jobs should preempt batch reprocessing, but both need the same machines.

---

## 6. Workers — The Execution Agents

Workers are lightweight, long-running polling processes on your infrastructure:

```bash
prefect worker start \
  --pool "cpu-pool" \
  --type docker \
  --limit 10 \
  --name "worker-$(hostname)"
```

**Worker lifecycle:**

1. Start up and register with the server
2. Poll their assigned work pool every ~15 seconds (configurable via `PREFECT_WORKER_QUERY_SECONDS`)
3. Claim `Scheduled` flow runs from the queue
4. Provision infrastructure (spin up a container, pod, or subprocess)
5. Submit the flow run for execution
6. Report state transitions back to the server
7. When the run finishes, claim the next one

**Key configuration flags:**

| Flag | Purpose |
|------|---------|
| `--pool` | Which work pool to poll |
| `--type` | Worker type (must match the work pool type) |
| `--limit` | Maximum concurrent flow runs this worker will execute |
| `--work-queue` | Restrict to specific queue(s) within the pool |
| `--prefetch-seconds` | How early to claim runs before their scheduled time (default: 10s) |
| `--name` | Custom identifier for this worker |

**Health monitoring:**
- Workers send heartbeats every 30 seconds (configurable via `PREFECT_WORKER_HEARTBEAT_SECONDS`)
- If a worker misses 3 consecutive heartbeats (~90 seconds), it is marked **offline**
- Work queue status shows `READY` if a worker has polled within the last 60 seconds

**Horizontal scaling — the key mechanism:**

Multiple workers can poll the **same work pool**. Runs are distributed on a first-come-first-served basis — whichever worker polls and claims a run first, executes it. There is no explicit load balancing algorithm.

```
                    Prefect Server
                         │
                    Work Pool: "cpu-pool"
                    (1000 flow runs queued)
                   /        |         \
            Worker A     Worker B     Worker C
          (Machine 1)  (Machine 2)  (Machine 3)
          --limit 20   --limit 20   --limit 10
```

Workers never communicate with each other — they only talk to the server. The server is the coordinator that prevents two workers from claiming the same run.

---

## 7. Multiple Pipelines Per Worker

The `--limit` flag on the worker controls how many concurrent flow runs a single worker executes:

```bash
# Runs up to 10 flow runs simultaneously, each in its own Docker container
prefect worker start --pool "cpu-pool" --type docker --limit 10
```

Each flow run gets its own isolated container/process. The worker manages them concurrently.

**When to increase `--limit`:**
- Flows are CPU-bound or I/O-bound (not GPU-bound)
- The machine has enough RAM/CPU for multiple concurrent containers
- Flows use different resources (e.g., one reads from S3, another writes to a DB)

**When to keep it at 1:**
- GPU workloads that need the full device memory
- Memory-heavy pipelines that would OOM if doubled up
- You want strict isolation between runs

**Example for this application:**

```bash
# Data ingestion is I/O bound (HTTP fetch + MongoDB write)
# → high concurrency is fine
prefect worker start --pool "cpu-pool" --type docker --limit 20

# Memory extraction with local LLM needs full GPU
# → one at a time
prefect worker start --pool "gpu-pool" --type docker --limit 1
```

---

## 8. Multiple Work Pools

You can create as many work pools as you want, each with its own type, concurrency limits, and workers. Pools are completely independent — their concurrency limits, queues, and workers don't affect each other.

**Common reasons to have multiple pools:**

| Use Case | Example |
|----------|---------|
| Different infrastructure | `gpu-pool` (Docker + CUDA machines) vs `cpu-pool` (lightweight containers) |
| Different environments | `dev-pool`, `staging-pool`, `prod-pool` |
| Different scaling profiles | `realtime-pool` (always-on workers) vs `batch-pool` (serverless/push) |
| Team isolation | `team-a-pool` vs `team-b-pool` with separate concurrency budgets |

**Each deployment targets a specific pool:**

```python
# GPU-heavy extraction → goes to GPU pool
memory_extraction.deploy(
    name="memory-extraction-etl",
    work_pool_name="gpu-pool",
    image="my-registry/twin-gpu:latest",
)

# Lightweight data ingestion → goes to CPU pool
ingest_substack_rss_feed.deploy(
    name="ingest-substack-rss-feed-etl",
    work_pool_name="cpu-pool",
    image="my-registry/twin:latest",
)
```

**Start workers for each pool on appropriate machines:**

```bash
# On GPU machines
prefect worker start --pool "gpu-pool" --type docker --limit 1

# On CPU machines
prefect worker start --pool "cpu-pool" --type docker --limit 20
```

A single machine can even run workers for multiple pools simultaneously if it has the resources.

---

## 9. How Work Pools Scale Up and Down

Work pools themselves don't scale — they're just queues in the server's database. The scaling happens at the infrastructure layer, and depends on the pool type:

### Docker / Process Pools — Manual Scaling

No auto-scaling built in. You manually start/stop workers:

```bash
# Scale up: start more workers on more machines
prefect worker start --pool "docker-pool" --type docker --limit 5

# Scale down: stop the worker process (Ctrl+C, kill, systemctl stop, etc.)
```

To auto-scale, you'd need to build it yourself — e.g., an autoscaling group of VMs where each VM starts a worker on boot via a startup script. Prefect doesn't manage that for you.

```
                    Work Pool: "docker-pool"
                         │
         ┌───────────────┼───────────────┐
         ▼               ▼               ▼
    Worker (VM 1)   Worker (VM 2)   Worker (VM 3)
         │               │               │
    ┌────┴────┐     ┌────┴────┐     ┌────┴────┐
    │Container│     │Container│     │Container│
    │Container│     │Container│     │Container│
    └─────────┘     └─────────┘     └─────────┘

    Scale up:   Launch a new VM, it starts a worker
    Scale down: Terminate a VM when its worker is idle
    WHO SCALES: You (manually or via VM autoscaling group)
```

### Kubernetes Pools — Auto-Scaling via Cluster Autoscaler

The worker creates a Kubernetes Job per flow run. The cluster autoscaler provisions and removes nodes:

```
                    Work Pool: "k8s-pool"
                         │
                    K8s Worker
                    (single process, runs in-cluster)
                         │
                  Creates K8s Jobs
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
       ┌────────┐   ┌────────┐   ┌────────┐
       │Pod: run│   │Pod: run│   │Pod: run│  ... ×hundreds
       │  #1    │   │  #2    │   │  #3    │
       └────────┘   └────────┘   └────────┘
            │            │            │
    ┌───────┴────────────┴────────────┴───────┐
    │     K8s Cluster Autoscaler              │
    │                                         │
    │  Pending pods → add nodes               │
    │  Idle nodes   → remove nodes            │
    │                                         │
    │  WHO SCALES: Kubernetes (not Prefect)   │
    └─────────────────────────────────────────┘
```

You set node pool min/max, Kubernetes handles the rest.

### Push Pools — Serverless Auto-Scaling (No Workers)

No worker process at all. Prefect submits runs directly to the cloud provider:

```
                    Work Pool: "cloudrun-pool"
                    (push type — no worker)
                         │
                Prefect Server submits
                directly via Cloud Run API
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
       ┌────────┐   ┌────────┐   ┌────────┐
       │Cloud   │   │Cloud   │   │Cloud   │  ... cloud auto-scales
       │Run #1  │   │Run #2  │   │Run #3  │
       └────────┘   └────────┘   └────────┘

       Scales to zero when idle.
       No worker process to manage.
       You pay only for execution time.
       WHO SCALES: Cloud provider (GCP, AWS, Azure)
```

### Managed Pools — Fully Managed by Prefect Cloud

Zero infrastructure management. Prefect Cloud handles everything.

### Comparison

| Pool Type | Who Scales | How | Ops Burden |
|-----------|-----------|-----|------------|
| Process / Docker | You | Manually start/stop workers on machines | High |
| Kubernetes | K8s cluster autoscaler | Automatically, based on pending pods | Medium |
| Push (Cloud Run, ECS, ACI) | Cloud provider | Automatically, serverless | Low |
| Managed | Prefect Cloud | Automatically, fully managed | None |

---

## 10. End-to-End Architecture Diagram

This diagram shows the full path from the application submitting a run to Prefect, through the server, down to all the different worker/infrastructure options:

```
                        ┌─────────────────────────────┐
                        │     Your App / Scripts       │
                        │                              │
                        │  scripts/run_data_pipeline.py│
                        │  scripts/run_memory_pipeline │
                        │                              │
                        │  client.create_flow_run_     │
                        │    from_deployment(          │
                        │      parameters={...}        │
                        │    )                         │
                        └──────────────┬───────────────┘
                                       │ REST API
                                       │ POST /api/flow_runs
                                       ▼
              ┌────────────────────────────────────────────────────┐
              │               PREFECT SERVER / CLOUD               │
              │                                                    │
              │  - Stores deployments, run state, logs             │
              │  - Enforces concurrency limits                     │
              │  - Serves UI dashboard (port 4200)                 │
              │  - Manages global concurrency slots                │
              │  - NEVER executes user code                        │
              │                                                    │
              │  ┌──────────────┐ ┌──────────────┐ ┌────────────┐ │
              │  │  Work Pool   │ │  Work Pool   │ │ Work Pool  │ │
              │  │  "cpu-pool"  │ │  "gpu-pool"  │ │  "cloud"   │ │
              │  │  type:docker │ │  type:docker  │ │  type:push │ │
              │  │  limit: 50   │ │  limit: 4     │ │  limit:200 │ │
              │  │              │ │              │ │            │ │
              │  │ ┌──────────┐ │ │ ┌──────────┐ │ │ ┌────────┐ │ │
              │  │ │ critical │ │ │ │ default  │ │ │ │default │ │ │
              │  │ │ queue(1) │ │ │ │ queue    │ │ │ │queue   │ │ │
              │  │ ├──────────┤ │ │ └──────────┘ │ │ └────────┘ │ │
              │  │ │ default  │ │ │              │ │            │ │
              │  │ │ queue(5) │ │ │              │ │            │ │
              │  │ ├──────────┤ │ │              │ │            │ │
              │  │ │ backfill │ │ │              │ │            │ │
              │  │ │ queue(10)│ │ │              │ │            │ │
              │  │ └──────────┘ │ │              │ │            │ │
              │  └──────┬───────┘ └──────┬───────┘ └─────┬──────┘ │
              └─────────┼───────────────┼───────────────┼─────────┘
                        │               │               │
                    HYBRID           HYBRID           PUSH
                  (you run          (you run        (no worker
                   workers)          workers)        needed)
                        │               │               │
          ┌─────────────┤               │               │
          │             │               │               │
          ▼             ▼               ▼               ▼
┌──────────────────────────────────────────────────────────────────┐
│                     INFRASTRUCTURE LAYER                         │
│                                                                  │
│  DOCKER WORKERS           K8S WORKER            PUSH / MANAGED   │
│  (poll every ~15s)        (poll every ~15s)     (server submits  │
│                                                  directly)       │
│  ┌────────────────┐      ┌────────────────┐    ┌──────────────┐  │
│  │   Worker A      │      │   Worker        │    │ Prefect      │  │
│  │   (CPU host-1)  │      │   (in-cluster)  │    │ sends run    │  │
│  │   --limit 20    │      │                 │    │ to cloud API │  │
│  │                 │      │  Creates K8s    │    │              │  │
│  │  ┌───┐┌───┐    │      │  Job per run    │    │  No worker   │  │
│  │  │run││run│... │      │                 │    │  process     │  │
│  │  └───┘└───┘    │      │  ┌───────────┐  │    └──────┬───────┘  │
│  └────────────────┘      │  │ Pod: run1 │  │           │          │
│                           │  ├───────────┤  │           ▼          │
│  ┌────────────────┐      │  │ Pod: run2 │  │    ┌──────────────┐  │
│  │   Worker B      │      │  ├───────────┤  │    │ Cloud Run /  │  │
│  │   (GPU host-2)  │      │  │ Pod: run3 │  │    │ ECS / ACI /  │  │
│  │   --limit 1     │      │  ├───────────┤  │    │ Managed      │  │
│  │                 │      │  │    ...    │  │    │              │  │
│  │  ┌───────────┐  │      │  └───────────┘  │    │ ┌──────────┐ │  │
│  │  │ Container │  │      │                 │    │ │Container │ │  │
│  │  │ (GPU run) │  │      │  K8s cluster    │    │ │(flow run)│ │  │
│  │  └───────────┘  │      │  autoscaler     │    │ ├──────────┤ │  │
│  └────────────────┘      │  scales nodes   │    │ │Container │ │  │
│                           │  up/down        │    │ │(flow run)│ │  │
│  ┌────────────────┐      └────────────────┘    │ ├──────────┤ │  │
│  │   Worker C      │                            │ │   ...    │ │  │
│  │   (CPU host-3)  │      ┌────────────────┐    │ └──────────┘ │  │
│  │   --limit 20    │      │  Node pool      │    │              │  │
│  │                 │      │  auto-scales    │    │ Cloud scales │  │
│  │  ┌───┐┌───┐    │      │  ┌────┐ ┌────┐  │    │ to zero when │  │
│  │  │run││run│... │      │  │node│ │node│  │    │ idle         │  │
│  │  └───┘└───┘    │      │  │ 1  │ │ 2  │  │    └──────────────┘  │
│  └────────────────┘      │  └────┘ └────┘  │                      │
│                           └────────────────┘                      │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                      SCALING COMPARISON                          │
│                                                                  │
│  Docker/Process       │  Kubernetes          │  Push/Managed     │
│  ──────────────       │  ──────────          │  ────────────     │
│  Manual               │  Auto (cluster       │  Auto (cloud      │
│  (add/remove          │  autoscaler adds     │  provider spins   │
│   worker machines)    │  nodes for pods)     │  containers       │
│                       │                      │  up/down)         │
│                       │                      │                   │
│  You: start/stop      │  You: set node       │  You: set pool    │
│  worker processes     │  pool min/max        │  concurrency      │
└──────────────────────────────────────────────────────────────────┘
```

---

## 11. Step-by-Step Scaling Path

### Step 1: Where You Are Now — `serve()` (Single Machine)

The current `orchestrator.py` uses `serve()`:

```python
# src/twin/orchestrator.py
serve(
    ingest_substack_rss_feed.to_deployment(
        name="ingest-substack-rss-feed-etl",
        tags=["data-pipeline", "substack"],
    ),
    memory_extraction.to_deployment(
        name="memory-extraction-etl",
        tags=["memory-pipeline", "extraction"],
    ),
    memory_materialization.to_deployment(
        name="memory-materialization-etl",
        tags=["memory-pipeline", "materialization"],
    ),
)
```

This starts a single long-lived process that listens for runs and executes them as local subprocesses.

```
┌──────────────────────────────────────────────┐
│              Single Machine                  │
│                                              │
│  ┌──────────────┐    ┌───────────────────┐   │
│  │Prefect Server│◄───│ orchestrator.py   │   │
│  │  (Docker)    │    │ serve() process   │   │
│  └──────────────┘    │                   │   │
│                      │ Runs ALL flows    │   │
│                      │ as subprocesses   │   │
│                      └───────────────────┘   │
│                                              │
│  Throughput: ~1 flow run at a time           │
│  Scaling: None                               │
│  Good for: Development, testing              │
└──────────────────────────────────────────────┘
```

**Limitations:**
- Single process executes everything
- If it crashes, all deployments go offline
- Can't distribute across machines
- No concurrency controls beyond in-flow `asyncio.gather()`

---

### Step 2: Docker Work Pool + Multiple Workers

Replace `serve()` with `flow.deploy()` targeting a Docker work pool.

**Updated `orchestrator.py`:**

```python
# src/twin/orchestrator.py — deploy mode

if __name__ == "__main__":
    ingest_substack_rss_feed.deploy(
        name="ingest-substack-rss-feed-etl",
        work_pool_name="cpu-pool",
        image="my-registry/twin:latest",
        tags=["data-pipeline", "substack"],
    )
    ingest_substack_rss_feed_batch.deploy(
        name="ingest-substack-rss-feed-batch-etl",
        work_pool_name="cpu-pool",
        image="my-registry/twin:latest",
        tags=["data-pipeline", "substack"],
    )
    memory_extraction.deploy(
        name="memory-extraction-etl",
        work_pool_name="cpu-pool",
        image="my-registry/twin:latest",
        tags=["memory-pipeline", "extraction"],
    )
    memory_materialization.deploy(
        name="memory-materialization-etl",
        work_pool_name="cpu-pool",
        image="my-registry/twin:latest",
        tags=["memory-pipeline", "materialization"],
    )
```

**Create the work pool and start workers:**

```bash
# One-time setup
prefect work-pool create "cpu-pool" --type docker

# Start workers on multiple machines
# Machine 1
prefect worker start --pool "cpu-pool" --type docker --limit 10 \
  --name "worker-$(hostname)"

# Machine 2
prefect worker start --pool "cpu-pool" --type docker --limit 10 \
  --name "worker-$(hostname)"

# Machine 3
prefect worker start --pool "cpu-pool" --type docker --limit 10 \
  --name "worker-$(hostname)"
```

```
┌───────────────────────────────────────────────────────────────┐
│                      Prefect Server                           │
│                                                               │
│   Work Pool: "cpu-pool" (type: docker, concurrency: 30)       │
│   ┌─────────────────────────────────────────────────────┐     │
│   │  Queue: 847 Scheduled │ 30 Running │ 123 Completed  │     │
│   └─────────────────────────────────────────────────────┘     │
└──────────┬──────────────────┬──────────────────┬──────────────┘
           │ poll              │ poll              │ poll
     ┌─────▼──────┐     ┌─────▼──────┐     ┌─────▼──────┐
     │  Worker A   │     │  Worker B   │     │  Worker C   │
     │  Machine 1  │     │  Machine 2  │     │  Machine 3  │
     │  --limit 10 │     │  --limit 10 │     │  --limit 10 │
     │             │     │             │     │             │
     │ ┌──┐┌──┐   │     │ ┌──┐┌──┐   │     │ ┌──┐┌──┐   │
     │ │C ││C │...│     │ │C ││C │...│     │ │C ││C │...│
     │ └──┘└──┘   │     │ └──┘└──┘   │     │ └──┘└──┘   │
     └─────────────┘     └─────────────┘     └─────────────┘
      10 containers       10 containers       10 containers
```

**Throughput: 30 concurrent flow runs across 3 machines.**

---

### Step 3: Auto-Scaling with Kubernetes or Push Pools

**The flow code doesn't change.** You only change the deployment target.

**Option A — Kubernetes:**

```python
memory_extraction.deploy(
    name="memory-extraction-etl",
    work_pool_name="k8s-pool",              # ← changed
    image="my-registry/twin:latest",
)
```

```bash
prefect work-pool create "k8s-pool" --type kubernetes
prefect worker start --pool "k8s-pool" --type kubernetes
```

One worker, but Kubernetes creates a pod per flow run and the cluster autoscaler handles node provisioning.

**Option B — Push Pool (Cloud Run):**

```python
memory_extraction.deploy(
    name="memory-extraction-etl",
    work_pool_name="cloudrun-pool",          # ← changed
    image="gcr.io/my-project/twin:latest",
)
```

```bash
prefect work-pool create "cloudrun-pool" --type cloud-run:push
# No worker to start — Prefect submits directly to Cloud Run
```

**Migration path:**

```
serve()  ──►  Docker pool + workers  ──┬──►  Kubernetes pool
                                       │
                                       └──►  Push pool (Cloud Run / ECS)
```

---

## 12. End-to-End Example: 1 Million Records

Imagine you have 1 million documents to ingest, extract knowledge from, and materialize into a queryable graph. Here's exactly how it flows through the system.

### Setup

```bash
# Create pools
prefect work-pool create "cpu-pool" --type docker --concurrency-limit 100
prefect work-pool create "gpu-pool" --type docker --concurrency-limit 4

# Protect the Gemini API from rate limiting
prefect gcl create "gemini-api" --limit 150

# Start CPU workers (3 machines, 30 concurrent each)
# Machine 1-3:
prefect worker start --pool "cpu-pool" --type docker --limit 30

# Start GPU workers (2 GPU machines, 1 concurrent each)
# GPU Machine 1-2:
prefect worker start --pool "gpu-pool" --type docker --limit 1
```

### Phase 1: Data Ingestion — 1M Runs Submitted

```python
# scripts/submit_million_jobs.py
async def submit_ingestion():
    async with get_client() as client:
        deployment = await client.read_deployment_by_name(
            "ingest-substack-rss-feed/ingest-substack-rss-feed-etl"
        )
        for feed_url in all_1_million_feed_urls:
            await client.create_flow_run_from_deployment(
                deployment.id,
                parameters={"feed_url": feed_url},
            )
```

**What happens in the system:**

```
 t=0s     1,000,000 flow runs enter "Scheduled" in cpu-pool
          ┌──────────────────────────────────────────────────────┐
          │ cpu-pool queue: 1,000,000 Scheduled                  │
          └──────────────────────────────────────────────────────┘

 t=15s    Workers poll, claim first batch (90 runs across 3 workers)
          ┌──────────────────────────────────────────────────────┐
          │ cpu-pool: 999,910 Scheduled │ 90 Running             │
          └──────────────────────────────────────────────────────┘
          Worker A (Machine 1): 30 Docker containers running
          Worker B (Machine 2): 30 Docker containers running
          Worker C (Machine 3): 30 Docker containers running

          Each container runs ingest_substack_rss_feed:
            fetch RSS → extract document → load to MongoDB

 t=30s    First runs complete (~5s each for I/O-bound RSS fetch)
          Workers immediately claim more from the queue
          ┌──────────────────────────────────────────────────────┐
          │ cpu-pool: 999,820 Scheduled │ 90 Running │ 90 Done  │
          └──────────────────────────────────────────────────────┘

 t=...    Steady state: 90 runs always in flight
          Queue drains at ~90 runs per batch (every few seconds)

          At 90 concurrent with ~5s per run:
          Throughput ≈ 18 runs/second ≈ 1,080/minute ≈ 64,800/hour
          1M records ≈ ~15.4 hours

          Want faster? Add more workers:
          6 workers × 30 limit = 180 concurrent → ~7.7 hours
          10 workers × 30 limit = 300 concurrent → ~4.6 hours

 t=end    All 1M documents ingested
          ┌──────────────────────────────────────────────────────┐
          │ cpu-pool: 0 Scheduled │ 0 Running │ 1,000,000 Done  │
          └──────────────────────────────────────────────────────┘
          MongoDB "documents" collection: 1,000,000 records
```

### Phase 2: Memory Extraction — 10K Runs (Batched)

Submit extraction in batches of 100 document IDs each:

```python
async def submit_extraction():
    async with get_client() as client:
        deployment = await client.read_deployment_by_name(
            "memory-extraction/memory-extraction-etl"
        )
        for batch in chunked(all_document_ids, 100):
            await client.create_flow_run_from_deployment(
                deployment.id,
                parameters={"document_ids": batch},
            )
# = 10,000 flow runs, each processing 100 documents
```

Each extraction flow run internally:
1. Chunks 100 documents (512 tokens, 64 overlap)
2. Calls Gemini API in parallel (semaphore=5 per flow run)
3. Normalizes nodes (fuzzy match, threshold=0.85)
4. Stores to `knowledge_graph_log`

```
          ┌──────────────────────────────────────────────────────┐
          │ cpu-pool: 10,000 extraction runs queued              │
          │                                                      │
          │ 90 running concurrently across 3 workers             │
          │ Each run has 5 concurrent LLM calls (semaphore)      │
          │ = 90 × 5 = 450 concurrent Gemini API calls           │
          │                                                      │
          │ But we set a global concurrency limit:               │
          │ prefect gcl "gemini-api" --limit 150                 │
          │ → only 150 LLM calls system-wide at any time         │
          │ → some runs block, waiting for API slots              │
          └──────────────────────────────────────────────────────┘

          Each run processes 100 docs:
          - ~500 chunks per run (avg 5 chunks per doc)
          - ~500 LLM calls per run (throttled by semaphore)
          - ~30-60 seconds per run

          At 90 concurrent, ~45s avg:
          Throughput ≈ 2 runs/second ≈ 200 docs/second
          1M documents ≈ ~1.4 hours

          Outputs: millions of NodeLogEntry + EdgeLogEntry records
          in "knowledge_graph_log" collection
```

### Phase 3: Materialization — 1 Run

A single flow run that aggregates everything:

```python
await client.create_flow_run_from_deployment(
    materialization_deployment.id
)
```

```
          ┌──────────────────────────────────────────────────────┐
          │ cpu-pool: 1 materialization run                      │
          │                                                      │
          │ Step 1: MongoDB aggregation pipeline                 │
          │   $group → $unionWith → $out "knowledge_graph"       │
          │   (server-side, runs in MongoDB, minutes for 1M)     │
          │                                                      │
          │ Step 2: Embed all nodes                              │
          │   Batch of 64 nodes → Gemini embedding API           │
          │   ~50K unique nodes → ~800 API calls                 │
          │   (throttled by gemini-api global concurrency)       │
          │                                                      │
          │ Step 3: Create reverse edges                         │
          │   Bidirectional traversal for $graphLookup            │
          │                                                      │
          │ Step 4: Ensure indexes                               │
          │   Text index + vector search index (mongot sync)     │
          └──────────────────────────────────────────────────────┘
```

### Full Timeline

```
TIME ────────────────────────────────────────────────────────────────────►

PHASE 1: DATA INGESTION (1M runs)
├───────────────────────────────────────────────────────────┤
│ ██████████████████████████████████████████████████████████ │  cpu-pool
│ 90 concurrent containers, draining 1M queue               │  3 workers
│ ~15 hours (or less with more workers)                     │
├───────────────────────────────────────────────────────────┤
                                                             │
PHASE 2: MEMORY EXTRACTION (10K runs)                        │
                                                             ├──────────────────┤
                                                             │ ████████████████ │ cpu-pool
                                                             │ 90 concurrent    │ 3 workers
                                                             │ 5 LLM calls each │
                                                             │ ~1.4 hours       │
                                                             ├──────────────────┤
                                                                                │
PHASE 3: MATERIALIZATION (1 run)                                                │
                                                                                ├─────┤
                                                                                │ ███ │
                                                                                │~30m │
                                                                                ├─────┤

MongoDB:
  "documents"              → 1,000,000 records
  "knowledge_graph_log"    → millions of node/edge log entries
  "knowledge_graph"        → deduplicated, embedded, indexed graph
```

### Concurrency Controls Across the Pipeline

Four layers prevent resource exhaustion:

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  Layer 1: WORK POOL CONCURRENCY                                 │
│  cpu-pool limit: 100 → max 100 runs across ALL workers          │
│  gpu-pool limit: 4   → max 4 GPU runs system-wide               │
│                                                                 │
│  Layer 2: WORK QUEUE PRIORITY                                   │
│  "critical" queue (priority 1) drains before "backfill" (10)    │
│  Extraction runs on "critical" → always processed first         │
│                                                                 │
│  Layer 3: WORKER --limit                                        │
│  Each worker caps its own concurrency                           │
│  CPU worker: --limit 30 (plenty of headroom)                    │
│  GPU worker: --limit 1  (exclusive GPU access)                  │
│                                                                 │
│  Layer 4: GLOBAL CONCURRENCY LIMITS                             │
│  "gemini-api" limit: 150 → max 150 LLM calls system-wide       │
│  Slot-based, enforced by server across all workers/machines     │
│  Prevents API rate limiting regardless of worker count          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 13. GPU vs CPU Work Pool Separation

Currently, this application uses the Gemini API (cloud-based) for LLM and embeddings — no local GPUs needed. But if you switch to local models (e.g., local LLaMA for extraction, local sentence-transformers for embedding), you'd separate pools by compute requirement:

### Pool Layout

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Prefect Server                               │
│                                                                     │
│  ┌──────────────────────────┐   ┌──────────────────────────┐        │
│  │ Work Pool: "cpu-pool"    │   │ Work Pool: "gpu-pool"    │        │
│  │ type: docker             │   │ type: docker             │        │
│  │ concurrency: 100         │   │ concurrency: 4           │        │
│  │                          │   │                          │        │
│  │ Deployments:             │   │ Deployments:             │        │
│  │ • ingest_substack_rss    │   │ • memory_extraction      │        │
│  │   (HTTP fetch, I/O)      │   │   (local LLM inference)  │        │
│  │ • materialization        │   │ • embed_nodes            │        │
│  │   (MongoDB aggregation)  │   │   (local embedding model)│        │
│  │ • reverse_edges          │   │ • fine_tuning (future)   │        │
│  │   (DB operations)        │   │   (model training)       │        │
│  │ • ensure_indexes         │   │                          │        │
│  │   (DB operations)        │   │                          │        │
│  └────────────┬─────────────┘   └────────────┬─────────────┘        │
└───────────────┼──────────────────────────────┼──────────────────────┘
                │                              │
    ┌───────────▼────────────┐     ┌───────────▼────────────┐
    │ CPU Workers            │     │ GPU Workers            │
    │ (cheap machines)       │     │ (GPU machines)         │
    │                        │     │                        │
    │ Worker 1: --limit 30   │     │ Worker 1: --limit 1    │
    │   (8 vCPU, 16GB RAM)   │     │   (1× A100 80GB)      │
    │ Worker 2: --limit 30   │     │ Worker 2: --limit 1    │
    │   (8 vCPU, 16GB RAM)   │     │   (1× A100 80GB)      │
    │ Worker 3: --limit 30   │     │ Worker 3: --limit 2    │
    │   (8 vCPU, 16GB RAM)   │     │   (2× T4 16GB)        │
    │                        │     │                        │
    │ 90 concurrent runs     │     │ 4 concurrent runs      │
    │ I/O + aggregation      │     │ inference + training   │
    └────────────────────────┘     └────────────────────────┘
```

### Pipeline-to-Pool Mapping

| Pipeline | Pool | Why | `--limit` |
|----------|------|-----|-----------|
| `ingest_substack_rss_feed` | `cpu-pool` | HTTP fetch + parse, I/O bound | 30 |
| `memory_extraction` (Gemini API) | `cpu-pool` | Network calls to cloud API, I/O bound | 30 |
| `memory_extraction` (local LLM) | `gpu-pool` | Local inference needs VRAM | 1 |
| `memory_materialization` — aggregate | `cpu-pool` | MongoDB aggregation, server-side compute | 30 |
| `memory_materialization` — embed (Gemini API) | `cpu-pool` | Network calls, I/O bound | 30 |
| `memory_materialization` — embed (local model) | `gpu-pool` | Local embedding model needs VRAM | 1-2 |
| Future: fine-tuning | `gpu-pool` | Training needs VRAM | 1 |

### Deploying with GPU Access

```python
# CPU-bound pipeline → cpu-pool
ingest_substack_rss_feed.deploy(
    name="ingest-substack-rss-feed-etl",
    work_pool_name="cpu-pool",
    image="my-registry/twin:latest",
    tags=["data-pipeline"],
)

# GPU-bound pipeline → gpu-pool with device_requests
memory_extraction.deploy(
    name="memory-extraction-etl",
    work_pool_name="gpu-pool",
    image="my-registry/twin-gpu:latest",
    job_variables={
        "device_requests": [
            {
                "Driver": "nvidia",
                "Capabilities": [["gpu"]],
                "Count": 1,  # 1 GPU per container
            }
        ]
    },
    tags=["memory-pipeline", "gpu"],
)
```

### Cost Optimization Pattern

Run GPU workers only when needed. CPU workers run always:

```
TIME ────────────────────────────────────────────────────────────────►

CPU WORKERS (always running, cheap)
│████████████████████████████████████████████████████████████████████│
│ Ingestion     │ idle │ Materialization (agg + index)  │ idle      │

GPU WORKERS (on-demand, expensive)
                      │████████████████│
                      │ Extraction     │
                      │ (local LLM)    │
                      │ Start when     │
                      │ extraction     │
                      │ runs queued,   │
                      │ stop when done │

With push pools (Cloud Run GPU / Vertex AI), this happens automatically.
With Docker pools, you script it:
  - Monitor queue depth via Prefect API
  - Start GPU VMs when extraction runs appear
  - Stop GPU VMs when queue is empty
```

---

## Summary

```
TODAY                       NEXT STEP                    PRODUCTION
─────                       ─────────                    ──────────

serve()                     Docker pool                  K8s or Push pool
1 process                   + N workers                  auto-scaling
1 machine                   N machines
sequential                  30-90 concurrent             hundreds concurrent

orchestrator.py             orchestrator.py              orchestrator.py
  serve(...)                  flow.deploy(...)             flow.deploy(...)
                              pool="cpu-pool"              pool="k8s-pool"
                              pool="gpu-pool"              pool="gpu-pool"

No concurrency              4 layers of control:         Same controls +
controls                    pool, queue, worker,         cloud auto-scaling
                            global limits

1M records: weeks           1M records: ~17 hours        1M records: hours
                            (ingestion + extraction      (scale workers
                             + materialization)           horizontally)
```

**The key insight:** your flow code (`ingest_substack_rss_feed`, `memory_extraction`, `memory_materialization`) doesn't change at all when you scale. You only change the deployment target — from `serve()` to `deploy(work_pool_name=...)` — and the infrastructure scales beneath it.
