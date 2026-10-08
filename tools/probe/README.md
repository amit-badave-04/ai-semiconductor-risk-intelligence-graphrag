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
