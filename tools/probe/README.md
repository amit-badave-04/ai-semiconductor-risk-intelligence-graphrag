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
