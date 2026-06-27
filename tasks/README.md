# tasks/

One markdown file per atomic task: `tasks/<NNN>-<slug>.md`, with state in the `status:`
frontmatter (`pending` → `in-progress` → `done`) and an append-only `## Log`. The set of
files for a `feature:` IS that feature's plan, processed in `NNN` order by `/implement-task`
/ `/implement-night`. (Earlier features live under `tracker/`; new ones land here.)
