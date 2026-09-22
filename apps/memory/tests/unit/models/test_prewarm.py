"""The **Pre-warm** helpers of :mod:`tree.models.get_model`:
:func:`prewarm_models` and :func:`modal_backed_models`, its caller-side gate.

Every fake here is a duck: the helper only ever touches ``ensure_warm`` and
``warm_key``, so no Modal client, no HTTP and no ``modal`` import is involved.
Nothing sleeps for real — the one ``asyncio.sleep(600)`` is the sibling that
gets cancelled, which is exactly what these tests prove.

``modal_backed_models`` is tested with the factories as TRIPWIRES: the point of
the helper is what it does NOT call, and "no factory call" is the only
assertion that catches a ``sentence-transformers`` torch load nobody asked for.
"""

import asyncio
import contextlib
import logging
import subprocess
import sys
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from tree.config.app_config import EmbeddingConfig
from tree.models.exceptions import ModelError
from tree.models.fake_model import MockEmbeddingModel
from tree.models.get_model import (
    _build_embedding_model,
    get_llm,
    modal_backed_models,
    prewarm_models,
)

_LOGGER_NAME = "tree.models.get_model"

# Real app names from the **Modal catalog**, so the log assertions read like a
# real run's line.
_EMBEDDING_APP = "ep-tree-voyage-4-nano"
_LLM_APP = "ep-tree-lfm2-5-350m"


def _running_tasks() -> set[asyncio.Task[Any]]:
    """Every task alive on the loop right now — the input of the leak check.

    The whole suite shares ONE session-scoped event loop, so ``all_tasks()``
    also answers tasks no test here started (a Mongo keep-alive from the
    session fixture, say). Comparing the WHOLE set before and after with ``==``
    therefore failed ~1 run in 6 on a foreign task that merely RETIRED during
    the call — it left the "after" set and the equality broke in the direction
    that means nothing. What these tests mean is ``after - before == set()``:
    ``prewarm_models`` left no NEW running task behind. The ``not done`` filter
    is belt-and-braces (``all_tasks`` already drops finished tasks); the
    SUBTRACTION is what makes the assertion say what the tests mean.
    """

    return {task for task in asyncio.all_tasks() if not task.done()}


class _WarmingFake:
    """A Modal-shaped model: it counts its warms and can block on an event."""

    def __init__(self, warm_key: str, *, release: asyncio.Event | None = None) -> None:
        self.warm_key = warm_key
        self.calls = 0
        self.entered = asyncio.Event()
        self._release = release

    async def ensure_warm(self) -> None:
        self.calls += 1
        self.entered.set()
        if self._release is not None:
            await self._release.wait()


class _SleepingFake:
    """Warms by waiting out the full 600 s deadline; records its cancellation."""

    def __init__(self, warm_key: str) -> None:
        self.warm_key = warm_key
        self.entered = asyncio.Event()
        self.cancelled = False

    async def ensure_warm(self) -> None:
        self.entered.set()
        try:
            await asyncio.sleep(600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _SyncWarmFake:
    """A duck that LOOKS warmable but whose ``ensure_warm`` is a plain ``def``.

    What a hand-written test double looks like when the next engineer forgets
    the ``async``: ``warm()`` returns ``None``, so ``create_task`` raises
    ``TypeError`` instead of scheduling anything (#149 QA, finding 1).
    """

    def __init__(self, warm_key: str) -> None:
        self.warm_key = warm_key

    def ensure_warm(self) -> None:
        return None


class _FailingFake:
    """A dead server: fails fast with the gate's ``ModelError``.

    ``after`` is the handshake that makes the cancellation test deterministic —
    the failure lands once the sibling is already inside its poll, which is the
    only situation where cancelling it matters.
    """

    def __init__(self, warm_key: str, *, after: asyncio.Event | None = None) -> None:
        self.warm_key = warm_key
        self._after = after

    async def ensure_warm(self) -> None:
        if self._after is not None:
            await self._after.wait()
        raise ModelError(
            f"Health poll of https://{self.warm_key}.modal.run/health returned "
            "HTTP 403 — not a cold start, giving up after 1 attempt."
        )


async def test_noop_without_ensure_warm(caplog) -> None:
    """Providers without a gate (Voyage, Gemini, mock) cost nothing at all."""

    before = _running_tasks()

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await prewarm_models(object(), MockEmbeddingModel(dimensions=8))

    assert _running_tasks() - before == set()
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []


async def test_warms_concurrently() -> None:
    """Two distinct servers boot at the same time, not one after the other."""

    release = asyncio.Event()
    llm = _WarmingFake(_LLM_APP, release=release)
    embedding = _WarmingFake(_EMBEDDING_APP, release=release)

    prewarm = asyncio.create_task(prewarm_models(llm, embedding))
    await asyncio.wait_for(
        asyncio.gather(llm.entered.wait(), embedding.entered.wait()), timeout=1
    )
    release.set()
    await asyncio.wait_for(prewarm, timeout=1)

    assert (llm.calls, embedding.calls) == (1, 1)


async def test_dedupes_on_warm_key(caplog) -> None:
    """The resolution and the search embedding on ONE app share ONE boot."""

    resolution = _WarmingFake(_EMBEDDING_APP)
    search = _WarmingFake(_EMBEDDING_APP)

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await prewarm_models(resolution, search)

    assert (resolution.calls, search.calls) == (1, 0)
    assert f"Pre-warming 1 Modal server(s): {_EMBEDDING_APP}" in caplog.text


async def test_logs_every_distinct_server_it_warms(caplog) -> None:
    """One INFO line names the apps, in the order the caller passed them."""

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await prewarm_models(_WarmingFake(_LLM_APP), _WarmingFake(_EMBEDDING_APP))

    assert f"Pre-warming 2 Modal server(s): {_LLM_APP}, {_EMBEDDING_APP}" in caplog.text


async def test_first_failure_cancels_the_sibling() -> None:
    """A 403 on one server must not wait out the other's 600 s deadline."""

    sleeping = _SleepingFake(_EMBEDDING_APP)
    dead = _FailingFake(_LLM_APP, after=sleeping.entered)
    before = _running_tasks()

    started = time.monotonic()
    with pytest.raises(ModelError, match="HTTP 403"):
        await prewarm_models(dead, sleeping)
    elapsed = time.monotonic() - started

    assert elapsed < 1
    assert sleeping.cancelled is True
    # The `finally` reaped the cancelled poll INSIDE the call: nothing is left
    # knocking on the loop the pipeline is about to fan out on.
    assert _running_tasks() - before == set()


async def test_a_sync_ensure_warm_does_not_leak_the_sibling() -> None:
    """A ``TypeError`` at scheduling time still reaps what was already scheduled.

    #149 QA, finding 1: the task list used to be built OUTSIDE the ``try``, so a
    LATER duck whose ``ensure_warm`` is a plain ``def`` raised before the
    ``finally`` existed — and the FIRST model's real 600 s poll stayed alive on
    the loop the run was about to fan out on, against this function's own
    "fail-fast with an explicit cancel" guarantee.
    """

    sleeping = _SleepingFake(_EMBEDDING_APP)
    sync_duck = _SyncWarmFake(_LLM_APP)
    before = _running_tasks()

    with pytest.raises(TypeError, match="a coroutine was expected"):
        await prewarm_models(sleeping, sync_duck)

    # Nothing is left knocking on the loop: the poll was cancelled and reaped
    # inside the call. It may never have started (no await separates the two
    # ``create_task`` calls) — but if it did, it took the cancellation.
    assert _running_tasks() - before == set()
    assert not sleeping.entered.is_set() or sleeping.cancelled is True


async def test_a_foreign_task_that_retires_mid_call_is_not_a_leak() -> None:
    """The flake itself (PR #44, Nit 1): the old ``==`` broke on a task
    LEAVING.

    The suite shares one session-scoped loop, so a task another module started
    (a Mongo keep-alive) can finish while this call awaits. The whole-set
    equality then failed because ``before`` held a task ``after`` no longer
    did — a direction that says nothing about ``prewarm_models``. The
    subtraction only ever looks at what was ADDED.
    """

    foreign = asyncio.create_task(asyncio.sleep(0))
    before = _running_tasks()

    # A gated model, so the call really suspends: that is the window in which
    # the foreign task retires.
    await prewarm_models(_WarmingFake(_EMBEDDING_APP))

    assert foreign.done()
    assert _running_tasks() - before == set()


async def test_a_leaked_foreign_task_that_never_finishes_is_not_a_leak() -> None:
    """The other half: a foreign task still RUNNING across the call.

    What a fixture elsewhere in the suite leaves alive on the shared loop —
    the situation CI actually failed on. It is in both sets, so it cancels out
    of the subtraction; only a task ``prewarm_models`` itself left could fail
    the check. Cancelled at the end so this test leaks nothing in its turn.
    """

    leaked = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)  # let it reach its await, so it is really running
    before = _running_tasks()

    await prewarm_models(MockEmbeddingModel(dimensions=8))

    assert leaked in _running_tasks()
    assert _running_tasks() - before == set()

    leaked.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await leaked


async def test_is_idempotent() -> None:
    """A second call re-asks each gate; the GATE, not this helper, makes it cheap."""

    embedding = _WarmingFake(_EMBEDDING_APP)

    await prewarm_models(embedding)
    await prewarm_models(embedding)

    assert embedding.calls == 2


async def test_default_providers_have_no_gate_to_warm(mocker, caplog) -> None:
    """The shipped defaults (Voyage + Gemini) make the pre-warm a no-op.

    Built through the real factories so the assertion tracks what the pipeline
    actually constructs, not a hand-written stand-in.
    """

    settings = MagicMock()
    settings.google_api_key.get_secret_value.return_value = "fake-google-key"
    settings.voyage_api_key.get_secret_value.return_value = "fake-voyage-key"
    mocker.patch("tree.models.get_model.settings", settings)
    llm = get_llm("gemini")
    embedding = _build_embedding_model(
        EmbeddingConfig(provider="voyage", model="voyage-4", dimensions=1024)
    )
    before = _running_tasks()

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await prewarm_models(llm, embedding)

    assert not hasattr(llm, "ensure_warm")
    assert not hasattr(embedding, "ensure_warm")
    assert _running_tasks() - before == set()
    assert "Pre-warming" not in caplog.text


class TestModalBackedModels:
    """The gate in front of the pre-warm: read the provider, THEN build.

    The providers and the two factory tripwires come from the ``modal_seam``
    fixture (``tests/unit/conftest.py``), shared with the pipeline seam's tests.
    """

    @pytest.mark.parametrize(
        "provider", ["sentence-transformers", "voyage", "gemini", "mock"]
    )
    def test_a_non_modal_provider_never_calls_its_factory(
        self, modal_seam, provider: str
    ) -> None:
        """#149 QA, finding 2: a fully cached ``sentence-transformers`` run used
        to pay a torch weight load for a model with no ``ensure_warm`` at all."""

        modal_seam.providers(llm="gemini", resolution=provider, search=provider)

        models = modal_backed_models("llm", "resolution_embedding", "search_embedding")

        assert models == []
        modal_seam.llm.assert_not_called()
        modal_seam.embedding.assert_not_called()

    def test_a_modal_block_is_built_once(self, modal_seam) -> None:
        """Only the ``modal`` block reaches its factory — the others cost nothing."""

        modal_seam.providers(llm="gemini", resolution="voyage", search="modal")

        models = modal_backed_models("llm", "resolution_embedding", "search_embedding")

        assert models == ["SEARCH-EMBEDDING"]
        assert modal_seam.embedding.call_count == 1
        modal_seam.llm.assert_not_called()

    def test_every_modal_block_is_built_in_the_order_asked(self, modal_seam) -> None:
        """All three on Modal: three instances, in the caller's order (the
        pre-warm logs the app names in exactly that order)."""

        modal_seam.providers(llm="modal", resolution="modal", search="modal")

        models = modal_backed_models("llm", "resolution_embedding", "search_embedding")

        assert models == ["LLM", "RESOLUTION-EMBEDDING", "SEARCH-EMBEDDING"]
        assert (modal_seam.llm.call_count, modal_seam.embedding.call_count) == (1, 2)

    def test_reads_the_provider_at_call_time(self, modal_seam) -> None:
        """No import-time freeze: Prefect re-imports this module inside flow-run
        subprocesses, and a ``TREE_MODELS__LLM__PROVIDER=modal`` override must
        move the gate with it."""

        modal_seam.providers(llm="gemini")

        before = modal_backed_models("llm")
        modal_seam.providers(llm="modal")
        after = modal_backed_models("llm")

        assert (before, after) == ([], ["LLM"])
        assert modal_seam.llm.call_count == 1


def test_prewarm_helper_does_not_import_modal() -> None:
    """The factory module — pre-warm included — keeps ``modal`` off the boot path.

    Runs in a fresh interpreter: this suite imports ``ModalEmbeddingModel`` at
    module level elsewhere, so a same-process assertion would pass on a
    regression. The MCP server imports this module at boot and must bind its
    port inside Horizon's 60 s readiness window.
    """

    probe = (
        "import sys, tree.models.get_model; "
        "assert 'modal' not in sys.modules, 'modal'; "
        "assert 'tree.models.modal_embedding' not in sys.modules, 'modal_embedding'; "
        "assert 'tree.models.modal_llm' not in sys.modules, 'modal_llm'"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )

    assert result.returncode == 0, (
        f"importing get_model pulled the Modal SDK: {result.stderr}"
    )
