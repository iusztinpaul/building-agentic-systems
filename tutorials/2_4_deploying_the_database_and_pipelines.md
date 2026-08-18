# Deploying the Database & Pipelines

This is the tutorial used to deploy the MongoDB database and Prefect pipelines from Chapter 2, section `2.4 Deploying the Database & Pipelines` of the book.

## Managing Environment Variables

When working only locally, it's easy to keep all your environment variables in a single `.env` file. But as you keep adding environments, such as a staging or production one, you want to easily switch between the MongoDB, Prefect, or other credentials specific to that environment without manually overriding the `.env` file or commenting out what you don't need. That is cumbersome, not very secure, and you can easily introduce bugs by mixing credentials from different environments. The process became even more important when we started using coding agents for all our projects.

Thus, what we want is a single command that easily switches between environments, and a different environment file for each one. This allows us or the coding agent to easily experiment across local, staging, or production.

In our use case, we created a couple of Make commands that switch between environments:

```bash
make env-local
make env-prod
make env-status # print the current active environment
```

These switch between two files: `.env` and `.env.prod`. Both follow the interface defined in `.env.example` and are easily extensible to a staging environment.

The Make commands simply update a local `.env.target` file with the enum `local` or `prod`. Then, whenever we run a Make command, based on the state from the `.env.target` file, we load either `.env` or `.env.prod`.

Because we want this to work outside Make commands as well, we also leverage `direnv`, a small program that executes the code from `.envrc`. Our `.envrc` contains a `watch_file .env.target` command, so every time `.env.target` changes, `direnv` reloads the environment variables from either `.env` or `.env.prod`, based on the latest state from `.env.target`. Thus, whenever we change the state, all future terminal commands get injected with the environment variables of the currently active environment.

Using this strategy ensures that our code is modularized enough to easily switch environments, which means we can inject the same variables from different setups, such as our CI/CD pipelines or the Prefect Cloud deployment.

**Check:** confirm the right environment is active before deploying:

```bash
make env-status  # must print: "Env target: prod (.env.prod)"
```

If it prints `local`, every command that follows in this section runs against your local Docker stack instead of the cloud deployment. Run `make env-prod` to fix.

## MongoDB Atlas: The Warehouse, Managed

To deploy a MongoDB cluster to MongoDB Atlas, we created a Python script at [apps/memory/deploy/atlas_cluster.py](../apps/memory/deploy/atlas_cluster.py), callable via a couple of Make commands that allow us to easily create, update, or destroy the cluster.

```bash
make memory-atlas-up
make memory-atlas-update
make memory-atlas-down
make memory-atlas-status # Print the cluster state and connection strings.
```

Within the [apps/memory/deploy/atlas_cluster.py](../apps/memory/deploy/atlas_cluster.py) script, to keep it simple, we avoided using infrastructure-as-code tools such as Terraform or Pulumi. Thus, we used Atlas's API via vanilla Python scripting to interface with the cluster.

To make this work, you first have to create an account on Atlas via MongoDB's main page (https://www.mongodb.com/), where they provide their M0 free tier with 512 MB of storage.

Next, the only manual steps you need to perform are to get the right credentials. To do so, you need to go to MongoDB's Atlas dashboard, create a project, then a service account, and ultimately add your IP to the allowed list.

Next, you need to put the client ID and secret into `.env.prod` as the `MDB_MCP_API_CLIENT_ID` and `MDB_MCP_API_CLIENT_SECRET` env vars, which will be used by the script to create everything else. Note that even though we have `_MCP_` in the name, we don't use any MCP here. It's a constraint that comes from MongoDB, as they also provide an MCP server to interact with the cluster that requires these names, which we initially used as inspiration to create the script. So, even though you don't need their MCP server, if you install it, it will work.

Before you run the script, you also need to set up, only in your `.env.prod` file, the `MONGO_INITDB_ROOT_USERNAME` and `MONGO_INITDB_ROOT_PASSWORD` env vars, which the script will use to automatically create your admin user. To generate the password, you can run `make generate-password`.

![Figure 2.22 The cluster in the Atlas console](assets/figure_2_22_atlas_cluster.png)

**Figure 2.22 The cluster in the Atlas console**

For more details on the MongoDB Atlas setup, check [docs/setup/mongodb_atlas.md](../docs/setup/mongodb_atlas.md).

Now, let's run the script that will create the M0 cluster, seed the database user (the same `MONGO_INITDB_ROOT_USERNAME` / `MONGO_INITDB_ROOT_PASSWORD`), and open network access to Prefect Cloud:

```bash
make env-prod
make memory-atlas-up
```

Outputs:

```
Cluster tree state=CREATING
Cluster tree state=IDLE
mongodb+srv://tree.….mongodb.net
```

Ultimately, after the cluster is up, you need to take the MongoDB host name from the up command and add it as the `MONGO_HOST` environment variable in `.env.prod`.

```bash
make env-prod
make memory-atlas-up  # prints mongodb+srv://tree.xxxxxxx.mongodb.net
# → take the host part (no scheme) into MONGO_HOST in .env.prod
# → ensure MONGO_SCHEME=mongodb+srv
make memory-check-db  # verifies the connection actually works
```

**Check:** verify the cluster is up and reachable before moving on:

```bash
make memory-atlas-status  # cluster state=IDLE + connection strings
make memory-check-db      # connects with your .env.prod credentials; exits non-zero on failure
```

`state=IDLE` means the cluster is provisioned and healthy (`CREATING` means Atlas is still working). A passing `check-db` proves the `MONGO_HOST` and credentials in `.env.prod` are correct by actually connecting.

As with the local setup, you can use MongoDB Compass GUI or mongosh CLI to look around the database by using the connection string from the `make memory-atlas-status` command.

![Figure 2.23 The database visualized in MongoDB Compass](assets/figure_2_23_mongodb_compass.png)

The final step is to deploy our code to Prefect Cloud.

## Prefect Cloud: The Pipelines, Hosted

To create the Prefect deployments, you first need to create an account and workspace at Prefect Cloud (https://app.prefect.cloud). Similar to the MongoDB setup, we automated most of the process, while you only need to set up a few credentials.

From Prefect, you need to take the `PREFECT_API_URL` and `PREFECT_API_KEY` environment variables and add them to `.env.prod`. In case Prefect needs to clone a private repository from GitHub, you also need to set up an optional `GITHUB_PAT` (a personal access token) env var.

Now, by running the [apps/memory/deploy/prefect_pipelines_setup.py](../apps/memory/deploy/prefect_pipelines_setup.py) script, we can create, update, or tear down a Prefect work pool containing all our deployments registered within `_DEPLOYMENT_SPECS` (from [apps/memory/src/tree/orchestrator.py](../apps/memory/src/tree/orchestrator.py)).

Before running the script, we must ensure that we have a user within our database by running the `make memory-signup` command. Additionally, through the `GROUPS` argument, we control whether we spin up only the data deployments, only the memory deployments, or all of them. At the moment we aim to deploy just the data pipelines.

```bash
make memory-signup USER_IDENTIFIER=you@example.com
make memory-deploy-prefect-setup-up GROUPS=data
make memory-deploy-prefect-setup-update GROUPS=data
make memory-deploy-prefect-setup-down GROUPS=data
make memory-deploy-prefect-setup-status
```

Outputs:

```
Seeded 14 runtime config store(s).
Created prefect:managed work pool 'tree-managed'.
Deployed 2 pipeline(s) to tree-managed: data-etl-coordinator, …
```

After running the setup-up command, you should see within your Prefect Cloud dashboard a setup similar to this:

![Figure 2.24 The pipelines in Prefect Cloud](assets/figure_2_24_prefect_cloud.png)

**Check:** verify the user and the deployments exist:

```bash
make memory-whoami                       # prints the current user (id, identifier, name)
make memory-deploy-prefect-setup-status  # work pool + each deployment's binding
```

The status command must list the `tree-managed` work pool with each deployment bound to it. A missing deployment means `setup-up` didn't finish.

As for the MongoDB Atlas setup, we have the full setup available at [docs/setup/prefect_cloud.md](../docs/setup/prefect_cloud.md) within the repository.

## Running the Backfill in the Cloud

The last step is to run the backfill within the cloud environment. If everything is set up correctly, just by pointing to the `.env.prod` file instead of the local `.env`, we can continue executing the exact same commands, as they will automatically switch from the local Docker setup to the cloud one:

```bash
make memory-run-data-pipeline SOURCE_FILE="sources/backfill.yaml"
```

After the flow successfully completes, within Prefect Cloud's flow tab, you should see something similar to the image below:

![Figure 2.17 The backfill in the Prefect UI. One data-etl-coordinator run dispatches five data-etl-worker runs](assets/figure_2_17_prefect_backfill.png)

**Check:** confirm the backfill actually landed in the cloud database:

```bash
make memory-check-db  # lists databases + collection counts
```

Outputs something like this:

```text
tree.documents: 10242 docs
tree.sessions: 1 docs
tree.users: 1 docs
```

A non-zero `users` and `documents` count means the user was successfully created and the pipelines wrote to Atlas, not to your local Docker instance.

To sum up, to deploy the whole stack and run the backfill, you need to run the following 5 commands, in this order:

```bash
make env-prod
make memory-atlas-up
make memory-signup USER_IDENTIFIER=you@example.com
make memory-deploy-prefect-setup-up GROUPS=data
make memory-run-data-pipeline SOURCE_FILE="sources/backfill.yaml"
```
