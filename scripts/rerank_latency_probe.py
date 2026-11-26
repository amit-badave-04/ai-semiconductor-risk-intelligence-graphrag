"""Time the MiniLM rerank stage on the serve machine (M2 R24: rerank adds at most 2 s at p95 on a rested shared-cpu-2x machine).

    python rerank_latency_probe.py [--runs N] [--warmup N] [--max-tokens N] [--max-candidates N]

No log line carries the rerank stage's time and Turnstile blocks scripted asks, so this loads the reranker the service loads (same
model directory and thread count, read from the machine's environment the way the app reads them, with the app's defaults) and
times ``rerank`` on the SERVED workload (decision 012): 20 candidates (the rerank depth) and 25 (the served cap), at the served
pair window (192 tokens). Size and window are read from the served config (``semigraph.retrieval.fusion``), not hardcoded here;
``--max-tokens`` and ``--max-candidates`` override the window and the larger size (to probe a fallback setting). Each passage is
longer than the window, so every pair is cut at the full window. The per-request time guard (RETRIEVAL_RERANK_BUDGET_S) is not in
the timed call: the probe measures the machine, the guard bounds the user. It reads the cgroup's CPU throttling before and
after.

Run it once, on a rested machine about 70 minutes after a deploy (burst credits refilled), never back to back: a second run
measures a drained machine, which the guard covers and the gate does not. It does not touch the database, ask a model or read a secret; the stage's candidate-text fetch from the graph is not included.
It loads a second copy of the model next to the running service, which keeps serving. Measured, the probe process reaches
about 305 MB RSS (importing semigraph.retrieval pulls in the answerer and litellm), not just the model's size. Run it with
no upload in flight (uploads are refused below mem_available_kb 1,250,000), and do not take an RSS or MemAvailable comparison while it runs.

Stage it like every ad-hoc operator script (docs/RUNBOOK.md), in the root-only directory, and run it isolated:

    flyctl ssh console -a semigraph -C "mkdir -m 700 -p /root/ops"
    flyctl ssh sftp shell -a semigraph
        put scripts/rerank_latency_probe.py /root/ops/rerank_latency_probe.py
    flyctl ssh console -a semigraph -C "sh -c 'cd /root/ops && python -I rerank_latency_probe.py'"

Prints one JSON line (p50, p95 and max seconds per size, throttling deltas, the verdict). Exit status: 0 PASS, 1 FAIL (some p95 is
over 2 s), 2 the model did not load.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

# Before any semigraph import: LiteLLM (loaded by semigraph.retrieval) fetches its price table from the internet unless told to use
# its bundled copy, and the production machine must make no call it was not asked to make.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

GATE_S = 2.0
MODEL_KEY = "minilm"
DEFAULT_MODEL_DIR = "/srv/models"       # fly.toml RETRIEVAL_RERANK_MODEL_DIR
DEFAULT_THREADS = 1                     # config.Settings.retrieval_rerank_threads
CPU_STAT = Path("/sys/fs/cgroup/cpu.stat")
QUERY = "How did export controls on advanced AI chips to China affect the company's data center revenue and outlook?"
_SENTENCE = ("The company recorded revenue from data center products, and export restrictions on advanced semiconductors "
             "required licenses for shipments to certain customers, which reduced sales in the period and may continue to "
             "affect results, supply commitments, inventory provisions and gross margin in future quarters. ")


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile (no interpolation, so a p95 is always a time that was observed)."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    return ordered[max(1, math.ceil(pct / 100 * len(ordered))) - 1]


def served_workload() -> tuple[int, int, int]:
    """(rerank depth, served candidate cap, served pair window in tokens) from the served config."""
    from semigraph.retrieval.fusion import RERANK_DEPTH, RERANK_SERVED_CANDIDATES, RERANK_SERVED_TOKENS

    return RERANK_DEPTH, RERANK_SERVED_CANDIDATES, RERANK_SERVED_TOKENS


def worst_case_passages(count: int, max_tokens: int = 384) -> list[str]:
    """``count`` distinct passages, each longer (in words, so in tokens) than the reranker's whole token window."""
    repeats = max(16, max_tokens // 20 + 1)             # a sentence is about 45 words
    return [f"Passage {i}. " + _SENTENCE * repeats for i in range(count)]


def time_rerank(reranker, query: str, passages: Sequence[str], *, runs: int, warmup: int,
                clock: Callable[[], float] | None = None) -> dict:
    clock = clock or time.perf_counter
    for _ in range(warmup):
        reranker.rerank(query, passages)
    seconds = []
    for _ in range(runs):
        began = clock()
        reranker.rerank(query, passages)
        seconds.append(clock() - began)
    return {"candidates": len(passages), "runs": runs, "p50_s": round(percentile(seconds, 50), 4),
            "p95_s": round(percentile(seconds, 95), 4), "max_s": round(max(seconds), 4)}


def cpu_stat(path: Path = CPU_STAT) -> dict[str, int] | None:
    """The cgroup v2 cpu.stat counters (``nr_throttled``, ``throttled_usec``, ...), or None where the file is absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    pairs = (line.split() for line in text.splitlines())
    return {p[0]: int(p[1]) for p in pairs if len(p) == 2 and p[1].isdigit()}


def verdict(results: Sequence[dict]) -> str:
    return "PASS" if all(r["p95_s"] <= GATE_S for r in results) else "FAIL"


def load_reranker(max_tokens: int | None = None):
    from semigraph.retrieval.rerank import get_reranker

    root = Path(os.environ.get("RETRIEVAL_RERANK_MODEL_DIR", DEFAULT_MODEL_DIR))
    threads = int(os.environ.get("RETRIEVAL_RERANK_THREADS", DEFAULT_THREADS))
    return get_reranker(MODEL_KEY, root=root, threads=threads, max_tokens=max_tokens)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=30, help="timed runs per size (default 30)")
    parser.add_argument("--warmup", type=int, default=2, help="untimed runs per size first (default 2)")
    parser.add_argument("--max-tokens", type=int, default=None, help="pair window in tokens (default: the served window)")
    parser.add_argument("--max-candidates", type=int, default=None, help="the larger candidate count (default: the served cap)")
    args = parser.parse_args(argv)
    if args.runs < 1 or args.warmup < 0:
        parser.error("--runs must be at least 1 and --warmup at least 0")
    if (args.max_tokens is not None and args.max_tokens < 1) or (args.max_candidates is not None and args.max_candidates < 1):
        parser.error("--max-tokens and --max-candidates must be at least 1")
    depth, served_cap, served_tokens = served_workload()
    max_tokens = args.max_tokens or served_tokens
    sizes = (depth, args.max_candidates or served_cap)

    reranker = load_reranker(max_tokens)
    if reranker is None:
        print(f"rerank model {MODEL_KEY!r} not loaded (see the warning above): nothing timed")
        return 2
    before = cpu_stat()
    results = [time_rerank(reranker, QUERY, worst_case_passages(n, max_tokens), runs=args.runs, warmup=args.warmup) for n in sizes]
    after = cpu_stat()
    throttling = None
    if before is not None and after is not None:
        throttling = {k: after[k] - before[k] for k in ("nr_periods", "nr_throttled", "throttled_usec") if k in before and k in after}
    report = {"model": MODEL_KEY, "max_tokens": max_tokens, "gate_s": GATE_S, "results": results, "throttling": throttling, "verdict": verdict(results)}
    print(json.dumps(report))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
