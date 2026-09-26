"""Synthetic gold + lake + alignment output for the verify_temporal tests (imported by tests, not collected).

A ``PairSpec`` describes one consecutive annual pair: how many items each filing has, the GOLD label of every item, the
alignment's PREDICTED label of every item (W-I's ``<T>_decisions.parquet`` schema), and optionally gold sentence labels and
predicted passages. Item ``i`` of a filing has a headline ``"<tag> headline <i>."`` and a body of three sentences (the
headline sentence, sentence A and sentence B); the risk section is the bodies joined by newlines, so section offsets are exact.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from semigraph.eval import gold

QUALITY_OK = {"low_coverage": False, "section_suspect": False, "coverage": 0.97, "n_items": 8, "method": "html", "notes": []}


@dataclass
class PairSpec:
    ticker: str
    older_acc: str
    newer_acc: str
    split: str
    older_gold: list[str]
    older_pred: list[str] | None = None                      # None: the alignment has no decisions for the pair
    newer_gold: list[str] | None = None                      # default: every newer item "carried"
    newer_pred: list[str] | None = None
    comparable: bool = True
    reason: str | None = None
    coverage: tuple[float, float] = (0.97, 0.97)
    newer_extra_text: str = ""                               # appended to the newer section (e.g. a surviving headline)
    gold_sentences: list[tuple[int, int, str]] = field(default_factory=list)      # (older item index, sentence 0-2, label)
    gold_sentences_newer: list[tuple[int, int, str]] = field(default_factory=list)      # (newer item index, sentence 0-2, present/reworded/added)
    newer_split: str | None = None                           # the newer side's split when it differs from ``split`` (a malformed gold)
    passages: list[dict] = field(default_factory=list)       # {"kind", "item": i, "sentence": n} or {"kind", "item", "text", "start", "end"}
    item_relative_offsets: bool = False
    paragraph: bool = False

    @property
    def pair_id(self) -> str:
        return f"{self.ticker}-{self.older_acc}-{self.newer_acc}"


# Headlines of the two filings of a pair share no words, and no two of them are alike: the fuzzy false-drop guard (rapidfuzz
# ratio >= 90) must find a headline in the newer text only when a test puts it there.
OLDER_HEADLINES = ("Our supply chain depends on a small number of contract manufacturers.",
                   "Currency swings could reduce our reported revenue in several regions.",
                   "Pending litigation may require costly settlements or damages awards.",
                   "Tariffs and trade restrictions could raise the cost of our components.",
                   "We may lose key engineers to better funded competitors abroad.",
                   "Climate related events could interrupt production at our fabrication sites.",
                   "Price competition may compress our margins during industry downturns.",
                   "Product warranty claims could exceed the reserves we have recorded.",
                   "Privacy regulation increases our compliance burden for customer data.",
                   "Our customers concentrate purchases among a few large distributors.")
NEWER_HEADLINES = ("Cyberattacks on our networks might expose confidential designs.",
                   "Inflation of wafer prices threatens our gross profit outlook.",
                   "Government subsidies for rivals may distort the markets we serve.",
                   "Quarterly results fluctuate with unpredictable inventory adjustments.",
                   "Our acquisitions could fail to deliver the synergies management expects.",
                   "Changes in tax law might increase our effective rate significantly.",
                   "Open source licensing terms may restrict how we ship software.",
                   "Natural disasters in seismic regions could halt shipments for months.",
                   "Executive turnover would disrupt our strategic planning processes.",
                   "Standards bodies may adopt technologies that favor other suppliers.")


def headline(side: str, i: int) -> str:
    return (OLDER_HEADLINES if side == "older" else NEWER_HEADLINES)[i]


def _sentences(tag: str, i: int, side: str = "older") -> list[str]:
    return [headline(side, i), f"Sentence A of {tag} item {i} written for the fixture with enough words.",
            f"Sentence B of {tag} item {i} written for the fixture with enough words."]


def _filing(acc: str, ticker: str, n: int, *, side: str = "older", extra: str = "", paragraph: bool = False):
    tag = acc[-6:]
    bodies = [" ".join(_sentences(tag, i, side)) for i in range(n)]
    text = "\n".join(bodies) + (("\n" + extra) if extra else "")
    rows, pos = [], 0
    for i, body in enumerate(bodies):
        start = text.index(body, pos)
        pos = start + len(body)
        rows.append({"item_id": f"{acc}:I.1A:i{i:03d}", "accession_no": acc, "ticker": ticker, "filer_cik": 1, "form": "10-K",
                     "filing_date": "2025-02-26", "section_id": "I.1A", "seq": i, "headline": "" if paragraph else headline(side, i),
                     "text": body, "text_hash": f"h{tag}{i}", "char_start": start, "char_end": start + len(body),
                     "unit_kind": "paragraph" if paragraph else "headline", "chunk_ids": [f"{acc}:I.1A:{i:04d}"]})
    return rows, text


def _decision(item_id, acc, side, label):
    return {"item_id": item_id, "accession_no": acc, "side": side, "label": label, "matched_item_id": None, "decided_by": "test",
            "sim_embed": None, "sim_lex": None, "headline_ratio": None, "quote": None, "quote_span_start": None,
            "quote_span_end": None, "adjudicated": False}


def build(tmp: Path, specs: list[PairSpec]) -> dict:
    items_dir, sections_dir, align_dir = tmp / "risk_items", tmp / "section_texts", tmp / "risk_alignment"
    for d in (items_dir, sections_dir, align_dir):
        d.mkdir()
    all_items, all_sections, quality, pairs_rows, decisions, passages = [], [], {}, [], [], []
    gold_pairs, gold_sentences = {}, {}
    texts = {}
    for spec in specs:
        n_old, n_new = len(spec.older_gold), len(spec.newer_gold or _carried(spec))
        older_rows, older_text = _filing(spec.older_acc, spec.ticker, n_old, paragraph=spec.paragraph)
        newer_rows, newer_text = _filing(spec.newer_acc, spec.ticker, n_new, side="newer", extra=spec.newer_extra_text,
                                         paragraph=spec.paragraph)
        texts[spec.older_acc], texts[spec.newer_acc] = older_text, newer_text
        all_items += older_rows + newer_rows
        for acc, text in ((spec.older_acc, older_text), (spec.newer_acc, newer_text)):
            all_sections.append({"accession_no": acc, "section_id": "I.1A", "form": "10-K", "filing_date": "2025-02-26",
                                 "section_title": "Risk", "text": text, "n_chars": len(text)})
        for acc, cov in zip((spec.older_acc, spec.newer_acc), spec.coverage, strict=True):
            quality[acc] = {**QUALITY_OK, "accession_no": acc, "coverage": cov, "low_coverage": cov < 0.9}
        newer_gold = spec.newer_gold or _carried(spec)
        gold_pairs[f"{spec.pair_id}|older"] = {"split": spec.split, "labels": {r["item_id"]: g for r, g in zip(older_rows, spec.older_gold, strict=True)}}
        gold_pairs[f"{spec.pair_id}|newer"] = {"split": spec.newer_split or spec.split, "labels": {r["item_id"]: g for r, g in zip(newer_rows, newer_gold, strict=True)}}
        pairs_rows.append({"pair_id": spec.pair_id, "ticker": spec.ticker, "older_accession": spec.older_acc,
                           "newer_accession": spec.newer_acc, "older_date": "2025-02-26", "newer_date": "2026-02-25",
                           "comparable": spec.comparable, "not_compared_reason": spec.reason})
        if spec.older_pred is not None:
            decisions += [_decision(r["item_id"], spec.older_acc, "older", p) for r, p in zip(older_rows, spec.older_pred, strict=True)]
            newer_pred = spec.newer_pred or ["carried"] * n_new
            decisions += [_decision(r["item_id"], spec.newer_acc, "newer", p) for r, p in zip(newer_rows, newer_pred, strict=True)]
        if spec.gold_sentences:
            gold_sentences[f"{spec.pair_id}|older"] = _gold_sentence_entry(spec.gold_sentences, spec.split, older_rows, older_text,
                                                                           spec.older_acc, "older")
        if spec.gold_sentences_newer:
            gold_sentences[f"{spec.pair_id}|newer"] = _gold_sentence_entry(spec.gold_sentences_newer, spec.newer_split or spec.split,
                                                                           newer_rows, newer_text, spec.newer_acc, "newer")
        passages += _passage_rows(spec, older_rows, older_text, newer_rows, newer_text)
    frozen = tmp / "gold.json"
    gold.freeze({"kind": "risk_items_gold", "pairs": gold_pairs, "sentences": gold_sentences}, frozen)
    pd.DataFrame(all_items).to_parquet(items_dir / "ZZ_risk_items.parquet")
    pd.DataFrame(all_sections).to_parquet(sections_dir / "ZZ_section_texts.parquet")
    (items_dir / "ZZ_risk_items_quality.json").write_text(json.dumps({"ticker": "ZZ", "filings": list(quality.values())}), encoding="utf-8")
    pd.DataFrame(pairs_rows).to_parquet(align_dir / "ZZ_pairs.parquet")
    pd.DataFrame(decisions, columns=list(_decision("x", "x", "older", "x"))).to_parquet(align_dir / "ZZ_decisions.parquet")
    pd.DataFrame(passages, columns=PASSAGE_COLUMNS).to_parquet(align_dir / "ZZ_passages.parquet")
    return {"gold": frozen, "items_dir": items_dir, "sections_dir": sections_dir, "alignment": align_dir, "texts": texts}


def _carried(spec: PairSpec) -> list[str]:
    return ["carried"] * len(spec.older_gold)


def _gold_sentence_entry(records: list[tuple[int, int, str]], split: str, rows: list[dict], section_text: str, acc: str, side: str) -> dict:
    labels, spans = {}, {}
    for item, sentence, label in records:
        row = rows[item]
        sid = f"{row['item_id']}#s{sentence:03d}"
        text = _sentences(acc[-6:], item, side)[sentence]
        start = section_text.index(text, row["char_start"])
        labels[sid], spans[sid] = label, [row["item_id"], start, start + len(text)]
    return {"split": split, "labels": labels, "spans": spans}


PASSAGE_COLUMNS = ["passage_id", "kind", "item_id", "seq", "text", "char_start", "char_end", "counterpart_text", "counterpart_span",
                   "similarity", "chunk_ids", "decided_by", "older_accession", "newer_accession", "filer_cik",
                   "counterpart_chunk_ids", "chunk_fallback"]


def _passage_rows(spec: PairSpec, older_rows, older_text, newer_rows, newer_text) -> list[dict]:
    out = []
    for n, p in enumerate(spec.passages):
        rows, text, acc = (newer_rows, newer_text, spec.newer_acc) if p["kind"] == "added" else (older_rows, older_text, spec.older_acc)
        row = rows[p["item"]]
        if "text" in p:
            sentence, start, end = p["text"], p["start"], p["end"]
        else:
            sentence = _sentences(acc[-6:], p["item"], "newer" if p["kind"] == "added" else "older")[p["sentence"]]
            start = text.index(sentence, row["char_start"])
            end = start + len(sentence)
        if spec.item_relative_offsets:
            start, end = start - row["char_start"], end - row["char_start"]
        out.append({"passage_id": f"{spec.pair_id}:p{n}", "kind": p["kind"], "item_id": row["item_id"], "seq": n, "text": sentence,
                    "char_start": start, "char_end": end, "counterpart_text": None, "counterpart_span": None, "similarity": None,
                    "chunk_ids": [], "decided_by": "test", "older_accession": spec.older_acc, "newer_accession": spec.newer_acc,
                    "filer_cik": 1, "counterpart_chunk_ids": [], "chunk_fallback": False})
    return out
