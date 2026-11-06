# Upload embed timeout, Plan 0: what the 30-page fixture really is and what it costs to embed

Date: 2026-11-06. Branch v2. Spec: `docs/specs/upload-embed-timeout.md` (Plan 0). Machine-readable data:
`artifacts/upload_embed_local_timing.json`. Cost: $0. Local CPU only: no Fly, no production Neo4j, no LLM call, no model
download. The spec was not edited; the changes it needs are listed at the end.

## Short answer

- The fixture (`tests/upload_fixtures.py` `max_size_pdf(30, 2700)`) parses to 30 pages and **60 chunks**, the same as every live
  boot. It embeds **14,151 tokens** (175 to 296 per chunk, mean 235.9). That is neither 13,923 (the spec's reconstruction, which I
  could not reproduce: 228 tokens, 1.6%, too low) nor 16,000 (the cap: the fixture is 88.4% of it).
- On one thread the local fp32 embedder takes **7.0 ms per token** (142.5 tokens/s on one desktop performance core, 1.66x the
  85.8 tokens/s Fly figure). Cost is proportional to tokens: every fit intercept is within 0.05 s of zero (under 5% of a chunk), so there is no per-call
  overhead to correct for. The 85.8 tok/s figure therefore carries over to any chunk shape (measured on this desktop core; that a Fly performance core
  has the same shape is **inferred**: same onnxruntime and model, different silicon).
- Two threads are **1.95x** faster (Windows, two performance cores) and 1.91x (WSL): a ratio for the record, valid at full speed,
  not under the Fly throttle (the quota is shared between a machine's vCPUs, as the spec says).
- The ceiling table is built on the 16,000-token cap, so it does not move. **The 2,100 s ceiling still holds its 1.5x rule**:
  1.595x (f = 1.18) and 1.77x (1.31) at the 12% share, and 1.582x against W1c boot 1 scaled to the cap.

## What was run

1. **Parse and chunk, WSL Ubuntu-24.04** (the parse sandbox with `RLIMIT_AS` is Linux-only). The repo's own code on the
   `/mnt/c` checkout: `parse_document(pdf, "pdf", timeout_s=90, max_pages=30)` (the sandboxed subprocess), `canonical_text`,
   `OnnxBackend.count_tokens` (a bare instance with only the tokenizer set, truncation 8,192 as in `OnnxBackend.__init__`),
   then `jobs._build_units_and_chunks` with `Settings(_env_file=None)` (so `.env` is never read): `max_tokens` 512 from
   `upload_max_chunk_tokens`, the `chunk_units` defaults 1,200 and 1,800 characters. Python 3.12.3, packages at the
   `deploy/requirements-serve.txt` pins (tokenizers 0.23.2, pypdfium2 5.13.0, pdfplumber 0.11.10, pdfminer-six 20260107).
   Cross-check on Windows (tokenizers 0.22.2): identical chunks, identical text.
2. **fp32 model.** `models/qwen3-embedding-0.6b-fp32` was absent. `scripts/predequantize_embedder.py` built it from the local q8
   model (sha256 `0b2937d8...cb710`, equal to `source_model_sha256` in `artifacts/embedder_parity_fp32.json`) with no network, into a
   scratch directory outside the repo and OneDrive. 196/196 nodes bit-exact; the output hashes (`model_fp32.onnx` `7442a36f...`,
   `.data` `149b4004...`) **equal the parity-checked model**. Fly's image builds from its own q8, whose hash may differ; the
   output does not depend on how the source stores its weights (`predequantize_embedder.py` docstring).
3. **Timing.** The repo's `OnnxBackend(model, threads=N)` as `serve` builds it (`embeddings.py:111`), then
   `encode_passages([text])` one chunk per call, timed with `perf_counter`. Two warm-up calls, then 3 passes over the 60 chunks
   in order; the per-chunk median of 3 is used. Primary run on **Windows native** (repo `.venv`, onnxruntime 1.29.0, the Fly
   version), AC power, host CPU about 3%. The CPU is hybrid (8 performance and 16 efficiency cores), so the process was pinned
   to performance cores before the session was created (threads 1: logical cpu 10; threads 2: cpus 10 and 11). A secondary run
   under **WSL** (onnxruntime 1.29.0, Linux; vCPUs 4 and 4,5, the host places them). A labelled **length sweep** (prefixes of
   the fixture text of 16 to 512 tokens, 3 reps each) is kept out of the fixture totals.

## Results

Fixture, 60 chunks, 14,151 tokens (onnxruntime 1.29.0, fp32):

| Run | pass totals (s) | core-s (sum of medians) | tok/s | s per token | spread |
|---|---|---|---|---|---|
| Windows, 1 thread | 100.4, 99.8, 99.3 | 99.28 | 142.5 | 0.00702 | pass CV 0.5% |
| Windows, 2 threads | 51.4, 50.9, 51.8 | 50.88 | 278.2 | 0.00360 | pass CV 0.7% |
| WSL, 1 thread | 102.5, 102.0, 101.9 | 101.59 | 139.3 | 0.00718 | pass CV 0.3% |
| WSL, 2 threads | 53.6, 53.5, 53.8 | 53.20 | 266.0 | 0.00376 | pass CV 0.2% |

- Linear fit, seconds against tokens (Windows, 1 thread): fixture chunks only (175 to 296 tokens) slope 7.198 ms per token,
  intercept -0.043 s, R2 0.9995; with the length sweep (16 to 512 tokens) slope 7.147 ms, intercept -0.026 s, R2 0.9986. WSL and
  the 2-thread fits agree (intercepts -0.043 to +0.038 s; the sweep intercepts are within 2 standard errors of zero, the
  narrow fixture-only fits extrapolate slightly negative, -0.015 to -0.043 s). The narrow fixture range did not make the slope
  unreliable: the sweep, 16 to 512 tokens, gives the same line. Seconds per token is flat to 1% across chunk-size thirds
  (6.96, 7.02, 7.04 ms at 182, 236 and 290 tokens a chunk). The first call is not slower (2.10 s cold against 2.07 to 2.09 s warm).
- Per-chunk seconds against tokens (Windows, 1 thread): 182 tokens 1.26 s, 236 tokens about 1.65 s (the mean chunk), 296 tokens
  2.07 s. Every row is in the JSON.
- Threads 2 against 1: total 1.951x (per chunk 1.92 to 2.00) on Windows, 1.910x (1.76 to 2.10) on WSL. Seconds per token
  0.00702 to 0.00360.
- The shape check: applying the fitted per-call overhead to the 85.8 tok/s run (54 chunks, 15,849 tokens, 184.8 s) puts the
  fixture at 164.1 to 165.2 core-s against 164.9 from tokens / 85.8, and a cap job at 182 to 188 core-s whatever its chunk count
  (31 to 120), against 186.5 (**inferred** for Fly, as above). Nothing to correct.
- A desktop performance core is 1.66x the Fly core in tokens per second (142.5 against 85.8). That ratio is not a Fly
  prediction (different silicon, no throttle, no class factor); it is only a reason the absolute seconds above must not be used.

## The ceiling table with the measured numbers

The main table (cap job, 16,000 tokens, 186.5 core-s at 85.8 tok/s) is unchanged: 930 / 1,082 / 1,264 / 1,317 / 1,580 / 1,975 /
2,634 s at f = 1.18 for 17% / 14.6% / 12.5% / 12% / 10% / 8% / 6%. The 2,100 s ceiling needs a share of 8.9%, 7.5%, 6.8% (f = 1,
1.18, 1.31) for the cap job, and 7.85%, 6.66%, 6.0% for this fixture. The 1.5x rule at 12%: needs 1,975 s (f = 1.18) or 1,779 s
(f = 1.31); 2,100 s gives **1.595x and 1.770x**. It holds.

What the measured fixture changes (everything built on 13,923):

| Quantity | Spec now | With 14,151 tokens |
|---|---|---|
| Fixture core-s at 85.8 tok/s | 162.3 | **164.9** |
| Fixture at the 12% share, f = 1.31 / 1.18 / 1 | not given | 1,049 / 1,165 / 1,374 s |
| W1c boot 1 scaled to the cap | 1,349 s, 2,100 s covers 1.556x | **1,327.7 s, 1.582x** |
| Fixture tokens boot 1 needs to keep 1.5x at 2,100 s | not given | at least 13,421 (measured 14,151) |
| Boot 1 average share (whole job, f = 1.31 to 1.18) | 10.5% to 13.5% | **10.7% to 11.9%** |
| Boot 2 average share | 18% to 23% | 18.5% to 20.5% |
| Boot 3 average share | 32% to 41% | 32.5% to 36.1% |
| Council 8: fixture at 17%, f = 1.31 to 1.18 | 729 to 809 s | 741 to 822 s (W1b boot 1's 842 s is above it and inside the cap-job range 837 to 930 s) |
| Asks bullet: fixture shared-core-s | 124 to 158 | **126 to 140** (158 was the cap job); 14 asks 3.4% to 3.7% (still "3% to 4%"); saturating load 36% to 40% (was 32% to 40%) |

**Boot 1 is now below the 12% over its whole range, not only at its low end.** With the measured tokens its whole-job average
share is 10.7% (f = 1.31) to 11.9% (f = 1.18); the spec's sentence says only its low end is below 12%. At 11.9% and f = 1.18 a
cap job needs 1,328 s, so 1.5x is 1,992 s; at 10.7% and f = 1.31 it needs 1,330 s, so 1.5x is 1,996 s. The 2,100 s ceiling
clears both by about 5% (1.581x and 1.578x; 1.582x from boot 1's 1,174.3 s scaled to the cap). These are whole-job averages
(`ready_s` includes parse, chunking and indexing), so the embed-only share is a little higher than they show. The 1.5x rule
holds, with less slack against boot 1 than against the 12% row (1.595x).

## What changes in the spec (not edited)

1. Inputs, "Fixture size": replace the lower reconstruction (13,923 tokens, 162.3 core-s, "Plan 0 settles it") with the measured
   14,151 embedded tokens (whole-document count 14,134), 60 chunks, 175 to 296 tokens, 164.9 core-s; keep 16,000 / 186.5 as the
   upper bound. Add one line: seconds are proportional to tokens (intercept within 0.05 s of zero), so 85.8 tok/s transfers across
   chunk shapes.
2. Council 8 reconciliation and the boot-1 sentence: 741 to 822 s (fixture at 17%), boot 1 share 10.7% to 11.9%, scaled to the cap
   1,327.7 s, 1.582x (replaces 1.556x). Boots 2 and 3: 18.5% to 20.5% and 32.5% to 36.1%. Decision 3's odds (only boot 1 of 3
   valid) are unchanged. In the ceiling-rule bullet, "boot 1's low end is a measured share below the 12%" becomes "boot 1's
   whole range (10.7% to 11.9%, whole-job averages) is below the 12%", with the check beside it: 1.5x of the cap job at 11.9% (f =
   1.18) is 1,992 s and at 10.7% (f = 1.31) 1,996 s, both under 2,100 s (1.58x).
3. Asks bullet: 126 to 140 shared-core-s, 36% to 40% for the saturating load.
4. "Not in this spec", B: add the local fp32 measurement, 1.95x for two threads (1.91x on Linux, WSL), next to the q8 1.7x
   "not re-run" figure. The reason it is not pursued (shared quota after the burst) is unchanged.
5. Requirement 4's last-chunk figure: the 461-token / 5.59 s pairing (marked inferred) is consistent with proportionality
   (461 / 85.8 = 5.37 s, 4.0% lower), and the about-70 s last chunk at the ceiling's floor share is supported (512 / 85.8 /
   (1.18 x 0.075) = 67.4 s). One line, no new run.
6. Plan 0 (a) and (b) are done; (c) is the config comment, part of Plan 1. The ceiling (2,100 s), the table, the validator and
   every figure in requirements 1 to 8 stay as they are.

## Deviations from the plan

- fp32 was absent from `models/` and from the scratch location the parity artifact names (`embed_paths/fp32` exists in the
  session scratchpad but is empty), so it was built (allowed: from the local q8, no network), into a new scratch directory, not
  `models/`. It is byte-identical to the parity-checked model. The 2.4 GB output stays in the scratch directory; delete it if the space is needed.
- WSL has no `python3-venv`/ensurepip and no sudo, and wipes `/tmp` on every instance restart. The throwaway `/tmp/plan0-venv` was
  made with `python3 -m venv --without-pip` and filled with Linux wheels that `uv` fetched on Windows (package downloads from
  PyPI at the `requirements-serve.txt` pins; no model download); each WSL invocation rebuilt it. The repo `.venv` was not touched.
- Timing is mainly Windows native pinned to performance cores (the CPU is hybrid; WSL cannot pin a physical core), with WSL as the
  Linux check. Both agree within 2.3% at one thread and 4.6% at two.
- Added: the length sweep, 3 passes, the shape check. q8 was not timed (fp32 is what ships). Memory (RSS) was not measured.

## Exact commands

Scratch directory `SP` is the session scratchpad; scripts are in it (`plan0_chunk.py`, `plan0_time.py`, `plan0_analyze.py`,
`wsl_setup.sh`, `wsl_time.sh`). Paths shortened.

```
# Linux wheels for the WSL venv (from Windows; pins from deploy/requirements-serve.txt)
uv pip install --python-platform x86_64-manylinux_2_28 --python-version 3.12 --target $SP/linux-site \
  numpy==2.5.3 onnxruntime==1.29.0 tokenizers==0.23.2 pypdfium2==5.13.0 pdfplumber==0.11.10 pdfminer.six==20260107 \
  pillow==12.3.0 cryptography==50.0.1 charset-normalizer==3.5.1 rapidfuzz==3.14.6 neo4j==6.3.0 pydantic==2.13.5 \
  pydantic-settings==2.15.0 python-dotenv==1.2.3 python-docx==1.2.0 lxml==6.1.3

# fp32 model, from the local q8, no network
PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 .venv/Scripts/python.exe scripts/predequantize_embedder.py \
  --src models/qwen3-embedding-0.6b-q8 --out $SP/fp32model --threads 4

# (a) parse and chunk under WSL (wsl_setup.sh makes /tmp/plan0-venv, then runs the command)
wsl -d Ubuntu-24.04 -- bash $SP/wsl_setup.sh /tmp/plan0-venv/bin/python $SP/plan0_chunk.py $REPO \
  $REPO/models/qwen3-embedding-0.6b-q8/tokenizer.json $SP/chunks_wsl.json $SP/fixture_30p.pdf
.venv/Scripts/python.exe $SP/plan0_chunk.py $REPO ...tokenizer.json $SP/chunks_win.json $SP/fixture_30p_win.pdf   # cross-check

# (b) timing, Windows, pinned to performance cores
.venv/Scripts/python.exe $SP/plan0_time.py $REPO $SP/fp32model/model_fp32.onnx $SP/chunks_wsl.json 1 $SP/time_win_t1.json --passes 3 --cpus 10
.venv/Scripts/python.exe $SP/plan0_time.py $REPO $SP/fp32model/model_fp32.onnx $SP/chunks_wsl.json 2 $SP/time_win_t2.json --passes 3 --cpus 10,11
# (b) timing, WSL (wsl_time.sh copies the model to /tmp and runs --cpus 4 / --cpus 4,5)
wsl -d Ubuntu-24.04 -- bash $SP/wsl_time.sh

# fits, ratios, recomputed table, the artifact
.venv/Scripts/python.exe -I $SP/plan0_analyze.py $SP artifacts/upload_embed_local_timing.json
```

Fixture: 94,456 bytes, sha256 `8c7890ac...b5b8df`, 30 pages, method pypdfium2, 84,079 characters, 31 units, 60 chunks.
