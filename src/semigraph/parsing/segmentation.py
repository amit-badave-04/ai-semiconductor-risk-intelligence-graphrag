"""Layout-aware semantic segmentation of SEC filings
(ported from notebook 03, generalized to all filers/forms in notebook 12).

sec-parser classifies every visual element of an EDGAR filing; we then
segment the element stream into SEC Items via part-aware regexes (section
ids are part-qualified — ``I.1A`` vs ``II.1A`` — so 10-Q item-number
collisions stay distinct). sec-parser 0.58 only ships ``Edgar10QParser``;
on 10-Ks it misses some ``TopSectionTitle``s, which is why we do our own
regex segmentation over the classified elements.

Battle scars preserved exactly (each cost a failed run — do not "clean up"):

- **Intel's integrated 10-K has NO inline "Item N." headings.** Section
  names ride on running page-headers like ``Risk Factors44`` (name + page
  number) — hence the page-header fallback (``segment_by_pageheaders``).
- **ASML's 20-F cover page carries a junk "Item 17 ☐ 18 ☐" checkbox
  heading**, which the item path happily turns into a (useless) section.
  The fallback must therefore trigger on "no KEEP sections found", NOT on
  "no rows found" (see ``needs_fallback``).
- **ASML's 2023/24 filings are unmarkable by either path** — they produce
  zero keep-section rows. That is by design: warn + skip, never crash the
  batch.
- **20-F filings use Items 3/4/5** (Key Information/Risk, Business,
  Operating Review). TSMC and ASML file 20-F; Samsung does not file at all
  and exists only as an entity mentioned by others.
"""

import logging
import re
from pathlib import Path

import pandas as pd

from ..config import Settings, get_settings
from ..ingestion.edgar import FILERS, load_manifest, resolve_local_path

logger = logging.getLogger("semigraph.parsing.segmentation")

ITEM_RE = re.compile(r"^item\s+(\d+[a-z]?)[\.\:\s]", re.IGNORECASE)  # \s covers 20-F thin-space
# Matches "Part II", "PART II — OTHER INFORMATION", "Part I." etc.
PART_RE = re.compile(r"^part\s+(i{1,3}|iv)\b", re.IGNORECASE)
NOISE_TYPES = {
    "IrrelevantElement", "PageHeaderElement", "PageNumberElement",
    "EmptyElement", "NotYetClassifiedElement", "ImageElement",
    "IntroductorySectionElement",
}
HEADING_TYPES = {"TitleElement", "TopSectionTitle"}

# Sections worth extracting from, per form type. 20-F item numbers verified
# against live TSMC + ASML filings (notebook 12).
KEEP_SECTIONS = {
    "10-K": ["I.1", "I.1A", "II.7"],
    "10-Q": ["I.2", "II.1A"],
    "20-F": ["I.3", "I.4", "I.5"],
}
RISK_SECTIONS = {"10-K": "I.1A", "10-Q": "II.1A", "20-F": "I.3"}

# --- Fallback for custom-layout filings with NO inline "Item N." headings
# (Intel's integrated 10-K): section names ride on running page-headers like
# "Risk Factors44" (name + page number).
PAGEHEAD_RE = re.compile(r"^([A-Za-z][A-Za-z &,’'\-]{2,60}?)\s*(\d{1,3})$")
PAGEHEAD_MAP = {
    "10-K": {
        "our business": "I.1",
        "our strategy": "I.1",
        "overview and our strategy": "I.1",
        "fundamentals of our business": "I.1",
        "risk factors": "I.1A",
        "risk factors and other key information": "I.1A",
        "md&a": "II.7",
    },
    "10-Q": {"md&a": "I.2", "risk factors": "II.1A"},
    # ASML integrated report: only its risk section is cleanly marked
    "20-F": {"risk factors": "I.3"},
}

SECTIONS_COLUMNS = [
    "element_index", "section_id", "section_title", "element_type", "text",
]


def norm_marker(text: str) -> str:
    """Normalize candidate section markers: strip trailing page digits
    (Intel 'MD&A44') and '(continued)' suffixes (ASML 'Risk factors
    (continued)')."""
    t = re.sub(r"\s*\(continued\)\s*$", "", text.strip(), flags=re.I)
    t = re.sub(r"\s*\d{1,3}$", "", t).strip()
    return t.lower()


def segment_by_items(elements) -> list[dict]:
    """Primary path: part-aware 'Item N.' heading segmentation (notebook 03)."""
    part, sid, stitle, rows = "I", None, None, []
    for idx, el in enumerate(elements):
        et, text = type(el).__name__, (el.text or "").strip()
        if not text or et in NOISE_TYPES:
            continue
        if et in HEADING_TYPES and len(text) < 200:
            if (pm := PART_RE.match(text)):
                part = pm.group(1).upper()
                continue
            if (m := ITEM_RE.match(text)):
                sid, stitle = f"{part}.{m.group(1).upper()}", text
                continue
        if sid is None:
            continue  # cover page / TOC before the first item
        rows.append(
            {
                "element_index": idx,
                "section_id": sid,
                "section_title": stitle,
                "element_type": et,
                "text": text,
            }
        )
    return rows


def segment_by_pageheaders(elements, form: str) -> list[dict]:
    """Custom-layout fallback. Section markers come in two styles:

    - Intel: running headers 'Risk Factors44' (name + page number) on any
      element type
    - ASML: 'Risk factors' heading + 'Risk factors (continued)'
      PageHeaderElements per page

    A PageHeaderElement naming a DIFFERENT section ends the current one
    (page boundary).
    """
    mapping = PAGEHEAD_MAP.get(form, {})
    sid, cur_name, rows = None, None, []
    for idx, el in enumerate(elements):
        et, text = type(el).__name__, (el.text or "").strip()
        if not text:
            continue
        if len(text) < 80:
            norm = norm_marker(text)
            is_numbered = bool(PAGEHEAD_RE.match(text))
            if norm in mapping and (
                is_numbered or et == "PageHeaderElement" or et in HEADING_TYPES
            ):
                sid, cur_name = mapping[norm], norm
                continue
            if is_numbered:  # Intel-style header for an UNMAPPED section
                sid, cur_name = None, None
                continue
            if et == "PageHeaderElement" and sid is not None and norm != cur_name:
                sid, cur_name = None, None  # ASML-style: page belongs to another section
                continue
        if sid is None or et in NOISE_TYPES:
            continue
        rows.append(
            {
                "element_index": idx,
                "section_id": sid,
                "section_title": cur_name,
                "element_type": et,
                "text": text,
            }
        )
    return rows


def needs_fallback(rows: list[dict], form: str) -> bool:
    """True when the item path found NOTHING USABLE for this form.

    Deliberately 'no KEEP sections', not 'no rows': Intel yields zero item
    headings (no rows at all), while ASML yields a junk cover-page
    'Item 17 / Item 18' checkbox section — rows exist, but none we keep.
    """
    keep = set(KEEP_SECTIONS.get(form, []))
    return not any(r["section_id"] in keep for r in rows)


def segment(html: str, form: str) -> pd.DataFrame:
    """Parse filing HTML and return one row per content element, tagged with
    its SEC section (ported from notebook 12 ``segment``).

    Tries item-heading segmentation first, then the page-header fallback.
    May return an empty frame (ASML 2023/24 are unmarkable — by design).
    """
    import sec_parser as sp  # heavy import kept local

    elements = sp.Edgar10QParser().parse(html)
    rows = segment_by_items(elements)
    if needs_fallback(rows, form):
        rows = segment_by_pageheaders(elements, form)
    return pd.DataFrame(rows, columns=SECTIONS_COLUMNS)


def sections_dir(settings: Settings) -> Path:
    return settings.interim_dir / "sections"


def segment_filings(
    settings: Settings | None = None, tickers: list[str] | None = None
) -> dict:
    """Segment every acquired filing into section-tagged element tables
    (idempotent).

    Reads raw HTML via the acquisition manifest and writes one parquet per
    filing at the notebooks' exact path/layout:
    ``data/interim/sections/<accession_no>.parquet`` with columns
    ``accession_no, form, filing_date, element_index, section_id,
    section_title, element_type, text`` (notebook 03). Existing parquets
    are left untouched (NVDA's were written by notebook 03 itself).

    Returns ``{ticker: {"filings": n, "segmented": n, "cached": n,
    "no_keep_sections": [accession_no, ...]}}``.
    """
    settings = settings or get_settings()
    manifest = load_manifest(settings)
    if not manifest:
        raise RuntimeError(
            "no acquisition manifest — run semigraph.ingestion.download_filings first"
        )
    tickers = list(tickers) if tickers else [t for t in FILERS if t in manifest]
    out_dir = sections_dir(settings)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, dict] = {}
    for ticker in tickers:
        stats = {"filings": 0, "segmented": 0, "cached": 0, "no_keep_sections": []}
        for meta in manifest.get(ticker, []):
            stats["filings"] += 1
            out_path = out_dir / f"{meta['accession_no']}.parquet"
            if out_path.exists():
                stats["cached"] += 1
                continue
            html = resolve_local_path(settings, meta).read_text(encoding="utf-8")
            df = segment(html, meta["form"])
            keep = KEEP_SECTIONS.get(meta["form"], [])
            if df.empty or not df["section_id"].isin(keep).any():
                # ASML 2023/24 land here — unmarkable by design: warn + skip
                logger.warning(
                    "%s %s %s: no keep-sections segmented — review layout",
                    ticker, meta["form"], meta["filing_date"],
                )
                stats["no_keep_sections"].append(meta["accession_no"])
            df.insert(0, "accession_no", meta["accession_no"])
            df.insert(1, "form", meta["form"])
            df.insert(2, "filing_date", meta["filing_date"])
            df.to_parquet(out_path, index=False)
            stats["segmented"] += 1
            logger.info(
                "%s %s %s: %d elements, %d sections -> %s",
                ticker, meta["form"], meta["filing_date"],
                len(df), df["section_id"].nunique(), out_path.name,
            )
        summary[ticker] = stats
    return summary
