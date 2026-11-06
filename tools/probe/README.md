# tools/probe: the fresh-machine throttle probe (window W0)

`scripts/ops.ps1 start` creates a NEW machine, and a fresh `shared-cpu` machine starts with a burst balance of a few seconds
(about 500 s at most, refilled at a rate Fly does not publish: docs/v2/M5_DECISIONS.md 1.2 and section 3, "fresh-machine throttle
probe"). The figures are documentation, not an observation. This probe makes the observation.

`burn.py` spins one full core in fixed chunks of integer work, counts the chunks, and writes one JSON line per second: iterations
per second, the share of the second this process was on a CPU, and the `steal` column of `/proc/stat` (the hypervisor taking the
core away). After the burn, an optional idle stretch has a two-second pulse every minute, to see how fast a rested machine gets
back to full speed. `report.py` turns the lines into `probe.json`: the baseline (the first 10 s), the onset (the first second from
which the throughput stayed under 90 % of it for 5 s), the sustained rate, the refill, and the steal before and after the onset.
With too few seconds it says so and claims nothing.

```
scripts/staging.py quote W0                 # about $0.05 (cap $0.10); then create W0, deploy W0 tools
flyctl ssh console -a semigraph-tools-stg -C "python -m tools.probe.burn --burn-s 3600 --idle-s 1800 --out /out/probe"
python -m tools.probe.report /out/probe     # or: scripts/staging.py snapshot W0 --out DIR, then report on DIR/tools-out/probe
```

If the probe shows throttling within seconds, the runbook gets a warm-up (start the machine well before a demo, or run a demo on a
performance-class machine) and the owner is told. Stdlib only; the clock, the work, `/proc/stat` and sleep are parameters so the
tests run in microseconds on any platform.

## `embed_rss.py`: resident memory of one embedder model (window W1)

The embedder choice (docs/v2/research/m5-councils/council6/verdict.md, M5 decision 14) is bound by memory as much as by speed. The
M4 live gate saw the uvicorn process peak at VmHWM 1,125,116 kB during a max-size upload (a 30-page PDF, 15,849 tokens embedded as
passages) against a limit of 2,400,000 kB (`artifacts/m4_live_gates.json`, G4.memory). The pre-dequantized fp32 model adds about
640 MB on Windows, and on Linux ONNX Runtime may hold its whole 2.3 GB external-data file resident. `scripts/embedder_timing.py`
holds both models in one process and cannot say. `embed_rss.py` loads ONE model in a fresh process the way the service does
(`OnnxBackend(model, None, threads=1)`, the `serve.main.bootstrap` warm-up query), then reads `/proc/self/status` after the load, after
60 query embeds (30 benchmark/example questions, each plain and with council 5's per-ask salt) and after the G4 passage workload
(synthetic prose cut by the real upload chunker, 15,849 tokens, one `encode_passages([chunk])` per call). It records VmRSS, VmHWM,
RssAnon, RssFile, RssShmem and VmSwap at each step (a mapped file shows as RssFile, private memory as RssAnon), the median and p90 of
each kind of call, and the salt's cost.

```
python embed_rss.py MODEL_Q8 --out q8.json                  # fresh process; prints one JSON object, progress on stderr
python embed_rss.py MODEL_FP32 --compare q8.json --out fp32.json
```

`--compare` takes the JSON of a q8 run on the same machine (or a q8 model file, which a fresh child process then measures first).
`predicted_uvicorn_vmhwm_kb` is 1,125,116 + (this model's peak - the q8 peak), judged against 2,400,000 kB: `verdict` is `PASS` or
`FAIL` and `margin_kb` is the room left (negative when over). A reference from another memory source, another thread count or another
workload, or one that is not a q8 run, gives no prediction (`NO_REFERENCE`, with the reason). Only a Linux run with the full workload
has `gate_evidence: true`: on Windows the counters come from psutil (working set and PEAK working set, not VmHWM), the run says so
(`memory_source: psutil_peak_wset`, `not_gate_evidence_because`) and its verdict is `INDICATIVE_PASS` / `INDICATIVE_FAIL`, which must
not be pasted into `artifacts/m5_spikes.json` as the gate. `--passage-tokens N` shortens the workload for a smoke run (also not
evidence). Exit code 0 measured, 1 FAIL, 2 the run could not be done. The workload is sequential: the five concurrent asks of G4 are
not replayed, because the delta method assumes that overhead is the same for both models.

On Fly the file is `/srv/models/embed_rss.py`, shipped by the main Dockerfile only with `--build-arg KEEP_UNPATCHED=1`. The run steps
are the `steps` of window W1 in `deploy/staging/windows.json`. Module level imports are the standard library only (onnxruntime,
tokenizers, semigraph and psutil are imported inside functions) and nothing imports `tools`, because the file runs on its own in the
serve image. Tests: `tests/test_tools_probe_embed_rss.py` (stub backend, fake `/proc`, no model).

## `memsample.py` and `w1b.py`: the fp32 memory confirmation on a fresh machine (window W1b)

Council 7 (decision 1, step 3) adopts the fp32 embedder only after the REAL service has run it on a fresh machine under M4's worst
upload. `deploy/staging/windows.json` window `W1b` is that run: the staging API from `deploy/staging/fly.w1b.toml` (the staging
environment with the live memory settings: the fp32 model path, `EMBED_SLOTS=1`, `MAX_CONCURRENT_ANSWERS=2`, the code-default upload
caps, 1 GiB of swap, the live proxy limits), image built `--build-arg EMBEDDER_VARIANT=fp32` and WITHOUT `KEEP_UNPATCHED=1` (the image
that would ship; `scripts/staging.py` refuses any other combination for this window), a fresh shared-cpu-2x / 4 GB machine in sin, the
staging Neo4j and the mock LLM (both on shared-cpu-1x / 1 GB: memory, not latency, is measured). Quote: `$0.10` (cap `$0.25`).

**PASS** (pre-registered; do not change), on EVERY one of three fresh boots: max `VmHWM` of the uvicorn process `<= 2,400,000 kB`, no OOM
kill and no process restart, machine creation to `/healthz` 200 `<= 180 s` (all inclusive). Reported besides: the spread of the three
peaks, the lowest `MemAvailable` and the swap used.

* `memsample.py` runs ON the machine (stdlib only; the driver ships its text inside a `python -c` command over `flyctl ssh console`, so
  nothing has to be copied there). Once a second it appends `{t, mem_available_kb, swap_free_kb, swap_total_kb, uvicorn_pid, vmrss_kb,
  vmhwm_kb, oom_kill}` from `/proc` (the serving process is the one run as `-m semigraph.serve.drain`, found again on every sample, so a
  replaced process shows as a new pid; `oom_kill` is the kernel's counter). It detaches (`--daemon`), flushes every line and ends by
  itself after `--duration-s`, at SIGTERM or when its stop file appears.
* `w1b.py boot` measures ONE freshly created machine. Started BEFORE the deploy with `--wait-for-machine-s`, it waits for the one machine
  of `semigraph-stg` that was created after it started (never the old one), polls `/healthz` every 2 s from that machine's `created_at`,
  waits until the kill level reads `off` (admin route), starts the sampler and checks that it sees the serving process, creates a
  workspace, uploads the 30-page max-size PDF (`tests/upload_fixtures.max_size_pdf(30, 2700)`: M4's gate G4 file) exactly as the page does,
  and WHILE the job embeds sends salted mock-LLM asks (benchmark questions, each with its own `(ref N)` salt so none is a cached example,
  each from its own `X-Test-Client-IP`, at most the live cap of 2 in flight) until the job is ready plus 30 s. It then stops the sampler,
  reads machine events and logs for an OOM kill or a restart, and prints one JSON object (no secret in it): `boot_to_healthy_s`,
  `max_vmhwm_kb`, `min_mem_available_kb`, `max_swap_used_kb`, `oom_or_restart` (`detected` and the evidence lines), `job_ready_s`,
  `asks` (`sent`, `done`, `errors` by status), `verdict`, `reasons`. The secrets are read from the state directory `staging.py` writes.
* The verdict fails closed. **FAIL**: a pre-registered criterion is violated. **INVALID**: the measurement was not made as intended (the job
  did not reach ready, no ask finished while it ran, the machine is not shared-cpu-2x / 4 GB, no swap, the deployed `EMBED_SLOTS` is not
  1). **INCONCLUSIVE**: a criterion could not be established (the sampler or both machine sources unreadable, the samples do not cover
  the workload, or `/healthz` was already 200 at the first look and the time is over 180 s, so the true time is unknown). **PASS** only
  when none of those holds. Exit code 0 PASS, 1 FAIL, 2 anything else. `w1b.py aggregate` re-derives each verdict from the numbers in its
  file and passes only for exactly three boots on three different machines that all pass (a FAIL anywhere fails; fewer than three is
  `INCOMPLETE`).

### Sequence (PowerShell, from the repository root)

```powershell
# 0. the owner approves the quote first
uv run python scripts/staging.py quote W1b
uv run python scripts/staging.py create W1b --org <org> --approve-quote 0.10
uv run python scripts/staging.py seed W1b
uv run python scripts/staging.py deploy W1b mockllm
uv run python scripts/staging.py deploy W1b api --build-arg EMBEDDER_VARIANT=fp32    # NOT KEEP_UNPATCHED=1: the tool refuses it
uv run python scripts/staging.py preflight W1b       # this first machine is a warm-up: it proves the build and the deployed config

# 1. three measured boots, each on a freshly created machine
$dir = "$env:TEMP\w1b"; New-Item -ItemType Directory -Force $dir | Out-Null
foreach ($n in 1, 2, 3) {
    foreach ($m in @(flyctl machine list -a semigraph-stg --json | ConvertFrom-Json)) {      # @( ) in Windows PowerShell 5.1: one object per machine
        if ($m.state -ne "destroyed") { flyctl machine destroy $m.id --force -a semigraph-stg }
    }
    # the driver starts BEFORE the deploy, so /healthz is polled from the new machine's creation, not from when the deploy returns
    $job = Start-Job -ArgumentList (Get-Location).Path, $n, $dir -ScriptBlock {
        param($repo, $n, $dir); Set-Location $repo
        uv run python -m tools.probe.w1b boot --boot $n --wait-for-machine-s 2700 --out "$dir\boot-$n.json"
    }
    uv run python scripts/staging.py deploy W1b api --build-arg EMBEDDER_VARIANT=fp32     # a new machine id and creation time
    Receive-Job $job -Wait -AutoRemoveJob
}

# 2. the window's verdict
uv run python -m tools.probe.w1b aggregate "$dir\boot-1.json" "$dir\boot-2.json" "$dir\boot-3.json" --out "$dir\w1b.json"

# 3. evidence, then teardown (verified gone)
uv run python scripts/staging.py snapshot W1b --out "$dir\snapshot"
uv run python scripts/staging.py destroy W1b --yes
```

Two terminals do the same as the `Start-Job`: run `w1b.py boot ... --wait-for-machine-s 2700` in one, then the deploy in the other (the wait
costs nothing; it covers a cold remote builder). Destroy EVERY machine of the app first: the driver refuses to measure while a second
machine exists, and while waiting it ignores a machine created before it started. The
deploy of boots 2 and 3 builds again (a layer-cache hit when the remote builder is warm; if it is cold the fp32 dequantization runs
again, which costs minutes but no machine time). If a boot is INVALID or INCONCLUSIVE, destroy the machine and repeat that boot with the same
`--boot` number only if its file is overwritten (the salt counter starts from the clock, so a repeat never re-sends a salt).
Each boot takes about 10 to 12 minutes (boot, a job of about 190 CPU-seconds, 30 s of tail, collection).

### UNVERIFIED (not run against this organisation or this flyctl; each fails closed, never into a PASS)

* `flyctl machine list --json` carries `created_at` and `config.guest` (the Machines API fields); `flyctl machine status <id> --json` carries
  `events` (with `request.exit_event.oom_killed`). If not, the class or the evidence is unreadable: INCONCLUSIVE.
* `flyctl logs --machine <id> --no-tail`: if this flyctl rejects the flag the driver falls back to the app's logs filtered by machine id.
* A process started by `flyctl ssh console -C`, detached by the sampler's double fork, survives the end of that ssh session; `python` is on
  the PATH of an ssh session; and `-C` hands the one quoted `python -c '...'` word through intact (the runbook's cypher-shell examples
  rely on the same). The driver checks, before any load, that the sampler sees the serving process; if it does not, the boot is INVALID.
* A fresh machine per boot by destroy + deploy is the method used here because the deploy path is the one W1 ran. `flyctl machine clone
  <id>` would skip the build; it has not been tried and is not used.
* `created_at` is Fly's clock and the polling is local: a skew of a second or two is inside `boot_to_healthy_s`. The first look at `/healthz`
  is reported (`healthy_on_first_poll`, `first_poll_after_creation_s`) so that a late start is visible.

## `paced_asks.py` and `w1b.py --window W1c`: the same measurement under a paced ask load (window W1c)

Council 8 (`docs/v2/research/m5-councils/council8/verdict.md`, "The Recommendation" 2) closed window W1b as INCOMPLETE (boot 2 was INVALID
twice; fp32 is not adopted) and the owner approved **W1c**: window W1b with ONE change, the ask workload.

Recorded reason: W1b's saturating ask load (~150 asks) starved the upload past its 1,200 s embed timeout on the throttled machine (boot 2 INVALID twice, memory within limits each time); council 8 chose a paced load anchored to the live day cap; the memory thresholds are unchanged.

**Unchanged from W1b:** the apps (api, neo4j, mockllm), the api toml (`deploy/staging/fly.w1b.toml`), the build-argument policy
(`EMBEDDER_VARIANT=fp32` required, `KEEP_UNPATCHED=1` refused), the machines and classes (shared-cpu-2x / 4 GB api in sin, quote `$0.10`, cap
`$0.25`), 3 FRESH boots, the PASS thresholds (every boot: max uvicorn `VmHWM <= 2,400,000 kB`, no OOM kill and no restart, machine creation to
`/healthz` 200 `<= 180 s`; the aggregate passes only for exactly three boots on three different machines that all pass) and the INVALID rule (the
upload job must reach ready; at least one ask done while the job runs). `w1b_judge.boot_verdict` is the same function for both windows.

**The one change, the workload (registered in `deploy/staging/windows.json`, W1c `workload`, and recorded in every boot's JSON):**

* **At most 14 salted asks (the cap).** 14 = floor(production max_spend_usd_per_day / the live hybrid estimate) = floor(10 / 0.709573) = floor(14.09) = 14. It is
  computed in code, `paced_asks.derive_ask_count()`: `semigraph.config.PRODUCTION_MAX_SPEND_USD_PER_DAY` over `semigraph.serve.estimate` for the
  models and token cap of `fly.toml`, in whole micro-dollars (10,000,000 // 709,573), and the driver refuses to run when the count registered in
  `windows.json` is not the one it derives (a test recomputes it and checks that it follows its inputs).
* **Pacing:** one ask every 30 s from upload accept (the first ask at upload accept; 14 asks span 390 s), each ask from a different X-Test-Client-IP,
  at most 2 in flight. When two are still in flight at the moment the next is due, the next waits and the delay is recorded; it is not skipped and the
  schedule is not shifted (every ask is planned at accept + 30 s x n).
* **Measurement:** until the upload job is ready plus 30 s. Sending stops at job ready plus 30 s: the 14 is a cap, not a target. An upload that is ready
  early gets fewer asks, one that outlasts the schedule gets no more after the 14th, and the boot records how many were sent (`asks.sent`, `asks.unsent`,
  `asks.stopped_by`).
* **The boot JSON records** `window`, `attempt`, `workload` (mode `paced`, `asks_cap`, `interval_s`, `max_in_flight`, `tail_s` and the `derivation`:
  day cap and estimate in micro-dollars, the models) and `asks.schedule` (for each ask sent: `planned_s`, `sent_s`, `delay_s`, `end_s`, `outcome`, in
  seconds after accept), `asks.planned_offsets_s`, `asks.max_in_flight_seen` and `asks.distinct_addresses`.
* `--window` also picks the registry entry and the state file (`W1c.json`, written by `staging.py create W1c`): W1b's saturating loop is unchanged and
  W1c cannot run it. The window fixes the workload, so `--tail-s` and `--concurrency` are refused when they differ from it. The aggregate refuses to
  pass a boot that did not record the registered workload (30 s, 2 in flight, a 30 s tail, a count that is the one derived from the cap and the
  estimate it recorded), so a test-sized run can never be mistaken for the window.

**Stop rule (encoded in `aggregate`, window W1c):** an INVALID boot is re-run unchanged ONCE; if still INVALID, W1c is INCOMPLETE and nothing else is amended; a memory FAIL is a FAIL.

* The re-run is `--attempt 2` of the same `--boot` on a freshly created machine; the attempt-1 file is kept and both are reported (`attempts`). The boot's
  last attempt decides it. An attempt 2 is legal only after an INVALID attempt 1: a re-run of a PASS or an INCONCLUSIVE boot, a third attempt, a gap
  and a machine used twice are `problems` that keep the window from passing. A boot that is INVALID with no re-run yet is `rerun_due`; one that is INVALID
  twice is `stopped`. Both give INCOMPLETE (never INVALID, which W1b's aggregate would say). A FAIL in ANY attempt is a FAIL.
* Open: an INCONCLUSIVE boot (evidence unreadable) is not re-run by this rule; the window is then INCONCLUSIVE and the owner decides.

### Sequence (PowerShell, from the repository root)

```powershell
# 0. the owner approves the quote first; W1b's window must be destroyed (the app names are shared)
uv run python scripts/staging.py destroy W1b --yes                   # only if its apps still exist
uv run python scripts/staging.py quote W1c
uv run python scripts/staging.py create W1c --org <org> --approve-quote 0.10
uv run python scripts/staging.py seed W1c
uv run python scripts/staging.py deploy W1c mockllm
uv run python scripts/staging.py deploy W1c api --build-arg EMBEDDER_VARIANT=fp32    # NOT KEEP_UNPATCHED=1: the tool refuses it
uv run python scripts/staging.py preflight W1c       # this first machine is a warm-up: it proves the build and the deployed config

# 1. three measured boots, each on a freshly created machine (the driver starts BEFORE the deploy)
$dir = "$env:TEMP\w1c"; New-Item -ItemType Directory -Force $dir | Out-Null
foreach ($n in 1, 2, 3) {
    foreach ($m in @(flyctl machine list -a semigraph-stg --json | ConvertFrom-Json)) {
        if ($m.state -ne "destroyed") { flyctl machine destroy $m.id --force -a semigraph-stg }
    }
    $job = Start-Job -ArgumentList (Get-Location).Path, $n, $dir -ScriptBlock {
        param($repo, $n, $dir); Set-Location $repo
        uv run python -m tools.probe.w1b boot --window W1c --boot $n --wait-for-machine-s 2700 --out "$dir\boot-$n.json"
    }
    uv run python scripts/staging.py deploy W1c api --build-arg EMBEDDER_VARIANT=fp32     # a new machine id and creation time
    Receive-Job $job -Wait -AutoRemoveJob
}

# 2. the stop rule: ONLY a boot whose JSON says INVALID is re-run, ONCE, unchanged, on a freshly created machine (boot-$n.json is kept)
foreach ($n in 1, 2, 3) {
    $first = Get-Content "$dir\boot-$n.json" -Raw | ConvertFrom-Json
    if ($first.verdict -ne "INVALID" -or (Test-Path "$dir\boot-$n-a2.json")) { continue }     # not INVALID, or its one re-run exists
    foreach ($m in @(flyctl machine list -a semigraph-stg --json | ConvertFrom-Json)) {
        if ($m.state -ne "destroyed") { flyctl machine destroy $m.id --force -a semigraph-stg }
    }
    $job = Start-Job -ArgumentList (Get-Location).Path, $n, $dir -ScriptBlock {
        param($repo, $n, $dir); Set-Location $repo
        uv run python -m tools.probe.w1b boot --window W1c --boot $n --attempt 2 --wait-for-machine-s 2700 --out "$dir\boot-$n-a2.json"
    }
    uv run python scripts/staging.py deploy W1c api --build-arg EMBEDDER_VARIANT=fp32
    Receive-Job $job -Wait -AutoRemoveJob
}

# 3. the window's verdict from EVERY boot file in the directory, so no attempt can be left out (INVALID twice: INCOMPLETE, stop; a memory FAIL: FAIL)
$files = (Get-ChildItem "$dir\boot-*.json").FullName
uv run python -m tools.probe.w1b aggregate $files --out "$dir\w1c.json"

# 4. evidence, then teardown (verified gone)
uv run python scripts/staging.py snapshot W1c --out "$dir\snapshot"
uv run python scripts/staging.py destroy W1c --yes
```

Everything else of W1b's sequence applies (the first machine is a warm-up; destroy EVERY machine of the app before each boot; the same `UNVERIFIED`
list). Each boot takes about 10 to 25 minutes (boot, a job of about 190 CPU-seconds that may be throttled, 30 s of tail, collection). The result is
recorded by hand under `S6_w1c` in `artifacts/m5_spikes.json` (its owner merges); W1b's INCOMPLETE record and its four boots stay as stress data.

Tests: `tests/test_tools_probe_memsample.py` (fake `/proc`), `tests/test_tools_probe_w1b.py` (the verdicts at their boundaries, the
aggregate, the workload against an `httpx.MockTransport`, flyctl stubbed; for W1c: the derived count and that it follows its inputs, the pacing
decision on a fake clock, the sender, a paced boot end to end, the stop rule, the registered-workload check, the docs), `tests/test_staging_script.py` and
`tests/test_staging_tomls.py` (the windows, their toml and the build-argument policy).
