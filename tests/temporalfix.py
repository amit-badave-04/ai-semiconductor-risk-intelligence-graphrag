"""A small synthetic source-text gold + lake for the temporal-eval script tests (imported by tests, not collected).

Six development pairs as in the real gold: NVDA FY25->FY26 (the flagship: no item removed, one new item, three removed
SENTENCES inside a surviving export-control risk factor), NVDA FY24->FY25 (one item removed), AMD, META, MU (headline items) and
TSM (paragraph units, no headlines). Every older item is labelled (fully labelled pairs) and every filing has a risk section
whose text is the items joined by newlines, so ``section_text[char_start:char_end] == item text`` exactly like the real lake.
"""

import json
from datetime import date
from pathlib import Path

import pandas as pd

from semigraph.eval import gold

FLAG = "NVDA-0001045810-25-000023-0001045810-26-000021"
PRIOR = "NVDA-0001045810-24-000029-0001045810-25-000023"
AMD = "AMD-0000002488-25-000012-0000002488-26-000018"
META = "META-0001326801-25-000017-0001628280-26-003942"
MU = "MU-0000723125-24-000027-0000723125-25-000028"
TSM = "TSM-0001193125-25-083423-0001628280-26-025362"
PAIRS = (FLAG, PRIOR, AMD, META, MU, TSM)

NAC = "For example, the Notified Advanced Computing, or NAC, process has not resulted in approvals for exports of products to customers in China."
HK = "Following these 2022 export controls, we transitioned some operations out of China and Hong Kong for testing and distribution."
UVEU = "The AI Diffusion IFR would confer special benefits on select Universal Verified End Users, or UVEU, and lesser benefits on others."
KEPT = "The AI Diffusion IFR would divide the world into three tiers, relegating most countries to a second tier of access."
EXPORT_BODY = "We are subject to complex export laws and political actions. " + " ".join([KEPT, NAC, HK, UVEU])
ADDED = "New licensing requirements apply to our H20 products after April 2025 according to the rule."
EXPORT_BODY_NEWER = "We are subject to complex export laws and political actions. " + KEPT + " " + ADDED    # three sentences gone, one new
NEW_ITEM = "Commercial arrangements expose us to counterparty risks. If a counterparty fails to perform our commitments could be delayed."

EXTRA = "0001045810-23-000017"      # NVDA FY2023: in the corpus (so FY23->FY24 exists) but in no gold pair
PERIOD_ENDS = {EXTRA: date(2023, 1, 29), "0001045810-24-000029": date(2024, 1, 28), "0001045810-25-000023": date(2025, 1, 26),
               "0001045810-26-000021": date(2026, 1, 25), "0000002488-25-000012": date(2024, 12, 28),
               "0000002488-26-000018": date(2025, 12, 27), "0001326801-25-000017": date(2024, 12, 31),
               "0001628280-26-003942": date(2025, 12, 31), "0000723125-24-000027": date(2024, 8, 29),
               "0000723125-25-000028": date(2025, 8, 28), "0001193125-25-083423": date(2024, 12, 31),
               "0001628280-26-025362": date(2025, 12, 31)}
FILED = {EXTRA: "2023-02-24", "0001045810-24-000029": "2024-02-21", "0001045810-25-000023": "2025-02-26", "0001045810-26-000021": "2026-02-25",
         "0000002488-25-000012": "2025-02-05", "0000002488-26-000018": "2026-02-04", "0001326801-25-000017": "2025-01-30",
         "0001628280-26-003942": "2026-01-29", "0000723125-24-000027": "2024-10-03", "0000723125-25-000028": "2025-10-02",
         "0001193125-25-083423": "2025-04-17", "0001628280-26-025362": "2026-04-16"}


def period_end(accession: str):
    return PERIOD_ENDS.get(accession)


def _bodies(tag: str, n: int) -> list[tuple[str, str]]:
    """(headline, text) of ``n`` generic risk items of filing ``tag``."""
    return [(f"{tag} risk number {i} may harm our business.",
             f"{tag} risk number {i} may harm our business. First supporting sentence of risk {i} in filing {tag}. "
             f"Second supporting sentence of risk {i} in filing {tag}, with enough words to be labelled.") for i in range(n)]


def _filing(acc: str, ticker: str, form: str, items: list[tuple[str, str]], *, paragraph: bool = False):
    text, rows = "", []
    for seq, (headline, body) in enumerate(items):
        text += ("\n" if text else "") + body
        start = len(text) - len(body)
        rows.append({"item_id": f"{acc}:I.1A:i{seq:03d}", "accession_no": acc, "ticker": ticker, "filer_cik": 1, "form": form,
                     "filing_date": FILED[acc], "section_id": "I.3" if form == "20-F" else "I.1A", "seq": seq,
                     "headline": "" if paragraph else headline, "text": body, "text_hash": f"h{acc}{seq}", "char_start": start,
                     "char_end": start + len(body), "unit_kind": "paragraph" if paragraph else "headline",
                     "chunk_ids": [f"{acc}:I.1A:{seq:04d}", f"{acc}:I.1A:{seq + 100:04d}"]})
    return rows, text


def build(tmp: Path) -> dict:
    """Write items/sections parquets and a FROZEN gold under ``tmp``; return the paths and the pair layout."""
    items_dir, sections_dir = tmp / "risk_items", tmp / "section_texts"
    items_dir.mkdir(), sections_dir.mkdir()
    all_items, all_sections, pairs, sentences = [], [], {}, {}
    for pid in PAIRS:
        ticker, *_ = pid.split("-")
        older_acc, newer_acc = pid[len(ticker) + 1:][:20], pid[len(ticker) + 1:][21:]
        form = "20-F" if ticker == "TSM" else "10-K"
        paragraph = ticker == "TSM"
        older_items, newer_items = _bodies(older_acc[-6:], 4), _bodies(newer_acc[-6:], 4)
        older_labels = {}
        if pid == FLAG:
            older_items[0] = ("We are subject to complex export laws and political actions.", EXPORT_BODY)
            newer_items[0] = (older_items[0][0], EXPORT_BODY_NEWER)
            newer_items[3] = ("Commercial arrangements expose us to counterparty risks.", NEW_ITEM)
        o_rows, o_text = _filing(older_acc, ticker, form, older_items, paragraph=paragraph)
        n_rows, n_text = _filing(newer_acc, ticker, form, newer_items, paragraph=paragraph)
        all_items += o_rows + n_rows
        for acc, rows, text, fm in ((older_acc, o_rows, o_text, form), (newer_acc, n_rows, n_text, form)):
            sid = "I.3" if fm == "20-F" else "I.1A"
            all_sections += [{"accession_no": acc, "section_id": "I.1", "form": fm, "filing_date": FILED[acc], "section_title": "Item 1",
                              "text": "Business.", "n_chars": 9}, {"accession_no": acc, "section_id": sid, "form": fm,
                                                                   "filing_date": FILED[acc], "section_title": "Risk", "text": text,
                                                                   "n_chars": len(text)}]
        for i, r in enumerate(o_rows):
            older_labels[r["item_id"]] = "unchanged" if i == 1 else "reworded"
        newer_labels = {r["item_id"]: "carried" for r in n_rows}
        if pid == PRIOR:
            older_labels[o_rows[2]["item_id"]] = "removed"            # the one gold-removed item
        if pid == FLAG:
            newer_labels[n_rows[3]["item_id"]] = "new"
        pairs[f"{pid}|older"] = {"split": "development", "labels": older_labels, "alpha": 0.9, "pairwise_agreement": 0.95}
        pairs[f"{pid}|newer"] = {"split": "development", "labels": newer_labels, "alpha": 0.9, "pairwise_agreement": 0.95}
        if pid == FLAG:
            sentences[f"{pid}|older"] = _flag_sentences(o_rows[0], o_text)
            sentences[f"{pid}|newer"] = _flag_added(n_rows[0], n_text)
    extra_rows, extra_text = _filing(EXTRA, "NVDA", "10-K", _bodies("000017", 4))
    all_items += extra_rows
    all_sections.append({"accession_no": EXTRA, "section_id": "I.1A", "form": "10-K", "filing_date": FILED[EXTRA],
                         "section_title": "Risk", "text": extra_text, "n_chars": len(extra_text)})
    doc = {"kind": "risk_items_gold", "pairs": pairs, "sentences": sentences}
    pd.DataFrame(all_items).to_parquet(items_dir / "ZZ_risk_items.parquet")
    pd.DataFrame(all_sections).to_parquet(sections_dir / "ZZ_section_texts.parquet")
    xbrl_dir = tmp / "xbrl"
    xbrl_dir.mkdir()
    pd.DataFrame([{"accn": acc, "end": end.isoformat()} for acc, end in PERIOD_ENDS.items()]).to_parquet(
        xbrl_dir / "ZZ_key_metrics.parquet")
    frozen = tmp / "gold.json"
    gold.freeze(doc, frozen)
    return {"items_dir": items_dir, "sections_dir": sections_dir, "xbrl_dir": xbrl_dir, "gold": frozen, "doc": doc}


def _flag_sentences(item: dict, section_text: str) -> dict:
    labels, spans = {}, {}
    for n, (sentence, label) in enumerate([(KEPT, "present"), (NAC, "removed"), (HK, "removed"), (UVEU, "removed")]):
        start = section_text.index(sentence)
        sid = f"{item['item_id']}#s{n:03d}"
        labels[sid], spans[sid] = label, [item["item_id"], start, start + len(sentence)]
    return {"split": "development", "labels": labels, "spans": spans}


def _flag_added(item: dict, section_text: str) -> dict:
    """Newer-side sentence gold: the kept sentence is present, the new licensing sentence is ``added``."""
    labels, spans = {}, {}
    for n, (sentence, label) in enumerate([(KEPT, "present"), (ADDED, "added")]):
        start = section_text.index(sentence)
        sid = f"{item['item_id']}#s{n:03d}"
        labels[sid], spans[sid] = label, [item["item_id"], start, start + len(sentence)]
    return {"split": "development", "labels": labels, "spans": spans}


def rewrite_gold(path: Path, mutate) -> Path:
    """Re-freeze ``path`` after ``mutate(doc)`` (a fresh, valid hash)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.pop("sha256")
    mutate(doc)
    gold.freeze(doc, path)
    return path
