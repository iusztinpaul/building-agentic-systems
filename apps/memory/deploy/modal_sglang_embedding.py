"""Serve ONE **Embedding catalog** model with SGLang — the `sglang` **Serving path**.

The EJECT PATH (ADR-009 §2): Modal's own generated `serve.py` for a
**Dedicated endpoint**, copied and parameterised by the catalog, for a model
Modal's managed recipe has no base model for but SGLang supports natively
(Qwen3-Embedding, BGE, E5, GTE-Qwen2, EmbeddingGemma). The preferred path is
`modal endpoint create` and costs no code at all; take this one only when that
failed (see the ladder in `apps/memory/README.md`).

A deliberate second copy of the vLLM script rather than a shared skeleton: the
engines differ in image, launcher and flags, and ADR-009 keeps two boring
copies until a third engine arrives.

Glue only: every decision — GPU, engine pin, revision, server flags — is
resolved by `tree.models.modal_catalog` on the operator's machine and crosses
into the container as ONE JSON env var baked into the image
(`EMBEDDING_DEPLOY_SPEC`, ADR-009 §3), because Modal re-imports this file
INSIDE the container, where the `tree` package is NOT installed. Hence the
`modal.is_local()` split below: nothing under `tree` may be imported outside it.

The app is `ep-<endpoint_name>` with `class Server` — the same SHAPE
(`ep-<name>`, class `Server`) a Dedicated endpoint has, inside our `tree-`
namespace, so ONE `modal.Server.from_name(app_name, "Server")` lookup resolves
every Serving path and the client stays path-blind. The shape is what the
lookup needs; the prefix is what keeps this deploy off an endpoint the operator
created by hand (`ep-<model>`, ADR-009 §3). A model is served by exactly one
path at a time: stop the endpoint before deploying this.
Auth is Modal **Proxy tokens** only (`unauthenticated=False`) — the edge answers
401 before a GPU wakes, so the engine needs no key of its own.

Deployed by the driver, never by hand:

    make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B SERVING=sglang
"""

import json
import logging
import os

import modal

# Seconds, so `5 * MINUTES` reads as a duration in the decorator below.
MINUTES = 60

# The port the engine listens on and the one Modal proxies to; they must match.
PORT = 8000

# Modal terminates a container that is not serving after `startup_timeout`.
# The engine's own health wait is strictly SHORTER, so a model that cannot load
# fails with SGLang's message in `modal app logs` instead of an opaque Modal
# termination at the same instant.
STARTUP_TIMEOUT = 20 * MINUTES
HEALTH_TIMEOUT = 18 * MINUTES

# The env var the resolved catalog entry crosses into the container in. The
# name is `tree.models.modal_catalog.DEPLOY_SPEC_ENV`, spelled out because
# `tree` is not importable here on the container side (a unit test pins the
# two together).
DEPLOY_SPEC_ENV = "EMBEDDING_DEPLOY_SPEC"

# Two texts for the one request `@modal.enter` makes before declaring the
# server up: a request that returns well-shaped vectors proves far more than a
# 200 on /health. Ranking quality is the driver's smoke test, not this.
PROBES = ["what does this server embed?", "It embeds text into vectors."]

if modal.is_local():
    from tree.logging import init_logger
    from tree.models.modal_catalog import build_deploy_spec, hf_token_env

    init_logger()
    # The engine is the SCRIPT's, never the entry's `serving`, so
    # `SERVING=sglang` on an `endpoint` entry works without editing the YAML.
    SPEC = build_deploy_spec(os.environ["EMBEDDING_MODEL"], "sglang").model_dump()
    # The Hugging Face token (optional, ADR-009 §9) travels as an EPHEMERAL
    # Secret built here, on the operator's machine — never in the image env
    # beside the spec, because image layers are cached and inspectable. It is
    # the only Secret this app has, and it is empty unless `.env` sets one.
    HF_SECRET = modal.Secret.from_dict(hf_token_env())
else:
    # `force=True`: Modal's runtime imports its own client before re-importing
    # this file, so the root logger may already carry a handler — plain
    # `basicConfig` would then be a no-op, root would stay at WARNING and the
    # one boolean line ADR-009 §9 rests on would never reach `modal app logs`.
    logging.basicConfig(level=logging.INFO, force=True)
    SPEC = json.loads(os.environ["EMBEDDING_DEPLOY_SPEC"])
    # Re-import inside the container: the local dict was already inlined into
    # the deployed Secret, so this side must only match its SHAPE (one element).
    HF_SECRET = modal.Secret.from_dict({})

logger = logging.getLogger(__name__)

image = (
    modal.Image.from_registry("nvidia/cuda:13.0.2-devel-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .uv_pip_install(
        f"sglang=={SPEC['engine_version']}",
        f"autoinference-utils=={SPEC['autoinference_utils_version']}",
        "httpx",
        "huggingface-hub",
    )
    .env({"HF_XET_HIGH_PERFORMANCE": "1", DEPLOY_SPEC_ENV: json.dumps(SPEC)})
)

app = modal.App(SPEC["app_name"])


@app.server(
    image=image,
    gpu=SPEC["gpu"],
    cpu=SPEC["cpu"],
    memory=SPEC["memory_mb"],
    min_containers=0,
    scaledown_window=5 * MINUTES,
    port=PORT,
    routing_region="eu-west",
    unauthenticated=False,
    exit_grace_period=25,
    startup_timeout=STARTUP_TIMEOUT,
    target_concurrency=16,
    secrets=[HF_SECRET],
    volumes={
        "/root/.cache/huggingface": modal.Volume.from_name(
            "huggingface-cache", create_if_missing=True
        )
    },
)
class Server:
    """The SGLang process this container exists to keep alive."""

    @modal.enter()
    def start(self) -> None:
        """Start SGLang and prove it embeds before the container serves traffic."""

        # FIRST line: the ONE observable proof the Secret arrived — the
        # boolean, never the value. `False` next to a 401/403 from
        # huggingface.co in `modal app logs` is the gated-repo diagnosis, and
        # logging it before any import keeps it readable even when the image
        # itself is broken.
        logger.info("HF_TOKEN set in container: %s", bool(os.environ.get("HF_TOKEN")))

        # `autoinference-utils` lives only inside this image, so it is imported
        # at container start — not at module level, where the deploying machine
        # would have to have it installed.
        from autoinference_utils.endpoint import (
            SGLangEndpoint,
            validate_embeddings_endpoint,
        )

        # `model_path=`, not `model=`: SGLang's launcher takes `--model-path`
        # (autoinference-utils 0.2.6, endpoint.py:122-235).
        self.endpoint = SGLangEndpoint(
            model_path=SPEC["repo_id"],
            worker_port=PORT,
            extra_server_args=SPEC["server_args"],
            health_timeout=float(HEALTH_TIMEOUT),
            health_poll_interval=5.0,
        )
        self.endpoint.start()

        validate_embeddings_endpoint(
            port=PORT,
            payload={
                "model": SPEC["repo_id"],
                "input": PROBES,
                "encoding_format": "float",
            },
            request_timeout=60.0,
        )
        logger.info(
            "SGLang serving %s (revision %s) on port %d",
            SPEC["repo_id"],
            SPEC["revision"],
            PORT,
        )

    @modal.exit()
    def stop(self) -> None:
        """Terminate the engine so a scaled-down container stops billing."""

        self.endpoint.stop()
