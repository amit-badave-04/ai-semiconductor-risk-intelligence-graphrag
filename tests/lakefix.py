"""A tiny synthetic data lake for the align-items / item-loader tests (imported by tests, not collected).

One ticker ``ZZZ`` (cik 999) with three annual filings whose risk sections hold hand-written items:

    F24: wafer, tax, pandemic
    F25: wafer (one sentence replaced), tax (unchanged), commercial (new)         pandemic REMOVED
    F26: wafer (unchanged), tax (unchanged), commercial (unchanged), AI (new)

The lake mirrors the real one: ``risk_items/<T>_risk_items.parquet`` (+ quality sidecar), ``section_texts/<T>_section_texts.parquet``
(several sections per accession, the risk section is the one the items carry), ``chunks/<T>_chunks.parquet`` (one chunk per item,
section-text offsets) and a manifest.
"""

import json
from pathlib import Path

import pandas as pd

from semigraph.config import Settings
from semigraph.hashing import content_hash

WAFER24 = ("Our wafer supply depends on a single foundry in Taiwan. Any disruption at that foundry would delay shipments to our "
           "largest customers for several quarters. We have no long-term capacity guarantees from that foundry and pricing may rise. "
           "Our foundry contract expires within two years and renewal terms are highly uncertain today.")
WAFER25 = ("Our wafer supply depends on a single foundry in Taiwan. Any disruption at that foundry would delay shipments to our "
           "largest customers for several quarters. We have no long-term capacity guarantees from the foundry and its pricing might "
           "increase. Capacity allocation is not guaranteed by contract and prices can change at short notice.")
TAX = ("Changes in tax law in the jurisdictions where we operate could raise our effective tax rate. Audits by tax authorities may "
       "also result in additional payments and penalties. Our tax positions rely on complex interpretations of local rules.")
PANDEMIC = ("The pandemic has interrupted our operations and our suppliers in unforeseen ways. Public health measures could again "
            "restrict travel and factory output. Recovery timing remains highly uncertain for our industry and customers.")
COMMERCIAL = ("Commercial arrangements expose us to counterparty risks. We enter agreements with strategic partners to secure future "
              "capacity. If a counterparty fails to perform, our revenue commitments could be delayed materially.")
AI = ("Sovereign artificial intelligence programs may not materialize as expected. Governments could postpone purchases of "
      "accelerated computing systems. Such delays would reduce our data center revenue in the affected fiscal quarters.")

CIK = 999
ACC = {"24": "0000999-24-000001", "25": "0000999-25-000001", "26": "0000999-26-000001"}
DATES = {"24": "2024-02-21", "25": "2025-02-26", "26": "2026-02-25"}
FILINGS = {"24": [WAFER24, TAX, PANDEMIC], "25": [WAFER25, TAX, COMMERCIAL], "26": [WAFER25, TAX, COMMERCIAL, AI]}
GOOD = {"low_coverage": False, "section_suspect": False, "coverage": 0.97}


def headline(text: str) -> str:
    return text.split(". ")[0] + "."


def build_filing(year: str, ticker: str = "ZZZ", cik: int = CIK, acc: str | None = None) -> tuple[list[dict], str, list[dict]]:
    """(item rows, risk section text, chunk rows) of one filing; one chunk per item, offsets are section-text offsets."""
    acc = acc or ACC[year]
    items, chunks, text, pos = [], [], "", 0
    for seq, body in enumerate(FILINGS[year]):
        text += ("\n" if text else "") + body
        start = len(text) - len(body)
        chunk_id = f"{acc}:I.1A:{seq:04d}"
        items.append({"item_id": f"{acc}:I.1A:i{seq:03d}", "accession_no": acc, "ticker": ticker, "filer_cik": cik, "form": "10-K",
                      "filing_date": DATES[year], "section_id": "I.1A", "seq": seq, "headline": headline(body), "text": body,
                      "text_hash": content_hash(body), "char_start": start, "char_end": start + len(body),
                      "unit_kind": "headline", "chunk_ids": [chunk_id]})
        chunks.append({"chunk_id": chunk_id, "accession_no": acc, "section_id": "I.1A", "char_start": start,
                       "char_end": start + len(body), "cik": cik, "ticker": ticker, "form": "10-K", "filing_date": DATES[year],
                       "text": body, "kind": "prose", "sub_heading": None, "n_tokens": len(body.split()), "source_url": "u",
                       "section_title": "Item 1A"})
    return items, text, chunks


def build_lake(root: Path, *, ticker: str = "ZZZ", cik: int = CIK, quality: dict | None = None, years=("24", "25", "26")) -> Settings:
    """Write the lake under ``root/data`` and return Settings pointing at it. ``quality``: accession -> overrides."""
    settings = Settings(data_dir=root / "data", _env_file=None)
    items, sections, chunks, records = [], [], [], []
    for year in years:
        acc = f"{ticker}{ACC[year]}" if ticker != "ZZZ" else ACC[year]
        i, text, c = build_filing(year, ticker, cik, acc)
        items += i
        chunks += c
        sections += [{"accession_no": acc, "section_id": "I.1", "form": "10-K", "filing_date": DATES[year],
                      "section_title": "Item 1", "text": "Business text.", "n_chars": 14},
                     {"accession_no": acc, "section_id": "I.1A", "form": "10-K", "filing_date": DATES[year],
                      "section_title": "Item 1A", "text": text, "n_chars": len(text)}]
        records.append({"accession_no": acc, "form": "10-K", "filing_date": DATES[year], "section_id": "I.1A",
                        "n_items": len(i), **GOOD, "method": "bold", "section_chars": len(text), "notes": [],
                        **(quality or {}).get(year, {})})
    for sub, name, frame in (("interim/risk_items", f"{ticker}_risk_items.parquet", pd.DataFrame(items)),
                             ("interim/section_texts", f"{ticker}_section_texts.parquet", pd.DataFrame(sections)),
                             ("processed/chunks", f"{ticker}_chunks.parquet", pd.DataFrame(chunks))):
        target = root / "data" / sub
        target.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(target / name, index=False)
    (root / "data" / "interim" / "risk_items" / f"{ticker}_risk_items_quality.json").write_text(
        json.dumps({"ticker": ticker, "filings": records}), encoding="utf-8")
    return settings
