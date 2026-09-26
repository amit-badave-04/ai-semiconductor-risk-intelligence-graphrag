"""Pairing of consecutive annual filings for risk-item alignment (M1b step 4) - pure, no I/O.

Moved out of ``scripts/label_risk_items.py`` so the labelling tooling and the ``align-items`` pipeline pair filings by
EXACTLY the same rule (the script imports these two functions; its behaviour is unchanged):

* ``consecutive_pairs``: neighbouring annual filings per ticker in filing-date order (only filings that have risk items).
  With the quality sidecar (``parsing.risk_item_quality``) every pair carries ``comparable`` and, when False, the
  ``not_compared_reason``: a pair with a low-coverage or suspect side is never aligned, never labelled and never loaded as
  a comparison (alignment would read text the parser lost as removed risks: the false-drop failure).
* ``risk_section_text``: the risk-section text of one filing. The section-text lake holds several sections per accession
  (Item 1, Item 1A, Item 7, ...), so the section is the one the filing's own items say they were cut from.
"""

from collections.abc import Mapping

import pandas as pd

from ..parsing import risk_item_quality


def consecutive_pairs(items: pd.DataFrame, quality: Mapping | None = None) -> list[dict]:
    """Neighbouring annual filings per ticker in filing-date order (only filings that have risk items).

    ``items``: a frame with ``ticker``, ``accession_no`` and ``filing_date`` columns (the risk-item parquet rows).
    ``quality``: ``risk_item_quality.load_quality`` (accession -> quality record); ``None`` means every pair is
    comparable. Returned sorted by ``pair_id``."""
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


def risk_section_text(items: pd.DataFrame, sections: pd.DataFrame, accession: str) -> str:
    """The risk-section text of one filing: the section its own items carry as ``section_id``, never simply "the first"."""
    ids = items.loc[items["accession_no"] == accession, "section_id"].unique()
    if len(ids) != 1:
        raise KeyError(f"cannot tell which section holds the risk items of {accession}: item section ids {list(ids)}")
    rows = sections[(sections["accession_no"] == accession) & (sections["section_id"] == ids[0])]
    if rows.empty:
        raise KeyError(f"no section text for {accession} (section {ids[0]})")
    return str(rows.iloc[0]["text"])
