"""Choose which items of the HELD-OUT pairs the blind labellers label (plan A: "label every item the algorithm marks
removed/new or places in the uncertainty band, plus 5 random unchanged/reworded per pair").

    python scripts/select_heldout_items.py [--out data/interim/gold/heldout_selection.json] [--seed 20260926]

The selection uses the aligner's lexical pass at its STARTING parameters, before any tuning, so it is a superset of what a
tuned algorithm would flag; the random extras estimate what the flags miss. Output feeds
``label_risk_items.py packets --select-file``. Only comparable held-out pairs are considered (a pair with a
low-coverage or suspect side is "not compared").
"""

import argparse
import glob
import json
import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import label_risk_items as lri  # noqa: E402

from semigraph.graph import alignment  # noqa: E402
from semigraph.graph.item_pairs import consecutive_pairs, risk_section_text  # noqa: E402
from semigraph.parsing import risk_item_quality  # noqa: E402

SEED = 20260926
N_RANDOM = 5
OLDER_FLAGGED = ("removed", "uncertain", "merged")
NEWER_FLAGGED = ("new", "uncertain")


def choose(result, *, pair_id: str, seed: int = SEED, n_random: int = N_RANDOM) -> dict:
    """Items to label for one aligned pair: every flagged item plus ``n_random`` seeded others per side."""
    out: dict = {"older": [], "newer": [], "reasons": {}}
    for side, decisions, flagged in (("older", result.older, OLDER_FLAGGED), ("newer", result.newer, NEWER_FLAGGED)):
        rest = []
        for d in decisions:
            if d.label in flagged:
                out[side].append(d.item_id)
                out["reasons"][d.item_id] = d.label
            else:
                rest.append(d.item_id)
        rng = random.Random(f"{seed}|{pair_id}|{side}")
        for item_id in sorted(rng.sample(sorted(rest), min(n_random, len(rest)))):
            out[side].append(item_id)
            out["reasons"][item_id] = "random"
        out[side].sort()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("data/interim/gold/heldout_selection.json"))
    ap.add_argument("--items-dir", type=Path, default=Path("data/interim/risk_items"))
    ap.add_argument("--sections-dir", type=Path, default=Path("data/interim/section_texts"))
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args(argv)
    items = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(str(args.items_dir / "*_risk_items.parquet")))], ignore_index=True)
    sections = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(str(args.sections_dir / "*_section_texts.parquet")))], ignore_index=True)
    pairs = [p for p in consecutive_pairs(items, risk_item_quality.load_quality(args.items_dir))
             if p["comparable"] and lri.split_of(p["pair_id"]) == "held_out"]
    selection = {}
    for pair in pairs:
        older = items[items.accession_no == pair["older_accession"]].sort_values("seq").to_dict("records")
        newer = items[items.accession_no == pair["newer_accession"]].sort_values("seq").to_dict("records")
        result = alignment.align(older, newer, risk_section_text(items, sections, pair["newer_accession"]),
                                 older_section_text=risk_section_text(items, sections, pair["older_accession"]))
        selection[pair["pair_id"]] = choose(result, pair_id=pair["pair_id"], seed=args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(selection, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
    n = sum(len(v["older"]) + len(v["newer"]) for v in selection.values())
    print(f"{len(selection)} held-out pairs, {n} items to label -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
