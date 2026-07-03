"""Graph loaders — deterministic layer, evidence layer, knowledge layer,
export controls (ported from notebooks 06, 09 and 12).

All loaders are idempotent MERGE-based and write in 100-row UNWIND batches
(the notebooks' transaction-size convention), reading the same data-lake
files the notebooks produced under ``settings.data_dir``:

- ``data/raw/edgar/company_tickers.json`` + ``manifest_universe.json``
- ``data/interim/section_texts/{TICKER}_section_texts.parquet``
- ``data/processed/chunks/{TICKER}_chunks.parquet``  (NVDA: ``nvda_chunks.parquet``)
- ``data/processed/embeddings/{TICKER}_chunk_embeddings.parquet``
- ``data/processed/extractions/{ticker}_extractions.jsonl``
- ``data/processed/xbrl/{TICKER}_key_metrics.parquet``
- ``data/raw/federal_register_bis_rules.json``

Samsung is an entity-only Company node: it doesn't file with the SEC, so it
appears in ``UNIVERSE`` (with a synthetic negative cik) but not in ``FILERS``
and never gets Filing/Metric/EvidenceSpan children.
"""

import hashlib
import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
from neo4j import Driver

from ..artifacts import load_canonical_entities
from ..config import Settings, get_settings
from ..embeddings import Embedder

logger = logging.getLogger("semigraph.graph.loaders")

BATCH_SIZE = 100  # UNWIND batch size keeping transactions small (notebooks 09/12)

# ticker: (canonical name, tier) — the 14-company universe (notebook 06).
# Samsung has no CIK (not an SEC filer) and gets a synthetic negative key.
UNIVERSE = {
    "MSFT": ("Microsoft", "Hyperscaler"), "AMZN": ("Amazon", "Hyperscaler"),
    "GOOGL": ("Alphabet", "Hyperscaler"), "META": ("Meta", "Hyperscaler"),
    "NVDA": ("Nvidia", "Silicon Designer"), "AMD": ("AMD", "Silicon Designer"),
    "AVGO": ("Broadcom", "Silicon Designer"), "QCOM": ("Qualcomm", "Silicon Designer"),
    "INTC": ("Intel", "IDM"), "TSM": ("TSMC", "Manufacturer"), "ASML": ("ASML", "Manufacturer"),
    "MU": ("Micron", "Memory"), "SSNLF": ("Samsung", "Memory"), "AAPL": ("Apple", "Ecosystem Anchor"),
}

# ticker: (canonical name, annual form, quarterly form or None) — the 13 SEC
# filers (notebook 12). TSMC and ASML file 20-F (annual only).
FILERS = {
    "NVDA": ("Nvidia", "10-K", "10-Q"), "AMD": ("AMD", "10-K", "10-Q"),
    "INTC": ("Intel", "10-K", "10-Q"), "AVGO": ("Broadcom", "10-K", "10-Q"),
    "QCOM": ("Qualcomm", "10-K", "10-Q"), "MU": ("Micron", "10-K", "10-Q"),
    "AAPL": ("Apple", "10-K", "10-Q"), "MSFT": ("Microsoft", "10-K", "10-Q"),
    "AMZN": ("Amazon", "10-K", "10-Q"), "GOOGL": ("Alphabet", "10-K", "10-Q"),
    "META": ("Meta", "10-K", "10-Q"),
    "TSM": ("TSMC", "20-F", None), "ASML": ("ASML", "20-F", None),
}

RELATION_TYPES = ["SUPPLIES_TO", "DEPENDS_ON", "CUSTOMER_OF", "COMPETES_WITH"]
RISK_SECTIONS = {"10-K": "I.1A", "10-Q": "II.1A", "20-F": "I.3"}
HIST_ANNUALS = 1  # prior annuals contributing risk-only chunks (notebook 12)

# rule-title keyword -> evidence-text keywords (notebook 12 stage 7 heuristic)
TOPIC_KEYWORDS = {
    "entity list": ["entity list"],
    "advanced computing": ["advanced computing", "ai chip", "accelerator"],
    "semiconductor manufacturing": ["manufacturing equipment", "semiconductor manufacturing"],
    "artificial intelligence": ["artificial intelligence", "ai diffusion"],
}


# --------------------------------------------------------------------------
# shared plumbing
# --------------------------------------------------------------------------

def _run_batched(driver: Driver, query: str, rows: list[dict],
                 batch_size: int = BATCH_SIZE) -> None:
    """Run an UNWIND query over rows in small batches (notebooks 09/12)."""
    with driver.session() as session:
        for i in range(0, len(rows), batch_size):
            session.run(query, rows=rows[i : i + batch_size])


def _chunks_path(settings: Settings, ticker: str) -> Path:
    # notebook 04's NVDA output kept its lowercase PoC name (notebook 12 stage 3)
    name = "nvda_chunks.parquet" if ticker == "NVDA" else f"{ticker}_chunks.parquet"
    return settings.chunks_dir / name


def _section_texts_path(settings: Settings, ticker: str) -> Path:
    name = ("nvda_section_texts.parquet" if ticker == "NVDA"
            else f"{ticker}_section_texts.parquet")
    return settings.interim_dir / "section_texts" / name


def _embeddings_cache_path(settings: Settings, ticker: str) -> Path:
    # notebook 09 cached NVDA under its lowercase PoC name; notebook 12 used
    # {TICKER}_chunk_embeddings.parquet for every other filer
    name = ("nvda_chunk_embeddings.parquet" if ticker == "NVDA"
            else f"{ticker}_chunk_embeddings.parquet")
    return settings.embeddings_dir / name


def _extractions_path(settings: Settings, ticker: str) -> Path:
    name = ("nvda_extractions.jsonl" if ticker == "NVDA"
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

def load_companies(driver: Driver, settings: Settings | None = None) -> int:
    """MERGE the 14-company universe as Company nodes (ported from notebook 06).

    CIKs come from SEC's ``company_tickers.json`` (downloaded by notebook 01's
    acquisition); companies without a CIK (Samsung) get a synthetic negative
    key so the uniqueness constraint still holds — Samsung stays entity-only,
    with no filings.
    """
    settings = settings or get_settings()
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
        SET c.ticker = row.ticker, c.name = row.name, c.tier = row.tier, c.sec_filer = row.sec_filer""",
        company_rows,
    )
    logger.info("%d universe Company nodes merged", len(company_rows))
    return len(company_rows)


def load_filings_and_sections(driver: Driver, settings: Settings | None = None,
                              tickers: list[str] | None = None) -> tuple[int, int]:
    """MERGE Filing nodes + FILED edges and FilingSection nodes + HAS_SECTION
    edges from the acquisition manifest and section-text parquets
    (ported from notebook 06, generalized by notebook 12 stage 4).

    Returns (n_filing_rows, n_section_rows). Requires load_companies first
    (FILED matches on Company.ticker).
    """
    settings = settings or get_settings()
    manifest = _load_manifest(settings)
    tickers = list(tickers) if tickers else list(FILERS)

    filing_rows = [row for t in tickers for row in manifest.get(t, [])]
    _run_batched(
        driver,
        """UNWIND $rows AS row
        MATCH (c:Company {ticker: row.ticker})
        MERGE (f:Filing {accession_no: row.accession_no})
        SET f.form = row.form, f.filing_date = date(row.filing_date), f.url = row.source_url
        MERGE (c)-[:FILED {date: date(row.filing_date)}]->(f)""",
        filing_rows,
    )

    section_rows = []
    for ticker in tickers:
        st = pd.read_parquet(_section_texts_path(settings, ticker))
        section_rows += [
            {"section_key": f"{r.accession_no}:{r.section_id}", "accession_no": r.accession_no,
             "section_id": r.section_id, "title": r.section_title, "n_chars": int(r.n_chars)}
            for r in st.itertuples()
        ]
    _run_batched(
        driver,
        """UNWIND $rows AS row
        MATCH (f:Filing {accession_no: row.accession_no})
        MERGE (s:FilingSection {section_key: row.section_key})
        SET s.section_id = row.section_id, s.title = row.title, s.n_chars = row.n_chars
        MERGE (f)-[:HAS_SECTION]->(s)""",
        section_rows,
    )
    logger.info("%d filings, %d sections loaded for %d filers",
                len(filing_rows), len(section_rows), len(tickers))
    return len(filing_rows), len(section_rows)


def load_metrics(driver: Driver, settings: Settings | None = None,
                 tickers: list[str] | None = None) -> int:
    """MERGE Metric nodes + REPORTS_METRIC edges from the curated XBRL parquets
    (ported from notebook 06, generalized by notebook 12 stage 4).

    The no-LLM-numbers rule in action: every value comes straight from XBRL.
    ``metric_id = {cik}:{metric}:{period_end}``; the ``accn`` provenance rides
    on the edge so every number is traceable to its first-disclosing filing.
    """
    settings = settings or get_settings()
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
            m.period_start = date(row.period_start), m.period_end = date(row.period_end)
        MERGE (c)-[rel:REPORTS_METRIC]->(m) SET rel.accession_no = row.accn""",
        metric_rows,
    )
    logger.info("%d metric-periods loaded", len(metric_rows))
    return len(metric_rows)


# --------------------------------------------------------------------------
# evidence layer — notebook 09, filer-aware form from notebook 12 stage 6
# --------------------------------------------------------------------------

def load_ecosystem_companies(driver: Driver) -> int:
    """MERGE canonical-dictionary entities beyond the 14 as tier-'Ecosystem'
    Company nodes (ported from notebook 09).

    Must run BEFORE MENTIONS edges are created, or mentions of e.g. SK Hynix
    would silently drop (the MENTIONS MATCH would find no node).
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
            c.ticker = coalesce(c.ticker, row.ticker)""",
        ecosystem_rows,
    )
    logger.info("%d ecosystem Company nodes merged", len(ecosystem_rows))
    return len(ecosystem_rows)


def _span_scope(ch: pd.DataFrame, ticker: str,
                hist_annuals: int = HIST_ANNUALS) -> pd.DataFrame:
    """Which chunks become EvidenceSpans, in chunk-parquet order.

    Ported from notebook 12's ``extraction_scope`` (stage 5), which stage 6
    reused to decide what to embed and load: latest annual (all kept
    sections) + latest quarterly + ``hist_annuals`` prior annuals (risk
    sections only — the history that powers notebook 13's lineages).

    NVDA is the exception: notebook 09 loaded ALL PoC chunks as spans before
    notebook 12 existed, and the proven graph (and NVDA's embedding cache)
    contains all of them — so NVDA returns the full frame.

    Row order is preserved from the chunk parquet so
    ``Embedder.encode_chunks_cached`` recognizes the notebooks' caches.
    """
    if ticker == "NVDA":
        return ch
    if ch.empty or "form" not in ch.columns:
        return ch.iloc[0:0]
    _, annual_form, quarterly_form = FILERS[ticker]
    annuals = sorted(ch[ch["form"] == annual_form]["accession_no"].unique(),
                     key=lambda a: ch[ch["accession_no"] == a]["filing_date"].iloc[0])
    parts = []
    if annuals:
        parts.append(ch[ch["accession_no"] == annuals[-1]])                # latest annual: everything
        risk = RISK_SECTIONS[annual_form]
        hist = annuals[-(1 + hist_annuals):-1]
        parts.append(ch[ch["accession_no"].isin(hist) & (ch["section_id"] == risk)])  # history: risks only
    if quarterly_form:
        qs = ch[ch["form"] == quarterly_form]
        if len(qs):
            parts.append(qs[qs["accession_no"] == qs["accession_no"].max()])
    if not parts:
        return ch.iloc[0:0]
    scope_ids = set(pd.concat(parts, ignore_index=True)["chunk_id"])
    return ch[ch["chunk_id"].isin(scope_ids)]  # parquet order, cache-compatible


_SPAN_CYPHER = """UNWIND $rows AS row
    MERGE (e:EvidenceSpan {chunk_id: row.chunk_id})
    SET e.text = row.text, e.kind = row.kind, e.sub_heading = row.sub_heading,
        e.char_start = row.char_start, e.char_end = row.char_end,
        e.n_tokens = row.n_tokens, e.source_url = row.source_url, e.embedding = row.embedding
    WITH e, row MATCH (sec:FilingSection {section_key: row.section_key})
    MERGE (e)-[:FROM_SECTION]->(sec)
    WITH e, row UNWIND row.mentions AS eid
    MATCH (c:Company {cik: eid}) MERGE (e)-[:MENTIONS]->(c)"""


def load_evidence_spans(driver: Driver, settings: Settings | None = None,
                        embedder: Embedder | None = None,
                        tickers: list[str] | None = None,
                        hist_annuals: int = HIST_ANNUALS) -> int:
    """MERGE EvidenceSpan nodes with embeddings + FROM_SECTION containment +
    MENTIONS company anchors (ported from notebook 09, filer-aware form from
    notebook 12 stage 6).

    - Ecosystem Company nodes are merged FIRST so MENTIONS never silently drop.
    - Embeddings reuse the notebooks' per-filer parquet caches under
      ``data/processed/embeddings/`` via ``Embedder.encode_chunks_cached``.
    - MENTIONS edges come from word-boundary alias regexes over the canonical
      entity dictionary.

    Returns the number of span rows loaded.
    """
    settings = settings or get_settings()
    embedder = embedder or Embedder()
    tickers = list(tickers) if tickers else list(FILERS)

    load_ecosystem_companies(driver)
    patterns = _alias_patterns()

    def mentioned(text: str) -> list[int]:
        return sorted({eid for pat, eid in patterns if pat.search(text)})

    total = 0
    for ticker in tickers:
        ch = pd.read_parquet(_chunks_path(settings, ticker))
        spans = _span_scope(ch, ticker, hist_annuals)
        if spans.empty:
            logger.warning("%s: no chunks in span scope — skipping", ticker)
            continue
        vecs = embedder.encode_chunks_cached(
            spans, _embeddings_cache_path(settings, ticker)
        )
        rows = [
            {"chunk_id": r.chunk_id, "text": r.text, "kind": r.kind,
             "section_key": f"{r.accession_no}:{r.section_id}", "sub_heading": r.sub_heading,
             "char_start": int(r.char_start), "char_end": int(r.char_end),
             "n_tokens": int(r.n_tokens), "source_url": r.source_url,
             "embedding": v.tolist(), "mentions": mentioned(r.text)}
            for r, v in zip(spans.itertuples(), vecs)
        ]
        _run_batched(driver, _SPAN_CYPHER, rows)
        total += len(rows)
        logger.info("%s: %d EvidenceSpans loaded", ticker, len(rows))
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

_PRODUCT_CYPHER = """UNWIND $rows AS row
    MERGE (p:Product {name: row.name}) SET p.type = coalesce(p.type, row.type)
    WITH p, row MATCH (e:EvidenceSpan {chunk_id: row.chunk_id})
    MERGE (p)-[:MENTIONED_IN]->(e)"""


def load_knowledge(driver: Driver, settings: Settings | None = None,
                   embedder: Embedder | None = None,
                   tickers: list[str] | None = None) -> dict[str, int]:
    """Load LLM-extracted knowledge into the graph, filer-aware (ported from
    notebook 12 stage 6, which generalized notebook 09's NVDA-hardcoded load).

    Per filer, reads ``{ticker}_extractions.jsonl`` and MERGEs:

    - relation edges per ``(source, type, target)``, accumulating
      ``evidence_chunk_ids`` across filings and keeping the EARLIEST
      ``start_date``; ``status`` starts 'Active' (notebook 13 manages closure)
    - RiskFactor nodes (``risk_id = sha1(chunk_id|summary)[:16]``) with summary
      embeddings + ``HAS_EVIDENCE {quote}`` edges + ``DISCLOSES_RISK`` from the
      CORRECT filer (``filer_cik`` from the filer's own chunk parquet — the
      filer-aware fix over notebook 09's NVDA hardcoding)
    - Product nodes + MENTIONED_IN provenance

    Unresolved entity names are logged to
    ``extractions/resolution_report_universe.parquet`` (only on full-universe
    runs, so a partial run never clobbers the universe report).

    Returns counts: relations, new_risks, product_mentions, unresolved.
    """
    settings = settings or get_settings()
    embedder = embedder or Embedder()
    full_universe = tickers is None
    tickers = list(tickers) if tickers else list(FILERS)

    canonical = load_canonical_entities()
    alias_lookup = {_normalize_name(a): n
                    for n, spec in canonical.items() for a in [n] + spec["aliases"]}

    totals = {"relations": 0, "new_risks": 0, "product_mentions": 0, "unresolved": 0}
    dropped_all: list[dict] = []
    for ticker in tickers:
        jl = _extractions_path(settings, ticker)
        if not jl.exists():
            logger.warning("%s: no extraction jsonl — skipping knowledge load", ticker)
            continue
        recs = [json.loads(line) for line in jl.open(encoding="utf-8") if line.strip()]
        ch = pd.read_parquet(_chunks_path(settings, ticker))
        ch_meta = ch.set_index("chunk_id")["filing_date"].to_dict()
        filer_cik = int(ch["cik"].iloc[0])

        rel_rows, risk_rows, prod_rows = [], [], []
        for rec in recs:
            fdate = ch_meta.get(rec["chunk_id"])
            if fdate is None:
                continue
            for rel in rec["relations"]:
                src, tgt = (_resolve(rel["source_entity"], alias_lookup),
                            _resolve(rel["target_entity"], alias_lookup))
                if src and tgt and src != tgt:
                    rel_rows.append({"src": canonical[src]["entity_id"],
                                     "tgt": canonical[tgt]["entity_id"],
                                     "type": rel["relation"], "chunk_id": rec["chunk_id"],
                                     "quote": rel["evidence_quote"], "date": fdate})
                else:
                    dropped_all.append({"ticker": ticker, **rel})
            for risk in rec["risk_factors"]:
                rid = hashlib.sha1(f"{rec['chunk_id']}|{risk['summary']}".encode()).hexdigest()[:16]
                risk_rows.append({"risk_id": rid, "summary": risk["summary"],
                                  "category": risk["category"], "chunk_id": rec["chunk_id"],
                                  "quote": risk["evidence_quote"], "date": fdate,
                                  "filer_cik": filer_cik})
            for p in rec["products"]:
                prod_rows.append({"name": p["name"], "type": p["type"], "chunk_id": rec["chunk_id"]})

        with driver.session() as s:
            have_risks = {r["id"] for r in s.run("MATCH (rf:RiskFactor) RETURN rf.risk_id AS id")}
        new_risks = [r for r in risk_rows if r["risk_id"] not in have_risks]
        if new_risks:
            r_embs = embedder.encode_passages([r["summary"] for r in new_risks])
            for r, v in zip(new_risks, r_embs):
                r["embedding"] = v.tolist()

        for rt in RELATION_TYPES:
            b = [r for r in rel_rows if r["type"] == rt]
            if b:
                _run_batched(driver, _RELATION_CYPHER.format(rel_type=rt), b)
        if new_risks:
            _run_batched(driver, _RISK_CYPHER, new_risks)
        if prod_rows:
            _run_batched(driver, _PRODUCT_CYPHER, prod_rows)

        totals["relations"] += len(rel_rows)
        totals["new_risks"] += len(new_risks)
        totals["product_mentions"] += len(prod_rows)
        logger.info("%s: +%d relation instances, +%d risks, %d product mentions",
                    ticker, len(rel_rows), len(new_risks), len(prod_rows))

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

def load_export_controls(driver: Driver, settings: Settings | None = None) -> dict[str, int]:
    """MERGE Federal Register BIS rules as ExportControl nodes, then link
    exposed companies via AFFECTED_BY (ported from notebook 12 stage 7).

    Reads the cached ``data/raw/federal_register_bis_rules.json`` written by
    the acquisition stage (semigraph.ingestion owns the Federal Register
    fetch); raises FileNotFoundError if absent so this module stays free of
    network calls.

    Returns {"rules": n, "affected_by": n, "exposed_companies": n}.
    """
    settings = settings or get_settings()
    fr_cache = settings.raw_dir / "federal_register_bis_rules.json"
    if not fr_cache.exists():
        raise FileNotFoundError(
            f"{fr_cache} not found — fetch BIS rules first (semigraph.ingestion "
            "federal-register acquisition, notebook 12 stage 7)."
        )
    rules = json.loads(fr_cache.read_text(encoding="utf-8"))["results"]

    _run_batched(
        driver,
        """UNWIND $rows AS row
        MERGE (x:ExportControl {rule_id: row.document_number})
        SET x.title = row.title, x.date = date(row.publication_date), x.url = row.html_url,
            x.abstract = left(coalesce(row.abstract, ''), 1000)""",
        rules,
    )
    n_edges, n_exposed = link_affected_by(driver, rules)
    logger.info("%d ExportControl nodes, %d AFFECTED_BY edges (%d companies disclose "
                "export-control risks)", len(rules), n_edges, n_exposed)
    return {"rules": len(rules), "affected_by": n_edges, "exposed_companies": n_exposed}


def link_affected_by(driver: Driver, rules: list[dict]) -> tuple[int, int]:
    """Create Company-[:AFFECTED_BY]->ExportControl edges (ported from
    notebook 12 stage 7).

    HONEST CAVEAT — this is a KEYWORD HEURISTIC, documented as such in the
    notebook and the M6 backlog: a company is linked to a rule when it
    discloses an 'Export Controls'-category risk whose evidence text contains
    the rule-topic's keywords (``TOPIC_KEYWORDS``). It is keyword co-occurrence,
    not verified causal impact — expect false positives/negatives; refine when
    evaluation demands it. Evidence chunk ids ride on the edge for audit.

    Returns (n_edges_proposed, n_exposed_companies).
    """
    with driver.session() as s:
        # companies with Export Controls-category risks, plus their evidence text
        exposures = s.run(
            """MATCH (c:Company)-[:DISCLOSES_RISK]->(rf:RiskFactor {category: 'Export Controls'})
                     -[:HAS_EVIDENCE]->(e:EvidenceSpan)
               RETURN c.cik AS cik, c.name AS name, collect(DISTINCT e.chunk_id) AS chunks,
                      left(reduce(t = '', x IN collect(e.text)[..5] | t + ' ' + x), 8000) AS text"""
        ).data()
    edges = []
    for exp in exposures:
        text_l = exp["text"].lower()
        for rule in rules:
            title_l = rule["title"].lower()
            for topic, ev_keywords in TOPIC_KEYWORDS.items():
                if topic in title_l and any(k in text_l for k in ev_keywords):
                    edges.append({"cik": exp["cik"], "rule_id": rule["document_number"],
                                  "chunks": exp["chunks"][:10], "date": rule["publication_date"]})
                    break
    _run_batched(
        driver,
        """UNWIND $rows AS row
        MATCH (c:Company {cik: row.cik}), (x:ExportControl {rule_id: row.rule_id})
        MERGE (c)-[r:AFFECTED_BY]->(x)
        ON CREATE SET r.start_date = date(row.date), r.status = 'Active', r.evidence_chunk_ids = row.chunks""",
        edges,
    )
    return len(edges), len(exposures)
