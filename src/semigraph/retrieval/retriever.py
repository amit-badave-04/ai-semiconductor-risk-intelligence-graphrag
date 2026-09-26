"""Retrieval strategies over the semigraph Neo4j graph.

Ported from notebook 14 (the authoritative final versions, benchmark-verified
at M6: hybrid 100% correct / 0.865 faithful / 0 hallucinated citations), which
finalized notebook 10's entity-first hybrid strategy; hardened in v2 M1 (C2).

Requires Neo4j >= 2026.02 (in-index ``SEARCH ... WHERE``) and a graph whose vector
indexes were created ``WITH [<filter properties>]`` (see ``semigraph.graph.schema``):
``evidence_embedding`` filters on ``retrievable``; ``risk_embedding`` on ``filer_cik``
and ``is_current``. A graph built by v1 has neither the properties nor the index
declarations and must be rebuilt first.

Battle scars preserved — each cost a failed run, do not relax:

- Neo4j 2026.x DEPRECATES ``db.index.vector.queryNodes`` (notebooks 09-11
  still used it and logged deprecation warnings). Every vector query here
  uses the SEARCH clause grammar verified live::

      MATCH (n:Label)
      SEARCH n IN (VECTOR INDEX index_name FOR $vec WHERE n.prop = $x AND n.flag = true
                   LIMIT k) SCORE AS score

  The MATCH must bind exactly ONE variable. A ``WHERE`` INSIDE the parentheses is
  an in-index pre-filter (available since 2026.02): it is applied while the index
  is searched, so ``LIMIT k`` is still filled with k qualifying rows. It may only
  reference properties declared in the index's ``WITH [...]`` list at creation.
  Anything written AFTER the SEARCH clause (later ``MATCH``/``WHERE``) still
  composes as a post-filter and can shrink the result below k.

- Inside ``SEARCH ... WHERE`` use ONLY ``=``, ``<``, ``>``, ``<=``, ``>=`` joined by
  ``AND``. Never ``IN``, ``OR``, ``NOT``, ``<>`` or ``IS NULL`` there: ``IN`` needs
  Neo4j 2026.06 and the local server is 2026.05. So membership over several
  anchors is done either outside the SEARCH (``c.cik IN $ids`` after the search
  clause, used for the excerpts) or by one SEARCH per anchor (active risks).
  ``tests/test_retrieval_pure.py`` pins this grammar.

- Relationship traversal must never pass THROUGH an ``ExportControl`` node: a rule
  is linked to every company exposed to it, so ``Company->ExportControl->Company``
  paths connect every company to every other and the prompt explodes (v1 averaged
  ~78 edge lines per answer, unbounded). Company relations and ``AFFECTED_BY`` are
  therefore two separate queries; ``AFFECTED_BY`` is only ever hop 1 from a
  company and is capped per company.

- Hybrid retrieval surfaces the TEXT-VERIFIED risk-item layer (M1b): per company, the risk items REMOVED from,
  ADDED to and REWORDED in the current annual filing against the annual filing it replaced, read from the
  ``RiskItem`` nodes (``removed_in`` / ``is_new`` / ``SUCCEEDED_BY {kind}``), never from the LLM-summary lineage
  clustering that marked risks "dropped" although their text was still in the newer filing (live audit,
  2026-09-26). The first eval run scored temporal questions 0.67/0.00 because retrieval never exposed what the
  graph knew.

- Metrics are chosen PER METRIC (the last fiscal periods of each), never "the newest N rows across metrics": that
  query returned old years unevenly and let an answer pair FY2020 revenue with FY2021 net income.

- An ``AFFECTED_BY`` row is a Federal Register rule linked to a company by a keyword heuristic: it is external to
  the company's disclosure. It stays in ``edges`` (relation ``AFFECTED_BY``) with its id, date and link
  provenance, and ``build_blocks`` presents it in its own block.

This module is importable without a Neo4j driver; functions receive one.
"""

import logging
import re
from collections.abc import Mapping
from functools import lru_cache

from ..artifacts import load_canonical_entities

logger = logging.getLogger("semigraph.retrieval")

# Default anchor when no canonical entity is detected in the question:
# Nvidia, the PoC filer (notebook 10 convention, kept through notebook 14).
# v2 M2 replaces this with a proper no-anchor mode; until then ``hybrid_retrieve``
# reports the fallback explicitly via ``anchor_defaulted``.
DEFAULT_ANCHOR_CIK = 1045810

# --- tunables (named, and pinned by tests) ---
RULES_PER_COMPANY = 8           # newest AFFECTED_BY rules per company in the external-events block
# Rules of the anchors' direct neighbours are OFF: each rule line is ~100 tokens and, with ~26 relevant
# rules, 12 per company over an anchor plus 16 neighbours added ~8k tokens per answer (cost regression
# vs v1). M2 re-adds them behind a relevance ranking.
NEIGHBOUR_RULES = False
ACTIVE_RISKS_PER_ANCHOR = 20    # ANN candidates per anchor company before the Active filter
ACTIVE_RISKS_TOP = 6            # active-risk rows kept overall, best score first
EXCERPT_CANDIDATES = 60         # ANN candidates before the MENTIONS-anchor filter
METRIC_PERIODS_SHOWN = 3        # fiscal periods per (company, metric) the prompt shows
METRIC_PERIODS_FETCHED = METRIC_PERIODS_SHOWN + 1   # + the year before the oldest shown, the base of its year-over-year
TEMPORAL_CAPS = {"removed": 8, "new": 8, "reworded": 4}   # items shown per company and change; totals are always stated
_LENGTH_BAND_EDGES = (1500, 500)   # item length (chars) bands: >= 1500, >= 500, shorter; a coarse "then length" rank
_PARAGRAPH_KIND = "paragraph"      # RiskItem.unit_kind of a unit with no detectable headline

# --- queries (module constants so the row shapes/limits can be unit-tested) ---

# (i) Company relations, undirected, variable length. ``{hops}`` is substituted by
# :func:`company_edges_query` (Cypher cannot parameterise path bounds). Endpoints are
# Companies; ExportControl can never appear, so no company reaches another via a rule.
COMPANY_EDGES_QUERY = """MATCH (a:Company) WHERE a.cik IN $ids
MATCH p = (a)-[:SUPPLIES_TO|DEPENDS_ON|CUSTOMER_OF|COMPETES_WITH*1..{hops}]-(b:Company)
UNWIND relationships(p) AS rel
RETURN DISTINCT coalesce(startNode(rel).name, startNode(rel).title) AS source, type(rel) AS relation,
       coalesce(endNode(rel).name, endNode(rel).title) AS target,
       rel.status AS status, rel.evidence_quote AS quote, rel.evidence_chunk_ids AS chunk_ids"""

# (ii) AFFECTED_BY rules for the anchors and (when ``$include_neighbours``) their direct
# company neighbours. Only Company->ExportControl at hop 1; newest ``$per_company`` rules
# per company (the CALL subquery makes the LIMIT per company, not global).
RULE_EDGES_QUERY = """MATCH (a:Company) WHERE a.cik IN $ids
OPTIONAL MATCH (a)-[:SUPPLIES_TO|DEPENDS_ON|CUSTOMER_OF|COMPETES_WITH]-(n:Company)
WHERE $include_neighbours
WITH collect(DISTINCT a) + collect(DISTINCT n) AS companies
UNWIND companies AS c
WITH DISTINCT c
CALL (c) {
  MATCH (c)-[r:AFFECTED_BY]->(x:ExportControl)
  RETURN r, x ORDER BY x.date DESC, x.title LIMIT $per_company
}
RETURN c.name AS source, type(r) AS relation, x.title AS target,
       r.status AS status, r.evidence_quote AS quote, r.evidence_chunk_ids AS chunk_ids,
       x.rule_id AS rule_id, toString(x.date) AS date, x.url AS url, x.kind AS kind,
       coalesce(r.source, 'federal_register') AS link_source, coalesce(r.link_method, 'keyword') AS link_method,
       coalesce(r.external, true) AS external"""

# The last ``$periods`` fiscal periods of EACH metric of each anchor (a CALL subquery, so the LIMIT is per metric, not
# global), keyed by cik for the ``xbrl:<cik>:<metric>:<period_end>`` citation id. ``$periods`` is
# :data:`METRIC_PERIODS_FETCHED`. Rows arrive ordered by company, metric, newest period first.
METRICS_QUERY = """MATCH (c:Company) WHERE c.cik IN $ids
MATCH (c)-[:REPORTS_METRIC]->(seed:Metric)
WITH DISTINCT c, seed.metric AS metric
CALL (c, metric) {
  MATCH (c)-[:REPORTS_METRIC]->(m:Metric)
  WHERE m.metric = metric
  RETURN m ORDER BY m.period_end DESC LIMIT $periods
}
RETURN c.cik AS cik, c.name AS company, m.metric AS metric, m.value AS value, m.unit AS unit,
       toString(m.period_start) AS period_start, toString(m.period_end) AS period_end
ORDER BY company, metric, period_end DESC"""

# One query PER anchor (``IN`` is unavailable inside SEARCH ... WHERE on 2026.05); the
# Active-status join and evidence hop are post-filters over the already fresh candidates.
ACTIVE_RISKS_QUERY = """MATCH (rf:RiskFactor)
SEARCH rf IN (VECTOR INDEX risk_embedding FOR $vec WHERE rf.filer_cik = $cik AND rf.is_current = true LIMIT $candidates) SCORE AS score
MATCH (a:Company {cik: rf.filer_cik})-[d:DISCLOSES_RISK {status:'Active'}]->(rf)-[:HAS_EVIDENCE]->(e:EvidenceSpan)
RETURN a.name AS company, rf.summary AS summary, rf.category AS category,
       e.chunk_id AS chunk_id, score"""

# The risk-item layer (M1b): for each anchor, the current annual filing and the annual filing it rolled over, then the
# items REMOVED (older filing's items whose text was verified absent from the current one: ``removed_in``), NEW (the
# current filing's ``is_new`` items) and REWORDED (``SUCCEEDED_BY {kind:'reworded'}``) between them. Both ends must be
# annual filings that actually have items: the current annual also has a 'rolled' edge to the last 10-Q, and a partial
# 10-K/A overlay (AMD's Item-7-only amendment) is 'current' too but carries no risk section. A 'pair' row per company
# says the comparison exists even when nothing changed; without it "no changes" and "no data" would look alike.
# All four UNION members return the same columns: rows are ranked and capped in :func:`select_temporal`.
_TEMPORAL_PAIR = """MATCH (c:Company)-[:FILED]->(cur:Filing {is_current: true})-[:SUPERSEDES {kind: 'rolled'}]->(prev:Filing)
WHERE c.cik IN $ids AND cur.form IN ['10-K', '10-K/A', '20-F', '20-F/A'] AND prev.form IN ['10-K', '10-K/A', '20-F', '20-F/A']
  AND EXISTS { MATCH (:RiskItem {filer_cik: c.cik, accession_no: cur.accession_no}) }
  AND EXISTS { MATCH (:RiskItem {filer_cik: c.cik, accession_no: prev.accession_no}) }"""
_TEMPORAL_PAIR_COLUMNS = """prev.accession_no AS older_accession, prev.form AS older_form, toString(prev.filing_date) AS older_date,
       cur.accession_no AS newer_accession, cur.form AS newer_form, toString(cur.filing_date) AS newer_date"""
# An item's length in characters: its char span when the loader stored one, else its chunk count at ~1,000 characters
# per chunk (the median chunk), else 0. Only a coarse rank ("then length") needs it.
_ITEM_LENGTH = "coalesce(i.char_end - i.char_start, size(coalesce(i.chunk_ids, [])) * 1000, 0)"

TEMPORAL_QUERY = "\nUNION ALL\n".join([
    _TEMPORAL_PAIR + """
RETURN c.name AS company, c.cik AS cik, 'pair' AS change, null AS item_id, null AS headline, null AS older_headline,
       null AS unit_kind, null AS section_id, null AS seq, null AS length, [] AS older_chunk_ids, [] AS newer_chunk_ids,
       null AS decided_by, null AS sim_embed, null AS sim_lex, null AS lineage,
       """ + _TEMPORAL_PAIR_COLUMNS,
    _TEMPORAL_PAIR + """
MATCH (i:RiskItem {filer_cik: c.cik, accession_no: prev.accession_no}) WHERE i.removed_in = cur.accession_no
RETURN c.name AS company, c.cik AS cik, 'removed' AS change, i.item_id AS item_id, i.headline AS headline,
       null AS older_headline, i.unit_kind AS unit_kind, i.section_id AS section_id, i.seq AS seq,
       """ + _ITEM_LENGTH + """ AS length, coalesce(i.chunk_ids, []) AS older_chunk_ids, [] AS newer_chunk_ids,
       null AS decided_by, null AS sim_embed, null AS sim_lex, i.lineage_id AS lineage,
       """ + _TEMPORAL_PAIR_COLUMNS,
    _TEMPORAL_PAIR + """
MATCH (i:RiskItem {filer_cik: c.cik, accession_no: cur.accession_no}) WHERE i.is_new = true
RETURN c.name AS company, c.cik AS cik, 'new' AS change, i.item_id AS item_id, i.headline AS headline,
       null AS older_headline, i.unit_kind AS unit_kind, i.section_id AS section_id, i.seq AS seq,
       """ + _ITEM_LENGTH + """ AS length, [] AS older_chunk_ids, coalesce(i.chunk_ids, []) AS newer_chunk_ids,
       null AS decided_by, null AS sim_embed, null AS sim_lex, i.lineage_id AS lineage,
       """ + _TEMPORAL_PAIR_COLUMNS,
    _TEMPORAL_PAIR + """
MATCH (o:RiskItem {filer_cik: c.cik, accession_no: prev.accession_no})-[s:SUCCEEDED_BY {kind: 'reworded'}]->(i:RiskItem {accession_no: cur.accession_no})
RETURN c.name AS company, c.cik AS cik, 'reworded' AS change, i.item_id AS item_id, i.headline AS headline,
       o.headline AS older_headline, i.unit_kind AS unit_kind, i.section_id AS section_id, i.seq AS seq,
       """ + _ITEM_LENGTH + """ AS length, coalesce(o.chunk_ids, []) AS older_chunk_ids,
       coalesce(i.chunk_ids, []) AS newer_chunk_ids, s.decided_by AS decided_by, s.sim_embed AS sim_embed,
       s.sim_lex AS sim_lex, i.lineage_id AS lineage,
       """ + _TEMPORAL_PAIR_COLUMNS,
])

# Excerpts keep v1 semantics (chunks that MENTION an anchor, from any filer); freshness is
# filtered in-index, the multi-anchor ``IN`` stays OUTSIDE the SEARCH where it is legal.
EXCERPTS_QUERY = """MATCH (node:EvidenceSpan)
SEARCH node IN (VECTOR INDEX evidence_embedding FOR $vec WHERE node.retrievable = true LIMIT $candidates) SCORE AS score
MATCH (node)-[:MENTIONS]->(c:Company) WHERE c.cik IN $ids
RETURN DISTINCT node.chunk_id AS chunk_id, score, node.text AS text, node.source_url AS source_url
ORDER BY score DESC LIMIT $k"""

VECTOR_QUERY = """MATCH (node:EvidenceSpan)
SEARCH node IN (VECTOR INDEX evidence_embedding FOR $vec WHERE node.retrievable = true LIMIT $k) SCORE AS score
RETURN node.chunk_id AS chunk_id, score, node.text AS text, node.source_url AS source_url"""


def company_edges_query(hops: int) -> str:
    """:data:`COMPANY_EDGES_QUERY` with the path bound substituted.

    ``hops`` is interpolated into the query text (Cypher cannot bind variable-length
    bounds), so it must be a plain integer >= 1 — anything else is rejected.
    """
    if isinstance(hops, bool) or not isinstance(hops, int) or hops < 1:
        raise ValueError(f"hops must be an integer >= 1, got {hops!r}")
    return COMPANY_EDGES_QUERY.format(hops=hops)


def run_cypher(driver, query: str, **params) -> list[dict]:
    """Run a read query and return plain dict rows (notebook helper)."""
    with driver.session() as session:
        return [dict(r) for r in session.run(query, **params)]


@lru_cache(maxsize=1)
def _alias_res() -> list[tuple[re.Pattern, tuple[str, int]]]:
    """Compiled alias regexes from the packaged canonical entity dictionary
    (notebook 14 ``ALIAS_RES``)."""
    canonical = load_canonical_entities()
    return [
        (re.compile(rf"\b{re.escape(a)}\b", re.I), (name, spec["entity_id"]))
        for name, spec in canonical.items()
        for a in {name, *spec["aliases"]}
    ]


def detect_anchors(question: str) -> dict[str, int]:
    """Deterministic anchor-entity detection via canonical alias matching
    (notebook 10 strategy C, no LLM). Returns {canonical_name: cik}."""
    return {name: eid for pat, (name, eid) in _alias_res() if pat.search(question)}


# Words every risk headline shares with every question about them ("How have the risk disclosures changed?"): they must
# not count towards a headline's similarity to the question.
_STOPWORDS = frozenset("""
a about above after all also an and any are as at be been between but by can could did do does for from had has have
how if in into is it its may more most no not of on or our out over should since so than that the their them then there
these they this those to under until up was we were what when where which while who why will with would you your
risk risks factor factors disclose disclosed disclosure disclosures disclosing change changed changes changing removed
remove removing dropped drop added add adding new newly latest annual report reports filing filings item items company
""".split())
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]+")
_PAIR_KEYS = ("company", "cik", "older_accession", "older_form", "older_date", "newer_accession", "newer_form",
              "newer_date")
_ITEM_KEYS = ("company", "cik", "change", "item_id", "headline", "older_headline", "unit_kind", "section_id", "seq",
              "length", "older_chunk_ids", "newer_chunk_ids", "decided_by", "sim_embed", "sim_lex", "lineage")


def _content_tokens(text: str | None) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 3 and t not in _STOPWORDS}


def _length_band(length: int | None) -> int:
    return next((band for band, edge in zip((2, 1), _LENGTH_BAND_EDGES) if (length or 0) >= edge), 0)


def _rank_key(row: dict, question_tokens: set[str]) -> tuple:
    """Headline items before paragraph units, then longer items (by coarse band), then similarity to the question,
    then exact length and item id (a deterministic tie-break). The RiskItem carries no text or embedding, so
    similarity is the lexical overlap of the question with the headline(s)."""
    similarity = len(question_tokens & _content_tokens(f"{row.get('headline')} {row.get('older_headline')}"))
    length = row.get("length") or 0
    return (row.get("unit_kind") == _PARAGRAPH_KIND, -_length_band(length), -similarity, -length,
            row.get("item_id") or "")


def select_temporal(rows: list[dict], question: str,
                    caps: Mapping[str, int] = TEMPORAL_CAPS) -> tuple[list[dict], list[dict]]:
    """Rank and cap the rows of :data:`TEMPORAL_QUERY`: ``(items, pairs)``.

    ``pairs`` has one dict per company whose comparison exists (older and newer filing, and ``totals`` of removed / new /
    reworded items BEFORE the cap, so the answer can say "showing 8 of 21"); ``items`` are flat rows (company, change,
    headline, chunk ids, ...) in pair order, ``removed`` then ``new`` then ``reworded``, each ranked and capped at
    ``caps``. No RiskItem data yet means ``([], [])``."""
    pairs: dict[int, dict] = {}
    changes: dict[int, dict[str, list[dict]]] = {}
    for row in rows:
        cik, change = row.get("cik"), row.get("change")
        if change == "pair":
            pairs[cik] = {**{k: row.get(k) for k in _PAIR_KEYS}, "totals": {c: 0 for c in caps}}
        elif change in caps:
            changes.setdefault(cik, {c: [] for c in caps})[change].append(row)
    question_tokens = _content_tokens(question)
    items: list[dict] = []
    for cik, pair in pairs.items():
        for change, group in changes.get(cik, {}).items():
            pair["totals"][change] = len(group)
            ranked = sorted(group, key=lambda r: _rank_key(r, question_tokens))
            items += [{**{k: r.get(k) for k in _ITEM_KEYS}, "older_chunk_ids": list(r.get("older_chunk_ids") or []),
                       "newer_chunk_ids": list(r.get("newer_chunk_ids") or [])} for r in ranked[:caps[change]]]
    return items, list(pairs.values())


def _company_edges(driver, anchor_ids: list[int], query: str) -> list[dict]:
    """Company-to-company relation rows (``query`` = :func:`company_edges_query`)."""
    return run_cypher(driver, query, ids=anchor_ids)


def _rule_edges(driver, anchor_ids: list[int], hops: int) -> list[dict]:
    """AFFECTED_BY rows: the anchors' newest rules and, only when ``NEIGHBOUR_RULES`` is on and the
    traversal reaches beyond hop 1 (``hops >= 2``), their direct company neighbours' rules."""
    return run_cypher(driver, RULE_EDGES_QUERY, ids=anchor_ids,
                      include_neighbours=NEIGHBOUR_RULES and hops >= 2, per_company=RULES_PER_COMPANY)


def _active_risks(driver, anchor_ids: list[int], vec: list[float]) -> list[dict]:
    """Per-anchor in-index-filtered risk search, merged and re-ranked globally."""
    rows: list[dict] = []
    for cik in anchor_ids:
        rows.extend(run_cypher(driver, ACTIVE_RISKS_QUERY, cik=cik, vec=vec,
                               candidates=ACTIVE_RISKS_PER_ANCHOR))
    return sorted(rows, key=lambda r: r["score"], reverse=True)[:ACTIVE_RISKS_TOP]


def hybrid_retrieve(question: str, driver, embedder, k_chunks: int = 8,
                    hops: int = 2) -> dict:
    """Entity-first hybrid retrieval — ported from notebook 14 (v3), C2-hardened.

    anchors -> company relation subgraph + capped AFFECTED_BY rules ->
    XBRL metrics (the last fiscal periods of each metric, with units) -> anchor-scoped active risks (per-anchor
    in-index filtered vector search) -> text-verified removed / added / reworded risk items -> anchor-scoped
    evidence chunks (in-index ``retrievable`` filter).

    Returns ``anchors``, ``edges``, ``metrics``, ``risks``, ``temporal`` (ranked, capped item rows),
    ``temporal_pairs`` (the compared filings and the uncapped totals, see :func:`select_temporal`), ``chunks``
    and ``anchor_defaulted``. ``anchor_defaulted`` is True when no canonical entity
    was detected and retrieval silently fell back to :data:`DEFAULT_ANCHOR_CIK`
    (Nvidia): that v1 behaviour is kept (benchmark question R2 relies on it) but is
    now explicit — ``anchors`` stays ``{}`` and the fallback is logged at INFO.
    v2 M2 replaces the fallback with a proper no-anchor mode.

    ``hops`` bounds the company-relation traversal; AFFECTED_BY rules of the anchors'
    direct neighbours are included only when ``hops >= 2`` (as in v1).
    """
    edges_query = company_edges_query(hops)      # rejects a bad ``hops`` before any work is done
    anchors = detect_anchors(question)
    anchor_defaulted = not anchors
    anchor_ids = list(dict.fromkeys(anchors.values())) or [DEFAULT_ANCHOR_CIK]
    if anchor_defaulted:
        logger.info("no canonical entity detected — anchor defaulted to CIK %d", DEFAULT_ANCHOR_CIK)
    vec = embedder.encode_query(question)
    edges = _company_edges(driver, anchor_ids, edges_query) + _rule_edges(driver, anchor_ids, hops)
    metrics = run_cypher(driver, METRICS_QUERY, ids=anchor_ids, periods=METRIC_PERIODS_FETCHED)
    risks = _active_risks(driver, anchor_ids, vec)
    temporal, temporal_pairs = select_temporal(run_cypher(driver, TEMPORAL_QUERY, ids=anchor_ids), question)
    chunks = run_cypher(driver, EXCERPTS_QUERY, ids=anchor_ids, vec=vec, k=k_chunks,
                        candidates=EXCERPT_CANDIDATES)
    logger.debug("hybrid_retrieve anchors=%s defaulted=%s edges=%d metrics=%d risks=%d "
                 "temporal=%d chunks=%d", anchors, anchor_defaulted, len(edges), len(metrics),
                 len(risks), len(temporal), len(chunks))
    return {"anchors": anchors, "edges": edges, "metrics": metrics, "risks": risks,
            "temporal": temporal, "temporal_pairs": temporal_pairs, "chunks": chunks,
            "anchor_defaulted": anchor_defaulted}


def vector_retrieve(question: str, driver, embedder, k: int = 8) -> dict:
    """Vector-only baseline — ported from notebook 14. Same context structure
    as hybrid_retrieve with the graph layers empty (only retrievable chunks)."""
    chunks = run_cypher(driver, VECTOR_QUERY, k=k, vec=embedder.encode_query(question))
    return {"anchors": {}, "edges": [], "metrics": [], "risks": [], "temporal": [], "temporal_pairs": [],
            "chunks": chunks}
