---
id: 084-shared-source-loader
feature: sources-config-split
status: done
---

# Add shared source loader (tree/config/sources.py)

Tags: `data`, `config`
Depends on: #083
Blocks: #085, #086
Implements: ADR-003

## Scope

Add a single new module `apps/memory/src/tree/config/sources.py` that is the one way to
turn source files / URL tokens into typed `SourceEntry`s: `load_sources(paths)`, a cached
`default_configured_sources()` (= backfill + listen), a `parse_uri_token(token)` helper for
the `URL` / `URL=TYPE` CLI syntax, and `build_uri_sources(specs)` (reuses the existing
untyped-entry inference and rejects `huggingface_dataset`). Pure addition — no existing
consumer is repointed in this task.

## Acceptance criteria

- [x] New module `tree/config/sources.py` imports the source models from
      `tree.config.app_config` (no model relocation; one-way import, no cycle).
- [x] Path constants `SOURCES_DIR`, `BACKFILL_PATH`, `LISTEN_PATH` point at the repo-root
      `sources/` dir and its two files.
- [x] `load_sources(paths: list[str | Path]) -> list[SourceEntry]` reads each YAML file,
      validates it via `SourcesConfig` (so untyped entries still infer their `type`), and
      concatenates the results in the given order.
- [x] `load_sources` resolves a RELATIVE path by trying both the module-derived repo root
      AND the process cwd, first-existing-wins; a path that resolves under neither raises
      `FileNotFoundError` naming both attempted locations. (This is what makes the cron's
      `"sources/listen.yaml"` resolve under local serve with cwd=`apps/memory/` AND under a
      Prefect cloud managed run with cwd=git-clone-root — see ADR-003.)
- [x] `default_configured_sources() -> list[SourceEntry]` returns
      `load_sources([BACKFILL_PATH, LISTEN_PATH])` and is cached (`functools.cache`); the
      cache is clearable for tests.
- [x] `parse_uri_token(token: str) -> tuple[str, str | None]` parses one CLI `--uri` value:
      it splits on the RIGHTMOST `=` ONLY when the suffix is a recognized `SourceEntry`
      `type` literal (the full set, incl. `huggingface_dataset`), returning `(uri, type)`;
      otherwise the whole token is the uri and the type is `None`. This keeps untyped URLs
      that contain `=` intact (e.g. `…/feeds/videos.xml?channel_id=UC…` → `(that_url, None)`)
      while `…/feed=substack_rss` → `(…/feed, "substack_rss")`.
- [x] `build_uri_sources(specs: list[tuple[str, str | None]]) -> list[SourceEntry]`:
      - builds raw dicts (`{"uri": u}` when type is `None`, else `{"uri": u, "type": t}`) and
        normalizes+validates ALL specs in one `SourcesConfig` pass, reusing
        `_normalize_untyped_entry` for omitted types (youtube_rss / youtube_video /
        substack_article / web), including cross-entry substack-host inference;
      - raises `ValueError` with a clear message if any resulting entry is a
        `HuggingFaceDatasetSource` (HF needs `max_samples`/`batch_size`/… — define it in
        `sources/backfill.yaml` and use `--source-file`). Inference never yields HF, so this
        fires only on an explicit `…=huggingface_dataset` token.
      - NOTE: signature takes already-parsed `(uri, type|None)` tuples — there is NO
        parallel-list / count-matching constraint (per-URI optional typing is intrinsic).
- [x] Unit tests (call the `/testing-python` skill): `load_sources` reads both committed
      files; `default_configured_sources()` equals their union and is cached;
      `parse_uri_token` returns `None` type for a bare URL and for a query-string URL that
      contains `=`, and splits only on a valid trailing `=TYPE`; `build_uri_sources` resolves
      a MIX of typed + untyped specs in one call (typed honored, untyped inferred), and
      raises on a spec that builds a `huggingface_dataset`.
- [x] No consumer is repointed; `AppConfig.sources` and `default.yaml` are untouched.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean; `make memory-unit-tests` green, 0 warnings.

## Out of scope

- Repointing `online_pipeline` / arxiv / orchestrator (085, 086). Removing
  `AppConfig.sources`, the `scheduled` field, or the `default.yaml` block (087).

## Log

### [SWE] 2026-06-27 — Implementation

**Files modified**
- `apps/memory/src/tree/config/sources.py` — NEW shared source loader: `SOURCES_DIR`/`BACKFILL_PATH`/`LISTEN_PATH` constants, `load_sources`, cached `default_configured_sources`, `parse_uri_token`, `build_uri_sources`. One-way import of the source models from `tree.config.app_config` (no cycle).
- `apps/memory/tests/unit/config/test_sources.py` — NEW unit tests (36 cases) covering every public function and the path-resolution / cache / HF-rejection behaviors.
- `sources/listen.yaml` — pre-commit's `prettier` hook removed one trailing blank line (whitespace-only, no semantic change). See Notes.

**Tests**
- Unit: 1708 passing, 0 failing, 0 warnings — `make memory-unit-tests` (full suite). The 36 new cases live in `tests/unit/config/test_sources.py`.
- Integration: N/A — pure library addition, no infra layer touched.

**Acceptance criteria**
- [x] New module imports `SourceEntry`/`SourcesConfig`/variants from `tree.config.app_config`; one-way, no cycle (`app_config.py` imports nothing from `sources.py`).
- [x] `SOURCES_DIR`/`BACKFILL_PATH`/`LISTEN_PATH` resolve to the repo-root `sources/` dir + its two files (`_REPO_ROOT = parents[5]`) — `tests/.../test_sources.py::TestPathConstants`.
- [x] `load_sources` reads + `SourcesConfig`-validates + concatenates in order — `TestLoadSources`.
- [x] Dual relative-path resolution (repo-root + cwd, first-existing-wins) + `FileNotFoundError` naming both — `TestLoadSourcesPathResolution`.
- [x] `default_configured_sources` = backfill+listen, `functools.cache`, clearable — `TestDefaultConfiguredSources`.
- [x] `parse_uri_token` splits only on a trailing real type literal; keeps query-string URLs intact — `TestParseUriToken`.
- [x] `build_uri_sources` one-pass normalize+validate (typed honored, untyped inferred incl. cross-entry substack host), rejects HF with a YAML-file message — `TestBuildUriSources`.
- [x] Unit tests cover all required scenarios.
- [x] No consumer repointed; `AppConfig.sources` + `default.yaml` untouched.
- [x] format/lint/pre-commit clean; unit tests green, 0 warnings.

**Evidence**
```
$ make memory-unit-tests
... 1708 passed in 46.72s ...

$ make memory-format-check && make memory-lint-check
288 files already formatted
All checks passed!

$ make pre-commit
prettier ... Passed
ruff check ... Passed
ruff format ... Passed
biome check (harness) ... Passed

$ uv run python -c "<exercise loader>"
SOURCES_DIR = .../sources
backfill: 14 entries; types=['huggingface_dataset', 'substack_article', 'web', 'youtube_video']
listen:   3 entries; types=['substack_rss']
default_configured_sources: 17 entries (cached); cache hit same obj? True
parse_uri_token bare url      -> ('https://example.com', None)
parse_uri_token query-string  -> ('https://www.youtube.com/feeds/videos.xml?channel_id=UCabc', None)
parse_uri_token =substack_rss -> ('https://www.decodingai.com/feed', 'substack_rss')
build_uri_sources mix         -> [('web', '...'), ('substack_article', '...'), ('substack_rss', '...')]
HF rejection                  -> ValueError: huggingface_dataset sources cannot be built from a --uri token ...
```

**Notes**
- `_normalize_untyped_entry` is reused *transitively* via the single `SourcesConfig.model_validate` pass (its `model_validator(mode="before")` calls it, including the cross-entry substack-host inference). I did NOT add a direct import of `_normalize_untyped_entry` to `sources.py` because it would be unused (ruff F401) — the acceptance criterion "normalize+validate ALL in one SourcesConfig pass" is satisfied by the single validate call.
- `parse_uri_token` derives the recognized type-literal set from `SourceEntry` itself (`_source_type_literals()`), so a new union variant is picked up automatically rather than drifting against a hand-maintained list.
- The `sources/listen.yaml` change is purely the repo-wide `prettier` pre-commit hook removing one trailing blank line that 083's committed file carried; kept because the repo-wide `make pre-commit` gate requires it. No YAML semantics changed (the `test_sources_files.py` counts still hold). Pre-existing PA grooming artifacts also present in the working tree (`docs/glossary.md`, `docs/adrs/003_*.md`, `tasks/085-088`, `tasks/README.md`) were left untouched.
- NOT committed — handing off to the Tester per the lifecycle.

### [Tester] 2026-06-27 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check` → 288 files already formatted; `make memory-lint-check` → All checks passed!; `make pre-commit` → prettier/ruff check/ruff format/biome all Passed)
- Unit tests: 1708 passed / 0 failed (`make memory-unit-tests`, env=local)
- Integration tests: N/A — my call. Pure library addition: file IO + Pydantic validation only, no MongoDB/Prefect/Opik/model infra path touched. No integration surface to exercise.
- Warnings: 0 (no pytest warnings section emitted)
- code-review plugin: enabled in `.claude/settings.json` but not invocable as a slash command from this tool-restricted QA agent; performed equivalent manual review instead (see Other issues).

**E2E adversarial pass** (independent script, 38 checks, all PASS — did not reuse SWE tests)
- Happy path: `default_configured_sources()` → 17 entries (14 backfill + 3 listen), equals `load_sources([BACKFILL_PATH, LISTEN_PATH])` (PASS)
- Break path 1 (parse_uri_token / query-string `=`): `https://www.youtube.com/feeds/videos.xml?channel_id=UCabc` → `(url, None)` NOT split; `.../page=notarealtype` → `(token, None)`; `.../feed=substack_rss` → `(.../feed, "substack_rss")`; all 6 type literals split; bare `=` empty suffix → `(token, None)` (PASS)
- Break path 2 (load_sources path resolution from foreign cwd): from a tmpdir cwd, `"sources/listen.yaml"` resolves via module repo root → 3 entries; `"does/not/exist.yaml"` → `FileNotFoundError` naming BOTH repo-root and cwd absolute paths; cwd-only `sub/feed.yaml` resolves via cwd; empty YAML → `[]`; missing absolute path raises (PASS)
- Break path 3 (default_configured_sources cache): 2nd call returns same object (`is`); equals union; `cache_clear()` forces recompute to a new object that still equals union (PASS)
- Break path 4 (build_uri_sources mix + HF reject): typed `substack_rss` honored + untyped substack_article/web/youtube_video/youtube_rss inferred in one pass; cross-entry substack-host inference works; explicit `=huggingface_dataset` → `ValueError` naming `sources/backfill.yaml`; HF mixed with valid specs still raises; untyped HF id → WebSource (not rejected, by design); empty `''` uri → ValidationError (PASS)
- Break path 5 (no import cycle): `tree.config.sources` + `tree.config.app_config` import cleanly; `app_config.py` contains no import of `sources` (one-way) (PASS)

**Acceptance criteria** — all verified PASS
- [x] PASS — new module imports source models one-way from `app_config` — `sources.py:20-24`; no cycle (adversarial check 0)
- [x] PASS — `SOURCES_DIR`/`BACKFILL_PATH`/`LISTEN_PATH` at repo-root `sources/` — `_REPO_ROOT=parents[5]` (`sources.py:29-33`); `SOURCES_DIR.is_dir()` true
- [x] PASS — `load_sources` reads+`SourcesConfig`-validates+concatenates in order — `test_sources.py::TestLoadSources` (5) + counts 14/3/17
- [x] PASS — relative path dual resolution + `FileNotFoundError` naming both — independent foreign-cwd run confirmed message names both abs paths
- [x] PASS — `default_configured_sources` = backfill+listen, `functools.cache`, clearable — `is`-identity + cache_clear recompute confirmed
- [x] PASS — `parse_uri_token` splits only on trailing real type literal; query-string `=` URLs stay intact — break path 1
- [x] PASS — `build_uri_sources` one SourcesConfig pass (typed honored, untyped inferred, cross-host) + HF `ValueError` pointing at a YAML file — break path 4
- [x] PASS — unit tests cover all required scenarios — 36 cases in `test_sources.py`
- [x] PASS — no consumer repointed; `AppConfig.sources` + `default.yaml` untouched — `git diff --name-only` = only `docs/glossary.md` + `sources/listen.yaml`; `app_config.py`/`default.yaml` not in diff
- [x] PASS — format/lint/pre-commit clean; unit tests green, 0 warnings — see Test summary

**Evidence**
```
$ make memory-unit-tests
============================ 1708 passed in 45.64s =============================

$ make memory-format-check && make memory-lint-check
288 files already formatted
All checks passed!

$ uv run python <adversarial.py>
total checks: 38 | PASS: 38 | FAIL: 0
ALL ADVERSARIAL CHECKS PASS
```

**Other issues found** (non-blocking, not in AC)
- Minor smell: `default_configured_sources()` returns the cached `list` object directly; a future consumer (085/086) that mutates it in place (`.append`/`.extend`) would poison the process-global cache. Consider returning a copy or a tuple when the consumers land. Not a defect today (no consumer repointed).
- `parse_uri_token` will split a query param whose value happens to equal a type literal (e.g. `https://example.com/?q=web` → `(.../?q, "web")`). This is the documented rightmost-`=` + valid-literal design and matches the AC; flagging only so consumers are aware a `--uri` ending in `?x=web`/`?x=substack_rss` needs the explicit-type escape hatch or trailing slash.
- `docs/glossary.md` is modified in the working tree (new "Source file"/"Backfill sources"/"Listen sources" terms) — a PA grooming artifact for the broader `sources-config-split` feature, not part of this task's required diff. Out of 084's scope; left for the PR Reviewer discipline backstop.

**VERDICT: PASS**
