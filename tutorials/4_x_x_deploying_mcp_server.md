# Deploying the MCP Server

A quick note on what it takes to deploy the Tree FastMCP server to Prefect Horizon. It assumes the cloud stack from [Deploying to the Cloud](2_4_2_deploying_to_the_cloud.md) is already up: the Atlas cluster, a signed-up user, and the Prefect Cloud pipelines with at least one indexing run.

## Order of Operations

Each step depends on the previous one at boot time:

1. **MongoDB Atlas** (`make memory-atlas-up`). Allow-list Horizon's egress IPs through `ATLAS_ACCESS_CIDRS`, or the server can't reach the cluster.
2. **User sign-up** (`make memory-signup IDENTIFIER=<email> NAME="<name>"`). The server refuses to boot when `TREE_USER_IDENTIFIER` matches no user.
3. **Prefect Cloud pipelines** (`make memory-deploy-prefect-setup-up`, then one indexing run). The indexing run creates the Atlas Search indexes. The MCP server only queries them, so search returns nothing until this run has happened.
4. **Horizon.** Connect the GitHub repo and set the entrypoint to `apps/memory/src/tree/mcp/server.py:mcp`. Every push to `main` redeploys, and every PR gets a preview deployment.

## Environment Variables

Horizon reads its environment from its own dashboard (Settings → Environment), not from `.env.prod`. On top of the credentials from `.env.example`, it needs three Horizon-specific values:

| Variable | Why |
| --- | --- |
| `TREE_USER_IDENTIFIER` | The user from step 2. Boot fails loudly without it. |
| `MCP_SKIP_INDEX_BOOTSTRAP=true` | Creating indexes and waiting for mongot to sync takes minutes, which blows Horizon's 60s readiness window. |
| `TREE_WORKING_DIR=/tmp/.tree` | The install dir is read-only. Rendered graph HTML files and deep-search sessions go here. |

There is no CLI or public API for setting Horizon env vars: `fastmcp login`/`whoami`/`logout` only manage your local sign-in. Instead, the **Add variable** dialog accepts a pasted `.env` file (⌘V / Ctrl+V), which adds every key at once. So the sync routine is:

1. Copy `.env.prod` plus the three rows above.
2. Paste it into **Add variable**, scoped to **Production**.
3. **Redeploy.** New values apply only on the next deployment, so a rotated key does nothing until you push or redeploy.

Good example: rotate `PREFECT_API_KEY` in Prefect Cloud, paste the new value, redeploy, then re-run the smoke test below. Bad example: rotate the key in `.env.prod` only. Horizon keeps the stale key, every ingest fails with a Prefect `401 Unauthorized`, and the tool reports it as "Prefect API unreachable".

**Idea (not built):** have the server load its rotating secrets (Voyage, Bright Data, Gemini, Opik, Mongo) from Prefect Secret blocks at startup. Horizon would then hold only `PREFECT_API_URL` and `PREFECT_API_KEY`, so most key rotations would need no Horizon edit at all.

## Auth and Clients

Auth is platform-level: Horizon OAuth plus org membership, so the endpoint answers 401 without a bearer token. Claude Code connects through `.mcp.json` (`"type": "http", "url": "https://tree-memory.fastmcp.app/mcp"`) and signs in with `/mcp`. Python clients use `tree.mcp.client.get_cloud_client` (`auth="oauth"`).

## Two Remote-Only Behaviours

- **Uploading local files.** The server never touches your filesystem. The client reads the file and sends its text to `ingest_file`, and the local path only serves as the dedup key (`file://<path>`). The server then submits an `online-pipeline` flow run on Prefect Cloud, which writes the chunks to Atlas. The tool returns as soon as the run is submitted, so the file isn't searchable yet when the call comes back.
- **Downloading visualizations.** With `as_html_file=true`, the HTML is written to the server's `/tmp/.tree/graphs/`, which you can't reach. The result also carries a `graphs://<name>.html` resource link: read that resource and save its text locally as `.html`. The file loads sigma, graphology and d3-force from jsdelivr, so it needs internet to render. Known limit: a full embedding map (~17k points, ~11 MB) fails on Horizon with JSON-RPC `-32603`, while structure views (0.2–0.7 MB) download fine.

## Smoke Test After Each Deploy

Call every tool against the `tree-memory` server, then check Horizon's two log views:

- **Traffic Logs** list each MCP request with its status and duration. Click a red row to see the JSON-RPC error.
- **Deployments → View server logs** shows the Python tracebacks behind those errors.

Minimum pass: `search_memory` returns `outcome: "found"`, and `ingest_file` returns a `flow_run_id` whose run lands in Atlas. After that, search for a marker phrase you put in the ingested file.

## Teardown

In reverse order: delete the Horizon server (UI), then `make memory-deploy-prefect-setup-down`, then `make memory-atlas-down`.
