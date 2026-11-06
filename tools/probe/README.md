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

Tests: `tests/test_tools_probe_memsample.py` (fake `/proc`), `tests/test_tools_probe_w1b.py` (the verdicts at their boundaries, the
aggregate, the workload against an `httpx.MockTransport`, flyctl stubbed), `tests/test_staging_script.py` and
`tests/test_staging_tomls.py` (the window, its toml and the build-argument policy).
