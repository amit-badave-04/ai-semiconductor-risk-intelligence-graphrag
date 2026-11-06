# M5a small follow-ups after the decision-21 deploy (2026-11-06)

Status: approved by the owner ("fix the small follow-ups, then start m5b", 2026-11-06). Path: full (touches IaC, the Neo4j
`fly.toml`), kept to config and tests. Source: `docs/v2/M5_DECISIONS.md` 10.9, "Still open".

## Requirements

1. **One source for `ESCALATION_MODEL`.** `fly.toml [env]` pins it (with `ANSWER_MODEL`); `scripts/push_fly_secrets.py` no
   longer pushes it by default, and the Fly secret is unset (staged: it drops at the next deploy; the value is the same, so
   nothing changes for visitors).
2. **Neo4j stops cleanly.** `deploy/neo4j/fly.toml` (and the staging copy, which mirrors it) set `kill_signal = "SIGTERM"` and
   `kill_timeout = 120`, so a restart or a host move lets Neo4j finish its shutdown instead of the VM ending 5 s after the
   signal (the unclean stop of 2026-11-06, journal recovered on boot).
3. **`.env.example` documents `UPLOAD_MIN_MEM_AVAILABLE_KB`.**

## Security requirements and abuse cases (checkable)

- F1. No secret value is read, printed or committed: the push list changes by name only; the Fly unset is by name; the
  `.env*` files are not opened by the agent (a deny rule covers them).
- F2. No default-pushed secret is also pinned in `fly.toml [env]` (two sources of truth; which wins on Fly is not documented).
  (test)
- F3. The Neo4j change alters only the stop signal and timeout: env, mounts, checks, VM size unchanged. (review; the existing
  test that the staging and live Neo4j env tables are equal still holds)

## Acceptance criteria (tests first)

- `tests/test_push_fly_secrets.py::test_no_key_the_default_push_sends_is_also_pinned_in_fly_toml_env` (F2).
- `tests/test_staging_tomls.py::test_the_neo4j_definitions_give_the_database_time_to_stop_cleanly[live, staging]`.
- `tests/test_check_env_fly.py`: the overlap warning still fires for a made-up overlap (its old example, `ESCALATION_MODEL`, can
  no longer overlap); a copy of `ESCALATION_MODEL` left in `.env.fly` is listed as "not pushed".

## Status

| Stage | Gate | Evidence |
|---|---|---|
| 1 Spec | owner approves | the owner's instruction above |
| 2 Dev (TDD) | red then green | RED: the 3 new tests failed (`ESCALATION_MODEL` in both places; no `kill_signal` in either Neo4j toml). GREEN: `uv run pytest -q tests/test_push_fly_secrets.py tests/test_staging_tomls.py tests/test_check_env_fly.py tests/test_serve_guard_pepper.py tests/test_staging_script.py` = 334 passed (after rewriting the overlap test, which used `ESCALATION_MODEL` as its example) |
| Live | applied | `flyctl secrets unset ESCALATION_MODEL -a semigraph --stage` done (`check_env_fly` RESULT: PASS; the copy in `.env.fly` is now listed as not pushed). **Not applied:** the Neo4j stop config (the `flyctl deploy -c deploy/neo4j/fly.toml --image <current image>` was refused by the session's permission policy; the owner runs it, see `docs/RUNBOOK.md`). **Not done:** requirement 3 and deleting the `ESCALATION_MODEL` line from `.env.fly` (both files are behind a deny rule; the owner edits them) |
| 3-7 | suite, review, verifier, push | see below |
