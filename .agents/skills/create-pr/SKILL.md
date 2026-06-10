---
name: create-pr
description: Create or update a GitHub pull request for the current branch. Use this skill whenever the user says "create PR", "open PR", "update PR", "push and PR", "/create-pr", or when the PR workflow step is reached in the development workflow. Handles both first-time PR creation and updating existing PRs with new changes.
---

# Create or Update Pull Request

This skill handles the full PR lifecycle: push, create/update PR, code review, and CI verification. It never merges — that's always the user's decision.

## Step 1: Preflight Checks

Run these in parallel:

- `git branch --show-current`
- `git status`
- `gh pr list --head $(git branch --show-current) --json number,url,title,state --limit 1`

**Stop if:**
- Current branch is `main` or `master` — tell the user to create a feature branch first.
- There are uncommitted changes — ask if they want to commit first (suggest the `commit-commands:commit` skill).

Save the branch name and whether a PR exists for the remaining steps.

## Step 2: Determine Base Branch

Find the branch this one was created from, using this priority order:

1. **Existing PR** — if Step 1 found a PR, use its base: `gh pr view --json baseRefName -q .baseRefName`
2. **Git tracking** — `git config branch.<current>.merge` may name the upstream branch.
3. **Merge-base heuristic** — list all local branches and find which one has the closest merge-base to HEAD:
   ```
   for branch in main $(git branch --format='%(refname:short)' | grep -v "^$(git branch --show-current)$"); do
     echo "$branch $(git merge-base HEAD $branch)"
   done
   ```
   Pick the branch whose merge-base commit is closest to HEAD (fewest commits between merge-base and HEAD). This correctly handles feature branches created from other feature branches.
4. **Ask the user** — if ambiguous, show the candidates and let them pick.

## Step 3A: Create New PR (no existing PR found)

1. **Push**:
   ```
   git push -u origin <current-branch>
   ```

2. **Build title and description** from commits:
   - `git log <base>..HEAD --oneline` for the commit list
   - `git diff <base>..HEAD --stat` for a file-level summary
   - Title: concise (<70 chars), Conventional Commits style derived from branch name or dominant commit type (e.g., `feat: add substack data pipeline`)

3. **Create the PR**:
   ```
   gh pr create --base <base-branch> --title "<title>" --body "$(cat <<'EOF'
   ## Summary
   <2-3 sentence overview of what this PR does and why>

   ## Changes
   <bullet list of key changes, grouped logically>

   ## Test plan
   - [ ] Unit tests pass (`make tests`)
   - [ ] Integration tests pass
   - [ ] Manual verification

   🤖 Generated with [Claude Code](https://claude.com/claude-code)
   EOF
   )"
   ```

4. Print the PR URL.

## Step 3B: Update Existing PR

1. **Push** latest commits:
   ```
   git push
   ```

2. **Read current description**: `gh pr view --json body,number,title`

3. **Regenerate the description** — rebuild the Summary and Changes sections from the full diff (`git log <base>..HEAD --oneline` and `git diff <base>..HEAD --stat`), but preserve any sections the user added manually (look for sections not in the original template). Update with `gh pr edit <number> --body "..."`.

4. Print the PR URL.

## Step 4: Code Review

Invoke the `code-review:code-review` skill to review the PR diff.

If the review finds issues worth fixing:
1. Fix them.
2. Commit via `commit-commands:commit`.
3. Push and update the PR description (repeat Step 3B).

If the review only has minor suggestions or style nits, mention them to the user but don't block on them.

## Step 5: CI/CD Verification

1. **Check status** (poll, don't use `--watch` which can hang):
   ```
   gh pr checks <number>
   ```
   If checks are still pending, wait briefly and re-check (up to ~5 minutes). Use `gh run list --branch <branch> --limit 1 --json databaseId,status,conclusion` to monitor.

2. **On failure**, get logs and diagnose:
   ```
   gh run view <run-id> --log-failed
   ```
   Fix the issue, commit, push, and re-check. Repeat until CI passes or you've tried 3 times (then ask the user for guidance).

3. After CI passes, update the PR description if any fixes were made.

## Step 6: Done

Report to the user:
- PR URL
- CI status (passed / still running / failed after N attempts)
- Remind them: "The PR is ready for your review. Merge when you're satisfied — I won't merge it."

## Edge Cases

- **Push rejected** (remote has new commits): suggest `git pull --rebase` and let the user decide.
- **No commits ahead of base**: warn that there's nothing to PR.
- **Force push**: never do it unless the user explicitly asks.
- **Multiple PRs for same branch**: use the most recent open one.
