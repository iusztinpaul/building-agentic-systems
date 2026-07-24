# tasks/

One markdown file per atomic task: `tasks/<NNN>-<slug>.md`, with state in the `status:`
frontmatter (`pending` → `in-progress` → `done`) and an append-only `## Log`. The set of
files for a `feature:` IS that feature's plan, processed in `NNN` order by `/implement-task`
/ `/implement-night`. (Earlier features live under `tracker/`; new ones land here.)

Once a feature has shipped, move its files to `tasks/done/` — same filename, `status: done`
unchanged. `tasks/` then lists only live work, so "what is left to build?" is answered by
`ls tasks/`, while the `## Log` history (SWE, Tester, PA and reviewer entries, plus any
live-acceptance evidence) stays readable instead of being recoverable only from git.

Good: `tasks/089-document-ingest-error-field.md` → `tasks/done/089-document-ingest-error-field.md`
after the feature merges. Bad: deleting the file, or archiving one task of a feature while
its siblings are still `pending`.
