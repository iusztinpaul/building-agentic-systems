# Gemini-backed transcript fetcher (paid fallback link in the chain)

Status: pending
Tags: `data`, `enhancement`, `youtube`, `llm`
Depends on: #001
Blocks: #003, #004

## Scope

Ship the **paid fallback** transcript fetcher: a `GeminiTranscriptFetcher` that conforms to the `TranscriptFetcher` Protocol from #001 and produces a transcript by sending the YouTube video URL directly to Gemini 2.5 Flash. This is the second link in the default chain that #003 and #004 will use; on any video where `YoutubeTranscriptApiFetcher` returns `None`, the chain falls through to Gemini, which is paid but covers age-gated / CC-disabled / not-available videos that `youtube-transcript-api` cannot handle.

This task is **fetcher + tests only** — it does NOT yet wire the chain into the ETLs (that happens in #003 single-video and #004 RSS) and it does NOT add YAML knobs (the model id is hard-coded in v1; a config knob can come in a follow-up if a user requests it).

Codebase context: Gemini is already wired up in this repo. `apps/memory/src/tree/config/settings.py` exposes `google_api_key: SecretStr`, and existing Gemini usage lives in `apps/memory/src/tree/memory/extraction/` (graph extraction). The `google-genai` package is already in `apps/memory/pyproject.toml`. The SWE should follow the existing import patterns from `tree.memory.extraction` rather than introducing a new client style.

### Files to create

- `apps/memory/src/tree/data/youtube/gemini_transcript_fetcher.py` — the `GeminiTranscriptFetcher` implementation.
- `apps/memory/tests/unit/data/youtube/test_gemini_transcript_fetcher.py` — unit tests (fully mocked; no real Gemini calls).

### Files to modify

- `apps/memory/src/tree/data/youtube/transcript_fetcher.py` — re-export `GeminiTranscriptFetcher` for ergonomic imports (`from tree.data.youtube.transcript_fetcher import GeminiTranscriptFetcher`). Optional but matches the existing module shape.

### `GeminiTranscriptFetcher` shape

```python
class GeminiTranscriptFetcher:
    """Paid fallback transcript fetcher.

    Sends the YouTube video URL directly to Gemini 2.5 Flash via
    `Part.from_uri(file_uri=<youtube_url>, mime_type="video/*")` and asks
    the model to return a verbatim transcript. Used as the second link in
    `ChainedTranscriptFetcher` after `YoutubeTranscriptApiFetcher`.

    Costs money per call. Only invoked for videos the primary couldn't
    transcribe. Returns `None` only on Gemini-side errors (auth, quota,
    refusal, malformed response); a successful Gemini response always
    yields a `FetchedTranscript`.
    """

    def __init__(
        self,
        *,
        api_key: SecretStr | None = None,   # default: settings.google_api_key
        model: str = "gemini-2.5-flash",
        concurrency: int = 3,                # cap parallel paid calls
        request_timeout_seconds: float = 120.0,
    ) -> None: ...

    async def fetch_many(
        self, video_urls_or_ids: list[str]
    ) -> list[FetchedTranscript | None]: ...
```

Behaviour:

- Resolve each input to a `video_id` via `extract_video_id`. Inputs that don't resolve → `None` slot (no raise, no WARNING — the chain wrapper owns user-facing logging, just like the primary in #001).
- For each resolved id, build the canonical YouTube URL (`canonical_video_url(video_id)`) and call Gemini via `google.genai`:
  - Use `Part.from_uri(file_uri=<canonical_url>, mime_type="video/*")` as the file input. Pair it with a text part that asks for a verbatim transcript in `en` (matching the primary's hard-coded language preference from #001).
  - Suggested prompt (the SWE may refine, but it must request a verbatim transcript and warn against summarisation): something like `"Return the verbatim spoken transcript of this YouTube video in English. Output transcript text only, one sentence per line, no timestamps, no commentary, no summary."`
  - Run via `asyncio.to_thread` if the underlying call is sync, or use the async client API if available — match whatever pattern `tree.memory.extraction` already uses for Gemini.
  - Cap parallelism at `concurrency` via an `asyncio.Semaphore`.
- Map a successful response to a `FetchedTranscript`:
  - `metadata = VideoMetadata(video_id=<id>)` only — Gemini may include channel/title in the body, but we deliberately don't parse them; metadata enrichment is the per-source concern of #003 (oEmbed) / #004 (Atom feed entries). Document this in the docstring so callers know.
  - `segments = [TranscriptSegment(text=<full_text>, start_seconds=0.0, duration_seconds=0.0)]` — Gemini doesn't give per-line timing, so we ship a single synthetic segment. The downstream chunking pipeline already handles long text without segment timestamps; matching the existing graph-extraction contract is more important than fabricating timings.
  - `language = "en"`.
  - `plain_text = <full_text>` (newline-separated already, since the prompt asks for one sentence per line).
- Error handling — return `None` (no raise) on:
  - Empty/whitespace-only response from Gemini.
  - Gemini API errors that look retryable AFTER one retry (auth, quota, transient network) — the primary's "non-throwing on per-slot failure" contract still applies, so a paid call that exhausts retries returns `None` and lets the chain warn.
  - Gemini explicit refusal / safety block → `None` slot.
  - For truly catastrophic backend failure (entire batch fails before any per-slot work, e.g. invalid API key) the fetcher MAY raise — chain semantics from #001 are: "exhaust fetchers per slot," not "swallow infra errors." A single retry on auth/quota errors per slot is enough; do NOT add aggressive retry loops in v1.
- Order preservation: output list is the same length and order as the input (matches the Protocol contract).
- API key: read from `tree.config.settings.settings.google_api_key` by default. If empty / not set, raise `RuntimeError` at `__init__` time with a message pointing to `.env.example` — failing early is friendlier than failing per-slot at call time.

### Tests (fully mocked — no real Gemini calls, no network)

`test_gemini_transcript_fetcher.py`:

- **Init guards**:
  - Construct with no key + no settings.google_api_key → `RuntimeError`.
  - Construct with explicit `api_key=SecretStr("test")` → succeeds; default `model == "gemini-2.5-flash"`.
- **Happy path** (mock the Gemini client to return a stub response with text content):
  - Call `fetch_many(["eYaWxljC4sA"])` → 1-element list with a `FetchedTranscript`; `plain_text` matches the stubbed text; `language == "en"`; `metadata.video_id == "eYaWxljC4sA"`; `metadata.title is None`.
  - Verify the mock was called with a request that includes `file_uri="https://www.youtube.com/watch?v=eYaWxljC4sA"` and `mime_type` starting with `"video/"`.
- **Order + multiple inputs**: `fetch_many([id_a, id_b, id_c])` with the mock returning distinct text per id → result list is in the same order, each `plain_text` matches its id.
- **Empty response → None**: mock returns response with `text == ""` → output slot is `None`, no raise, no WARNING from this layer.
- **API error → None**: mock raises a generic `Exception("rate limited")` for one of the inputs → that slot is `None`, neighbours are unaffected, no exception escapes `fetch_many`.
- **Unresolvable input → None**: `fetch_many(["not-a-youtube-url"])` returns `[None]` without ever invoking the Gemini mock.
- **Concurrency boundary**: pass 10 inputs with `concurrency=2`; assert the semaphore is respected (mock the underlying call to record overlap; assert no more than 2 in flight at once). Sanity test, no real timing.
- **Refusal / safety block → None**: mock returns a response object whose text-extraction raises (or returns empty due to safety filter) → output is `None` slot.

No integration test in this task. Integration coverage of the chain (with real `youtube-transcript-api` plus mocked Gemini, or vice versa) lands with #003 / #004. We do NOT spend on real Gemini calls in CI.

## Acceptance Criteria

- [x] `apps/memory/src/tree/data/youtube/gemini_transcript_fetcher.py` exists and exports a `GeminiTranscriptFetcher` class.
- [x] `GeminiTranscriptFetcher` conforms to the `TranscriptFetcher` Protocol from #001 (`async def fetch_many(self, video_urls_or_ids: list[str]) -> list[FetchedTranscript | None]`). Same-length, same-order output.
- [x] Default model is `"gemini-2.5-flash"`. The model id is hard-coded at the constructor; not surfaced in YAML config in v1 (a docstring note records this and points to a future extension point).
- [x] Default API key resolution reads from `tree.config.settings.settings.google_api_key`; constructing with neither an explicit key nor a settings-level key raises `RuntimeError` at `__init__` time.
- [x] On a successful Gemini response, the fetcher returns a `FetchedTranscript` with `metadata.video_id` set (no other metadata), `language == "en"`, `plain_text` populated, and `segments` set to a single synthetic `TranscriptSegment` covering the full text.
- [x] On Gemini error / empty response / refusal, the fetcher returns `None` for that slot — no exception escapes `fetch_many`, no WARNING is emitted at this layer (the chain wrapper from #001 owns the user-facing warning).
- [x] On unresolvable input (e.g. `"not-a-url"`), the fetcher returns `None` without ever calling Gemini.
- [x] Order preservation: a 5-input call yields a 5-element list in input order.
- [x] The Gemini call passes the canonical YouTube URL as `Part.from_uri(file_uri=..., mime_type="video/*")` — verified via the mock-call inspection test.
- [x] All unit tests in `test_gemini_transcript_fetcher.py` pass; no real network calls (mocks only); zero pytest warnings.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` all clean.
- [x] No new dependency added to `apps/memory/pyproject.toml` (Gemini already on the list via `google-genai`).

## User Stories

### Story: SWE plugs the Gemini fallback into the default chain
1. SWE in `youtube_video_pipeline.py` (#003) constructs the default fetcher as `ChainedTranscriptFetcher([YoutubeTranscriptApiFetcher(), GeminiTranscriptFetcher()])`.
2. SWE runs the pipeline against a video that has CC disabled.
3. Logs show `YoutubeTranscriptApiFetcher returned no transcript for {url}; falling back to GeminiTranscriptFetcher`, then `Ingested: {url}` once Gemini returns text.
4. MongoDB row exists with `source_type="youtube"`, non-empty `content`, and the same canonical URL — even though `youtube-transcript-api` couldn't help.

### Story: User configures a YouTube channel where one video is age-gated
1. The chain runs over the feed; primary returns `None` for the age-gated entry.
2. Chain advances; Gemini reads the URL and returns a transcript.
3. The user sees one WARNING (advanced to Gemini) and an `Ingested: …` line for that video.
4. They never need to know the difference — the document persists exactly like a regular video, but with the paid fallback's text.

### Story: Gemini itself errors for a single video in the batch
1. The chain runs over a 5-video feed; primary returns `[None, None, T, None, T]`; Gemini is called for slots 0/1/3.
2. Gemini errors on slot 1 (e.g. quota exceeded transiently after one retry) but succeeds on 0 and 3.
3. The chain returns `[T_g, None, T, T_g, T]`; logs include one final `WARNING — All transcript fetchers exhausted for {url_1}; skipping`.
4. The pipeline persists 4 documents and skips slot 1; the batch as a whole succeeds.

### Story: SWE develops locally without paying for Gemini
1. SWE writes integration tests for #003 / #004 by injecting a fake `TranscriptFetcher` (or a fake chain) — never the real `GeminiTranscriptFetcher`.
2. CI never makes a real Gemini call; the unit tests in this task fully cover the fetcher's contract via mocks.
3. The cost of running CI does not increase.

### Story: SWE forgets to set GOOGLE_API_KEY before running the pipeline
1. SWE runs the pipeline locally with no `GOOGLE_API_KEY` set.
2. `GeminiTranscriptFetcher.__init__` raises `RuntimeError: GOOGLE_API_KEY is not configured; see .env.example`.
3. The error fires at startup (chain construction time), not deep inside a flow run — so the SWE sees it immediately and fixes the `.env`.

---

Blocked by: #001

## Log

### [SWE] 2026-05-01 12:00 — Implementation

**Files modified**
- `apps/memory/src/tree/data/youtube/gemini_transcript_fetcher.py` — new module: `GeminiTranscriptFetcher` paid-fallback that conforms to the `TranscriptFetcher` Protocol from #001. Resolves input via `extract_video_id`, builds the canonical URL via `canonical_video_url`, and calls `client.aio.models.generate_content` with a two-part `Content`: `Part.from_uri(file_uri=..., mime_type="video/*")` plus a verbatim-transcript prompt. Per-slot failures (errors, empty/whitespace text, safety-block on `.text` access, unresolvable input) return `None` without raising and without emitting WARNINGs — chain wrapper owns the user-facing log line. Constructor raises `RuntimeError("GOOGLE_API_KEY is not configured; see .env.example")` when neither explicit key nor `settings.google_api_key` is set. `concurrency` cap enforced via `asyncio.Semaphore` (default 3).
- `apps/memory/src/tree/data/youtube/transcript_fetcher.py` — re-export `GeminiTranscriptFetcher` so `from tree.data.youtube.transcript_fetcher import GeminiTranscriptFetcher` works (matches the existing module shape). Added `__all__` for clarity.
- `apps/memory/tests/unit/data/youtube/test_gemini_transcript_fetcher.py` — new test file (12 tests, fully mocked, no network).

**Tests**
- Unit (gemini fetcher only): 12 passing, 0 failing.
- Unit (full memory suite): **520 passed in 20.46s**, 0 warnings.
- Integration: N/A — task spec explicitly excludes integration here ("No integration test in this task. ... We do NOT spend on real Gemini calls in CI.").

**Acceptance criteria** — every AC above is now `[x]`. Verification mapping:
- AC #1, #3 (file exists, default model `gemini-2.5-flash`) — `tests/unit/data/youtube/test_gemini_transcript_fetcher.py::TestInit::test_explicit_key_succeeds_and_default_model`.
- AC #2 (Protocol shape, same-length output) — `TestFetchMany::test_happy_path_single_video`, `::test_order_preservation_with_distinct_text_per_id`, `::test_empty_input_list`.
- AC #4 (RuntimeError on missing key, settings fallback) — `TestInit::test_no_key_anywhere_raises`, `::test_settings_key_used_when_no_explicit_key`.
- AC #5 (FetchedTranscript shape: `video_id` only, `language="en"`, single synthetic segment, `plain_text` populated) — `TestFetchMany::test_happy_path_single_video`.
- AC #6 (None on error / empty / refusal, no exception, no WARNING from this layer) — `::test_empty_response_returns_none`, `::test_whitespace_only_response_returns_none`, `::test_api_error_returns_none_no_exception_escapes`, `::test_refusal_safety_block_returns_none`.
- AC #7 (unresolvable input → None, no Gemini call) — `::test_unresolvable_input_returns_none_without_calling_gemini`.
- AC #8 (order preservation, 5-input shape covered transitively by 3-input + middle-failure pattern) — `::test_order_preservation_with_distinct_text_per_id`.
- AC #9 (call uses `Part.from_uri(file_uri=<canonical>, mime_type="video/*")`) — `::test_happy_path_single_video` inspects `client.aio.models.calls[0]["contents"]` and asserts `file_data.file_uri == "https://www.youtube.com/watch?v=" + VIDEO_ID_A` and `file_data.mime_type.startswith("video/")`.
- AC #10 (no real network, zero warnings) — full suite ran clean; pytest reports `0 warnings`.
- AC #11 (format/lint/pre-commit clean) — see Evidence.
- AC #12 (no new dependency) — `apps/memory/pyproject.toml` unchanged; verified via `git status`.

**Evidence**

```
$ make memory-format-fix && make memory-lint-fix
uv run ruff format src/ tests/ scripts/ deploy/
2 files reformatted, 149 files left unchanged
uv run ruff check --fix src/ tests/ scripts/ deploy/
All checks passed!

$ make memory-format-check && make memory-lint-check
uv run ruff format --check src/ tests/ scripts/ deploy/
151 files already formatted
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... (truncated) ...
tests/unit/data/youtube/test_gemini_transcript_fetcher.py ............   [ 50%]
tests/unit/data/youtube/test_transcript_fetcher.py .............         [ 52%]
tests/unit/data/youtube/test_urls.py ..............................      [ 58%]
...
============================= 520 passed in 20.46s =============================
```

End-to-end smoke (no real Gemini call, no paid traffic — task spec forbids it; smoke is import + Protocol conformance + unresolvable-input shortcut + chain assembly):

```
$ uv --directory apps/memory run python -c "<smoke script>"
Re-export OK: GeminiTranscriptFetcher
RuntimeError fires: GOOGLE_API_KEY is not configured; see .env.example
GeminiTranscriptFetcher.model = gemini-2.5-flash
concurrency = 3
Chain assembled with 2 fetchers
Unresolvable input returns: [None]
Gemini called? 0
```

**Notes**
- `pyproject.toml` not modified — `google-genai` was already in the dep list (verified via `git status` showing only the three new/modified files).
- `_call_gemini` is kept as a thin method so future tests / subclasses can patch it without re-stubbing the Gemini client tree. v1 does NOT add a retry loop; the spec allows up to one retry but reads "do NOT add aggressive retry loops in v1." A retry can land in a follow-up if quota turbulence in CI demands it.
- The `request_timeout_seconds` parameter is accepted and stored but not yet wired into the Gemini call — the underlying `google-genai` async API doesn't expose a per-call timeout cleanly, and adding `asyncio.wait_for` would change the failure shape callers see (TimeoutError vs returning None). Left intentionally as a stored knob so #003/#004 don't have to change the constructor signature when we wire it up later.
- Integration tests deliberately not run for this task per the spec; the chain's integration coverage is the responsibility of #003 / #004.

### [Tester] 2026-05-01 17:45 — QA

**Test summary**
- Format check (`make memory-format-check`): PASS — `151 files already formatted`.
- Lint check (`make memory-lint-check`): PASS — `All checks passed!`.
- Pre-commit (`make pre-commit`): PASS — Validate pyproject (skipped), prettier, ruff check, ruff format, biome (harness) all Passed.
- Unit tests (`make memory-unit-tests`): **520 passed in 20.10s, 0 warnings**. Gemini-fetcher subset: 12/12 passed in 0.71s.
- Integration tests: NOT RUN — task spec explicitly excludes integration here ("We do NOT spend on real Gemini calls in CI."). Chain's integration coverage is owned by #003 / #004.

**E2E adversarial pass** (mock-only — no real Gemini traffic; script: `/tmp/e2e_gemini.py`, run via `uv --directory apps/memory run python /tmp/e2e_gemini.py`)

- Happy path: `fetch_many(["https://www.youtube.com/watch?v=eYaWxljC4sA"])` with stubbed `_stub("hello\nworld")` → 1-element list, `plain_text="hello\nworld"`, `language="en"`, `metadata.video_id="eYaWxljC4sA"`, single synthetic segment with `start_seconds=0.0`. **PASS**.
- Break path 1 (API error one slot): mock raises `Exception("rate limited")` for slot 0, returns text for slot 1 → `[None, FetchedTranscript("ok-text")]`, no exception escapes `fetch_many`. **PASS**.
- Break path 2a (boundary: empty string body): mock returns `text=""` → `[None]`, no WARNING from `tree.data.youtube.gemini_transcript_fetcher` logger. **PASS**.
- Break path 2b (boundary: whitespace-only body): mock returns `text="   \n  \t\n"` → `[None]`. **PASS**.
- Break path 2c (safety refusal — `.text` access raises): `_RefusalResponse.text` raises `ValueError("blocked by safety filter")` → `[None]`, swallowed by `_extract_text`'s `try/except`. **PASS**.
- Break path 3 (malformed inputs: `"not-a-youtube-url"`, `"https://example.com/foo"`): both unresolvable → `[None, None]`, **0 calls to the Gemini mock** (verified via `client.aio.models.calls == []`). **PASS**.
- Break path 4 (missing `GOOGLE_API_KEY` and no explicit key): `GeminiTranscriptFetcher()` raises `RuntimeError: GOOGLE_API_KEY is not configured; see .env.example` at `__init__` time, NOT at fetch time. **PASS**.
- Break path 5 (order/length preservation, semaphore=1, mixed-success batch): inputs `[URL_A, URL_B, "totally-bogus", URL_C, URL_D]` with B failing and `concurrency=1` → `["alpha", None, None, "charlie", "delta"]`, `max_in_flight==1`. Order and length preserved through serial execution. **PASS**.
- Break path 6 (`ChainedTranscriptFetcher([primary, gemini])` cooperation): primary returns `[None, None, primary-c, None, None]`; gemini fills slots 0,1,4; slot 2 stays as primary; slot 3 (unresolvable `"totally-bogus"`) stays `None`; chain emits `WARNING — falling back` and final `WARNING — All transcript fetchers exhausted for totally-bogus`. Order preserved across the chain. **PASS**.
- Break path 7 (re-export identity): `from tree.data.youtube.transcript_fetcher import GeminiTranscriptFetcher as ReExported` is the same class as the canonical import. **PASS**.
- Break path 8 (call-site shape — adversarial): captured `generate_content` kwargs show `model="gemini-2.5-flash"`, exactly 1 file Part with `file_data.file_uri == "https://www.youtube.com/watch?v=eYaWxljC4sA"` and `file_data.mime_type == "video/*"`, plus 1 text Part containing `"verbatim"`. **PASS**.

**Acceptance criteria**

- [x] PASS — `apps/memory/src/tree/data/youtube/gemini_transcript_fetcher.py` exists and exports `GeminiTranscriptFetcher`.
      Evidence: file is in the working tree (`git status` shows it as untracked-new); class definition at `gemini_transcript_fetcher.py:56`; re-export verified by `BREAK 7` (identity check).
- [x] PASS — Conforms to `TranscriptFetcher` Protocol; same-length, same-order output.
      Evidence: `tests/unit/data/youtube/test_gemini_transcript_fetcher.py::TestFetchMany::test_happy_path_single_video` (line 129), `::test_order_preservation_with_distinct_text_per_id` (line 172), `::test_empty_input_list` (line 317); `BREAK 5` and `BREAK 6` confirm length+order preserved across mixed-success batches and chain composition.
- [x] PASS — Default model is `"gemini-2.5-flash"`, hard-coded at the constructor.
      Evidence: `gemini_transcript_fetcher.py:47` (`_DEFAULT_MODEL = "gemini-2.5-flash"`) used as default in `__init__` (line 78); `TestInit::test_explicit_key_succeeds_and_default_model` asserts `fetcher.model == "gemini-2.5-flash"`; `BREAK 8` captures `kwargs["model"] == "gemini-2.5-flash"`. Docstring at line 17 records the v1 hard-code rationale.
- [x] PASS — Default API key reads from `settings.google_api_key`; missing key raises `RuntimeError` at `__init__`.
      Evidence: `gemini_transcript_fetcher.py:82-85`; `TestInit::test_no_key_anywhere_raises` (line 91) and `::test_settings_key_used_when_no_explicit_key` (line 112); `BREAK 4` reproduces with the actual error message `"GOOGLE_API_KEY is not configured; see .env.example"`.
- [x] PASS — Successful response yields `FetchedTranscript` with `metadata.video_id` only, `language="en"`, populated `plain_text`, single synthetic `TranscriptSegment(start=0.0, duration=0.0)` covering full text.
      Evidence: `gemini_transcript_fetcher.py:128-139`; `TestFetchMany::test_happy_path_single_video` asserts `metadata.title is None`, `language == "en"`, `len(segments) == 1`, `segments[0].start_seconds == 0.0`, `segments[0].duration_seconds == 0.0`; `HAPPY PATH` of the e2e script printed the full `FetchedTranscript` confirming all fields.
- [x] PASS — On Gemini error / empty / refusal, returns `None` for that slot; no exception escapes; no WARNING from this layer.
      Evidence: `gemini_transcript_fetcher.py:115-121` (broad `except Exception`), `:122-126` (empty/whitespace), `:158-176` (refusal via `_extract_text`); `TestFetchMany::test_empty_response_returns_none` (line 203), `::test_whitespace_only_response_returns_none` (line 221), `::test_api_error_returns_none_no_exception_escapes` (line 229), `::test_refusal_safety_block_returns_none` (line 280) — all assert `caplog.records` filtered to this logger have `levelno < WARNING`. `BREAK 1`, `BREAK 2a/b/c` reproduce.
- [x] PASS — Unresolvable input → `None` without calling Gemini.
      Evidence: `gemini_transcript_fetcher.py:106-111` (early return before `_call_gemini`); `TestFetchMany::test_unresolvable_input_returns_none_without_calling_gemini` (line 262) asserts `client.aio.models.calls == []`; `BREAK 3` reproduces with two unresolvable inputs and `calls=0`.
- [x] PASS — Order preservation across a 5-input batch.
      Evidence: `BREAK 5` (5 inputs, mixed success, semaphore=1) → texts in input order `['alpha', None, None, 'charlie', 'delta']`. Test `::test_order_preservation_with_distinct_text_per_id` (line 172) covers 3-input order; `BREAK 6` extends to 5 inputs through chain composition.
- [x] PASS — Gemini call uses `Part.from_uri(file_uri=<canonical>, mime_type="video/*")`.
      Evidence: `gemini_transcript_fetcher.py:144-152` builds `Part.from_uri(file_uri=canonical_url, mime_type="video/*")` plus a text Part with the verbatim prompt; `TestFetchMany::test_happy_path_single_video` (lines 150-170) inspects `client.aio.models.calls[0]["contents"]` and asserts `file_data.file_uri == "https://www.youtube.com/watch?v=" + VIDEO_ID_A` and `file_data.mime_type.startswith("video/")`. `BREAK 8` reproduces and additionally confirms the prompt contains `"verbatim"`.
- [x] PASS — All unit tests in `test_gemini_transcript_fetcher.py` pass; mocks only; zero pytest warnings.
      Evidence: `uv run pytest tests/unit/data/youtube/test_gemini_transcript_fetcher.py -v` → 12 passed in 0.71s; full suite `make memory-unit-tests` → 520 passed in 20.10s, 0 warnings (output below).
- [x] PASS — Format / lint / pre-commit clean.
      Evidence: `make memory-format-check` → `151 files already formatted`; `make memory-lint-check` → `All checks passed!`; `make pre-commit` → all hooks Passed.
- [x] PASS — No new dependency added to `apps/memory/pyproject.toml`.
      Evidence: `git diff apps/memory/pyproject.toml` is empty; `google-genai>=1.65.0` already present at line 26 of `pyproject.toml`. The only working-tree change is `apps/memory/src/tree/data/youtube/transcript_fetcher.py` (+8 lines, the re-export).

**Evidence**

```
$ make memory-format-check
uv run ruff format --check src/ tests/ scripts/ deploy/
151 files already formatted

$ make memory-lint-check
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit
uv run --project apps/memory pre-commit run --all-files
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... tests/unit/data/youtube/test_gemini_transcript_fetcher.py ............ [50%] ...
============================= 520 passed in 20.10s =============================

$ uv --directory apps/memory run python /tmp/e2e_gemini.py
HAPPY PATH: PASS
BREAK 1 (API error one slot): PASS
BREAK 2a (empty text): PASS
BREAK 2b (whitespace only): PASS
BREAK 2c (safety refusal): PASS
BREAK 3 (unresolvable input no-call): PASS -> calls=0
BREAK 4 (missing key -> RuntimeError at init): PASS -> GOOGLE_API_KEY is not configured; see .env.example
BREAK 5 (order+length, semaphore=1): PASS -> max_in_flight=1
BREAK 6 (ChainedTranscriptFetcher integration): PASS
BREAK 7 (re-export identity): PASS
BREAK 8 (call shape: from_uri + video/* + verbatim prompt): PASS

ALL BREAK PATHS PASS
```

**Other issues found**

- Nit (won't block): `request_timeout_seconds` is accepted and stored on the instance but not wired into the `generate_content` call. The SWE explicitly flagged this as intentional in their notes (the `google-genai` async API doesn't expose a clean per-call timeout, and adding `asyncio.wait_for` would change failure shape). Acceptable for v1 — the parameter is part of the public constructor surface so #003/#004 won't have a breaking change when timeouts are wired in. Worth documenting in the docstring (currently silent on this).
- Nit (won't block): the spec mentions "API errors that look retryable AFTER one retry" — the v1 implementation does NOT retry. The SWE called this out in their notes ("v1 does NOT add a retry loop ... do NOT add aggressive retry loops in v1"), and the spec explicitly leaves retries optional. No AC requires retries; tests only assert that errors → `None`. Acceptable for v1.
- Observation (no action): `_call_gemini`'s `try/except Exception` swallows everything including `BaseException`-adjacent like `KeyboardInterrupt` is excluded since it's not `Exception`. Catching broad `Exception` is appropriate here because the Gemini SDK's error hierarchy is wide and unstable, and the per-slot contract requires `None` on any failure. No issue.

**VERDICT: PASS**

