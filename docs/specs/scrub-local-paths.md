# Scrub absolute local paths from the public branch, and stop new ones (2026-11-06)

Status: requested by the owner (2026-11-06): replace each absolute local path in the current files with a repo-relative path
or a placeholder, fix the writers that recorded them test-first, add a guard, do not rewrite history. Path: fast, as the owner
declared it (docs scrub, 4 small writer fixes, tests). Note: 4 source files is one over the fast path's 3-file limit.

## Problem

Committed files on `v2` held absolute Windows paths that name the owner's user folder, and a Claude session-scratchpad id:
`artifacts/eval_report.agent.json`, `docs/RUNBOOK.md`, `docs/v2/M4_PLAN.md`, `docs/v2/M5_PLAN.md`, eight `docs/v2/research`
files, and the cell outputs of `notebooks/00_smoke_test.ipynb` and `01_edgar_acquisition.ipynb`. Not a secret; personal
information on a public surface. The writers that produced them: `tools/probe/w1d.py` (`stages.log_file`), `semigraph
eval-agent` (`baseline`: its default is resolved, so every default run wrote the user folder), and two latent ones,
`scripts/judge_acceptance.py` (`probes_file` given an absolute `--probes`) and `tools/loadtest/pool.py` (`source_files` for
a source outside the repository).

## Requirements

1. Each absolute path in the current files becomes repo-relative or a placeholder, meaning kept, nothing else changed:
   the repo root `<repo>`, the session scratchpad `<session-scratchpad>`, another temp folder `%TEMP%`.
2. A report writer names a file relative to its anchor when the file lies inside it, else by its file name alone: never
   absolute, never `../`. Anchors: w1d, the report's folder (`--out`'s parent, else the working folder); `eval-agent`, the
   repository; `judge_acceptance`, the working folder; the load-test pool, the repository (its in-repo form is unchanged, so
   `pool.json` still rebuilds byte-identically).
3. `tests/test_repo_no_local_paths.py` fails when any tracked text file holds a user folder (`C:\Users\<name>`, JSON-escaped
   too; `/c/Users/<name>`; the `C--Users-<name>` project slug), with a real name where `<name>` stands. Placeholders pass. It runs in CI's full `uv run pytest -q` job.

## Security requirements and abuse cases

- No report writer above can emit an absolute path or a `../` climb, whatever the arguments (different drive, path outside
  the anchor, file not yet created): each returns `relative_to(anchor)` or `.name`. Proven by one test per writer that
  feeds absolute tmp paths and asserts the tmp folder appears in no form (plain, `/`, JSON-escaped) in the written report.
- The guard reads tracked files only (no local ignored output can fail it), skips binaries and itself, and names
  `file:line` in its failure, never the matched text.

## Not done (owner's call)

- **History.** 16 commits on `v2` (the oldest 2026-07-02) still hold the paths; `master` holds them in 4 files
  (`.claude/settings.local.json`, `docs/RUNBOOK.md`, notebooks 00 and 01). A rewrite is the owner's decision.
- **`.claude/settings.local.json`** is tracked and holds the paths as live permission rules. The guard lists it as PENDING;
  the fix is to untrack it (`git rm --cached` plus a `.gitignore` entry), not to edit it.
- The `SEC_USER_AGENT` name and email in notebooks 00, 01, 02 and 12 (the SEC's required identity; already public as the
  commit author).

## Status

| Stage | Gate | Evidence |
|---|---|---|
| 2 Dev (TDD) | red then green | Guard RED before the scrub (16 lines, 14 files), GREEN after (13 passed). Writers: 6 new tests RED on the old code (absolute tmp paths; one `AttributeError` for the new `_REPO_ROOT` seam), GREEN after. Widened guard RED on exactly `.claude/settings.local.json` (3 lines), GREEN with it PENDING. |
| 3 Regression | full suite green | `pytest -q -p no:cacheprovider` with `PYTHONPATH=src` on `ea907c7`: **9787 passed, 473 skipped, 1 failed**. The failure is `test_the_new_text_files_are_ascii_with_lf_line_ends`, an artifact of a fresh worktree under a system `core.autocrlf=true` (CRLF working copies, untouched files too); the committed blobs of every file it names are ASCII with LF (checked with `git show HEAD:<file>`). |
| 4 Security | gitleaks, /security-review, semgrep | `gitleaks git --log-opts=origin/v2..HEAD`: no leaks (3 commits). `semgrep scan --config p/python --config p/security-audit --baseline-commit origin/v2 --error`: 0 new findings, exit 0. /security-review (Opus) over `origin/v2..HEAD` (`a554ed4`, `ea907c7`, `ac8c008`): no HIGH or MEDIUM finding; every added line and commit message is free of the user folder, slug and session id; the helpers only build strings for JSON output (nothing opens a computed path). Below the bar: a run anchored at a drive root would record `Users/<name>/...`, which the guard does not match (an odd, trusted CLI choice). No lockfile or IaC change. Commit messages and the two `.docx` files: no user folder or session id. |
| 6 Verifier | Opus | PASS WITH CONDITIONS. Scrub keeps meaning; notebooks and the artifact stay valid JSON; no writer can emit an absolute path or `../`; the new tests fail on the old code. Findings: HIGH `.claude/settings.local.json` (owner's call, above); MEDIUM guard scope too narrow (fixed in `ac8c008`); LOW backslash vs `/` separators in old artifacts (a one-line diff at the next regeneration); LOW `judge_acceptance` names a probes file outside the working folder by its name alone (identity stays pinned by `probes_sha256`). |
| 7 Push, CI | owner approves | PENDING |
