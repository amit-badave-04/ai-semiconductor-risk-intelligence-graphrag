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
index instead; the shipped ``nvda_chunks.parquet`` keeps those ids. Existing
rows are therefore NEVER regenerated or renumbered: ``chunk_filings`` is
append-only per accession (only accessions with no rows yet are chunked, and
their rows are appended after the existing ones), so seeded citations and
cached benchmark answers that key on every existing chunk_id stay valid.
"""

import logging
import re
from functools import lru_cache
from pathlib import Path

import pandas as pd

from ..config import Settings, get_settings
from ..ingestion.edgar import FILERS, load_manifest
from .segmentation import KEEP_SECTIONS, keep_sections_for, sections_dir

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
        keep = keep_sections_for(meta["form"])
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


def _write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    """Write via a sibling temp file + rename so an interrupted run can never
    leave a truncated parquet where the precious chunk ids used to be."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        df.to_parquet(tmp, index=False)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _load_existing(path: Path, columns: list[str]) -> pd.DataFrame:
    """Read an existing parquet, or an empty frame with ``columns`` if absent.

    A non-empty parquet missing any expected column is refused: appending to
    it would silently corrupt rows whose ids other artifacts depend on.
    """
    if not path.exists():
        return pd.DataFrame(columns=columns)
    df = pd.read_parquet(path)
    missing = [c for c in columns if c not in df.columns]
    if len(df) and missing:
        raise ValueError(
            f"{path.name}: existing parquet lacks columns {missing} — refusing "
            "to append (would corrupt existing rows)"
        )
    return df


def _append_rows(
    existing: pd.DataFrame, new_rows: list[dict], columns: list[str]
) -> pd.DataFrame:
    """``existing`` rows untouched (order, values, dtypes) + ``new_rows`` after.

    Returns a new frame; neither input is mutated.
    """
    new_df = pd.DataFrame(new_rows, columns=columns)
    if existing.empty:
        return new_df
    combined = pd.concat(
        [existing, new_df.reindex(columns=existing.columns)], ignore_index=True
    )
    return combined.astype(existing.dtypes.to_dict())


def _pending_filings(
    ticker: str, rows: list[dict], done: set[str], sec_dir: Path
) -> tuple[list[dict], list[str]]:
    """Manifest filings not yet chunked that have segmentation output.

    Returns ``(pending manifest rows in manifest order, warnings)``. A filing
    with no interim sections parquet is reported, not fatal.
    """
    pending: list[dict] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for meta in rows:
        accession = meta["accession_no"]
        if accession in done or accession in seen:
            continue
        seen.add(accession)
        if not (sec_dir / f"{accession}.parquet").exists():
            msg = f"{accession}: no interim sections parquet — run segment_filings first"
            logger.warning("%s %s", ticker, msg)
            warnings.append(msg)
            continue
        pending.append(meta)
    return pending, warnings


def _chunk_pending(
    ticker: str, pending: list[dict], sec_dir: Path
) -> tuple[list[dict], list[dict], list[str], list[str]]:
    """Chunk each pending filing. Returns ``(section_text_rows, chunk_rows,
    accessions that yielded chunks, warnings)``. A filing that yields nothing
    is warned about and adds nothing — it never aborts the batch."""
    st_rows: list[dict] = []
    ch_rows: list[dict] = []
    new_accessions: list[str] = []
    warnings: list[str] = []
    for meta in pending:
        accession = meta["accession_no"]
        try:
            elements_df = pd.read_parquet(sec_dir / f"{accession}.parquet")
        except (OSError, ValueError) as exc:
            msg = f"{accession}: unreadable interim sections parquet ({exc})"
            logger.warning("%s %s", ticker, msg)
            warnings.append(msg)
            continue
        st, ch = chunk_filing_elements(meta, elements_df)
        if not ch:
            msg = (
                f"{accession} ({meta['form']} {meta['filing_date']}): no "
                "keep-sections segmented — review layout"
            )
            logger.warning("%s %s", ticker, msg)
            warnings.append(msg)
            continue
        st_rows.extend(st)
        ch_rows.extend(ch)
        new_accessions.append(accession)
    return st_rows, ch_rows, new_accessions, warnings


def _chunk_ticker(settings: Settings, ticker: str, rows: list[dict]) -> dict:
    """Append-only chunking for one filer (see :func:`chunk_filings`)."""
    chunks_path = chunks_path_for(settings, ticker)
    st_path = section_texts_path_for(settings, ticker)
    existing = _load_existing(chunks_path, CHUNK_COLUMNS)
    existing_st = _load_existing(st_path, SECTION_TEXT_COLUMNS)
    done = set(existing["accession_no"]) if len(existing) else set()

    pending, warnings = _pending_filings(ticker, rows, done, sections_dir(settings))
    st_rows, ch_rows, new_accessions, chunk_warnings = _chunk_pending(
        ticker, pending, sections_dir(settings)
    )
    warnings = warnings + chunk_warnings

    combined, combined_st = existing, existing_st
    if new_accessions or not chunks_path.exists():
        # a re-added accession replaces any orphaned section texts from a
        # run that died between the two writes; chunk rows are never replaced
        kept_st = existing_st[~existing_st["accession_no"].isin(new_accessions)]
        combined_st = _append_rows(kept_st, st_rows, SECTION_TEXT_COLUMNS)
        combined = _append_rows(existing, ch_rows, CHUNK_COLUMNS)
        _write_parquet_atomic(combined_st, st_path)  # texts first: a crash just re-chunks
        _write_parquet_atomic(combined, chunks_path)
        logger.info(
            "%s: +%d chunks from %d new filing(s), %d chunks total",
            ticker, len(ch_rows), len(new_accessions), len(combined),
        )
    else:
        logger.info("%s: chunks up to date (%s)", ticker, chunks_path.name)

    return {
        "chunks": len(combined),
        "new_chunks": len(ch_rows),
        "new_accessions": new_accessions,
        "sections": len(combined_st) if st_path.exists() else None,
        "tokens": int(combined["n_tokens"].sum()) if len(combined) else 0,
        "cached": not new_accessions,
        "warnings": warnings,
    }


def chunk_filings(
    settings: Settings | None = None, tickers: list[str] | None = None
) -> dict:
    """Chunk every segmented filing, APPEND-ONLY per accession (idempotent).

    Reads ``data/interim/sections/<accession_no>.parquet`` (run
    ``segment_filings`` first) and maintains, per filer:

    - ``data/interim/section_texts/<ticker>_section_texts.parquet``
    - ``data/processed/chunks/<ticker>_chunks.parquet``

    For each filer the existing chunk parquet is read; only manifest
    accessions that have a sections parquet but NO rows yet in it are chunked,
    and their rows are appended AFTER the existing ones. Existing rows —
    including NVDA's legacy ids that the graph's EvidenceSpans, seeded
    citations and cached benchmark answers key on — keep their values and
    order exactly; the parquet is never regenerated from scratch, so a new
    filing for an existing filer needs (and risks) no rebuild. New ids follow
    ``accession:section:seq`` with ``seq`` counting per section. An accession
    that yields no keep-sections chunks is reported in ``warnings`` and adds
    nothing (it is retried on the next run, which is free and warns again).

    Returns ``{ticker: {"chunks": total, "new_chunks": n, "new_accessions":
    [...], "sections": total section texts (None if no section-text file),
    "tokens": total, "cached": True when there was no new work,
    "warnings": [...]}}``.
    """
    settings = settings or get_settings()
    manifest = load_manifest(settings)
    if not manifest:
        raise RuntimeError(
            "no acquisition manifest — run semigraph.ingestion.download_filings first"
        )
    tickers = list(tickers) if tickers else [t for t in FILERS if t in manifest]
    section_texts_dir(settings).mkdir(parents=True, exist_ok=True)
    settings.chunks_dir.mkdir(parents=True, exist_ok=True)
    return {
        ticker: _chunk_ticker(settings, ticker, manifest.get(ticker, []))
        for ticker in tickers
    }
