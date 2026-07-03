"""Retrieval strategies over the semigraph Neo4j graph.

Ported from notebook 14 (the authoritative final versions, benchmark-verified
at M6: hybrid 100% correct / 0.865 faithful / 0 hallucinated citations), which
finalized notebook 10's entity-first hybrid strategy.

Battle scars preserved — each cost a failed run, do not relax:

- Neo4j 2026.x DEPRECATES ``db.index.vector.queryNodes`` (notebooks 09-11
  still used it and logged deprecation warnings). Every vector query here
  uses the SEARCH clause grammar verified live in notebook 14::

      MATCH (n:Label)
      SEARCH n IN (VECTOR INDEX index_name FOR $vec LIMIT k) SCORE AS score

  The MATCH must bind exactly ONE variable; any filters/joins compose AFTER
  the search clause.

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
DEFAULT_ANCHOR_CIK = 1045810


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


def hybrid_retrieve(question: str, driver, embedder, k_chunks: int = 8,
                    hops: int = 2) -> dict:
    """Entity-first hybrid retrieval — ported from notebook 14 (v3).

    anchors -> relation subgraph (incl. AFFECTED_BY/ExportControl) ->
    XBRL metrics -> anchor-scoped active risks (vector) -> BITEMPORAL
    dropped-risk lineages -> anchor-scoped evidence chunks (vector).
    """
    anchors = detect_anchors(question)
    anchor_ids = list(anchors.values()) or [DEFAULT_ANCHOR_CIK]
    vec = embedder.encode_query(question)
    edges = run_cypher(
        driver,
        f"""MATCH (a:Company) WHERE a.cik IN $ids
        MATCH p = (a)-[r:SUPPLIES_TO|DEPENDS_ON|CUSTOMER_OF|COMPETES_WITH|AFFECTED_BY*1..{hops}]-(b)
        WHERE (b:Company OR b:ExportControl)
        UNWIND relationships(p) AS rel
        RETURN DISTINCT coalesce(startNode(rel).name, startNode(rel).title) AS source, type(rel) AS relation,
               coalesce(endNode(rel).name, endNode(rel).title) AS target,
               rel.status AS status, rel.evidence_quote AS quote, rel.evidence_chunk_ids AS chunk_ids""",
        ids=anchor_ids)
    metrics = run_cypher(
        driver,
        """MATCH (c:Company)-[:REPORTS_METRIC]->(m:Metric) WHERE c.cik IN $ids
        RETURN c.name AS company, m.metric AS metric, m.value AS value,
               toString(m.period_start) AS period_start, toString(m.period_end) AS period_end
        ORDER BY m.period_end DESC LIMIT 20""", ids=anchor_ids)
    risks = run_cypher(
        driver,
        """MATCH (rf:RiskFactor)
        SEARCH rf IN (VECTOR INDEX risk_embedding FOR $vec LIMIT 40) SCORE AS score
        MATCH (a:Company)-[d:DISCLOSES_RISK {status:'Active'}]->(rf)-[:HAS_EVIDENCE]->(e:EvidenceSpan)
        WHERE a.cik IN $ids
        RETURN a.name AS company, rf.summary AS summary, rf.category AS category,
               e.chunk_id AS chunk_id, score ORDER BY score DESC LIMIT 6""",
        ids=anchor_ids, vec=vec)
    temporal = run_cypher(
        driver,
        """MATCH (a:Company)-[d:DISCLOSES_RISK {status:'Deleted'}]->(rf:RiskFactor)
        WHERE a.cik IN $ids AND rf.lineage_id IS NOT NULL
        WITH a.name AS company, rf.lineage_id AS lineage,
             toString(min(rf.first_seen)) AS first_seen, toString(max(rf.last_seen)) AS last_seen,
             collect(rf.summary)[0] AS example
        RETURN company, lineage, first_seen, last_seen, example
        ORDER BY last_seen DESC LIMIT 10""", ids=anchor_ids)
    chunks = run_cypher(
        driver,
        """MATCH (node:EvidenceSpan)
        SEARCH node IN (VECTOR INDEX evidence_embedding FOR $vec LIMIT 60) SCORE AS score
        MATCH (node)-[:MENTIONS]->(c:Company) WHERE c.cik IN $ids
        RETURN DISTINCT node.chunk_id AS chunk_id, score, node.text AS text, node.source_url AS source_url
        ORDER BY score DESC LIMIT $k""", ids=anchor_ids, vec=vec, k=k_chunks)
    logger.debug("hybrid_retrieve anchors=%s edges=%d metrics=%d risks=%d temporal=%d chunks=%d",
                 anchors, len(edges), len(metrics), len(risks), len(temporal), len(chunks))
    return {"anchors": anchors, "edges": edges, "metrics": metrics, "risks": risks,
            "temporal": temporal, "chunks": chunks}


def vector_retrieve(question: str, driver, embedder, k: int = 8) -> dict:
    """Vector-only baseline — ported from notebook 14. Same context structure
    as hybrid_retrieve with the graph layers empty."""
    chunks = run_cypher(
        driver,
        """MATCH (node:EvidenceSpan)
        SEARCH node IN (VECTOR INDEX evidence_embedding FOR $vec LIMIT $k) SCORE AS score
        RETURN node.chunk_id AS chunk_id, score, node.text AS text, node.source_url AS source_url""",
        k=k, vec=embedder.encode_query(question))
    return {"anchors": {}, "edges": [], "metrics": [], "risks": [], "temporal": [],
            "chunks": chunks}
