"""Graph loaders — deterministic layer, evidence layer, knowledge layer,
export controls (ported from notebooks 06, 09 and 12), plus the v2 freshness
properties.

All loaders are idempotent MERGE-based (every property is SET on every run)
and write in 100-row UNWIND batches (the notebooks' transaction-size
convention), reading the data-lake files the notebooks produced under
``settings.data_dir``:

- ``data/raw/edgar/company_tickers.json`` + ``manifest_universe.json``
- ``data/interim/section_texts/{TICKER}_section_texts.parquet``
- ``data/processed/chunks/{TICKER}_chunks.parquet``  (NVDA: ``nvda_chunks.parquet``)
- ``data/processed/embeddings/{TICKER}_chunk_embeddings.parquet``
- ``data/processed/extractions/{ticker}_extractions.jsonl``
- ``data/processed/xbrl/{TICKER}_key_metrics.parquet``
- ``data/raw/federal_register_bis_rules.json``

Freshness (which filing/section is current, what may be retrieved) is decided by the
pure ``semigraph.versions`` rules and ``freshness``; this module stamps the result:

- ``Filing``: ``status`` / ``supersede_kind`` / ``superseded_by`` / ``is_current`` /
  ``corrected_sections``, ``(newer)-[:SUPERSEDES {kind}]->(older)`` and
  ``(amendment)-[:AMENDS {sections}]->(original)`` (partial amendments);
- ``EvidenceSpan`` / ``RiskFactor``: ``valid_from`` / ``valid_to`` (open-ended =
  ``9999-12-31``), ``is_current`` and, for spans, ``retrievable`` (current, or a
  superseded ANNUAL — historical annual risk text stays searchable), per section;
- relation edges: ``has_current_evidence`` / ``last_evidenced_at``;
- every node written carries ``snapshot_id``.

Samsung is an entity-only Company node: it doesn't file with the SEC, so it
appears in ``UNIVERSE`` (with a synthetic negative cik) but not in ``FILERS``
and never gets Filing/Metric/EvidenceSpan children.
"""

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
from neo4j import Driver

from .. import __version__
from ..artifacts import load_canonical_entities
from ..config import Settings, get_settings
from ..embeddings import Embedder
from ..snapshot import compute_snapshot_id
from ..universe import FILERS, HIST_ANNUALS, RISK_SECTIONS, UNIVERSE  # noqa: F401 — re-exported for callers
from .client import run_cypher
from .export_rules import (  # noqa: F401 — re-exported: the pure helpers are part of this module's API
    EXPORT_CONTROLS_CATEGORY,
    KIND_EVIDENCE_KEYWORDS,
    LEGACY_KIND,
    MAX_AFFECTED_BY_PER_COMPANY,
    MAX_EDGE_EVIDENCE_CHUNKS,
    TOPIC_KEYWORDS,
    build_affected_by_rows,
    normalize_rule,
    rule_matches_evidence,
    select_affected_rules,
)
from .freshness import (  # noqa: F401 — re-exported: the pure helpers are part of this module's API
    NVDA_TICKER,
    SOURCE_TYPE_SEC,
    VALID_TO_OPEN,
    FilingState,
    _span_scope,
    build_amends_rows,
    build_filing_rows,
    build_span_rows,
    build_supersedes_rows,
    chunk_sections,
    current_filings_without_chunks,
    derive_filing_states,
    risk_stamp,
    select_span_chunks,
    span_freshness,
    versions_for_ticker,
)

logger = logging.getLogger("semigraph.graph.loaders")

BATCH_SIZE = 100  # UNWIND batch size keeping transactions small (notebooks 09/12)

RELATION_TYPES = ["SUPPLIES_TO", "DEPENDS_ON", "CUSTOMER_OF", "COMPETES_WITH"]

# --------------------------------------------------------------------------
# shared plumbing
# --------------------------------------------------------------------------

def _run_batched(driver: Driver, query: str, rows: list[dict],
                 batch_size: int = BATCH_SIZE, **params) -> None:
    """Run an UNWIND query over rows in small batches (notebooks 09/12).

    ``params`` (e.g. ``snapshot_id``) are sent with every batch."""
    with driver.session() as session:
        for i in range(0, len(rows), batch_size):
            session.run(query, rows=rows[i : i + batch_size], **params)


def _resolve_snapshot_id(settings: Settings, snapshot_id: str | None) -> str:
    """The explicit id, else the id of the data lake as it stands now. A build that
    calls several loaders should compute ONE id and pass it to all of them: files
    that change between calls (e.g. paid extraction) would otherwise leave it
    stamped with several ids."""
    if snapshot_id:
        return snapshot_id
    computed = compute_snapshot_id(settings)
    logger.warning("no snapshot_id passed — stamping %s computed from the data lake as it is now; "
                   "compute one id per build and pass it to every loader", computed)
    return computed


def _chunks_path(settings: Settings, ticker: str) -> Path:
    # notebook 04's NVDA output kept its lowercase PoC name (notebook 12 stage 3)
    name = "nvda_chunks.parquet" if ticker == NVDA_TICKER else f"{ticker}_chunks.parquet"
    return settings.chunks_dir / name


def _section_texts_path(settings: Settings, ticker: str) -> Path:
    name = ("nvda_section_texts.parquet" if ticker == NVDA_TICKER
            else f"{ticker}_section_texts.parquet")
    return settings.interim_dir / "section_texts" / name


def _embeddings_cache_path(settings: Settings, ticker: str) -> Path:
    # notebook 09 cached NVDA under its lowercase PoC name; notebook 12 used
    # {TICKER}_chunk_embeddings.parquet for every other filer
    name = ("nvda_chunk_embeddings.parquet" if ticker == NVDA_TICKER
            else f"{ticker}_chunk_embeddings.parquet")
    return settings.embeddings_dir / name


def _extractions_path(settings: Settings, ticker: str) -> Path:
    name = ("nvda_extractions.jsonl" if ticker == NVDA_TICKER
            else f"{ticker.lower()}_extractions.jsonl")
    return settings.extractions_dir / name


def _load_manifest(settings: Settings) -> dict:
    path = settings.raw_dir / "edgar" / "manifest_universe.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run the acquisition stage (semigraph.ingestion / "
            "notebook 12 stage 1) first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _load_extraction_records(settings: Settings, ticker: str) -> list[dict] | None:
    """The ticker's extraction records, or None when it has no extraction file."""
    path = _extractions_path(settings, ticker)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _read_chunks(settings: Settings, ticker: str, *, must_exist: bool = True) -> pd.DataFrame | None:
    """The filer's chunk frame, or None (with a warning) for an empty / column-less
    parquet (no segmentable filing) or, if not ``must_exist``, a missing one."""
    path = _chunks_path(settings, ticker)
    if not path.exists() and not must_exist:
        logger.warning("%s: no chunk parquet at %s — no amendment is treated as parsed", ticker, path)
        return None
    ch = pd.read_parquet(path)
    if ch.empty or not {"accession_no", "section_id", "chunk_id"} <= set(ch.columns):
        logger.warning("%s: chunk parquet has no chunks — skipping", ticker)
        return None
    return ch


def _ticker_freshness(manifest: dict, ticker: str, ch: pd.DataFrame | None) -> tuple[list, dict]:
    """(versions, states) of one filer; ``ch`` = its chunk frame (None: nothing parsed)."""
    rows = manifest.get(ticker, [])
    sections = chunk_sections(ch) if ch is not None else {}
    versions = versions_for_ticker(ticker, rows, sections)
    return versions, derive_filing_states(rows, versions, sections)


def _alias_patterns() -> list[tuple[re.Pattern, int]]:
    """Word-boundary alias regexes over the canonical dictionary (notebooks 09/12)."""
    canonical = load_canonical_entities()
    return [
        (re.compile(rf"\b{re.escape(a)}\b", re.I), spec["entity_id"])
        for nm, spec in canonical.items()
        for a in {nm, *spec["aliases"]}
    ]


# --------------------------------------------------------------------------
# deterministic layer (no LLM) — notebook 06, generalized by notebook 12 stage 4
# --------------------------------------------------------------------------

def load_companies(driver: Driver, settings: Settings | None = None, *,
                   snapshot_id: str | None = None) -> int:
    """MERGE the 14-company universe as Company nodes (ported from notebook 06).

    CIKs come from SEC's ``company_tickers.json`` (downloaded by notebook 01's
    acquisition); companies without a CIK (Samsung) get a synthetic negative
    key so the uniqueness constraint still holds — Samsung stays entity-only,
    with no filings.
    """
    settings = settings or get_settings()
    snapshot_id = _resolve_snapshot_id(settings, snapshot_id)
    tickers_path = settings.raw_dir / "edgar" / "company_tickers.json"
    raw_tickers = json.loads(tickers_path.read_text(encoding="utf-8"))
    ticker_to_cik = {row["ticker"]: row["cik_str"] for row in raw_tickers.values()}

    company_rows = []
    synthetic = -1
    for ticker, (name, tier) in UNIVERSE.items():
        cik = ticker_to_cik.get(ticker)
        if cik is None:
            cik, synthetic = synthetic, synthetic - 1  # non-SEC-filer
        company_rows.append({"cik": cik, "ticker": ticker, "name": name, "tier": tier,
                             "sec_filer": ticker in ticker_to_cik})

    _run_batched(
        driver,
        """UNWIND $rows AS row
        MERGE (c:Company {cik: row.cik})
        SET c.ticker = row.ticker, c.name = row.name, c.tier = row.tier, c.sec_filer = row.sec_filer,
            c.snapshot_id = $snapshot_id""",
        company_rows, snapshot_id=snapshot_id,
    )
    logger.info("%d universe Company nodes merged", len(company_rows))
    return len(company_rows)


_FILING_CYPHER = """UNWIND $rows AS row
    MATCH (c:Company {ticker: row.ticker})
    MERGE (f:Filing {accession_no: row.accession_no})
    SET f.form = row.form, f.filing_date = date(row.filing_date), f.url = row.source_url,
        f.status = row.status, f.supersede_kind = row.supersede_kind,
        f.superseded_by = row.superseded_by, f.is_current = row.is_current,
        f.corrected_sections = row.corrected_sections, f.snapshot_id = $snapshot_id
    MERGE (c)-[:FILED {date: date(row.filing_date)}]->(f)"""

# SUPERSEDES is derived data: drop the edges into these filings, then recreate the
# current set, so a filing whose superseder changed never keeps a stale edge.
_DROP_SUPERSEDES_CYPHER = """UNWIND $rows AS row
    MATCH (:Filing)-[s:SUPERSEDES]->(:Filing {accession_no: row.accession_no})
    DELETE s"""

_SUPERSEDES_CYPHER = """UNWIND $rows AS row
    MATCH (newer:Filing {accession_no: row.newer}), (older:Filing {accession_no: row.older})
    MERGE (newer)-[s:SUPERSEDES]->(older)
    SET s.kind = row.kind"""

# AMENDS: a PARTIAL amendment (AMD's Item-7-only 10-K/A) overlays the filing it
# amends; `sections` are the sections it restates. Recomputed like SUPERSEDES.
_DROP_AMENDS_CYPHER = """UNWIND $rows AS row
    MATCH (:Filing {accession_no: row.accession_no})-[a:AMENDS]->(:Filing)
    DELETE a"""

_AMENDS_CYPHER = """UNWIND $rows AS row
    MATCH (newer:Filing {accession_no: row.newer}), (older:Filing {accession_no: row.older})
    MERGE (newer)-[a:AMENDS]->(older)
    SET a.sections = row.sections"""

_SECTION_CYPHER = """UNWIND $rows AS row
    MATCH (f:Filing {accession_no: row.accession_no})
    MERGE (s:FilingSection {section_key: row.section_key})
    SET s.section_id = row.section_id, s.title = row.title, s.n_chars = row.n_chars,
        s.snapshot_id = $snapshot_id
    MERGE (f)-[:HAS_SECTION]->(s)"""


def load_filings_and_sections(driver: Driver, settings: Settings | None = None,
                              tickers: list[str] | None = None, *,
                              snapshot_id: str | None = None) -> tuple[int, int]:
    """MERGE Filing nodes (+ version status, ``corrected_sections``, SUPERSEDES and
    AMENDS edges) + FILED edges and FilingSection nodes + HAS_SECTION edges from
    the acquisition manifest and section-text parquets (ported from notebook 06,
    generalized by notebook 12 stage 4).

    Version status comes from ``semigraph.versions``; the accessions and section
    ids in the filer's chunk parquet say which amendments were parsed and
    whether they replace their original whole or only overlay some sections.
    Returns (n_filing_rows, n_section_rows). Requires load_companies first
    (FILED matches on Company.ticker).
    """
    settings = settings or get_settings()
    snapshot_id = _resolve_snapshot_id(settings, snapshot_id)
    manifest = _load_manifest(settings)
    tickers = list(tickers) if tickers else list(FILERS)

    filing_rows, supersedes_rows, amends_rows = [], [], []
    for ticker in tickers:
        _, states = _ticker_freshness(manifest, ticker, _read_chunks(settings, ticker, must_exist=False))
        filing_rows += build_filing_rows(manifest.get(ticker, []), states)
        supersedes_rows += build_supersedes_rows(states)
        amends_rows += build_amends_rows(states)
    _run_batched(driver, _FILING_CYPHER, filing_rows, snapshot_id=snapshot_id)
    _run_batched(driver, _DROP_SUPERSEDES_CYPHER, filing_rows)
    _run_batched(driver, _SUPERSEDES_CYPHER, supersedes_rows)
    _run_batched(driver, _DROP_AMENDS_CYPHER, filing_rows)
    _run_batched(driver, _AMENDS_CYPHER, amends_rows)

    section_rows = []
    for ticker in tickers:
        st = pd.read_parquet(_section_texts_path(settings, ticker))
        section_rows += [
            {"section_key": f"{r.accession_no}:{r.section_id}", "accession_no": r.accession_no,
             "section_id": r.section_id, "title": r.section_title, "n_chars": int(r.n_chars)}
            for r in st.itertuples()
        ]
    _run_batched(driver, _SECTION_CYPHER, section_rows, snapshot_id=snapshot_id)
    logger.info("%d filings (%d SUPERSEDES, %d AMENDS), %d sections loaded for %d filers",
                len(filing_rows), len(supersedes_rows), len(amends_rows), len(section_rows), len(tickers))
    return len(filing_rows), len(section_rows)


def load_metrics(driver: Driver, settings: Settings | None = None,
                 tickers: list[str] | None = None, *, snapshot_id: str | None = None) -> int:
    """MERGE Metric nodes + REPORTS_METRIC edges from the curated XBRL parquets
    (ported from notebook 06, generalized by notebook 12 stage 4).

    The no-LLM-numbers rule in action: every value comes straight from XBRL.
    ``metric_id = {cik}:{metric}:{period_end}``; the ``accn`` provenance rides
    on the edge so every number is traceable to its first-disclosing filing.
    """
    settings = settings or get_settings()
    snapshot_id = _resolve_snapshot_id(settings, snapshot_id)
    tickers = list(tickers) if tickers else list(FILERS)
    xbrl_dir = settings.processed_dir / "xbrl"

    metric_rows = []
    for ticker in tickers:
        mp = xbrl_dir / f"{ticker}_key_metrics.parquet"
        if not mp.exists():
            logger.warning("%s: no key-metrics parquet — skipping", ticker)
            continue
        for r in pd.read_parquet(mp).itertuples():
            metric_rows.append({"metric_id": f"{int(r.cik)}:{r.metric}:{r.end}", "cik": int(r.cik),
                                "metric": r.metric, "concept": r.concept, "value": float(r.val),
                                "unit": r.unit, "period_start": r.start, "period_end": r.end,
                                "accn": r.accn})
    _run_batched(
        driver,
        """UNWIND $rows AS row
        MATCH (c:Company {cik: row.cik})
        MERGE (m:Metric {metric_id: row.metric_id})
        SET m.metric = row.metric, m.concept = row.concept, m.value = row.value, m.unit = row.unit,
            m.period_start = date(row.period_start), m.period_end = date(row.period_end),
            m.snapshot_id = $snapshot_id
        MERGE (c)-[rel:REPORTS_METRIC]->(m) SET rel.accession_no = row.accn""",
        metric_rows, snapshot_id=snapshot_id,
    )
    logger.info("%d metric-periods loaded", len(metric_rows))
    return len(metric_rows)


# --------------------------------------------------------------------------
# evidence layer — notebook 09, filer-aware form from notebook 12 stage 6
# --------------------------------------------------------------------------

def load_ecosystem_companies(driver: Driver, snapshot_id: str | None = None) -> int:
    """MERGE canonical-dictionary entities beyond the 14 as tier-'Ecosystem'
    Company nodes (ported from notebook 09).

    Must run BEFORE MENTIONS edges are created, or mentions of e.g. SK Hynix would
    silently drop. A None ``snapshot_id`` leaves an existing stamp untouched.
    """
    canonical = load_canonical_entities()
    ecosystem_rows = [
        {"cik": spec["entity_id"], "ticker": spec.get("ticker"), "name": name, "tier": "Ecosystem"}
        for name, spec in canonical.items() if spec["cik"] is None and name != "Samsung"
    ]
    _run_batched(
        driver,
        """UNWIND $rows AS row
        MERGE (c:Company {cik: row.cik})
        SET c.name = row.name, c.tier = coalesce(c.tier, row.tier), c.sec_filer = coalesce(c.sec_filer, false),
            c.ticker = coalesce(c.ticker, row.ticker), c.snapshot_id = coalesce($snapshot_id, c.snapshot_id)""",
        ecosystem_rows, snapshot_id=snapshot_id,
    )
    logger.info("%d ecosystem Company nodes merged", len(ecosystem_rows))
    return len(ecosystem_rows)


_SPAN_CYPHER = """UNWIND $rows AS row
    MERGE (e:EvidenceSpan {chunk_id: row.chunk_id})
    SET e.text = row.text, e.kind = row.kind, e.sub_heading = row.sub_heading,
        e.char_start = row.char_start, e.char_end = row.char_end,
        e.n_tokens = row.n_tokens, e.source_url = row.source_url, e.embedding = row.embedding,
        e.content_hash = row.content_hash, e.filer_cik = row.filer_cik, e.form = row.form,
        e.accession_no = row.accession_no, e.section_id = row.section_id,
        e.filing_date = date(row.filing_date), e.valid_from = date(row.valid_from),
        e.valid_to = date(row.valid_to), e.is_current = row.is_current,
        e.retrievable = row.retrievable, e.status = row.status,
        e.source_type = row.source_type, e.snapshot_id = $snapshot_id
    WITH e, row MATCH (sec:FilingSection {section_key: row.section_key})
    MERGE (e)-[:FROM_SECTION]->(sec)
    WITH e, row UNWIND row.mentions AS eid
    MATCH (c:Company {cik: eid}) MERGE (e)-[:MENTIONS]->(c)"""


def load_evidence_spans(driver: Driver, settings: Settings | None = None,
                        embedder: Embedder | None = None,
                        tickers: list[str] | None = None,
                        hist_annuals: int = HIST_ANNUALS, *,
                        snapshot_id: str | None = None) -> int:
    """MERGE EvidenceSpan nodes with embeddings + freshness properties +
    FROM_SECTION containment + MENTIONS company anchors (ported from notebook 09,
    filer-aware form from notebook 12 stage 6).

    - Loads EVERY chunk that has an extraction record plus the current
      extraction scope (see ``select_span_chunks``): history is never dropped.
    - Ecosystem Company nodes are merged FIRST so MENTIONS never silently drop.
    - Embeddings reuse the notebooks' per-filer parquet caches under
      ``data/processed/embeddings/`` via ``Embedder.encode_chunks_cached``.
    - MENTIONS edges come from word-boundary alias regexes over the canonical
      entity dictionary.

    Returns the number of span rows loaded.
    """
    settings = settings or get_settings()
    embedder = embedder or Embedder()
    snapshot_id = _resolve_snapshot_id(settings, snapshot_id)
    manifest = _load_manifest(settings)
    tickers = list(tickers) if tickers else list(FILERS)

    load_ecosystem_companies(driver, snapshot_id)
    patterns = _alias_patterns()

    def mentioned(text: str) -> list[int]:
        return sorted({eid for pat, eid in patterns if pat.search(text)})

    total = 0
    for ticker in tickers:
        ch = _read_chunks(settings, ticker)
        if ch is None:
            continue
        versions, states = _ticker_freshness(manifest, ticker, ch)
        no_chunks = current_filings_without_chunks(states, set(ch["accession_no"]))
        if no_chunks:
            logger.warning("%s: current filing(s) %s have no chunks — nothing current can be retrieved "
                           "from them (run ingest/segment/chunk before build-graph)", ticker, no_chunks)
        records = _load_extraction_records(settings, ticker) or []
        spans = select_span_chunks(ch, ticker, {rec["chunk_id"] for rec in records}, versions, hist_annuals)
        if spans.empty:
            logger.warning("%s: no chunks in span scope — skipping", ticker)
            continue
        vecs = embedder.encode_chunks_cached(spans, _embeddings_cache_path(settings, ticker))
        rows = build_span_rows(spans, vecs, states, mentioned)
        _run_batched(driver, _SPAN_CYPHER, rows, snapshot_id=snapshot_id)
        total += len(rows)
        logger.info("%s: %d EvidenceSpans loaded (%d current)", ticker, len(rows),
                    sum(r["is_current"] for r in rows))
    return total


# --------------------------------------------------------------------------
# knowledge layer — notebook 09, filer-aware form from notebook 12 stage 6
# --------------------------------------------------------------------------

# Entity-resolution helpers ported from notebook 12 cell 14 (same logic as
# notebook 08). semigraph.extraction owns the canonical resolution module;
# these stay private here so graph loading has no cross-module dependency.
_LEGAL_SUFFIXES = re.compile(
    r"\b(incorporated|corporation|corp|inc|ltd|limited|llc|plc|co|company|holdings?|nv|sa|ag|kk)\b\.?",
    re.I,
)


def _normalize_name(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", s.lower())
    s = _LEGAL_SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _resolve(raw: str, alias_lookup: dict[str, str], threshold: float = 0.90) -> str | None:
    norm = _normalize_name(raw)
    if not norm:
        return None
    if norm in alias_lookup:
        return alias_lookup[norm]
    best, score = None, 0.0
    for alias, name in alias_lookup.items():
        s = SequenceMatcher(None, norm, alias).ratio()
        if s > score:
            best, score = name, s
    return best if score >= threshold else None


# notebook 12's relation MERGE with notebook 09's earliest-start_date clause
# restored on ON MATCH (accumulate evidence ids across filings AND keep the
# lineage's first-disclosure date as the citable start).
_RELATION_CYPHER = """UNWIND $rows AS row
    MATCH (a:Company {{cik: row.src}}), (t:Company {{cik: row.tgt}})
    MERGE (a)-[r:{rel_type}]->(t)
    ON CREATE SET r.start_date = date(row.date), r.status = 'Active',
                  r.evidence_chunk_ids = [row.chunk_id], r.evidence_quote = row.quote
    ON MATCH SET r.evidence_chunk_ids = CASE WHEN row.chunk_id IN r.evidence_chunk_ids
                  THEN r.evidence_chunk_ids ELSE r.evidence_chunk_ids + row.chunk_id END,
                 r.start_date = CASE WHEN date(row.date) < r.start_date
                  THEN date(row.date) ELSE r.start_date END"""

_RISK_CYPHER = """UNWIND $rows AS row
    MERGE (rf:RiskFactor {risk_id: row.risk_id})
    SET rf.summary = row.summary, rf.category = row.category, rf.embedding = row.embedding
    WITH rf, row MATCH (e:EvidenceSpan {chunk_id: row.chunk_id})
    MERGE (rf)-[:HAS_EVIDENCE {quote: row.quote}]->(e)
    WITH rf, row MATCH (filer:Company {cik: row.filer_cik})
    MERGE (filer)-[d:DISCLOSES_RISK]->(rf)
    ON CREATE SET d.start_date = date(row.date), d.status = 'Active'"""

# Freshness is re-stamped on EVERY risk of the run: an existing risk keeps its
# embedding, but its filing (or section) may have been superseded since.
_RISK_STAMP_CYPHER = """UNWIND $rows AS row
    MATCH (rf:RiskFactor {risk_id: row.risk_id})
    SET rf.filer_cik = row.filer_cik, rf.form = row.form, rf.filing_date = date(row.filing_date),
        rf.valid_from = date(row.valid_from), rf.valid_to = date(row.valid_to),
        rf.is_current = row.is_current, rf.snapshot_id = $snapshot_id"""

_PRODUCT_CYPHER = """UNWIND $rows AS row
    MERGE (p:Product {name: row.name}) SET p.type = coalesce(p.type, row.type), p.snapshot_id = $snapshot_id
    WITH p, row MATCH (e:EvidenceSpan {chunk_id: row.chunk_id})
    MERGE (p)-[:MENTIONED_IN]->(e)"""

# has_current_evidence: is ANY evidencing span current? last_evidenced_at: latest span date.
_RELATION_EVIDENCE_CYPHER = """MATCH ()-[r:%s]->()
    CALL (r) {
        UNWIND r.evidence_chunk_ids AS cid
        MATCH (e:EvidenceSpan {chunk_id: cid})
        RETURN max(e.filing_date) AS last_at,
               any(x IN collect(coalesce(e.is_current, false)) WHERE x) AS has_current
    }
    SET r.has_current_evidence = has_current, r.last_evidenced_at = last_at
    RETURN count(r) AS n""" % "|".join(RELATION_TYPES)


def stamp_relation_evidence(driver: Driver) -> int:
    """Post-load pass over ALL relation edges: ``has_current_evidence`` and
    ``last_evidenced_at``. Run after evidence spans and relations are loaded;
    returns the number of edges stamped."""
    with driver.session() as session:
        record = session.run(_RELATION_EVIDENCE_CYPHER).single()
    n = record["n"] if record else 0
    logger.info("%d relation edges stamped with evidence currency", n)
    return n


def _knowledge_rows(records: list[dict], ticker: str, chunk_meta: pd.DataFrame,
                    states: Mapping[str, FilingState], filer_cik: int,
                    canonical: dict, alias_lookup: dict[str, str]
                    ) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Relation / risk / product rows (+ dropped relations) of one filer's extraction records."""
    meta = chunk_meta.set_index("chunk_id")[["filing_date", "accession_no", "section_id"]].to_dict("index")
    rel_rows, risk_rows, prod_rows, dropped = [], [], [], []
    for rec in records:
        info = meta.get(rec["chunk_id"])
        if info is None:
            continue
        fdate = info["filing_date"]
        state = states.get(info["accession_no"])
        if state is None:
            raise ValueError(f"{ticker}: chunk {rec['chunk_id']} belongs to filing "
                             f"{info['accession_no']} which has no manifest row")
        for rel in rec["relations"]:
            src, tgt = (_resolve(rel["source_entity"], alias_lookup),
                        _resolve(rel["target_entity"], alias_lookup))
            if src and tgt and src != tgt:
                rel_rows.append({"src": canonical[src]["entity_id"], "tgt": canonical[tgt]["entity_id"],
                                 "type": rel["relation"], "chunk_id": rec["chunk_id"],
                                 "quote": rel["evidence_quote"], "date": fdate})
            else:
                dropped.append({"ticker": ticker, **rel})
        for risk in rec["risk_factors"]:
            rid = hashlib.sha1(f"{rec['chunk_id']}|{risk['summary']}".encode()).hexdigest()[:16]
            risk_rows.append({"risk_id": rid, "summary": risk["summary"], "category": risk["category"],
                              "chunk_id": rec["chunk_id"], "quote": risk["evidence_quote"], "date": fdate,
                              **risk_stamp(state, filer_cik, info["section_id"])})
        for p in rec["products"]:
            prod_rows.append({"name": p["name"], "type": p["type"], "chunk_id": rec["chunk_id"]})
    return rel_rows, risk_rows, prod_rows, dropped


def _load_ticker_knowledge(driver: Driver, embedder: Embedder, ticker: str, rel_rows: list[dict],
                           risk_rows: list[dict], prod_rows: list[dict], snapshot_id: str) -> int:
    """Write one filer's rows; embeds only risks not yet in the graph. Returns the new-risk count."""
    with driver.session() as s:
        have_risks = {r["id"] for r in s.run("MATCH (rf:RiskFactor) RETURN rf.risk_id AS id")}
    new_risks = [r for r in risk_rows if r["risk_id"] not in have_risks]
    if new_risks:
        for r, v in zip(new_risks, embedder.encode_passages([r["summary"] for r in new_risks])):
            r["embedding"] = v.tolist()
    for rt in RELATION_TYPES:
        batch = [r for r in rel_rows if r["type"] == rt]
        if batch:
            _run_batched(driver, _RELATION_CYPHER.format(rel_type=rt), batch)
    if new_risks:
        _run_batched(driver, _RISK_CYPHER, new_risks)
    _run_batched(driver, _RISK_STAMP_CYPHER,
                 [{k: r[k] for k in ("risk_id", "filer_cik", "form", "filing_date", "valid_from",
                                     "valid_to", "is_current")} for r in risk_rows],
                 snapshot_id=snapshot_id)
    if prod_rows:
        _run_batched(driver, _PRODUCT_CYPHER, prod_rows, snapshot_id=snapshot_id)
    logger.info("%s: +%d relation instances, +%d new risks (%d stamped), %d product mentions",
                ticker, len(rel_rows), len(new_risks), len(risk_rows), len(prod_rows))
    return len(new_risks)


def load_knowledge(driver: Driver, settings: Settings | None = None,
                   embedder: Embedder | None = None,
                   tickers: list[str] | None = None, *,
                   snapshot_id: str | None = None) -> dict[str, int]:
    """Load LLM-extracted knowledge into the graph, filer-aware (ported from
    notebook 12 stage 6, which generalized notebook 09's NVDA-hardcoded load).

    Per filer, reads ``{ticker}_extractions.jsonl`` and MERGEs:

    - relation edges per ``(source, type, target)``, accumulating
      ``evidence_chunk_ids`` across filings and keeping the EARLIEST
      ``start_date``; ``status`` starts 'Active' (notebook 13 manages closure)
    - RiskFactor nodes (``risk_id = sha1(chunk_id|summary)[:16]``) with summary
      embeddings + ``HAS_EVIDENCE {quote}`` edges + ``DISCLOSES_RISK`` from the
      CORRECT filer (``filer_cik`` from the filer's own chunk parquet — the
      filer-aware fix over notebook 09's NVDA hardcoding). Every risk (new or
      existing) is re-stamped with its filing's freshness properties
    - Product nodes + MENTIONED_IN provenance

    Afterwards every relation edge gets ``has_current_evidence`` / ``last_evidenced_at``
    (run AFTER ``load_evidence_spans``).

    Unresolved entity names are logged to ``extractions/resolution_report_universe.parquet``
    (full-universe runs only, so a partial run never clobbers it).

    Returns counts: relations, new_risks, product_mentions, unresolved.
    """
    settings = settings or get_settings()
    embedder = embedder or Embedder()
    snapshot_id = _resolve_snapshot_id(settings, snapshot_id)
    manifest = _load_manifest(settings)
    full_universe = tickers is None
    tickers = list(tickers) if tickers else list(FILERS)

    canonical = load_canonical_entities()
    alias_lookup = {_normalize_name(a): n
                    for n, spec in canonical.items() for a in [n] + spec["aliases"]}

    totals = {"relations": 0, "new_risks": 0, "product_mentions": 0, "unresolved": 0}
    dropped_all: list[dict] = []
    for ticker in tickers:
        records = _load_extraction_records(settings, ticker)
        if records is None:
            logger.warning("%s: no extraction jsonl — skipping knowledge load", ticker)
            continue
        ch = _read_chunks(settings, ticker)
        if ch is None:
            continue
        _, states = _ticker_freshness(manifest, ticker, ch)
        rel_rows, risk_rows, prod_rows, dropped = _knowledge_rows(
            records, ticker, ch, states, int(ch["cik"].iloc[0]), canonical, alias_lookup)
        dropped_all += dropped
        totals["new_risks"] += _load_ticker_knowledge(
            driver, embedder, ticker, rel_rows, risk_rows, prod_rows, snapshot_id)
        totals["relations"] += len(rel_rows)
        totals["product_mentions"] += len(prod_rows)

    stamp_relation_evidence(driver)
    totals["unresolved"] = len(dropped_all)
    if full_universe:
        report = settings.extractions_dir / "resolution_report_universe.parquet"
        pd.DataFrame(dropped_all).to_parquet(report, index=False)
        logger.info("unresolved entities logged: %d -> %s (dictionary growth candidates)",
                    len(dropped_all), report.name)
    return totals


# --------------------------------------------------------------------------
# export controls — notebook 12 stage 7
# --------------------------------------------------------------------------

_EXPORT_CONTROL_CYPHER = """UNWIND $rows AS row
    MERGE (x:ExportControl {rule_id: row.document_number})
    SET x.title = row.title, x.date = date(row.publication_date), x.url = row.html_url,
        x.abstract = left(coalesce(row.abstract, ''), 1000),
        x.kind = row.kind, x.topics = row.topics, x.relevant = row.relevant,
        x.snapshot_id = $snapshot_id"""

_AFFECTED_BY_CYPHER = """UNWIND $rows AS row
    MATCH (c:Company {cik: row.cik}), (x:ExportControl {rule_id: row.rule_id})
    MERGE (c)-[r:AFFECTED_BY]->(x)
    SET r.start_date = date(row.date), r.status = 'Active', r.evidence_chunk_ids = row.chunks"""


def load_export_controls(driver: Driver, settings: Settings | None = None, *,
                         snapshot_id: str | None = None) -> dict[str, int]:
    """MERGE Federal Register BIS rules as ExportControl nodes (with ``kind`` /
    ``topics`` / ``relevant``), then link exposed companies via AFFECTED_BY for
    the relevant rules only (ported from notebook 12 stage 7).

    Reads the cached ``data/raw/federal_register_bis_rules.json`` written by
    the acquisition stage (semigraph.ingestion owns the Federal Register
    fetch); raises FileNotFoundError if absent so this module stays free of
    network calls. A legacy cache without per-rule classification keeps the
    old behaviour (see ``normalize_rule``).

    Returns {"rules": n, "relevant_rules": n, "affected_by": n, "exposed_companies": n}.
    """
    settings = settings or get_settings()
    snapshot_id = _resolve_snapshot_id(settings, snapshot_id)
    fr_cache = settings.raw_dir / "federal_register_bis_rules.json"
    if not fr_cache.exists():
        raise FileNotFoundError(
            f"{fr_cache} not found — fetch BIS rules first (semigraph.ingestion "
            "federal-register acquisition, notebook 12 stage 7)."
        )
    rules = [normalize_rule(r) for r in json.loads(fr_cache.read_text(encoding="utf-8"))["results"]]

    _run_batched(driver, _EXPORT_CONTROL_CYPHER, rules, snapshot_id=snapshot_id)
    n_edges, n_exposed = link_affected_by(driver, rules)
    n_relevant = sum(r["relevant"] for r in rules)
    logger.info("%d ExportControl nodes (%d relevant), %d AFFECTED_BY edges (%d companies disclose "
                "current export-control risks)", len(rules), n_relevant, n_edges, n_exposed)
    return {"rules": len(rules), "relevant_rules": n_relevant, "affected_by": n_edges,
            "exposed_companies": n_exposed}


def _current_export_control_evidence(driver: Driver) -> dict[int, list[dict]]:
    """company cik -> evidence chunks of its CURRENT 'Export Controls' risks."""
    rows = run_cypher(driver, """
        MATCH (c:Company)-[:DISCLOSES_RISK]->(rf:RiskFactor)-[:HAS_EVIDENCE]->(e:EvidenceSpan)
        WHERE toLower(rf.category) CONTAINS $category AND rf.is_current = true
        RETURN DISTINCT c.cik AS cik, e.chunk_id AS chunk_id, e.text AS text
        ORDER BY cik, chunk_id""", category=EXPORT_CONTROLS_CATEGORY)
    exposures: dict[int, list[dict]] = {}
    for row in rows:
        exposures.setdefault(row["cik"], []).append({"chunk_id": row["chunk_id"], "text": row["text"]})
    return exposures


def link_affected_by(driver: Driver, rules: list[dict]) -> tuple[int, int]:
    """Create Company-[:AFFECTED_BY]->ExportControl edges (ported from
    notebook 12 stage 7; recomputed wholesale on every run).

    HONEST CAVEAT — this is a KEYWORD HEURISTIC, documented as such in the
    notebook and the M6 backlog: a company is linked to a RELEVANT rule when it
    discloses a CURRENT export-control risk (category contains 'export control') whose evidence text
    contains keywords for the rule's kind (``KIND_EVIDENCE_KEYWORDS``; legacy
    rules use ``TOPIC_KEYWORDS`` on the title). It is keyword co-occurrence, not
    verified causal impact — expect false positives/negatives; refine when
    evaluation demands it. At most ``MAX_AFFECTED_BY_PER_COMPANY`` (the most
    recent) rules per company; the matching evidence chunk ids ride on the
    edge for audit. Existing AFFECTED_BY edges are removed first so a rule that
    is no longer relevant, or was pushed out by the cap, cannot linger.

    Returns (n_edges, n_exposed_companies).
    """
    exposures = _current_export_control_evidence(driver)
    edges = build_affected_by_rows(exposures, [normalize_rule(r) for r in rules])
    with driver.session() as session:
        session.run("MATCH ()-[r:AFFECTED_BY]->() DELETE r")
    _run_batched(driver, _AFFECTED_BY_CYPHER, edges)
    return len(edges), len(exposures)


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------

def _iso_date(value: date | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return (value if isinstance(value, date) else date.fromisoformat(value)).isoformat()


def snapshot_props(snapshot_id: str, as_of: date | str | None, counts: Mapping,
                   *, code_version: str = __version__) -> dict:
    """Parameters of the ``Snapshot`` node (``counts`` is stored as a sorted JSON string)."""
    return {"id": snapshot_id, "as_of": _iso_date(as_of), "code_version": code_version,
            "counts": json.dumps(dict(counts), sort_keys=True, default=str)}


def stamp_snapshot(driver: Driver, snapshot_id: str, as_of: date | str | None = None,
                   counts: Mapping | None = None, *, code_version: str = __version__) -> str:
    """MERGE the ``(:Snapshot {id, as_of, created_at, code_version, counts})`` node
    the loaders' ``snapshot_id`` stamps point at. ``created_at`` is set once;
    the rest is refreshed. Returns the snapshot id."""
    params = snapshot_props(snapshot_id, as_of, counts or {}, code_version=code_version)
    with driver.session() as session:
        session.run(
            """MERGE (s:Snapshot {id: $id})
            ON CREATE SET s.created_at = datetime()
            SET s.as_of = CASE WHEN $as_of IS NULL THEN null ELSE date($as_of) END,
                s.code_version = $code_version, s.counts = $counts""", **params)
    logger.info("snapshot %s stamped (as_of %s)", snapshot_id, params["as_of"])
    return snapshot_id
