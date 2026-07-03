"""Table-aware semantic chunking
(ported from notebook 04, generalized to all filers in notebook 12).

Chunks are the atoms of provenance for the whole system — every
``EvidenceSpan`` node is one of these rows. Design invariants (feasibility
studies: chunking mistakes are "the single largest driver of silent
quality loss"):

- chunks **never cross section boundaries**; each carries its hierarchy
  (ticker -> filing -> section -> sub-heading)
- **tables become standalone chunks** (never merged into prose, never split)
- prose accumulates whole elements up to ``TARGET_TOKENS``; a single
  element longer than ``MAX_TOKENS`` is split on sentence boundaries
- chunk text is an **exact substring of the canonical section text**
  (``char_start:char_end``) — byte-precise provenance without re-parsing
  HTML. The canonical text is each section's elements joined with
  ``SEP`` and persisted to ``data/interim/section_texts/``.

The core is pure (no filesystem): ``chunk_filing_elements`` maps segmented
element rows -> (section-text rows, chunk rows). ``chunk_filings`` is the
filesystem driver reading ``data/interim/sections/`` and writing the
notebook-identical parquets under ``data/processed/chunks/``.

Output schema (matches the existing parquets byte-for-byte in column order):
``chunk_id, ticker, cik, form, filing_date, accession_no, section_id,
section_title, sub_heading, kind, text, char_start, char_end, n_tokens,
source_url``.

Note on chunk ids: this ports notebook 12's convention —
``{accession_no}:{section_id}:{seq:04d}`` with ``seq`` counting per
section. Notebook 04's original NVDA run numbered chunks by global row
index instead; the shipped ``nvda_chunks.parquet`` keeps those ids and is
therefore never rewritten (idempotent skip), exactly as notebook 12 did.
"""

import logging
import re
from functools import lru_cache
from pathlib import Path

import pandas as pd

from ..config import Settings, get_settings
from ..ingestion.edgar import FILERS, load_manifest
from .segmentation import KEEP_SECTIONS, sections_dir

logger = logging.getLogger("semigraph.parsing.chunker")

SEP = "\n\n"
TARGET_TOKENS = 700  # start a new chunk once the current one reaches this
MAX_TOKENS = 1100    # hard cap — oversized single elements get sentence-split
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

CHUNK_COLUMNS = [
    "chunk_id", "ticker", "cik", "form", "filing_date", "accession_no",
    "section_id", "section_title", "sub_heading", "kind", "text",
    "char_start", "char_end", "n_tokens", "source_url",
]
SECTION_TEXT_COLUMNS = [
    "accession_no", "section_id", "form", "filing_date", "section_title",
    "text", "n_chars",
]


@lru_cache(maxsize=1)
def _enc():
    import tiktoken  # loads the cl100k_base BPE lazily (cached on disk)

    return tiktoken.get_encoding("cl100k_base")


def n_tokens(text: str) -> int:
    """cl100k_base token count (budgeting for extraction + embedding)."""
    return len(_enc().encode(text, disallowed_special=()))


def split_oversized(
    text: str, abs_start: int, max_tokens: int = MAX_TOKENS
) -> list[tuple[str, int, int]]:
    """Split one long element into <= max_tokens pieces at sentence
    boundaries. Returns (piece_text, abs_char_start, abs_char_end) with
    offsets into the canonical section text. Pure; ported from notebook 12.
    """
    pieces, buf, buf_start, cursor = [], [], abs_start, abs_start
    for sent in SENTENCE_RE.split(text):
        if buf and n_tokens(" ".join(buf + [sent])) > max_tokens:
            joined = " ".join(buf)
            pieces.append((joined, buf_start, buf_start + len(joined)))
            buf, buf_start = [sent], cursor
        else:
            buf.append(sent)
        cursor += len(sent) + 1  # +1 for the split whitespace
    if buf:
        joined = " ".join(buf)
        pieces.append(
            (joined, buf_start, min(buf_start + len(joined), abs_start + len(text)))
        )
    return pieces


def chunk_filing_elements(
    meta: dict, elements_df: pd.DataFrame, keep: list[str] | None = None
) -> tuple[list[dict], list[dict]]:
    """Chunk one filing's segmented elements. PURE — no filesystem.

    Ported from notebook 12 ``chunk_filing``. ``meta`` is the filing's
    manifest row (ticker, cik, form, filing_date, accession_no, source_url);
    ``elements_df`` is the segmentation output (element_index, section_id,
    section_title, element_type, text). Restricted to ``keep`` sections
    (default: ``KEEP_SECTIONS[form]``).

    Returns ``(section_text_rows, chunk_rows)``. Empty input -> empty
    output — the caller reports it; never crash the batch (ASML 2023/24).
    """
    if keep is None:
        keep = KEEP_SECTIONS.get(meta["form"], [])
    st_rows: list[dict] = []
    chunk_rows: list[dict] = []
    if elements_df.empty:
        return st_rows, chunk_rows
    for sid, grp in elements_df[elements_df["section_id"].isin(keep)].groupby(
        "section_id", sort=False
    ):
        grp = grp.sort_values("element_index")
        # canonical section text = elements joined with SEP; offsets index into it
        offsets, cursor, parts = [], 0, []
        for _, row in grp.iterrows():
            offsets.append((row, cursor, cursor + len(row["text"])))
            parts.append(row["text"])
            cursor += len(row["text"]) + len(SEP)
        full_text = SEP.join(parts)
        st_rows.append(
            {
                "accession_no": meta["accession_no"],
                "section_id": sid,
                "form": meta["form"],
                "filing_date": meta["filing_date"],
                "section_title": grp.iloc[0]["section_title"],
                "text": full_text,
                "n_chars": len(full_text),
            }
        )
        buf, sub_heading, seq = [], None, 0

        def flush(kind):
            nonlocal seq
            if not buf:
                return
            start, end = buf[0][1], buf[-1][2]
            text = full_text[start:end]
            chunk_rows.append(
                {
                    "chunk_id": f"{meta['accession_no']}:{sid}:{seq:04d}",
                    "ticker": meta["ticker"],
                    "cik": meta["cik"],
                    "form": meta["form"],
                    "filing_date": meta["filing_date"],
                    "accession_no": meta["accession_no"],
                    "section_id": sid,
                    "section_title": grp.iloc[0]["section_title"],
                    "sub_heading": sub_heading,
                    "kind": kind,
                    "text": text,
                    "char_start": start,
                    "char_end": end,
                    "n_tokens": n_tokens(text),
                    "source_url": meta["source_url"],
                }
            )
            seq += 1
            buf.clear()

        for row, start, end in offsets:
            if row["element_type"] == "TitleElement":
                flush("prose")
                sub_heading = row["text"][:150]
                continue
            if row["element_type"] == "TableElement":
                # tables are standalone: flush prose, emit the table alone
                flush("prose")
                buf.append((row, start, end))
                flush("table")
                continue
            if n_tokens(row["text"]) > MAX_TOKENS:  # oversized prose element
                flush("prose")
                for piece, ps, pe in split_oversized(row["text"], start):
                    buf.append((row, ps, pe))
                    flush("prose")
                continue
            buf.append((row, start, end))
            if n_tokens(full_text[buf[0][1] : buf[-1][2]]) >= TARGET_TOKENS:
                flush("prose")
        flush("prose")
    return st_rows, chunk_rows


def section_texts_dir(settings: Settings) -> Path:
    return settings.interim_dir / "section_texts"


def chunks_path_for(settings: Settings, ticker: str) -> Path:
    """Notebook filename convention: notebook 04 wrote NVDA's chunks as
    ``nvda_chunks.parquet`` (lowercase); notebook 12 kept that file as-is
    and named every other filer ``<TICKER>_chunks.parquet``."""
    name = "nvda_chunks.parquet" if ticker == "NVDA" else f"{ticker}_chunks.parquet"
    return settings.chunks_dir / name


def section_texts_path_for(settings: Settings, ticker: str) -> Path:
    name = (
        "nvda_section_texts.parquet"
        if ticker == "NVDA"
        else f"{ticker}_section_texts.parquet"
    )
    return section_texts_dir(settings) / name


def chunk_filings(
    settings: Settings | None = None, tickers: list[str] | None = None
) -> dict:
    """Chunk every segmented filing (idempotent; ported from notebook 12
    stage 3).

    Reads ``data/interim/sections/<accession_no>.parquet`` (run
    ``segment_filings`` first) and writes, per filer:

    - ``data/interim/section_texts/<ticker>_section_texts.parquet``
    - ``data/processed/chunks/<ticker>_chunks.parquet``

    A filer whose chunks parquet already exists (and is non-empty) is
    skipped, so the notebook-built data lake — including NVDA's
    notebook-04 chunk ids that the graph's EvidenceSpans key on — is never
    rewritten.

    Returns ``{ticker: {"chunks": n, "sections": n, "tokens": n,
    "cached": bool, "warnings": [...]}}``.
    """
    settings = settings or get_settings()
    manifest = load_manifest(settings)
    if not manifest:
        raise RuntimeError(
            "no acquisition manifest — run semigraph.ingestion.download_filings first"
        )
    tickers = list(tickers) if tickers else [t for t in FILERS if t in manifest]
    sec_dir = sections_dir(settings)
    st_dir = section_texts_dir(settings)
    st_dir.mkdir(parents=True, exist_ok=True)
    settings.chunks_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, dict] = {}
    for ticker in tickers:
        chunks_path = chunks_path_for(settings, ticker)
        if chunks_path.exists():
            existing = pd.read_parquet(chunks_path)
            if len(existing):
                logger.info("%s: chunks cached (%s)", ticker, chunks_path.name)
                summary[ticker] = {
                    "chunks": len(existing),
                    "sections": None,
                    "tokens": int(existing["n_tokens"].sum()),
                    "cached": True,
                    "warnings": [],
                }
                continue
        st_all, ch_all, warnings = [], [], []
        for meta in manifest.get(ticker, []):
            sec_path = sec_dir / f"{meta['accession_no']}.parquet"
            if not sec_path.exists():
                msg = (
                    f"{meta['accession_no']}: no interim sections parquet — "
                    "run segment_filings first"
                )
                logger.warning("%s %s", ticker, msg)
                warnings.append(msg)
                continue
            elements_df = pd.read_parquet(sec_path)
            st, ch = chunk_filing_elements(meta, elements_df)
            if not ch:
                msg = (
                    f"{meta['form']} {meta['filing_date']}: no keep-sections "
                    "segmented — review layout"
                )
                logger.warning("%s %s", ticker, msg)
                warnings.append(msg)
            st_all.extend(st)
            ch_all.extend(ch)
        pd.DataFrame(st_all, columns=SECTION_TEXT_COLUMNS).to_parquet(
            section_texts_path_for(settings, ticker), index=False
        )
        pd.DataFrame(ch_all, columns=CHUNK_COLUMNS).to_parquet(
            chunks_path, index=False
        )
        tokens = sum(c["n_tokens"] for c in ch_all)
        logger.info(
            "%s: %d chunks / %d sections / %s tokens",
            ticker, len(ch_all), len(st_all), f"{tokens:,}",
        )
        summary[ticker] = {
            "chunks": len(ch_all),
            "sections": len(st_all),
            "tokens": tokens,
            "cached": False,
            "warnings": warnings,
        }
    return summary
