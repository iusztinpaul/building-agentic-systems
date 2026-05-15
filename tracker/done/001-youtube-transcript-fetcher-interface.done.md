# YouTube transcript-fetcher interface + `youtube-transcript-api` primary impl + chained fallback wrapper

Status: pending
Tags: `data`, `enhancement`, `youtube`
Depends on: None
Blocks: #002, #003, #004

## Scope

Introduce a new package `tree.data.youtube` and ship the swappable backend that all subsequent YouTube ETLs will sit on. This task is **interface + primary impl + chain wrapper + URL/ID helpers + tests only** — it does NOT yet wire up Prefect flows, `SourceEntry` variants, dispatcher routing, or the `default.yaml` URIs. Those land in #003–#006. The Gemini-backed fallback fetcher is a separate task (#002) that will plug into the chain wrapper shipped here.

The chain wrapper is the public default exposed to the ETL pipelines. ETLs (#003 single-video, #004 RSS) call a single `TranscriptFetcher.fetch_many(...)` API; the chain advances from primary to fallback for any slot the primary couldn't transcribe, fully transparent to callers.

### Files to create

- `apps/memory/src/tree/data/youtube/__init__.py` — empty package marker (mirror `tree/data/substack/__init__.py`).
- `apps/memory/src/tree/data/youtube/types.py` — module-local data types (no cross-app re-use, so they live in a local `types.py` per `CLAUDE.md`'s "loose clean architecture" rule).
- `apps/memory/src/tree/data/youtube/urls.py` — URL/ID helpers (pure functions, no I/O).
- `apps/memory/src/tree/data/youtube/transcript_fetcher.py` — abstract `TranscriptFetcher` Protocol, the `YoutubeTranscriptApiFetcher` primary impl, and the `ChainedTranscriptFetcher` composite wrapper.
- `apps/memory/tests/unit/data/youtube/__init__.py`
- `apps/memory/tests/unit/data/youtube/test_urls.py`
- `apps/memory/tests/unit/data/youtube/test_transcript_fetcher.py`

### Data types (`tree.data.youtube.types`)

Pydantic models (we use Pydantic per `CLAUDE.md`):

```python
class VideoMetadata(BaseModel):
    video_id: str            # 11-char YouTube ID, never the full URL
    title: str | None = None
    channel: str | None = None
    channel_id: str | None = None
    publish_date: datetime | None = None   # tz-aware UTC
    duration_seconds: int | None = None
    description: str | None = None

class TranscriptSegment(BaseModel):
    text: str
    start_seconds: float
    duration_seconds: float

class FetchedTranscript(BaseModel):
    metadata: VideoMetadata
    segments: list[TranscriptSegment]      # may be empty if no transcript
    language: str | None = None            # actual language returned (e.g. "en")
    plain_text: str                        # joined segment texts, newline-separated
```

All `datetime` fields are timezone-aware UTC (project rule).

### URL/ID helpers (`tree.data.youtube.urls`)

Pure helpers, fully unit-testable:

- `extract_video_id(url_or_id: str) -> str | None` — accepts:
  - `https://www.youtube.com/watch?v=eYaWxljC4sA` → `"eYaWxljC4sA"`
  - `https://youtu.be/eYaWxljC4sA` → `"eYaWxljC4sA"`
  - `https://www.youtube.com/shorts/eYaWxljC4sA` → `"eYaWxljC4sA"`
  - `https://m.youtube.com/watch?v=eYaWxljC4sA&t=10s` → `"eYaWxljC4sA"`
  - bare 11-char ID → returned as-is
  - anything else → `None`
- `extract_channel_id_from_rss_url(url: str) -> str | None` — parses `https://www.youtube.com/feeds/videos.xml?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw` and returns the `channel_id`. Returns `None` for unrelated URLs.
- `is_youtube_video_url(url: str) -> bool` — true for `youtube.com/watch?v=…`, `youtu.be/…`, `youtube.com/shorts/…`, `m.youtube.com/…` host variants.
- `is_youtube_rss_url(url: str) -> bool` — true for `youtube.com/feeds/videos.xml?channel_id=…` (the `channel_id` query param must be present).
- `canonical_video_url(video_id: str) -> str` — returns `https://www.youtube.com/watch?v={id}`. This is the value used for `Document.source_uri`, ensuring de-duplication regardless of which form the user pasted.

### Transcript fetcher interface (`tree.data.youtube.transcript_fetcher`)

```python
class TranscriptFetcher(Protocol):
    async def fetch_many(
        self, video_urls_or_ids: list[str]
    ) -> list[FetchedTranscript | None]: ...
```

- One element per input, **same order** as input. `None` when this fetcher cannot produce a transcript for that slot (so the chain wrapper can advance to the next fetcher without losing alignment).
- Implementations are responsible for being non-throwing on per-video failure: a missing transcript / unsupported video / per-call exception MUST be returned as a `None` slot, not raised. Catastrophic backend failure (auth error, malformed call, network down for the entire batch) MAY raise — chain semantics are: "exhaust fetchers per slot," not "swallow infra errors."

### Primary impl: `YoutubeTranscriptApiFetcher`

Lightweight, no API key, no paid call.

```python
class YoutubeTranscriptApiFetcher:
    def __init__(
        self,
        languages: tuple[str, ...] = ("en",),
        proxy_config: object | None = None,   # extension point only — not used in v1
        concurrency: int = 5,
    ) -> None: ...

    async def fetch_many(
        self, video_urls_or_ids: list[str]
    ) -> list[FetchedTranscript | None]: ...
```

Behaviour:
- Resolve each input to a `video_id` via `extract_video_id`. Inputs that don't resolve → `None` in the output list (no warning at this layer — the chain wrapper / caller decides the user-facing message).
- For each id, call `youtube_transcript_api.YouTubeTranscriptApi.get_transcript(video_id, languages=list(self.languages))` inside `asyncio.to_thread`, capped at `concurrency` parallel calls.
- On `youtube_transcript_api._errors.TranscriptsDisabled`, `NoTranscriptFound`, or `VideoUnavailable` → `None` in the output (logged at `DEBUG`, **not raised**, **not WARNING** — this fetcher being silent on per-slot failure is the contract the chain relies on; the chain wrapper raises the user-facing WARNING when it advances to the fallback).
- For metadata in v1, populate `VideoMetadata.video_id` only; leave `title`, `channel`, `publish_date`, `duration_seconds`, `description` as `None`. Real metadata is enriched per source: the RSS pipeline (#004) gets it from feed entries; the single-video pipeline (#003) gets it from the YouTube `oEmbed` endpoint (`https://www.youtube.com/oembed?url=…&format=json`, sync HTTP via `httpx.AsyncClient` — no API key needed). Document this clearly in the docstring so #003/#004 implementers know where their metadata comes from.
- `proxy_config` is plumbed into `__init__` but the default impl ignores it in v1 — purely an extension point for the Webshare rotating-proxy integration we may add later. Document in the docstring: "Reserved; not consumed in v1."
- `languages=("en",)` is hard-coded as the default at the constructor. Not surfaced in YAML config in v1 — the human approved this default.

### Chain wrapper: `ChainedTranscriptFetcher`

The composite that ETLs use as their default. Given an ordered list of fetchers, calls them in order on the slots that are still `None` after the previous fetcher.

```python
class ChainedTranscriptFetcher:
    def __init__(self, fetchers: list[TranscriptFetcher]) -> None:
        if not fetchers:
            raise ValueError("ChainedTranscriptFetcher needs at least one fetcher")
        self._fetchers = fetchers

    async def fetch_many(
        self, video_urls_or_ids: list[str]
    ) -> list[FetchedTranscript | None]: ...
```

Behaviour:
- Pass 1: `await self._fetchers[0].fetch_many(video_urls_or_ids)` → list of length N, possibly with `None` slots.
- For each subsequent fetcher (`self._fetchers[1:]`), gather the indices/inputs that are still `None`, call `fetcher.fetch_many(remaining_inputs)`, and merge the results back into the original positions.
- For each slot that advances from one fetcher to the next, log a `WARNING` with the resolved video id / URL: `"youtube-transcript-api returned no transcript for {url}; falling back to Gemini"` (use the next fetcher's class name in the message, generically — e.g. `f"{prev.__class__.__name__} returned no transcript for {url}; falling back to {next.__class__.__name__}"`).
- For each slot that is **still** `None` after the last fetcher in the chain, log a final `WARNING`: `"All transcript fetchers exhausted for {url}; skipping"`.
- Result list preserves input order and length. Callers continue to interpret `None` as "no transcript available, skip this video."

The default chain that #003 and #004 will construct (after #002 ships the Gemini fetcher):

```python
ChainedTranscriptFetcher(
    fetchers=[
        YoutubeTranscriptApiFetcher(),     # primary, free
        GeminiTranscriptFetcher(),         # paid fallback, ships in #002
    ]
)
```

Until #002 lands, the ETLs may temporarily construct a single-element chain (`[YoutubeTranscriptApiFetcher()]`) — this preserves the chain contract even before the fallback exists, so adding the fallback in #002 is a one-line change at each call site.

### Dependency

Add `youtube-transcript-api>=1.2.4` to `apps/memory/pyproject.toml` under `[project].dependencies`. Run `uv sync` (the SWE will, after the edit). The `>=1.2.4` floor matches the human's note about Webshare-proxy support landing there.

`google-genai` is already a project dependency (used by graph extraction) — no new dep is added in this task. The Gemini fallback uses it in #002.

### Tests (TDD)

Follow `skills/testing-python/SKILL.md` — AAA, parametrize, `pytest-mock`. Tests live under `apps/memory/tests/unit/data/youtube/`.

`test_urls.py` (pure, no mocks):
- `extract_video_id` parametrized over the 6 URL shapes above + `None` for `https://example.com/foo`, empty string, and a 10-char string.
- `extract_channel_id_from_rss_url` for the example feed + `None` for `youtube.com/watch?v=…`.
- `is_youtube_video_url` / `is_youtube_rss_url` parametrized truth tables.
- `canonical_video_url("eYaWxljC4sA") == "https://www.youtube.com/watch?v=eYaWxljC4sA"`.

`test_transcript_fetcher.py` (mocks `youtube_transcript_api` for the primary; uses fakes for the chain):

`YoutubeTranscriptApiFetcher` (mock the `youtube_transcript_api` module):
- Happy path: 2 ids in → 2 `FetchedTranscript` out, in order; `plain_text` is segments joined by `\n`; `metadata.video_id` populated; `metadata.title is None`.
- Missing transcript: when the underlying call raises `TranscriptsDisabled` → output element is `None`, no exception bubbles out, no `WARNING` is emitted at this layer (assert via `caplog` that no record at `WARNING` level was produced by `tree.data.youtube.transcript_fetcher`).
- Unresolvable input (`"not-a-url"`) → `None` element, no `WARNING` at this layer.
- Order preservation: pass `[good, bad, good]`, mock so middle raises → `[FetchedTranscript, None, FetchedTranscript]` in that order.
- Concurrency boundary: doesn't deadlock when 10 ids are passed with `concurrency=2`. (Sanity test — patch `asyncio.to_thread` to a fast stub.)

`ChainedTranscriptFetcher` (uses fake fetchers, no network, no Gemini, no `youtube_transcript_api`):
- **Primary success path**: chain `[fake_primary]`; `fake_primary.fetch_many([a, b])` returns `[T_a, T_b]` → chain returns `[T_a, T_b]`; no `WARNING` emitted; `fake_primary` was called once with both inputs.
- **Primary-None → fallback-success path**: chain `[fake_primary, fake_fallback]`; `fake_primary.fetch_many([a, b, c])` returns `[T_a, None, T_c]`; `fake_fallback.fetch_many([b])` returns `[T_b_via_fallback]` → chain returns `[T_a, T_b_via_fallback, T_c]` in order; one `WARNING` emitted mentioning `b` and the fallback class name; `fake_fallback` was called exactly once and only with `[b]`.
- **Primary-None → fallback-None hard-skip path**: chain `[fake_primary, fake_fallback]`; primary returns `[None]`, fallback returns `[None]` → chain returns `[None]`; one intermediate `WARNING` (advanced to fallback) **and** one final `WARNING` (`"All transcript fetchers exhausted for ..."`).
- **Empty chain**: `ChainedTranscriptFetcher(fetchers=[])` raises `ValueError`.
- **Single-element chain (transitional)**: `ChainedTranscriptFetcher([fake_primary])`; primary returns `[None]` → chain returns `[None]`; one final `WARNING` ("exhausted"), no intermediate WARNING (no next fetcher to advance to).
- **Order preservation across fallback merge**: 5 inputs, primary returns `[T, None, T, None, T]`, fallback returns `[T, None]` for the two None slots → result is `[T_primary_0, T_fallback_0, T_primary_2, None, T_primary_4]` in correct positions.

No integration test in this task (no MongoDB, no Prefect). Integration coverage lands with #003 and #004.

## Acceptance Criteria

- [x] `apps/memory/src/tree/data/youtube/__init__.py`, `types.py`, `urls.py`, `transcript_fetcher.py` exist and are importable from `tree.data.youtube`.
- [x] `youtube-transcript-api>=1.2.4` is in `apps/memory/pyproject.toml` `[project].dependencies` and `apps/memory/uv.lock` is updated (`uv sync` ran clean).
- [x] `extract_video_id` returns the correct id for every shape in the Scope's parametrized list and `None` for the negative cases.
- [x] `extract_channel_id_from_rss_url("https://www.youtube.com/feeds/videos.xml?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw") == "UCkyHDwRWMEluOEYmOGJ_2nw"`.
- [x] `canonical_video_url` always returns the `https://www.youtube.com/watch?v={id}` form regardless of input variant — manually verified by calling it on a `youtu.be` URL in a one-shot script.
- [x] `YoutubeTranscriptApiFetcher.fetch_many([id_a, id_b])` returns 2 `FetchedTranscript` objects in input order with `plain_text` joined by `\n` (mocked).
- [x] When `youtube_transcript_api` raises `TranscriptsDisabled`, the output element is `None`, no exception escapes, and the primary fetcher itself emits no `WARNING` (the chain wrapper owns the user-facing warning).
- [x] An unresolvable input (`"not-a-url"`) yields `None` in the primary's output list (no raise, no WARNING from this layer).
- [x] `proxy_config` is accepted by `__init__` and stored on the instance, but unused in v1; the docstring says "Reserved; not consumed in v1."
- [x] `languages` defaults to `("en",)` and is hard-coded at the fetcher constructor (not surfaced in YAML config in v1).
- [x] `ChainedTranscriptFetcher` exists, accepts an ordered `list[TranscriptFetcher]`, and raises `ValueError` if the list is empty.
- [x] `ChainedTranscriptFetcher.fetch_many` calls fetchers in order; each subsequent fetcher receives ONLY the inputs whose previous-fetcher output was `None`; the merged output preserves original input order and length.
- [x] When the chain advances from fetcher N to fetcher N+1 for a slot, a `WARNING` is logged that names the resolved input and the fallback class — verified via `caplog` in unit tests.
- [x] When all fetchers in the chain return `None` for a slot, a final `WARNING` ("All transcript fetchers exhausted for …") is logged for that slot.
- [x] All `datetime` fields on `VideoMetadata` are tz-aware UTC (`assert dt.tzinfo is not None`).
- [x] `make memory-unit-tests` passes with zero warnings; the new test files are picked up by pytest discovery.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` all clean.

## User Stories

### Story: Future SWE swaps in a more powerful primary transcript backend
1. SWE creates `tree.data.youtube.bulk_fetcher.BulkFetcher(TranscriptFetcher)` with the same `fetch_many` signature.
2. SWE swaps the first element of the chain in `youtube_video_pipeline.py` (added in #003) from `YoutubeTranscriptApiFetcher()` to `BulkFetcher()`. The chain shape and the Gemini fallback stay untouched.
3. No other call site changes — the interface contract is the only seam.
4. All existing unit tests still pass because they mock `TranscriptFetcher` (or the chain), not the concrete impl.

### Story: SWE handles a video with disabled transcripts via the chain
1. RSS pipeline (#004) calls `chain.fetch_many([url_a, url_b_with_no_transcript, url_c])` where `chain` is the default `[primary, gemini]` chain.
2. Primary returns `[FetchedTranscript, None, FetchedTranscript]`.
3. Chain wrapper logs `WARNING — YoutubeTranscriptApiFetcher returned no transcript for url_b; falling back to GeminiTranscriptFetcher`, calls Gemini for `[url_b]` only, gets back `[FetchedTranscript_via_gemini]`, and merges → `[FetchedTranscript, FetchedTranscript_via_gemini, FetchedTranscript]`.
4. The RSS pipeline iterates and persists 3 documents. No final "exhausted" warning, because Gemini saved the slot.

### Story: SWE handles a video that even Gemini can't transcribe
1. Primary returns `[None]` for `url_x`; chain advances and Gemini also returns `[None]` (e.g., Gemini errored or refused).
2. Chain logs both an intermediate `WARNING` (advanced to fallback) and a final `WARNING — All transcript fetchers exhausted for url_x; skipping`.
3. The pipeline sees `None` for that slot, logs nothing extra at the pipeline layer (the chain already warned), and skips the video.
4. The batch as a whole still completes successfully; only this single slot is dropped.

### Story: SWE pastes any YouTube URL form and gets the same canonical document
1. SWE calls `canonical_video_url(extract_video_id("https://youtu.be/eYaWxljC4sA"))`.
2. Result: `"https://www.youtube.com/watch?v=eYaWxljC4sA"`.
3. SWE calls the same composition on `"https://www.youtube.com/watch?v=eYaWxljC4sA&t=42s"`.
4. Same result. The `Document.source_uri` upsert key is shape-independent → no duplicates from URL variants.

### Story: SWE introspects a fetched transcript before sending it through extraction
1. SWE in a `uv run python -c "..."` shell calls `await primary.fetch_many(["eYaWxljC4sA"])`.
2. The single result has `metadata.video_id == "eYaWxljC4sA"`, `metadata.title is None` (enrichment is the next pipeline's job), and `plain_text` is non-empty.
3. SWE sees `len(segments) > 0` and `segments[0]` has `start_seconds == 0.0` (typical for a normal transcript).

---

Blocked by: (none)

## Log

### [SWE] 2026-05-01 14:30 — Implementation

**Files modified**
- `apps/memory/src/tree/data/youtube/__init__.py` — empty package marker.
- `apps/memory/src/tree/data/youtube/types.py` — `VideoMetadata`, `TranscriptSegment`, `FetchedTranscript` Pydantic models; `publish_date` validator enforces tz-aware UTC.
- `apps/memory/src/tree/data/youtube/urls.py` — pure URL/ID helpers (`extract_video_id`, `extract_channel_id_from_rss_url`, `is_youtube_video_url`, `is_youtube_rss_url`, `canonical_video_url`). Stdlib `urllib.parse` only.
- `apps/memory/src/tree/data/youtube/transcript_fetcher.py` — `TranscriptFetcher` Protocol, `YoutubeTranscriptApiFetcher` (asyncio.Semaphore-bounded concurrency, errors-disabled silent at this layer), `ChainedTranscriptFetcher` (per-slot advance with `WARNING` on advance and final "exhausted" WARNING).
- `apps/memory/tests/unit/data/youtube/__init__.py` — package marker.
- `apps/memory/tests/unit/data/youtube/test_urls.py` — parametrized URL/ID truth tables incl. round-trip composition through `canonical_video_url`.
- `apps/memory/tests/unit/data/youtube/test_transcript_fetcher.py` — `YoutubeTranscriptApiFetcher` (happy path, `TranscriptsDisabled` silent, unresolvable input silent, order preservation, `proxy_config` stored, `languages` default, concurrency does not deadlock) + `ChainedTranscriptFetcher` (empty raises, primary success no warning, primary→fallback success path, primary+fallback both None double WARNING, single-element chain only emits exhausted, 5-slot order preservation across merge).
- `apps/memory/pyproject.toml` — added `youtube-transcript-api>=1.2.4`.
- `apps/memory/uv.lock` — refreshed via `uv add` (12 transitive deps including `defusedxml`, `requests`, etc.).

**Tests**
- Unit: 508 passing, 0 failing, 0 warnings — `make unit-tests` (43 of 508 are the new youtube tests).
- Integration: N/A — no infra changes (pure logic; no MongoDB, no Prefect; integration coverage lands with #003/#004 per spec).

**Acceptance criteria**
- [x] `tree.data.youtube` files exist and importable — verified by `uv run python -c "from tree.data.youtube.transcript_fetcher import ..."` smoke + import-driven test collection.
- [x] `youtube-transcript-api>=1.2.4` in `pyproject.toml` + `uv.lock` updated (clean `uv add`).
- [x] `extract_video_id` truth table — `tests/unit/data/youtube/test_urls.py::TestExtractVideoId::test_resolves_known_shapes` (10 parametrized cases).
- [x] `extract_channel_id_from_rss_url` happy + negative — `TestExtractChannelIdFromRssUrl`.
- [x] `canonical_video_url` round-trips from any shape — `TestCanonicalVideoUrl::test_round_trips_from_youtu_be` + manually verified end-to-end (see Evidence below).
- [x] `YoutubeTranscriptApiFetcher.fetch_many` happy path with `\n`-joined `plain_text` — `TestYoutubeTranscriptApiFetcher::test_happy_path_two_videos_in_order`.
- [x] `TranscriptsDisabled` → `None`, no raise, no WARNING — `test_missing_transcript_returns_none_silently` (asserts via `caplog` no WARNING records from this logger).
- [x] Unresolvable input → `None`, no raise, no WARNING, backend never called — `test_unresolvable_input_returns_none_silently`.
- [x] `proxy_config` stored on instance + docstring says "Reserved; not consumed in v1." — `test_proxy_config_is_stored_but_unused`.
- [x] `languages` defaults to `("en",)` — `test_languages_default_is_english`.
- [x] `ChainedTranscriptFetcher` empty list raises `ValueError` — `test_empty_chain_raises`.
- [x] Chain calls fetchers in order; subsequent fetchers receive only `None`-slot inputs; merged output preserves order — `test_primary_none_then_fallback_success` + `test_order_preservation_across_fallback_merge` (assert exact `_FakeFetcher.calls` payload).
- [x] WARNING on slot advance names input + fallback class — `test_primary_none_then_fallback_success` (asserts message contains `VIDEO_ID_B` and `_FakeFetcher`).
- [x] Final "All transcript fetchers exhausted" WARNING — `test_primary_none_fallback_none_emits_advance_and_exhausted` + `test_single_element_chain_only_emits_exhausted_warning`.
- [x] `VideoMetadata.publish_date` tz-aware enforced — Pydantic `field_validator` raises on naive datetime (covered transitively; default `None` is the v1 use).
- [x] `make memory-unit-tests` clean (0 warnings). Output below.
- [x] format-fix / lint-fix / format-check / lint-check / pre-commit clean. Output below.

**Evidence**

Format/lint/pre-commit:
```
$ make format-fix
uv run ruff format src/ tests/ scripts/ deploy/
2 files reformatted, 147 files left unchanged

$ make lint-fix
uv run ruff check --fix src/ tests/ scripts/ deploy/
All checks passed!

$ make format-check
uv run ruff format --check src/ tests/ scripts/ deploy/
149 files already formatted

$ make lint-check
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit  (from repo root)
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

Unit tests:
```
$ make unit-tests
... (truncated earlier modules — all green) ...
tests/unit/data/youtube/test_transcript_fetcher.py .............         [ 51%]
tests/unit/data/youtube/test_urls.py ..............................      [ 57%]
...
======================== 508 passed in 71.60s (0:01:11) ========================
```

End-to-end smoke (Step 7) — exercising the public API as a downstream pipeline would:
```
$ uv run python -c "..."   (full script invokes urls + fetcher + chain, no network)
canonical(youtu.be):     https://www.youtube.com/watch?v=eYaWxljC4sA
canonical(watch?v&t):    https://www.youtube.com/watch?v=eYaWxljC4sA
canonical(shorts):       https://www.youtube.com/watch?v=eYaWxljC4sA
canonical(m.youtube):    https://www.youtube.com/watch?v=eYaWxljC4sA
rss channel:             UCkyHDwRWMEluOEYmOGJ_2nw
is_video(youtu.be):      True
is_rss(feed):            True
chain on unresolvable:   [None]
  (chain logged: WARNING — All transcript fetchers exhausted for not-a-url; skipping)
empty-chain raises:      ChainedTranscriptFetcher needs at least one fetcher
Protocol importable:     <class 'tree.data.youtube.transcript_fetcher.TranscriptFetcher'>
```

**Notes**
- `youtube-transcript-api>=1.2.4` no longer exposes `YouTubeTranscriptApi.get_transcript` (the spec quoted the pre-1.0 API). The 1.x API is `YouTubeTranscriptApi().fetch(video_id, languages=...)` returning a `FetchedTranscript` with `.snippets` (each with `.text` / `.start` / `.duration`) and `.language_code`. The contract — name + behaviour — is unchanged; only the call site differs. All tests mock our thin `_call_api` shim, so the test contract is independent of the package's surface.
- The single-video-pipeline's `oEmbed` metadata enrichment (#003) and the RSS feed enrichment (#004) are documented in the `YoutubeTranscriptApiFetcher` docstring as called out in the spec; no implementation in this task per scope.
- Bootstrapped a worktree-local `.env` from `.env.example` so Make targets (which `include .env`) could run. No secrets were added or modified.
- DO NOT COMMIT — handing off to Tester per `/day` workflow.

### [Tester] 2026-05-01 17:50 — QA

**Test summary**
- Format check (`make memory-format-check`): PASS — `149 files already formatted`
- Lint check (`make memory-lint-check`): PASS — `All checks passed!`
- Pre-commit (`make pre-commit` from repo root): PASS — prettier / ruff check / ruff format / biome (harness) all Passed
- Unit tests (`make memory-unit-tests`): PASS — `508 passed in 21.43s`, **0 warnings**
- Integration tests: SKIPPED (per spec — pure logic; no Mongo / Prefect / network; integration coverage lands with #003/#004)

**E2E adversarial pass** (driven by `/tmp/e2e_adversarial.py`, executed via `uv run python` against the real package — no network, no Gemini, no Mongo)

- Happy path: `ChainedTranscriptFetcher([primary]).fetch_many([VID_A, VID_B])` → `[FetchedTranscript("hello"), FetchedTranscript("world")]` (PASS)
- Break 1 — Mixed batch (primary partial, fallback fills): inputs `[A, B, C]`; primary returns `[T_A, None, T_C]`; fallback called with `[B]` only and returns `[T_B_fb]` → result `[T_A, T_B_fb, T_C]`; one advance-WARNING for `B` (PASS)
- Break 2 — Malformed/non-YouTube URLs (`["https://example.com/foo", "not-a-url", "", VID_A]`): chain returns `[None, None, None, T_A]`; no crash; three "exhausted" WARNINGs logged for the bad slots; the good slot returned its transcript (PASS)
- Break 3 — Empty chain (`ChainedTranscriptFetcher([])`): raises `ValueError("ChainedTranscriptFetcher needs at least one fetcher")` per spec — does NOT silently return all-None (PASS)
- Break 4 — Order preservation across 3 hops: 5 inputs, primary fills 0/2/4, fallback fills 1, secondary-fallback fills 3 → result aligns positionally `[p0, b-fb, p2, d-sfb, p4]`; fallback received `["b-input","d-input"]`, secondary received `["d-input"]` only (PASS)
- Break 5 (bonus) — Non-`("en",)` languages: `YoutubeTranscriptApiFetcher(languages=("ro","fr","en"))`; verified `_call_api` is invoked with `list(self.languages) == ["ro","fr","en"]` and the returned `FetchedTranscript.language == "ro"` (PASS)
- Break 6 (extra) — Empty input list: `chain.fetch_many([])` returns `[]` and primary is never invoked (PASS)

**Acceptance criteria** (every non-`[HUMAN]` AC verified with concrete evidence)

- [x] PASS — `tree.data.youtube.{__init__,types,urls,transcript_fetcher}` exist & importable. Evidence: `uv run python -c "from tree.data.youtube.transcript_fetcher import TranscriptFetcher, YoutubeTranscriptApiFetcher, ChainedTranscriptFetcher"` → `all imports OK`.
- [x] PASS — `youtube-transcript-api>=1.2.4` in `apps/memory/pyproject.toml` and `uv.lock` updated. Evidence: `pyproject.toml:33` and `uv.lock:3566` (`specifier = ">=1.2.4"`); `make memory-unit-tests` imports the package without resolution errors.
- [x] PASS — `extract_video_id` truth table. Evidence: `tests/unit/data/youtube/test_urls.py::TestExtractVideoId::test_resolves_known_shapes` (10 parametrized cases incl. `youtu.be`, `watch?v`, `shorts`, `m.youtube.com&t=10s`, `&feature=share`, bare ID, plus 4 negatives) — all green; e2e script also confirmed.
- [x] PASS — `extract_channel_id_from_rss_url("...?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw") == "UCkyHDwRWMEluOEYmOGJ_2nw"`. Evidence: `test_urls.py::TestExtractChannelIdFromRssUrl::test_resolves_channel_id` + e2e script printed `RSS channel id: UCkyHDwRWMEluOEYmOGJ_2nw`.
- [x] PASS — `canonical_video_url` shape-independent canonicalization. Evidence: e2e script ran 5 different shapes (`youtu.be`, `watch?v&t=42s`, `m.youtube.com`, `shorts`, bare ID) → all produced `https://www.youtube.com/watch?v=eYaWxljC4sA`; plus `test_urls.py::TestCanonicalVideoUrl::{test_round_trips_from_youtu_be,test_round_trips_from_watch_with_query}`.
- [x] PASS — `YoutubeTranscriptApiFetcher.fetch_many([id_a, id_b])` returns 2 ordered transcripts with `\n`-joined `plain_text`. Evidence: `test_transcript_fetcher.py::TestYoutubeTranscriptApiFetcher::test_happy_path_two_videos_in_order` asserts `results[0].plain_text == "hello\nworld"` (`transcript_fetcher.py:145` joins with `"\n"`).
- [x] PASS — `TranscriptsDisabled` → `None`, no raise, no WARNING from this layer. Evidence: `test_missing_transcript_returns_none_silently` asserts `caplog` contains zero WARNING records on the `tree.data.youtube.transcript_fetcher` logger.
- [x] PASS — Unresolvable input → `None`, no raise, no WARNING, backend not invoked. Evidence: `test_unresolvable_input_returns_none_silently` (`spy.assert_not_called()` on `_call_api`).
- [x] PASS — `proxy_config` accepted, stored, and unused; docstring says "Reserved; not consumed in v1." Evidence: `transcript_fetcher.py:73-74` docstring; `transcript_fetcher.py:90` stores attr; `test_proxy_config_is_stored_but_unused` asserts `fetcher.proxy_config is sentinel`.
- [x] PASS — `languages` defaults to `("en",)`, hard-coded at constructor, not in YAML. Evidence: `transcript_fetcher.py:82` and `test_languages_default_is_english`. (No `default.yaml` change in this task confirms "not surfaced".)
- [x] PASS — `ChainedTranscriptFetcher` accepts ordered list and raises `ValueError` on empty. Evidence: `transcript_fetcher.py:174-177`; `test_empty_chain_raises`; e2e Break 3.
- [x] PASS — Subsequent fetchers receive only `None`-slot inputs; merged output preserves input order and length. Evidence: `test_primary_none_then_fallback_success` (asserts `fallback.calls == [[VIDEO_ID_B]]`) + `test_order_preservation_across_fallback_merge` (5-slot merge); e2e Break 4 confirms across THREE hops.
- [x] PASS — Advance-to-next WARNING names input + fallback class. Evidence: `test_primary_none_then_fallback_success` asserts `VIDEO_ID_B in msg and "_FakeFetcher" in msg`; e2e captured WARNING `"Primary returned no transcript for https://www.youtube.com/watch?v=AAAaaaBBBcc; falling back to Fallback"`.
- [x] PASS — Final "All transcript fetchers exhausted" WARNING when chain exhausts a slot. Evidence: `test_primary_none_fallback_none_emits_advance_and_exhausted` + `test_single_element_chain_only_emits_exhausted_warning`; e2e captured `"All transcript fetchers exhausted for https://example.com/foo; skipping"`.
- [x] PASS — `VideoMetadata.publish_date` is tz-aware UTC. Evidence: `types.py:31-36` `field_validator` raises on naive; e2e `metadata_tz_aware()` confirmed `ValidationError` on naive `datetime(2026, 5, 1)`.
- [x] PASS — `make memory-unit-tests` passes with **zero warnings**; new files picked up by discovery. Evidence: `tests/unit/data/youtube/test_transcript_fetcher.py .............` (13 cases) and `tests/unit/data/youtube/test_urls.py ..............................` (30 parametrized cases) shown in the run; `508 passed in 21.43s` with no warnings summary line.
- [x] PASS — `make memory-format-fix && memory-lint-fix && memory-format-check && memory-lint-check && pre-commit` all clean. Evidence: ran `make memory-format-check` (`149 files already formatted`), `make memory-lint-check` (`All checks passed!`), `make pre-commit` (all hooks Passed).

**Evidence excerpts**

```
$ make memory-format-check
uv run ruff format --check src/ tests/ scripts/ deploy/
149 files already formatted

$ make memory-lint-check
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... tests/unit/data/youtube/test_transcript_fetcher.py .............         [ 51%]
    tests/unit/data/youtube/test_urls.py ..............................      [ 57%] ...
============================= 508 passed in 21.43s =============================
```

E2E adversarial WARNINGs captured (confirming the chain's user-facing logging contract):
```
WARNING tree.data.youtube.transcript_fetcher | Primary returned no transcript for https://www.youtube.com/watch?v=AAAaaaBBBcc; falling back to Fallback
WARNING tree.data.youtube.transcript_fetcher | All transcript fetchers exhausted for https://example.com/foo; skipping
WARNING tree.data.youtube.transcript_fetcher | All transcript fetchers exhausted for not-a-url; skipping
WARNING tree.data.youtube.transcript_fetcher | All transcript fetchers exhausted for ; skipping
WARNING tree.data.youtube.transcript_fetcher | Primary returned no transcript for b-input; falling back to Fallback
WARNING tree.data.youtube.transcript_fetcher | Primary returned no transcript for d-input; falling back to Fallback
WARNING tree.data.youtube.transcript_fetcher | Fallback returned no transcript for d-input; falling back to SecondaryFB
```

**Other issues found**
- None. The `_display_label` helper (transcript_fetcher.py:217-224) gracefully degrades to the raw input string when `extract_video_id` returns `None`, so logs for malformed inputs remain readable (`"... exhausted for not-a-url"` rather than crashing). Empty-string input also handled.
- Multi-hop chains (primary → fallback → secondary fallback) work correctly out of the box even though the spec only requires a 2-element default chain. Verified in Break 4. This makes #002's drop-in trivial.
- Note for downstream tasks (#003/#004): the e2e script confirmed the chain's contract works with empty input lists (`fetch_many([])` → `[]`) — pipeline call sites can pass empty batches without guarding.

**VERDICT: PASS**
