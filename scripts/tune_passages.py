"""Parameter sweep of the passage layer against the FROZEN sentence gold, on the DEVELOPMENT pairs only.

    python scripts/tune_passages.py [--gold artifacts/gold/risk_items_gold.json]

Prints, per parameter setting, the micro-averaged sentence-level precision and recall of the positive class (removed on the
older side, added on the newer side) over the six development pairs. Held-out pairs are never touched here: their numbers come
from ``scripts/verify_temporal.py`` after the parameters are chosen. The frozen file's sha256 is verified first.
"""

import argparse
import glob
import itertools
import json
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import label_risk_items as lri  # noqa: E402

from semigraph.eval import gold  # noqa: E402
from semigraph.graph import alignment, passages  # noqa: E402
from semigraph.graph.item_pairs import consecutive_pairs, risk_section_text  # noqa: E402

GRID = {"reword_min": (0.35, 0.45, 0.6, 0.99), "partial_min": (0.0, 60.0, 62.0, 65.0, 70.0, 75.0),
        "present_min_ratio": (75.0, 80.0), "decompose_uncertain": (True,), "suppress_added_with_counterpart": (True,)}


def _spans(chunks: pd.DataFrame, accession: str, section: str) -> list[tuple[str, int, int]]:
    c = chunks[(chunks.accession_no == accession) & (chunks.section_id == section)]
    return [(r.chunk_id, int(r.char_start), int(r.char_end)) for r in c.itertuples()]


def load_pairs(split: str) -> list[dict]:
    items = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob("data/interim/risk_items/*_risk_items.parquet"))], ignore_index=True)
    sections = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob("data/interim/section_texts/*_section_texts.parquet"))], ignore_index=True)
    chunks = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob("data/processed/chunks/*_chunks.parquet"))], ignore_index=True)
    out = []
    for pair in consecutive_pairs(items):
        if lri.split_of(pair["pair_id"]) != split:
            continue
        oa, na = pair["older_accession"], pair["newer_accession"]
        older = items[items.accession_no == oa].sort_values("seq").to_dict("records")
        newer = items[items.accession_no == na].sort_values("seq").to_dict("records")
        o_text, n_text = risk_section_text(items, sections, oa), risk_section_text(items, sections, na)
        out.append({"pair_id": pair["pair_id"], "older": older, "newer": newer, "o_text": o_text, "n_text": n_text,
                    "o_spans": _spans(chunks, oa, older[0]["section_id"]), "n_spans": _spans(chunks, na, newer[0]["section_id"]),
                    "result": alignment.align(older, newer, n_text, older_section_text=o_text)})
    return out


def evaluate(pairs: list[dict], sentence_gold: dict, params: passages.PassageParams) -> dict:
    tp = fp = fn = 0
    confusion = {"reworded_as_positive": 0, "present_as_reworded": 0}
    for p in pairs:
        ps = passages.compute_passages(p["older"], p["newer"], p["result"], p["o_text"], p["n_text"], p["o_spans"], p["n_spans"], params)
        for side in ("older", "newer"):
            entry = sentence_gold.get(f"{p['pair_id']}|{side}")
            if not entry:
                continue
            m = gold.score_passages(ps, gold.gold_sentence_records(entry), side=side).to_dict()
            tp, fp, fn = tp + m["sentence"]["tp"], fp + m["sentence"]["fp"], fn + m["sentence"]["fn"]
            positive = "removed" if side == "older" else "added"
            confusion["reworded_as_positive"] += m["confusion"].get("reworded", {}).get(positive, 0)
            confusion["present_as_reworded"] += m["confusion"].get("present", {}).get("reworded", 0)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None, **confusion}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", type=Path, default=Path("artifacts/gold/risk_items_gold.json"))
    ap.add_argument("--split", choices=("development",), default="development")
    args = ap.parse_args(argv)
    if not gold.verify_frozen(args.gold):
        print(f"error: {args.gold} does not match its sha256", file=sys.stderr)
        return 2
    sentence_gold = json.loads(args.gold.read_text(encoding="utf-8"))["sentences"]
    pairs = load_pairs(args.split)
    base = passages.PassageParams()
    print(f"{'reword_min':>10} {'partial':>8} {'present':>8} | {'tp':>4} {'fp':>4} {'fn':>4} {'prec':>6} {'rec':>6} | rewordedAsPos presentAsRew")
    keys = list(GRID)
    for values in itertools.product(*GRID.values()):
        params = replace(base, **dict(zip(keys, values)))
        m = evaluate(pairs, sentence_gold, params)
        prec = "  n/a" if m["precision"] is None else f"{m['precision']:.3f}"
        rec = "  n/a" if m["recall"] is None else f"{m['recall']:.3f}"
        print(f"{values[0]:>10} {values[1]:>8} {values[2]:>8} | {m['tp']:>4} {m['fp']:>4} {m['fn']:>4} {prec:>6} {rec:>6} | "
              f"{m['reworded_as_positive']:>13} {m['present_as_reworded']:>12}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
