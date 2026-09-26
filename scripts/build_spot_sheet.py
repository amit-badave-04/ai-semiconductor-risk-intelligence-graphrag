"""Build the owner spot-check sheet for the sentence gold (plan A: "owner human spot labels", checkpoint 1).

    python scripts/build_spot_sheet.py build --gold <candidate gold json> [--out-dir data/interim/gold]
    python scripts/build_spot_sheet.py score --labels <the human's labels json>

A frozen-candidate gold file (``label_risk_items.freeze_gold`` output) holds the annotators' consensus per sampled sentence.
The sheet asks a human to judge ~36 of those sentences independently. Selection is seeded and stratified, and the strata
use the ALGORITHM'S passages on purpose: every sentence the algorithm calls removed/added is where a wrong label matters:

- ``gold_removed``: the annotators' consensus is removed/added (control that the gold's positives are real)
- ``algo_fp``: the algorithm says removed/added, the annotators say present or reworded (the disputed cases)
- ``gold_present``: consensus present (random control)

Two files are written: ``spot_sheet.json`` (what the human sees: the sentence, its item headline, the closest sentences of
the other filing by plain text similarity, the wrapped full text of the other filing to search) and ``spot_key.json`` (row
id -> the gold label and the stratum; never shown to the human). ``score`` compares the human's labels with the key.
"""

import argparse
import json
import random
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from rapidfuzz import fuzz, process
from rapidfuzz.utils import default_process

SEED = 20260926
STRATA = (("gold_removed", 12), ("algo_fp", 12), ("gold_present", 12))
POSITIVE = {"older": "removed", "newer": "added"}
CANDIDATES = 3
COLLAPSE = {"present": "present", "reworded": "present", "removed": "absent", "added": "absent"}


def _rows(gold_entry: Mapping, side: str) -> list[dict]:
    """The frozen sentence entry as flat rows (sentence id, item id, gold label)."""
    return [{"sentence_id": sid, "item_id": span[0], "gold": gold_entry["labels"][sid], "side": side}
            for sid, span in gold_entry["spans"].items() if sid in gold_entry["labels"]]


def _predicted_positive(passages: Sequence, side: str, gold_entry: Mapping) -> set[str]:
    """Sentence ids of the entry that fall (>= half their characters) inside a predicted removed/added passage."""
    kind = POSITIVE[side]
    out: set[str] = set()
    for sid, (item_id, start, end) in gold_entry["spans"].items():
        for p in passages:
            view = p if isinstance(p, Mapping) else p.__dict__
            if view["kind"] != kind or view["item_id"] != item_id:
                continue
            overlap = min(end, view["char_end"]) - max(start, view["char_start"])
            if overlap * 2 >= (end - start):
                out.add(sid)
                break
    return out


def select(candidates: Mapping[str, list[dict]], *, seed: int = SEED) -> list[dict]:
    """Seeded stratified draw: ``candidates`` maps a stratum name to its eligible rows (each with ``sentence_id`` and
    ``pair_id``). Rows are drawn round-robin across pairs so no single pair dominates a stratum."""
    rng = random.Random(f"{seed}|spot")
    chosen: list[dict] = []
    for stratum, want in STRATA:
        pool = sorted(candidates.get(stratum, []), key=lambda r: r["sentence_id"])
        by_pair: dict[str, list[dict]] = {}
        for row in pool:
            by_pair.setdefault(row["pair_id"], []).append(row)
        for rows in by_pair.values():
            rng.shuffle(rows)
        order = sorted(by_pair)
        picked: list[dict] = []
        while len(picked) < want and any(by_pair[k] for k in order):
            for key in order:
                if by_pair[key] and len(picked) < want:
                    picked.append({**by_pair[key].pop(), "stratum": stratum})
        chosen.extend(picked)
    rng.shuffle(chosen)
    return chosen


def closest(sentence: str, other_sentences: Sequence[str], k: int = CANDIDATES) -> list[str]:
    hits = process.extract(sentence, other_sentences, scorer=fuzz.token_sort_ratio, processor=default_process, limit=k)
    return [text for text, _score, _idx in hits]


def score(labels: Mapping[str, str], key: Mapping[str, Mapping]) -> dict:
    """Agreement of the human's labels (row id -> present/reworded/absent/unsure) with the gold key."""
    rows = [(rid, lab, key[rid]) for rid, lab in labels.items() if rid in key and lab != "unsure"]
    exact = sum(1 for _, lab, k in rows if (lab if lab != "absent" else POSITIVE[k["side"]]) == k["gold"])
    collapsed = sum(1 for _, lab, k in rows if COLLAPSE.get(lab, lab) == COLLAPSE[k["gold"]])
    by_stratum: dict[str, list[int]] = {}
    for _, lab, k in rows:
        cell = by_stratum.setdefault(k["stratum"], [0, 0])
        cell[1] += 1
        cell[0] += COLLAPSE.get(lab, lab) == COLLAPSE[k["gold"]]
    n = len(rows)
    return {"n": n, "unsure": sum(1 for lab in labels.values() if lab == "unsure"),
            "agreement_exact": exact / n if n else None, "agreement_present_vs_absent": collapsed / n if n else None,
            "by_stratum": {s: {"agree": a, "of": t} for s, (a, t) in sorted(by_stratum.items())},
            "disagreements": [rid for rid, lab, k in rows if COLLAPSE.get(lab, lab) != COLLAPSE[k["gold"]]]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--gold", type=Path, required=True)
    b.add_argument("--out-dir", type=Path, default=Path("data/interim/gold"))
    b.add_argument("--gold-dir", type=Path, default=Path("data/interim/gold"))
    h = sub.add_parser("html", help="write one self-contained HTML page the human fills in (embeds the other filings' text)")
    h.add_argument("--sheet", type=Path, default=Path("data/interim/gold/spot_sheet.json"))
    h.add_argument("--out", type=Path, default=Path("data/interim/gold/spot_sheet.html"))
    s = sub.add_parser("score")
    s.add_argument("--labels", type=Path, required=True, help="JSON object: row id -> present|reworded|absent|unsure")
    s.add_argument("--key", type=Path, default=Path("data/interim/gold/spot_key.json"))
    args = ap.parse_args(argv)
    if args.cmd == "html":
        return _html(args)
    if args.cmd == "score":
        print(json.dumps(score(json.loads(args.labels.read_text(encoding="utf-8")),
                               json.loads(args.key.read_text(encoding="utf-8"))), indent=1))
        return 0
    return _build(args)


def render_html(sheet: Sequence[Mapping], texts: Mapping[str, str], template: str) -> str:
    """The page: the template with the sheet rows and the searchable filing texts embedded as JSON (``</`` escaped)."""
    def dump(obj) -> str:
        return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
    return template.replace("__ROWS__", dump(list(sheet))).replace("__TEXTS__", dump(dict(texts)))


def _html(args) -> int:  # pragma: no cover - reads the lake files
    sheet = json.loads(args.sheet.read_text(encoding="utf-8"))
    folder = args.sheet.parent
    texts = {r["other_file"]: (folder / r["other_file"]).read_text(encoding="utf-8") for r in sheet}
    template = (Path(__file__).resolve().parent / "spot_sheet_template.html").read_text(encoding="utf-8")
    args.out.write_text(render_html(sheet, texts, template), encoding="utf-8", newline="\n")
    print(f"{len(sheet)} rows, {len(texts)} filing texts -> {args.out} ({args.out.stat().st_size // 1024} KB)")
    return 0


def _build(args) -> int:  # pragma: no cover - needs the lake; the pure parts above are tested
    import glob

    import pandas as pd

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import label_risk_items as lri
    from semigraph.graph import alignment as align_mod
    from semigraph.graph import passages as passages_mod
    from semigraph.graph.align_text import split_sentences

    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    items = pd.concat([pd.read_parquet(f) for f in glob.glob("data/interim/risk_items/*_risk_items.parquet")], ignore_index=True)
    sections = pd.concat([pd.read_parquet(f) for f in glob.glob("data/interim/section_texts/*_section_texts.parquet")], ignore_index=True)
    chunks = pd.concat([pd.read_parquet(f) for f in glob.glob("data/processed/chunks/*_chunks.parquet")], ignore_index=True)
    pools: dict[str, list[dict]] = {name: [] for name, _ in STRATA}
    context: dict[str, dict] = {}
    for pair_id in sorted(lri.DEV_PAIRS):
        pair = next(p for p in lri.consecutive_pairs(items) if p["pair_id"] == pair_id)
        oa, na = pair["older_accession"], pair["newer_accession"]
        older = items[items.accession_no == oa].sort_values("seq").to_dict("records")
        newer = items[items.accession_no == na].sort_values("seq").to_dict("records")
        o_text, n_text = lri._risk_section_text(items, sections, oa), lri._risk_section_text(items, sections, na)
        spans = {acc: [(r.chunk_id, int(r.char_start), int(r.char_end)) for r in chunks[(chunks.accession_no == acc) & (chunks.section_id == rows[0]["section_id"])].itertuples()]
                 for acc, rows in ((oa, older), (na, newer))}
        result = align_mod.align(older, newer, n_text, older_section_text=o_text)
        passages = passages_mod.compute_passages(older, newer, result, o_text, n_text, spans[oa], spans[na])
        for side in ("older", "newer"):
            entry = gold["sentences"][f"{pair_id}|{side}"]
            predicted = _predicted_positive(passages, side, entry)
            other_text = n_text if side == "older" else o_text
            other_sentences = [other_text[a:b] for a, b in split_sentences(other_text)]
            packet = json.loads((args.gold_dir / f"{pair_id}.{side}.sent.packet.json").read_text(encoding="utf-8"))
            text_of = {s["sentence_id"]: (it["headline"], s["text"]) for it in packet["items"] for s in it["sentences"]}
            for row in _rows(entry, side):
                sid = row["sentence_id"]
                stratum = ("gold_removed" if row["gold"] == POSITIVE[side]
                           else "algo_fp" if sid in predicted else "gold_present" if row["gold"] == "present" else None)
                if stratum:
                    pools[stratum].append({**row, "pair_id": pair_id, "algo_positive": sid in predicted})
                    context[sid] = {"headline": text_of[sid][0], "text": text_of[sid][1], "other": other_sentences,
                                    "other_file": f"{pair_id}.{side}.sent.packet.other.txt"}
    chosen = select(pools)
    sheet, key = [], {}
    for n, row in enumerate(chosen, 1):
        c = context[row["sentence_id"]]
        rid = f"R{n:02d}"
        which = "older" if row["side"] == "older" else "newer"
        sheet.append({"row_id": rid, "ticker": row["pair_id"].split("-")[0], "statement": c["text"], "item_headline": c["headline"],
                      "direction": ("Is this sentence of the OLDER annual report still stated in the NEWER one?" if which == "older"
                                    else "Was this sentence of the NEWER annual report already stated in the OLDER one?"),
                      "closest": closest(c["text"], c["other"]), "other_file": c["other_file"]})
        key[rid] = {"sentence_id": row["sentence_id"], "gold": row["gold"], "side": row["side"], "stratum": row["stratum"],
                    "algo_positive": row["algo_positive"], "pair_id": row["pair_id"]}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "spot_sheet.json").write_text(json.dumps(sheet, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    (args.out_dir / "spot_key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    print(dict(Counter(r["stratum"] for r in chosen)), "->", args.out_dir / "spot_sheet.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
