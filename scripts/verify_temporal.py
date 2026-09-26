"""Verify the M1b temporal layer against the FROZEN source-text gold (docs/v2/M1B_PLAN.md sections E, H and L).

    PYTHONPATH=src python scripts/verify_temporal.py --gold artifacts/gold/risk_items_gold.json --alignment data/interim/risk_alignment

Reads the ``align-items`` output (per ticker ``<T>_pairs.parquet``, ``<T>_decisions.parquet``, ``<T>_passages.parquet``), the
risk items and section texts it was built from, and the gold. It first checks the sha256 of EVERY ``--gold`` file (``gold.verify_frozen``) and
refuses on a mismatch: a gold edited after the freeze proves nothing. ``--gold`` is repeatable (the development gold now, the held-out
gold when it is frozen): the files are merged and the ``split`` recorded in each pair-side and sentence entry decides which
split it is scored under; a pair side in two files is refused. No model is called and no database is read (graph invariants are
``scripts/verify_graph.py``'s job).

What it reports, PER SPLIT (development = used to design the algorithm, so informative only; held_out = the gates), each with
Wilson 95% intervals, the raw counts (tp / fp / fn) and the false-positive / false-negative ids:

* item level (``gold.score_predictions``): drop precision and recall on the older side (positive class ``removed``) and on the
  newer side (positive class ``new``: gold ``new`` and predicted ``new`` are mapped to the positive class, ``carried`` to
  present), plus the share of items the algorithm left ``uncertain``;
* passage level (``gold.score_passages`` on the sentence gold; ``gold.must_hit`` for the named flagship needles);
* the plan-H gates as PASS / FAIL / INSUFFICIENT-DATA:
    - held-out item drop precision >= 0.90 and recall >= 0.80, and the same two on passages (L.3);
    - NVDA FY25->FY26: the three named passages are reported removed, and the item level has no false removed item and finds the
      genuinely new item (L.1);
    - unit coverage >= 90% for every compared pair (a pair the alignment did not compare is listed as excluded, with its reason);
    - the false-drop guard: no ``removed`` older item whose headline fuzzy-matches (rapidfuzz ``ratio`` >= 90, ``default_process``)
      a sentence of the newer risk section, or is contained in it (a headline glued to a longer sentence).

A RATE gate never passes on fewer than 5 positives (its ``n`` is stated): with fewer it is INSUFFICIENT-DATA, or FAIL when the
result is statistically incompatible with the threshold (exact one-sided binomial tail < 0.05, e.g. two misses in a row against
0.80). The named-case checks (flagship needles, flagship item level, coverage, false-drop guard) are exact yes/no checks and pass
or fail on their own count.

Passage offsets are checked before any passage is scored: ``section_text[char_start:char_end]`` must equal the passage text for
at least 98% of the passages, else the script refuses (item-relative offsets would silently score recall as 0).

Output: ``artifacts/temporal_eval.json`` (sorted keys, no timestamps, byte-identical for identical inputs) and a compact table.
"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from math import comb, sqrt
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process
from rapidfuzz.utils import default_process

from semigraph.eval import gold
from semigraph.extraction.gates import normalize
from semigraph.graph.item_pairs import risk_section_text
from semigraph.parsing import risk_item_quality

DEFAULT_GOLD = Path("artifacts/gold/risk_items_gold.json")
DEFAULT_ALIGNMENT = Path("data/interim/risk_alignment")
DEFAULT_ITEMS = Path("data/interim/risk_items")
DEFAULT_SECTIONS = Path("data/interim/section_texts")
DEFAULT_OUT = Path("artifacts/temporal_eval.json")
FLAGSHIP_PAIR = "NVDA-0001045810-25-000023-0001045810-26-000021"
FLAGSHIP_NEEDLES = ("Notified Advanced Computing", "out of China and Hong Kong", "AI Diffusion")
PRECISION_MIN, RECALL_MIN = 0.90, 0.80
MIN_POSITIVES = 5
EARLY_FAIL_ALPHA = 0.05
MIN_COVERAGE = 0.90
GUARD_RATIO = 90
OFFSET_MATCH_MIN = 0.98
SPLITS = ("development", "held_out")
Z95 = 1.96
PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT-DATA"
# decision label -> the class scoring uses; anything else is listed under ``unknown_labels`` and counted as uncertain
OLDER_PRED = {"unchanged": "unchanged", "reworded": "reworded", "merged": "merged", "removed": "removed", "uncertain": "uncertain"}
NEWER_PRED = {"carried": "unchanged", "new": "removed", "uncertain": "uncertain"}
NEWER_GOLD = {"carried": "unchanged", "new": "removed"}
CAVEATS = (
    "Held-out labels are sampled partly from algorithm output (every item the algorithm marks removed or new, and the items in its "
    "uncertainty band, are labelled), so held-out recall is biased upward; read it as an upper bound, not an estimate.",
    "Development pairs were used to design and tune the algorithm: their numbers are informative only, not gates.",
    "The sentence gold is a seeded sample of items per pair: passage metrics cover only the sampled items.",
    "Rate gates never pass on fewer than 5 positives; named-case checks (flagship, coverage, false-drop guard) are exact.",
)


class InputError(ValueError):
    """An input is unusable (not frozen, missing, or inconsistent): the run stops before anything is scored."""


def load_gold(paths: list[Path]) -> dict:
    """The verified, merged gold of one or more frozen files: ``pairs`` and ``sentences`` are unioned, ``files`` maps each file
    name to its sha256 and ``sha256`` is the file's own hash (one file) or the hash of the sorted file hashes (several).
    Every file must verify; a pair side or sentence entry that appears in two files is refused, never silently overridden."""
    docs: dict[str, dict] = {}
    for path in paths:
        if not path.exists() or not gold.verify_frozen(path):
            raise InputError(f"{path} is missing or its sha256 does not verify (edited after the freeze, or never frozen): "
                             f"refusing to score against it")
        key = path.name if path.name not in docs else path.as_posix()
        docs[key] = json.loads(path.read_text(encoding="utf-8"))
    merged: dict = {"kind": "risk_items_gold", "pairs": {}, "sentences": {}}
    for name, doc in docs.items():
        for table in ("pairs", "sentences"):
            for entry_id, entry in (doc.get(table) or {}).items():
                if entry_id in merged[table]:
                    raise InputError(f"{table[:-1]} entry {entry_id} is in more than one gold file (second: {name}): the files must "
                                     f"cover disjoint pairs")
                merged[table][entry_id] = entry
    hashes = {name: doc["sha256"] for name, doc in docs.items()}
    merged["files"] = hashes
    merged["sha256"] = (next(iter(hashes.values())) if len(hashes) == 1
                        else hashlib.sha256(",".join(sorted(hashes.values())).encode("utf-8")).hexdigest())
    return merged


# --- statistics -------------------------------------------------------------------------------------------------------

def wilson_interval(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """The Wilson score interval of k successes in n trials; None when n is 0."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _binomial_tail(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p)."""
    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def rate_status(k: int, n: int, threshold: float) -> str:
    """PASS / FAIL / INSUFFICIENT-DATA for a rate gate. With 5 or more trials the point estimate decides. With fewer it never
    passes; it FAILS only when k successes in n trials are statistically incompatible with a true rate at the threshold (the
    one-sided exact binomial tail is below 0.05, e.g. two misses in a row against 0.80), else it is INSUFFICIENT-DATA."""
    if n >= MIN_POSITIVES:
        return PASS if k / n >= threshold else FAIL
    return FAIL if n > 0 and _binomial_tail(k, n, threshold) < EARLY_FAIL_ALPHA else INSUFFICIENT


def _ratio(k: int, n: int) -> float | None:
    return round(k / n, 6) if n else None


def _ci(k: int, n: int) -> list[float] | None:
    interval = wilson_interval(k, n)
    return None if interval is None else [round(interval[0], 6), round(interval[1], 6)]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- loading --------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Alignment:
    pairs: pd.DataFrame
    decisions: pd.DataFrame
    passages: pd.DataFrame
    hashes: dict


def load_alignment(directory: Path) -> Alignment:
    if not directory.is_dir() or not list(directory.glob("*_pairs.parquet")):
        raise InputError(f"no alignment output in {directory}: expected <T>_pairs.parquet / <T>_decisions.parquet / "
                         f"<T>_passages.parquet (run `semigraph align-items`)")

    def frame(suffix: str) -> pd.DataFrame:
        files = sorted(directory.glob(f"*_{suffix}.parquet"))
        return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else pd.DataFrame()

    hashes = {p.name: _sha256(p) for p in sorted(directory.glob("*.parquet"))}
    return Alignment(frame("pairs"), frame("decisions"), frame("passages"), hashes)


def _labels(decisions: pd.DataFrame, accession: str, side: str, mapping: dict, unknown: dict) -> dict[str, str]:
    """item_id -> the scoring class of the algorithm's label; an unlisted label is recorded and scored as uncertain."""
    if decisions.empty:
        return {}
    rows = decisions[(decisions["accession_no"] == accession) & (decisions["side"] == side)]
    out = {}
    for item_id, label in zip(rows["item_id"], rows["label"], strict=True):
        if label in mapping:
            out[item_id] = mapping[label]
        else:
            unknown.setdefault(side, Counter())[str(label)] += 1
            out[item_id] = "uncertain"
    return out


# --- item level -------------------------------------------------------------------------------------------------------------

def _item_ids(truth: dict[str, str], predicted: dict[str, str]) -> dict[str, list[str]]:
    fp, fn, unc, unpredicted = [], [], [], []
    for item in sorted(truth):
        pred = predicted.get(item)
        if pred is None:
            unpredicted.append(item)
            pred = "uncertain"
        if pred == "uncertain":
            unc.append(item)
        if truth[item] == "removed" and pred != "removed":
            fn.append(item)
        elif pred == "removed" and truth[item] != "removed":
            fp.append(item)
    return {"fp": fp, "fn": fn, "uncertain": unc, "unpredicted": unpredicted}


def score_items(truth: dict[str, str], predicted: dict[str, str]) -> dict:
    """``gold.score_predictions`` counts plus the ids behind them (a cross-check guards against the two drifting apart)."""
    m = gold.score_predictions(truth, predicted)
    ids = _item_ids(truth, predicted)
    gold_positive = m.true_drops + m.missed_drops + sum(1 for i in ids["uncertain"] if truth[i] == "removed")
    if len(ids["fp"]) != m.false_drops or m.uncertain != len(ids["uncertain"]) or len(ids["fn"]) != gold_positive - m.true_drops:
        raise RuntimeError("item scoring drifted from gold.score_predictions: fix scripts/verify_temporal.py")
    return {"n": m.n, "tp": m.true_drops, "fp": m.false_drops, "fn": gold_positive - m.true_drops, "gold_positive": gold_positive,
            "uncertain": m.uncertain, "unpredicted": len(ids["unpredicted"]), "false_positive_ids": ids["fp"],
            "false_negative_ids": ids["fn"]}


def _merge_item_blocks(blocks: list[dict]) -> dict:
    tp, fp = sum(b["tp"] for b in blocks), sum(b["fp"] for b in blocks)
    gp, n, unc = sum(b["gold_positive"] for b in blocks), sum(b["n"] for b in blocks), sum(b["uncertain"] for b in blocks)
    return {"n_pairs": len(blocks), "n_gold_items": n, "tp": tp, "fp": fp, "fn": gp - tp, "gold_positive": gp,
            "predicted_positive": tp + fp, "uncertain": unc, "uncertain_rate": _ratio(unc, n) or 0.0,
            "unpredicted": sum(b["unpredicted"] for b in blocks), "precision": _ratio(tp, tp + fp),
            "precision_ci": _ci(tp, tp + fp), "recall": _ratio(tp, gp), "recall_ci": _ci(tp, gp),
            "false_positive_ids": sorted(i for b in blocks for i in b["false_positive_ids"]),
            "false_negative_ids": sorted(i for b in blocks for i in b["false_negative_ids"])}


# --- passages --------------------------------------------------------------------------------------------------------------

def _passage_dicts(passages: pd.DataFrame, older: str, newer: str) -> list[dict]:
    if passages.empty:
        return []
    rows = passages[(passages["older_accession"] == older) & (passages["newer_accession"] == newer)]
    return [{"kind": r.kind, "item_id": r.item_id, "char_start": None if pd.isna(r.char_start) else int(r.char_start),
             "char_end": None if pd.isna(r.char_end) else int(r.char_end), "text": r.text, "passage_id": r.passage_id}
            for r in rows.itertuples(index=False)]


def check_offsets(passages: list[dict], texts: dict[str, str], older: str, newer: str) -> tuple[int, int, list[str]]:
    """(checked, matched, ids of the first mismatches): does the section text at the passage's offsets equal the passage text?"""
    checked = matched = 0
    bad: list[str] = []
    for p in passages:
        section = texts[newer if p["kind"] == "added" else older]
        checked += 1
        ok = (p["char_start"] is not None and p["char_end"] is not None and isinstance(p["text"], str)
              and normalize(section[p["char_start"]:p["char_end"]]) == normalize(p["text"]))
        matched += ok
        if not ok and len(bad) < 3:
            bad.append(str(p["passage_id"]))
    return checked, matched, bad


def _merge_passage_blocks(blocks: list[dict]) -> dict:
    def total(path: tuple[str, str]) -> int:
        return sum(b[path[0]][path[1]] for b in blocks)

    s_tp, s_fp, s_gp = total(("sentence", "tp")), total(("sentence", "fp")), sum(b["n_gold_positive"] for b in blocks)
    p_tp, p_fp = total(("passage", "tp")), total(("passage", "fp"))
    runs, recalled = total(("passage", "n_gold_runs")), total(("passage", "recalled"))
    confusion: dict[str, dict[str, int]] = {}
    for b in blocks:
        for truth, cell in b["confusion"].items():
            for pred, count in cell.items():
                confusion.setdefault(truth, {})[pred] = confusion.get(truth, {}).get(pred, 0) + count
    n_sent = sum(b["n_gold_sentences"] for b in blocks)
    unc = sum(b["uncertain"] for b in blocks)
    return {"n_pairs": len(blocks), "n_gold_sentences": n_sent, "n_gold_positive": s_gp,
            "sentence": {"tp": s_tp, "fp": s_fp, "fn": s_gp - s_tp, "precision": _ratio(s_tp, s_tp + s_fp),
                         "precision_ci": _ci(s_tp, s_tp + s_fp), "recall": _ratio(s_tp, s_gp), "recall_ci": _ci(s_tp, s_gp),
                         "false_positive_ids": sorted(i for b in blocks for i in b["false_positive_ids"]),
                         "false_negative_ids": sorted(i for b in blocks for i in b["false_negative_ids"])},
            "passage": {"n_predicted": total(("passage", "n_predicted")), "tp": p_tp, "fp": p_fp,
                        "unverifiable": total(("passage", "unverifiable")), "precision": _ratio(p_tp, p_tp + p_fp),
                        "precision_ci": _ci(p_tp, p_tp + p_fp), "n_gold_runs": runs, "recalled": recalled,
                        "recall": _ratio(recalled, runs), "recall_ci": _ci(recalled, runs)},
            "uncertain": unc, "uncertain_rate": _ratio(unc, n_sent) or 0.0, "confusion": confusion,
            "ignored_passages": sum(b["ignored_passages"] for b in blocks)}


# --- the false-drop guard ------------------------------------------------------------------------------------------------------

def false_drops(removed: list[dict], newer_text: str, pair_id: str) -> tuple[list[dict], int, int]:
    """(violations, checked, skipped without a headline) for the removed older items of one pair against the newer section."""
    from semigraph.graph.alignment import split_sentences      # lazy: pulls in numpy / scipy

    sentences = [newer_text[a:b] for a, b in split_sentences(newer_text)]
    section_norm = normalize(newer_text)
    violations, checked, skipped = [], 0, 0
    for item in removed:
        headline = (item.get("headline") or "").strip()
        if not headline:
            skipped += 1
            continue
        checked += 1
        best = process.extractOne(headline, sentences, scorer=fuzz.ratio, processor=default_process, score_cutoff=GUARD_RATIO)
        contained = normalize(headline) in section_norm
        if best is not None or contained:
            violations.append({"item_id": item["item_id"], "headline": headline, "pair_id": pair_id,
                               "score": 100.0 if contained else round(float(best[1]), 2)})
    return violations, checked, skipped


# --- the evaluation ----------------------------------------------------------------------------------------------------------

def _gate(status: str, n: int, **extra) -> dict:
    return {"status": status, "n": n, **extra}


def _rate_gate(k: int, n: int, threshold: float, interval: list[float] | None, detail: str | None = None) -> dict:
    return _gate(rate_status(k, n, threshold), n, threshold=threshold, value=_ratio(k, n), interval=interval,
                 **({"detail": detail} if detail else {}))


class _Evaluation:
    """The accumulators of one run over the alignment's pairs (kept together so no function has to carry twenty of them)."""

    def __init__(self, gold_doc: dict, alignment: Alignment, items: pd.DataFrame, sections: pd.DataFrame, quality: dict,
                 flagship_pair: str):
        self.gold_doc, self.alignment, self.items, self.sections = gold_doc, alignment, items, sections
        self.quality, self.flagship_pair = quality, flagship_pair
        self.headlines = dict(zip(items["item_id"], items["headline"], strict=True))
        self.texts: dict[str, str] = {}
        self.unknown: dict[str, Counter] = {}
        self.item_blocks: dict = {(side, s): [] for side in ("older", "newer") for s in SPLITS}
        self.passage_blocks: dict = {(side, s): [] for side in ("older", "newer") for s in SPLITS}
        self.pair_rows: list[dict] = []
        self.excluded: list[dict] = []
        self.below: list[dict] = []
        self.unscored: list[dict] = []
        self.guard: list[dict] = []
        self.guard_checked = self.guard_skipped = 0
        self.off_checked = self.off_matched = 0
        self.off_bad: list[str] = []
        self.flag: dict = {"scored": False, "removed_passages": [], "items": {}}

    def text_of(self, accession: str) -> str:
        if accession not in self.texts:
            self.texts[accession] = risk_section_text(self.items, self.sections, accession)
        return self.texts[accession]

    def run(self) -> "_Evaluation":
        for row in self.alignment.pairs.sort_values("pair_id").to_dict("records"):
            self._pair(row)
        if self.off_checked and self.off_matched / self.off_checked < OFFSET_MATCH_MIN:
            raise InputError(
                f"passage offsets do not match the section text: {self.off_matched} of {self.off_checked} passages have their text "
                f"at section_text[char_start:char_end] (need {OFFSET_MATCH_MIN:.0%}; first mismatches: {', '.join(self.off_bad)}). "
                f"char_start/char_end must be offsets into the RISK SECTION text, not into the item")
        return self

    def _pair(self, row: dict) -> None:
        pid, older, newer = row["pair_id"], row["older_accession"], row["newer_accession"]
        reason = row["not_compared_reason"] if isinstance(row["not_compared_reason"], str) else None
        entry = {"pair_id": pid, "comparable": bool(row["comparable"]), "not_compared_reason": reason, "scored": False}
        self.pair_rows.append(entry)
        if not row["comparable"]:
            self.excluded.append({"pair_id": pid, "reason": reason})
            return
        cov = [self.quality.get(a, {}).get("coverage") for a in (older, newer)]
        entry["coverage_older"], entry["coverage_newer"] = cov
        if any(c is None or float(c) < MIN_COVERAGE for c in cov):
            self.below.append({"pair_id": pid, "older": cov[0], "newer": cov[1]})
        older_pred = _labels(self.alignment.decisions, older, "older", OLDER_PRED, self.unknown)
        newer_pred = _labels(self.alignment.decisions, newer, "newer", NEWER_PRED, self.unknown)
        self._false_drop_guard(pid, newer, older_pred)
        gold_pairs = self.gold_doc["pairs"]
        if f"{pid}|older" not in gold_pairs or f"{pid}|newer" not in gold_pairs:
            return                                                  # not in the gold: the guard and coverage still apply
        if not older_pred and not newer_pred:
            self.unscored.append({"pair_id": pid, "reason": "the alignment has no decisions for this pair"})
            return
        self._score_pair(entry, row, older_pred, newer_pred)

    def _false_drop_guard(self, pid: str, newer: str, older_pred: dict[str, str]) -> None:
        removed = [{"item_id": i, "headline": self.headlines.get(i, "")} for i, lab in sorted(older_pred.items()) if lab == "removed"]
        if removed:
            violations, checked, skipped = false_drops(removed, self.text_of(newer), pid)
            self.guard += violations
            self.guard_checked += checked
            self.guard_skipped += skipped

    def _score_pair(self, entry: dict, row: dict, older_pred: dict[str, str], newer_pred: dict[str, str]) -> None:
        pid, older, newer = row["pair_id"], row["older_accession"], row["newer_accession"]
        gold_older, gold_newer = self.gold_doc["pairs"][f"{pid}|older"], self.gold_doc["pairs"][f"{pid}|newer"]
        split = gold_older["split"]
        entry.update({"scored": True, "split": split})
        older_block = score_items(gold_older["labels"], older_pred)
        newer_block = score_items({k: NEWER_GOLD[v] for k, v in gold_newer["labels"].items()}, newer_pred)
        self.item_blocks[("older", split)].append(older_block)
        self.item_blocks[("newer", split)].append(newer_block)
        entry["older_removed"], entry["newer_new"] = ({k: b[k] for k in ("n", "tp", "fp", "fn", "uncertain")}
                                                       for b in (older_block, newer_block))
        preds = _passage_dicts(self.alignment.passages, older, newer)
        checked, matched, bad = check_offsets(preds, {older: self.text_of(older), newer: self.text_of(newer)}, older, newer)
        self.off_checked, self.off_matched, self.off_bad = self.off_checked + checked, self.off_matched + matched, self.off_bad + bad
        for side in ("older", "newer"):
            sentence_entry = (self.gold_doc.get("sentences") or {}).get(f"{pid}|{side}")
            if sentence_entry:
                metrics = gold.score_passages(preds, gold.gold_sentence_records(sentence_entry), side=side).to_dict()
                self.passage_blocks[(side, sentence_entry["split"])].append(metrics)
        if pid == self.flagship_pair:
            self.flag = {"scored": True, "removed_passages": preds, "items": {"older": older_block, "newer": newer_block}}


def _flagship_gates(flag: dict, needles: tuple[str, ...]) -> tuple[dict, dict]:
    if not flag["scored"]:
        note = "the flagship pair was not compared or is not in the alignment / gold"
        return _gate(INSUFFICIENT, 0, detail={"reason": note}), _gate(INSUFFICIENT, 0, detail={"reason": note})
    hits = gold.must_hit(flag["removed_passages"], needles, kind="removed")
    passages = _gate(PASS if all(hits.values()) else FAIL, len(needles), detail={"must_hit": hits})
    older, newer = flag["items"]["older"], flag["items"]["newer"]
    detail = {"false_removed": older["fp"], "missed_removed": older["fn"], "false_new": newer["fp"], "missed_new": newer["fn"]}
    return passages, _gate(FAIL if any(detail.values()) else PASS, older["n"] + newer["n"], detail=detail)


def _passage_gates(po: dict) -> tuple[dict, dict]:
    if not po["n_pairs"]:
        note = "no held-out sentence gold: the passage gates cannot be evaluated"
        return _gate(INSUFFICIENT, 0, detail=note), _gate(INSUFFICIENT, 0, detail=note)
    p = po["passage"]
    return (_rate_gate(p["tp"], p["tp"] + p["fp"], PRECISION_MIN, p["precision_ci"]),
            _rate_gate(p["recalled"], p["n_gold_runs"], RECALL_MIN, p["recall_ci"]))


def _gates(ev: _Evaluation, item_level: dict, passage_level: dict, needles: tuple[str, ...]) -> dict:
    ho = item_level["older_removed"]["held_out"]
    passage_precision, passage_recall = _passage_gates(passage_level["older"]["held_out"])
    flag_passages, flag_items = _flagship_gates(ev.flag, needles)
    n_comparable = sum(1 for r in ev.pair_rows if r["comparable"])
    coverage = FAIL if ev.below else (PASS if n_comparable else INSUFFICIENT)
    guard = FAIL if ev.guard else (PASS if ev.guard_checked else INSUFFICIENT)
    return {
        "item_drop_precision_heldout": _rate_gate(ho["tp"], ho["tp"] + ho["fp"], PRECISION_MIN, ho["precision_ci"]),
        "item_drop_recall_heldout": _rate_gate(ho["tp"], ho["gold_positive"], RECALL_MIN, ho["recall_ci"]),
        "passage_drop_precision_heldout": passage_precision, "passage_drop_recall_heldout": passage_recall,
        "nvda_flagship_passages": flag_passages, "nvda_flagship_items": flag_items,
        "unit_coverage": _gate(coverage, n_comparable, threshold=MIN_COVERAGE,
                               detail={"below_threshold": ev.below, "n_excluded": len(ev.excluded)}),
        "false_drop_guard": _gate(guard, ev.guard_checked, threshold=GUARD_RATIO, detail={"n_violations": len(ev.guard)}),
    }


def _max_passage_chars(passages: pd.DataFrame) -> int | None:
    """The longest passage text of the alignment: tells a tuned run (cap 450) from one built with the legacy 1200-character cap."""
    if passages.empty or "text" not in passages:
        return None
    return int(passages["text"].str.len().max())


def _report(ev: _Evaluation, needles: tuple[str, ...]) -> dict:
    item_level = {name: {s: _merge_item_blocks(ev.item_blocks[(side, s)]) for s in SPLITS}
                  for name, side in (("older_removed", "older"), ("newer_new", "newer"))}
    passage_level = {side: {s: _merge_passage_blocks(ev.passage_blocks[(side, s)]) for s in SPLITS} for side in ("older", "newer")}
    gates = _gates(ev, item_level, passage_level, needles)
    statuses = {g["status"] for g in gates.values()}
    guard = gates["false_drop_guard"]["status"]
    return {"kind": "temporal_eval", "caveats": list(CAVEATS),
            "inputs": {"gold_sha256": ev.gold_doc["sha256"], "gold_files": ev.gold_doc.get("files", {}),
                       "alignment_sha256": ev.alignment.hashes,
                       "alignment_max_passage_chars": _max_passage_chars(ev.alignment.passages),
                       "flagship_pair": ev.flagship_pair, "flagship_needles": list(needles)},
            "thresholds": {"precision": PRECISION_MIN, "recall": RECALL_MIN, "min_positives": MIN_POSITIVES,
                           "coverage": MIN_COVERAGE, "guard_ratio": GUARD_RATIO},
            "pairs": ev.pair_rows, "item_level": item_level, "passage_level": passage_level,
            "passage_offsets": {"checked": ev.off_checked, "matched": ev.off_matched},
            "coverage": {"n_comparable": gates["unit_coverage"]["n"], "below_threshold": ev.below, "excluded": ev.excluded,
                         "unscored": ev.unscored},
            "false_drop_guard": {"status": guard, "n_checked": ev.guard_checked, "skipped_no_headline": ev.guard_skipped,
                                 "violations": sorted(ev.guard, key=lambda v: (v["pair_id"], v["item_id"])), "threshold": GUARD_RATIO},
            "unknown_labels": {side: dict(sorted(c.items())) for side, c in sorted(ev.unknown.items())},
            "gates": gates, "overall": FAIL if FAIL in statuses else (INSUFFICIENT if INSUFFICIENT in statuses else PASS)}


def evaluate(gold_doc: dict, alignment: Alignment, items: pd.DataFrame, sections: pd.DataFrame, quality: dict, *,
             flagship_pair: str = FLAGSHIP_PAIR, needles: tuple[str, ...] = FLAGSHIP_NEEDLES) -> dict:
    """Score every alignment pair that is in the gold and assemble the report (raises ``InputError`` on bad passage offsets)."""
    return _report(_Evaluation(gold_doc, alignment, items, sections, quality, flagship_pair).run(), needles)


# --- output ---------------------------------------------------------------------------------------------------------------

def _fmt(value: float | None, interval: list[float] | None) -> str:
    return "  -  " if value is None else f"{value:.3f}" + (f" [{interval[0]:.2f},{interval[1]:.2f}]" if interval else "")


def print_report(report: dict) -> None:
    print(f"verify_temporal: gold {report['inputs']['gold_sha256'][:8]} ({len(report['inputs']['gold_files'])} file(s)), "
          f"{len(report['pairs'])} alignment pairs (longest passage {report['inputs']['alignment_max_passage_chars']} chars), "
          f"overall {report['overall']}")
    if report["unknown_labels"]:
        print(f"WARNING: unknown decision labels (scored as uncertain): {json.dumps(report['unknown_labels'], sort_keys=True)}")
    print(f"\n{'level':<22}{'split':<13}{'pairs':>5} {'gold+':>5} {'tp':>4} {'fp':>4} {'fn':>4} {'unc%':>6}  "
          f"{'precision [95% CI]':<22}{'recall [95% CI]':<22}")
    for name, splits in report["item_level"].items():
        for split, b in splits.items():
            print(f"{'item ' + name:<22}{split:<13}{b['n_pairs']:>5} {b['gold_positive']:>5} {b['tp']:>4} {b['fp']:>4} {b['fn']:>4} "
                  f"{100 * b['uncertain_rate']:>5.1f}%  {_fmt(b['precision'], b['precision_ci']):<22}{_fmt(b['recall'], b['recall_ci']):<22}")
    for side, splits in report["passage_level"].items():
        for split, b in splits.items():
            p = b["passage"]
            print(f"{'passage ' + side:<22}{split:<13}{b['n_pairs']:>5} {p['n_gold_runs']:>5} {p['tp']:>4} {p['fp']:>4} "
                  f"{p['n_gold_runs'] - p['recalled']:>4} {100 * b['uncertain_rate']:>5.1f}%  "
                  f"{_fmt(p['precision'], p['precision_ci']):<22}{_fmt(p['recall'], p['recall_ci']):<22}")
    print("\ngates:")
    for name, g in report["gates"].items():
        value = "" if g.get("value") is None else f"  {g['value']:.3f} (need {g['threshold']})"
        print(f"  {name:<32}{g['status']:<18}n={g['n']}{value}")
    if report["coverage"]["excluded"]:
        print(f"\nexcluded (not compared): {len(report['coverage']['excluded'])} pair(s)")
    for v in report["false_drop_guard"]["violations"]:
        print(f"  FALSE DROP {v['item_id']}: {v['headline'][:80]}")
    print("\ncaveats:\n" + "\n".join(f"  - {c}" for c in report["caveats"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", type=Path, action="append",
                    help="frozen gold file (repeatable: development + held-out; default: artifacts/gold/risk_items_gold.json)")
    ap.add_argument("--alignment", type=Path, default=DEFAULT_ALIGNMENT)
    ap.add_argument("--items-dir", type=Path, default=DEFAULT_ITEMS)
    ap.add_argument("--sections-dir", type=Path, default=DEFAULT_SECTIONS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--flagship-pair", default=FLAGSHIP_PAIR)
    ap.add_argument("--needle", action="append", help="flagship passage needle (repeatable; default: NAC, Hong Kong, AI Diffusion)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when a gate FAILS, 3 when the overall result is INSUFFICIENT-DATA")
    args = ap.parse_args(argv)
    try:
        gold_doc = load_gold(args.gold or [DEFAULT_GOLD])
        alignment = load_alignment(args.alignment)
        items = pd.concat([pd.read_parquet(f) for f in sorted(args.items_dir.glob("*_risk_items.parquet"))], ignore_index=True)
        sections = pd.concat([pd.read_parquet(f) for f in sorted(args.sections_dir.glob("*_section_texts.parquet"))], ignore_index=True)
        quality = risk_item_quality.load_quality(args.items_dir)
        report = evaluate(gold_doc, alignment, items, sections, quality, flagship_pair=args.flagship_pair,
                          needles=tuple(args.needle) if args.needle else FLAGSHIP_NEEDLES)
    except (InputError, ValueError, KeyError, FileNotFoundError) as err:
        print(f"error: {err.args[0] if err.args else err}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print_report(report)
    print(f"\nreport -> {args.out}")
    if args.strict and report["overall"] == FAIL:
        return 1
    if args.strict and report["overall"] == INSUFFICIENT:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
