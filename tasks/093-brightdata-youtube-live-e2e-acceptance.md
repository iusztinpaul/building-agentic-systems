---
id: 093-brightdata-youtube-live-e2e-acceptance
feature: brightdata-youtube-transcripts
status: done
---

# Live E2E acceptance for the Bright Data YouTube backend + accept ADR-004

Tags: `data`, `infra`, `docs`
Depends on: #092
Implements: ADR-004

## Scope

One-off operator verification against the live local stack (user "Paul Iusztin", env =
local), then flip ADR-004 Status Proposed → Accepted. This is NOT a committed test —
the feature's committed suite stays unit-only per ADR-004 Decision 8; this task proves
the real chain once, cost-bounded (one Bright Data collection ≈ $0.0007/record; one
short Gemini transcription). Only committed change: the ADR status flip (+ this task
file). If a path fails, file a rollup task — do not expand this one.

Runs (stack via `make local-start`, `make memory-serve-workflows` in background,
document every command + observed result in the log):

1. **Primary path (both keys set):** ingest ONE short video via the offline pipeline
   with a scratch source file (`youtube_video` entry; committed `sources/*.yaml`
   untouched). Verify in Mongo (`mongosh`): the Document's `content` is the transcript,
   `date` equals the video's REAL publish date (not ingest time — the behavioural
   improvement), `authors`/`title` match the channel/video, `ingest_error` is null; the
   Prefect/run logs show NO fallback WARNING.
2. **Batch-wide fallback (Bright Data key absent for one run):** same video for a
   second user or after clearing the row — the run logs the batch-wide fallback WARNING
   (reason = missing credentials, explicit Gemini token/cost mention) and the Gemini
   branch lands a Document with the BASE metadata intact. Note: direnv/Makefile export
   `.env` vars, so blanking on the command line may not win — achieve the unset via a
   scratch env mechanism and record HOW in the log.
3. **Neither key:** the run aborts up-front with the RuntimeError naming BOTH
   `BRIGHTDATA_API_KEY` and `GOOGLE_API_KEY` and `.env.example`, before any flow-side
   billable call.
4. **Ingest error row + retry:** feed an unresolvable input (e.g. a `youtube_video`
   entry whose uri has no video id) → verify the `invalid_url: no video id in input`
   row in Mongo keyed on the raw input with `content: null`; run the extraction
   pipeline and confirm the failure row is NOT processed (excluded by the
   `content != null` filter); re-run a fixed input over an errored row and confirm the
   re-attempt WARNING + row replacement. (A live `no_transcript` check needs a
   caption-less video with Gemini also failing — unit-covered in #092; verify live only
   if a cheap candidate is at hand, and say so either way.)
5. **ADR flip:** `docs/adrs/004_brightdata_primary_youtube_transcript_backend.md`
   Status Proposed → Accepted.

## Acceptance criteria

- [x] Primary path verified live: Bright-Data-sourced Document with real publish date,
      transcript content, merged metadata, `ingest_error` null, and NO fallback WARNING
      in the run logs.
- [x] Batch-wide fallback verified live: WARNING with explicit Gemini token/cost
      mention fired; Gemini-branch Document landed with base metadata intact; the
      unset mechanism documented.
- [x] Neither-key fast-fail verified: clear RuntimeError naming both env vars +
      `.env.example`, no flow-side billable call made.
- [x] `invalid_url` Ingest error row verified in Mongo (raw-input key, `content: null`,
      normalized error string); extraction run excludes it; retry-over-errored-row
      WARNING + replacement observed.
- [x] ADR-004 Status flipped Proposed → Accepted; no other ADR content changed.
- [x] Log records every command, the observed Mongo documents/counts, and the cost
      bounding (which video, how many records billed).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit` clean;
      `make memory-unit-tests` green; `make memory-integration-tests-all` green (~5 min,
      feature-done gate per AGENTS.md).
- [x] Diff discipline: committed changes are ONLY the ADR status flip + this task file
      (scratch source files / env live in the session scratchpad, not the repo).

## Out of scope

- Any code change (a failing path → rollup task). Committed live tests (forbidden by
  ADR-004 Decision 8). Ingesting long/expensive videos or full RSS channels.

## Log

### [SWE] 2026-07-24 16:15 — Live E2E operator verification

Operator verification only — NO code changed. Committed diff is the ADR-004 status
flip (one line) + this task file. Everything else (scratch source files, scratch
mongosh scripts, run logs) lives in the session scratchpad.

**Environment**

```
$ make env-status                       -> Env target: local (.env)
$ make memory-whoami                    -> 6a635d3fb312726aefbd8f94  paul.iusztin@example.com  Paul Iusztin
$ docker ps                             -> tree-mongodb / tree-mongot / tree-prefect-server all Up
```

`make local-start` errored with "container name /tree-prefect-server already in use":
the shared stack was already up from the MAIN worktree (different compose project
name, same containers/ports). Used the running stack; no restart needed.

Mongo was EMPTY at session start (0 documents, 0 knowledge_graph objects).
`make memory-signup USER_IDENTIFIER=paul.iusztin@example.com NAME="Paul Iusztin"`
created the user (id `6a635d3fb312726aefbd8f94`) and set it current.

**Cost bounding**

| item | count | note |
|---|---|---|
| Bright Data records billed | **1** | ~$0.0007. ONE video, one collection, one run (run 1). |
| Gemini video transcriptions | **1** | one 3m33s video (run 2's fallback branch). |
| Gemini extraction + Voyage embeddings | 1 small doc | run 4c, over a 1847-char transcript. |
| Bright Data collections that cost $0 | 1 | run A's trigger was rejected HTTP 401 — no collection started. |

Video used: `https://www.youtube.com/watch?v=dQw4w9WgXcQ` — 3m33s (213 s). Chosen
because it is the exact video the committed snapshot fixture
(`tests/unit/data/youtube/fixtures/brightdata_youtube_snapshot.json`) was captured
from, so it is KNOWN to carry captions (no wasted collection), and its 2009 publish
date is 17 years from ingest time, which makes the "`date` is the real publish date,
not `datetime.now(UTC)`" assertion unambiguous. No RSS channel, no long video.

Scratch source files (NOT in the repo — `sources/*.yaml` untouched, confirmed by
`git status`):

```
$SCRATCH/sources/e2e_video.yaml     -> - uri: https://www.youtube.com/watch?v=dQw4w9WgXcQ
                                        type: youtube_video
$SCRATCH/sources/e2e_invalid.yaml   -> - uri: https://www.youtube.com/watch?v=
                                        type: youtube_video
```

`--source-file` takes absolute paths verbatim (`tree.config.sources._resolve_source_path`),
so a scratchpad file works without touching the repo.

**How the env vars were actually unset (spec's explicit question)**

`make VAR=` on the command line. A make command-line variable outranks BOTH the
`include .env` assignment AND a pre-exported (direnv) value, and propagates through
the root → app sub-make via MAKEFLAGS. Verified before spending anything, with a
throwaway two-level Makefile mirroring the real root/app structure:

```
$ make -C $SCRATCH/envcheck memory-show
BRIGHTDATA_API_KEY='real-root-key'      GOOGLE_API_KEY='real-google-key'
$ make -C $SCRATCH/envcheck memory-show BRIGHTDATA_API_KEY=
BRIGHTDATA_API_KEY=''                   GOOGLE_API_KEY='real-google-key'
$ BRIGHTDATA_API_KEY=direnv-key GOOGLE_API_KEY=direnv-google \
    make -C $SCRATCH/envcheck memory-show BRIGHTDATA_API_KEY= GOOGLE_API_KEY=
BRIGHTDATA_API_KEY=''                   GOOGLE_API_KEY=''
```

Second half of the mechanism: `Settings` reads `_env_file = ".env"` RELATIVE to cwd,
and the serve process runs with cwd `apps/memory/`, which has no `.env` — so pydantic
cannot re-read the blanked key from a file. The env var is the whole story. `""`
makes `_is_configured()` False.

The override must be passed BOTH to the serve process (the flows run there) and to
the trigger command; both are recorded per run below.

**⚠ Incident on the first serve start — nightly cron backlog**

The first `make memory-serve-workflows` immediately executed 12 LATE
`data-etl-coordinator` runs (cron `0 3 * * *`, `sources/listen.yaml`) that had piled
up on the shared local Prefect server. It ingested 60 substack + 1893 latent rows
under the new user. Not a feature defect and $0 billed (`listen.yaml` has only
Substack RSS — its YouTube entry is commented out, and `substack_rss` uses plain
`httpx`, no Bright Data / Gemini). Handled:

- killed serve (`pkill -f tree.orchestrator`), the backlog was already drained so no
  LATE runs remained (`prefect flow-run ls --state Scheduled` → only 3 future-dated);
- restored the pre-task empty DB with a scratch `deleteMany({})` on `documents`
  (`knowledge_graph` untouched — it held only the signup self-person node).

Every subsequent serve start was checked for stray pickups
(`grep -c "Beginning flow run" serve_*.log` → 0 at boot).

---

#### Run A (setup, $0) — real errored row for the retry check

Rather than hand-fabricating an errored row, one was produced for free: an INVALID
Bright Data key makes the trigger 401 (a rejected trigger starts no collection, so
nothing is billed) and no Gemini key means no fallback.

```
$ nohup make memory-serve-workflows BRIGHTDATA_API_KEY=invalid-key-for-093-e2e GOOGLE_API_KEY= &
$ make memory-run-data-pipeline-offline SOURCE_FILE="$SCRATCH/sources/e2e_video.yaml" \
    BRIGHTDATA_API_KEY=invalid-key-for-093-e2e GOOGLE_API_KEY=
```

The serve process booted fine with `GOOGLE_API_KEY` blank. Run log:

```
WARNING | tree.data.youtube.youtube_ingest - Bright Data collection unavailable for 1 videos (reason=brightdata_request_error): Bright Data Web Scraper API returned HTTP 401 for https://api.brightdata.com/datasets/v3/trigger?dataset_id=gd_lk56epmy2i5g7lzu0k&format=json: Invalid credentials
WARNING | tree.data.youtube.youtube_ingest - No Gemini fallback for 1/1 videos (reason=brightdata_request_error): GOOGLE_API_KEY is not configured — recording ingest_error rows
WARNING | tree.data.youtube.youtube_ingest - No transcript for https://www.youtube.com/watch?v=dQw4w9WgXcQ (no_transcript: brightdata unavailable (trigger rejected); gemini not configured)
```

Mongo (1 document):

```
_id           : 6a635f0857f158395411bad4
source_uri    : "https://www.youtube.com/watch?v=dQw4w9WgXcQ"   <- canonical URL key
title         : "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)"
authors       : ["Rick Astley"]        <- oEmbed base metadata survived onto the failure row
date          : null
content       : null
ingest_error  : "no_transcript: brightdata unavailable (trigger rejected); gemini not configured"
```

This also covers the LIVE `no_transcript` shape the spec left optional — via the
"backend unavailable" variant, not the caption-less-video variant. A caption-less
video whose Gemini call ALSO fails was **NOT** verified live (no cheap candidate at
hand, and it would have cost a wasted collection + a wasted Gemini call); it stays
unit-covered from #092.

#### Run 1 — primary path, both keys set (1 Bright Data record billed)

```
$ pkill -f tree.orchestrator
$ nohup make memory-serve-workflows &                       # real env, no overrides
$ make memory-run-data-pipeline-offline SOURCE_FILE="$SCRATCH/sources/e2e_video.yaml"
```

Wall clock 15:49:34 → 15:50:54 (~80 s for the collection; ADR-004 measured ~173 s, so
well inside the 600 s bound). Coordinator: `data fan-out: shards_total=1 succeeded=1
failed=0`, `Done. Flow completed successfully.`

**The whole run produced exactly ONE WARNING, and it is not a fallback:**

```
$ grep WARNING serve_1.log | grep -v "Pydantic V1"
WARNING | tree.data.youtube.youtube_video - Re-attempting previously failed ingest: https://www.youtube.com/watch?v=dQw4w9WgXcQ (prior error: no_transcript: brightdata unavailable (trigger rejected); gemini not configured)
```

No "Falling back to Gemini", no "Bright Data collection unavailable". Mongo:

```
_id           : 6a635f0857f158395411bad4      <- SAME _id as run A -> row REPLACED in place
source_uri    : "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
title         : "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)"
authors       : ["Rick Astley"]
date          : 2009-10-25T06:57:33.000Z
ingest_error  : null
content       : 2089 chars
content[0:160]: "[♪♪♪] ♪ We're no strangers to love ♪ ♪ You know the rules and so do I ♪ ..."
date vs now   : 8807394.7 min apart -> REAL publish date
```

`date` is the video's REAL publish date (2009-10-25), **17 years before ingest time**
— the behavioural improvement, asserted explicitly by the scratch check above, which
computes |now - date| and only calls it a real publish date at >60 min. The caption
markers (`[♪♪♪]`, `♪`) confirm the content is the Bright Data CAPTION transcript, not
a model transcription. This run doubles as the "success replaces an errored row"
half of run 4's retry check: same `_id`, re-attempt WARNING naming the prior error.

#### Run 2 — batch-wide fallback, Bright Data key absent (1 Gemini transcription)

Row cleared first so the run actually re-ingests instead of duplicate-skipping (the
spec's "after clearing the row"):

```
$ mongosh ... --file $SCRATCH/mongo_clear_video.js       # deleteMany({source_uri: ".../watch?v=dQw4w9WgXcQ"}) -> deletedCount 1
$ pkill -f tree.orchestrator
$ nohup make memory-serve-workflows BRIGHTDATA_API_KEY= &
$ make memory-run-data-pipeline-offline SOURCE_FILE="$SCRATCH/sources/e2e_video.yaml" BRIGHTDATA_API_KEY=
```

15:53:30 → 15:54:17. The one WARNING of the run:

```
WARNING | tree.data.youtube.youtube_ingest - Falling back to Gemini for 1/1 videos (reason=brightdata_not_configured) — consumes Gemini tokens and incurs API cost
```

Reason is `brightdata_not_configured` (the MISSING-credential branch, not the
rejected-trigger one), the count is batch-wide 1/1, and the token/cost consequence is
stated explicitly. Mongo:

```
_id           : 6a636070f8230fe2bd44f727
source_uri    : "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
title         : "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)"   <- oEmbed base
authors       : ["Rick Astley"]                                                          <- oEmbed base
date          : 2026-07-24T12:54:08.110Z
ingest_error  : null
content       : 1847 chars
content[0:160]: "We're no strangers to love\nYou know the rules and so do I\n..."
date vs now   : 0.2 min apart -> INGEST-TIME fallback
```

Base (oEmbed) metadata intact — Gemini's transcript metadata carries only `video_id`,
so the merge leaves title/authors untouched. Content differs from run 1's (no caption
markers, sentence-per-line), proving it really came from Gemini. And `date` falls back
to ingest time here, which is exactly the OLD behaviour — a clean A/B against run 1's
2009 date.

#### Run 3 — neither key: up-front RuntimeError, no billable call

```
$ pkill -f tree.orchestrator
$ nohup make memory-serve-workflows BRIGHTDATA_API_KEY= GOOGLE_API_KEY= &
$ make memory-run-data-pipeline-offline SOURCE_FILE="$SCRATCH/sources/e2e_video.yaml" \
    BRIGHTDATA_API_KEY= GOOGLE_API_KEY=
```

15:55:19 → 15:55:56 (~37 s, i.e. 3 task attempts under `retries=2`; nowhere near a
~80 s collection or a Gemini call — nothing billable ran):

```
File ".../tree/data/youtube/youtube_ingest.py", line 307, in _bulk_build_and_load
File ".../tree/data/youtube/youtube_ingest.py", line 107, in fetch_transcripts_batch
    raise RuntimeError(
RuntimeError: Neither BRIGHTDATA_API_KEY nor GOOGLE_API_KEY is configured; see .env.example
```

Both env vars named, `.env.example` named, raised at the TOP of
`fetch_transcripts_batch` before either fetcher is constructed. Prefect flow-run
states: the two worker/subflow runs are `FAILED`. Mongo unchanged afterwards (still
the single run-2 document, byte-identical) — the run wrote nothing.

**Observation (NOT a defect of this feature, no fix attempted):** while both worker
flow runs ended `FAILED`, the coordinator logged `data fan-out: shards_total=1
succeeded=1 failed=0` and finished `Completed`. Cause is pre-existing and untouched
by this feature — `apps/memory/src/tree/data/offline_pipeline.py:464` counts a shard
as succeeded whenever `run_deployment(...)` RETURNS, never inspecting the returned
run's terminal state (`git log main..HEAD -- .../offline_pipeline.py` is empty).
Worth a separate rollup task; it makes a hard-failing shard invisible to the
coordinator's summary line.

#### Run 4 — invalid_url row, retry, extraction exclusion ($0 for 4a/4b)

```
$ pkill -f tree.orchestrator
$ nohup make memory-serve-workflows &                       # real env again (4c needs Gemini)
$ make memory-run-data-pipeline-offline SOURCE_FILE="$SCRATCH/sources/e2e_invalid.yaml"   # 4a
$ make memory-run-data-pipeline-offline SOURCE_FILE="$SCRATCH/sources/e2e_invalid.yaml"   # 4b
$ make memory-run-memory-pipeline-extraction-offline                                       # 4c
```

4a (15:57:57 → 15:58:26). The unresolvable input is alone in the shard, so
`fetch_transcripts_batch` short-circuits on an empty item list — no backend call, $0.

```
WARNING | tree.data.youtube.youtube_pipeline - Could not resolve video id from input: https://www.youtube.com/watch?v=
```

```
_id           : 6a636169f97c8074c7002447
source_uri    : "https://www.youtube.com/watch?v="        <- keyed on the RAW input, not a canonical URL
title         : null
authors       : []
date          : null
content       : null
ingest_error  : "invalid_url: no video id in input"       <- normalized, no exception dump
```

4b (15:58:44 → 15:59:05) — same input again over the errored row:

```
WARNING | tree.data.youtube.youtube_pipeline - Could not resolve video id from input: https://www.youtube.com/watch?v=
WARNING | tree.data.youtube.youtube_video - Re-attempting previously failed ingest: https://www.youtube.com/watch?v= (prior error: invalid_url: no video id in input)
```

`_id` still `6a636169f97c8074c7002447` and the collection still holds 2 documents —
replaced in place, not duplicated. (The success-over-errored-row direction of the same
semantics was observed in run 1.)

4c — extraction over the pending set. DB state going in: 2 YouTube documents, one with
content (run 2's) and one failure row.

```
INFO | extraction fan-out: resolved 1 pending document(s) for user_id=6a635d3fb312726aefbd8f94
INFO | extraction fan-out: partitioned 1 document(s) into 1 shard(s) (num_shards=1)
INFO | extraction fan-out: shards_total=1 succeeded=1 failed=0
INFO | extraction fan-out: triggering single memory-indexing-etl run
Done. Flow completed successfully.
```

1 pending of 2 documents — the failure row is excluded by
`compute_pending_document_ids`'s `{"content": {"$ne": None}}`. Confirmed against the
resulting graph rather than just the count:

```
failure row _id : 6a636169f97c8074c7002447  (invalid_url: no video id in input)
content row _id : 6a636070f8230fe2bd44f727
knowledge_graph objects: 14
KG objects citing the FAILURE row : 0
KG objects citing the CONTENT row : 8
```

#### Run 5 — ADR flip

`docs/adrs/004_brightdata_primary_youtube_transcript_backend.md` Status
`Proposed` → `Accepted`. `git diff` is exactly `1 file changed, 1 insertion(+),
1 deletion(-)`; no other ADR content touched.

---

**Feature-done gate**

```
$ make memory-format-check    -> 294 files already formatted
$ make memory-lint-check      -> All checks passed!
$ make pre-commit             -> prettier / ruff check / ruff format / biome check: all Passed
$ make memory-unit-tests      -> 1824 passed in 43.38s
$ make memory-integration-tests-all
   data   : passed (see flake note below)
   memory : 135 passed in 288.82s
   mcp    : 42 passed in 37.82s
   rest   : 70 passed in 179.77s
   exit code 0
```

The FIRST `memory-integration-tests-all` attempt aborted in the `data` area on
`tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_returns_results_with_titles_and_urls`
with `httpx.ReadTimeout` — a live Bright Data SERP call timing out, unrelated to this
feature. It passed on the clean re-run (exit 0 requires every area to pass, since the
Makefile loops `... || exit 1`). The two known-flaky tests called out in the task
brief (`test_indexing_pipeline.py::test_embeds_nodes`,
`test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent`) both PASSED here,
so nothing was waived.

**Diff discipline**

```
$ git status --porcelain
 M docs/adrs/004_brightdata_primary_youtube_transcript_backend.md
?? tasks/093-brightdata-youtube-live-e2e-acceptance.md
```

Nothing else. `sources/*.yaml` untouched; no code changed; scratch sources, scratch
mongosh scripts and all run logs stayed in the session scratchpad.

**Cleanup**

Serve process killed (`pkill -f tree.orchestrator`; `pgrep` clean). Docker stack left
running as found. Local Mongo left with the 2 YouTube documents + 14 knowledge_graph
objects this verification produced.

**Notes**

- NOT RUN — live `no_transcript` for a caption-less video where Gemini ALSO fails: no
  cheap candidate at hand and it would burn a collection plus a Gemini call for a path
  #092 already unit-covers. The `no_transcript:` row SHAPE was still verified live in
  run A via the backend-unavailable variant.
- Follow-up candidate (pre-existing, out of scope here): the data coordinator's
  fan-out counts a shard as succeeded when `run_deployment` returns, so a FAILED
  worker reports as `succeeded=1 failed=0` (see run 3).
- Follow-up candidate (pre-existing, out of scope here): starting a serve process
  drains the nightly `listen.yaml` cron backlog immediately, which silently ingests
  under whichever user is current. Worth a guard or a documented pre-serve check.
- `tree.*` INFO logs do not reach the terminal or the Prefect log store from worker
  subprocesses (only WARNING+ does), so the Bright Data client's "Triggering
  collection… / snapshot returned N record(s)" INFO lines are not quotable here. All
  criteria hinge on WARNINGs and Mongo state, both of which are fully observable.
  Pre-existing logging behaviour, not touched by this feature.

### [Tester] 2026-07-24 16:35 — QA (acceptance review, no live reproduction)

This is an acceptance task with zero production-code diff. Per the assignment I did
NOT re-run the live Bright Data / Gemini E2E (cost + time). I instead: (1) verified
diff discipline, (2) independently re-ran every non-live gate, (3) line-by-line
cross-referenced every quoted log message / error string / Mongo-key-filter the SWE
reported against the actual source so the evidence can't be a fabrication, (4)
checked internal consistency of the five runs (ObjectId ordering, replacement
semantics, document/KG counts), and (5) attempted an independent live-Mongo spot
check, which surfaced a genuine but out-of-scope caveat (see below).

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check` → 294 files already
  formatted; `make memory-lint-check` → All checks passed; `make pre-commit` →
  prettier / ruff check / ruff format / biome check all Passed) — matches SWE report.
- Unit tests: 1824 passed / 0 failed (`make memory-unit-tests`, 43.29s) — count
  matches SWE report exactly.
- Integration tests (mongot-free CI mirror, `make memory-integration-tests-ci`, run
  by me to avoid live-cost re-execution of `integration-tests-all`): data area
  29 passed / 1 skipped (including `test_web_serp.py` — 3/3 passed, no flake this
  time), memory 92 passed, mcp 42 passed, rest 43 passed. All exit 0. I did not
  re-run `make memory-integration-tests-all` myself (it exercises the same live SERP
  path that flaked once for the SWE and offers no additional signal over the CI
  mirror for this diff-free task); I instead judged the SWE's reported clean
  re-run (exit 0, both previously-flaky tests passing) as sufficient given it is
  fully consistent with what I observed in the mirror.
- Warnings: 0 (ruff, pytest — no collection warnings beyond the pre-existing
  `opik`/pydantic-v1/Python-3.14 `UserWarning`, present in every run and unrelated to
  this task).

**E2E adversarial pass**
This is an acceptance task with NO code changes and an explicit "do not re-run the
live E2E" instruction, so the "attack it yourself" step is replaced by an adversarial
*evidence audit* — the equivalent rigor applied to the artifact under review (the
Log), not to a running system:
- Happy path equivalent — code-vs-log cross-reference: every quoted WARNING/error
  string in the Log (`Neither BRIGHTDATA_API_KEY nor GOOGLE_API_KEY is configured;
  see .env.example` at `youtube_ingest.py:107-109`; `Falling back to Gemini for %d/%d
  videos (reason=%s) — consumes Gemini tokens and incurs API cost` at
  `youtube_ingest.py:122-127`; `No Gemini fallback for %d/%d videos...` at
  `youtube_ingest.py:135`; `Re-attempting previously failed ingest: %s (prior error:
  %s)` at `youtube_video.py:210`; `Could not resolve video id from input: %s` at
  `youtube_pipeline.py:47/69`; `INVALID_URL_ERROR = "invalid_url: no video id in
  input"` at `youtube_video.py:43`) exists verbatim in the current source. PASS — the
  evidence is not fabricated, it traces to real code paths.
- Break path 1 (evidence self-consistency: ObjectId/state audit) — cross-checked the
  five runs against each other rather than against the live system: run A and run 1
  share `_id 6a635f0857f158395411bad4` (replace-in-place, consistent with the
  `SourceType`-replace design); run 2's `_id 6a636070f8230fe2bd44f727` and run 4's
  `_id 6a636169f97c8074c7002447` sort strictly after run 1's in ObjectId hex order,
  consistent with Mongo's roughly-monotonic ObjectId timestamps and the claimed wall
  clock ordering (15:49→15:59). Final claimed state (2 documents: run 2's content row
  + run 4's failure row; run 1's row was explicitly deleted before run 2) is
  arithmetically consistent with "1 pending of 2 documents" and "14 KG objects, 0
  citing the failure row, 8 citing the content row" in run 4c. PASS — no arithmetic or
  ID-ordering contradiction found across the five runs.
- Break path 2 (extraction-exclusion filter claim) — verified
  `{"content": {"$ne": None}}` is the actual live filter at
  `src/tree/memory/extraction/pipeline.py:1802`, and the log line
  `"extraction fan-out: resolved %d pending document(s) for user_id=%s"` at line 1590
  matches the Log's quoted `INFO` line verbatim. PASS — the "failure row excluded from
  extraction" claim is grounded in real code, not asserted on faith.
- Break path 3 (out-of-scope defect (a): coordinator fan-out miscounting FAILED
  workers as succeeded) — read `offline_pipeline.py:459-465`: `stats.failed` only
  increments `if isinstance(result, Exception)`, i.e. only when `run_deployment`
  itself raises (network/RPC failure), never when the returned run's own terminal
  state is `FAILED`. Confirmed the claim is accurate. Confirmed via
  `git log main..HEAD -- apps/memory/src/tree/data/offline_pipeline.py` (empty) that
  no commit on this feature branch touched this file — genuinely pre-existing, not
  introduced or worsened by this feature. Correctly out of scope; rollup material,
  not a FAIL here.
- Break path 4 (independent live-Mongo spot check, done to sanity-check claim 4(b) —
  "DB really was restored / run 1-4 evidence not contaminated") — connected to the
  live `tree` database myself (`mongosh` with `.env` creds) post-session and found 2
  documents, but NEITHER matches anything from the SWE's runs: `source_uri`s
  `.../watch?v=eYaWxljC4sA` and `.../watch?v=AAAaaaBBBcc`, titles `"An Interesting
  Video"`, `ingest_error: "no_transcript: brightdata + gemini both returned empty"`.
  Traced these video IDs: they appear ONLY as literal fixtures in
  `tests/integration/data/youtube/test_youtube_pipeline.py` (lines 334-372) — but that
  suite's `mongo_client` fixture hard-codes `TEST_DATABASE = "integration_tests_twin"`
  (`tests/integration/conftest.py:14-25`), a database dropped at session end, NOT
  `tree`. So this drift did not come from my own `integration-tests-ci` run moments
  earlier, and it does not match anything in the SWE's Log. Conclusion: the shared
  Docker Mongo container (confirmed already, by the SWE's own nightly-cron-backlog
  incident, to be shared across worktrees/sessions on this machine) has been mutated
  by SOME other process since the SWE's session ended — most plausibly manual/agent
  activity in another worktree pointed at the same container. This is NOT evidence
  that the SWE's own run 1-4 results were contaminated (their Log captures each
  run's Mongo output inline, contemporaneously, and that inline evidence is internally
  consistent per Break path 1 above); it IS confirmation that this shared-infra setup
  is fragile and that "final DB state" claims age out fast. Verdict: does not
  invalidate the SWE's evidence (the claim under test was about state at the END OF
  THEIR SESSION, which their own inline mongosh dumps already document), but
  reinforces that the SWE's own "worth a guard or a documented pre-serve check"
  follow-up candidate is real and should be prioritized. Not a FAIL — this task
  forbids me from re-running the live E2E to get a byte-for-byte final-state
  reproduction, and the SWE's contemporaneous per-run evidence is what this task asks
  me to judge, not the DB's state days/hours later.

**Acceptance criteria**
- [x] PASS — Primary path verified live (real publish date, transcript content,
      merged metadata, `ingest_error` null, no fallback WARNING) — Log run 1: `date:
      2009-10-25T06:57:33.000Z` (8.8M minutes before ingest), `content` 2089 chars
      with `[♪♪♪]` caption markers, `ingest_error: null`, only WARNING in the run is
      the unrelated re-attempt one (grep for "Falling back"/"unavailable" returns
      nothing). Grounded: `merge_video_metadata` / Bright-Data-wins design in ADR-004
      Decision 5, matching code paths confirmed above.
- [x] PASS — Batch-wide fallback verified live (explicit token/cost WARNING, Gemini
      Document with base metadata intact, unset mechanism documented) — Log run 2:
      `Falling back to Gemini for 1/1 videos (reason=brightdata_not_configured) —
      consumes Gemini tokens and incurs API cost` (string verified verbatim at
      `youtube_ingest.py:122-127`); oEmbed title/authors survive; `date` falls back to
      ingest time (clean A/B vs run 1's real date); unset mechanism (`make VAR=`
      outranking `include .env` + direnv via MAKEFLAGS) demonstrated with a
      throwaway two-level Makefile BEFORE any live call.
- [x] PASS — Neither-key fast-fail verified (RuntimeError naming both vars +
      `.env.example`, no billable call) — Log run 3: exact string matches
      `youtube_ingest.py:107-109` verbatim; raised before any fetcher construction;
      ~37s wall clock (well under an ~80s collection or a Gemini call); Mongo
      unchanged after the run.
- [x] PASS — `invalid_url` row + retry + extraction exclusion verified — Log run 4:
      `source_uri` keyed on raw input `".../watch?v="`, `content: null`,
      `ingest_error: "invalid_url: no video id in input"` (matches
      `INVALID_URL_ERROR` constant at `youtube_video.py:43` exactly); 4b re-attempt
      WARNING + same `_id` (replace not duplicate); 4c extraction resolves 1 pending
      of 2 (matches the live `{"content": {"$ne": None}}` filter at
      `extraction/pipeline.py:1802`), 0 KG objects cite the failure row.
- [x] PASS — ADR-004 Status flipped Proposed → Accepted, no other content changed —
      `git diff docs/adrs/004_brightdata_primary_youtube_transcript_backend.md` is
      exactly `1 file changed, 1 insertion(+), 1 deletion(-)`, the single line being
      the Status field; reproduced independently.
- [x] PASS — Log records every command, observed Mongo docs/counts, cost bounding —
      read in full; command + observed-output pairs present for all 5 runs plus run A;
      cost table present (1 Bright Data record ~$0.0007, 1 Gemini transcription, 1
      small extraction/indexing run, 1 rejected trigger at $0).
- [x] PASS — feature-done gate green — independently reproduced format-check,
      lint-check, pre-commit, and unit-tests (see Test summary; unit count matches
      exactly); reproduced the mongot-free CI mirror in place of re-running the
      costly `integration-tests-all` (see Evidence).
- [x] PASS — diff discipline: only the ADR status flip + task file —
      `git status --porcelain --untracked-files=all` →
      ` M docs/adrs/004_brightdata_primary_youtube_transcript_backend.md` and
      `?? tasks/093-brightdata-youtube-live-e2e-acceptance.md`, nothing else;
      reproduced independently.

**Evidence**
```
$ git status --porcelain --untracked-files=all
 M docs/adrs/004_brightdata_primary_youtube_transcript_backend.md
?? tasks/093-brightdata-youtube-live-e2e-acceptance.md

$ git diff --stat
 docs/adrs/004_brightdata_primary_youtube_transcript_backend.md | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)

$ make memory-format-check && make memory-lint-check
294 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness): all Passed

$ make memory-unit-tests
1824 passed in 43.29s

$ make memory-integration-tests-ci
data:   29 passed, 1 skipped in 126.66s
memory: 92 passed, 43 deselected in 85.74s
mcp:    42 passed in 23.15s
rest:   43 passed, 27 deselected in 2.56s
```

**Other issues found**
- Out-of-scope defect (a) confirmed real and pre-existing, not introduced by this
  feature: `offline_pipeline.py:459-465` counts a fan-out shard as succeeded whenever
  `run_deployment` *returns* (no exception), never inspecting the returned flow run's
  own terminal state — a hard-FAILED worker is invisible to the coordinator's summary
  line. `git log main..HEAD` on that file is empty. Rollup task candidate, already
  flagged by the SWE.
- Out-of-scope defect (b) confirmed real and pre-existing: the shared local Docker
  Mongo container is mutated by whatever worktree/session currently points at it
  (demonstrated twice now — once by the SWE's own nightly-cron-backlog incident, once
  by my own post-session spot check turning up unrelated fixture-shaped documents
  from neither my nor the SWE's runs). Worth the guard/pre-serve check the SWE already
  suggested; not a defect of this feature's code.
- Minor: the Log cannot quote Bright Data client INFO lines ("Triggering
  collection…") because `tree.*` INFO doesn't reach the terminal/Prefect log store
  from worker subprocesses — pre-existing logging behavior, correctly called out by
  the SWE as a gap rather than silently glossed over.

**VERDICT: PASS**

Diff discipline holds (only the ADR-004 status flip + this task file). Every quoted
log message, error string, and Mongo-filter claim in the Log traces to real,
unmodified source code — the evidence is not asserted on faith. The five runs are
internally consistent (ObjectId ordering, replace-in-place semantics, document/KG
counts). `no_transcript` is genuinely unit-covered in #092's suite across four
distinct scenarios, so the one live path the SWE skipped is not actually unverified.
Both out-of-scope observations are genuinely pre-existing and unrelated to this
feature's diff (confirmed via `git log main..HEAD`), correctly routed to a rollup
task rather than fixed here or hidden. All non-live gates (format, lint, pre-commit,
unit, CI-mirror integration) reproduced green with 0 warnings. Hand off to PA for
acceptance review.

### [PA] 2026-07-24 17:05 — Acceptance Review (feature `brightdata-youtube-transcripts`, tasks #089–#093, PR #34)

**VERDICT: ACCEPT**

Reviewed the whole feature from the user's perspective against the user's own
grilling-session asks, not only the task ACs. Every ask maps to shipped, live-verified
behavior:

1. **Fallback per credential combination** — both keys: Bright Data primary inside the
   YouTube ETL (not the generic web ETL), Gemini rescues transcript-less slots AND any
   batch-wide Bright Data failure (`youtube_ingest.py:113-140`, live runs 1/2/A).
   BD key absent: whole batch → Gemini (run 2). Neither key: up-front
   `RuntimeError: Neither BRIGHTDATA_API_KEY nor GOOGLE_API_KEY is configured; see
   .env.example` before any billable call (run 3). BD-only: misses become error rows
   instead of a crash — a sensible completion of the user's matrix.
2. **Cost WARNING** — single warning site fires on EVERY Gemini invocation ("Falling
   back to Gemini for N/M videos (reason=…) — consumes Gemini tokens and incurs API
   cost"), at WARNING level, which is exactly the level that reaches the terminal /
   Prefect store from workers (INFO does not) — genuinely visible, explicitly costed.
3. **"What failed / what to rerun"** — `Document.ingest_error` is a normalized
   `code: message` from a fixed vocabulary; `{ingest_error: {$ne: null}}` lists
   failures, the code prefix tells the user the fix (`invalid_url` → fix input,
   `…not configured` → set a key, `both returned empty` → no captions), and a plain
   pipeline re-run auto-retries errored rows with a WARNING naming the prior error —
   verified live (run A → run 1, same `_id` replaced; success rows never clobbered by
   later failures).
4. **"Completely separate"** as clarified — two independent fetchers, no base class,
   `gemini_transcript_fetcher.py` byte-identical; ONE `merge_video_metadata` combines
   feed/oEmbed base metadata with either backend's output in BOTH branches (BD non-None
   wins → real 2009 publish date in run 1; Gemini carries only `video_id` → base intact
   in run 2).
5. **Testing posture** — committed suite is mocked/fixture-only, mapping proven against
   the genuine captured vendor snapshot fixture; live proof was the one-off operator
   run in #093, not a committed test. Exactly "recorded and mocked", "one-off, not in
   the integration tests".
6. **Scope** — nothing missing; nothing unasked-for. The `youtube:` YAML block (2
   timing knobs, no `enabled` toggle, no new env vars) is the minimum the bounded
   async wait needs. No speculative interface. ADR-004 (one ADR for the feature,
   Accepted) + 3 glossary terms used consistently in code and copy.

Adjacent follow-up candidates (out of feature scope, new tasks — not expansion):
coordinator fan-out counting a FAILED worker as succeeded (`offline_pipeline.py:464`,
pre-existing); serve-start draining the nightly cron backlog under the current user;
Gemini-side unexpected exception mid-fallback re-billing Bright Data on Prefect retry.

All acceptance criteria verified from user POV. Hand off to the PR Reviewer.

### [PR Reviewer] 2026-07-24 — Review (PR #34, branch `feat/brightdata-youtube-transcripts`)

**VERDICT: NO BLOCKERS**

Reviewed all 29 changed files (~6,000 added lines) against `docs/adrs/004_brightdata_primary_youtube_transcript_backend.md` (Accepted): full diff read file-by-file; walked performance, clean-code, untested-code, standards, documentation-discipline, and simplicity dimensions. Re-ran `make memory-unit-tests` on the local env: 1,824 passed.

Verified specifically:
- **No test can trigger a live paid call** — every Bright Data path is patched at the `collect` seam or the `_post_json`/`_get_json` seams; both fetcher construction points are patched in ingest/pipeline/integration tests; credential presence is faked via `settings` patches, so the suite behaves identically with or without keys in `.env`. The committed snapshot fixture carries no credentials (the `video_url` googlevideo link is an expired signed playback URL, not a secret).
- Credential gate raises before either fetcher is constructed; an unconfigured backend is never built; fetchers stay inside the Prefect task body (never task inputs).
- `merge_video_metadata` is pure, non-mutating, and cannot drop base metadata (only non-None override fields win; Gemini's video_id-only metadata leaves base intact — unit-proven).
- `web_scraper_api` trigger/poll/download loop: bounded deadline, sleep clamped to remaining time, `failed` status → typed error, 2xx-with-non-JSON body (WAF/captcha/empty) → `BrightDataRequestError`, keeping the fallback chain intact.
- Fetcher record→slot realignment by video id (arbitrary order, dedup billed once, `input.url` fallback) and ms→s conversion asserted against the real captured fixture.
- Docs discipline: ADR-004 present + Accepted; glossary gains Transcript fetcher / Transcript fallback chain / Ingest error; no ADR contradicted.

Blockers: 0; Nits: 4 (appended to the PR #34 description): stale "SOLE backend" docstrings in `gemini_transcript_fetcher.py`; untyped httpx transport errors bypassing the fallback chain in `web_scraper_api.py` (consistent with `web_unlocker`, non-blocking); missing return annotations on new test helpers; the two known follow-ups confirmed as recorded.

Pipeline may advance to hand-off.
