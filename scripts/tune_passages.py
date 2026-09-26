"""Parameter sweep of the passage layer against the FROZEN sentence gold, on the DEVELOPMENT pairs only.

    python scripts/tune_passages.py [--gold artifacts/gold/risk_items_gold.json]
    python scripts/tune_passages.py --verdicts data/interim/risk_alignment/passage_adjudications.jsonl --band 0.5,0.6,0.7

Prints, per parameter setting, the micro-averaged sentence-level precision and recall of the positive class (removed on the
older side, added on the newer side) over the six development pairs. Held-out pairs are never touched here: their numbers come
from ``scripts/verify_temporal.py`` after the parameters are chosen. The frozen file's sha256 is verified first.

``--verdicts <jsonl>``: score with the recorded band answers (``semigraph align-items --adjudicate-passages`` writes them; nothing
is bought here, no model is called). Each setting lists its band sentences, looks their answers up (key: sentence hash | hash of
the other section | prompt version | ``--model``, default the configured ``ADJUDICATION_MODEL``), applies the code rules of
``graph/passage_adjudicate`` and passes the resulting verdicts to the passage layer; a band sentence without an answer stays
``reworded`` (the safe default), so a setting whose band contains sentences that were never asked is scored conservatively.
``--band``: comma-separated ``reword_confident`` values to sweep (``none`` = no band); the default ``0.6`` is the shipped value.
Without ``--verdicts`` the band changes nothing but the ``decided_by`` label, so one value is enough. ``--fast`` sweeps ``--band``
alone (the shipped values of the other parameters). Rows are only comparable when ``band == answered`` (columns at the right): a band
sentence nobody was asked about stays ``reworded`` and scores conservatively (a band wider than the one the answers were bought for,
``partial_min`` > 0 and ``present_min_ratio`` 80 all enlarge the set of band sentences).
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
from semigraph.graph import adjudicate as adj  # noqa: E402
from semigraph.graph import alignment, passages  # noqa: E402
from semigraph.graph import passage_adjudicate as pad  # noqa: E402
from semigraph.graph.item_pairs import consecutive_pairs, risk_section_text  # noqa: E402

GRID = {"reword_min": (0.35, 0.45, 0.6, 0.99), "partial_min": (0.0, 60.0, 62.0, 65.0, 70.0, 75.0),
        "present_min_ratio": (75.0, 80.0), "decompose_uncertain": (True,), "suppress_added_with_counterpart": (True,)}


def build_grid(band: tuple[float | None, ...], fast: bool = False) -> dict[str, tuple]:
    """The swept parameters: ``reword_confident`` (``--band``) first, then ``GRID``; ``fast`` keeps only the shipped value of every
    other parameter (the band alone is swept: a handful of settings instead of dozens, each ~10 s on the six development pairs)."""
    base = passages.PassageParams()
    rest = {"reword_min": (base.reword_min,), "partial_min": (base.partial_min,), "present_min_ratio": (base.present_min_ratio,),
            "decompose_uncertain": (True,), "suppress_added_with_counterpart": (True,)} if fast else GRID
    return {"reword_confident": band, **rest}


def parse_band(text: str) -> tuple[float | None, ...]:
    """``"none,0.5,0.6"`` -> ``(None, 0.5, 0.6)`` (the values of ``PassageParams.reword_confident``)."""
    values = []
    for part in (t.strip().lower() for t in text.split(",") if t.strip()):
        values.append(None if part == "none" else float(part))
    if not values:
        raise argparse.ArgumentTypeError("--band needs at least one value")
    return tuple(values)


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


def pair_passages(p: dict, params: passages.PassageParams, records: dict | None, model: str | None) -> tuple[tuple, dict]:
    """The passages of one development pair under ``params`` and the band bookkeeping ``{band, answered, applied}``."""
    engine = passages.PairPassages(p["older"], p["newer"], p["result"], p["o_text"], p["n_text"], p["o_spans"], p["n_spans"], params)
    if not records:
        return engine.passages(), {"band": 0, "answered": 0, "applied": 0}
    bands = engine.band_sentences()
    resolved = pad.resolve_verdicts(bands, records, model, older_text=p["o_text"], newer_text=p["n_text"])
    return engine.passages(resolved.verdicts), {"band": len(bands), "answered": len(bands) - len(resolved.unanswered),
                                                "applied": len(resolved.verdicts)}


def evaluate(pairs: list[dict], sentence_gold: dict, params: passages.PassageParams, records: dict | None = None,
             model: str | None = None) -> dict:
    tp = fp = fn = 0
    confusion = {"reworded_as_positive": 0, "present_as_reworded": 0}
    band = {"band": 0, "answered": 0, "applied": 0}
    for p in pairs:
        ps, counts = pair_passages(p, params, records, model)
        band = {k: band[k] + counts[k] for k in band}
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
            "recall": tp / (tp + fn) if tp + fn else None, **confusion, **band}


def load_records(path: Path, model: str) -> dict:
    """The recorded band answers of ``path``; refuses a missing file and warns when none belongs to ``model``."""
    if not path.exists():
        raise SystemExit(f"error: {path} not found: run `semigraph align-items --adjudicate-passages` first")
    records = adj.Checkpoint(path).records
    models = {r.get("model") for r in records.values()}
    if model not in models:
        print(f"warning: no recorded answer is for model {model!r} (the file holds {sorted(map(str, models))}): pass --model",
              file=sys.stderr)
    return records


def _fmt(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:.3f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", type=Path, default=Path("artifacts/gold/risk_items_gold.json"))
    ap.add_argument("--split", choices=("development",), default="development")
    ap.add_argument("--verdicts", type=Path, default=None, help="passage_adjudications.jsonl: score with the recorded band answers")
    ap.add_argument("--model", default=None, help="model whose recorded answers are used (default: the configured ADJUDICATION_MODEL)")
    ap.add_argument("--band", type=parse_band, default=(0.6,), help="comma-separated reword_confident values, 'none' = no band (default 0.6)")
    ap.add_argument("--fast", action="store_true", help="sweep only --band; keep the shipped value of every other parameter")
    args = ap.parse_args(argv)
    if not gold.verify_frozen(args.gold):
        print(f"error: {args.gold} does not match its sha256", file=sys.stderr)
        return 2
    sentence_gold = json.loads(args.gold.read_text(encoding="utf-8"))["sentences"]
    model = args.model
    if args.verdicts is not None and model is None:
        from semigraph.config import get_settings

        model = get_settings().adjudication_model
    records = load_records(args.verdicts, model) if args.verdicts is not None else None
    pairs = load_pairs(args.split)
    base = passages.PassageParams()
    grid = build_grid(args.band, args.fast)
    print(f"{'confident':>9} {'reword_min':>10} {'partial':>8} {'present':>8} | {'tp':>4} {'fp':>4} {'fn':>4} {'prec':>6} {'rec':>6} | "
          f"rewordedAsPos presentAsRew | band answered applied")
    keys = list(grid)
    for values in itertools.product(*grid.values()):
        params = replace(base, **dict(zip(keys, values)))
        m = evaluate(pairs, sentence_gold, params, records, model)
        confident = "none" if values[0] is None else f"{values[0]}"
        print(f"{confident:>9} {values[1]:>10} {values[2]:>8} {values[3]:>8} | {m['tp']:>4} {m['fp']:>4} {m['fn']:>4} "
              f"{_fmt(m['precision']):>6} {_fmt(m['recall']):>6} | {m['reworded_as_positive']:>13} {m['present_as_reworded']:>12} | "
              f"{m['band']:>4} {m['answered']:>8} {m['applied']:>7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
