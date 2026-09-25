"""Regenerate src/semigraph/artifacts/examples.json from a benchmark run of ONE graph snapshot.

    PYTHONPATH=src python scripts/build_examples.py \\
        --runs data/processed/eval_runs.v2-baseline.jsonl --snapshot snap-20260924-7feaaf9bfe

The service seeds these answers into its cache as free example clicks, but only when the file's
snapshot id equals the running graph's (an answer from another data snapshot could cite text that
is no longer current). No LLM call: the answers are the hybrid runs already paid for by `eval`.
"""

import argparse
import json
import re
import sys
from pathlib import Path

SNAPSHOT_RE = re.compile(r"^snap-[0-9]{8}-[0-9a-f]{10}$")
EXAMPLES_PATH = Path(__file__).resolve().parents[1] / "src" / "semigraph" / "artifacts" / "examples.json"
BENCHMARK_PATH = EXAMPLES_PATH.with_name("benchmark.json")


def build_examples(runs: list[dict], benchmark: list[dict], snapshot_id: str, *, source: str) -> dict:
    """One example per benchmark question, in benchmark order, from its hybrid run."""
    if not SNAPSHOT_RE.match(snapshot_id):
        raise ValueError(f"not a snapshot id: {snapshot_id!r} (expected snap-YYYYMMDD-<10 hex>)")
    hybrid = {r["id"]: r for r in runs if r["system"] == "hybrid"}
    missing = [b["id"] for b in benchmark if b["id"] not in hybrid]
    if missing:
        raise ValueError(f"no hybrid run for benchmark question(s): {', '.join(missing)}")
    examples = []
    for b in benchmark:
        r = hybrid[b["id"]]
        if not r["answer"].strip():
            raise ValueError(f"{b['id']}: empty answer")
        if r["hallucinated"]:
            raise ValueError(f"{b['id']}: hallucinated citations {r['hallucinated']}")
        examples.append({"id": b["id"], "type": b["type"], "question": b["q"], "answer": r["answer"],
                         "citations": sorted(r["cited"]), "hallucinated": []})
    return {"source": source, "snapshot_id": snapshot_id, "examples": examples}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=Path, required=True, help="benchmark runs jsonl (from `semigraph eval --runs-file`)")
    ap.add_argument("--snapshot", required=True, help="snapshot id of the graph those runs were answered against")
    ap.add_argument("--source", default=None, help="provenance line stored in the file")
    ap.add_argument("--out", type=Path, default=EXAMPLES_PATH)
    args = ap.parse_args()
    runs = [json.loads(line) for line in args.runs.read_text(encoding="utf-8").splitlines() if line.strip()]
    benchmark = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    source = args.source or f"benchmark run {args.runs.name}, hybrid system, Claude Sonnet 5"
    doc = build_examples(runs, benchmark, args.snapshot, source=source)
    args.out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(doc['examples'])} examples for {args.snapshot} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
