"""Generate the 12 temporal benchmark questions from the FROZEN source-text gold (docs/v2/M1B_PLAN.md sections A and E).

    PYTHONPATH=src python scripts/build_temporal_questions.py                # writes artifacts/gold/temporal_questions.json
    PYTHONPATH=src python scripts/build_temporal_questions.py --dry-run      # prints what it would write
    PYTHONPATH=src python scripts/build_temporal_questions.py --merge        # ... and appends T4.. to BOTH benchmark copies

Every judge note is built ONLY from the gold labels (item labels and sentence labels of the frozen file) plus the text of the
filings the labels point at; the script has no argument for, and never reads, an algorithm's output (alignment, lineage, the
graph). That is what breaks the circularity of the earlier benchmark, whose notes assumed the drop layer worked. It refuses
when the gold's sha256 does not verify (``eval/gold.py::verify_frozen``), so a note can never come from unfrozen labels.

The 12 questions (ids T4..T15, deterministic; the seed is a documented decision, not a tuning knob):

* 6 x "Did <Company> remove any risk factors between its <older FY> and <newer FY> annual reports?", one per fully labelled
  development pair. Notes: the verified removed items (gold ``removed`` on the older side) and verified new items (gold ``new`` on
  the newer side) with their chunk ids, and the counts of unchanged / reworded / merged / carried.
* 3 x "Did <Company> stop disclosing its risk about <headline>?" on 3 of the pairs (``random.Random(f"{seed}|stop-pairs")``
  chooses the pairs; ``random.Random(f"{seed}|stop|<pair>")`` chooses the older item, preferring a ``reworded`` one). Only items
  whose gold label says still present (unchanged / reworded) and that have a headline are eligible (paragraph units have none),
  so the verified answer is always NO.
* 3 flagship passage questions on the NVDA FY25->FY26 pair (the NAC-process sentence, the China/Hong Kong-transition sentence and
  the AI Diffusion IFR "Universal Verified End Users" sentence). The sentence text is cut from the OLDER section text by the span
  the gold recorded, its gold label must be ``removed`` (a mismatch stops the build), and the containing risk factor must be one
  the gold says survives.

Fiscal years come from the period end (``xbrl_period_resolver``: an accession's latest XBRL period), never from the filing year:
AMD, META, MU and TSMC file in the year after their fiscal year. Where no period end is known the question names the filing date.

``--merge`` also replaces the ``judge_notes`` of the legacy T1, T2 and T3 (ids and question text are untouched): their original
notes ("dozens of risk lineages were dropped") asserted exactly what the audit found false, so under the new judge rule (a removal
claim is correct only if the notes support it) they would grade the wrong answers correct. Entries the merge does not touch keep
their bytes (CRLF, no trailing newline, ascii escapes); the two benchmark copies must be identical before and after.
"""

import argparse
import json
import random
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from semigraph.eval import gold as gold_mod
from semigraph.extraction.gates import normalize
from semigraph.graph.item_pairs import risk_section_text
from semigraph.universe import FILERS

SEED = 20260927
STOP_PAIRS = 3
FIRST_ID = 4
FLAGSHIP_PAIR = "NVDA-0001045810-25-000023-0001045810-26-000021"
DEFAULT_GOLD = Path("artifacts/gold/risk_items_gold.json")
DEFAULT_OUT = Path("artifacts/gold/temporal_questions.json")
DEFAULT_ITEMS = Path("data/interim/risk_items")
DEFAULT_SECTIONS = Path("data/interim/section_texts")
DEFAULT_XBRL = Path("data/processed/xbrl")
DEFAULT_BENCHMARKS = (Path("src/semigraph/artifacts/benchmark.json"), Path("artifacts/benchmark.json"))
LEGACY_TICKER = {"T1": "NVDA", "T2": "META", "T3": "NVDA"}       # legacy question -> the company its notes are refreshed from
PRESENT_LABELS = ("unchanged", "reworded")
MAX_LISTED = 6
HEADLINE_CHARS = 160
QUOTE_CHARS = 200
MAX_CHUNKS_SHOWN = 3
SENTENCES_LISTED = 8              # removed / added sentences quoted per side in a pair's notes (the flagship needles first)
SENTENCE_QUOTE_CHARS = 120
PAIR_ID_RE = re.compile(r"^([A-Z0-9]+)-(\d{10}-\d{2}-\d{6})-(\d{10}-\d{2}-\d{6})$")

FLAGSHIPS = (
    {"key": "nac", "company": "NVIDIA", "needle": "Notified Advanced Computing",
     "claim": "the Notified Advanced Computing (NAC) process has not resulted in approvals for exports of products to customers in China"},
    {"key": "hk", "company": "NVIDIA", "needle": "out of China and Hong Kong",
     "claim": "it transitioned some operations out of China and Hong Kong after the 2022 export controls"},
    {"key": "ai_diffusion", "company": "NVIDIA", "needle": "Universal Verified End Users", "context_needle": "AI Diffusion",
     "claim": 'the AI Diffusion IFR would confer special benefits on select "Universal Verified End Users" (UVEU)'},
)


class IncompleteGold(ValueError):
    """A development pair is not fully labelled (a note about it would silently cover only part of the filing)."""


class FlagshipMismatch(ValueError):
    """The gold does not say what a flagship passage question asserts (no such sentence, or it is not labelled removed)."""


class MergeRefused(ValueError):
    """The benchmark cannot be merged without overwriting something that is not a source-text question."""


# --- filings and the lake --------------------------------------------------------------------------------------------

def parse_pair_id(pair_id: str) -> tuple[str, str, str]:
    m = PAIR_ID_RE.match(pair_id)
    if not m:
        raise ValueError(f"not a pair id (<TICKER>-<older accession>-<newer accession>): {pair_id!r}")
    return m.group(1), m.group(2), m.group(3)


def fiscal_year_of(period_end: date) -> int:
    """The fiscal year a period end belongs to: its calendar year, except a 52/53-week year that ends in the first days of January."""
    return period_end.year - 1 if period_end.month == 1 and period_end.day <= 3 else period_end.year


def xbrl_period_resolver(xbrl_dir: Path) -> Callable[[str], date | None]:
    """accession -> the latest period end among the XBRL facts that accession reported (its own fiscal year), else None."""
    frames = [pd.read_parquet(p, columns=["accn", "end"]) for p in sorted(Path(xbrl_dir).glob("*_key_metrics.parquet"))]
    if not frames:
        return lambda accession: None
    latest = pd.concat(frames).astype({"end": str}).groupby("accn")["end"].max().to_dict()
    return lambda accession: date.fromisoformat(latest[accession][:10]) if accession in latest else None


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    filed: str
    fiscal_year: int | None

    @property
    def label(self) -> str:
        return f"FY{self.fiscal_year}" if self.fiscal_year else f"annual report filed {self.filed}"

    @property
    def describe(self) -> str:
        return f"FY{self.fiscal_year} ({self.form} filed {self.filed})" if self.fiscal_year else f"the {self.form} filed {self.filed}"

    @property
    def report(self) -> str:
        """``FY2026 10-K`` or ``10-K filed 2026-02-25`` (the possessive object of a question)."""
        return f"FY{self.fiscal_year} {self.form}" if self.fiscal_year else f"{self.form} filed {self.filed}"


def between(older: Filing, newer: Filing) -> str:
    if older.fiscal_year and newer.fiscal_year:
        return f"its {older.label} and {newer.label} annual reports"
    return f"its {older.label} and its {newer.label}"


class Corpus:
    """The risk items and risk-section texts of the lake, indexed for the gold's ids."""

    def __init__(self, items: pd.DataFrame, sections: pd.DataFrame, period_end: Callable[[str], date | None]):
        self.items, self.sections, self.period_end = items, sections, period_end
        self._by_id = {r["item_id"]: r for r in items.to_dict("records")}
        self._texts: dict[str, str] = {}

    def filing(self, accession: str) -> Filing:
        rows = self.items[self.items["accession_no"] == accession]
        if rows.empty:
            raise IncompleteGold(f"no risk items for filing {accession}: the lake does not match the gold")
        end = self.period_end(accession)
        return Filing(accession, str(rows.iloc[0]["form"]), str(rows.iloc[0]["filing_date"])[:10],
                      fiscal_year_of(end) if end else None)

    def items_of(self, accession: str) -> list[dict]:
        return [r for _, r in sorted(self._by_id.items()) if r["accession_no"] == accession]

    def item(self, item_id: str) -> dict:
        if item_id not in self._by_id:
            raise IncompleteGold(f"gold item {item_id} is not in the risk-item lake")
        return self._by_id[item_id]

    def section_text(self, accession: str) -> str:
        if accession not in self._texts:
            self._texts[accession] = risk_section_text(self.items, self.sections, accession)
        return self._texts[accession]

    def filings_of(self, ticker: str) -> list[Filing]:
        accessions = self.items[self.items["ticker"] == ticker][["accession_no", "filing_date"]].drop_duplicates()
        return [self.filing(a) for a in accessions.sort_values("filing_date")["accession_no"]]


def load_corpus(items_dir: Path, sections_dir: Path, period_end: Callable[[str], date | None]) -> Corpus:
    items = pd.concat([pd.read_parquet(f) for f in sorted(Path(items_dir).glob("*_risk_items.parquet"))], ignore_index=True)
    sections = pd.concat([pd.read_parquet(f) for f in sorted(Path(sections_dir).glob("*_section_texts.parquet"))], ignore_index=True)
    return Corpus(items, sections, period_end)


# --- what the gold says about a pair ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class PairFacts:
    pair_id: str
    ticker: str
    company: str
    older: Filing
    newer: Filing
    unit: str                                 # "risk factor" or "paragraph" (20-F filers have no headlines)
    older_labels: Mapping[str, str]
    older_counts: Mapping[str, int]
    newer_counts: Mapping[str, int]
    removed: tuple
    new: tuple
    n_older: int = 0
    n_newer: int = 0
    sentences: Mapping[str, tuple | None] | None = None      # side -> the gold sentence records, None when not labelled

    @property
    def units(self) -> str:
        return self.unit + "s"


def dev_pairs(gold_doc: Mapping) -> list[str]:
    """Pair ids whose two sides are both in the gold and belong to the development split, sorted."""
    pairs = gold_doc["pairs"]
    ids = {k.rsplit("|", 1)[0] for k in pairs}
    return sorted(p for p in ids if f"{p}|older" in pairs and f"{p}|newer" in pairs and pairs[f"{p}|older"]["split"] == "development")


def _display(item: Mapping) -> str:
    text = (item.get("headline") or "").strip() or (str(item["text"]).strip()[:100] + "...")
    return text if len(text) <= HEADLINE_CHARS else text[:HEADLINE_CHARS - 3] + "..."


def _ref(item: Mapping) -> dict:
    return {"item_id": item["item_id"], "headline": _display(item), "chunk_ids": list(item["chunk_ids"])}


def pair_facts(gold_doc: Mapping, corpus: Corpus, pair_id: str) -> PairFacts:
    ticker, older_acc, newer_acc = parse_pair_id(pair_id)
    older_labels = gold_doc["pairs"][f"{pair_id}|older"]["labels"]
    newer_labels = gold_doc["pairs"][f"{pair_id}|newer"]["labels"]
    older_items, newer_items = corpus.items_of(older_acc), corpus.items_of(newer_acc)
    for side, labels, items in (("older", older_labels, older_items), ("newer", newer_labels, newer_items)):
        if {i["item_id"] for i in items} != set(labels):
            raise IncompleteGold(f"{pair_id}: {len(labels)} of {len(items)} {side}-side items are labelled (a development pair "
                                 f"must be fully labelled; {ticker} is not usable until it is)")
    paragraph = all(i["unit_kind"] == "paragraph" for i in older_items + newer_items)
    by_id = {i["item_id"]: i for i in older_items + newer_items}
    removed = tuple(_ref(by_id[i]) for i in sorted(older_labels) if older_labels[i] == "removed")
    new = tuple(_ref(by_id[i]) for i in sorted(newer_labels) if newer_labels[i] == "new")
    sentences = {side: sentence_records(gold_doc, corpus, pair_id, side) for side in ("older", "newer")}
    return PairFacts(pair_id, ticker, FILERS[ticker][0], corpus.filing(older_acc), corpus.filing(newer_acc),
                     "paragraph" if paragraph else "risk factor", dict(older_labels), Counter(older_labels.values()),
                     Counter(newer_labels.values()), removed, new, len(older_items), len(newer_items), sentences)


def sentence_records(gold_doc: Mapping, corpus: Corpus, pair_id: str, side: str) -> tuple | None:
    """The gold's labelled sentences of one side with their text (cut from that side's section text by the recorded span),
    in sentence-id order; None when the gold has no sentence labels for this pair side."""
    entry = (gold_doc.get("sentences") or {}).get(f"{pair_id}|{side}")
    if not entry:
        return None
    text = corpus.section_text(parse_pair_id(pair_id)[1 if side == "older" else 2])
    out = []
    for sid in sorted(entry["labels"]):
        item_id, start, end = entry["spans"][sid]
        out.append({"sentence_id": sid, "item_id": item_id, "label": entry["labels"][sid], "text": text[start:end]})
    return tuple(out)


# --- notes ---------------------------------------------------------------------------------------------------------------

def _sha8(gold_doc: Mapping) -> str:
    """The gold's short hash for the notes; a gold that is not frozen yet (only the interim legacy notes, written before the
    freeze, are built from one; the CLI itself refuses it) says so."""
    return str(gold_doc["sha256"])[:8] if gold_doc.get("sha256") else "pending, gold not yet frozen"


def _chunks(ids: Sequence[str]) -> str:
    shown = ", ".join(ids[:MAX_CHUNKS_SHOWN])
    return shown + (f", +{len(ids) - MAX_CHUNKS_SHOWN} more" if len(ids) > MAX_CHUNKS_SHOWN else "")


def _listing(refs: Sequence[Mapping], side: str) -> str:
    shown = [f"'{r['headline']}' ({side} chunks {_chunks(r['chunk_ids'])})" for r in refs[:MAX_LISTED]]
    return "; ".join(shown) + (f"; and {len(refs) - MAX_LISTED} more" if len(refs) > MAX_LISTED else "")


def _removed_sentence(f: PairFacts) -> str:
    if not f.removed:
        return f"NO whole {f.unit} was removed."
    n = len(f.removed)
    return f"Removed: exactly {n} {f.unit}{'' if n == 1 else 's'}: {_listing(f.removed, f.older.label)}."


def _new_sentence(f: PairFacts) -> str:
    if not f.new:
        return f"No {f.unit} is new."
    return f"{len(f.new)} new: {_listing(f.new, f.newer.label)}."


def _counts_sentence(f: PairFacts) -> str:
    o, n = f.older_counts, f.newer_counts
    return (f"Older filing, {sum(o.values())} {f.units} labelled: {o['unchanged']} unchanged, {o['reworded']} reworded, "
            f"{o['merged']} merged into another {f.unit}, {o['removed']} removed. Newer filing, {sum(n.values())} labelled: "
            f"{n['carried']} carried over from the older filing, {n['new']} new.")


def _quote(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SENTENCE_QUOTE_CHARS else text[:SENTENCE_QUOTE_CHARS - 3] + "..."


def sentence_summary(records: Sequence[Mapping], side: str, *, total_items: int, unit: str, priority: Sequence[str] = ()) -> str:
    """One side's sentence-level gold as text: how many sentences of how many sampled items were labelled removed (older side) /
    added (newer side), reworded or present, and up to ``SENTENCES_LISTED`` of the removed / added ones quoted (a sentence that
    contains a ``priority`` needle first, then sentence-id order), with the count of the rest."""
    positive = "removed" if side == "older" else "added"
    counts = Counter(r["label"] for r in records)
    items = len({r["item_id"] for r in records})
    head = (f"{side.capitalize()} filing sentences (a seeded SAMPLE of {items} of {total_items} {unit}s, {len(records)} sentences "
            f"labelled): {counts[positive]} {positive}, {counts['reworded']} reworded, {counts['present']} present.")
    hits = sorted((r for r in records if r["label"] == positive), key=lambda r: (
        not any(normalize(n) in normalize(r["text"]) for n in priority), r["sentence_id"]))
    if not hits:
        return f"{head} No sentence was labelled {positive}."
    shown = "; ".join(f"\"{_quote(r['text'])}\"" for r in hits[:SENTENCES_LISTED])
    more = f"; and {len(hits) - SENTENCES_LISTED} more" if len(hits) > SENTENCES_LISTED else ""
    return f"{head} {positive.capitalize()}: {shown}{more}."


def _sentence_line(f: PairFacts, priority: Sequence[str]) -> str:
    sides = [(side, f.sentences.get(side) if f.sentences else None) for side in ("older", "newer")]
    if not any(records for _, records in sides):
        return "Sentence level: not labelled for this pair, so no claim about an individual sentence is verified."
    parts = [sentence_summary(records, side, total_items=f.n_older if side == "older" else f.n_newer, unit=f.unit,
                              priority=priority if side == "older" else ())
             for side, records in sides if records]
    return "Sentence level: " + " ".join(parts) + " Claims about sentences outside this sample are not verified by this note."


def pair_summary(f: PairFacts, priority: Sequence[str] = ()) -> str:
    """The verified facts of one pair (the body of a note; ``Correct answer`` lines are added by the callers): item level, then
    the sentence-level gold with its sample size, so a claim about a single sentence can be checked."""
    return (f"{f.company} {f.older.describe} vs {f.newer.describe}, risk section only. {_counts_sentence(f)} "
            f"{_removed_sentence(f)} {_new_sentence(f)} {_sentence_line(f, priority)}")


_CAUTION = ("A sentence inside a surviving {unit} can be removed without the {unit} being removed: never describe a surviving or "
            "reworded {unit} as dropped.")


def removed_question(f: PairFacts, qid: str, sha: str, priority: Sequence[str] = ()) -> dict:
    answer = "no" if not f.removed else "yes"
    verdict = (f"Correct answer: NO {f.unit} was removed between these two reports." if not f.removed else
               f"Correct answer: YES, exactly {len(f.removed)} {f.unit}{'' if len(f.removed) == 1 else 's'} (the ones listed) and no other.")
    notes = (f"VERIFIED against the full text of both filings (source-text gold sha256 {sha}). {pair_summary(f, priority)} "
             f"{_CAUTION.format(unit=f.unit)} Only this consecutive pair was labelled; other years are not covered. {verdict}")
    return {"id": qid, "type": "temporal", "q": f"Did {f.company} remove any risk factors between {between(f.older, f.newer)}?",
            "judge_notes": notes, "gold": "source_text", "subtype": "removed_any", "pair_id": f.pair_id, "ticker": f.ticker,
            "verified_answer": answer, "facts": {"removed": list(f.removed), "new": list(f.new),
                                                 "older_counts": dict(f.older_counts), "newer_counts": dict(f.newer_counts)}}


_MEANING = {"unchanged": "the same text is in the newer filing", "reworded": "the same risk is disclosed in edited wording"}


def _stop_candidates(f: PairFacts, corpus: Corpus) -> list[dict]:
    out = []
    for item_id, label in sorted(f.older_labels.items()):
        item = corpus.item(item_id)
        if label in PRESENT_LABELS and item["unit_kind"] == "headline" and (item.get("headline") or "").strip():
            out.append({**_ref(item), "headline": item["headline"].strip(), "label": label})     # the FULL headline: it is the question
    return out


def stop_question(f: PairFacts, pick: Mapping, qid: str, sha: str) -> dict:
    notes = (f"VERIFIED against the full text of both filings (source-text gold sha256 {sha}). Correct answer: NO, {f.company} did "
             f"not stop disclosing this risk. The {f.unit} \"{pick['headline']}\" ({f.older.describe}, chunks {_chunks(pick['chunk_ids'])}) "
             f"is still disclosed in {f.newer.describe}: its gold label is '{pick['label']}' ({_MEANING[pick['label']]}); a labeller "
             f"quoted the matching newer text and the quote was machine-checked. The location of that newer text is not recorded in the "
             f"gold. An answer that says the risk was dropped, removed or is no longer disclosed is incorrect; a reworded {f.unit} is "
             f"still disclosed.")
    return {"id": qid, "type": "temporal",
            "q": f"Did {f.company} stop disclosing its risk about \"{pick['headline']}\" between {between(f.older, f.newer)}?",
            "judge_notes": notes, "gold": "source_text", "subtype": "stop_disclosing", "pair_id": f.pair_id, "ticker": f.ticker,
            "verified_answer": "no", "facts": {"item_id": pick["item_id"], "headline": pick["headline"], "label": pick["label"],
                                               "older_chunk_ids": pick["chunk_ids"], "newer_location": "not recorded in the gold"}}


def choose_stops(facts: Mapping[str, PairFacts], corpus: Corpus, seed: int, k: int = STOP_PAIRS) -> list[tuple[PairFacts, dict]]:
    eligible = {pid: c for pid in sorted(facts) if (c := _stop_candidates(facts[pid], corpus))}
    chosen = sorted(random.Random(f"{seed}|stop-pairs").sample(sorted(eligible), min(k, len(eligible))))
    picks = []
    for pid in chosen:
        pool = [c for c in eligible[pid] if c["label"] == "reworded"] or eligible[pid]
        picks.append((facts[pid], random.Random(f"{seed}|stop|{pid}").choice(pool)))
    return picks


# --- flagship passages -----------------------------------------------------------------------------------------------

def _gold_sentences(gold_doc: Mapping, corpus: Corpus, pair_id: str) -> list[dict]:
    entry = (gold_doc.get("sentences") or {}).get(f"{pair_id}|older")
    if not entry:
        raise FlagshipMismatch(f"the gold has no older-side sentence labels for {pair_id}")
    text = corpus.section_text(parse_pair_id(pair_id)[1])
    out = []
    for sid in sorted(entry["labels"]):
        item_id, start, end = entry["spans"][sid]
        out.append({"sentence_id": sid, "item_id": item_id, "label": entry["labels"][sid], "text": text[start:end]})
    return out


def _find(sentences: Sequence[Mapping], needle: str) -> list[dict]:
    return [s for s in sentences if normalize(needle) in normalize(s["text"])]


def _clip(text: str) -> str:
    return text if len(text) <= QUOTE_CHARS else text[:QUOTE_CHARS - 3] + "..."


def passage_question(gold_doc: Mapping, corpus: Corpus, spec: Mapping, f: PairFacts, qid: str, sha: str) -> dict:
    sentences = _gold_sentences(gold_doc, corpus, f.pair_id)
    matches = _find(sentences, spec["needle"])
    if not matches:
        raise FlagshipMismatch(f"{f.pair_id}: no labelled sentence contains {spec['needle']!r}; the flagship question cannot be "
                               f"verified from the gold")
    wrong = [f"{s['sentence_id']} ({s['label']})" for s in matches if s["label"] != "removed"]
    if wrong:
        raise FlagshipMismatch(f"{f.pair_id}: the gold does not label every sentence containing {spec['needle']!r} removed: "
                               f"{', '.join(wrong)}")
    item_label = f.older_labels.get(matches[0]["item_id"])
    if item_label not in ("unchanged", "reworded", "merged"):
        raise FlagshipMismatch(f"{f.pair_id}: the risk factor containing {spec['needle']!r} has item-level gold label "
                               f"{item_label!r}, not a surviving one")
    item = corpus.item(matches[0]["item_id"])
    quotes = " ".join(f"\"{_clip(s['text'])}\"" for s in matches[:2])
    context = ""
    if spec.get("context_needle"):
        around = _find(sentences, spec["context_needle"])
        removed = sum(1 for s in around if s["label"] == "removed")
        n = len(around)
        context = (f" {n} labelled sentence{' mentions' if n == 1 else 's mention'} '{spec['context_needle']}' ({removed} removed, "
                   f"{n - removed} still present): only the removed ones are gone from {f.newer.describe}.")
    notes = (f"VERIFIED against the full text of both filings (source-text gold sha256 {sha}). Correct answer: NO. This statement of "
             f"{spec['company']}'s {f.older.describe} risk section {quotes} does NOT appear, verbatim or reworded, anywhere in the "
             f"{f.newer.describe} risk section (gold sentence label: removed; sentence id {matches[0]['sentence_id']}; machine-checked "
             f"against the whole newer section).{context} The {f.unit} containing it (\"{_display(item)}\", {item['item_id']}) SURVIVES "
             f"in {f.newer.label} (item-level gold label: {item_label}): this is a removed sentence, not a removed {f.unit}. "
             f"An answer that says {f.newer.label} still contains the statement is incorrect, and so is one that says the whole "
             f"{f.unit} was dropped.")
    return {"id": qid, "type": "temporal",
            "q": f"Does {spec['company']}'s {f.newer.report} still say that {spec['claim']}, or was that statement removed?",
            "judge_notes": notes, "gold": "source_text", "subtype": "passage", "pair_id": f.pair_id, "ticker": f.ticker,
            "verified_answer": "no", "facts": {"key": spec["key"], "needle": spec["needle"],
                                               "sentence_ids": [s["sentence_id"] for s in matches],
                                               "labels": [s["label"] for s in matches], "item_id": item["item_id"],
                                               "item_label": item_label}}


def build_questions(gold_doc: Mapping, corpus: Corpus, *, seed: int = SEED, flagship_pair: str = FLAGSHIP_PAIR) -> list[dict]:
    """The 12 questions, ids T4.. in the order removed_any (by pair id), stop_disclosing, passages."""
    sha = _sha8(gold_doc)
    pairs = dev_pairs(gold_doc)
    if flagship_pair not in pairs:
        raise FlagshipMismatch(f"the flagship pair {flagship_pair} is not a fully labelled development pair of this gold")
    facts = {pid: pair_facts(gold_doc, corpus, pid) for pid in pairs}
    counter = iter(range(FIRST_ID, 10_000))

    def next_id() -> str:
        return f"T{next(counter)}"

    questions = [removed_question(facts[pid], next_id(), sha) for pid in pairs]
    questions += [stop_question(f, pick, next_id(), sha) for f, pick in choose_stops(facts, corpus, seed)]
    questions += [passage_question(gold_doc, corpus, spec, facts[flagship_pair], next_id(), sha) for spec in FLAGSHIPS]
    return questions


# --- the legacy T1 / T2 / T3 notes -----------------------------------------------------------------------------------------

_LEGACY_RULES = ("Correct answer: reports at most the removals and additions verified above, describes removed sentences as "
                 "sentences (never as dropped risk factors), and treats any other claim that a risk factor was dropped, removed, added "
                 "or new as unsupported and incorrect.")


def legacy_notes(gold_doc: Mapping, corpus: Corpus, ticker: str, *, flagship_pair: str = FLAGSHIP_PAIR) -> str:
    """Gold-derived replacement for the judge notes of a legacy temporal question about ``ticker``: EVERY fully labelled
    development pair of the company (a one-pair note would be cherry-picked), the flagship passages, and what is not covered."""
    sha = _sha8(gold_doc)
    mine = [pair_facts(gold_doc, corpus, pid) for pid in dev_pairs(gold_doc) if parse_pair_id(pid)[0] == ticker]
    if not mine:
        raise IncompleteGold(f"the gold has no fully labelled development pair for {ticker}")
    mine.sort(key=lambda f: f.newer.filed)
    parts = [f"VERIFIED against the full text of the filings (source-text gold sha256 {sha})."]
    parts += [pair_summary(f) for f in mine]
    if flagship_pair in {f.pair_id for f in mine}:
        f = next(f for f in mine if f.pair_id == flagship_pair)
        sentences = _gold_sentences(gold_doc, corpus, flagship_pair)
        gone = []
        for spec in FLAGSHIPS:
            hits = _find(sentences, spec["needle"])
            if not hits or any(s["label"] != "removed" for s in hits):
                raise FlagshipMismatch(f"{flagship_pair}: the gold does not label {spec['needle']!r} removed")
            gone.append(f"\"{_clip(hits[0]['text'])}\"")
        parts.append(f"Sentence level ({f.older.label} -> {f.newer.label}): these statements were removed although the {f.unit} "
                     f"containing them survives: {' '.join(gone)}")
    covered = {(f.older.accession, f.newer.accession) for f in mine}
    filings = corpus.filings_of(ticker)
    uncovered = [f"{a.label} -> {b.label}" for a, b in zip(filings, filings[1:]) if (a.accession, b.accession) not in covered]
    parts.append("These consecutive pairs are not in the gold, so nothing is verified about them: " + ", ".join(uncovered) + "."
                 if uncovered else "Every consecutive pair of this company in the corpus is covered above.")
    parts.append(_LEGACY_RULES)
    return " ".join(parts)


# --- merging into the benchmark ----------------------------------------------------------------------------------------------

def split_entries(text: str) -> tuple[list[tuple[dict, str]], str, str]:
    """(entries as (object, raw text), the line ending, whatever follows the closing bracket) of a benchmark file, refusing a
    layout that would not round-trip byte for byte."""
    eol = "\r\n" if "\r\n" in text else "\n"
    decoder, i, entries = json.JSONDecoder(), text.index("[") + 1, []
    while True:
        while text[i] in " \t\r\n,":
            i += 1
        if text[i] == "]":
            break
        obj, j = decoder.raw_decode(text, i)
        entries.append((obj, text[i:j]))
        i = j
    tail = text[i + 1:]
    if _assemble(entries, eol, tail) != text:
        raise MergeRefused("the benchmark file's layout is not the expected two-space-indented JSON list; refusing to rewrite it")
    return entries, eol, tail


def _assemble(entries: Sequence[tuple[dict, str]], eol: str, tail: str) -> str:
    return "[" + eol + ("," + eol).join("  " + raw for _, raw in entries) + eol + "]" + tail


def _raw(obj: Mapping, eol: str) -> str:
    return json.dumps(obj, indent=2).replace("\n", eol + "  ")      # default ensure_ascii, like the existing entries


def merge_text(text: str, questions: Sequence[Mapping], legacy: Mapping[str, str], source: str) -> str:
    """The benchmark text with the questions appended (or replaced in place when they are source-text questions already) and the
    legacy notes refreshed. Untouched entries keep their exact bytes."""
    entries, eol, tail = split_entries(text)
    index = {obj["id"]: pos for pos, (obj, _) in enumerate(entries)}
    for q in questions:
        old = entries[index[q["id"]]][0] if q["id"] in index else None
        if old is not None and old.get("gold") != "source_text":
            raise MergeRefused(f"{source}: id {q['id']} already exists and is not a source-text question; refusing to overwrite it")
    for q in questions:
        pos = index.get(q["id"])
        if pos is None:
            index[q["id"]] = len(entries)
            entries.append((dict(q), _raw(q, eol)))
        elif entries[pos][0] != dict(q):
            entries[pos] = (dict(q), _raw(q, eol))
    for qid, notes in legacy.items():
        if qid in index and entries[index[qid]][0].get("judge_notes") != notes:
            obj = {**entries[index[qid]][0], "judge_notes": notes}
            entries[index[qid]] = (obj, _raw(obj, eol))
    return _assemble(entries, eol, tail)


def merge_benchmarks(paths: Sequence[Path], questions: Sequence[Mapping], legacy: Mapping[str, str]) -> list[Path]:
    """Merge into every copy, or into none: all outputs are computed (and required to be identical) before anything is written."""
    texts = {p: p.read_bytes().decode("utf-8") for p in paths}
    if len(set(texts.values())) != 1:
        raise MergeRefused("the benchmark copies differ before the merge: " + ", ".join(str(p) for p in paths))
    merged = {p: merge_text(t, questions, legacy, str(p)) for p, t in texts.items()}
    for p, new in merged.items():
        if new != texts[p]:
            p.write_bytes(new.encode("utf-8"))
    return list(paths)


# --- command line --------------------------------------------------------------------------------------------------------------

def _fail(message: object) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    ap.add_argument("--items-dir", type=Path, default=DEFAULT_ITEMS)
    ap.add_argument("--sections-dir", type=Path, default=DEFAULT_SECTIONS)
    ap.add_argument("--xbrl-dir", type=Path, default=DEFAULT_XBRL, help="key_metrics parquets: an accession's latest period end gives its fiscal year")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--flagship-pair", default=FLAGSHIP_PAIR)
    ap.add_argument("--dry-run", action="store_true", help="print the summary, write nothing")
    ap.add_argument("--merge", action="store_true", help="append T4.. to the benchmark copies and refresh the legacy T1-T3 notes")
    ap.add_argument("--benchmark", type=Path, action="append", help="benchmark copy to merge into (repeatable; default: both copies)")
    args = ap.parse_args(argv)
    if not args.gold.exists() or not gold_mod.verify_frozen(args.gold):
        return _fail(f"{args.gold} is missing or not frozen (its sha256 is absent or does not match its content): questions are "
                     f"only built from a frozen gold (scripts/label_risk_items.py freeze)")
    doc = json.loads(args.gold.read_text(encoding="utf-8"))
    try:
        corpus = load_corpus(args.items_dir, args.sections_dir, xbrl_period_resolver(args.xbrl_dir))
        questions = build_questions(doc, corpus, seed=args.seed, flagship_pair=args.flagship_pair)
        legacy = {qid: legacy_notes(doc, corpus, ticker, flagship_pair=args.flagship_pair) for qid, ticker in LEGACY_TICKER.items()}
    except (IncompleteGold, FlagshipMismatch, KeyError, ValueError) as err:
        return _fail(err.args[0] if err.args else err)
    print(f"{len(questions)} questions from the gold {doc['sha256'][:8]}: "
          + ", ".join(f"{k} x {v}" for k, v in Counter(q["subtype"] for q in questions).items()))
    if args.dry_run:
        return 0
    document = {"kind": "temporal_questions", "gold_sha256": doc["sha256"], "seed": args.seed,
                "generator": "scripts/build_temporal_questions.py", "legacy_notes": legacy, "questions": questions}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    if args.merge:
        try:
            written = merge_benchmarks(args.benchmark or list(DEFAULT_BENCHMARKS), questions, legacy)
        except MergeRefused as err:
            return _fail(err.args[0])
        print("merged into " + ", ".join(str(p) for p in written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
