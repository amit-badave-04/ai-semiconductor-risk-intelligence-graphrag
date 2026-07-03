"""LLM extraction, anti-fabrication gates, and entity resolution.

Ported from notebooks 07 (PoC extractor + critic + gates), 08 (canonical
dictionary + fuzzy resolution) and 12 (evolved multi-filer loop, Haiku critic,
filer-aware resolution). The notebooks remain the historical record; this
package is the supported interface.
"""

from .extractor import (
    FILERS,
    HIST_ANNUALS,
    RISK_SECTIONS,
    build_extraction_plan,
    chunk_parquet_path,
    estimate_extraction_cost,
    extraction_scope,
    extractions_jsonl_path,
    load_done_chunk_ids,
    run_extraction,
)
from .gates import normalize, quote_in_chunk
from .resolution import (
    FUZZY_THRESHOLD,
    LEGAL_SUFFIXES,
    SELF_REFERENCES,
    build_alias_lookup,
    normalize_name,
    resolve_entity,
    resolve_extractions,
    resolved_jsonl_path,
)
from .schemas import (
    RELATION_TYPES,
    RISK_CATEGORIES,
    ChunkExtraction,
    CriticVerdict,
    Product,
    Relation,
    RiskFactor,
    normalize_category,
)

__all__ = [
    # schemas
    "Relation", "RiskFactor", "Product", "ChunkExtraction", "CriticVerdict",
    "RELATION_TYPES", "RISK_CATEGORIES", "normalize_category",
    # gates
    "normalize", "quote_in_chunk",
    # extractor
    "FILERS", "RISK_SECTIONS", "HIST_ANNUALS", "run_extraction",
    "build_extraction_plan", "estimate_extraction_cost", "extraction_scope",
    "load_done_chunk_ids", "chunk_parquet_path", "extractions_jsonl_path",
    # resolution
    "resolve_entity", "resolve_extractions", "build_alias_lookup",
    "normalize_name", "resolved_jsonl_path", "LEGAL_SUFFIXES",
    "SELF_REFERENCES", "FUZZY_THRESHOLD",
]
