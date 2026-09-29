---
name: pr-description
description: Write a PR title and description for merging one branch into another in this repo (feature branch → develop, or develop → main). Use when the user asks for a PR title/description, PR text, or release notes for a merge.
argument-hint: "[source branch] [target branch]  (default: current branch → develop)"
---

# PR title and description

Write a ready-to-paste PR title and description for `$ARGUMENTS`.

## 1. Work out the branches

- Two branch names given: source → target.
- One given: that branch → `develop` (or → `main` if the branch is `develop`).
- None given: the current branch → `develop` (or `develop` → `main` if on `develop`).
- The user may ask for several PRs at once ("both branches"); write one block per PR. If one branch is
  stacked on another, say which to merge first and that the second shrinks once the first is in.

## 2. Gather what actually changed

Always compare against the remote, not stale local refs:

```bash
git fetch -q origin
git log --oneline origin/<target>..origin/<source>
git log --oneline origin/<source>..origin/<target>   # anything the target has that the source lacks
git diff --stat origin/<target>...origin/<source>
```

- Use the local branch instead of `origin/<source>` if it isn't pushed yet (and tell the user to push).
- For `develop → main`, list the merged PR numbers from the merge commits (`Merge pull request #N`).
- Read the diff of anything the commit messages don't explain. Describe what the code does, not
  what the commit messages claim.
- Note anything in the target that isn't in the source. Merge commits only are harmless; say so.

## 3. Write it

Output the title and the description each in its own fenced block so they copy cleanly.

**Title**
- Conventional prefix matching the repo's commits: `feat(scope):`, `fix(scope):`, `style(web):`, …
- For `develop → main`, start with `Release:` and name the two or three biggest changes.
- One line, no trailing period.

**Description**, in this order and leaving out empty sections:

```markdown
## Summary
Why this change exists and what it does, in 1–3 sentences.

Included: #N (short name), …        ← develop → main only

## Changes
Grouped by area (Admin, Dashboard, Poller/inference, Storage, Server, ML, CI…), as short bullets.
Lead with user-visible behavior; mention files only when it helps a reviewer.

## Notes
- Schema changes: the new SCHEMA_VERSION and migration name, or "No schema changes".
- API: any changed response shapes or endpoints, or "No API response shape changes".
- Auth: confirm admin endpoints still fail closed when admin/server code is touched.
- Inference/sale counting semantics: state explicitly if anything changed and why.
- Deploy/VPS steps needed after merge (scripts to run with preview then --apply, config, Caddy).
- New dependencies, or "No new dependencies".

## Testing
What was actually run or checked (test suite, targeted tests, browser checks at desktop/phone and
dark/light). Only claim what was verified in this session or is visible from CI; otherwise say
what still needs checking.
```

## Rules

- Never add Claude attribution ("Generated with Claude Code", Co-Authored-By lines).
- Never include secrets, tokens or the ADMIN_TOKEN.
- Include a rationale for behavior changes (CLAUDE.md requires it), especially inference and ML changes.
- Keep it scannable: bullets over paragraphs; no filler.
- After the blocks, add at most one or two lines of advice (merge order, branch cleanup afterwards).
- Don't open the PR yourself unless the user asks; they create it on GitHub.
