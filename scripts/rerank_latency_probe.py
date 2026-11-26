"""Time the MiniLM rerank stage on the serve machine (M2 R24: rerank adds at most 2 s at p95 on the throttled machine).

    python rerank_latency_probe.py [--runs N] [--warmup N]

No log line carries the rerank stage's time and Turnstile blocks scripted asks, so this loads the reranker the service loads (same
model directory and thread count, read from the machine's environment the way the app reads them, with the app's defaults) and
times ``rerank`` on worst-case inputs: 20 candidates (the rerank depth) and 35 (the depth plus the slot-guarantee heads), each
passage longer than the 384-token window so every pair is cut at the full window. It reads the cgroup's CPU throttling before and
after. It does not touch the database, ask a model or read a secret; the stage's candidate-text fetch from the graph is not included.
It loads a second copy of the model (about 23 MB) next to the running service, which keeps serving.

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

GATE_S = 2.0
SIZES = (20, 35)
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


def worst_case_passages(count: int) -> list[str]:
    """``count`` distinct passages, each longer than the reranker's whole token window."""
    return [f"Passage {i}. " + _SENTENCE * 16 for i in range(count)]


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


def load_reranker():
    from semigraph.retrieval.rerank import get_reranker

    root = Path(os.environ.get("RETRIEVAL_RERANK_MODEL_DIR", DEFAULT_MODEL_DIR))
    threads = int(os.environ.get("RETRIEVAL_RERANK_THREADS", DEFAULT_THREADS))
    return get_reranker(MODEL_KEY, root=root, threads=threads)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=30, help="timed runs per size (default 30)")
    parser.add_argument("--warmup", type=int, default=2, help="untimed runs per size first (default 2)")
    args = parser.parse_args(argv)
    if args.runs < 1 or args.warmup < 0:
        parser.error("--runs must be at least 1 and --warmup at least 0")

    reranker = load_reranker()
    if reranker is None:
        print(f"rerank model {MODEL_KEY!r} not loaded (see the warning above): nothing timed")
        return 2
    before = cpu_stat()
    results = [time_rerank(reranker, QUERY, worst_case_passages(n), runs=args.runs, warmup=args.warmup) for n in SIZES]
    after = cpu_stat()
    throttling = None
    if before is not None and after is not None:
        throttling = {k: after[k] - before[k] for k in ("nr_periods", "nr_throttled", "throttled_usec") if k in before and k in after}
    report = {"model": MODEL_KEY, "gate_s": GATE_S, "results": results, "throttling": throttling, "verdict": verdict(results)}
    print(json.dumps(report))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
