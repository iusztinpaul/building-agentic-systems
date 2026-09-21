"""Deploy, smoke-test and stop ONE **Modal catalog** model — of either kind.

Make cannot read YAML, so this driver turns ``MODEL=<repo_id>`` into the
``modal`` commands the model needs. It never decides HOW the model is served:
the router asks Modal and logs ONE ``Routing …`` line (ADR-009 §2), and
``-stop`` tries both paths so an operator never has to remember the answer.

Glue only. The routing lives in ``tree.models.modal_router``, every rail in
``tree.models.modal_cli`` — the ``tree-`` ownership check, the existence guard
that looks before a deploy writes, and the dry run that starts no ``modal``
process at all. This file decides only which exit code each failure gets:

* ``0`` ok (or a dry run), ``1`` the smoke test failed, ``2`` usage or
  configuration (unknown ``MODEL``, bad ``SERVING``, a missing App script,
  ``modal`` not installed, a name without the ``tree-`` prefix), ``3`` a guard
  refusal, otherwise ``modal``'s own exit code.

The optional ``HF_TOKEN`` (private or gated Hugging Face repos, ADR-009 §9) is
read HERE and handed to the router, which appends it to a custom-weights
create and to nothing else; every argv that is logged passes through
``redact_argv`` first.

Usage:
    make memory-deploy-model MODEL=Qwen/Qwen3-Embedding-0.6B
    make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes
    make memory-deploy-model MODEL=Qwen/Qwen3-Embedding-0.6B SERVING=app
    make memory-deploy-model-test MODEL=Qwen/Qwen3-Embedding-0.6B
    make memory-deploy-model-stop MODEL=Qwen/Qwen3-Embedding-0.6B
"""

import asyncio
import logging
from collections.abc import Callable

import click

from tree.config.app_config import ModalModelConfig
from tree.config.settings import settings
from tree.logging import init_logger
from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_catalog import get_catalog_entry
from tree.models.modal_cli import ModalGuardError, wait_until_live
from tree.models.modal_router import hint_if_gated, run_deploy, run_stop
from tree.models.modal_server import chat_smoke_test, smoke_test

init_logger()
logger = logging.getLogger(__name__)

model_option = click.option(
    "--model",
    required=True,
    help="Modal catalog repo_id, e.g. Qwen/Qwen3-Embedding-0.6B.",
)
serving_option = click.option(
    "--serving",
    default="",
    help="Escape hatch: pin the Serving path for THIS command — endpoint|app. "
    "Unset (the default) lets the router ask Modal.",
)
dry_run_option = click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Log the modal command(s) and exit 0 — no modal process is started.",
)


@click.group()
def main() -> None:
    """Serve one Modal catalog model on Modal."""


@main.command()
@model_option
@serving_option
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Deploy even when the name is already live on Modal (the tree- "
    "ownership check is never overridden).",
)
@dry_run_option
def deploy(model: str, serving: str, force: bool, dry_run: bool) -> None:
    """Serve MODEL on Modal, letting the router pick the Serving path."""

    _exit_with(
        lambda: run_deploy(
            get_catalog_entry(model),
            serving=serving,
            force=force,
            dry_run=dry_run,
            token=settings.hf_token.get_secret_value(),
        )
    )


@main.command()
@model_option
@serving_option
@dry_run_option
def stop(model: str, serving: str, dry_run: bool) -> None:
    """Stop MODEL on Modal — the Dedicated endpoint if it is one, else the App.

    No ``--force``: a stop runs no existence check, and the ownership check it
    does run is not overridable.
    """

    _exit_with(
        lambda: run_stop(
            get_catalog_entry(model),
            serving=serving,
            dry_run=dry_run,
            token=settings.hf_token.get_secret_value(),
        )
    )


@main.command("test")
@model_option
def test_command(model: str) -> None:
    """Smoke-test the served MODEL through proxy-token auth.

    The KIND picks the test — vectors and a ranking for an embedding entry, one
    JSON-mode chat completion for an LLM — exactly as it picks the App script
    (ADR-009 §2). No ``--serving``: under H1 the lookup is identical on both
    paths. One read-only ``modal endpoint list --json`` first (skipped under
    ``DRY_RUN=yes``), to sit out a Dedicated endpoint still ``provisioning``.
    """

    entry = _entry_or_exit(model)
    test = smoke_test if entry.kind == "embedding" else chat_smoke_test

    try:
        # Before the event loop: the wait is synchronous, and a URL resolved
        # while the endpoint provisions has no server behind it.
        wait_until_live(entry)
        asyncio.run(test(model))
    except ExtractionError as exc:
        # Server-side: the model answered, or failed to. Only these can be a
        # gated download — a bare `ModelError` is OUR configuration (a wrong
        # Proxy token, whose message carries a 401 that would otherwise read
        # as gated).
        logger.error("%s", exc)
        hint_if_gated(model, settings.hf_token.get_secret_value(), str(exc))
        raise SystemExit(1) from exc
    except ModelError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc


def _exit_with(command: Callable[[], int]) -> None:
    """Run ONE router command, turning every rail's refusal into its code."""

    try:
        code = command()
    except ModalGuardError as exc:
        logger.error("%s", exc)
        raise SystemExit(3) from exc
    except ModelError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

    if code:
        raise SystemExit(code)


def _entry_or_exit(model: str) -> ModalModelConfig:
    """The catalog entry, or exit 2 listing the ids it could have meant."""

    try:
        return get_catalog_entry(model)
    except ModelError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
