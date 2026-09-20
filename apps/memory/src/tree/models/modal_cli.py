"""The ONE place that runs the ``modal`` CLI — and the rails around it.

Everything here exists because of one incident (2026-09-20, ADR-009 §3 and its
Consequences): the catalog derived the SAME Modal app name a hand-made
**Dedicated endpoint** of the model has, and the driver deployed without
looking — silently overwriting an endpoint the operator had built in the
dashboard, on a plan with no ``modal app rollback``.

Four rails, smallest first:

1. **The namespace** (``tree.config.app_config.MODAL_NAME_PREFIX``): every name
   we create is ``tree-<slug>`` / ``ep-tree-<slug>``, so the collision is gone
   by construction. Not enforced here — derived here-adjacent, in the catalog.
2. **The ownership check** (:func:`assert_owned_name`): a ``deploy`` or a
   ``stop`` of a name WITHOUT the prefix is refused, ``--force`` or not. The
   prefix IS the ownership mark: no tags, no registry, checkable offline.
3. **The existence guard** (:func:`guard_deploy` over :func:`existing_kind`):
   two read-only ``list --json`` calls before a deploy, refusing when the name
   is live as something this command would not merely update. It fails CLOSED —
   a list we cannot read is not evidence that nothing is there.
4. **The dry run** (:func:`is_dry_run`, honoured inside :func:`run_modal`): the
   only way to exercise the driver without deploying. A fake ``modal`` on
   ``PATH`` is NOT such a way — ``make`` runs ``uv run``, which puts
   ``.venv/bin`` (the real, authenticated CLI) first. That is how the two
   accidental deploys happened.

``tree.models.modal_catalog`` stays pure and subprocess-free, and
``tree.models.modal_router`` imports its process door from here rather than
``subprocess``; the entry-point script stays glue.
"""

import json
import logging
import os
import subprocess
from typing import Literal

from tree.config.app_config import MODAL_NAME_PREFIX, ModalModelConfig
from tree.models.exceptions import ModelError
from tree.models.modal_catalog import redact_argv

logger = logging.getLogger(__name__)

# What `existing_kind` found live under the entry's names.
ExistingKind = Literal["none", "endpoint", "app"]

# What a `deploy` is about to CREATE. Keyed on the OUTCOME rather than on a
# path name, which is why the guard survived the auto-router: the router picks
# between exactly these two (`tree.models.modal_router`).
DeployTarget = Literal["endpoint", "app"]

ModalAction = Literal["deploy", "stop"]

# What `run_modal` answers when it actually ran something. Exported so a caller
# can annotate a captured result WITHOUT importing `subprocess` itself: the
# process boundary — types included — stays in this module (the router's
# `test_router_does_not_import_modal_or_subprocess` rests on that).
ModalResult = subprocess.CompletedProcess[str]

# Set to 1 by `make ... DRY_RUN=yes`, by `--dry-run`, and by the unit suite for
# EVERY test (tests/unit/conftest.py) — so a driver test that forgets to mock
# `subprocess.run` dry-runs instead of reaching Modal.
DRY_RUN_ENV = "TREE_MODAL_DRY_RUN"

# The two name shapes this project creates (`tree-voyage-4-nano` and
# `ep-tree-voyage-4-nano`), derived from the ONE prefix constant so a rename
# cannot leave the guard checking the old namespace.
_ENDPOINT_NAME_PREFIX = f"{MODAL_NAME_PREFIX}-"
_APP_NAME_PREFIX = f"ep-{MODAL_NAME_PREFIX}-"

# `modal app list --json` states that mean "not live" (modal 1.5.5,
# `modal/cli/app.py:41-50`). Everything else — deployed, ephemeral,
# initializing..., disabled — is something a deploy would write over.
_DEAD_APP_STATES = frozenset({"stopped", "stopping..."})

_KIND_LABELS: dict[ExistingKind, str] = {
    "endpoint": "Dedicated endpoint",
    "app": "app",
}
_KIND_ARTICLES: dict[ExistingKind, str] = {"endpoint": "a", "app": "an"}

_MODAL_NOT_INSTALLED = (
    "The `modal` CLI is not installed. Install the local-models extra: "
    "uv --directory apps/memory sync --extra local-models"
)


class ModalGuardError(ModelError):
    """A deploy the existence guard refused — the driver's exit code 3.

    Its own type so the driver can tell a guard refusal (exit 3, recoverable
    with ``FORCE=yes``) from a configuration error (exit 2, which ``FORCE``
    never overrides) without parsing messages.

    ``reason`` is the short cause ("could not list Modal apps (exit 1)"), kept
    separately so ``FORCE=yes`` can log it as a warning without echoing a
    refusal that did not happen.
    """

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


def is_dry_run(flag: bool = False) -> bool:
    """Is this command a dry run — the ``--dry-run`` flag OR the env var?

    Read at CALL time, never cached at import: ``make`` exports the operator's
    environment into the process, and the unit suite sets the variable per
    test.
    """

    return flag or os.environ.get(DRY_RUN_ENV, "") not in ("", "0")


def run_modal(
    argv: list[str],
    *,
    dry_run: bool = False,
    capture_output: bool = False,
    env: dict[str, str] | None = None,
) -> ModalResult | None:
    """Run ONE ``modal`` command, or describe it and start nothing.

    The single door: every ``modal`` process this project starts goes through
    here, so the dry-run rail and the redaction rail each have exactly one
    implementation.

    Returns ``None`` when the command was NOT run (dry run) — the caller's
    signal to report and stop.

    ``check=False`` always: the raising form embeds the full argv — which on a
    custom-weights create is the Hugging Face token — in the
    ``CalledProcessError`` message (ADR-009 §9).

    Raises:
        ModelError: the ``modal`` CLI is not installed. Deliberately WITHOUT
            the argv: the raw ``FileNotFoundError`` traceback printed it.
    """

    redacted = " ".join(redact_argv(argv))
    if is_dry_run(dry_run):
        logger.info("DRY RUN — would run: %s", redacted)
        return None

    logger.info("Running: %s", redacted)
    try:
        return subprocess.run(  # noqa: S603 — argv is built from the catalog, never a shell
            argv,
            check=False,
            capture_output=capture_output,
            text=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ModelError(_MODAL_NOT_INSTALLED) from exc


def assert_owned_name(name: str, action: ModalAction) -> None:
    """Refuse to touch a Modal name this project did not create.

    ``FORCE`` never overrides this: forcing is about deploying over OUR own
    name, never about reaching into someone else's. Today it can only fire if
    the name derivation regresses — which is precisely the regression that
    cost an operator their endpoint.

    Raises:
        ModelError: ``name`` carries neither ``tree-`` (an endpoint name) nor
            ``ep-tree-`` (an app name).
    """

    if name.startswith(_ENDPOINT_NAME_PREFIX) or name.startswith(_APP_NAME_PREFIX):
        return

    raise ModelError(
        f"Refusing to {action} {name!r}: it lacks the "
        f"{_ENDPOINT_NAME_PREFIX!r} prefix, so this project did not create it."
    )


def existing_kind(entry: ModalModelConfig) -> ExistingKind:
    """What is live on Modal under ``entry``'s names, read-only.

    Two ``list --json`` calls, because they answer different questions:
    ``modal endpoint list`` hides STOPPED endpoints (modal 1.5.5,
    ``modal/cli/endpoint.py:271-311``), so the app list is what catches
    everything else — including an app of ours from a previous deploy.

    Column names verified on the pinned client (modal 1.5.5, 2026-09-20):
    endpoints ``name, endpoint_id, status, created_at, created_by``
    (``cli/endpoint.py:296-311``), apps ``app_id, description, state, tasks,
    created_at, stopped_at`` (``cli/app.py:103-131``); the JSON keys are the
    snake_cased column titles (``cli/utils.py:132-160``).

    Raises:
        ModalGuardError: a list could not be read. Fails CLOSED — "I could not
            look" is not "nothing is there".
    """

    endpoints = _list_rows(entry, ["modal", "endpoint", "list", "--json"], "endpoints")
    if any(row.get("name") == entry.endpoint_name for row in endpoints):
        return "endpoint"

    apps = _list_rows(entry, ["modal", "app", "list", "--json"], "apps")
    for row in apps:
        if row.get("description") == entry.app_name:
            if row.get("state") not in _DEAD_APP_STATES:
                return "app"
    return "none"


def guard_deploy(
    entry: ModalModelConfig,
    target: DeployTarget,
    force: bool,
    *,
    path: str,
    kind: ExistingKind | None = None,
) -> None:
    """Look before the deploy writes (ADR-009 §3).

    The matrix, keyed on what the command would CREATE:

    ============ ============== ================== ==========================
    ``target``   existing none   existing endpoint  existing app (live)
    ============ ============== ================== ==========================
    ``endpoint`` run            REFUSE             REFUSE
    ``app``      run            REFUSE             run (a normal update)
    ============ ============== ================== ==========================

    ``path`` is only ever quoted back to the operator, so it is a plain
    ``str``: the router passes the route it is taking (``endpoint`` / ``app``).

    ``kind`` is the answer of :func:`read_existing_kind` when the caller
    already has it — the router reads the workspace ONCE, to route, and hands
    the same reading to the guard. ``None`` means "read it now".

    Raises:
        ModalGuardError: the deploy was refused, or a list could not be read.
            Both become exit code 3 and both are overridable with ``FORCE=yes``
            — a WARNING then replaces the refusal.
    """

    if kind is None:
        kind = read_existing_kind(entry, force)

    if kind == "none" or (target == "app" and kind == "app"):
        return

    name = entry.endpoint_name if kind == "endpoint" else entry.app_name
    if force:
        logger.warning(
            "FORCE=yes: deploying over the existing %s %r.", _KIND_LABELS[kind], name
        )
        return

    raise ModalGuardError(
        f"Refusing to deploy {entry.repo_id} via {path}: {name!r} already "
        f"exists on Modal as {_KIND_ARTICLES[kind]} {_KIND_LABELS[kind]}. Stop "
        f"it first (make memory-deploy-model-stop MODEL={entry.repo_id}) or "
        "pass FORCE=yes to deploy over it."
    )


def read_existing_kind(entry: ModalModelConfig, force: bool) -> ExistingKind:
    """:func:`existing_kind`, with ``FORCE=yes`` downgrading a closed door.

    The fail-closed rule says an unreadable list is not evidence that nothing
    is there — but ``FORCE=yes`` is exactly the operator saying "deploy
    anyway", so it becomes a WARNING and the caller proceeds as if the name
    were free.

    Raises:
        ModalGuardError: a list could not be read and ``force`` is false.
    """

    try:
        return existing_kind(entry)
    except ModalGuardError as exc:
        if not force:
            raise
        logger.warning("FORCE=yes: %s — deploying anyway.", exc.reason)
        return "none"


def _list_rows(entry: ModalModelConfig, argv: list[str], noun: str) -> list[dict]:
    """One ``modal ... list --json`` call, or a closed door.

    The human-readable table is NEVER parsed as a fallback: a guard that reads
    a format nobody promised is a guard that eventually reads it wrong.
    """

    result = run_modal(argv, capture_output=True)
    detail = ""
    rows: object = None

    if result is None:
        # Unreachable from the driver (a dry run skips the guard entirely), so
        # this is the door's own answer: a check we did not run is not a pass.
        detail = "dry run"
    elif result.returncode != 0:
        detail = f"exit {result.returncode}"
    else:
        try:
            rows = json.loads(result.stdout or "")
        except ValueError:
            detail = "invalid JSON"

    if not detail:
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
            return rows
        detail = "invalid JSON"

    reason = f"could not list Modal {noun} ({detail})"
    raise ModalGuardError(
        f"Refusing to deploy {entry.repo_id}: {reason}, so an overwrite cannot "
        "be ruled out. Pass FORCE=yes to skip the check.",
        reason=reason,
    )
