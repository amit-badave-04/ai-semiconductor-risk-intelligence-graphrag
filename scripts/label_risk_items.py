"""Build blind-labeller packets for the risk-item gold set, then collect, validate and aggregate their labels.

    python scripts/label_risk_items.py packets --pair-id NVDA-0001045810-25-000023-0001045810-26-000021
    python scripts/label_risk_items.py packets --dev                # the six fully labelled development pairs
    python scripts/label_risk_items.py collect --pair-id <id> --side older
    python scripts/label_risk_items.py freeze --out-file artifacts/gold/risk_items_gold.json

and, once the item labels are collected, the SENTENCE layer (a risk item can survive while single sentences of it were
removed, which is what the passage change layer must catch):

    python scripts/label_risk_items.py sentence-packets --dev                  # or --pair-id <id>; needs the item reports
    python scripts/label_risk_items.py collect-sentences --pair-id <id> --side older
    python scripts/label_risk_items.py freeze                                  # merges both layers under one sha256

A packet holds ONLY the items of one filing, the FULL section text of the neighbouring filing, and the labelling rules
(eval/gold.py): the labeller never sees algorithm output. Labels come back as ``<pair>.<side>.<annotator>.labels.json``
(items) and ``<pair>.<side>.sent.<annotator>.labels.json`` (sentences) and are machine-checked (a quote must literally
occur in the source text; a "removed" label is contradicted when the item's headline / the sentence is still there).
Inputs are the M1b artifacts: data/interim/risk_items/*.parquet and data/interim/section_texts/*.parquet (a filing has
several sections: the risk section is the one the filing's own items carry as ``section_id``).
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from semigraph.eval import gold
from semigraph.parsing import risk_item_quality

DEFAULT_ITEMS = Path("data/interim/risk_items")
DEFAULT_SECTIONS = Path("data/interim/section_texts")
DEFAULT_OUT = Path("data/interim/gold")
SENT = "sent"       # file-name marker of the sentence layer: <pair>.<side>.sent.packet.json / .sample.json / .<annotator>.labels.json
# The fully labelled DEVELOPMENT pairs (headline-rich, headline-poor, 20-F): used to design and tune the algorithm.
# Every other consecutive pair is HELD OUT and only touched for the final numbers.
DEV_PAIRS = frozenset({
    "NVDA-0001045810-25-000023-0001045810-26-000021", "NVDA-0001045810-24-000029-0001045810-25-000023",
    "AMD-0000002488-25-000012-0000002488-26-000018", "META-0001326801-25-000017-0001628280-26-003942",
    "MU-0000723125-24-000027-0000723125-25-000028", "TSM-0001193125-25-083423-0001628280-26-025362"})


def split_of(pair_id: str) -> str:
    return "development" if pair_id in DEV_PAIRS else "held_out"


def consecutive_pairs(items: pd.DataFrame, quality: dict | None = None) -> list[dict]:
    """Neighbouring annual filings per ticker in filing-date order (only filings that have risk items).

    With ``quality`` (parsing.risk_item_quality.load_quality) every pair carries ``comparable`` and, when False, the
    ``not_compared_reason``: a pair with a low-coverage or suspect side is never labelled or aligned."""
    pairs = []
    filings = items[["ticker", "accession_no", "filing_date"]].drop_duplicates()
    for ticker, group in filings.groupby("ticker"):
        ordered = group.sort_values("filing_date")
        rows = list(ordered.itertuples(index=False))
        for older, newer in zip(rows, rows[1:]):
            ok, reason = (risk_item_quality.comparability(older.accession_no, newer.accession_no, quality)
                          if quality is not None else (True, None))
            pairs.append({"pair_id": f"{ticker}-{older.accession_no}-{newer.accession_no}", "ticker": ticker,
                          "older_accession": older.accession_no, "newer_accession": newer.accession_no,
                          "older_date": str(older.filing_date)[:10], "newer_date": str(newer.filing_date)[:10],
                          "comparable": ok, "not_compared_reason": reason})
    return sorted(pairs, key=lambda p: p["pair_id"])


def _risk_section_text(items: pd.DataFrame, sections: pd.DataFrame, accession: str) -> str:
    """The risk-section text of one filing. The section-text lake holds several sections per accession (Item 1, Item 1A,
    Item 7, ...), so the section is the one the filing's own items say they were cut from, never simply "the first"."""
    ids = items.loc[items["accession_no"] == accession, "section_id"].unique()
    if len(ids) != 1:
        raise KeyError(f"cannot tell which section holds the risk items of {accession}: item section ids {list(ids)}")
    rows = sections[(sections["accession_no"] == accession) & (sections["section_id"] == ids[0])]
    if rows.empty:
        raise KeyError(f"no section text for {accession} (section {ids[0]})")
    return str(rows.iloc[0]["text"])


def _items_of(items: pd.DataFrame, accession: str, *, with_offsets: bool = False) -> list[dict]:
    rows = items[items["accession_no"] == accession].sort_values("item_id")
    out = [{"item_id": r.item_id, "headline": r.headline or "", "text": r.text} for r in rows.itertuples(index=False)]
    if with_offsets:
        if not {"char_start", "char_end"} <= set(items.columns):
            raise ValueError("the risk-item parquet has no char_start/char_end columns: rebuild it with parsing/risk_items.py")
        for entry, r in zip(out, rows.itertuples(index=False)):
            entry["char_start"], entry["char_end"] = int(r.char_start), int(r.char_end)
    return out


def write_packets(pairs: list[dict], items: pd.DataFrame, sections: pd.DataFrame, out_dir: Path) -> list[Path]:
    """One packet per pair and side: older items vs the newer section, and newer items vs the older section."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for pair in (p for p in pairs if p.get("comparable", True)):
        older_text = _risk_section_text(items, sections, pair["older_accession"])
        newer_text = _risk_section_text(items, sections, pair["newer_accession"])
        older_items, newer_items = _items_of(items, pair["older_accession"]), _items_of(items, pair["newer_accession"])
        meta = {"pair_id": pair["pair_id"], "ticker": pair["ticker"], "split": split_of(pair["pair_id"])}
        older = gold.build_packet({**meta, "side": "older"}, older_items, newer_text)
        newer = {"pair": {**meta, "side": "newer"}, "side": "newer", "items": newer_items, "other_section_text": older_text,
                 "instructions": gold.NEWER_LABELLING_INSTRUCTIONS}
        for side, packet in (("older", older), ("newer", newer)):
            path = out_dir / f"{pair['pair_id']}.{side}.packet.json"
            path.write_text(json.dumps(packet, ensure_ascii=False, indent=1), encoding="utf-8")
            written.append(path)
    return written


def _read_labels(path: Path) -> list:
    try:
        labels = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"{path.name} is not valid JSON: {err}") from err
    if not isinstance(labels, list):
        raise ValueError(f"{path.name} must hold a JSON list of label objects, not {type(labels).__name__}")
    return labels


def collect(pair_id: str, side: str, out_dir: Path) -> dict:
    """Validate every annotator's labels for one pair side, aggregate, and write ``<pair>.<side>.report.json``."""
    packet = json.loads((out_dir / f"{pair_id}.{side}.packet.json").read_text(encoding="utf-8"))
    items = packet["older_items"] if side == "older" else packet["items"]
    other = packet["newer_section_text"] if side == "older" else packet["other_section_text"]
    accepted, rejected, annotators = {}, {}, []
    for path in sorted(out_dir.glob(f"{pair_id}.{side}.*.labels.json")):
        name = path.name[len(pair_id) + len(side) + 2: -len(".labels.json")]
        if name == SENT or name.startswith(SENT + "."):
            continue                                        # a SENTENCE label file: collect_sentences reads those
        validated = gold.validate_annotation(_read_labels(path), items, other, side=side)
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


class MissingItemReports(FileNotFoundError):
    """Sentence packets are sampled from the item-level consensus, which ``collect`` writes."""


def _sample_paths(out_dir: Path, pair_id: str, side: str) -> dict[str, Path]:
    return {kind: out_dir / f"{pair_id}.{side}.{SENT}.{kind}.json" for kind in ("packet", "sample", "report")}


def _check_offsets(items: list[dict], section_text: str) -> None:
    """Section offsets are the contract between the gold and the passage layer: fail loudly if they drift."""
    for item in items:
        if section_text[item["char_start"]:item["char_start"] + len(item["text"])] != item["text"]:
            raise ValueError(f"item {item['item_id']}: char_start/char_end offsets do not match the section text")


def write_sentence_packets(pairs: list[dict], items: pd.DataFrame, sections: pd.DataFrame, out_dir: Path, *,
                           sides: tuple[str, ...] = ("older", "newer"), seed: int = gold.SENTENCE_SAMPLE_SEED,
                           k: int = gold.SENTENCE_SAMPLE_K, max_sentences: int = gold.SENTENCE_SAMPLE_CAP) -> list[Path]:
    """Per comparable pair and side: a blind labeller packet (``<pair>.<side>.sent.packet.json``: sentences and the full
    other-filing section, no labels) and the sampling manifest (``<pair>.<side>.sent.sample.json``: seed, flagship,
    dropped items, offsets; for ``collect-sentences`` and the frozen gold, never given to a labeller).

    The item-level gold decides only WHICH items are eligible (consensus, or a strict majority of the votes when the
    labellers split among present labels); it is not copied into the packet."""
    todo = [p for p in pairs if p.get("comparable", True)]
    missing = [out_dir / f"{p['pair_id']}.{s}.report.json" for p in todo for s in sides
               if not (out_dir / f"{p['pair_id']}.{s}.report.json").exists()]
    if missing:
        raise MissingItemReports(
            "sentence packets are sampled from the item-level gold; these item reports are missing (write them with "
            "`label_risk_items.py collect --pair-id <id> --side <older|newer>` after the item labels are in): "
            + ", ".join(str(m) for m in missing))
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for pair in todo:
        texts = {s: _risk_section_text(items, sections, pair[f"{s}_accession"]) for s in ("older", "newer")}
        for side in sides:
            side_items = _items_of(items, pair[f"{side}_accession"], with_offsets=True)
            _check_offsets(side_items, texts[side])
            report = json.loads((out_dir / f"{pair['pair_id']}.{side}.report.json").read_text(encoding="utf-8"))
            sample = gold.sentence_sample(side_items, report["consensus"], pair_id=pair["pair_id"], side=side,
                                          item_votes=report.get("votes"), seed=seed, k=k, max_sentences=max_sentences)
            meta = {"pair_id": pair["pair_id"], "ticker": pair["ticker"], "split": split_of(pair["pair_id"]), "side": side}
            packet = gold.build_sentence_packet(meta, sample["items"], texts["newer" if side == "older" else "older"])
            paths = _sample_paths(out_dir, pair["pair_id"], side)
            paths["packet"].write_text(json.dumps(packet, ensure_ascii=False, indent=1), encoding="utf-8")
            paths["sample"].write_text(json.dumps({"pair": meta, **sample}, ensure_ascii=False, indent=1), encoding="utf-8")
            written += [paths["packet"], paths["sample"]]
    return written


def collect_sentences(pair_id: str, side: str, out_dir: Path) -> dict:
    """Validate every annotator's sentence labels for one pair side, aggregate, and write ``<pair>.<side>.sent.report.json``."""
    paths = _sample_paths(out_dir, pair_id, side)
    if not paths["sample"].exists() or not paths["packet"].exists():
        raise FileNotFoundError(f"{paths['sample'].name} / {paths['packet'].name} not found in {out_dir}: run "
                                f"`label_risk_items.py sentence-packets --pair-id {pair_id}` first")
    manifest = json.loads(paths["sample"].read_text(encoding="utf-8"))
    other = json.loads(paths["packet"].read_text(encoding="utf-8"))["other_section_text"]
    sentences = [s for item in manifest["items"] for s in item["sentences"]]
    prefix = f"{pair_id}.{side}.{SENT}."
    accepted, rejected, annotators = {}, {}, []
    for path in sorted(out_dir.glob(f"{prefix}*.labels.json")):
        name = path.name[len(prefix): -len(".labels.json")]
        validated = gold.validate_sentence_annotation(_read_labels(path), sentences, other, side=side)
        annotators.append(name)
        accepted[name] = validated.accepted
        if validated.rejected:
            rejected[name] = [list(r) for r in validated.rejected]
    agg = gold.aggregate(accepted) if accepted else gold.Aggregate({}, [], 1.0, 1.0)
    labelled = set(agg.consensus) | set(agg.needs_adjudication)
    sampling = {k: v for k, v in manifest.items() if k not in ("items", "pair")}
    sampling["sampled_items"] = [i["item_id"] for i in manifest["items"]]
    report = {"pair_id": pair_id, "side": side, "kind": "sentences", "split": split_of(pair_id), "annotators": annotators,
              "consensus": agg.consensus, "needs_adjudication": agg.needs_adjudication,
              "unlabelled": [s["sentence_id"] for s in sentences if s["sentence_id"] not in labelled],
              "pairwise_agreement": agg.pairwise_agreement, "alpha": agg.alpha, "rejected": rejected,
              "votes": {s["sentence_id"]: {a: accepted[a][s["sentence_id"]] for a in accepted if s["sentence_id"] in accepted[a]}
                        for s in sentences},
              "spans": gold.sentence_spans(manifest), "sampling": sampling}
    paths["report"].write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def _reports(out_dir: Path, *, sentences: bool):
    """Item reports (``<pair>.<side>.report.json``) or sentence reports (``<pair>.<side>.sent.report.json``)."""
    for path in sorted(out_dir.glob("*.report.json")):
        if path.name.endswith(f".{SENT}.report.json") == sentences:
            yield json.loads(path.read_text(encoding="utf-8"))


def _sentence_entry(rep: dict, adjudicated: dict[str, str]) -> dict:
    allowed = gold.SENTENCE_LABELS_OLDER if rep["side"] == "older" else gold.SENTENCE_LABELS_NEWER
    resolved = {k: v for k, v in adjudicated.items() if k in rep["needs_adjudication"]}
    bad = {k: v for k, v in resolved.items() if v not in allowed}
    if bad:
        raise ValueError(f"adjudicated sentence labels outside {list(allowed)} for the {rep['side']} side: {bad}")
    labels = dict(sorted({**rep["consensus"], **resolved}.items()))
    return {"split": rep["split"], "labels": labels, "spans": {sid: rep["spans"][sid] for sid in labels},
            "sampling": rep["sampling"], "n_sampled_sentences": len(rep["spans"]),
            "n_unresolved": len(rep["spans"]) - len(labels), "alpha": rep["alpha"],
            "pairwise_agreement": rep["pairwise_agreement"]}


def freeze_gold(out_dir: Path, target: Path, adjudicated: dict[str, str] | None = None) -> str:
    """Merge every report's consensus (plus adjudicated labels) into one frozen gold file and return its sha256.

    Two layers under the one hash: ``pairs`` (item labels) and ``sentences`` (sentence labels with the section spans that
    scoring needs; ``{}`` when no sentence report exists). ``adjudicated`` maps item ids and sentence ids (separate
    namespaces) to the adjudicator's label for the disputed ones."""
    adjudicated = adjudicated or {}
    labels: dict[str, dict] = {}
    for rep in _reports(out_dir, sentences=False):
        merged = {**rep["consensus"], **{k: v for k, v in adjudicated.items() if k in rep["needs_adjudication"]}}
        labels[f"{rep['pair_id']}|{rep['side']}"] = {"split": rep["split"], "labels": merged,
                                                    "alpha": rep["alpha"], "pairwise_agreement": rep["pairwise_agreement"]}
    sentences = {f"{rep['pair_id']}|{rep['side']}": _sentence_entry(rep, adjudicated) for rep in _reports(out_dir, sentences=True)}
    target.parent.mkdir(parents=True, exist_ok=True)
    return gold.freeze({"kind": "risk_items_gold", "pairs": labels, "sentences": sentences}, target)


def _load(items_dir: Path, sections_dir: Path, tickers: list[str] | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    files = sorted(items_dir.glob("*_risk_items.parquet"))
    if tickers:
        files = [f for f in files if f.name.split("_")[0].upper() in {t.upper() for t in tickers}]
    items = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    sections = pd.concat([pd.read_parquet(f) for f in sorted(sections_dir.glob("*_section_texts.parquet"))], ignore_index=True)
    return items, sections




def _fail(message: object) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _cmd_packets(args) -> int:
    items, sections = _load(args.items_dir, args.sections_dir, args.ticker)
    pairs = consecutive_pairs(items, risk_item_quality.load_quality(args.items_dir))
    for skipped in (q for q in pairs if not q["comparable"]):
        print(f"NOT COMPARED {skipped['pair_id']}: {skipped['not_compared_reason']}", file=sys.stderr)
    if args.dev:
        pairs = [q for q in pairs if q["pair_id"] in DEV_PAIRS]
    if args.pair_id:
        pairs = [q for q in pairs if q["pair_id"] in set(args.pair_id)]
    for path in write_packets(pairs, items, sections, args.out):
        print(path)
    return 0


def _cmd_sentence_packets(args, parser: argparse.ArgumentParser) -> int:
    if not args.pair_id and not args.dev:
        parser.error("sentence-packets needs --pair-id <id> (repeatable) or --dev")
    items, sections = _load(args.items_dir, args.sections_dir, args.ticker)
    pairs = consecutive_pairs(items, risk_item_quality.load_quality(args.items_dir))
    unknown = sorted(set(args.pair_id or []) - {q["pair_id"] for q in pairs})
    if unknown:
        return _fail(f"unknown pair id(s): {', '.join(unknown)}")
    wanted = set(args.pair_id or []) | (DEV_PAIRS if args.dev else set())
    selected = [q for q in pairs if q["pair_id"] in wanted]
    for skipped in (q for q in selected if not q["comparable"]):
        print(f"NOT COMPARED {skipped['pair_id']}: {skipped['not_compared_reason']}", file=sys.stderr)
    selected = [q for q in selected if q["comparable"]]
    if not selected:
        return _fail("no comparable pair selected")
    sides = ("older", "newer") if args.side == "both" else (args.side,)
    try:
        written = write_sentence_packets(selected, items, sections, args.out, sides=sides, seed=args.seed, k=args.k,
                                         max_sentences=args.max_sentences)
    except (MissingItemReports, ValueError, KeyError) as err:
        return _fail(err.args[0] if err.args else err)
    for path in written:
        print(path)
        if path.name.endswith(f".{SENT}.sample.json"):
            m = json.loads(path.read_text(encoding="utf-8"))
            print(f"  {m['pair_id']} {m['side']}: {len(m['items'])} items, {m['n_sentences']} sentences, flagship "
                  f"{m['flagship_item_id']}, dropped {len(m['dropped_items'])}", file=sys.stderr)
    return 0


def _print_collect_summary(rep: dict) -> None:
    print(json.dumps({k: rep[k] for k in ("pair_id", "side", "annotators", "pairwise_agreement", "alpha",
                                          "needs_adjudication", "unlabelled")}, indent=1))
    for name, rej in rep["rejected"].items():
        print(f"{name}: {len(rej)} label(s) rejected by machine, e.g. {rej[0]}")


def _cmd_collect_sentences(args) -> int:
    try:
        rep = collect_sentences(args.pair_id, args.side, args.out)
    except (FileNotFoundError, ValueError) as err:
        return _fail(err)
    _print_collect_summary(rep)
    return 0


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
    sp = sub.add_parser("sentence-packets", help="sentence-level packets from the item-level reports (run `collect` first)")
    sp.add_argument("--pair-id", action="append")
    sp.add_argument("--dev", action="store_true", help="the six development pairs")
    sp.add_argument("--ticker", "-t", action="append")
    sp.add_argument("--side", choices=("older", "newer", "both"), default="both")
    sp.add_argument("--seed", type=int, default=gold.SENTENCE_SAMPLE_SEED)
    sp.add_argument("--k", type=int, default=gold.SENTENCE_SAMPLE_K, help="items sampled per pair side, flagship included")
    sp.add_argument("--max-sentences", type=int, default=gold.SENTENCE_SAMPLE_CAP, help="cap on sentences per pair side")
    cs = sub.add_parser("collect-sentences")
    cs.add_argument("--pair-id", required=True)
    cs.add_argument("--side", choices=("older", "newer"), required=True)
    for parser in (p, c, f, sp, cs):
        parser.add_argument("--items-dir", type=Path, default=DEFAULT_ITEMS)
        parser.add_argument("--sections-dir", type=Path, default=DEFAULT_SECTIONS)
        parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    if args.cmd == "packets":
        return _cmd_packets(args)
    if args.cmd == "sentence-packets":
        return _cmd_sentence_packets(args, sp)
    if args.cmd == "collect-sentences":
        return _cmd_collect_sentences(args)
    if args.cmd == "collect":
        try:
            _print_collect_summary(collect(args.pair_id, args.side, args.out))
        except (FileNotFoundError, ValueError) as err:
            return _fail(err)
        return 0
    digest = freeze_gold(args.out, args.out_file)
    frozen = json.loads(args.out_file.read_text(encoding="utf-8"))
    print(f"frozen {len(frozen['pairs'])} item entries and {len(frozen['sentences'])} sentence entries", file=sys.stderr)
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
