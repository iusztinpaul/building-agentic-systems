"""Regression test for the macOS ``sun_path`` overflow that crashes torch's
``torch_shm_manager`` when ``TMPDIR`` is too long.

Background (see ``tracker/done/035-pin-torch-version-py314-arm64.md`` for the
full diagnostic log): on macOS, ``sockaddr_un.sun_path`` is **104 bytes**
(vs. 108 on Linux). torch's ``torch_shm_manager`` constructs a Unix-domain
socket path of the form ``<TMPDIR>/torch_<pid>_<rand>/manager.sock`` and
stuffs it into an on-stack ``sun_path`` buffer. If the inherited ``TMPDIR``
is long enough (e.g. the
``/var/folders/.../com.apple.shortcuts.mac-helper/`` path some agent shells
inherit, ~81 chars), the full path overflows the buffer,
``__stack_chk_fail`` traps, the helper aborts with SIGABRT, and the parent
raises ``RuntimeError: no response from torch_shm_manager``.

The ``apps/memory/Makefile`` ships a ``TMPDIR`` shim
(``export TMPDIR := $(shell getconf DARWIN_USER_TEMP_DIR)`` on Darwin) so
every ``make memory-*`` target inherits a short tmpdir. This test is the
regression sentinel for that shim — it asserts two things:

1.  The synthesized torch socket path (using realistic length surrogates
    for ``<pid>_<rand>``) fits in 104 bytes. This is the static check that
    catches a regression even on Linux (where the check trivially passes).
2.  ``torch.tensor(0).share_memory_()`` runs without raising. This is the
    runtime check that catches a regression on macOS where (1) might be
    true on paper but the platform tmpdir is something else entirely.

Marked ``@pytest.mark.slow`` because importing torch is ~1-2s.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


# Worst-case length of the torch_shm_manager-generated suffix:
# ``torch_<pid>_<rand>/manager.sock``. ``<pid>`` is up to 7 digits on macOS
# (PID_MAX = 99999), ``<rand>`` is a 10-char alphanumeric token in current
# libshm. The "/" between the dir and ``manager.sock`` plus the trailing
# NUL byte in sun_path are included. Concretely:
#   "torch_" (6) + pid (<=7) + "_" (1) + rand (10) + "/" (1)
#   + "manager.sock" (12) + NUL (1) = 38 bytes
# We round up to 40 for safety margin against future libshm changes.
_TORCH_SHM_SUFFIX_BYTES = 40

# macOS sockaddr_un.sun_path is 104 bytes (sys/un.h). Linux is 108.
# We use the smaller value as the binding constraint regardless of platform
# so the test is meaningful even when run on Linux (it just trivially passes
# there because /tmp is 5 chars).
_MACOS_SUN_PATH_MAX = 104


@pytest.mark.slow
def test_torch_shm_socket_path_fits_in_sockaddr_un() -> None:
    """Static check: the worst-case torch_shm_manager socket path under the
    current ``TMPDIR`` must fit in macOS's 104-byte ``sun_path``.

    Catches regressions in the ``apps/memory/Makefile`` TMPDIR shim (e.g.
    someone removes the ``export TMPDIR := ...`` block or breaks the
    Darwin detection) before they manifest as a flaky SIGABRT in a torch
    code path that's hard to bisect.
    """
    # Arrange
    #
    # On macOS, ``TMPDIR`` MUST be set — the Makefile shim
    # (``export TMPDIR := $(shell getconf DARWIN_USER_TEMP_DIR)``) is the
    # whole point of this regression test. A missing ``TMPDIR`` on macOS
    # means the shim regressed, which is exactly the failure mode this
    # test is the sentinel for. On Linux (including GitHub Actions
    # runners, which call ``uv run pytest`` directly without going
    # through the Makefile and where the shell step does not always
    # export ``TMPDIR``), fall back to ``tempfile.gettempdir()`` — that
    # mirrors what Python / glibc / torch's libshm actually do at runtime
    # when ``$TMPDIR`` is unset, so the synthesized-socket-path check
    # below remains meaningful. The 104-byte ``sun_path`` constraint
    # itself is macOS-specific; on Linux the check trivially passes
    # because ``/tmp`` is 4 chars, but keeping it exercised on Linux
    # catches a regression where the synthesis logic itself breaks
    # (e.g. someone bumps ``_TORCH_SHM_SUFFIX_BYTES`` past 100).
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir is None:
        assert sys.platform != "darwin", (
            "TMPDIR is not set on macOS; the Makefile shim "
            "(export TMPDIR := $(shell getconf DARWIN_USER_TEMP_DIR)) "
            "should have exported it. This is the regression this "
            "sentinel test exists to catch — investigate apps/memory/"
            "Makefile's Darwin TMPDIR block."
        )
        # Linux fallback: use the platform default that torch's libshm
        # would actually see when ``$TMPDIR`` is unset.
        tmpdir = tempfile.gettempdir()

    # Act: synthesize the longest socket path torch_shm_manager would
    # construct under this TMPDIR.
    synthesized_path = str(Path(tmpdir) / ("x" * _TORCH_SHM_SUFFIX_BYTES))
    synthesized_path_bytes = len(synthesized_path.encode("utf-8"))

    # Assert
    assert synthesized_path_bytes <= _MACOS_SUN_PATH_MAX, (
        f"TMPDIR={tmpdir!r} is too long: a torch_shm_manager socket path "
        f"under it would be {synthesized_path_bytes} bytes, exceeding "
        f"macOS's sockaddr_un.sun_path limit of {_MACOS_SUN_PATH_MAX}. "
        f"This will crash torch_shm_manager with SIGABRT and surface as "
        f"'RuntimeError: no response from torch_shm_manager'. See "
        f"apps/memory/Makefile's TMPDIR shim and "
        f"tracker/done/035-pin-torch-version-py314-arm64.md."
    )


@pytest.mark.slow
def test_torch_share_memory_runs_without_raising() -> None:
    """Runtime check: ``torch.tensor(0).share_memory_()`` must complete
    without raising.

    This is the end-to-end sentinel for the SIGABRT crash described in
    ``tracker/done/035-pin-torch-version-py314-arm64.md``. On macOS arm64
    + Python 3.14, if the inherited ``TMPDIR`` overflows
    ``sockaddr_un.sun_path``, this call surfaces as
    ``RuntimeError: no response from torch_shm_manager``. Under the
    Makefile TMPDIR shim it succeeds.
    """
    # Arrange
    import torch

    tensor = torch.tensor(0)

    # Act + Assert (no raise)
    tensor.share_memory_()
