---
id: 095-batch-retry-waste-and-shard-success-masking
feature: brightdata-youtube-transcripts-followups
status: pending
---

# Stop discarding paid transcripts on batch retry + stop counting failed workers as succeeded

Tags: `data`, `infra`
Depends on: None (both surfaced during PR #34; part B is older)
Implements: —

Two independent defects, both found during the `brightdata-youtube-transcripts` review
and both about failures being silently absorbed. They are bundled by the project owner's
explicit request; keep the two parts separate in the diff and in the log.

## Part A — a Gemini exception discards already-paid-for Bright Data transcripts

Found by the Tester on #092, accepted there as non-blocking because it matches the
pre-existing whole-batch-failure pattern in `apps/memory/src/tree/data/batch.py`.

In `youtube_ingest.fetch_transcripts_batch`, an unexpected exception escaping Gemini's
bulk `fetch_many` (anything other than the three named `BrightData*Error`s) fails the
whole Prefect task. Prefect then retries the task from the top — re-running the Bright
Data collection and **re-billing it** — even though those transcripts had already been
fetched successfully moments earlier. The cost is real but bounded: `retries=2` on the
task, so a single bad Gemini call can bill Bright Data three times for the same batch.

Fix so that a Gemini-side failure cannot destroy Bright Data work already done. Options
worth weighing in the log before choosing:
- Catch broadly around the Gemini fallback call and treat its failure as "no Gemini
  rescue available" — the transcript-less slots become `no_transcript:` Ingest error
  rows (already a supported outcome), the Bright Data successes still land, and the task
  completes. Simple, no new state.
- Anything requiring cross-retry persistence is almost certainly over-built for this;
  argue for it explicitly if you disagree.

Whatever you pick, a Gemini outage must not silently degrade into lost data: the
outcome has to be visible in the logs and in `ingest_error`.

## Part B — coordinator counts a hard-failed worker as succeeded

Pre-existing, unrelated to Bright Data, confirmed by the Tester via an empty
`git log main..HEAD` on the file. Observed live during #093 run 3: two workers hard-FAILED
and the coordinator still reported `succeeded=1 failed=0`.

`apps/memory/src/tree/data/offline_pipeline.py:459-465` counts a shard as succeeded
whenever `run_deployment` RETURNS, without inspecting the returned flow run's terminal
state. A worker that raises still reports success, so the fan-out summary — the operator's
main signal that an offline run worked — can read green while data is missing. Inspect
the returned flow-run state and count only genuinely-Completed runs.

Check whether the memory-pipeline coordinator shares this pattern; if it does, fix both
and say so, rather than leaving a known-broken twin.

## Acceptance criteria

- [ ] **Part A:** a Gemini `fetch_many` raising an unexpected exception no longer fails
      the task; Bright Data transcripts fetched in that batch still land as Documents,
      and the un-rescued slots become `no_transcript:` Ingest error rows. Unit-asserted
      with an exception injected at the Gemini seam.
- [ ] **Part A:** the Gemini failure is visible — a WARNING naming the failure and the
      affected slot count; no silent data loss.
- [ ] **Part A:** the chosen approach, and why the alternatives were rejected, recorded
      in the log.
- [ ] **Part B:** a shard whose `run_deployment` returns a FAILED/CRASHED flow run is
      counted as failed, not succeeded; the fan-out summary reports it. Unit-asserted
      against each non-Completed terminal state, not just one.
- [ ] **Part B:** a statement in the log on whether the memory-pipeline coordinator
      shares the pattern, and if so, that it was fixed too.
- [ ] Parts A and B are separable in the diff (distinct files; ideally distinct commits).
- [ ] NO live Bright Data or Gemini call in any committed test (ADR-004 Decision 8).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit` clean;
      `make memory-unit-tests` green, 0 warnings; `make memory-integration-tests` green
      (Part B touches the coordinator).

## Out of scope

- Rewriting the shared whole-batch-failure contract in `data/batch.py` — Part A is a
  targeted fix at the YouTube fallback chain, not a redesign of batch semantics.
- Retry-count or backoff tuning on any Prefect task.

## Log
