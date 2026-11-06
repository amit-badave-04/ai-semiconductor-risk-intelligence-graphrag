# Upload embed timeout sized from the measured CPU throttle (M5a follow-up to decision 21)

Status: **APPROVED by the owner 2026-11-06** (decisions 1 and 3-6 as recommended: ceiling 2,100 s, the confirmation boot after stage 6, keep shared-cpu-2x, defer H and B), revised 2026-11-06 after two adversarial Opus reviews (see
"Review disposition"). Path: full (changes a resource limit on the upload job loop and the upload sweeper's age rule, both on the sensitive
list: input handling and the upload pipeline).

## Why

Window W1c (2026-11-06, `docs/v2/research/m5-spikes/fly-2026-11-05/w1c`) ran the same 30-page fixture
(`tests/upload_fixtures.py:231`, `max_size_pdf(30, 2700)`, 60 chunks live) on three fresh shared-cpu-2x 4 GB machines with the
fp32 embedder. The job reached `ready` at **387.6 s, 682.0 s and 1,174.3 s** (`w1c/w1c.json`; `w1c/boot-1.json` `job.ready_s`),
a 3x spread on identical machines. `ready_s` is timed from upload accept (`tools/probe/w1b.py:598`), so it includes parse, chunking,
compare and indexing. Compare is about zero for these jobs (a first version in a fresh workspace returns "not compared" without a
subprocess, `jobs.py:373-380`); the other three are unrecorded. W1b boot 2's two failures bound parse, chunking and the last-chunk
overrun together at under about 38 s and 62 s (`workload_s` 1,268.4 and 1,291.6, minus the 30 s tail and the 1,200 s budget, before
an unrecorded overhead of a few seconds; **inferred**). The 1,200 s embed budget (`upload_embed_timeout_s`,
`src/semigraph/config.py:302`) starts at the embed loop. Boot 1 therefore left the embed stage a margin of at least 26 s (the figure
`docs/v2/M5_DECISIONS.md` 10.8 gives, there measured from accept), and a little more by the pre-embed time (requirement 3 records
it). In W1b the same job failed with code `timeout` on boot 2, twice, under about 150 asks (`w1b/boot-2.attempt1-invalid.json`,
`...attempt2-invalid.json`; boot 1 with 118 asks was ready at 842 s, `w1b/boot-1.json`). `docs/v2/M5_DECISIONS.md` 10.8 and 10.9
record the finding and say it "needs its own spec"; council 8 (`docs/v2/research/m5-councils/council8/verdict.md:41`) asked for the
cause. This is that spec.

**Cause (the leading explanation, not isolated by a no-ask boot): the timeout has no margin against what the throttled machine
delivers.** A cap-size job (16,000 tokens, `config.py:293`) is 186.5 s of one performance-class core. After the burst balance is spent
a shared-cpu-2x gives a fraction of one shared core; at the measured 12 to 12.5% the cap job needs about 1,140 to 1,320 s
(**inferred**, table below), against a 1,200 s budget that was set without that fraction in the sum. The confirmation boot in Plan 2
tests this.

Inputs (all from files or the one page named; where readings disagree the spec keeps all of them):

- Full-core throughput **85.8 tok/s**: `w1/rss_fp32.json` `timing.passages` (54 chunks, 15,849 tokens, 184.8 s, `threads` 1; 293
  tokens a chunk on average). The run was on **performance-2x:4gb**: recorded at `artifacts/m5_spikes.json:194` (the figure is at
  `:402`); the file itself records only 2 CPUs and 4 GB.
- Fixture size: 60 chunks (`chunks_total`, every W1b and W1c boot); tokens not recorded. Upper bound: the job was accepted, so at
  most 16,000 tokens (`upload_max_tokens`), at most 266.7 a chunk, at most **186.5 core-s** (16,000 / 85.8). The 85.8 run's 3.30 s
  median and 3.42 s mean chunk times (184.77 / 54) come from larger chunks, so 60 x 3.30 = 198 s and 60 x 3.42 = 205 s are not
  bounds (205 s is above the cap). Lower reconstruction: 13,923 tokens, 162.3 core-s (the diagnosis's local figure, **not re-run
  here**; Plan 0 settles it).
- Class factor f. The same W1 run timed the embedder on a fresh shared-cpu-2x and on performance-2x: fp32 query median 0.334 s
  against 0.437 s (`w1/t_fresh.json`, `w1/t_perf.json`: 1.31x), q8 1.192 s against 1.409 s (1.18x). A fresh shared core is faster
  per query than the performance core the 85.8 tok/s came from, and the shares below are fractions of the shared core, so
  throttled time = performance-core seconds / (f x share), with **f = 1.18 to 1.31** (**inferred range**: query ratios may not
  carry over to passages of about 290 tokens). The table's first column (f = 1, no correction) is an upper bound, not a case.
- Sustained share after the burst: **17%** (`w0/report.txt`: one burn thread, throttled from second 115) and, from the same run,
  **14.6%** median share after 300 s (`artifacts/m5_spikes.json:437`); **12.1% and 12.0%** (`w1/t_fresh.json` against
  `w1/t_drained.json`: the fp32 per-question median went 0.334 s to 2.759 s, the q8 median 1.192 s to 9.958 s, ratios 8.27 and
  8.35; the share is the inverse, **inferred**); **12.5%** from Fly's page `docs.fly.io/machines/cpu-performance` (fetched
  2026-11-06; the page shows no date): 5 ms per 80 ms for each shared vCPU, the quota shared between a machine's vCPUs, 10 ms per
  80 ms for shared-cpu-2x. The page does not print 12.5%; 10 / 80 gives it (derived). It also gives an initial burst balance of 5 s
  (maximum 500 s) and is silent on whether a new machine resets it.

The cap job (16,000 tokens, 186.5 core-s) at each sustained share, in seconds (it bounds the fixture too; the fixture's own time is
no higher):

| Sustained share | f = 1 (upper bound) | f = 1.18 | f = 1.31 |
|---|---|---|---|
| 17% | 1,097 | 930 | 837 |
| 14.6% | 1,277 | 1,082 | 975 |
| 12.5% | 1,492 | 1,264 | 1,139 |
| 12.0% | 1,554 | 1,317 | 1,186 |
| 10% | 1,865 | 1,580 | 1,424 |
| 8% | 2,331 | 1,975 | 1,779 |
| 6% | 3,108 | 2,634 | 2,373 |
| Share at which a 2,100 s ceiling just suffices | 8.9% | 7.5% | 6.8% |

Reading it:

- To finish the cap inside 1,200 s a job needs a sustained share of at least 15.5% (f = 1), 13.2% (1.18) or 11.9% (1.31)
  (16,000 / (85.8 x f x 1,200)). At 12 to 12.5% the cap needs 1,139 to 1,317 s: from 5% under to 10% over the budget. At 10% it
  needs 1,424 to 1,580 s, 19% to 32% over. The budget is marginal, not generous: it has no room for a slower boot.
- The M4 plan predicted the throttle and set the budget anyway. Risk 13 (`docs/v2/M4_PLAN.md:490-497`) says a max upload throttled
  from the start would take about 25 minutes (190 s / 12.5% = 1,520 s) and names the 1,200 s budget as the response, so a job
  finishes rather than fails; line 114 calls the budget generous on purpose. M4's 190 s is a local desktop CPU figure on the q8
  model (`M4_PLAN.md:505, 515`: "~190 s of one core for 16,000 tokens"), taken before any Fly window, so no Fly class factor
  applies to it. From this spec's inputs (186.5 performance-core s at the cap, f = 1.18 to 1.31) the same 12.5% gives 1,139 to
  1,264 s (table). The 1,200 s budget sits inside that range: the margin the word "generous" implies was never in the sum.
- **The ceiling rule.** A ceiling must be at least 1.5x the cap job's time at the lowest sustained share measured after the burst
  (12%): 1,975 s at f = 1.18, 1,779 s at 1.31 (2,331 s at f = 1). It must also keep the embed ceilings in proportion to the day:
  ceiling x 40 uploads a day <= 86,400 s, so at most 2,160 s. That is a check on the embed ceiling alone. Parse (up to 90 s,
  `config.py:300`) and the last chunk (up to about 70 s) add up to about 160 s a job on top, which the one-at-a-time slot absorbs
  (S3); the rule does not keep total slot time under 24 h. **Every ceiling from 2,000 s to 2,160 s meets both under both
  factors.** This spec takes **2,100 s**: 1.59x (f = 1.18) and 1.77x (1.31) at 12%, 23.3 h of embed ceiling a day (S3). At f = 1 it
  is 1.35x; f = 1 is not a measured case (W1 shows the shared core faster), and the 2,331 s it would need fails the embed-ceiling
  day check (93,240 s).
- Council 8 reconciliation. Council 8 (`verdict.md:27`) found the throttle explanation did not add up: the probe README's pre-run 190 CPU-s
  (`tools/probe/README.md:132`; the same figure as M4's desktop q8 spike, `M4_PLAN.md:505, 515`) at W0's about 15% is about 1,270 s with no asks, yet W1b boot 1 was ready at 842 s with 118
  asks. Re-based on this spec's inputs the gap closes: the cap job's 186.5 performance-core s / (1.18 to 1.31) / 0.17 = 837 to
  930 s (949 to 1,054 s at 15%), and the fixture's lower reconstruction (162.3 s) gives 729 to 809 s at 17%; 842 s falls inside
  the cap-job range, at its low end, and above the lower one. The 118 asks' embedding (about 39 shared-core-s) is not in that sum
  and boots differ in how much burst they have left, so the match is approximate (**inferred**). W1c boot 1 (1,174 s)
  implies an average share of about 10.5% to 13.5% over the whole job (token total 13,923 to 16,000, both factors; **inferred**),
  close to the 12.5% pooled baseline from near the start. Boots 2 and 3 average about 18% to 23% and 32% to 41% on the same basis.
  Boot 1's low end is a measured share below the 12% the ceiling rule uses, and it changes nothing: scaled to the cap, boot 1 is
  at most 1,174.3 x 16,000 / 13,923 = 1,349 s (whole-job time, so an upper figure for the embed), which 2,100 s covers at least
  1.55x (1.556x).
- The documented 5 s initial balance does not explain boots 2 and 3, nor W0's roughly 115 s at full speed on a fresh machine
  (`artifacts/m5_spikes.json:440`). How much burst a fresh machine has looks contention-dependent (**inferred**). The spread is the
  point: upload time on this class is not predictable from one boot, so the ceiling has to cover the slow end, and a test of the
  ceiling has to check that its boot was in fact throttled (Plan 2).
- Asks: second order at the live spend cap, a co-factor at saturation. The $10 a day cap admits about 14 hybrid asks a day
  (`council8/verdict.md:24`); 14 paced asks cost about 4.7 shared-core-s of embedding (14 x 0.334 s, `w1/t_fresh.json`), 3% to 4% of
  the fixture's 124 to 158 shared-core-s (13,923 to 16,000 tokens, f = 1.31 to 1.18). The saturating W1b load (about 150 asks)
  costs about 50 shared-core-s, 32% to 40% of that, and it coincided with two failures. An ask's non-embedding CPU is not measured. W1b boot 1 (118 asks, 842 s) was faster than W1c
  boot 1 (14 asks, 1,174 s) on a different machine, so the asks do not predict upload time on this evidence. The job runs at nice 10
  (`uploads/jobs.py:298-308`, Linux only), which decides who runs inside the VM, not how much quota the VM has (**inferred**).
- A timeout throws all of the work away: nothing is written until `put_version` at indexing (`uploads/jobs.py:462-467`, `505-511`).
- Not changed by anything below: a deploy during an upload. The drain ceiling is 254 s (`config.py:89`), shorter than any
  measured upload, so the job ends `interrupted` at the next start (**inferred** from the sweeper's start pass, `jobs.py:615`).

### Options considered

| Option | Verdict |
|---|---|
| A. Size the ceiling from the arithmetic (1,200 s to 2,100 s), validate it, log stage timings | **This spec.** $0, one default, a validator and a log line, reversible by config |
| B. `ONNX_THREADS=2` (`fly.toml:29`) | Not pursued (see "Not in this spec") |
| C. Lower the caps (tokens 16,000, pages 30) | Reject: halves the product to hide a mis-sized timeout; a 15-page upload can still crawl |
| D. True batching in `encode_passages` (`embeddings_onnx.py:163-165`) | Reject: needs padding for last-token pooling, fidelity re-verification on the schema-locked index, small gain on one thread |
| E. Pause uploads during asks | Reject: no effect on upload time (already nice 10), only makes uploads slower under load |
| F. `performance-2x:4gb` for the serve machine | A priced owner option, not recommended now: +$51.53 per 30 days ($83.77 against $32.24, `windows.json` rates). The embed is 186.5 s of core time at the cap whatever the balance (85.8 tok/s is recorded on that class). Owner decision 4 |
| G. A separate always-on upload worker machine | Reject: removes ask contention, which is not the leading cause; still throttled; +$32.24 per 30 days, a hand-off design, a second 2.3 GB model copy |
| H. Resume partial work after a failure | Deferred, and it does not work as first proposed (see "Not in this spec"). Owner decision 5 |
| I. A per-chunk time limit | Dropped on review (see "Not in this spec") |

## Requirements

1. **Ceiling.** `upload_embed_timeout_s` (env `UPLOAD_EMBED_TIMEOUT_S`) default 1,200 becomes **2,100**. Semantics unchanged: checked
   after each chunk, only while chunks remain (a job that has embedded its last chunk is never failed), measured from the start of
   the embed loop (`jobs.py:351`, `358`). The config comment states the derivation: cap tokens / (full-core tok/s x class factor x
   lowest measured share) x 1.5 = 16,000 / (85.8 x 1.18 x 0.12) x 1.5 = 1,975 s, rounded up to 2,100 s (a cap job still finishes at a
   sustained share down to 7.5%), and the embed-ceiling day check 2,100 x 40 = 84,000 s <= 86,400 s (requirement 5).
2. **Injectable clock and a stage record.** `_process` gains a keyword-only `clock=time.monotonic` and passes it to
   `_chunk_and_embed_stage` and `_embed_chunks` (which gain it too, keyword-only), which use it for the loop's elapsed time and for
   the stage timings. `_process` also creates one small dict, `stages`, and passes it down with the clock (keyword-only, default
   `None`: a function called without it keeps a local one). Each stage writes its own entries into it as it ends: seconds for
   `parse_s`, `chunk_s`, `embed_s`, `compare_s` and `index_s` (`_process` times the parse, compare and index calls itself), and
   for the embed stage the counts `fresh`, `done` and `tokens` (requirement 3). A stage that returns `None` or the bare
   `"timeout"`, or raises, has already written what it reached (the duration from a `finally` of its own, the counts per chunk),
   so the `finally` in `_process` writes the line from the dict and nothing travels through a return value. Production passes
   nothing; existing positional callers are unchanged.
3. **Stage timings, log only.** One INFO line, written from a `finally` in `_process` that opens once `_parse_stage` has returned
   a result, for every job that got that far and ended `ready`, `too_many_chunks`, `workspace_quota` (here only the embedded-token
   budget: `_embed_chunks` returned `None`, `jobs.py:460`), `timeout`, `workspace_deleted` or `internal_error`:
   `upload stages ws_hash=... job_id=... parse_s=... chunk_s=... embed_s=... compare_s=... index_s=... chunks=... fresh=... done=... tokens=...`
   (stages not reached are omitted with their field). No line is written when `_parse_stage` returns `None` or raises: a parse
   failure writes only the existing failure line, and so do `too_many_pages`, `too_many_tokens` and the page-quota
   `workspace_quota` (`jobs.py:428-437`: the same code as the embedded-token exit, but from the parse stage). `chunks=` is the
   number of chunks cut; `fresh=` the number of them to embed (not reused); `done=` the number embedded when the loop ended, so
   `done` below `fresh` marks a stop inside the loop (the failure line with the same `job_id` names the exit); `tokens=` is the
   PLANNED freshly embedded tokens, `new_tokens` of the existing `upload embed` line (`jobs.py:345`, `361`): reused chunks are
   not counted, on a first version it equals all chunk tokens, and on a `timeout` it is still the plan, not what was embedded, so
   a share is computed from a `ready` line only (Plan 2). The existing `upload embed` line stays; the new line adds the other
   stages and the failure exits. It is absent from every client event and every persisted job payload. This closes the gap that
   parse, chunk and index wall times are unrecorded; the confirmation boot (Plan 2) collects it from the machine's logs.
4. **Sweeper age.** The formula stays (`jobs.py:604-609`: parse + embed ceiling + `FAIL_INTERRUPTED_STAGE_MARGIN_S` 600 s), which
   gives **2,790 s** with the new default (1,890 s today). `repo.FAIL_INTERRUPTED_AFTER_S` (`repo.py:153`, the default for a caller
   without settings: 35 x 60 = 2,100 s, with a comment that hard-codes 1,200 s) becomes 50 x 60 = 3,000 s, and its comment and the
   comments at `jobs.py:591-600` and `config.py:302-303` are corrected. This is a consistency change, not a safety property: the age
   compared is the time since the job's last `put_job` write (`repo.py:118`, `j.updated_at < $threshold`), this process's open jobs
   are excluded (`repo.py:740`, `jobs.py:607-608`), and the sweeper's start pass uses age 0 (`jobs.py:615`), so on one machine the
   threshold applies to another process's jobs only. The embed stage ends at the ceiling plus the longest single chunk call (about
   70 s at the ceiling's floor share: 2,100 x 512 / 16,000 = 67 s, and the largest measured chunk, 461 tokens in 5.59 s
   (`w1/rss_fp32.json` records `largest_chunk_tokens` and `max_s` separately, so the pairing is **inferred**), scales to about
   70 s (5.59 x 512 / 461 / (1.18 x 0.075)); unbounded if a native call hangs, see "Not in this spec"), then compare (up to
   120 s, `config.py:301`) and `put_version` (no setting-backed bound): inside the 600 s margin unless a call hangs.
5. **Production validator.** In production only, `UPLOAD_EMBED_TIMEOUT_S` must be at least 600 and its product with the daily
   upload count (`max_uploads_per_day`) at most 86,400 (2,160 s at 40 a day). The floor stops a zero, a near-zero value or a unit
   slip (35 meaning minutes) from booting. It is not a throttle margin: any value from 600 up to about 1,186 s is accepted and
   fails a cap job at the 12% share under either factor (up to about 1,317 s under f = 1.18; table: 1,186 to 1,317 s), and any
   value under about 1,975 s breaks the 1.5x rule (the f = 1.18 design value; 1,779 s at 1.31); only the machine's share can
   judge those, so they are tuning, which the RUNBOOK paragraph (requirement 8) says. The product rule is
   what closes owner decision 2: a ceiling or a daily count raised until ceiling x count passes one day refuses boot. It checks
   the embed ceiling alone; parse and the last chunk add up to about 160 s a job, which the one-at-a-time slot absorbs (S3), so it
   is a proportion check, not a bound on slot time. `max_uploads_per_day` is not itself bounded in production (it is not in
   `PRODUCTION_COUNT_CEILINGS`, `config.py:49-53`), so the rule bounds the product, not either factor. Outside production any
   positive value is allowed (tests need tiny ones; `tests/integration/test_workspace_leak_neo4j.py:318` builds a real `Settings`
   with 60). A refusal names only `UPLOAD_EMBED_TIMEOUT_S` (the daily count is described in words, not by name) and never a value.
   The pre-deploy check takes each problem's name from its first word (`scripts/check_env_fly.py:53`), so an operator who raises
   `MAX_UPLOADS_PER_DAY` sees a FAIL that names `UPLOAD_EMBED_TIMEOUT_S`; the product-rule message therefore says in words that
   the daily upload count is part of the cause. `CHECKED_SETTINGS` (`config.py:90-98`) lists every setting a refusal can name,
   so `UPLOAD_EMBED_TIMEOUT_S` is added to it (after `UPLOAD_MIN_MEM_AVAILABLE_KB`) and to `PINNED`
   (`tests/test_serve_config_production.py:43`), with a pre-deploy test patterned on `tests/test_check_env_fly.py:166-175` (the
   PASS-list test at `:99` follows `CHECKED_SETTINGS` by itself).
6. **Comment fix.** The `max_uploads_per_day` comment (`config.py:303`, "40 x ~190 CPU-s stays under the shared-cpu-2x baseline")
   also states the slot-time figures of S3 (embed ceiling x 40 = 23.3 h; about 25 h with parse and the last chunk, which the
   one-at-a-time slot absorbs), because CPU-seconds per completed job do not change but slot wall time does.
7. **Unchanged:** every cap (`config.py:291-299`), the parse and compare timeouts, nice 10, `embed_slots`, `ONNX_THREADS`, the
   machine class, the `progress` payload (`done`, `total`, `eta_s`), the page, every gate and the gate order, and the failure code
   and message of a ceiling stop.
8. **Docs.** `docs/RUNBOOK.md` gets one paragraph: the setting, the derivation and the validator, and how to read the `upload stages`
   line; it also says that the validator does not enforce the 1.5x rule (a ceiling under about 1,975 s is accepted and can fail
   throttled uploads). `.env.example` is read-denied in the agent session: if it lists `UPLOAD_EMBED_TIMEOUT_S` at 1200 the owner updates that line
   (stage-2 note).

## Security requirements and abuse cases (checkable)

- S1. No request data reaches a file path, and the change adds no file I/O: the ceiling is an integer read from the environment at
  boot, never from a request. (test: the embed loop and the stage-line code run with `builtins.open` patched to raise; review item:
  no new `open(`, `Path(` or `os.` call)
- S2. No new information in any response. The `failed` event for a ceiling stop is byte-identical to today's `timeout` event;
  `progress` keeps exactly `done`, `total`, `eta_s`; the limit, shares, stage times and the cause never appear in an event, a
  response body or a persisted job payload. (tests: the failure event equals one constant dict; the serialized events of a failed
  and a ready job contain no `_s` key other than `eta_s` and no `stage`)
- S3. No new way to starve other visitors' uploads or asks.
  - No new gate, route or setting reachable from a request; the gate order is untouched (the memory-guard S5 order tests stay
    green).
  - Asks: the ask path is untouched (`serve/embed.py:58-75`, the shared embed slot); passages still bypass the slot
    (`embed.py:77-81`) and still run at nice 10; the loop's bookkeeping is the one clock read per chunk it has today
    (`jobs.py:354`). (review item)
  - Upload slot: it is one global slot (`workspace_routes.py:106`, `DrainCountedSlot(1)`, `serve/main.py:313`). The worst hold of a
    job that is slow and still progressing rises from 1,200 s + one chunk to 2,100 s + one chunk (about 70 s). A job stopped at
    the ceiling also holds the slot for its parse (up to 90 s, `config.py:300`), its chunking (unrecorded) and its last chunk, so
    up to about 2,260 s a job. Daily, 40 uploads (`config.py:303`) if every job runs to the ceiling and fails: 40 x 2,100 s =
    84,000 s = **23.3 h** of embed ceiling alone (today's equivalent: 40 x 1,200 s = 13.3 h), and about 40 x 2,260 = 90,400 s =
    25.1 h with parse and the last chunk. That is more than the day holds, and the slot absorbs it: uploads run one at a time, so
    the later ones are refused with a 429 `busy` (`workspace_routes.py:404-408`), much as when one visitor spends the cap today. A
    realistic cap job at the 12% share takes 1,186 to 1,317 s of embedding, so 13.2 to 14.6 h of embed time. Requirement 5's rule
    (ceiling x daily count <= 86,400 s) is a check on the embed ceiling alone: it keeps the ceiling and the count in proportion
    and does not claim to keep total slot time under 24 h. The denial exists today: one visitor can spend the whole 40-a-day cap
    (10 an hour per address, `config.py:305`; faster with several addresses) and every other upload is refused for the rest of
    the day. This change lengthens how long the slot can be held (to about 2,260 s for a job stopped at the ceiling (from about
    1,360 s), plus its unrecorded chunking; a job whose last chunk crosses the ceiling and completes holds it further through
    compare (up to 120 s, `config.py:301`) and `put_version` (requirement 4)), not who can reach it. **Owner decision 2 is closed.** (review item plus the arithmetic above)
- S4. Timeouts never leave an orphaned job or slot. Every exit of the embed stage ends in `ready`, `failed` or the existing
  `internal_error`; `_worker`'s `finally` releases the upload slot exactly once (`jobs.py:563-564`; `BoundedSemaphore` raises on a
  double release); the registry marks the job terminal; the terminal write keeps its retries (`jobs.py:232-263`); after a timeout
  no `put_version` runs, nothing is stored and the workspace's `embedded_tokens` is not bumped; the stage line's `finally` in
  `_process` cannot skip or double the slot release (it runs inside `_process`, before `_worker`'s `finally`). The sweeper default
  stays above the stage budgets (requirement 4; consistency only). (tests, below.) A native call that never returns is not
  covered: it is unbounded today and stays so (see "Not in this spec").
- S5. Cost caps. The path makes no provider call and spends no money (local embedding). CPU per completed job is unchanged (the
  same chunks); a failing job is bounded by the ceiling (one thread, nice 10); the daily cap of 40 is unchanged. Any staging
  window below has a quote and a cap and runs only on the owner's go. (review item)
- S6. In production the setting cannot be set to zero, to a near-zero value (under 600 s) or to a ceiling whose product with the
  daily count passes one day: those refuse boot (requirement 5), and the refusal never echoes the value. The validator does not
  protect throttled uploads: a value from 600 up to about 1,186 s is accepted and fails a cap job at the 12% share under either
  factor (up to about 1,317 s under f = 1.18), and one under about 1,975 s breaks the 1.5x rule (f = 1.18; 1,779 s at 1.31); that
  is tuning, stated in the RUNBOOK (requirement 8). (tests)
- S7. Logs carry no IP, IP hash, workspace id, file name or upload content: the stage line and the failure lines use `ws_hash`,
  `job_id` and counts only, the same fields as `jobs.py:288` and `361`. (test: captured lines match an allow-listed format)
- S8. LLM-specific cases (direct and indirect prompt injection, PII leakage, tool or agent misuse): not applicable, with the
  reason recorded: this change touches no prompt, tool or agent; uploaded text is embedded locally as today and the change adds
  no retained user-derived data (that is why H is deferred).

## Acceptance criteria (tests, written first)

**How a throttled CPU is simulated.** Two test doubles, no sleeping and no real CPU: a fake clock (a callable returning a float)
and an embedder whose `encode_passages([text])` advances that clock by `core_seconds(text) / share(now)`. `core_seconds` is
`tokens / (85.8 x 1.18)`: the conservative class factor (1.18 gives the longer time), fixed for every test. `share(now)` is a
constant (0.17, 0.12, 0.10, 0.08, 0.06) or 1.0 until a burst end (115 s, `w0/report.txt`) and a constant after. The clock is passed
as `jobs._process(..., clock=fake)`; worker-level tests wrap `_process` with `functools.partial` through a `monkeypatch` of
`jobs._process`. The global `time.monotonic` is never patched, and a 1,580 s job runs in milliseconds on any OS. The cap job is
16,000 tokens as 40 chunks of 400 tokens.

1. Cap job from the start of the throttle: `ready` with ceiling 2,100 at 17% (930 s), 12% (1,317 s), 10% (1,580 s) and 8%
   (1,975 s); `timeout` at 6% (2,634 s), stopped by the ceiling with chunks remaining (below the design floor, documented). The
   contrast case at 10% with ceiling 1,200 (the old behaviour): `timeout`. The contrast sits at 10% because it times out under
   either class factor (1,424 and 1,580 s), where 12% flips with the factor (1,186 s at 1.31 would pass).
2. Boundary, with integer clock steps (exactly representable): chunks that each advance the clock by 700 s, ceiling 2,100: after the
   third chunk the elapsed time equals the ceiling and the job goes on (strict `>`); after the fourth it stops with chunks
   remaining.
3. Last chunk: a job whose final chunk crosses the ceiling still completes (no chunk remains).
4. S2: the ceiling failure event equals one constant dict
   `{job_id, state: failed, document_id, version, error: {code: timeout, message: "processing took too long and was stopped"}}`;
   every progress event has exactly `done`, `total`, `eta_s`.
5. S4 through `_worker` called directly: after the failure `put_version_calls == []`, the last persisted state is
   `failed`/`timeout`, `upload_slots.released == 1` (and the drain count is back to 0 with `DrainCountedSlot`), and
   `registry.is_terminal`; an embedder that raises inside the loop still ends `internal_error` with the slot released once and the
   stage line written.
6. Stage line (S7): one INFO line per job whose parse stage returned a result, including `timeout`, `workspace_deleted`,
   `internal_error`, `too_many_chunks` and the embedded-token `workspace_quota` exits; none for a parse failure, `too_many_pages`,
   `too_many_tokens`, the page-quota `workspace_quota` (`jobs.py:432`) or an exception raised inside the parse stage; the fields
   match an allow-listed pattern; with the fake clock the stage values equal the simulated durations (a stage whose double does
   not advance the clock reads 0.0), and they survive the three early exits through the `stages` dict: `too_many_chunks` has
   `chunk_s` and no `embed_s`, a ceiling stop after 2 of 40 chunks has `embed_s`, `fresh=40`, `done=2` and `tokens=` equal to the
   plan (16,000, not the embedded 800), and an embedder that raises on the third chunk ends `internal_error` with `done=2` and
   `embed_s` present; `tokens=` equals the planned freshly embedded tokens (a second version that reuses chunks logs fewer, with
   `fresh` below `chunks`); no workspace id, IP, file name or text.
7. Sweeper (S4): `jobs._fail_interrupted_older_than_s(settings)` = parse + ceiling + 600 = 2,790 at the defaults;
   `repo.FAIL_INTERRUPTED_AFTER_S` >= 2,790 and above parse + ceiling (the shape of `tests/test_serve_upload_repo.py:745`); the
   existing relation test (`tests/test_serve_upload_jobs.py:903-907`) stays green.
8. Config: default 2,100 (`tests/test_serve_private_state.py:57` is updated from `(90, 1200, 120)`, a deliberate RED on the default
   change); env override; the production validator at and beyond each bound (599 refused, 600 accepted; 2,160 accepted and 2,161
   refused at 40 a day; the default 2,100 refused with a daily count of 42, 88,200 > 86,400); 1,000 accepted at 40 a day (the
   validator enforces no throttle margin, S6); the rule not applied outside production; the value never in the message (S6); the
   product-rule message starts with `UPLOAD_EMBED_TIMEOUT_S` and says in words that the daily upload count is part of the cause;
   `UPLOAD_EMBED_TIMEOUT_S` in `CHECKED_SETTINGS` and `PINNED`, and the pre-deploy check failing on a refused value by that name
   only.
9. No new setting, so no settings fake needs a new attribute. `FakeSettings` (`tests/test_serve_upload_jobs.py:26-36`) takes the
   new default 2,100 so it matches. `tests/test_serve_upload_repo.py:745` (`Settings(_env_file=None)`) goes red on the default
   change alone (90 + 2,100 = 2,190 is above today's repo default of 2,100) until `repo.py` changes: a deliberate RED.
   `_FakeJobSettings` (`tests/integration/test_workspace_repo_neo4j.py:458-466`) keeps 1,200: its test exercises a deleted
   workspace, not timing, and is untouched.
   `tests/integration/test_workspace_leak_neo4j.py:318` builds a real `Settings` with 60 outside production and is untouched.
10. **No test covers the `"timeout"` path of `_embed_chunks` today.** `tests/test_serve_upload_jobs.py` has two `_embed_chunks` tests
    (reuse, workspace budget: lines 549 and 564) and the only other `timeout` tests there are the comparison ones. Every timeout
    test above is new. RED is run against the unchanged code (items 1 to 3 and 5 against its 1,200 s loop).
11. Regression gate: the full suite and the serve-shipped selection green, coverage of the changed modules at least 80%, the
    memory-guard order tests unchanged. Mutations for the verifier: ceiling `>` to `>=`; the `remaining` guard removed; a different
    message for the ceiling stop; the validator floor and the product rule; the sweeper age formula; `ws_hash` replaced by the
    workspace id in the stage line; the stage line written outside the `finally` (it would miss `internal_error`); the stage
    entries written only on a normal return (the early-exit lines would lose `embed_s`); `done=` read as the plan.

## Plan

**0. $0 local work, after approval, no paid call, no Fly, no production Neo4j.**
(a) Run the fixture through the real parse and `chunk_units` path and record chunks and tokens (live: 60 chunks, tokens unrecorded;
this settles 13,923 against 16,000); the parse is a Linux sandbox, so on this Windows machine that means WSL Ubuntu (the memory-guard
verifier used Ubuntu-24.04) or the Linux CI job. (b) Time the local **fp32** embed (the shipped model since v15, `fly.toml:28`) over
those chunks to check the shape of the inputs (core-seconds per chunk against tokens); a desktop CPU is not a Fly core, so only
ratios transfer, and 85.8 tok/s stays the Fly figure. (c) Put the arithmetic table into the config comment (requirement 1).

**1. One Sonnet worker (TDD) owns:** `src/semigraph/uploads/jobs.py` (the clock, the stage line, the comments),
`src/semigraph/config.py` (the default, the validator, `CHECKED_SETTINGS`, comments), `src/semigraph/uploads/repo.py` (the default
and its comment only), the tests named in the acceptance criteria (a new `tests/test_serve_upload_embed_timeout.py`, plus
`tests/test_serve_private_state.py`, `tests/test_serve_config_production.py`, `tests/test_check_env_fly.py`,
`tests/test_serve_upload_repo.py` and `tests/test_serve_upload_jobs.py`), and `docs/RUNBOOK.md` (one paragraph). Then
`ecc:code-reviewer` (sonnet), `ecc:security-reviewer` (opus; the change is on the sensitive list), the full suite, the Opus verifier.
Stage 4 per the SDD rule (`/security-review` and gitleaks before the push; `differential-review`, semgrep and
`spec-to-code-compliance` at merge time). `fly.toml` is untouched (the code default is inside the production bounds). It ships in
the next deploy; this spec does not deploy.

**2. Staging, optional, only on the owner's go and a quote (Owner decision 3).** Council 8 item 3 (`verdict.md:41`) asked for one
no-ask boot each on q8 and fp32, started after the burst is used up. This spec narrows that to one fp32 boot (fp32 shipped in v15)
and makes it optional, and adds a burn step and a validity rule, because the documented 5 s initial balance, W0's 115 s and W1c's
spread show that a fresh machine's burst state cannot be arranged in advance. The boot: a fresh shared-cpu-2x 4 GB machine, the
fp32 image with this change, no asks, the burn step, the 30-page fixture uploaded, the `upload stages` line read from the logs.
The expected odds are in decision 3.

- **Amendment (2026-11-06, after the live canary on window W1d; recorded with the owner's standing go to run the boot): the burn is no longer stopped early.** The canary showed that a full-core burn uses up the machine's pooled shared-CPU quota and starves its ssh server too: reads over `flyctl ssh` failed from second 223 of the burn on, and the stop file could not be written, while the burn itself ran its full 600 s (600 samples on the machine; onset at 126 s, sustained share 0.139). So the burn now runs its fixed 600 s and ends by itself, the driver waits locally with no ssh traffic (600 s plus a 30 s margin), then reads the whole samples file once the machine is idle, confirms the burn is over (its last sample is second 600 and a second read finds nothing new; a shorter file is a burn that died and sends no upload), and uploads at once; the confirmation has one overall deadline (1,500 s) so the memory sampler outlives it. The onset analysis is recorded, not used to stop. 600 s stays inside this step's "at most about 600 s"; the text below is kept as written for the record.
- **Burn step (before the upload).** On the api machine itself, run `tools/probe/burn.py --burn-s 600` (the W0 probe: one full-core loop,
  measured every second, stdlib only) and stop it early (it flushes a sample each second, so a killed run keeps them) once at least
  300 s have passed and `tools/probe/report.py` on those samples reports an onset (below 90% of the baseline for 5 s in a row,
  `report.py:6-7`; W0's machine throttled from second 115, `w0/report.txt`), but for at most about 600 s: the maximum balance
  is 500 s (`M4_PLAN.md:491-492`, the Fly page) and one full core drains it at about 1 - 0.125 = 0.875 s per s, so 571 s
  (**inferred**). If the criterion has not shown by then, send the upload anyway and let the validity rule decide. Send the
  upload as the burn ends: W0 saw an idle pulse back at 90% of the baseline after 60 s of rest, so a gap lets the balance
  refill. The burn does not run beside the upload (the quota is pooled across the machine's vCPUs, so it would take the embed's
  share). Its time sits inside the hour
  priced for the api machine below (600 s of burn, a worst job of about 2,260 s and the setup come to under an hour); how the
  script reaches the machine is the window plan's. The rule below, not the burn, decides whether the boot counts.
- **Validity (numeric).** The embed-stage share = `tokens` / (85.8 x 1.18 x `embed_s`), from the `upload stages` line of a job that
  reached `ready` (on a `timeout` line `tokens=` is the plan, so the figure would overstate the share). The boot is **INVALID, not
  a pass,** if that share is above 0.15 (the job ran faster than the slow end this boot tests: for the cap job, `embed_s` under
  about 1,054 s; a machine throttled like W0's post-burst 17% would be ruled INVALID at either factor, and its 14.6% median is
  borderline, so this boot tests the 12 to 12.5% case), or if `embed_s` is under 0.9 x the harness's `ready_s`
  (`tools/probe/w1b.py:598`). Using f = 1.18 gives the higher share, so a valid boot was throttled under either factor. An INVALID
  boot is re-run unchanged on a fresh machine once (the cap below covers it); two INVALID boots end the window INCOMPLETE and
  nothing is amended.
- **Pass.** Valid, and the job reaches `ready` (not `timeout`), and uvicorn VmHWM at most 2,400,000 kB (the code path is
  unchanged, so this is a check, not a new risk). `ready` is the ceiling's own verdict: it is judged on the embed loop's clock
  (`jobs.py:351`), and a last chunk that crosses it still reaches `ready` (requirement 1). The harness's `ready_s` starts at
  upload accept (`tools/probe/w1b.py:598`), so it is not compared with 2,100 s; `embed_s` is recorded from the line, not tested.
- It adds a W1c-derived window entry to `deploy/staging/windows.json`, written before the quote. Cost from the `windows.json` rates,
  reusing W1c's machine set: api shared-cpu-2x:4gb 1 h $0.0448 + neo4j shared-cpu-1x:1gb 1.5 h $0.0177 + mockllm shared-cpu-1x:1gb
  1.5 h $0.0177 + 3 GB volume 1.5 h $0.0009 = **$0.081** (`windows.json` quotes that set at $0.10); two boots $0.162; the mock LLM is
  not needed with no asks, so the real quote can be lower; **cap $0.25**. Remote-builder minutes and egress are unpriced
  (`windows.json` `source`). The official quote is `scripts/staging.py quote`, and a window is created only with the owner's
  `--approve-quote`. No provider (LLM) call is made.

## Not in this spec

- **H, resume after a failure (deferred).** The diagnosis proposed writing vectors as chunks finish so a retry resumes through
  `repo.embedded_chunks`. That does not work for the main case: a new upload gets a fresh random id
  (`uploads/__init__.py:22-23`, `workspace_routes.py:321-322`); the `UserDocument` node is created only inside `put_version` at
  indexing (`repo.py:303-307`), so a first upload that times out leaves no document, and a client-supplied id for it is a 404
  (`workspace_routes.py:324-325`); and `embedded_chunks` reads per (workspace, document) (`repo.py:291-293`). A retry therefore
  starts from zero. Resume needs vectors keyed by (workspace id, text hash), stored outside a version: a data-model change (new
  retained user-derived data, quota, retention and sweep). Three traps for its own spec: chunks reused from a checkpoint must
  still count toward `embedded_tokens` at commit, because `put_version` bumps only freshly embedded chunks
  (`repo.py:405`, `478`; the test double mirrors it at `tests/test_serve_upload_jobs.py:103-106`); the key must include the
  workspace, because a shared key makes a faster embed a signal that another workspace uploaded the same text; and a persisted
  version is council-worthy. The cheapest viable shape is in-process: at most 120 chunks x 1,024 float32 = 491,520 bytes, TTL,
  evicted on workspace delete, lost on restart. Reopen only if live logs show ceiling stops after A. Owner decision 5.
- **Per-chunk limit (dropped on review).** The first draft added a 120 s limit on one `encode_passages` call. A 512-token chunk (the
  largest legal) takes 120 s only below a sustained share of about 4.2% (3.8% to 4.2% across the class factors, 5.0% at f = 1),
  where a cap job needs about 3,750 s and the ceiling ends the job anyway; no recorded boot shows such a share. It would add a
  second way to throw all the work away and a second cause to tell apart in the logs. The draft's design also had a flaw: with one
  clock read per chunk the measured interval covers the previous iteration's emit (a Neo4j `put_job` write, `jobs.py:357`, then
  `_persist_job`), so a slow database write could fail a healthy job. If reinstated: two clock reads around the `encode_passages` call alone.
- **Hang protection (deferred).** The ceiling runs only when a chunk returns (`jobs.py:352-359`). A native `session.run` that never
  returns is unbounded today and stays so. A watchdog thread could emit `failed` but cannot free the upload slot (released only in
  `_worker`'s `finally`, `jobs.py:563-564`), so it would either leave the slot held under a job the client sees as failed, or let a
  second job run beside a stuck thread: a thread-abandonment design, not a sizing change. No recorded boot under `w1b` or `w1c`
  shows a non-returning chunk (the two W1b failures ended through the ceiling check, which only runs when chunks return).
  Owner decision 6.
- **B, `ONNX_THREADS=2` (not pursued).** After the burst it does not help: Fly's quota is shared between a machine's vCPUs (the page
  fetched 2026-11-06), so two threads share one pool. The 1.7x per-chunk gain (q8, 1 thread 3.28 s to 2 threads 1.93 s on a
  296-token chunk) is the diagnosis's local figure, **not re-run in this session**, and applies to the burst period. The memory
  margin is 77,444 kB (`w1c/w1c.json`): a second intra-op arena is an unmeasured risk. If reopened: a two-thread burn probe
  (W0-derived; `windows.json` W0 quote $0.05, cap $0.10) and a local cosine check of threads 1 against 2 ($0); adoption would also
  need Linux VmHWM evidence. Owner decision 6.
- **F, a bigger machine (priced option).** On `performance-2x:4gb` a fp32 query took 0.437 s median (`w1/t_perf.json`) and the
  embed of an upload is its core time, 186.5 s at the cap (85.8 tok/s is recorded on that class); with parse and indexing, about 3
  to 4 minutes (**inferred**: those stages are unrecorded). Owner decision 4.

## Owner decisions (recommendation first)

1. **Approve the spec as written** (recommended): ceiling 2,100 s, the production validator (floor 600 s, a guard against zero and
   unit slips, not a throttle margin; ceiling x daily uploads <= 86,400 s), the stage-timing log line, the sweeper default fix
   (3,000 s), no per-chunk limit. Alternatives: 2,000 s (1.52x at 12% under f = 1.18: thin, a 7.9% floor share) or 2,160 s (the
   validator's top at 40 a day: 1.64x, but 24.0 h of embed ceiling a day with no slack, and about 25.8 h with parse and the last
   chunk). 1,800 s meets the 1.5x rule only at f = 1.31 (1.37x at 1.18) and 2,400 s fails the embed-ceiling day check (96,000 s).
2. **Closed by the review.** Slot time (S3): every ceiling from 2,000 s to 2,160 s keeps the 1.5x margin and passes the embed-ceiling
   check ceiling x 40 <= 86,400 s, so requirement 5 adds the product rule at no cost. That check covers the embed
   ceiling only: with parse and the last chunk a job at the ceiling holds the slot up to about 2,260 s (25.1 h a day at 2,100 s if
   all 40 ran to the ceiling), and the one-at-a-time slot absorbs the excess by refusing later uploads. The remaining denial (one
   visitor can spend the 40-a-day cap) exists today and is not changed by this spec. Reopen only to lower `max_uploads_per_day` (a
   product cap).
3. **Confirmation boot** (recommended: yes, after stage 6): about $0.08 for one boot, $0.16 with the one re-run, cap $0.25, a burn
   step, a numeric validity rule, an official quote first, run only on your go. The tests prove the logic; only a throttled
   machine shows the ceiling fits. It narrows council 8 item 3 to one fp32 boot. **Expected odds (inferred):** on W1c's implied
   shares only boot 1 of 3 would have been valid (boot 2 at 18% to 23%, boot 3 at 32% to 41%; whole-job averages); the burn step is
   meant to improve that, but W0's own post-burst shares, 17% and 14.6%, straddle the 0.15 limit, so a burned machine can still be
   ruled INVALID. At a 1-in-3 chance per boot, one re-run leaves about a 44% chance (2/3 squared) that the window ends INCOMPLETE
   with up to $0.16 spent and no result; three boots are a thin sample. Alternative: ship on the tests and the arithmetic, and read
   the `upload stages` line from the first real max-size upload.
4. **Machine class** (recommended: keep shared-cpu-2x). With this change a max-size upload takes from about 6 minutes on the
   fastest measured boot (388 s, W1c boot 3) to about 22 minutes for a cap job at a 12% share (1,317 s of embedding, inferred), up
   to about 26 minutes of embedding if the share drops to 10% (1,580 s); `performance-2x:4gb` makes it about 3 to 4 minutes
   (**inferred**) for +$51.53 a month. Do you want uploads under about 5 minutes at that price?
5. **Defer H** (recommended), with its flaw on record above; reopen as its own spec (and the council, if the vectors are persisted)
   only if live logs show ceiling stops after A.
6. **Defer B, hang protection and the per-chunk limit** (recommended). Reopening B costs $0.05 for the probe and $0 locally; hang
   protection needs its own design; the per-chunk limit returns only with a boot that shows a share under about 4%.

Council: **not recommended.** The fix is $0, config-level and reversible, and the ceiling is right without isolating the cause: the
arithmetic and four recorded shares put a cap job at 1,139 to 1,317 s at 12 to 12.5%, and the ceiling carries a 1.5x margin over
that. The one expensive choice (decision 4) is a plain price-for-latency question for you. The deferred H would meet the council
rule if it is ever persisted.

## Status

| Stage | Gate | Evidence |
|---|---|---|
| 1 Spec | owner approves | **APPROVED by the owner 2026-11-06**: "Approve + run the boot" (decision 1 as written; decision 3 yes, after stage 6, quoted first; decision 4 keep shared-cpu-2x; decisions 5-6 deferred). Drafted and revised 2026-11-06 (two Opus reviews applied); passed a final Opus check on 2026-11-06 (READY_FOR_OWNER) with its minor leftovers applied|
| 2 Dev (TDD) | tests red then green; reviews clean | **Tests RED then GREEN, 2026-11-06 (Sonnet worker, Plan 1); reviews done** (see the end of this row). Interpreter `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`. Baseline before any change, on `tests/test_serve_private_state.py tests/test_serve_config_production.py tests/test_check_env_fly.py tests/test_serve_upload_repo.py tests/test_serve_upload_jobs.py tests/test_serve_upload_*.py tests/test_serve_config*.py`: 678 passed, 3 skipped. **RED 1**, new `tests/test_serve_upload_embed_timeout.py` (35 tests) on the unchanged source: 27 failed, 8 passed (failures are `TypeError: _process() got an unexpected keyword argument 'clock'`, ending the job `internal_error`, and the default; the 8 that passed are negative or characterization cases: five "no stage line" parse-stage cases, the `failed` half of the S2 key scan, the drain count, `_embed_chunks` without a record). **RED 2**, clock, `stages` and the stage line in `jobs.py`, default still 1,200: 7 failed, 28 passed; the failures are the cap job at 12%, 10% and 8% (`timeout` at the old 1,200 s loop; 17% is `ready` under both ceilings, characterization), the 12% stage-line test, the 1.5x derivation test and the two sweeper-age tests. **RED 3**, `tests/test_serve_private_state.py:57` set to `(90, 2100, 120)` and `FakeSettings` to 2,100 (default still 1,200), on `test_serve_private_state.py test_serve_upload_repo.py test_serve_upload_jobs.py`: 1 failed (the deliberate one), 146 passed. **RED 4**, default changed to 2,100 (config.py, comment with the arithmetic table): on those three files plus the new one 2 failed, 180 passed: `test_serve_upload_repo.py:745` (the deliberate RED, 90 + 2,100 above the repo default 2,100) and the new repo-age test. **GREEN 4**, `repo.FAIL_INTERRUPTED_AFTER_S` = 50 x 60 and the comments of `jobs.py:591-600` and `repo.py` corrected: 182 passed. **RED 5**, `PINNED` plus 21 new tests in `tests/test_serve_config_production.py` and 2 in `tests/test_check_env_fly.py` before the validator, on `test_serve_config_production.py test_check_env_fly.py test_serve_config_staging.py`: 16 failed, 316 passed. **GREEN 5**, `CHECKED_SETTINGS` and `Settings._upload_embed_timeout_problems`: 332 passed. **Selection GREEN**, `tests/test_serve_upload_*.py tests/test_serve_config*.py tests/test_serve_private_state.py tests/test_check_env_fly.py`: 736 passed, 3 skipped (`-rs`: `test_serve_upload_compare.py:325`, `test_serve_upload_parse.py:407` and `:471`, all Linux-only RLIMIT_AS tests). Coverage: every non-docstring statement of the changed functions of `jobs.py` and `config.py` executed (stdlib `trace`); module coverage not measured, because `coverage.py` and `pytest-cov` are not in the venv, so the stage 3 coverage gate is still open. Acceptance 11 mutations on a scratch copy of the tree (the live tree was not mutated): 16 of 16 killed (ceiling `>=`, `remaining` guard removed, other ceiling message, validator floor off by one and removed, product rule `>=` and removed, product message naming the count, sweeper formula, default back to 1,200, `ws_hash` replaced by the workspace id, stage line outside the `finally`, `embed_s` only on a normal return, `done` read as the plan, `tokens` not the plan, a line written for a parse failure). Not done here: `.env.example` (read-denied: if it lists `UPLOAD_EMBED_TIMEOUT_S` at 1200 the owner updates that line), `fly.toml` (untouched by design). **Review fix 2026-11-06 (MEDIUM, S4: the stage line could add a second terminal event):** `_log_stages` ran unguarded in the `finally` of `_process`, after `emit("ready")`; a raise inside it reached `_worker`'s `except Exception`, which sent `failed`/`internal_error` for a job whose version was already stored (slot released once, `embedded_tokens` already bumped, a client retry would spend another upload). **RED 6**, new `test_a_stage_line_that_cannot_be_formatted_never_changes_how_a_ready_job_ends` (`_STAGE_FIELDS` set to a format that raises `TypeError` inside the function, so the guard itself is under test), `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_serve_upload_embed_timeout.py`: 1 failed, 35 passed (a `failed` event after `ready`, log shows `upload job crashed` raised from `_log_stages`). **GREEN 6**, `_log_stages` body wrapped in `try/except Exception`; the handler logs one WARNING with `job_id` and the exception class name only (no `ws_hash`, because building it may be what raised, and no message text): same file 36 passed; `tests/test_serve_upload_*.py tests/test_serve_private_state.py`: 375 passed, 3 skipped (the same three Linux-only RLIMIT_AS skips). Mutation 17 of 17 killed: the guard removed, run on a scratch copy of `src` and `tests`: the new test fails, the other 35 pass; "stage line outside the `finally`" is still killed by the `internal_error` test. Full suite not run here (the orchestrator runs it once) **Reviews (2026-11-06):** `ecc:code-reviewer` (sonnet) APPROVE, 0 CRITICAL/HIGH/MEDIUM, 3 LOW (the existing embed log line, `_process` at about 60 lines against the 50-line rule, a stale explicit `older_than_s=1890` in a test). `ecc:security-reviewer` (Opus) APPROVE, S1-S8 met, 1 MEDIUM fixed test-first (a stage line that raised after `ready` could add a second terminal event: `_log_stages` now never raises; RED 1 failed / 35 passed, then GREEN), 1 LOW. Opus verifier: PASS WITH CONDITIONS, 18 of 18 mutations killed; coverage of the changed modules (coverage 7.17.2 outside the venv): `config.py` 96%, `uploads/jobs.py` 97%, `uploads/repo.py` 95%, no changed line missed. Conditions closed at commit: these rows, the security review on record, only this task's files staged. Open, not blocking: `.env.example` may still carry `UPLOAD_EMBED_TIMEOUT_S=1200` (read-denied; the owner checks). |
| 3 Regression | full suite green | **2026-11-06:** `uv run pytest -q -p no:cacheprovider --ignore=tests/test_loadtest_local_smoke.py` = **9635 passed, 422 skipped, 2 xfailed, 0 failed** (521 s), run after the last edit, with M5b I0b in the same tree; `node --test tests/ui/*.test.mjs` 242 passed |
| Decision 3 | the confirmation boot | **W1d confirmation boot (2026-11-06): FAIL** (evidence `docs/v2/research/m5-spikes/fly-2026-11-06/w1d`). Fresh shared-cpu-2x 4 GB, fp32 image with this change, healthy in 200.8 s; burn 600 s complete (onset 116 s, sustained burn share 0.101, steal 0.45); the 30-page upload sent 35 s after the burn's last sample (machine clock); the embed loop ran 2,128.9 s and stopped at the 2,100 s ceiling with 50 of 60 chunks done (11,798 of 14,151 tokens): 5.54 tok/s, an embed share of 0.055 under f = 1.18 (0.065 under f = 1), half the burn's share and below this spec's 12% design floor. Memory fine: VmHWM 2,284,852 kB, no OOM. At that rate the fixture needs about 2,553 s and a cap job about 2,887 s, above every ceiling the day check allows at 40 uploads a day (2,160 s). The change still helps (the job ran 2,129 s instead of dying at 1,200 s) but does not make a max-size upload reliable on this class. Staging spend about $0.15 (quote $0.15, cap $0.25). The canary (two runs) proved the harness first; the first canary exposed and the fix `0d1e330` removed ssh steering during the burn. Next: an owner decision on the fix (see M5_DECISIONS 10.11). |
| 4 Security | /security-review, gitleaks before push | **2026-11-06, before the push of `9945483`:** gitleaks over `origin/v2..HEAD` flagged 2 `generic-api-key` hits, both the sha256 of the tokenizer file in `artifacts/upload_embed_local_timing.json` (a file hash): fingerprints added to `.gitleaksignore`, rescan clean. `/security-review` (Opus): see the push commit |
| 5 Fix findings | only if stage 4 finds any | Pending |
| 6 Verifier | Opus | Pending |
| 7 Push, CI | green | Pending |

Stage 8 (archify) is expected to be skipped: no service, module boundary, data store, trust boundary or dependency changes; to be
confirmed at stage 7.

## Review disposition (adversarial Opus review, 2026-11-06)

Requirement numbers in the findings are the first draft's; the first draft's requirements 2 and 3 are gone, so 4 to 10 became 2 to 8.

| Finding | Disposition |
|---|---|
| 1. Class factor (major): the 85.8 tok/s is performance-2x, the shares are fresh shared-core fractions; times overstated by 18% to 31% | **Applied.** Class recorded at `m5_spikes.json:194`; factor 1.18 to 1.31 as an inferred range; table, "-5% to +10%", ceiling, S3 hours, decision 1 alternatives and decision 4 minutes recomputed. **Applied differently:** the fixture total uses the 16,000-token bound (186.5 core-s), not 198 s, so the review's 1.55x to 1.71x, 14.0 to 15.5 h and 12.9% to 14.3% became 1.59x to 1.77x (cap, 2,100 s), 13.2 to 14.6 h and 10.5% to 13.5%; ceiling 2,100 s chosen inside the review's 2,000 to 2,160 s window; old decision 2 closed |
| 2. Cause and asks (minor) | **Applied.** Cause worded as the leading explanation; asks second order at the live cap (about 14 a day), a co-factor at saturation (32% to 40%); council 8 reconciled (853 to 947 s against 842 s; boot 1 share 10.5% to 13.5%); the narrowing of council 8 item 3 to one optional fp32 boot recorded |
| 3. Fly page and the confirmation boot (minor) | **Applied.** Page cited, fetched 2026-11-06 (10 ms per 80 ms for shared-cpu-2x, quota shared across vCPUs, 5 s initial balance; 12.5% marked derived); boots 2 and 3 marked inconsistent with the 5 s balance; numeric validity rule added (share at most 0.15 with f = 1.18; `embed_s` at least 0.9 x `ready_s`) |
| 4. Requirement 6 and S4 (minor) | **Applied.** Bound restated as ceiling + longest chunk call (about 70 s, unbounded if hung) + compare + `put_version`; requirement described as consistency only; the `max_chunk_s` relation test dropped. **Applied differently:** sweeper age 2,790 s and repo default 3,000 s (the review's values assumed the first draft's 2,400 s ceiling) |
| 5. Per-chunk limit (minor) | **Applied (dropped).** Recorded under "Not in this spec" with the 4.2% share arithmetic and the two-clock-read condition for reinstating it; every reference removed |
| 6. Requirement 7 and test lists (minor) | **Applied.** `CHECKED_SETTINGS`, `PINNED`, `tests/test_check_env_fly.py` and `tests/test_serve_upload_repo.py:745` added; cross-field rule stated as production only; leak fake removed from the fakes item. **Applied differently:** the refusal names only `UPLOAD_EMBED_TIMEOUT_S`, so `MAX_UPLOADS_PER_DAY` needs no entry in `CHECKED_SETTINGS`; the 3,600 s top replaced by the daily-product rule; the second setting no longer exists |
| 7. Stage line and clock (minor) | **Applied.** Clock injected at `_process` so every stage reads it; exits that write the line and the meaning of `tokens=` defined (freshly embedded tokens) |
| 8. Accuracy points (minor): (a) mean vs median; (b) compare about zero; (c) W1b bound; (d) q8 timed in Plan 0; (e) float boundary | (a) **applied differently:** the token bound replaces both, because the 3.42 s mean also gives 205 s, above the cap; (b) applied (`jobs.py:373-380`); (c) applied, as inferred (about 38 s and 62 s); (d) applied (fp32); (e) applied (integer 700 s steps) |

Two further findings concern commit `0fb5a2b` and `docs/specs/m5a-followups.md`, not this spec; they are handled outside it.

### Round 2 (second adversarial Opus re-review, 2026-11-06)

The round-1 rows above stay as history: their figures were superseded where a round-2 row gives corrected ones (853 to 947 s,
967 to 1,073 s, 1,160 to 1,290 s and "23.3 h at most" are withdrawn). Round-2 entries are numbered R2-1 to R2-11.

| Finding | Disposition |
|---|---|
| R2-1. Option G still said the asks "are not the cause" (leftover of round-1 finding 2) | **Applied.** "not the leading cause", matching the cause paragraph and the asks bullet |
| R2-2. M4 premise (minor): M4's 190 s is a desktop q8 figure (`M4_PLAN.md:505, 515`), so no Fly class factor applies | **Applied.** Premise removed from the risk 13 bullet and the council 8 reconciliation; both redone from 186.5 performance-core s (162.3 to 186.5 for the fixture): 1,139 to 1,264 s at 12.5% (the table's row), 837 to 930 s at 17% and 949 to 1,054 s at 15% for the cap job, 729 to 809 s at 17% for the fixture's lower reconstruction, against 842 s (inside the cap-job range, at its low end). Council 8's own 190 s / 15% = about 1,270 s is kept as its figure and labelled a desktop q8 number |
| R2-3. The 600 s floor does not protect throttled uploads, so S6 and requirement 5 overclaimed (minor) | **Applied** (the narrowing option). Floor stays 600 s as a guard against zero, near-zero and unit slips; requirement 5, S6, decision 1 and requirement 8 now say it is not a throttle margin (600 to about 1,186 s accepted and fails a cap job at 12% under either factor, up to about 1,317 s under f = 1.18; under about 1,975 s breaks the 1.5x rule at the design factor, 1,779 s at 1.31; both are tuning, in the RUNBOOK). Acceptance 8 adds a 1,000 s accept and a refusal-wording test; its 599/600 boundaries do not move because the floor does not. Not raised: 1,200 s still fails a cap job at 12% (1,186 to 1,317 s), and 1,975 s would refuse boot at any `MAX_UPLOADS_PER_DAY` of 44 or more (1,975 x 44 = 86,900) and write an inferred class factor into a refusal rule |
| R2-4. The day bound counts only the embed ceiling (minor) | **Applied.** Ceiling rule, requirement 5, S3, requirement 6 and decisions 1 and 2 restated: the 86,400 s rule is a check on the embed ceiling only; parse (up to 90 s) plus the last chunk (up to about 70 s) add about 160 s a job, so 40 x 2,260 = 90,400 s = 25.1 h if every job ran to the ceiling (decision 1's 2,160 s alternative: 24.0 h, 25.8 h with parse and the last chunk), which the one-at-a-time slot absorbs by refusing later uploads (429 `busy`, `workspace_routes.py:404-408`). "Fits the day" and "bounds the length by the day" removed; the 13.2 to 14.6 h labelled embedding time. The optional refusal wording is applied: the product-rule message says in words that the daily upload count is part of the cause, because `scripts/check_env_fly.py:53` names a problem by its first word |
| R2-5. Plan 2 validity conflicts with the spec's own inputs (minor) | **Applied.** Parenthetical reworded ("the slow end this boot tests"; W0's post-burst 17% would be INVALID at either factor, its 14.6% is borderline: 0.17 x 1.31 / 1.18 = 0.189, 0.146 x 1.31 / 1.18 = 0.162). Expected odds in decision 3 and marked inferred: only boot 1 of 3 W1c boots valid (18% to 23% and 32% to 41% for boots 2 and 3), about 44% INCOMPLETE after one re-run at 1 in 3 per boot, up to $0.16 spent. Burn step now in Plan 2. **Applied differently:** not "a burn of at least 300 s" alone: the burn runs on the api machine, for at least 300 s and until W0's throttle criterion holds (below 90% of the baseline) but at most about 600 s (500 s maximum balance / 0.875 s per s = 571 s, inferred; the upload is sent anyway if the criterion never shows), and the upload is sent as it ends, because W0 saw the balance refill after 60 s of rest; it is not run beside the upload because the quota is pooled. The 0.15 limit is unchanged |
| R2-6. The pass test mixes two clocks (minor) | **Applied differently.** Pass = valid and the job reaches `ready`; the optional `embed_s` <= 2,100 s is not added because a last chunk that crosses the ceiling still reaches `ready` (requirement 1, acceptance 3), so a healthy job can have `embed_s` above 2,100 s. `embed_s` is recorded, not tested; `ready_s` (from accept) is not compared with the ceiling |
| R2-7. Ceiling rule uses 12% while boot 1 implies 10.5% (minor) | **Applied.** Sentence added: scaled to the cap, boot 1 is at most 1,174.3 x 16,000 / 13,923 = 1,349 s, so 2,100 s covers it at least 1.55x (1.556x; the review's "at least 1.56x" would round up) |
| R2-8. Asks bullet label (minor) | **Applied** (first option): "the fixture's 124 to 158 shared-core-s (13,923 to 16,000 tokens, f = 1.31 to 1.18)"; 3% to 4% and 32% to 40% are right for that range (the cap job's 142 to 158 would give about 3% and 32% to 35%) |
| R2-9. Decision 4 wording (minor) | **Applied.** "From about 6 minutes on the fastest measured boot (388 s, W1c boot 3) to about 22 minutes for a cap job at a 12% share (1,317 s of embedding, inferred)"; the 10% case (1,580 s) labelled embedding too |
| R2-10. Stage-line gaps (minor): `workspace_quota`, `tokens=` on a timeout, how timings reach the `finally` | **Applied.** (1) `too_many_pages`, `too_many_tokens` and the page-quota `workspace_quota` (`jobs.py:428-437`) write no line, nor an exception in the parse stage; the line's `workspace_quota` is the embedded-token budget (`jobs.py:460`). (2) `tokens=` is the planned fresh tokens, and `fresh=` and `done=` are added (beyond the finding) so a stop inside the loop is recognisable (`done` below `fresh`) and a share is taken from a `ready` line only. (3) Timings travel in a `stages` dict passed down with the clock (requirement 2), written per stage, including on `None`, `"timeout"` and raise. Acceptance 6 and the mutation list test all three |
| R2-11. Small citations and wording (minor) | **Applied** for five of six: `jobs.py:357` for the per-chunk emit; the 461 / 5.59 pairing marked inferred; "3.8% to 4.2% across the class factors, 5.0% at f = 1"; caps at `config.py:291-299`; the check_env_fly memory test at `:166-175`. **Not applied:** the `FakeSettings` range. The repo shows `class FakeSettings` at `tests/test_serve_upload_jobs.py:26` and its last attribute at `:36`, so the spec's 26-36 stands and the finding's 24-35 does not |

### Round 3 (final Opus check, 2026-11-06: READY_FOR_OWNER, minor leftovers applied)

- R3-1. **Applied.** S3 last sentence: 2,260 s is for a job stopped at the ceiling, plus unrecorded chunking; a job whose last chunk crosses the ceiling also holds the slot through compare (up to 120 s, `config.py:301`) and `put_version`, as requirement 4 says.
- R3-2. **Applied.** Plan 2 burn step: `tools/probe/burn.py --burn-s 600`, stopped early once at least 300 s have passed and `tools/probe/report.py` reports an onset (below 90% of the baseline for 5 s in a row, `report.py:6-7`); burn.py has no stop-on-criterion option.
- R3-3. **Applied.** Acceptance 9 now names `_FakeJobSettings` (`tests/integration/test_workspace_repo_neo4j.py:458-466`), which keeps 1,200 and is untouched (first option; Plan 1's file list is unchanged).
- R3-4. **Applied.** Council 8 reconciliation: the 190 CPU-s is the probe README's pre-run figure (`tools/probe/README.md:132`), the same number as M4's desktop q8 spike (`M4_PLAN.md:505, 515`); no claim that one derives from the other.
- R3-5. **Applied.** Plan 2 cost line items printed unrounded ($0.0448 + $0.0177 + $0.0177 + $0.0009 = $0.081).
