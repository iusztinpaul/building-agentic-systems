# HuggingFace source window fields + serve admission bump

Status: pending
Tags: `config`, `data`, `infra`
Depends on: #069
Blocks: #071, #072

## Scope

Lay the CONFIG foundation for platform-grouped data orchestration + HuggingFace
dataset windowing (feature `data-platform-sharding-hf-windows`). This task ships ONLY
the typed config surface + the serve admission bump — NO fan-out wiring, NO offset
threading (those land in #071/#072). It must be a behavior-preserving change for every
existing run: a HuggingFace source with no new keys behaves exactly as today.

Two concrete changes:

### 1. `HuggingFaceDatasetSource` gains two fields

In `apps/memory/src/tree/config/app_config.py`, add two fields to
`HuggingFaceDatasetSource` (the discriminated-union variant), placed next to the
existing `batch_size`/`concurrency` knobs:

- `num_workers: int = 1` — a CONFIG knob (authored in YAML, like `batch_size`). It is
  the HuggingFace offset-window fan-out width: #072 will dispatch `num_workers`
  `data-etl-worker` runs, each ingesting one disjoint offset-window of the dataset.
  Default `1` ⇒ a single window covering the whole `max_samples` ⇒ today's behavior.
- `offset: int | None = None` — a RUNTIME coordinate, NOT authored in YAML. #072 sets
  it ONLY at dispatch via `entry.model_copy(update={"offset": …})`; #071 makes the
  ingest skip the first `offset` rows. Default `None` ⇒ no skip ⇒ today's behavior.

Document both fields in the class docstring, making the authored-vs-runtime split
explicit: `num_workers` is operator-authored YAML; `offset` is set by the orchestrator
at dispatch and must never appear in `default.yaml`.

The discriminated-union round-trip MUST still hold: `model_dump()` → JSON →
`TypeAdapter(list[SourceEntry]).validate_python` must preserve both new fields (the
orchestrator serializes shards through `run_deployment` flow-run params). `offset=None`
round-trips as `None`; a set `offset` round-trips as the int.

### 2. Raise `concurrency.runner_global_limit` 4 → 6 in `default.yaml`

In `apps/memory/configs/default.yaml`, under `concurrency:`, change
`runner_global_limit: 4` → `runner_global_limit: 6`. Update the inline comment to note
the bump accommodates the platform/window data fan-out (data workers are NOT
Voyage-bound; the `voyage-embeddings` GCL still caps embedding, so over-admitting is
safe). The typed default on `ConcurrencyConfig.runner_global_limit` in `app_config.py`
STAYS at `4` (it's the fallback when YAML omits the block — unchanged behavior for
configs that don't set it).

The frozen test fixture
`apps/memory/tests/unit/config/fixtures/frozen_config.yaml` MUST NOT be edited — it
stays at `runner_global_limit: 4`. The config tests in `test_app_config.py` assert
against the fixture (and against inline YAML they write themselves), NOT against
`default.yaml`, so they stay green by construction. Do NOT touch the fixture or its
source counts.

### Files touched

- `apps/memory/src/tree/config/app_config.py` — add `num_workers` + `offset` to
  `HuggingFaceDatasetSource`; update its docstring. (Leave
  `ConcurrencyConfig.runner_global_limit` default at `4`.)
- `apps/memory/configs/default.yaml` — `runner_global_limit: 4` → `6` (+ comment); the
  HF arxiv source entry MAY gain an explicit `num_workers:` line (optional; default 1 is
  fine) — do NOT add `offset` to YAML.
- `apps/memory/tests/unit/config/test_app_config.py` — add assertions for the new HF
  fields' defaults + an explicit-value parse + the round-trip (see test guidance). Do
  NOT change the existing `runner_global_limit == 4` assertions (they read the fixture).
- (NOT touched, asserted by absence) `apps/memory/tests/unit/config/fixtures/frozen_config.yaml`.

## Acceptance Criteria

- [x] `HuggingFaceDatasetSource` has `num_workers: int = 1` and `offset: int | None = None`,
      placed alongside `batch_size`/`concurrency`, with a docstring that states
      `num_workers` is YAML-authored and `offset` is a dispatch-time runtime coordinate
      (never in YAML).
- [x] `HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot")` yields
      `num_workers == 1` and `offset is None` (defaults preserve today's behavior).
- [x] An explicitly-constructed `HuggingFaceDatasetSource(..., num_workers=4, offset=500)`
      carries those exact values.
- [x] `model_dump()` → `json.dumps`/`json.loads` →
      `TypeAdapter(list[SourceEntry]).validate_python` round-trips a HF entry preserving
      `type`, `uri`, `max_samples`, `fetch_content`, `batch_size`, `concurrency`,
      `num_workers`, and `offset` (both the `None` and a set-int case).
- [x] `apps/memory/configs/default.yaml` sets `concurrency.runner_global_limit: 6`, with
      an updated comment explaining the data-fan-out justification.
- [x] `load_app_config("apps/memory/configs/default.yaml").concurrency.runner_global_limit == 6`.
- [x] `ConcurrencyConfig().runner_global_limit == 4` (the typed default is UNCHANGED;
      the bump is YAML-only).
- [x] `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` is byte-unchanged and
      still parses to `runner_global_limit == 4`; the existing fixture-based config tests
      (`test_concurrency_block_loaded_from_default_yaml`, `test_concurrency_defaults_when_absent`)
      remain green WITHOUT edits.
- [x] No existing behavior changes for a HF source without the new keys (defaults make
      it byte-equivalent to today).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` all clean.
- [x] `make memory-unit-tests` passes, 0 warnings.

## BDD scenarios

### Scenario: HF source defaults preserve today's behavior
- **Given** a `HuggingFaceDatasetSource` constructed with only a `uri`
- **When** I read its fields
- **Then** `num_workers == 1` and `offset is None`, so #071/#072 treat it as a single
  whole-`max_samples` window with no skip — identical to the pre-feature run.

### Scenario: HF source carries an authored fan-out width and a runtime offset
- **Given** `HuggingFaceDatasetSource(uri="…", max_samples=1000, num_workers=4, offset=250)`
- **When** I serialize it with `model_dump()`, JSON round-trip it, and re-parse via
  `TypeAdapter(list[SourceEntry])`
- **Then** the re-parsed entry is a `HuggingFaceDatasetSource` with `num_workers == 4`
  and `offset == 250` (the discriminated-union round-trip through `run_deployment`
  params is intact).

### Scenario: default.yaml admission limit is raised but the typed default is not
- **Given** the edited `apps/memory/configs/default.yaml`
- **When** I `load_app_config("apps/memory/configs/default.yaml")`
- **Then** `concurrency.runner_global_limit == 6`
- **And** a bare `ConcurrencyConfig()` (no YAML) still reports `runner_global_limit == 4`.

### Scenario: the frozen fixture and its tests are untouched
- **Given** the config-test suite that loads `frozen_config.yaml`
- **When** I run `make memory-unit-tests` after this change
- **Then** `test_concurrency_block_loaded_from_default_yaml` and
  `test_concurrency_defaults_when_absent` still assert `runner_global_limit == 4` and
  pass — the fixture was not edited.

## User Stories

### Story: Operator declares a HuggingFace fan-out width in YAML
1. Operator opens `apps/memory/configs/default.yaml` and finds the
   `librarian-bots/arxiv-metadata-snapshot` HuggingFace source entry.
2. Operator adds `num_workers: 4` under that entry (next to `max_samples`, `batch_size`,
   `concurrency`).
3. On the next config load, `app_config.sources.sources` contains a
   `HuggingFaceDatasetSource` with `num_workers == 4` and `offset is None`.
4. No other source type is affected; no `offset` key is needed or accepted in YAML.

### Story: More data runs can be admitted concurrently
1. Operator (or the platform fan-out in #072) wants to run several data-worker runs at
   once across platform buckets + HF windows.
2. With `runner_global_limit: 6` in `default.yaml`, `make memory-serve-workflows` admits
   up to 6 concurrent flow runs instead of 4.
3. Embedding throughput is unaffected — the `voyage-embeddings` GCL still throttles real
   Voyage POSTs; data workers don't embed, so the extra admission slots are free.

## Test guidance

- These are PURE config tests → UNIT (`tests/unit/config/test_app_config.py`), no Mongo,
  no Prefect, no markers.
- Mirror the existing `TestConcurrencyConfig` / source-variant test style. Use
  `load_app_config(<path>)` for file-based assertions and direct model construction for
  field-default assertions.
- For the round-trip, reuse the `_SOURCES_ADAPTER` / `TypeAdapter(list[SourceEntry])`
  pattern already proven in #068's `test_pipeline.py::test_reconstructs_sources_from_serialized_dicts`.
- Per CLAUDE.md, call the `/testing-python` skill when writing the tests.

---

Blocked by: #069

## Log

### [PA] 2026-06-22 14:00 — Grooming

**Summary**
Config foundation for the platform-sharding + HF-windowing feature: add
`num_workers` (YAML-authored fan-out width) and `offset` (dispatch-time runtime
coordinate) to `HuggingFaceDatasetSource`, and raise `concurrency.runner_global_limit`
4→6 in `default.yaml` only. No fan-out or offset wiring yet — pure typed surface +
admission bump. Behavior-preserving for any HF source without the new keys.

**Key decisions**
- `num_workers` default `1` and `offset` default `None` make the change byte-equivalent
  to today for existing configs (the whole point of landing config first).
- `runner_global_limit` is bumped in YAML only; the typed `ConcurrencyConfig` default
  stays `4`. The frozen fixture is deliberately NOT edited so the fixture-based config
  tests stay green with zero changes (they assert against the fixture, not default.yaml).
- The discriminated-union round-trip through `run_deployment` params is the load-bearing
  invariant for #072's dispatch — covered by an explicit round-trip test here.

**Dependencies**
- #069 — the orchestrator/worker split + serve-registration baseline this feature builds
  on (last task of the prior feature; the data orchestrator/worker exist and are green).

**User stories**
- 2 stories: operator declares `num_workers` in YAML; more concurrent data runs admitted
  via the raised serve limit.

Ready for implementation.

### [SWE] 2026-06-22 17:30 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — added `num_workers: int = Field(default=1, ge=1)`
  (YAML-authored fan-out width) and `offset: int | None = None` (dispatch-time runtime
  coordinate) to `HuggingFaceDatasetSource`, next to `batch_size`/`concurrency`; expanded the
  class docstring with the authored-vs-runtime split and the round-trip invariant. Left
  `ConcurrencyConfig.runner_global_limit` typed default at `4`.
- `apps/memory/configs/default.yaml` — `concurrency.runner_global_limit: 4 → 6` with an
  updated comment justifying the data-fan-out bump (data workers aren't Voyage-bound; the
  `voyage-embeddings` GCL still caps embedding). Also added an explicit `num_workers: 1`
  line + comment to the HF arxiv entry to make the knob discoverable (default 1 = today's
  behavior; `offset` deliberately NOT authored).
- `apps/memory/tests/unit/config/test_app_config.py` — added `TestHuggingFaceWindowFields`
  (defaults, explicit values, `num_workers >= 1` validation, YAML-authored entry leaves
  `offset=None`, dispatch-time `model_copy(update={"offset": ...})`, discriminated-union
  JSON round-trip for both `offset=None` and a set int) and `TestRunnerGlobalLimitBump`
  (default.yaml loads `6`; typed `ConcurrencyConfig()` stays `4`).

**Tests**
- Unit: 1561 passing, 0 failing, 0 warnings — `make memory-unit-tests` (was 1554; +7 new).
  Wrote the 7 new tests red first (`AttributeError: ... no attribute 'num_workers'` / `assert 4 == 6`),
  then implemented to green.
- Integration: N/A — pure config change, no Mongo/Prefect/infra touched.

**Acceptance criteria** — all met (checkboxes flipped above)
- [x] `num_workers`/`offset` fields + docstring — `test_defaults_preserve_todays_behavior`,
      `test_explicit_values_are_carried`
- [x] `uri`-only HF source → `num_workers == 1`, `offset is None` — `test_defaults_preserve_todays_behavior`
- [x] explicit `num_workers=4, offset=500` carried — `test_explicit_values_are_carried`
- [x] JSON discriminated-union round-trip (None + set int) — `test_discriminated_union_round_trip_preserves_window_fields`
- [x] default.yaml `runner_global_limit: 6` + comment — `test_default_yaml_raises_runner_global_limit_to_six`
- [x] `load_app_config(default.yaml).concurrency.runner_global_limit == 6` — same test
- [x] typed `ConcurrencyConfig().runner_global_limit == 4` — `test_typed_default_runner_global_limit_unchanged`
- [x] frozen fixture byte-unchanged, still `4`; `test_concurrency_*` green WITHOUT edits —
      verified via `git diff --name-only` (no output for the fixture) + full suite green
- [x] no behavior change for a HF source without the new keys — defaults are byte-equivalent
- [x] format/lint/pre-commit clean
- [x] `make memory-unit-tests` passes, 0 warnings

**Evidence**
```
$ make memory-unit-tests
============================ 1561 passed in 46.36s =============================

$ git diff --name-only -- apps/memory/tests/unit/config/fixtures/frozen_config.yaml
(no output — fixture byte-unchanged)
$ grep -n runner_global_limit apps/memory/tests/unit/config/fixtures/frozen_config.yaml
110:  runner_global_limit: 4

$ make memory-format-check && make memory-lint-check
280 files already formatted
All checks passed!
$ make pre-commit
... ruff check Passed / ruff format Passed / biome check Passed

# End-to-end (load the real config the way the runtime does):
default.yaml runner_global_limit = 6
default.yaml HF num_workers = 1 | offset = None
ConcurrencyConfig().runner_global_limit = 4
defaults -> num_workers = 1 | offset = None
after model_copy -> authored.offset = None | dispatched.offset = 250
round-trip -> [(4, None), (4, 250)]
num_workers=0 rejected: ValidationError
```

**Notes**
- Behavior-preserving by construction: `num_workers=1` + `offset=None` reproduce today's
  single-whole-window, no-skip ingest exactly.
- Per the repo decoupling discipline, NO test asserts `default.yaml`'s limit by re-reading
  the frozen fixture. The 4→6 assertion uses `load_app_config(_DEFAULT_CONFIG_PATH)` (a
  loaded-from-default smoke against the real human config), and the frozen fixture stays at
  4 so the existing fixture-based tests pass with zero edits.
- No fan-out / offset-threading wiring here (that's #071/#072) — this ships only the typed
  config surface + the admission bump.
- NOT committed — handing to Tester for verification first.

### [Tester] 2026-06-22 18:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`280 files already formatted`; `All checks passed!`; prettier/ruff-check/ruff-format/biome all `Passed`)
- Unit tests: 1561 passed / 0 failed (`make memory-unit-tests`)
- Integration tests: N/A — pure config change, no Mongo/Prefect/infra touched (correctly skipped per task)
- Warnings: 0 pytest warnings. The lone stderr `UserWarning` (opik `pydantic.v1` on Python 3.14) is a pre-existing third-party import-time warning, NOT in pytest's warnings summary and unrelated to the 070 diff.

**Scope hygiene**
- `git diff --stat` for 070 touches exactly the 3 named files (`app_config.py`, `default.yaml`, `test_app_config.py`). `frozen_config.yaml` is NOT in the diff. The ADR + glossary modifications are PA/planning artifacts, out of scope per task instructions — ignored.

**E2E adversarial pass** (config surface, exercised the way the runtime + #071/#072 will)
- Happy path: `load_app_config(_DEFAULT_CONFIG_PATH)` → `runner_global_limit == 6`, single HF source with `num_workers == 1`, `offset is None` (PASS)
- Break path 1 (boundary: `num_workers` < 1): `HuggingFaceDatasetSource(uri=…, num_workers=0 / -1 / -100)` → all raise `ValidationError` vs expected reject (PASS); `num_workers=10000` accepted (PASS)
- Break path 2 (malformed type): `num_workers="abc"` → `ValidationError`; `num_workers=1.5` → `ValidationError` (strict int); `offset="abc"` → `ValidationError`; `num_workers="3"` → coerced to `3` (consistent with sibling `batch_size`/`concurrency` int fields) (PASS)
- Break path 3 (falsy/runtime edge: `offset=0` round-trip): `offset=0` survives `model_dump()`→JSON→`TypeAdapter(list[SourceEntry])` as `0` (not dropped as falsy); `num_workers` preserved (PASS)
- Break path 4 (state edge: dispatch via `model_copy`): `authored.model_copy(update={"offset":250})` → authored stays `offset=None`, dispatched `offset=250`, `num_workers` carried (PASS)
- Break path 5 (extra-field discipline): YAML-style `model_validate({type,uri,num_workers:4})` → `offset is None` (not required in YAML) (PASS)

**Acceptance criteria** — all verified independently
- [x] PASS — `num_workers: int = Field(default=1, ge=1)` + `offset: int | None = None` placed next to `batch_size`/`concurrency`, docstring states authored-vs-runtime split — `app_config.py:349-353` + docstring `:322-341`
- [x] PASS — uri-only HF source → `num_workers == 1`, `offset is None` — `test_defaults_preserve_todays_behavior` + adversarial probe
- [x] PASS — explicit `num_workers=4, offset=500` carried — `test_explicit_values_are_carried` + probe
- [x] PASS — `model_dump()`→json→`TypeAdapter(list[SourceEntry])` preserves `type/uri/max_samples/fetch_content/batch_size/concurrency/num_workers/offset` (None + set int) — `test_discriminated_union_round_trip_preserves_window_fields` + probe (also confirmed `offset=0` falsy edge survives)
- [x] PASS — `default.yaml` sets `runner_global_limit: 6` with data-fan-out comment — `default.yaml:171-181`
- [x] PASS — `load_app_config(default.yaml).concurrency.runner_global_limit == 6` — `test_default_yaml_raises_runner_global_limit_to_six` + probe
- [x] PASS — `ConcurrencyConfig().runner_global_limit == 4` (typed default unchanged) — `test_typed_default_runner_global_limit_unchanged`; `app_config.py:242` still `= 4`
- [x] PASS — frozen fixture byte-unchanged (not in `git diff`), still `runner_global_limit: 4` (line 110), `test_concurrency_block_loaded_from_default_yaml` + `test_concurrency_defaults_when_absent` green WITHOUT edits (both read `frozen_config_path`, assert `== 4` at lines 304/318) — decoupling discipline held, new bump test reads `_DEFAULT_CONFIG_PATH` not the fixture
- [x] PASS — no behavior change for HF source without new keys: `num_workers=1`+`offset=None` byte-equivalent; fixture/`default.yaml`-omitting configs unaffected (fixture has neither field → typed defaults apply)
- [x] PASS — format/lint/pre-commit clean
- [x] PASS — `make memory-unit-tests` passes, 0 pytest warnings

**Evidence**
```
$ make memory-unit-tests
======================= 1561 passed in 93.41s (0:01:33) ========================

$ git diff --name-only -- apps/memory/tests/unit/config/fixtures/frozen_config.yaml
(no output — fixture byte-unchanged)
$ grep -n runner_global_limit apps/memory/tests/unit/config/fixtures/frozen_config.yaml
110:  runner_global_limit: 4

$ uv run pytest .../TestConcurrencyConfig .../TestHuggingFaceWindowFields .../TestRunnerGlobalLimitBump -v
============================== 11 passed in 0.52s ==============================
  (incl. test_concurrency_block_loaded_from_default_yaml PASSED, test_concurrency_defaults_when_absent PASSED — unedited)

# Adversarial probe (load real config + boundary/type/round-trip attacks):
default.yaml runner_global_limit = 6 | HF num_workers = 1 | offset = None
ConcurrencyConfig().runner_global_limit = 4
num_workers 0/-1/-100 rejected (ValidationError); 10000 accepted
offset=0 round-trip -> 0 ; num_workers=2 round-trip -> 2
ALL ASSERTIONS PASSED
```

**Other issues found** (non-blocking — not in 070's ACs)
- `offset` has no `ge=0` constraint, so `offset=-5` is accepted. The 070 spec defines `offset` as a free runtime coordinate and does NOT require a constraint; #071 (which makes the ingest "skip the first `offset` rows") owns the consuming validation. Flagging as a follow-up note for #071, not a 070 defect.
- `num_workers="3"` is lax-coerced to `3` (standard Pydantic v2, consistent with sibling `batch_size`/`concurrency` plain-int fields). Not a regression.

**VERDICT: PASS**
