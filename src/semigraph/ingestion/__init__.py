"""Data acquisition: SEC EDGAR filings, XBRL company facts, and BIS /
Federal Register export-control rules (ported from notebooks 01, 02 and 12).

Everything here is free, checkpointed to disk and idempotent — re-running
never re-downloads what is already in the data lake.
"""

from .edgar import (
    ANNUAL_SINCE,
    FILERS,
    download_filings,
    load_manifest,
    load_ticker_to_cik,
    resolve_local_path,
)
from .federal_register import TOPIC_KEYWORDS, download_bis_rules
from .xbrl import (
    KEY_CONCEPTS,
    curate_metrics,
    download_companyfacts,
    extract_metrics,
)

__all__ = [
    "ANNUAL_SINCE",
    "FILERS",
    "KEY_CONCEPTS",
    "TOPIC_KEYWORDS",
    "curate_metrics",
    "download_bis_rules",
    "download_companyfacts",
    "download_filings",
    "extract_metrics",
    "load_manifest",
    "load_ticker_to_cik",
    "resolve_local_path",
]
