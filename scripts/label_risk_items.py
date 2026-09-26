"""Build blind-labeller packets for the risk-item gold set, then collect, validate and aggregate their labels.

    python scripts/label_risk_items.py packets --pair-id NVDA-0001045810-25-000023-0001045810-26-000021
    python scripts/label_risk_items.py packets --dev                # the six fully labelled development pairs
    python scripts/label_risk_items.py collect --pair-id <id> --side older
    python scripts/label_risk_items.py freeze --out artifacts/gold/risk_items_gold.json

A packet holds ONLY the items of one filing, the FULL section text of the neighbouring filing, and the labelling rules
(eval/gold.py): the labeller never sees algorithm output. Labels come back as ``<pair>.<side>.<annotator>.labels.json``
and are machine-checked (a quote must literally occur in the source text; a "removed" label is contradicted when the
item's headline is still there). Inputs are the M1b artifacts: data/interim/risk_items/*.parquet and
data/interim/section_texts/*.parquet.
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from semigraph.eval import gold

DEFAULT_ITEMS = Path("data/interim/risk_items")
DEFAULT_SECTIONS = Path("data/interim/section_texts")
DEFAULT_OUT = Path("data/interim/gold")
# The fully labelled DEVELOPMENT pairs (headline-rich, headline-poor, 20-F): used to design and tune the algorithm.
# Every other consecutive pair is HELD OUT and only touched for the final numbers.
DEV_PAIRS = frozenset({
    "NVDA-0001045810-25-000023-0001045810-26-000021", "NVDA-0001045810-24-000029-0001045810-25-000023",
    "AMD-0000002488-25-000012-0000002488-26-000018", "META-0001326801-25-000017-0001628280-26-003942",
    "MU-0000723125-24-000027-0000723125-25-000028", "TSM-0001193125-25-083423-0001628280-26-025362"})


def split_of(pair_id: str) -> str:
    return "development" if pair_id in DEV_PAIRS else "held_out"


def consecutive_pairs(items: pd.DataFrame) -> list[dict]:
    """Neighbouring annual filings per ticker in filing-date order (only filings that have risk items)."""
    pairs = []
    filings = items[["ticker", "accession_no", "filing_date"]].drop_duplicates()
    for ticker, group in filings.groupby("ticker"):
        ordered = group.sort_values("filing_date")
        rows = list(ordered.itertuples(index=False))
        for older, newer in zip(rows, rows[1:]):
            pairs.append({"pair_id": f"{ticker}-{older.accession_no}-{newer.accession_no}", "ticker": ticker,
                          "older_accession": older.accession_no, "newer_accession": newer.accession_no,
                          "older_date": str(older.filing_date)[:10], "newer_date": str(newer.filing_date)[:10]})
    return sorted(pairs, key=lambda p: p["pair_id"])


def _section_text(sections: pd.DataFrame, accession: str) -> str:
    rows = sections[sections["accession_no"] == accession]
    if rows.empty:
        raise KeyError(f"no section text for {accession}")
    return str(rows.iloc[0]["text"])


def _items_of(items: pd.DataFrame, accession: str) -> list[dict]:
    rows = items[items["accession_no"] == accession].sort_values("item_id")
    return [{"item_id": r.item_id, "headline": r.headline or "", "text": r.text} for r in rows.itertuples(index=False)]


def write_packets(pairs: list[dict], items: pd.DataFrame, sections: pd.DataFrame, out_dir: Path) -> list[Path]:
    """One packet per pair and side: older items vs the newer section, and newer items vs the older section."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for pair in pairs:
        older_text, newer_text = _section_text(sections, pair["older_accession"]), _section_text(sections, pair["newer_accession"])
        older_items, newer_items = _items_of(items, pair["older_accession"]), _items_of(items, pair["newer_accession"])
        meta = {"pair_id": pair["pair_id"], "ticker": pair["ticker"], "split": split_of(pair["pair_id"])}
        older = gold.build_packet({**meta, "side": "older"}, older_items, newer_text)
        newer = {"pair": {**meta, "side": "newer"}, "side": "newer", "items": newer_items, "other_section_text": older_text,
                 "instructions": gold.LABELLING_INSTRUCTIONS.replace("older", "newer-side").replace("OLDER", "NEWER")
                 + "\nFor this packet the labels are `carried` (the risk was already disclosed in the older filing; give a verbatim "
                   "`quote` from the older text) and `new` (it was not; give >= 3 `search_terms` you searched for in the WHOLE older text)."}
        for side, packet in (("older", older), ("newer", newer)):
            path = out_dir / f"{pair['pair_id']}.{side}.packet.json"
            path.write_text(json.dumps(packet, ensure_ascii=False, indent=1), encoding="utf-8")
            written.append(path)
    return written


def collect(pair_id: str, side: str, out_dir: Path) -> dict:
    """Validate every annotator's labels for one pair side, aggregate, and write ``<pair>.<side>.report.json``."""
    packet = json.loads((out_dir / f"{pair_id}.{side}.packet.json").read_text(encoding="utf-8"))
    items = packet["older_items"] if side == "older" else packet["items"]
    other = packet["newer_section_text"] if side == "older" else packet["other_section_text"]
    accepted, rejected, annotators = {}, {}, []
    for path in sorted(out_dir.glob(f"{pair_id}.{side}.*.labels.json")):
        name = path.name[len(pair_id) + len(side) + 2: -len(".labels.json")]
        validated = gold.validate_annotation(json.loads(path.read_text(encoding="utf-8")), items, other, side=side)
        annotators.append(name)
        accepted[name] = validated.accepted
        if validated.rejected:
            rejected[name] = [list(r) for r in validated.rejected]
    agg = gold.aggregate(accepted) if accepted else gold.Aggregate({}, [], 1.0, 1.0)
    labelled = set(agg.consensus) | set(agg.needs_adjudication)
    report = {"pair_id": pair_id, "side": side, "split": split_of(pair_id), "annotators": annotators,
              "consensus": agg.consensus, "needs_adjudication": agg.needs_adjudication,
              "unlabelled": [i["item_id"] for i in items if i["item_id"] not in labelled],
              "pairwise_agreement": agg.pairwise_agreement, "alpha": agg.alpha, "rejected": rejected,
              "votes": {i["item_id"]: {a: accepted[a][i["item_id"]] for a in accepted if i["item_id"] in accepted[a]} for i in items}}
    (out_dir / f"{pair_id}.{side}.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def freeze_gold(out_dir: Path, target: Path, adjudicated: dict[str, str] | None = None) -> str:
    """Merge every report's consensus (plus adjudicated labels) into one frozen gold file and return its sha256."""
    labels: dict[str, dict] = {}
    for path in sorted(out_dir.glob("*.report.json")):
        rep = json.loads(path.read_text(encoding="utf-8"))
        merged = {**rep["consensus"], **{k: v for k, v in (adjudicated or {}).items() if k in rep["needs_adjudication"]}}
        labels[f"{rep['pair_id']}|{rep['side']}"] = {"split": rep["split"], "labels": merged,
                                                    "alpha": rep["alpha"], "pairwise_agreement": rep["pairwise_agreement"]}
    target.parent.mkdir(parents=True, exist_ok=True)
    return gold.freeze({"kind": "risk_items_gold", "pairs": labels}, target)


def _load(items_dir: Path, sections_dir: Path, tickers: list[str] | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    files = sorted(items_dir.glob("*_risk_items.parquet"))
    if tickers:
        files = [f for f in files if f.name.split("_")[0].upper() in {t.upper() for t in tickers}]
    items = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    sections = pd.concat([pd.read_parquet(f) for f in sorted(sections_dir.glob("*_section_texts.parquet"))], ignore_index=True)
    return items, sections


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("packets")
    p.add_argument("--pair-id", action="append")
    p.add_argument("--dev", action="store_true", help="only the six development pairs")
    p.add_argument("--ticker", "-t", action="append")
    c = sub.add_parser("collect")
    c.add_argument("--pair-id", required=True)
    c.add_argument("--side", choices=("older", "newer"), required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--out-file", type=Path, default=Path("artifacts/gold/risk_items_gold.json"))
    for sp in (p, c, f):
        sp.add_argument("--items-dir", type=Path, default=DEFAULT_ITEMS)
        sp.add_argument("--sections-dir", type=Path, default=DEFAULT_SECTIONS)
        sp.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    if args.cmd == "packets":
        items, sections = _load(args.items_dir, args.sections_dir, args.ticker)
        pairs = consecutive_pairs(items)
        if args.dev:
            pairs = [q for q in pairs if q["pair_id"] in DEV_PAIRS]
        if args.pair_id:
            pairs = [q for q in pairs if q["pair_id"] in set(args.pair_id)]
        for path in write_packets(pairs, items, sections, args.out):
            print(path)
    elif args.cmd == "collect":
        rep = collect(args.pair_id, args.side, args.out)
        print(json.dumps({k: rep[k] for k in ("pair_id", "side", "annotators", "pairwise_agreement", "alpha", "needs_adjudication", "unlabelled")}, indent=1))
        for name, rej in rep["rejected"].items():
            print(f"{name}: {len(rej)} label(s) rejected by machine, e.g. {rej[0]}")
    else:
        print(freeze_gold(args.out, args.out_file))
    return 0


if __name__ == "__main__":
    sys.exit(main())
