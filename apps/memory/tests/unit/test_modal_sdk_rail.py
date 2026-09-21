"""The unit suite's SECOND safety rail: the Modal **SDK** door (#152 QA).

``tests/unit/conftest.py`` closes two doors to Modal, not one:

* the CLI door — ``_modal_dry_run`` sets ``TREE_MODAL_DRY_RUN``, so the deploy
  driver starts no ``modal`` process (#143's rail, after a live-CLI incident);
* the SDK door — ``_no_live_modal_sdk`` replaces ``modal.Server.from_name`` and
  its neighbours with a raiser, so a test that flips a provider to ``modal``
  without patching the factory fails LOUDLY instead of resolving a real server.

These tests prove the second rail five ways: it fires, a test's own patch still
wins over it, the documented opt-out really hands the SDK back, every door it
names is a real SDK attribute, and it neither imports the SDK at conftest
import time nor breaks a box that has no ``modal`` installed at all.

NOT red-first, deliberately: a genuine red here means a live Modal lookup from
a unit test, which is the thing being prevented (and forbidden while #141 is
open). Non-vacuity was proven by MUTATION instead — with the raiser replaced by
a no-op, the two rows below go red; see ``## Log``.
"""

import subprocess
import sys

import modal
import pytest

from tests.unit.conftest import (
    MODAL_SDK_RAIL_MARKER,
    MODAL_SDK_RAIL_MESSAGE,
    _install_modal_sdk_rail,
)
from tree.models.exceptions import ModelError
from tree.models.modal_catalog import get_catalog_entry
from tree.models.modal_server import resolve_server_url

_QWEN = "Qwen/Qwen3-Embedding-0.6B"
_URL = "https://acme--ep-tree-qwen3-embedding-0-6b-server.modal.run"

# Same shape as `_MODAL_SDK_DOORS`, spelled out here so the test names the
# entry points rather than importing the list it is checking — and so a door
# added there without a real SDK attribute behind it fails HERE
# (`_install_modal_sdk_rail` skips what the SDK does not have, silently).
_DOORS = [
    ("Server", "from_name"),
    ("Function", "from_name"),
    ("App", "lookup"),
    ("Cls", "from_name"),
]


def _door(class_name: str, attribute: str) -> object:
    return getattr(getattr(modal, class_name), attribute)


@pytest.mark.parametrize("class_name,attribute", _DOORS)
def test_every_sdk_door_carries_the_rail(class_name: str, attribute: str) -> None:
    """Asserted on the MARKER, never by calling: calling is the accident."""

    assert getattr(_door(class_name, attribute), MODAL_SDK_RAIL_MARKER, False)


def test_a_live_lookup_raises_the_rails_assertion() -> None:
    """The door the source actually uses (``modal_server.py``'s one lookup)."""

    with pytest.raises(AssertionError, match="live Modal SDK lookup"):
        modal.Server.from_name("ep-tree-qwen3-embedding-0-6b", "Server")


async def test_the_client_path_fails_on_the_rail_not_on_the_network() -> None:
    """``resolve_server_url`` un-patched — what a forgetful test really hits.

    It turns EVERY lookup failure into one ``ModelError`` ("is the model
    deployed?"), so the rail's ``AssertionError`` arrives as its ``__cause__``.
    That distinction is the whole assertion: a network error would have meant
    the lookup left the process.
    """

    with pytest.raises(ModelError) as excinfo:
        await resolve_server_url(get_catalog_entry(_QWEN))

    cause = excinfo.value.__cause__
    assert isinstance(cause, AssertionError)
    assert str(cause) == MODAL_SDK_RAIL_MESSAGE


async def test_a_test_level_patch_still_wins(mocker) -> None:
    """The rail must not break the tests that mock ``modal.Server`` themselves.

    Theirs is applied inside the test, over the rail, and is restored first —
    so the usual ``mocker.patch("tree.models.modal_server.modal.Server")``
    keeps working exactly as before.
    """

    server = mocker.MagicMock()
    server.get_url.aio = mocker.AsyncMock(return_value=_URL)
    server_cls = mocker.patch("tree.models.modal_server.modal.Server")
    server_cls.from_name.return_value = server

    assert await resolve_server_url(get_catalog_entry(_QWEN)) == _URL


def test_the_rail_is_a_no_op_without_the_sdk(mocker) -> None:
    """``modal`` is in the optional ``local-models`` extra, so it may be absent.

    ``None`` in ``sys.modules`` is what an uninstalled module looks like to an
    ``import`` statement (the in-repo idiom — see
    ``tests/unit/memory/graph/resolution/test_composite.py``). The installer
    must then close NOTHING and raise NOTHING: without the SDK no test can
    reach it, and a conftest that blew up would take the whole suite with it.
    """

    mocker.patch.dict(sys.modules, {"modal": None})
    patcher = mocker.MagicMock()

    assert _install_modal_sdk_rail(patcher) == []
    assert patcher.patch.object.call_count == 0


def test_the_conftest_imports_the_sdk_lazily() -> None:
    """The rail may not put ``modal`` on the import path of the suite itself.

    A fresh interpreter, because this very file imports ``modal`` at module
    level: a same-process assertion would pass on a regression. Importing the
    conftest must not import the SDK — that is what makes the fixture a no-op
    on a box without the extra, and it keeps the fresh-interpreter probes that
    assert ``'modal' not in sys.modules`` honest.
    """

    probe = "import sys, tests.unit.conftest; assert 'modal' not in sys.modules"

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )

    assert result.returncode == 0, (
        f"importing the unit conftest pulled the Modal SDK: {result.stderr}"
    )


@pytest.mark.parametrize("class_name,attribute", _DOORS)
def test_the_opt_out_hands_the_real_sdk_back(
    modal_sdk_allowed: None, class_name: str, attribute: str
) -> None:
    """Requesting ``modal_sdk_allowed`` lifts the rail for THAT test only.

    Nothing needs it today (the whole suite is green with the rail on); it
    exists so a test that legitimately needs the real SDK opts out explicitly
    instead of deleting the fixture for everyone. Proven on the marker, not by
    a live call — this test must reach Modal no more than any other.
    """

    assert not getattr(_door(class_name, attribute), MODAL_SDK_RAIL_MARKER, False)
