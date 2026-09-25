"""Data acquisition: SEC EDGAR filings, XBRL company facts, and BIS /
Federal Register export-control rules (ported from notebooks 01, 02 and 12),
plus freshness checks that say what is new at the source.

Everything here is free, checkpointed to disk and incremental — re-running
downloads only what the data lake does not hold yet (per accession for
filings, on refresh for XBRL / Federal Register).
"""

from .edgar import (
    ANNUAL_SINCE,
    FILERS,
    FilingRecord,
    download_filings,
    load_manifest,
    load_ticker_to_cik,
    parse_as_of,
    resolve_local_path,
    select_targets,
)
from .federal_register import (
    RELEVANT_KINDS,
    RULE_KINDS,
    TOPIC_KEYWORDS,
    classify_rule,
    download_bis_rules,
)
from .freshness import federal_register_pending, fetch_submissions, pending_filings
from .xbrl import (
    KEY_CONCEPTS,
    curate_metrics,
    download_companyfacts,
    extract_metrics,
    supplement_metrics_from_filing_xbrl,
)

__all__ = [
    "ANNUAL_SINCE",
    "FILERS",
    "KEY_CONCEPTS",
    "RELEVANT_KINDS",
    "RULE_KINDS",
    "TOPIC_KEYWORDS",
    "FilingRecord",
    "classify_rule",
    "curate_metrics",
    "download_bis_rules",
    "download_companyfacts",
    "download_filings",
    "extract_metrics",
    "federal_register_pending",
    "fetch_submissions",
    "load_manifest",
    "load_ticker_to_cik",
    "parse_as_of",
    "pending_filings",
    "resolve_local_path",
    "select_targets",
    "supplement_metrics_from_filing_xbrl",
]
