"""Entity resolution & canonicalization — ported from notebooks 08 and 12.

The LLM emits entity strings as written ("Taiwan Semiconductor Manufacturing
Company Limited", "TSMC", "Taiwan Semi") — all must resolve to ONE canonical
graph node, or the graph fragments and multi-hop traversal silently breaks.

Pipeline (knowledge-base-first, precision over recall):
1. canonical dictionary (packaged ``artifacts/canonical_entities.json``) alias match
2. normalization — casefold, strip legal suffixes (Inc/Corp/Ltd/...)
3. fuzzy fallback — ``difflib.SequenceMatcher`` ratio >= 0.90 against all aliases
4. unresolved entities are dropped from relations and logged to the resolution
   report (reviewing that report is how the dictionary grows)

Filer-aware: bare self-references ("we"/"our"/"us") resolve to the filer —
a safety net behind the extractor prompt, which already instructs the LLM to
resolve them to the filer's name.
"""

import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

from ..artifacts import load_canonical_entities
from ..config import Settings
from .extractor import FILERS, extractions_jsonl_path
from .schemas import normalize_category

logger = logging.getLogger("semigraph.resolution")

FUZZY_THRESHOLD = 0.90

LEGAL_SUFFIXES = re.compile(
    r"\b(incorporated|corporation|corp|inc|ltd|limited|llc|plc|co|company|holdings?|nv|sa|ag|kk)\b\.?",
    re.I,
)

# bare self-references a filer uses for itself (checked against the raw,
# casefolded string BEFORE suffix-stripping, which would mangle "the company")
SELF_REFERENCES = {"we", "our", "us", "ourselves", "the company", "the filer"}

REPORT_NAME = "resolution_report_universe.parquet"
_REPORT_COLUMNS = ["ticker", "chunk_id", "source_entity", "relation", "target_entity",
                   "evidence_quote", "src_resolved", "tgt_resolved"]


def normalize_name(s: str) -> str:
    """Casefold, strip punctuation and legal suffixes, collapse whitespace. Ported from notebook 08."""
    s = re.sub(r"[^\w\s]", " ", s.lower())
    s = LEGAL_SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def build_alias_lookup(canonical: dict) -> dict[str, str]:
    """normalized alias -> canonical name, from the canonical entity dictionary."""
    return {normalize_name(alias): name
            for name, spec in canonical.items()
            for alias in [name] + spec["aliases"]}


def resolve_entity(
    name: str,
    alias_lookup: dict[str, str],
    *,
    filer: str | None = None,
    fuzzy_threshold: float = FUZZY_THRESHOLD,
) -> str | None:
    """Raw entity string -> canonical name, or None if unresolvable.

    Pure function, ported from notebooks 08/12 with the filer-aware
    self-reference net: "we"/"our"/... -> ``filer`` (a canonical name)
    when provided.
    """
    if filer and name.strip().lower() in SELF_REFERENCES:
        return filer
    norm = normalize_name(name)
    if not norm:
        return None
    if norm in alias_lookup:
        return alias_lookup[norm]
    best_name, best_score = None, 0.0
    for alias_norm, canonical_name in alias_lookup.items():
        score = SequenceMatcher(None, norm, alias_norm).ratio()
        if score > best_score:
            best_name, best_score = canonical_name, score
    return best_name if best_score >= fuzzy_threshold else None


def resolved_jsonl_path(settings: Settings, ticker: str) -> Path:
    raw = extractions_jsonl_path(settings, ticker)
    return raw.with_name(raw.name.replace("_extractions.jsonl", "_extractions_resolved.jsonl"))


def resolve_extractions(
    settings: Settings,
    tickers: list[str] | None = None,
    canonical: dict | None = None,
) -> dict:
    """Resolve every filer's ``*_extractions.jsonl`` -> ``*_extractions_resolved.jsonl``.

    Ported from notebook 08 (resolved-record format: each kept relation gains
    ``source_canonical``/``target_canonical``) + notebook 12 (filer-aware loop
    over the universe; drop-and-log report at
    ``data/processed/extractions/resolution_report_universe.parquet``).

    Relations are dropped (and logged) when either endpoint is unresolvable or
    when both resolve to the same entity (self-loop). Risk-factor categories
    are normalized onto ``RISK_CATEGORIES`` at persist time so already-billed
    extraction files get the category fix without re-extraction.

    Returns {"kept": int, "dropped": int, "per_ticker": {ticker: kept}, "report_path": Path}.
    """
    canonical = canonical or load_canonical_entities()
    alias_lookup = build_alias_lookup(canonical)
    tickers = list(tickers) if tickers else list(FILERS)

    dropped_all: list[dict] = []
    per_ticker: dict[str, int] = {}
    n_kept_total = 0
    for ticker in tickers:
        jl = extractions_jsonl_path(settings, ticker)
        if not jl.exists():
            logger.warning("%s: no extractions at %s — skipping", ticker, jl)
            continue
        filer = FILERS[ticker][0] if ticker in FILERS else None
        records = [json.loads(line) for line in jl.open(encoding="utf-8") if line.strip()]
        resolved_records = []
        n_kept = 0
        for rec in records:
            out = dict(rec, relations=[])
            for rel in rec["relations"]:
                src = resolve_entity(rel["source_entity"], alias_lookup, filer=filer)
                tgt = resolve_entity(rel["target_entity"], alias_lookup, filer=filer)
                if src and tgt and src != tgt:
                    out["relations"].append(dict(rel, source_canonical=src, target_canonical=tgt))
                    n_kept += 1
                else:
                    dropped_all.append({"ticker": ticker, "chunk_id": rec["chunk_id"], **rel,
                                        "src_resolved": src, "tgt_resolved": tgt})
            out["risk_factors"] = [{**rf, "category": normalize_category(rf["category"])}
                                   for rf in rec.get("risk_factors", [])]
            resolved_records.append(out)
        out_path = resolved_jsonl_path(settings, ticker)
        with out_path.open("w", encoding="utf-8") as f:
            for rec in resolved_records:
                f.write(json.dumps(rec) + "\n")
        per_ticker[ticker] = n_kept
        n_kept_total += n_kept
        logger.info("%s: kept %d relations -> %s", ticker, n_kept, out_path.name)

    report_path = settings.extractions_dir / REPORT_NAME
    pd.DataFrame(dropped_all, columns=_REPORT_COLUMNS).to_parquet(report_path, index=False)
    logger.info("kept %d relations, dropped %d (unresolved/self-loop) -> %s",
                n_kept_total, len(dropped_all), report_path.name)
    return {"kept": n_kept_total, "dropped": len(dropped_all),
            "per_ticker": per_ticker, "report_path": report_path}
