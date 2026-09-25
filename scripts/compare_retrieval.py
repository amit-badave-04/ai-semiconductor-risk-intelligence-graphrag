"""Snapshot and diff what retrieval returns for the gold benchmark questions.

Two modes (both read-only against the graph; no LLM calls, no API spend):

    python scripts/compare_retrieval.py snapshot --out data/backups/retrieval_v1.json
    python scripts/compare_retrieval.py diff data/backups/retrieval_v1.json data/backups/retrieval_v2.json

`snapshot` runs ``hybrid_retrieve`` for every question in the packaged benchmark against
whatever Neo4j the environment points at (NEO4J_URI / NEO4J_PASSWORD / NEO4J_DATABASE) using
whichever ``semigraph`` is on PYTHONPATH — so the same script measures the v1 code on the v1
graph (PYTHONPATH = a ``git archive v1-final`` checkout) and the v2 code on the v2 graph.
`diff` lists, per question and per context layer, what was gained and lost. It is the
"every answer-relevant fact that changed" report of the M1 gate.
"""

import argparse
import json
import sys
from pathlib import Path

LAYERS = ("edges", "metrics", "risks", "temporal", "chunks")


def summarize(retrieval: dict) -> dict:
    """A JSON-safe, order-independent fingerprint of one retrieval result."""
    edges = sorted({f"{e['source']} -{e['relation']}-> {e['target']}" for e in retrieval.get("edges", [])})
    metrics = sorted({f"{m['company']}|{m['metric']}|{m.get('period_end')}|{m.get('value')}|{m.get('unit', 'USD')}"
                      for m in retrieval.get("metrics", [])})
    risks = sorted({r["chunk_id"] for r in retrieval.get("risks", [])})
    temporal = sorted({f"{t['company']}|{t['lineage']}" for t in retrieval.get("temporal", [])})
    chunks = sorted({c["chunk_id"] for c in retrieval.get("chunks", [])})
    return {"anchors": sorted(retrieval.get("anchors", {})), "anchor_defaulted": retrieval.get("anchor_defaulted"),
            "edges": edges, "metrics": metrics, "risks": risks, "temporal": temporal, "chunks": chunks}


def diff_summaries(before: dict, after: dict) -> dict:
    """Per layer: what only ``before`` has (lost) and what only ``after`` has (gained)."""
    out: dict = {}
    for layer in LAYERS:
        b, a = set(before.get(layer, [])), set(after.get(layer, []))
        out[layer] = {"before": len(b), "after": len(a), "lost": sorted(b - a), "gained": sorted(a - b)}
    out["anchors_changed"] = before.get("anchors") != after.get("anchors")
    return out


def diff_snapshots(before: dict, after: dict) -> dict:
    """Question id -> per-layer diff (only questions present in both snapshots)."""
    return {qid: diff_summaries(before[qid], after[qid]) for qid in before if qid in after}


def render_diff(diff: dict, limit: int = 4) -> str:
    lines = []
    for qid, layers in diff.items():
        changed = [layer for layer in LAYERS if layers[layer]["lost"] or layers[layer]["gained"]]
        head = f"{qid}: " + ("identical" if not changed and not layers["anchors_changed"] else "CHANGED " + ",".join(changed))
        lines.append(head)
        for layer in changed:
            d = layers[layer]
            lines.append(f"    {layer}: {d['before']} -> {d['after']}  (-{len(d['lost'])} / +{len(d['gained'])})")
            for tag, items in (("lost", d["lost"]), ("gained", d["gained"])):
                for item in items[:limit]:
                    lines.append(f"        {tag}: {item[:150]}")
                if len(items) > limit:
                    lines.append(f"        {tag}: ... and {len(items) - limit} more")
    return "\n".join(lines)


def take_snapshot(out: Path) -> None:
    from semigraph.artifacts import load_benchmark
    from semigraph.config import get_settings
    from semigraph.embeddings import Embedder
    from semigraph.graph import client
    from semigraph.retrieval.retriever import hybrid_retrieve

    settings = get_settings()
    driver = client.get_driver(settings)
    embedder = Embedder()
    try:
        result = {q["id"]: summarize(hybrid_retrieve(q["q"], driver, embedder)) for q in load_benchmark()}
    finally:
        driver.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"wrote {len(result)} question fingerprints -> {out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--out", type=Path, required=True)
    d = sub.add_parser("diff")
    d.add_argument("before", type=Path)
    d.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "snapshot":
        take_snapshot(args.out)
    else:
        before, after = (json.loads(p.read_text(encoding="utf-8")) for p in (args.before, args.after))
        print(render_diff(diff_snapshots(before, after)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
