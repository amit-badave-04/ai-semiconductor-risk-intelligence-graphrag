# Upload cap and projected-finish abort (after W1d; council 9)

Status: **APPROVED by the owner 2026-11-06** ("Approve council 9"). Path: full (upload input handling, the daily count in the
state store). Amends `docs/specs/upload-embed-timeout.md`, whose 2,100 s ceiling and 40-a-day rule stay.
Deploy (owner, 2026-11-07): held until the M5b I4 preview deploy, which ships this together with M5b I1; the live 1,200 s
upload ceiling stays until then. The build itself goes ahead once M5b I1 is pushed.

## Why

The confirmation boot W1d (`docs/v2/M5_DECISIONS.md` 10.11, evidence `docs/v2/research/m5-spikes/fly-2026-11-06/w1d`) ran the
2,100 s ceiling on a fresh shared-cpu-2x machine: the machine sustained a 10.1% burn share after its burst, the embed ran at
5.54 tok/s, and the 30-page fixture (14,151 tokens) stopped at the ceiling with 50 of 60 chunks done. At that rate a
16,000-token upload needs about 2,887 s, more than any ceiling the day rule allows at 40 uploads a day (2,160 s). The throttle
state of a machine is random (W1c finished the same fixture in 388 to 1,174 s). Council 9
(`docs/v2/research/m5-councils/council9/verdict.md`) chose: keep 2,100 s and 40 a day, cap uploads at a size the worst measured
rate finishes, and stop a doomed upload early instead of after 35 minutes. Performance-2x (+$51.53 a month) and off-machine
embedding are deferred until real-upload data shows a need.

## Requirements

1. **Cap.** `upload_max_tokens` defaults to **10,000** (from 16,000). At the worst measured rate, 5.54 tok/s, a 10,000-token
   upload embeds in about 1,805 s, inside the 2,100 s ceiling. The check stays where it is (after the parse, before any chunking
   or embedding: `uploads/jobs.py` `too_many_tokens`), so an oversized document does no embedding work. `upload_max_pages` stays
   30 (the token cap binds first for normal text). The production validator refuses a cap above 10,000 unless the ceiling
   covers it at 5.54 tok/s (`upload_max_tokens <= upload_embed_timeout_s x 5.54`, rounded down), so a later cap raise needs a
   matching ceiling and its own evidence.
2. **Projected-finish abort.** After each embedded chunk, from the third chunk on, the job projects its finish:
   `elapsed_embed + remaining_tokens / (embedded_tokens / elapsed_embed)`. If the projection is above the ceiling, the job stops
   at once with a new code `too_slow` (client text: "Uploads are slow right now. Please try again in a few minutes, or upload a
   shorter PDF.") instead of running to the ceiling. The projection uses the job's own measured rate only (no fixed figure).
   The first two chunks never abort (the rate is noise there).
3. **No charge for an abort.** A `too_slow` job gives back the visitor's daily upload count (`store.reserve_daily_upload` has no
   refund today: add `store.release_daily_upload`, idempotent per job, never below zero) and frees the slot as any failure does.
   The per-address upload window (10 an hour) is not refunded, as for every other refusal.
4. **Timing line.** The `upload stages` line gains the cgroup CPU counters where the kernel offers them
   (`/sys/fs/cgroup/cpu.stat`: `usage_usec`, `nr_throttled`, `throttled_usec`, read at the embed's start and end; absent fields
   are left out, never guessed) and the projected-finish decision (`projected_s`, `aborted=too_slow` when it fired).
5. **Copy.** The live page says "up to 30 pages" (`index.html:115`); it becomes "up to about 20 pages" (10,000 tokens is about
   19 to 21 pages at the measured densities). Public surfaces state the limit as a plain fact (strengths-only rule); the reason
   stays in internal docs. The limits the workspace API reports (`max_tokens_per_version`, `workspace_routes.py:133`) follow
   the setting, so they show 10,000.

## Security requirements and abuse cases (checkable)

- S1. The abort cannot be triggered by a visitor for anyone else: it reads only the job's own clock and token counts. (test)
- S2. A refund happens once per job and only for `too_slow`: a client cannot replay or forge it (no client input reaches it), and
  the count never goes below zero. (tests, both state backends)
- S3. The cgroup read takes no request data and opens only the fixed path; a missing or unreadable file never fails a job. (test)
- S4. The abort frees the slot exactly once and leaves no job, version or vectors behind (as `timeout` does today). (test)
- S5. No new information in responses: the client sees the fixed message, not rates or projections. (test)

## Acceptance criteria (tests first)

- A slow rate (fake clock) aborts within a few chunks with `too_slow`; a burst-then-throttle rate aborts mid-run; a normal rate
  completes; a rate exactly at the projection boundary does not abort (strict `>`); chunks 1-2 never abort.
- A `too_slow` job's daily count is refunded once (a second refund call is a no-op), never below zero; `timeout` and every other
  failure keep today's charge.
- The cap: 10,000 tokens accepted, 10,001 refused before chunking; the validator rule with its boundary.
- The timing line: with a fake cpu.stat the counters appear; without one they are absent and the job is unaffected.
- The page copy says "about 20 pages"; the node UI pins updated.

## Plan

One Sonnet worker (TDD) after M5b I1 lands (both touch `config.py`): `src/semigraph/config.py`, `src/semigraph/uploads/jobs.py`,
`src/semigraph/serve/store.py` (refund) and its state backends if the refund lives there, `src/semigraph/serve/static/index.html`
(copy), tests, `docs/RUNBOOK.md`. Then `ecc:code-reviewer` (sonnet), `ecc:security-reviewer` (opus), the full suite, the Opus
verifier, pre-push `/security-review` and gitleaks. Then one deploy (the owner's approval of council 9 covers it: "then deployed
with the 2,100 s ceiling"), held until the M5b I4 preview deploy (owner, 2026-11-07), which ships it together with M5b I1:
there is no separate deploy before then, and the live 1,200 s upload ceiling stays until then. The build itself goes ahead
once M5b I1 is pushed. The I4 deploy carries the 2,100 s ceiling (live on staging only until then, `docs/v2/M5_DECISIONS.md`
10.11): the image built first, the kill switch around it, live checks that are this spec's, including
`/api/workspace/limits` showing `max_tokens_per_version` 10,000 in the workspace limits, plus M5b's (`docs/specs/m5b-frontend.md`,
Acceptance, live checks). The I4 deploy also carries M5b I1's upload-sandbox hardening (commit 6, round 5): the compare wall
timeout is 110 s, below the 120 s CPU cap, and its Linux-only checks are in the same Acceptance list.

## Status

| Stage | Gate | Evidence |
|---|---|---|
| 1 Spec | owner approves | **Approved 2026-11-06** ("Approve council 9") |
| 2 Dev (TDD) | red then green; reviews | pending (after M5b I1 is pushed) |
| 3 Regression | full suite | pending |
| 4 Security | /security-review, gitleaks | pending |
| 6 Verifier | Opus | pending |
| 7 Push, CI, deploy | green; live checks | pending: push and CI after the build; the deploy is held until the M5b I4 preview deploy, which ships it with M5b I1 (owner, 2026-11-07), and the live 1,200 s ceiling stays until then; the live checks are this spec's plus M5b's |
