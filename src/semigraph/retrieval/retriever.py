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

- Hybrid retrieval surfaces the BITEMPORAL layer (deleted/closed risk
  lineages written by notebook 13: ``DISCLOSES_RISK {status:'Deleted'}``,
  ``rf.lineage_id`` / ``rf.first_seen`` / ``rf.last_seen``). The first eval
  run scored temporal questions 0.67/0.00 because retrieval never exposed
  what the graph knew.

This module is importable without a Neo4j driver; functions receive one.
"""

import logging
import re
from functools import lru_cache

from ..artifacts import load_canonical_entities

logger = logging.getLogger("semigraph.retrieval")

# Default anchor when no canonical entity is detected in the question:
# Nvidia, the PoC filer (notebook 10 convention, kept through notebook 14).
# v2 M2 replaces this with a proper no-anchor mode; until then ``hybrid_retrieve``
# reports the fallback explicitly via ``anchor_defaulted``.
DEFAULT_ANCHOR_CIK = 1045810

# --- tunables (named, and pinned by tests) ---
RULES_PER_COMPANY = 8           # newest AFFECTED_BY rules per company in the relations block
# Rules of the anchors' direct neighbours are OFF: each rule line is ~100 tokens and, with ~26 relevant
# rules, 12 per company over an anchor plus 16 neighbours added ~8k tokens per answer (cost regression
# vs v1). M2 re-adds them behind a relevance ranking.
NEIGHBOUR_RULES = False
ACTIVE_RISKS_PER_ANCHOR = 20    # ANN candidates per anchor company before the Active filter
ACTIVE_RISKS_TOP = 6            # active-risk rows kept overall, best score first
EXCERPT_CANDIDATES = 60         # ANN candidates before the MENTIONS-anchor filter

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
       r.status AS status, r.evidence_quote AS quote, r.evidence_chunk_ids AS chunk_ids"""

METRICS_QUERY = """MATCH (c:Company)-[:REPORTS_METRIC]->(m:Metric) WHERE c.cik IN $ids
RETURN c.name AS company, m.metric AS metric, m.value AS value, m.unit AS unit,
       toString(m.period_start) AS period_start, toString(m.period_end) AS period_end
ORDER BY m.period_end DESC LIMIT 20"""

# One query PER anchor (``IN`` is unavailable inside SEARCH ... WHERE on 2026.05); the
# Active-status join and evidence hop are post-filters over the already fresh candidates.
ACTIVE_RISKS_QUERY = """MATCH (rf:RiskFactor)
SEARCH rf IN (VECTOR INDEX risk_embedding FOR $vec WHERE rf.filer_cik = $cik AND rf.is_current = true LIMIT $candidates) SCORE AS score
MATCH (a:Company {cik: rf.filer_cik})-[d:DISCLOSES_RISK {status:'Active'}]->(rf)-[:HAS_EVIDENCE]->(e:EvidenceSpan)
RETURN a.name AS company, rf.summary AS summary, rf.category AS category,
       e.chunk_id AS chunk_id, score"""

TEMPORAL_QUERY = """MATCH (a:Company)-[d:DISCLOSES_RISK {status:'Deleted'}]->(rf:RiskFactor)
WHERE a.cik IN $ids AND rf.lineage_id IS NOT NULL
WITH a.name AS company, rf.lineage_id AS lineage,
     toString(min(rf.first_seen)) AS first_seen, toString(max(rf.last_seen)) AS last_seen,
     collect(rf.summary)[0] AS example
RETURN company, lineage, first_seen, last_seen, example
ORDER BY last_seen DESC LIMIT 10"""

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
    XBRL metrics (with units) -> anchor-scoped active risks (per-anchor in-index
    filtered vector search) -> BITEMPORAL dropped-risk lineages -> anchor-scoped
    evidence chunks (in-index ``retrievable`` filter).

    Returns ``anchors``, ``edges``, ``metrics``, ``risks``, ``temporal``, ``chunks``
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
    metrics = run_cypher(driver, METRICS_QUERY, ids=anchor_ids)
    risks = _active_risks(driver, anchor_ids, vec)
    temporal = run_cypher(driver, TEMPORAL_QUERY, ids=anchor_ids)
    chunks = run_cypher(driver, EXCERPTS_QUERY, ids=anchor_ids, vec=vec, k=k_chunks,
                        candidates=EXCERPT_CANDIDATES)
    logger.debug("hybrid_retrieve anchors=%s defaulted=%s edges=%d metrics=%d risks=%d "
                 "temporal=%d chunks=%d", anchors, anchor_defaulted, len(edges), len(metrics),
                 len(risks), len(temporal), len(chunks))
    return {"anchors": anchors, "edges": edges, "metrics": metrics, "risks": risks,
            "temporal": temporal, "chunks": chunks, "anchor_defaulted": anchor_defaulted}


def vector_retrieve(question: str, driver, embedder, k: int = 8) -> dict:
    """Vector-only baseline — ported from notebook 14. Same context structure
    as hybrid_retrieve with the graph layers empty (only retrievable chunks)."""
    chunks = run_cypher(driver, VECTOR_QUERY, k=k, vec=embedder.encode_query(question))
    return {"anchors": {}, "edges": [], "metrics": [], "risks": [], "temporal": [],
            "chunks": chunks}
