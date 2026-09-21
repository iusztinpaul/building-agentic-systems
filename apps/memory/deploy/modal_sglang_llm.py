"""Serve ONE **Modal catalog** LLM with SGLang — the `app` route for LLMs.

LLMs from Hugging Face, nothing else (ADR-009 §2): the recipe Modal's own
generated `serve.py` uses for its LLM endpoints (`openai/gpt-oss-120b` and
`Qwen/Qwen3.6-35B-A3B-FP8`, read 2026-09-20) — the OFFICIAL `lmsysorg/sglang`
docker image (no CUDA base, no `add_python`, no pip-installed engine), the same
`autoinference-utils` helpers, `SGLangEndpoint(tp=<n_gpus>)`, and a warm-up
that is a chat completion under a STRICT JSON SCHEMA: a server that cannot do
constrained JSON never reports healthy, so the memory never talks to one.

GENERIC-SAFE flags only. Modal's two recipes also carry speculative decoding
with a model-specific draft model, mamba scheduler and multimodal flags, and
per-model image env — tuning for a 120B and a 35B-MoE model, which a script
serving whatever the catalog names must not assume. What is left is the subset
any LLM takes (`--served-model-name`, `--revision`, `--trust-remote-code`,
`--mem-fraction-static`, `--context-length`); a model that wants
`--reasoning-parser` / `--tool-call-parser` names them in its entry's
`extra_server_args`.

Glue only: every decision — GPU, engine pin, revision, server flags — is
resolved by `tree.models.modal_catalog` on the operator's machine and crosses
into the container as ONE env var baked into the image (`LLM_DEPLOY_SPEC`,
ADR-009 §3), because Modal re-imports this file INSIDE the container, where the
`tree` package is NOT installed. Hence the `modal.is_local()` split below:
nothing under `tree` may be imported outside it.

That env var holds the BASE64 of the spec's JSON — `base64 -d` reads a
deployed layer back. Modal renders an image env var as a Dockerfile
`ENV {key}={shlex.quote(value)}` (modal 1.5.5, `_image.py:2821`) and the image
build unescaped every backslash of the value, so the raw JSON of a spec whose
values are themselves JSON arrived corrupted and the container it belonged to
died at import (live, 2026-09-21; this script's quote-free spec crossed
intact, which is what hid the defect). Base64's alphabet leaves no quote,
backslash or `$` for any quoting layer to interpret.

The app is `ep-<endpoint_name>` with `class Server` — the same SHAPE
(`ep-<name>`, class `Server`) a Dedicated endpoint has, inside our `tree-`
namespace, so ONE `modal.Server.from_name(app_name, "Server")` lookup resolves
both routes and the client stays path-blind. The shape is what the lookup
needs; the prefix is what keeps this deploy off an endpoint the operator
created by hand (`ep-<model>`, ADR-009 §3), and the driver's existence guard
refuses before it writes over anything.
Auth is Modal **Proxy tokens** only (`unauthenticated=False`) — the edge answers
401 before a GPU wakes, so the engine needs no key of its own.

Where this file deviates from Modal's own template, on purpose:

* weights by `repo_id` + `revision` into the shared `huggingface-cache` Volume
  — Modal's `MODEL_PATH` is a snapshot inside the endpoint's own volume, which
  nothing outside that endpoint can mount;
* `unauthenticated=False` spelled as a literal, never `not REQUIRE_AUTH`;
* the stdlib logger instead of `print`, so nothing but one boolean about the
  Hugging Face token can reach `modal app logs`;
* GPU, CPU, memory and tensor parallelism come from the catalog entry instead
  of the per-recipe module constants Modal bakes in.

Deployed by the driver, never by hand:

    make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M
"""

import base64
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
# termination at the same instant. Both are the ENGINE's budget for starting
# up, unrelated to `modal.warmup_deadline_s`, which is how long a CLIENT polls
# a cold server from outside (ADR-009 §11).
STARTUP_TIMEOUT = 20 * MINUTES
HEALTH_TIMEOUT = 18 * MINUTES

# Where the shared `huggingface-cache` Volume mounts, and the root the image
# env derives every Hugging Face cache path from. NOT the default cache under
# the home directory: Modal refuses with `cannot mount volume on non-empty
# path` when an image already ships files there — the official SGLang image
# COPYs a kernels cache into it (live, 2026-09-21). ONE path for both engines,
# so ONE Volume layout: its root holds `hub/`, where `HF_HOME` puts it again.
HF_CACHE_DIR = "/cache/huggingface"

# The env var the resolved catalog entry crosses into the container in. The
# name is `tree.models.modal_catalog.LLM_DEPLOY_SPEC_ENV`, spelled out because
# `tree` is not importable here on the container side (a unit test pins the two
# together).
DEPLOY_SPEC_ENV = "LLM_DEPLOY_SPEC"

# What the container says when that variable does not decode. A corrupted
# value is a TRANSPORT failure, so the import dies naming the variable instead
# of falling back to a default nobody deployed — the crash-loop that made this
# transport base64 was diagnosed from exactly such an import traceback.
SPEC_DECODE_ERROR = (
    "LLM_DEPLOY_SPEC does not hold the base64 of a JSON object "
    "(tree.models.modal_catalog.encode_deploy_spec writes it). Redeploy with "
    "`make memory-deploy-model MODEL=<repo_id>`."
)

if modal.is_local():
    from tree.logging import init_logger
    from tree.models.modal_catalog import (
        build_llm_deploy_spec,
        encode_deploy_spec,
        hf_token_env,
    )

    init_logger()
    # The engine is the SCRIPT's: an entry names no engine, and the router
    # sends every LLM Modal refuses to this file.
    RESOLVED_SPEC = build_llm_deploy_spec(os.environ["MODAL_MODEL"])
    SPEC = RESOLVED_SPEC.model_dump()
    # ONE name on both sides of the split, and the container ECHOES the string
    # it was given rather than re-encoding `SPEC`: Pydantic's
    # `model_dump_json()` and a hand-rolled serialisation differ in separators,
    # so recomputing the value on the re-import would change the image
    # definition Modal compares.
    SPEC_ENV_VALUE = encode_deploy_spec(RESOLVED_SPEC)
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
    SPEC_ENV_VALUE = os.environ["LLM_DEPLOY_SPEC"]
    # `validate=True`: a value corrupted in transit must RAISE here, not be
    # silently stripped of the bytes that are not base64 and then parsed into
    # some other spec.
    try:
        SPEC = json.loads(base64.b64decode(SPEC_ENV_VALUE, validate=True))
    except ValueError as error:
        raise RuntimeError(SPEC_DECODE_ERROR) from error
    if not isinstance(SPEC, dict):
        raise RuntimeError(SPEC_DECODE_ERROR)
    # Re-import inside the container: the local dict was already inlined into
    # the deployed Secret, so this side must only match its SHAPE (one element).
    HF_SECRET = modal.Secret.from_dict({})

logger = logging.getLogger(__name__)

# The request `@modal.enter` makes (twice) before declaring the server up —
# Modal's own warm-up payload, value for value. It is a chat completion under a
# STRICT JSON schema, so two successes prove the whole path the memory uses:
# the chat template, the tokenizer, the decoder AND the grammar backend
# `ModalLLM`'s JSON mode relies on. A model that ignores `response_format` never gets
# two successes, so its container never serves traffic.
WARMUP_PAYLOAD = {
    "model": SPEC["repo_id"],
    "messages": [{"role": "user", "content": "Reply with JSON facts about Tokyo."}],
    "max_tokens": 64,
    "temperature": 0,
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "city_facts",
            "schema": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "population": {"type": "integer"},
                },
                "required": ["city", "population"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    },
}

image = (
    # The OFFICIAL SGLang image, which already carries the engine, its kernels
    # and a Python 3.12 on PATH — so no CUDA base, no `add_python` and no
    # `.entrypoint([])` (the image's own is `CMD ["/bin/bash"]`). The pin is a
    # DOCKER TAG from the catalog, not a PyPI version.
    modal.Image.from_registry(f"lmsysorg/sglang:{SPEC['engine_version']}")
    .uv_pip_install(f"autoinference-utils=={SPEC['autoinference_utils_version']}")
    # `HF_HUB_CACHE` as well as `HF_HOME`: it names the same directory but
    # OUTRANKS both `HF_HOME` and the legacy `HUGGINGFACE_HUB_CACHE`
    # (`huggingface_hub/constants.py`), so an image that sets a cache variable
    # of its own cannot move the weights off the Volume.
    .env(
        {
            "HF_XET_HIGH_PERFORMANCE": "1",
            "HF_HOME": HF_CACHE_DIR,
            "HF_HUB_CACHE": f"{HF_CACHE_DIR}/hub",
            DEPLOY_SPEC_ENV: SPEC_ENV_VALUE,
        }
    )
)

app = modal.App(SPEC["app_name"])


@app.server(
    image=image,
    # `<type>:<count>` — the same string Modal's own recipe builds from its
    # `GPU_TYPE` and `N_GPUS`; the same `n_gpus` is SGLang's `tp` below.
    gpu=f"{SPEC['gpu']}:{SPEC['n_gpus']}",
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
        HF_CACHE_DIR: modal.Volume.from_name(
            "huggingface-cache", create_if_missing=True
        )
    },
)
class Server:
    """The SGLang process this container exists to keep alive."""

    @modal.enter()
    def start(self) -> None:
        """Start SGLang and prove it answers in strict JSON before serving."""

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
            warmup_chat_completions,
        )

        # `model_path=`, not `model=`: SGLang's launcher takes `--model-path`
        # (autoinference-utils 0.2.6, endpoint.py:122-235). No
        # `speculative_model_path`: a draft model is per-model tuning, and the
        # parameter is optional there (`Optional[str] = None`).
        self.endpoint = SGLangEndpoint(
            model_path=SPEC["repo_id"],
            worker_port=PORT,
            tp=SPEC["n_gpus"],
            extra_server_args=SPEC["server_args"],
            health_timeout=float(HEALTH_TIMEOUT),
            health_poll_interval=5.0,
        )
        self.endpoint.start()

        warmup_chat_completions(
            port=PORT,
            payload=WARMUP_PAYLOAD,
            successful_requests=2,
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
