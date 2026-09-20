"""The **Pre-warm** helper: :func:`tree.models.get_model.prewarm_models`.

Every fake here is a duck: the helper only ever touches ``ensure_warm`` and
``warm_key``, so no Modal client, no HTTP and no ``modal`` import is involved.
Nothing sleeps for real — the one ``asyncio.sleep(600)`` is the sibling that
gets cancelled, which is exactly what these tests prove.
"""

import asyncio
import logging
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

from tree.config.app_config import EmbeddingConfig
from tree.models.exceptions import ModelError
from tree.models.fake_model import MockEmbeddingModel
from tree.models.get_model import _build_embedding_model, get_llm, prewarm_models

_LOGGER_NAME = "tree.models.get_model"

# Real app names from the **Modal catalog**, so the log assertions read like a
# real run's line.
_EMBEDDING_APP = "ep-tree-voyage-4-nano"
_LLM_APP = "ep-tree-lfm2-5-350m"


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

    before = asyncio.all_tasks()

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await prewarm_models(object(), MockEmbeddingModel(dimensions=8))

    assert asyncio.all_tasks() == before
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
    before = asyncio.all_tasks()

    started = time.monotonic()
    with pytest.raises(ModelError, match="HTTP 403"):
        await prewarm_models(dead, sleeping)
    elapsed = time.monotonic() - started

    assert elapsed < 1
    assert sleeping.cancelled is True
    # The `finally` reaped the cancelled poll INSIDE the call: nothing is left
    # knocking on the loop the pipeline is about to fan out on.
    assert asyncio.all_tasks() == before


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
    before = asyncio.all_tasks()

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await prewarm_models(llm, embedding)

    assert not hasattr(llm, "ensure_warm")
    assert not hasattr(embedding, "ensure_warm")
    assert asyncio.all_tasks() == before
    assert "Pre-warming" not in caplog.text


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
