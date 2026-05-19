# Force a short TMPDIR for the memory app so torch's shared-memory helper survives on macOS

Status: in-progress
Tags: `infra`, `make`, `macos`, `torch`, `tmpdir`, `huggingface`
Depends on: None
Blocks: #037

> **Note on the filename.** This file is still named `035-pin-torch-version-py314-arm64` for git/history continuity, but the **scope changed** after the original spec's root-cause hypothesis was disproved during investigation. The original framing (pin a working torch version) is preserved below under "Background investigation" so future readers can see why the spec pivoted; this header section reflects the **real, operator-approved scope** for #035. See the SWE investigation log entry (2026-05-19 17:10) for the full diagnostic.

## Scope

The originally reported failure — `RuntimeError: no response from torch_shm_manager` when any code path called `torch.tensor(...).share_memory_()` (notably HuggingFace's `arxiv-metadata-snapshot` ingestion in `tree.data.huggingface.arxiv_dataset_pipeline`) — is **not** a torch version regression. It is a `sockaddr_un.sun_path` length overflow on macOS:

- macOS's `sockaddr_un.sun_path` is **104 bytes** (vs. 108 on Linux).
- `torch_shm_manager` constructs a Unix-domain socket path of the form `<TMPDIR>/torch_<pid>_<rand>/manager.sock` and stuffs it into an on-stack `sun_path` buffer.
- When `TMPDIR` is long enough (the agent harness here inherits an ~81-char `/var/folders/.../com.apple.shortcuts.mac-helper/` path) the full socket path overflows the buffer, `__stack_chk_fail` traps, and the helper aborts with `SIGABRT`.
- Verified empirically across all five published `cp314-...macosx_*_arm64` torch wheels (2.9.0, 2.9.1, 2.10.0, 2.11.0, 2.12.0) — every one crashes under the long TMPDIR; every one succeeds under the standard 49-char `getconf DARWIN_USER_TEMP_DIR`. **No torch pin fixes this.**

Operator picked **Option B (force a short TMPDIR in the Makefile)** at escalation.

### What this task ships

1. **Makefile shim in `apps/memory/Makefile`.** On Darwin only, set
   ```make
   export TMPDIR := $(shell getconf DARWIN_USER_TEMP_DIR)
   ```
   so every `make memory-*` target — including `make memory-serve-workflows` and the Prefect-spawned flow subprocesses it manages — inherits a sub-104-byte TMPDIR. No-op on Linux (`sun_path` is 108 there and `/tmp` is 5 chars; the inherited TMPDIR is already short).
2. **Diagnostic make target `make memory-print-tmpdir`** that prints the effective `TMPDIR` and its byte length so future operators can verify the shim is active without reading the Makefile.
3. **Smoke target `make memory-smoke-torch-shared-memory`** that runs the minimal repro (`import torch; torch.tensor(0).share_memory_()`) end-to-end through `uv run` under the make-provided TMPDIR. Exits 0 from any shell, regardless of caller's inherited TMPDIR.
4. **Regression integration test** at `apps/memory/tests/integration/test_torch_shared_memory.py` with two `@pytest.mark.slow` tests:
   - `test_torch_shm_socket_path_fits_in_sockaddr_un` — static check: synthesizes the worst-case torch socket path under the current `TMPDIR` and asserts it fits in 104 bytes. Catches a regression in the Makefile shim (e.g. someone removes the `export TMPDIR := ...` block) before it manifests as a SIGABRT. Runs on every platform — trivially passes on Linux because `/tmp` is 5 chars.
   - `test_torch_share_memory_runs_without_raising` — runtime check: actually calls `torch.tensor(0).share_memory_()` and asserts no raise. End-to-end sentinel for the SIGABRT class of bug.
5. **CLAUDE.md note** under the new `## Configuration` section (added by #034). A short subsection that documents the macOS sockaddr / TMPDIR constraint, points at the Makefile shim, and gives the manual workaround (`TMPDIR=$(getconf DARWIN_USER_TEMP_DIR) uv run ...`) for operators who invoke scripts outside `make`.

### Explicitly NOT in scope

- **No `torch` pin in `apps/memory/pyproject.toml`.** Investigation confirmed every published `cp314-...macosx_*_arm64` torch wheel has the same SIGABRT under a long TMPDIR; pinning solves nothing and locks the dep down for cargo-cult reasons. Leave torch unpinned (currently resolves to 2.10.0 transitively via `sentence-transformers>=4.0`).
- **No `uv.lock` change.** No dependency edits → no lock change.
- **No code-level `os.environ["TMPDIR"] = ...` shim in `tree.*`.** The Makefile shim covers every entry point the operator and CI actually use; adding a Python-level shim creates an import-order constraint (must run before any torch import) that is easy to break silently. Out of scope unless option C is later picked.

## Acceptance Criteria

- [x] `apps/memory/Makefile` has a Darwin-only `export TMPDIR := $(shell getconf DARWIN_USER_TEMP_DIR)` shim with an inline comment naming the macOS sockaddr_un.sun_path overflow it dodges. Reference: this task and the SWE investigation log entry below.
- [x] `make memory-print-tmpdir` runs and prints the effective TMPDIR + byte length. Output captured below.
- [x] `make memory-smoke-torch-shared-memory` exits 0 from any shell (including a shell with `TMPDIR=/var/folders/.../com.apple.shortcuts.mac-helper/`). Output captured below.
- [x] `apps/memory/tests/integration/test_torch_shared_memory.py` exists with two `@pytest.mark.slow` tests (path-fits-in-sockaddr static check + runtime no-raise check). Naming follows the project convention (`test_*`, AAA, no manual setup/teardown). Tests pass through `make`.
- [x] `apps/memory/pyproject.toml` and `apps/memory/uv.lock` are unchanged (no torch pin).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit && make memory-unit-tests` clean.
- [x] CLAUDE.md gets a short subsection (4-6 lines) under `## Configuration` documenting the macOS TMPDIR constraint, the Makefile shim, and the manual workaround for non-`make` invocations.
- [ ] [HUMAN] Operator runs `make memory-smoke-torch-shared-memory` from their daily shell (no Claude Code wrapper) and confirms `OK` is printed; then runs `make memory-run-data-pipeline USER_ID=<oid>` and confirms the HuggingFace arxiv source ingests without the SIGABRT.

## User Stories

### Story: Operator runs the HF arxiv ingestion on macOS arm64 from any shell

1. Operator pulls the feature branch.
2. Operator runs `make memory-serve-workflows &` from any shell — including one whose `TMPDIR` is the long `com.apple.shortcuts.mac-helper` path that some agent harnesses inherit.
3. The serve-workflows process inherits the Makefile's short TMPDIR shim. Every Prefect-spawned flow subprocess inherits the same TMPDIR via the normal `subprocess.Popen` env-inheritance path.
4. Operator runs `make memory-run-data-pipeline USER_ID=<oid>`.
5. The HF arxiv source ingests at least one document (`max_samples: 10` in `apps/memory/configs/default.yaml`). No `no response from torch_shm_manager` traceback appears in the Prefect run logs.

### Story: CI catches a regression that re-introduces the overflow

1. A future PR removes or breaks the Makefile TMPDIR shim, or someone introduces a code path that re-builds the torch socket path using a different (long) directory.
2. `tests/integration/test_torch_shared_memory.py` runs as part of `make memory-integration-tests-all` (which CI uses).
3. On macOS the runtime check fails red and the static check fails red. On Linux both checks trivially pass — the test does not block Linux CI on a macOS-only regression. (CI's primary runner is `ubuntu-latest`, so this safety net is for the operator's local pre-push run, not for CI itself. That's fine: the operator already runs the full integration suite locally before any push per `CLAUDE.md`'s workflow.)

### Story: Operator runs a script outside `make`

1. Operator invokes a memory-app script directly via `uv --directory apps/memory run python ...` without going through `make`.
2. The Makefile shim does not apply.
3. CLAUDE.md's `## Configuration > macOS torch / TMPDIR shim` subsection tells the operator to set `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)` manually.

---

Blocked by: (none)

## Log

### [SWE] 2026-05-19 17:10 — Investigation + USER ACTION REQUIRED (preserved for history)

> This log entry is the **original investigation** that disproved the spec's root-cause hypothesis ("torch is broken on Py3.14 arm64; pin a working version") and surfaced the real cause (macOS `sockaddr_un.sun_path` overflow under a long inherited TMPDIR). It is preserved verbatim below so future readers can see why this task's scope and title diverged. The implementation log for the operator-approved fix is in the next entry (2026-05-19 18:30).

**TL;DR.** The spec's hypothesis ("torch 2.10.0 ships a broken `torch_shm_manager` binary on Py3.14 + macOS arm64; downgrade until the bug is gone") is incorrect. **Every published torch wheel for cp314 + macOS arm64 reproduces the SIGABRT in this environment, and none of them are actually broken** — they all crash for the same root cause, which is a `sun_path` length overflow in `libshm.dylib::ManagerServerSocket`, triggered by the agent shell's *long `TMPDIR`*, not by the torch binary itself. No torch pin will fix this. The right next move is operator-level: either accept that this is harmless outside the agent harness (operator's daily shell + Linux CI are unaffected) or take a workaround approach that the spec didn't anticipate. Escalating per the role's "undocumented architectural fork" rule.

**Versions tried (all installed, all share-memory repro'd):**

| torch | resolves with `sentence-transformers>=4.0`? | `share_memory_()` repro (this shell, `TMPDIR=/var/folders/77/qsf7lbgs6mv7q4tn90mqgpl00000gn/T/com.apple.shortcuts.mac-helper//`, 81 chars) |
|---|---|---|
| 2.9.0   | yes | **SIGABRT** in `torch_shm_manager` (`__stack_chk_fail`) |
| 2.9.1   | yes | **SIGABRT** in `torch_shm_manager` (`__stack_chk_fail`) |
| 2.10.0 (current, unpinned)  | yes | **SIGABRT** in `torch_shm_manager` (`__stack_chk_fail`) |
| 2.11.0  | yes | **SIGABRT** in `torch_shm_manager` (`__stack_chk_fail`) |
| 2.12.0  | yes | **SIGABRT** in `torch_shm_manager` (`__stack_chk_fail`) |

`sentence-transformers>=4.0` only requires `torch>=1.11.0`, so the transitive constraint is satisfied by all of the above; the lock-file regen was fine on every attempt.

The five rows above are the **complete set of published torch wheels for `cp314-...macosx_*_arm64`** on PyPI (verified via PyPI's JSON API). 2.10.0 is the version `requires-python>=3.14` currently resolves to without an explicit pin; 2.9.0 is the earliest Py3.14 arm64 wheel torch has ever shipped. There is nothing older to downgrade to.

**Root cause (from the macOS crash report `~/Library/Logs/DiagnosticReports/Retired/torch_shm_manager-2026-05-19-170153.ips`):**

```
"asi" : {"libsystem_c.dylib":["stack buffer overflow"]},
"exception" : {"type":"EXC_CRASH","signal":"SIGABRT"},
faulting frames:
  __stack_chk_fail
  ManagerServerSocket::ManagerServerSocket(std::string const&)
  main
```

`torch_shm_manager` constructs a Unix-domain socket path of the form `<TMPDIR>/torch_<pid>_<rand>/manager.sock` and stuffs it into a `sockaddr_un` whose `sun_path` is **104 bytes** on macOS (vs. 108 on Linux). When `TMPDIR` is long enough that the full socket path exceeds 104 bytes, `libshm` writes past the on-stack `sun_path` buffer, `__stack_chk_fail` traps, and the helper process aborts before answering the parent — which is exactly what the parent surfaces as `RuntimeError: no response from torch_shm_manager`.

This shell session inherited `TMPDIR=/var/folders/77/qsf7lbgs6mv7q4tn90mqgpl00000gn/T/com.apple.shortcuts.mac-helper//` (81 chars). With torch's `~20-char` `torch_<pid>_<rand>/manager.sock` suffix, the total path is ~101–105 bytes, right on the edge — and tips over the limit. Verified empirically by re-running the repro under a short `TMPDIR`:

```
$ cd apps/memory && TMPDIR=$(getconf DARWIN_USER_TEMP_DIR) uv run python -c "import torch; print(torch.__version__); torch.tensor(0).share_memory_(); print('OK')"
torch: 2.10.0
OK
```

So **the *currently-installed, unpinned* `torch==2.10.0` works fine on the operator's standard macOS per-user TMPDIR** (`getconf DARWIN_USER_TEMP_DIR` → `/var/folders/77/qsf7lbgs6mv7q4tn90mqgpl00000gn/T/`, 49 chars). It only fails inside this agent harness whose shell has the `com.apple.shortcuts.mac-helper/` suffix bolted on.

**What this means for the original failure mode the spec described (HF arxiv ingestion):**

- On the operator's normal shell on macOS arm64 (`TMPDIR` = standard 49-char per-user folder): `share_memory_()` works on **every** torch version 2.9.0–2.12.0, including the current 2.10.0. The HF arxiv ingestion will not see the SIGABRT.
- In CI on `ubuntu-latest`: `sun_path` is 108 bytes and `TMPDIR=/tmp` (5 chars), so the socket path comfortably fits. Every torch version is fine. The proposed integration test (`tests/integration/test_torch_shared_memory.py`) would pass on CI regardless of which torch is pinned — it cannot catch the bug because the bug is environment-specific, not code-specific.
- In this Claude Code agent harness (long inherited `TMPDIR`): every published torch crashes. No torch pin fixes it.

**Why I am stopping rather than picking one of the listed escalation options.** The four options in the groomed spec assume the bug is a torch version regression. None of them apply:

- "Pin Python to 3.13 (project-wide downgrade) and re-pin torch to a stable 2.x" — does not address `sun_path` overflow; Python version is irrelevant.
- "Pin sentence-transformers to a version that allows torch <X.Y" — `sentence-transformers>=4.0` already accepts `torch>=1.11.0`; no narrower constraint is being violated.
- "Ship without sentence-transformers in prod" — would not change torch's presence; `torch` is also a direct dep candidate for any future PyTorch use, and removing sentence-transformers would not remove the `share_memory_()` crash path if any other code path triggers it.
- "Wait for upstream torch to publish a working Py3.14 arm64 wheel" — every wheel already published works fine outside the long-TMPDIR environment. Upstream has no bug to fix here from their POV; the limit is a macOS kernel constant (`sizeof(sockaddr_un.sun_path) == 104`).

The actual decision space the operator now faces is different from what the spec laid out. **USER ACTION REQUIRED — please pick one of:**

  A. **Do nothing / close as `wontfix`.** Operator's daily shell + Linux CI are unaffected. The repro the operator captured "earlier in this session" was inside Claude Code's agent harness, which has the abnormal long `TMPDIR`. Verify in your own terminal: `echo $TMPDIR; TMPDIR=$(getconf DARWIN_USER_TEMP_DIR) uv run python -c "import torch; torch.tensor(0).share_memory_(); print('OK')"`. If it prints `OK`, the original premise of the task is moot for your workflow.
  B. **Workaround in the project's Makefile / entrypoints: force `TMPDIR=/tmp` (or any short path) on macOS** for the memory-app processes that touch torch. Cheapest fix; one-line `export TMPDIR=/tmp` at the top of `apps/memory/Makefile` (guarded by `uname -s` == `Darwin`). Tradeoff: changes process-wide tempfile semantics; other code paths that assume the per-user `TMPDIR` would now write to `/tmp`. Probably fine in practice, but it's a behavioral change that warrants an ADR.
  C. **Workaround in code: set `os.environ["TMPDIR"] = "/tmp"` at process start in `tree.logging` / `tree.config` init**, before any `torch` import. Same tradeoff as B but localized to the memory app's Python entry points. Still warrants an ADR ("we override TMPDIR on macOS to dodge a torch libshm `sun_path` overflow"). Would need to land in `scripts/serve_workflows.py`, `scripts/serve_mcp.py`, etc. Risk: misses any future entry point that forgets the import-order constraint.
  D. **Pin torch defensively anyway (e.g. `torch==2.9.0`)** as a precaution and rely on the operator's standard shell short TMPDIR to keep the project running. Pure security-blanket pin — doesn't fix the agent-harness scenario but locks the dep down. Would still want a new task to drop sentence-transformers from prod deps later (it's only a dev/local fallback now, per #034).
  E. **File an upstream torch bug** (path-length stack overflow in `libshm/ManagerServerSocket` on macOS) and let upstream fix it on their side. Multi-week / months at best; combine with one of A–D in the meantime.

My recommendation, with low confidence pending operator judgement: **B + E**. B is the smallest behavior change that makes the agent harness usable and keeps the operator's daily macOS workflow working unchanged; E gets the bug fixed properly long-term. But the choice between B / C / D depends on how much the operator wants to absorb a TMPDIR override into the project's invariants, which is a documented architectural decision (it touches every tempfile-using subsystem), so this is squarely PM/operator territory — I'm not picking silently.

**State left behind.** Reverted `apps/memory/pyproject.toml` and `apps/memory/uv.lock` back to the pre-investigation baseline (torch 2.10.0, no explicit pin). `git diff --stat apps/memory/pyproject.toml apps/memory/uv.lock` is empty. No regression test was added — the integration test the spec called for (`test_torch_shared_memory.py`) would pass in CI regardless of torch version (because CI's `sun_path`/`TMPDIR` are short), so it provides no signal for the real bug. Once the operator picks an option I can revisit and add the appropriate regression test for whichever path is chosen.

**Files touched during investigation, all reverted:** `apps/memory/pyproject.toml`, `apps/memory/uv.lock` (multiple iterations across 2.9.0, 2.9.1, 2.11.0, 2.12.0 → restored to 2.10.0 baseline via `git checkout`).

**Evidence of repro across versions** (each was `uv lock && uv sync && uv run python -c "import torch; torch.tensor(0).share_memory_()"`):

```
torch 2.10.0  → RuntimeError: no response from torch_shm_manager (frame: __stack_chk_fail at ManagerServerSocket)
torch 2.9.1   → RuntimeError: no response from torch_shm_manager
torch 2.9.0   → RuntimeError: no response from torch_shm_manager
torch 2.11.0  → RuntimeError: no response from torch_shm_manager
torch 2.12.0  → RuntimeError: no response from torch_shm_manager
```

**Evidence the bug is TMPDIR, not torch:**

```
$ cd /Users/pauliusztin/Documents/01-Projects/AI-Engineer-Handbook/building-agentic-systems-voyage-default-and-torch-pin/apps/memory \
  && TMPDIR=$(getconf DARWIN_USER_TEMP_DIR) uv run python -c "import torch; print('torch:', torch.__version__); torch.tensor(0).share_memory_(); print('OK')"
torch: 2.10.0
OK
```

```
$ /usr/bin/env -u TMPDIR /Users/pauliusztin/Documents/01-Projects/AI-Engineer-Handbook/building-agentic-systems-voyage-default-and-torch-pin/apps/memory/.venv/bin/python -c "import torch; torch.tensor(0).share_memory_(); print('OK')"
OK
```

**Crash report extract** (`~/Library/Logs/DiagnosticReports/Retired/torch_shm_manager-2026-05-19-170153.ips`):

```
"asi" : {"libsystem_c.dylib":["stack buffer overflow"]},
"exception" : {"type":"EXC_CRASH","signal":"SIGABRT"},
frames: __pthread_kill -> pthread_kill -> __abort -> __stack_chk_fail
        -> ManagerServerSocket::ManagerServerSocket(std::string const&)
        -> main
```

---

### [SWE] 2026-05-19 18:30 — Implementation (Option B: Makefile TMPDIR shim)

Operator picked **Option B** at escalation. Shipped the smallest behavior change that makes the agent-harness scenario work and leaves the operator's daily macOS workflow unchanged.

**Files modified**

- `apps/memory/Makefile` — added a Darwin-only `export TMPDIR := $(shell getconf DARWIN_USER_TEMP_DIR)` block (with inline comment explaining the macOS sockaddr_un.sun_path overflow it dodges). Added two diagnostic targets: `print-tmpdir` (prints the effective TMPDIR + byte length) and `smoke-torch-shared-memory` (runs the minimal `torch.tensor(0).share_memory_()` repro via `uv run`).
- `apps/memory/tests/integration/test_torch_shared_memory.py` (new) — two `@pytest.mark.slow` tests: `test_torch_shm_socket_path_fits_in_sockaddr_un` (static path-length check against the 104-byte macOS limit) and `test_torch_share_memory_runs_without_raising` (runtime no-raise check).
- `CLAUDE.md` — added a `### macOS torch / TMPDIR shim` subsection under `## Configuration` explaining the constraint, the Makefile shim, and the manual workaround for operators invoking scripts outside `make`.

**Files NOT modified** (per operator instruction)

- `apps/memory/pyproject.toml` — no torch pin. Investigation confirmed every published cp314-arm64 torch wheel has the same SIGABRT under a long TMPDIR; pinning solves nothing.
- `apps/memory/uv.lock` — no change for the same reason.

**Tests**

- Unit: 1197 passing, 0 failing — `make memory-unit-tests` clean.
- Integration (targeted, the two new tests): 2 passing — `make memory-format-fix && uv run pytest tests/integration/test_torch_shared_memory.py -v` from a make-equivalent env both pass.
- Format / lint / pre-commit: all clean.

**Acceptance criteria**

- [x] Makefile TMPDIR shim present with inline rationale.
- [x] `make memory-print-tmpdir` works.
- [x] `make memory-smoke-torch-shared-memory` exits 0 from any shell.
- [x] Regression test file exists, two tests, AAA pattern, no manual setup/teardown, `@pytest.mark.slow`.
- [x] `pyproject.toml` + `uv.lock` unchanged (no torch pin).
- [x] format-fix / lint-fix / format-check / lint-check / pre-commit / unit-tests clean.
- [x] CLAUDE.md updated with the short subsection.
- [ ] [HUMAN] Operator runs the repro from their daily shell and from `make memory-run-data-pipeline` — pending Tester / operator acceptance.

**Evidence**

Verify TMPDIR propagates through `make` even when the caller's shell has the long `com.apple.shortcuts.mac-helper` TMPDIR:

```
$ TMPDIR='/var/folders/77/qsf7lbgs6mv7q4tn90mqgpl00000gn/T/com.apple.shortcuts.mac-helper//' make memory-print-tmpdir
/Library/Developer/CommandLineTools/usr/bin/make -C apps/memory print-tmpdir
TMPDIR=/var/folders/77/qsf7lbgs6mv7q4tn90mqgpl00000gn/T/
TMPDIR length: 49 bytes
uname -s: Darwin
```

End-to-end repro now exits 0 through make, from a long-TMPDIR shell:

```
$ TMPDIR='/var/folders/77/qsf7lbgs6mv7q4tn90mqgpl00000gn/T/com.apple.shortcuts.mac-helper//' make memory-smoke-torch-shared-memory
/Library/Developer/CommandLineTools/usr/bin/make -C apps/memory smoke-torch-shared-memory
TMPDIR=/var/folders/77/qsf7lbgs6mv7q4tn90mqgpl00000gn/T/
OK
```

Regression tests pass through make-equivalent shell:

```
$ # From a make-driven shell that inherits the TMPDIR shim (verified via custom verify rule that includes Makefile)
$ uv run pytest tests/integration/test_torch_shared_memory.py -v
============================= test session starts ==============================
platform darwin -- Python 3.14.0, pytest-9.0.2, pluggy-1.6.0 ...
collected 2 items

tests/integration/test_torch_shared_memory.py::test_torch_shm_socket_path_fits_in_sockaddr_un PASSED [ 50%]
tests/integration/test_torch_shared_memory.py::test_torch_share_memory_runs_without_raising PASSED [100%]

============================== 2 passed in 1.08s ===============================
```

The same test correctly **fails red** when invoked directly (no make wrapper) under the long TMPDIR — confirming the regression sentinel works:

```
$ TMPDIR='/var/folders/77/qsf7lbgs6mv7q4tn90mqgpl00000gn/T/com.apple.shortcuts.mac-helper//' uv run pytest tests/integration/test_torch_shared_memory.py -v
FAILED tests/integration/test_torch_shared_memory.py::test_torch_shm_socket_path_fits_in_sockaddr_un
FAILED tests/integration/test_torch_shared_memory.py::test_torch_share_memory_runs_without_raising
   E       RuntimeError: no response from torch_shm_manager
```

The new test was also picked up by `make memory-integration-tests-slow` running the full slow suite — `tests/integration/test_torch_shared_memory.py ..` passed (both green) inside that broader run. Other failures observed in that broader run (`tests/integration/data/test_pipeline.py`, `tests/integration/mcp/test_ingest_tools.py`, `tests/integration/test_two_user_isolation.py`) are network/mongot-dependent and orthogonal to this change.

Format / lint / pre-commit / unit-tests:

```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit && make memory-unit-tests
... (full clean output) ...
1197 passed in 41.28s
```

**Notes / caveats for the Tester**

1. **Does the TMPDIR shim propagate into `make memory-serve-workflows` background processes?** Yes. `make memory-serve-workflows` invokes `uv run python -m tree.orchestrator`. Make exports `TMPDIR` into that subprocess's env (verified via `make print-tmpdir` and `make smoke-torch-shared-memory`, both of which invoke `uv run`-style subprocesses). The Tester should run `make memory-serve-workflows &` from a long-TMPDIR shell and confirm `echo $TMPDIR` *inside* the serve-workflows process is the short DARWIN_USER_TEMP_DIR (a one-line `print(os.environ["TMPDIR"])` patched into `tree.orchestrator.py`'s entry point would prove it, or grep the Prefect logs after a flow run).
2. **Does it propagate into Prefect-spawned subprocess flows?** Yes — by default `prefect.serve()` launches each flow run as a subprocess that inherits the parent process's environment (Python's `subprocess.Popen` default), so the shim's TMPDIR carries through. `grep -rn "subprocess\|Popen\|spawn\|multiprocessing" apps/memory/src/tree/` finds **no** tree-code subprocess spawn that would override env, so nothing in our code path is breaking the inheritance. Confirm via the Prefect-flow-side `echo $TMPDIR` if the Tester wants belt-and-braces evidence.
3. **The test marker is `@pytest.mark.slow`.** This means `make memory-integration-tests` (fast loop) **does NOT** run it. `make memory-integration-tests-all` and `make memory-integration-tests-slow` do. The CI mirror `make memory-integration-tests-ci` also does (it only excludes `requires_mongot`, not `slow`). Pick the right target for the acceptance gate.
4. **The regression test is meaningful on macOS only.** On Linux the static path-length check trivially passes (`/tmp` is 5 chars) and the runtime no-raise check also trivially passes (Linux `sun_path` is 108 bytes). That's intentional: the test is the sentinel for the macOS-specific Makefile shim, and running it on Linux is harmless and gives a tiny extra signal that the codepath is wired up.
5. **No torch pin.** Per investigation, every published cp314-arm64 torch wheel has the same SIGABRT under a long TMPDIR. The fix is the TMPDIR shim, not a torch pin. The Tester should NOT expect `pyproject.toml` or `uv.lock` diffs.
6. **For HUMAN acceptance (the `[HUMAN]` AC):** the operator should run `make memory-smoke-torch-shared-memory` from their daily terminal (no Claude Code wrapper) — should print `OK`. Then `make memory-serve-workflows &` + `make memory-run-data-pipeline USER_ID=<oid>` — the HF arxiv source should ingest without the SIGABRT. This is the end-to-end signal that the original #035 failure mode is dead.

---

### [Tester] 2026-05-19 19:00 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check && make pre-commit` clean)
- Unit tests: 1197 passed / 0 failed (`make memory-unit-tests`)
- Integration tests (slow, via make): 60 passed / 10 failed (deselected: 143). **The two new `test_torch_shared_memory.py` tests both PASSED.** The 10 failures are pre-existing, unrelated to #035 (Voyage multimodal API rejecting `voyage-3` model; Substack/Brightdata scraping). Zero references to `torch_shm_manager` / `SIGABRT` / `share_memory_` in any failure traceback — confirmed orthogonal per SWE's note 6.
- Warnings: 0 from the #035 tests (the pre-existing test failures generate noise unrelated to this task).

**E2E adversarial pass**

- **Happy path** — `make memory-smoke-torch-shared-memory` from a long-TMPDIR (81-char `com.apple.shortcuts.mac-helper`) harness shell.
  Output: `TMPDIR=/var/folders/77/qsf7lbgs6mv7q4tn90mqgpl00000gn/T/` then `OK`. **PASS.**

- **Break path A — shim only activates on Darwin (negation logic).**
  Static review of the Makefile conditional at lines 26-29: `UNAME_S := $(shell uname -s)` followed by `ifeq ($(UNAME_S),Darwin)` is byte-exact GNU-make string compare; no case folding, no whitespace tolerance. Linux returns `Linux` from `uname -s`, so the `export TMPDIR := $(shell getconf DARWIN_USER_TEMP_DIR)` block is skipped — Linux inherits its standard short `/tmp` (5 chars) and `sun_path` is 108 bytes there anyway. The conditional uses the canonical idiom (`UNAME_S := $(shell uname -s)` + `ifeq`) so there's no subtle quirk to flag. **PASS.**

- **Break path B — repro fails OUTSIDE make (sentinel proves real).**
  `cd apps/memory && uv run pytest tests/integration/test_torch_shared_memory.py -v` from the agent harness shell (long TMPDIR, no make wrapper):
  ```
  FAILED tests/integration/test_torch_shared_memory.py::test_torch_shm_socket_path_fits_in_sockaddr_un
    AssertionError: TMPDIR='/var/folders/.../com.apple.shortcuts.mac-helper//' is too long: a torch_shm_manager socket path under it would be 121 bytes, exceeding macOS's sockaddr_un.sun_path limit of 104.
  FAILED tests/integration/test_torch_shared_memory.py::test_torch_share_memory_runs_without_raising
    RuntimeError: no response from torch_shm_manager
  ============================== 2 failed in 0.86s ===============================
  ```
  Then `make memory-integration-tests-slow` (which goes through the make TMPDIR shim): `tests/integration/test_torch_shared_memory.py ..` — both PASSED.
  **PASS.** The tests are real sentinels: red without the shim, green with it.

- **Break path C — subprocess propagation.**
  Verified transitively via `make memory-smoke-torch-shared-memory`. That target runs `uv run python -c "... torch.tensor(0).share_memory_() ..."`, and `share_memory_()` internally forks `torch_shm_manager` as a grandchild process. The `OK` print proves TMPDIR survives ≥2 levels of subprocess inheritance (`make` → `uv run` (which itself spawns a venv-python subprocess) → torch's `torch_shm_manager` subprocess). Additionally verified directly: a Popen-based child-of-child probe under `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" bash -c '... uv run python ...'` shows both PARENT and CHILD TMPDIR are the short 49-byte path. Prefect's `serve()` uses Python's default `subprocess.Popen` (env inherited); a grep of `apps/memory/src/tree/` for `subprocess|Popen|spawn|multiprocessing` finds **no** tree-code override of `env`, so nothing in the codebase breaks the inheritance chain.
  **PASS.**

- **Break path D — HF datasets / torch share_memory_ end-to-end.**
  Reproduced the user's original failure mode: under long TMPDIR, `cd apps/memory && uv run python -c "import torch; torch.tensor([1.0, 2.0]).share_memory_()"` raises `RuntimeError: no response from torch_shm_manager` (full traceback captured: `THManagedMapAllocatorInit::THManagedMapAllocatorInit + 1660 (libshm.dylib)` — exactly the `sun_path` overflow described in the SWE diagnostic). Under the short TMPDIR (`TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`), the same one-liner prints `OK`. The HF arxiv pipeline's `load_dataset(..., streaming=True)` lands on the same `share_memory_()` call path, so this is the headline regression — and it's dead under make's TMPDIR shim.
  **PASS.**

- **Break path E — pin-free contract.**
  `git diff main..HEAD -- apps/memory/pyproject.toml apps/memory/uv.lock` returns empty (zero diff). The system-reminder dump of `apps/memory/pyproject.toml` shows no `torch==` pin and no constraint narrower than the transitive `sentence-transformers>=4.0` (which only requires `torch>=1.11.0`). Operator's "no silent downgrade" rule is honored.
  **PASS.**

**Spot-check of Makefile diff**

- Conditional: `UNAME_S := $(shell uname -s)` then `ifeq ($(UNAME_S),Darwin)` — exact match, idiomatic, no quirks.
- Export directive: `export TMPDIR := $(shell getconf DARWIN_USER_TEMP_DIR)` — Make's `export` propagates the variable into the environment of every recipe AND every sub-make and subprocess. Verified empirically via `make memory-print-tmpdir` (caller's 81-byte TMPDIR is replaced by 49-byte one in the recipe's env).
- Diagnostic targets (`print-tmpdir`, `smoke-torch-shared-memory`) are wired correctly. `print-tmpdir`'s byte-length echo correctly trims whitespace (`tr -d ' '`).
- Root `Makefile`'s `include .env; export` does NOT clobber TMPDIR (verified `.env` and `.env.example` carry no `TMPDIR` key).
- Root `memory-%` delegator runs `$(MAKE) -C apps/memory $*`, which spawns a fresh make process inside `apps/memory/`, which re-evaluates and applies the shim. Confirmed via `make memory-print-tmpdir` from the repo root.

**Acceptance criteria**

- [x] PASS — Darwin-only `export TMPDIR := $(shell getconf DARWIN_USER_TEMP_DIR)` shim with inline rationale in `apps/memory/Makefile:12-29`. Comment names the `sockaddr_un.sun_path` overflow and points at the tracker file. Evidence: `git diff apps/memory/Makefile`.
- [x] PASS — `make memory-print-tmpdir` runs and prints TMPDIR + byte length + uname. Evidence: command output above (`TMPDIR=/var/folders/77/.../T/`, `TMPDIR length: 49 bytes`, `uname -s: Darwin`).
- [x] PASS — `make memory-smoke-torch-shared-memory` exits 0 from a long-TMPDIR shell (the agent harness). Evidence: command output `OK` above.
- [x] PASS — `apps/memory/tests/integration/test_torch_shared_memory.py` exists with two `@pytest.mark.slow` tests, AAA pattern, no manual setup/teardown, `test_*` naming. Both pass through make-driven `pytest` and both fail red without the shim (proving the sentinel works). Evidence: file inspection + `make memory-integration-tests-slow` output.
- [x] PASS — `pyproject.toml` and `uv.lock` unchanged. Evidence: `git diff main..HEAD --stat -- apps/memory/pyproject.toml apps/memory/uv.lock` returns empty.
- [x] PASS — Format / lint / pre-commit / unit-tests clean. Evidence: `make memory-format-check && make memory-lint-check && make pre-commit && make memory-unit-tests` all green; 1197 unit tests passed.
- [x] PASS — `CLAUDE.md` `### macOS torch / TMPDIR shim` subsection under `## Configuration` (4 lines of prose + 1 heading; ~6 lines). Evidence: `git diff CLAUDE.md` shows the new subsection covering the constraint, the Makefile shim, the manual workaround, and a pointer to the regression sentinel.
- [ ] AWAITING HUMAN — `[HUMAN]` AC for operator's daily-shell verification of `make memory-smoke-torch-shared-memory` and full HF arxiv ingest. Not blocked by Tester gate.

**Evidence**

```
$ make memory-print-tmpdir
/Library/Developer/CommandLineTools/usr/bin/make -C apps/memory print-tmpdir
TMPDIR=/var/folders/77/qsf7lbgs6mv7q4tn90mqgpl00000gn/T/
TMPDIR length: 49 bytes
uname -s: Darwin

$ make memory-smoke-torch-shared-memory
/Library/Developer/CommandLineTools/usr/bin/make -C apps/memory smoke-torch-shared-memory
TMPDIR=/var/folders/77/qsf7lbgs6mv7q4tn90mqgpl00000gn/T/
OK

$ cd apps/memory && uv run pytest tests/integration/test_torch_shared_memory.py -v   # OUTSIDE make
FAILED tests/integration/test_torch_shared_memory.py::test_torch_shm_socket_path_fits_in_sockaddr_un
FAILED tests/integration/test_torch_shared_memory.py::test_torch_share_memory_runs_without_raising
   E       RuntimeError: no response from torch_shm_manager
============================== 2 failed in 0.86s ===============================

$ make memory-integration-tests-slow       # via make → shim active
tests/integration/test_torch_shared_memory.py ..                         [ 58%]
# (both green; pre-existing voyage-3/scraping failures elsewhere, none referencing torch_shm_manager)

$ make memory-unit-tests
1197 passed in 40.21s

$ git diff main..HEAD --stat -- apps/memory/pyproject.toml apps/memory/uv.lock
# (empty — no pin)
```

**Other issues found**

- None blocking. The 10 pre-existing slow-integration failures (Voyage multimodal `voyage-3` model rejection; Substack/Brightdata scraping) are noted as orthogonal per the SWE's caveat #6 and confirmed by zero references to `torch_shm_manager` in any failure trace. They belong to other tasks, not #035.
- Minor observation (Nit, do not block): the test's `_TORCH_SHM_SUFFIX_BYTES = 40` is a hand-rolled worst-case estimate. If a future libshm bumps the rand/pid lengths, the static check could stop catching cases the runtime check still catches. Fine as-is — the runtime check is the real safety net.

**VERDICT: PASS**
