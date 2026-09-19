"""Deploy, smoke-test and stop ONE **Embedding catalog** model on Modal.

Make cannot read YAML, so this driver turns ``MODEL=<repo_id>`` into the
``modal`` command the entry's **Serving path** needs (ADR-009 §2/§3):

* ``endpoint`` — ``modal endpoint create``: a **Dedicated endpoint**, where
  Modal picks the recipe, GPU, engine and flags. No code of ours runs.
* ``sglang`` / ``vllm`` — ``modal deploy deploy/modal_*_embedding.py``: our
  fallback scripts, for what a managed recipe cannot express (#142).

``SERVING=<path>`` walks that ladder for ONE command without editing the YAML
the ``ModalEmbeddingModel`` client reads.

The optional ``HF_TOKEN`` (private or gated Hugging Face repos, ADR-009 §9)
joins the argv HERE and nowhere else, and every argv this script logs passes
through ``redact_argv`` first. ``subprocess.run`` is always called with
``check=False``: its raising form embeds the full argv — the token — in the
``CalledProcessError`` message, so the first failed create would print it.

Usage:
    make memory-deploy-embedding-model MODEL=Qwen/Qwen3-Embedding-0.6B
    make memory-deploy-embedding-model MODEL=voyageai/voyage-4-nano SERVING=vllm
    make memory-deploy-embedding-model-test MODEL=Qwen/Qwen3-Embedding-0.6B
    make memory-deploy-embedding-model-stop MODEL=Qwen/Qwen3-Embedding-0.6B
"""

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Literal

import click

from tree.config.app_config import ModalEmbeddingModelConfig, ServingPath
from tree.config.settings import settings
from tree.logging import init_logger
from tree.models.exceptions import ModelError
from tree.models.modal_catalog import (
    HF_TOKEN_HINT,
    fallback_script,
    get_catalog_entry,
    hf_token_args,
    modal_cli_command,
    redact_argv,
    resolve_serving,
)
from tree.models.modal_server import smoke_test

init_logger()
logger = logging.getLogger(__name__)

_DEDICATED_ENDPOINT_NOTE = (
    "Dedicated endpoint: Modal picks the GPU, engine and flags — this entry's "
    "gpu/cpu/memory_mb/max_model_len/extra_server_args are used only by "
    "SERVING=sglang|vllm."
)

model_option = click.option(
    "--model",
    required=True,
    help="Embedding catalog repo_id, e.g. Qwen/Qwen3-Embedding-0.6B.",
)
serving_option = click.option(
    "--serving",
    default="",
    help="Override the entry's Serving path for THIS command: endpoint|sglang|vllm.",
)


def _resolve(model: str, serving: str) -> tuple[ModalEmbeddingModelConfig, ServingPath]:
    """The catalog entry and the path this command runs on.

    A mistyped ``MODEL`` or ``SERVING`` exits 2 BEFORE anything is spent: the
    catalog's own message already lists every id / path it could have meant.
    """

    try:
        entry = get_catalog_entry(model)
        path = resolve_serving(entry, serving)
    except ModelError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

    if serving and path != entry.serving:
        logger.info(
            "SERVING=%s overrides the catalog's serving: %s for THIS command "
            "only — the ModalEmbeddingModel client keeps reading "
            "configs/default.yaml.",
            path,
            entry.serving,
        )
    return entry, path


def _run_modal(action: Literal["deploy", "stop"], model: str, serving: str) -> None:
    """Build the argv for ``action`` and run it, logging only a redacted copy."""

    entry, path = _resolve(model, serving)
    argv = modal_cli_command(action, entry, path)

    script = fallback_script(path) if action == "deploy" else None
    if script is not None and not Path(script).exists():
        logger.error("Serving path '%s' needs %s, which does not exist.", path, script)
        raise SystemExit(2)

    token = settings.hf_token.get_secret_value()
    run_argv = argv
    if action == "deploy":
        run_argv = argv + hf_token_args(entry, path, token)
        if path == "endpoint":
            logger.info(_DEDICATED_ENDPOINT_NOTE)

    # A plain join, not `shlex.join`: every element is catalog-derived (a repo
    # id, a git sha, a derived name, the region, a script path) and carries no
    # whitespace, while `shlex.join` would quote the redaction into `'***'` —
    # hiding the very marker an operator (and the leak tests) look for.
    redacted = " ".join(redact_argv(run_argv))
    logger.info("Running: %s", redacted)

    result = subprocess.run(  # noqa: S603 — argv is built from the catalog, never a shell
        run_argv,
        check=False,
        env={**os.environ, "EMBEDDING_MODEL": entry.repo_id},
    )
    if result.returncode != 0:
        logger.error("modal command failed (exit %d): %s", result.returncode, redacted)
        _hint_if_no_token(entry.repo_id, token)
        raise SystemExit(result.returncode)


def _hint_if_no_token(repo_id: str, token: str) -> None:
    """The ONE gated-repo hint (ADR-009 §9).

    A gated download fails inside the engine at container start, which
    surfaces either as a failed create or as a smoke test that never gets
    ``health 200`` — so the hint is logged at both, and only when no token is
    set. There is no pre-flight Hub call.
    """

    if not token:
        logger.warning(HF_TOKEN_HINT.format(repo_id=repo_id))


@click.group()
def main() -> None:
    """Serve one Embedding catalog model on Modal."""


@main.command()
@model_option
@serving_option
def deploy(model: str, serving: str) -> None:
    """Serve MODEL on Modal through its Serving path."""

    _run_modal("deploy", model, serving)


@main.command()
@model_option
@serving_option
def stop(model: str, serving: str) -> None:
    """Stop MODEL on Modal (pass the same SERVING you deployed with)."""

    _run_modal("stop", model, serving)


@main.command("test")
@model_option
def test_command(model: str) -> None:
    """Smoke-test the served MODEL through proxy-token auth.

    No ``--serving``: under H1 the lookup is identical on every path.
    """

    try:
        asyncio.run(smoke_test(model))
    except ModelError as exc:  # ExtractionError is a ModelError
        logger.error("%s", exc)
        _hint_if_no_token(model, settings.hf_token.get_secret_value())
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
