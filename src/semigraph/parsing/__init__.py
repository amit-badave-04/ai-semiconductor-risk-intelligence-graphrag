"""Data preparation: layout-aware segmentation (sec-parser + part-aware
regexes with the Intel/ASML custom-layout fallbacks) and table-aware
chunking (ported from notebooks 03, 04 and 12).

Pipeline: ``segment_filings`` (raw HTML -> ``data/interim/sections/``)
then ``chunk_filings`` (-> ``data/interim/section_texts/`` +
``data/processed/chunks/``).
"""

from .chunker import (
    CHUNK_COLUMNS,
    MAX_TOKENS,
    SEP,
    TARGET_TOKENS,
    chunk_filing_elements,
    chunk_filings,
    chunks_path_for,
    n_tokens,
    split_oversized,
)
from .segmentation import (
    KEEP_SECTIONS,
    RISK_SECTIONS,
    needs_fallback,
    segment,
    segment_filings,
)

__all__ = [
    "CHUNK_COLUMNS",
    "KEEP_SECTIONS",
    "MAX_TOKENS",
    "RISK_SECTIONS",
    "SEP",
    "TARGET_TOKENS",
    "chunk_filing_elements",
    "chunk_filings",
    "chunks_path_for",
    "n_tokens",
    "needs_fallback",
    "segment",
    "segment_filings",
    "split_oversized",
]
