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
  ``RiskItem`` nodes (``removed_in`` / ``is_new`` / ``SUCCEEDED_BY {kind}``), plus the older items the text check could
  NOT settle (``unsettled_in``: neither verified present nor verified gone; a separate list, never counted as removed),
  never from the LLM-summary lineage
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
from datetime import date
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
# Items shown per company and change; totals are always stated. The order is the order the block prints them in.
TEMPORAL_CAPS = {"removed": 8, "unsettled": 6, "new": 8, "reworded": 4}
PASSAGE_CAPS = {"removed": 8, "added": 4, "reworded": 4}   # passages shown per company and kind (a removed one is the
                                                           # flagship finding, so it gets the most room); totals are stated
MAX_MENTIONED_PERIODS = 4       # fiscal years / period-end dates named in the question that get their own METRICS rows
_MAX_YEAR_RANGE = 5             # "2019-2021" / "2019 to 2021" expand to every year in between, up to this gap
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
# :data:`METRIC_PERIODS_FETCHED`. PLUS the periods the question NAMES: ``$years`` (fiscal years, matched on the year of the
# period end: NVIDIA's "fiscal 2020" ends January 2020) and ``$dates`` (ISO period-end dates), each with the fiscal year
# before it (period end 350-380 days earlier) as the base of its computed year-over-year. A question that names no
# period passes two empty lists and gets exactly the latest periods. Rows arrive ordered by company, metric, newest
# period first.
METRICS_QUERY = """MATCH (c:Company) WHERE c.cik IN $ids
MATCH (c)-[:REPORTS_METRIC]->(seed:Metric)
WITH DISTINCT c, seed.metric AS metric
CALL (c, metric) {
  MATCH (c)-[:REPORTS_METRIC]->(m:Metric)
  WHERE m.metric = metric
  RETURN m ORDER BY m.period_end DESC LIMIT $periods
  UNION
  MATCH (c)-[:REPORTS_METRIC]->(t:Metric)
  WHERE t.metric = metric AND (t.period_end.year IN $years OR toString(t.period_end) IN $dates)
  MATCH (c)-[:REPORTS_METRIC]->(m:Metric)
  WHERE m.metric = metric AND m.period_end <= t.period_end AND m.period_end >= t.period_end - duration({days: 380})
  RETURN m
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
# items REMOVED (older filing's items whose text was verified absent from the current one: ``removed_in``), UNSETTLED (older
# items the text check could not settle, ``unsettled_in``: NOT verified removed, listed apart from the removed ones, same
# columns), NEW (the current filing's ``is_new`` items) and REWORDED (``SUCCEEDED_BY {kind:'reworded'}``) between them.
# Both ends must be annual filings that actually have items: the current annual also has a 'rolled' edge to the last 10-Q,
# and a partial 10-K/A overlay (AMD's Item-7-only amendment) is 'current' too but carries no risk section. A pair the loader
# marked ``items_compared = false`` on its SUPERSEDES edge (an untrustworthy side: INTC, ASML FY24->FY25) survives the
# has-items guards whatever its sides hold, and comes back with ``compared = false`` and the reason: a side with no items
# must render "comparison not available", never vanish into "(none)". No item row is read for such a pair (the contract
# says none exists; a stale ``removed_in`` / ``unsettled_in`` on one is ignored). A 'pair' row per company says the
# comparison exists even when nothing changed; without it "no changes"
# and "no data" would look alike. A headline-less paragraph unit also returns ``lead_text``: the text of its first chunk
# from the unit's own offset (chunk and item offsets are both section-text offsets), so it can be labelled by its first
# sentence. All five UNION members return the same columns: rows are ranked and capped in :func:`select_temporal`.
_TEMPORAL_PAIR = """MATCH (c:Company)-[:FILED]->(cur:Filing {is_current: true})-[sup:SUPERSEDES {kind: 'rolled'}]->(prev:Filing)
WHERE c.cik IN $ids AND cur.form IN ['10-K', '10-K/A', '20-F', '20-F/A'] AND prev.form IN ['10-K', '10-K/A', '20-F', '20-F/A']
  AND (sup.items_compared = false OR (
       EXISTS { MATCH (:RiskItem {filer_cik: c.cik, accession_no: cur.accession_no}) }
       AND EXISTS { MATCH (:RiskItem {filer_cik: c.cik, accession_no: prev.accession_no}) }))"""
_TEMPORAL_PAIR_COLUMNS = """prev.accession_no AS older_accession, prev.form AS older_form, toString(prev.filing_date) AS older_date,
       cur.accession_no AS newer_accession, cur.form AS newer_form, toString(cur.filing_date) AS newer_date,
       coalesce(sup.items_compared, true) AS compared, sup.not_compared_reason AS not_compared_reason"""
# An item's length in characters: its char span when the loader stored one, else its chunk count at ~1,000 characters
# per chunk (the median chunk), else 0. Only a coarse rank ("then length") needs it.
_ITEM_LENGTH = "coalesce(i.char_end - i.char_start, size(coalesce(i.chunk_ids, [])) * 1000, 0)"
_LEAD_MATCH = "OPTIONAL MATCH (lead:EvidenceSpan {chunk_id: head(i.chunk_ids)})"
_LEAD_TEXT = """CASE WHEN i.unit_kind = 'paragraph' AND lead IS NOT NULL
            THEN substring(lead.text, CASE WHEN i.char_start > lead.char_start AND i.char_start - lead.char_start < size(lead.text)
                                           THEN i.char_start - lead.char_start ELSE 0 END, 400) END"""

TEMPORAL_QUERY = "\nUNION ALL\n".join([
    _TEMPORAL_PAIR + """
RETURN c.name AS company, c.cik AS cik, 'pair' AS change, null AS item_id, null AS headline, null AS older_headline,
       null AS unit_kind, null AS section_id, null AS seq, null AS length, [] AS older_chunk_ids, [] AS newer_chunk_ids,
       null AS decided_by, null AS sim_embed, null AS sim_lex, null AS lineage, null AS lead_text,
       """ + _TEMPORAL_PAIR_COLUMNS,
    _TEMPORAL_PAIR + """
MATCH (i:RiskItem {filer_cik: c.cik, accession_no: prev.accession_no}) WHERE i.removed_in = cur.accession_no AND coalesce(sup.items_compared, true)
""" + _LEAD_MATCH + """
RETURN c.name AS company, c.cik AS cik, 'removed' AS change, i.item_id AS item_id, i.headline AS headline,
       null AS older_headline, i.unit_kind AS unit_kind, i.section_id AS section_id, i.seq AS seq,
       """ + _ITEM_LENGTH + """ AS length, coalesce(i.chunk_ids, []) AS older_chunk_ids, [] AS newer_chunk_ids,
       null AS decided_by, null AS sim_embed, null AS sim_lex, i.lineage_id AS lineage,
       """ + _LEAD_TEXT + """ AS lead_text,
       """ + _TEMPORAL_PAIR_COLUMNS,
    _TEMPORAL_PAIR + """
MATCH (i:RiskItem {filer_cik: c.cik, accession_no: prev.accession_no}) WHERE i.unsettled_in = cur.accession_no AND coalesce(sup.items_compared, true)
""" + _LEAD_MATCH + """
RETURN c.name AS company, c.cik AS cik, 'unsettled' AS change, i.item_id AS item_id, i.headline AS headline,
       null AS older_headline, i.unit_kind AS unit_kind, i.section_id AS section_id, i.seq AS seq,
       """ + _ITEM_LENGTH + """ AS length, coalesce(i.chunk_ids, []) AS older_chunk_ids, [] AS newer_chunk_ids,
       null AS decided_by, null AS sim_embed, null AS sim_lex, i.lineage_id AS lineage,
       """ + _LEAD_TEXT + """ AS lead_text,
       """ + _TEMPORAL_PAIR_COLUMNS,
    _TEMPORAL_PAIR + """
MATCH (i:RiskItem {filer_cik: c.cik, accession_no: cur.accession_no}) WHERE i.is_new = true AND coalesce(sup.items_compared, true)
""" + _LEAD_MATCH + """
RETURN c.name AS company, c.cik AS cik, 'new' AS change, i.item_id AS item_id, i.headline AS headline,
       null AS older_headline, i.unit_kind AS unit_kind, i.section_id AS section_id, i.seq AS seq,
       """ + _ITEM_LENGTH + """ AS length, [] AS older_chunk_ids, coalesce(i.chunk_ids, []) AS newer_chunk_ids,
       null AS decided_by, null AS sim_embed, null AS sim_lex, i.lineage_id AS lineage,
       """ + _LEAD_TEXT + """ AS lead_text,
       """ + _TEMPORAL_PAIR_COLUMNS,
    _TEMPORAL_PAIR + """
MATCH (o:RiskItem {filer_cik: c.cik, accession_no: prev.accession_no})-[s:SUCCEEDED_BY {kind: 'reworded'}]->(i:RiskItem {accession_no: cur.accession_no})
WHERE coalesce(sup.items_compared, true)
""" + _LEAD_MATCH + """
RETURN c.name AS company, c.cik AS cik, 'reworded' AS change, i.item_id AS item_id, i.headline AS headline,
       o.headline AS older_headline, i.unit_kind AS unit_kind, i.section_id AS section_id, i.seq AS seq,
       """ + _ITEM_LENGTH + """ AS length, coalesce(o.chunk_ids, []) AS older_chunk_ids,
       coalesce(i.chunk_ids, []) AS newer_chunk_ids, s.decided_by AS decided_by, s.sim_embed AS sim_embed,
       s.sim_lex AS sim_lex, i.lineage_id AS lineage,
       """ + _LEAD_TEXT + """ AS lead_text,
       """ + _TEMPORAL_PAIR_COLUMNS,
])

# The same five members for the pairs a question NAMES (see :func:`select_pairs`): the newer filing is any annual filing in
# ``$newer_accessions`` instead of the current one. Built from TEMPORAL_QUERY by replacing only the pair match, so the two can
# never drift apart (tests/test_retrieval_pairs.py pins that the texts differ in exactly that).
_TEMPORAL_PAIR_SELECTED = (_TEMPORAL_PAIR.replace("(cur:Filing {is_current: true})", "(cur:Filing)")
                           .replace("WHERE c.cik IN $ids", "WHERE c.cik IN $ids AND cur.accession_no IN $newer_accessions", 1))
TEMPORAL_SELECTED_QUERY = TEMPORAL_QUERY.replace(_TEMPORAL_PAIR, _TEMPORAL_PAIR_SELECTED)

# EVERY consecutive annual-filing pair of the anchors (not only the current one), one row per pair, with what
# :func:`select_pairs` needs to pick by fiscal year: the fiscal year of each filing and whether each side has risk items.
# The graph's ``Filing`` carries no fiscal-year property, so a filing's fiscal year is the YEAR OF THE PERIOD END of its own
# annual XBRL metrics (``REPORTS_METRIC`` carries the filing's accession; a period of 300+ days is an annual one), which is the
# benchmark's convention ("FY2025" = the 10-K for the fiscal year ending in 2025, e.g. NVIDIA's ending January 2025). A
# filing whose metrics are not in the graph has ``null`` there and is never matched by year. Newest pair first per company.
ANNUAL_PAIRS_QUERY = """MATCH (c:Company)-[:FILED]->(cur:Filing)-[sup:SUPERSEDES {kind: 'rolled'}]->(prev:Filing)
WHERE c.cik IN $ids AND cur.form IN ['10-K', '10-K/A', '20-F', '20-F/A'] AND prev.form IN ['10-K', '10-K/A', '20-F', '20-F/A']
CALL (c, cur) {
  OPTIONAL MATCH (c)-[:REPORTS_METRIC {accession_no: cur.accession_no}]->(m:Metric)
  WHERE duration.inDays(m.period_start, m.period_end).days >= 300
  RETURN max(m.period_end) AS cur_pe
}
CALL (c, prev) {
  OPTIONAL MATCH (c)-[:REPORTS_METRIC {accession_no: prev.accession_no}]->(m:Metric)
  WHERE duration.inDays(m.period_start, m.period_end).days >= 300
  RETURN max(m.period_end) AS prev_pe
}
RETURN c.name AS company, c.cik AS cik,
       """ + _TEMPORAL_PAIR_COLUMNS + """,
       cur.is_current AS is_current,
       toString(prev_pe) AS older_period_end, prev_pe.year AS older_fy, toString(cur_pe) AS newer_period_end, cur_pe.year AS newer_fy,
       EXISTS { MATCH (:RiskItem {filer_cik: c.cik, accession_no: cur.accession_no}) } AS newer_has_items,
       EXISTS { MATCH (:RiskItem {filer_cik: c.cik, accession_no: prev.accession_no}) } AS older_has_items
ORDER BY cik, newer_date DESC"""

# The change layer BELOW the item (M1b plan L.2, L.7): the passages of surviving items that changed between the two
# filings of a compared pair, read through their item (``HAS_PASSAGE``) and matched on the pair itself (filer and both
# accessions), so a stale passage of another pair is never read. ``$pairs`` is a list of ``{cik, older, newer}`` for the
# pairs that WERE compared (a not-compared pair has no passages by contract). ``chunk_ids`` are those of the filing the
# passage text is quoted from (the older filing for ``removed`` / ``reworded``, the newer for ``added``);
# ``counterpart_chunk_ids`` (a reworded passage's newer wording) is an optional property: [] when the graph has none.
# Ranked and capped in :func:`select_passages`.
PASSAGES_QUERY = """UNWIND $pairs AS pr
MATCH (i:RiskItem)-[:HAS_PASSAGE]->(p:RiskPassage {filer_cik: pr.cik, older_accession: pr.older, newer_accession: pr.newer})
""" + _LEAD_MATCH + """
RETURN pr.cik AS cik, pr.older AS older_accession, pr.newer AS newer_accession, p.passage_id AS passage_id, p.kind AS kind,
       i.item_id AS item_id, i.headline AS item_headline,
       i.unit_kind AS item_unit_kind, i.section_id AS section_id,
       """ + _LEAD_TEXT + """ AS lead_text,
       p.text AS text, p.counterpart_text AS counterpart_text, p.similarity AS similarity,
       coalesce(p.chunk_ids, []) AS chunk_ids, coalesce(p.counterpart_chunk_ids, []) AS counterpart_chunk_ids
ORDER BY passage_id"""

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
# What :func:`select_pairs` adds to a pair beside ``_PAIR_KEYS``: how it was chosen, the fiscal years / period ends of its
# two filings (for the block's "covers ..." line) and whether the temporal query can be asked for it at all.
_PAIR_EXTRA_KEYS = ("selection", "older_fy", "newer_fy", "older_period_end", "newer_period_end", "queryable")
_ITEM_KEYS = ("company", "cik", "change", "item_id", "headline", "older_headline", "unit_kind", "section_id", "seq",
              "length", "older_chunk_ids", "newer_chunk_ids", "decided_by", "sim_embed", "sim_lex", "lineage",
              "lead_text", "older_accession", "newer_accession")
_PASSAGE_KEYS = ("cik", "passage_id", "kind", "item_id", "item_headline", "item_unit_kind", "section_id", "lead_text",
                 "text", "counterpart_text", "similarity", "chunk_ids", "counterpart_chunk_ids", "older_accession",
                 "newer_accession")


def _content_tokens(text: str | None) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 3 and t not in _STOPWORDS}


def _length_band(length: int | None) -> int:
    return next((band for band, edge in zip((2, 1), _LENGTH_BAND_EDGES) if (length or 0) >= edge), 0)


def _rank_key(row: dict, question_tokens: set[str]) -> tuple:
    """Headline items before paragraph units, then the items the question talks about (lexical overlap with the
    headline(s); zero for everything when the question shares no content word with any of them), then longer items (by
    coarse band), then exact length and item id (a deterministic tie-break). The RiskItem carries no text or embedding,
    so similarity is the lexical overlap of the question with the headline(s). Similarity ranks BEFORE the length band:
    a question about one named risk must not be answered with the longest unrelated ones."""
    similarity = len(question_tokens & _content_tokens(f"{row.get('headline')} {row.get('older_headline')}"))
    length = row.get("length") or 0
    return (row.get("unit_kind") == _PARAGRAPH_KIND, -similarity, -_length_band(length), -length,
            row.get("item_id") or "")


def _pair_id(row: Mapping) -> tuple:
    """A pair is one company's comparison of two annual filings; the newer filing's accession names it within the company."""
    return row.get("cik"), row.get("newer_accession")


def _per_pair_caps(caps: Mapping[str, int], n_pairs: int) -> dict[str, int]:
    """A company's line budget split over its ``n_pairs`` pairs (each list keeps at least one line): one pair keeps ``caps``
    unchanged, so a question that names no pair renders exactly what it always did."""
    return dict(caps) if n_pairs <= 1 else {kind: max(1, cap // n_pairs) for kind, cap in caps.items()}


def _pair_dict(source: Mapping, caps: Mapping[str, int]) -> dict:
    compared = source.get("compared") is not False
    return {**{k: source.get(k) for k in _PAIR_KEYS}, **{k: source[k] for k in _PAIR_EXTRA_KEYS if k in source},
            "compared": compared, "not_compared_reason": None if compared else source.get("not_compared_reason"),
            "totals": {c: 0 for c in caps}}


def select_temporal(rows: list[dict], question: str, caps: Mapping[str, int] = TEMPORAL_CAPS,
                    pairs: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Rank and cap the rows of :data:`TEMPORAL_QUERY` (or :data:`TEMPORAL_SELECTED_QUERY`): ``(items, pairs)``.

    ``pairs`` has one dict per compared filing pair (older and newer filing, ``compared`` / ``not_compared_reason`` from the
    SUPERSEDES edge, and ``totals`` of removed / unsettled / new / reworded items BEFORE the cap, so the answer can say
    "showing 8 of 21"); ``items`` are flat rows (company, change, headline, chunk ids, ...) in pair order, ``removed``, then
    ``unsettled`` (older items the text check could not settle: NOT counted as removed), then ``new``, then ``reworded``,
    each ranked and capped at ``caps``. A pair the loader could not compare (``compared`` False) has all-zero totals and no
    items, whatever rows arrive for it. A row with no ``compared`` value (a graph from before the loader stamped the edge)
    means compared. No RiskItem data yet means ``([], [])``.

    Without ``pairs`` the pairs are the ``'pair'`` rows (the current pair of each company); with ``pairs`` (the output of
    :func:`select_pairs`) they are exactly those, so a chosen pair with no rows keeps zero totals. Everything is keyed by
    ``(cik, newer accession)``: two pairs of one company never share items, totals or caps, and when a company has several
    pairs each list gets its share of the caps (:func:`_per_pair_caps`). A row without ``newer_accession`` (a saved fixture)
    belongs to its company's only pair."""
    pair_map: dict[tuple, dict] = {}
    if pairs is not None:
        pair_map = {_pair_id(p): _pair_dict(p, caps) for p in pairs}
    else:
        for row in rows:
            if row.get("change") == "pair":
                pair_map[_pair_id(row)] = _pair_dict(row, caps)

    def owner(row: Mapping) -> tuple | None:
        if _pair_id(row) in pair_map:
            return _pair_id(row)
        if row.get("newer_accession") is None:
            mine = [key for key in pair_map if key[0] == row.get("cik")]
            return mine[0] if len(mine) == 1 else None
        return None

    changes: dict[tuple, dict[str, list[dict]]] = {}
    for row in rows:
        change = row.get("change")
        if change in caps and (key := owner(row)) is not None:
            changes.setdefault(key, {c: [] for c in caps})[change].append(row)
    question_tokens = _content_tokens(question)
    n_pairs = {cik: sum(1 for k in pair_map if k[0] == cik) for cik in {k[0] for k in pair_map}}
    items: list[dict] = []
    for key, pair in pair_map.items():
        if not pair["compared"]:
            continue
        pair_caps = _per_pair_caps(caps, n_pairs[key[0]])
        for change, group in changes.get(key, {}).items():
            pair["totals"][change] = len(group)
            ranked = sorted(group, key=lambda r: _rank_key(r, question_tokens))
            items += [{**{k: r.get(k) for k in _ITEM_KEYS}, "older_chunk_ids": list(r.get("older_chunk_ids") or []),
                       "newer_chunk_ids": list(r.get("newer_chunk_ids") or []),
                       "older_accession": pair["older_accession"], "newer_accession": pair["newer_accession"]}
                      for r in ranked[:pair_caps[change]]]
    return items, list(pair_map.values())


def _passage_rank_key(row: dict, question_tokens: set[str]) -> tuple:
    """The passages that talk about the question first (lexical overlap with the quoted text and its counterpart, when
    there is any), then the longer ones, then the passage id (deterministic)."""
    overlap = len(question_tokens & _content_tokens(f"{row.get('text')} {row.get('counterpart_text')}"))
    return (-overlap, -len(row.get("text") or ""), row.get("passage_id") or "")


def select_passages(rows: list[dict], pairs: list[dict], question: str,
                    caps: Mapping[str, int] = PASSAGE_CAPS) -> tuple[list[dict], list[dict]]:
    """Rank and cap the rows of :data:`PASSAGES_QUERY`: ``(passages, pairs)``.

    Each company keeps at most ``caps[kind]`` passages per kind, ranked by lexical overlap with the question then length,
    in the fixed order ``removed``, ``added``, ``reworded``. The returned ``pairs`` are copies of the input with
    ``passage_totals`` (per kind, BEFORE the cap) added, so the answer can say "showing 8 of 12". Passages of a pair
    that was not compared, or of a company with no pair, are dropped."""
    def key_of(pair: Mapping) -> tuple:
        return pair.get("cik"), pair.get("older_accession"), pair.get("newer_accession")

    comparable = {key_of(p): p for p in pairs if p.get("compared", True)}
    n_pairs = {cik: sum(1 for k in comparable if k[0] == cik) for cik in {k[0] for k in comparable}}

    def owner(row: Mapping) -> tuple | None:
        if key_of(row) in comparable:
            return key_of(row)
        if row.get("older_accession") is None and row.get("newer_accession") is None:   # a saved fixture: its company's only pair
            mine = [k for k in comparable if k[0] == row.get("cik")]
            return mine[0] if len(mine) == 1 else None
        return None

    grouped: dict[tuple, dict[str, list[dict]]] = {}
    for row in rows:
        if row.get("kind") in caps and (key := owner(row)) is not None:
            grouped.setdefault(key, {kind: [] for kind in caps})[row["kind"]].append(row)
    tokens = _content_tokens(question)
    passages: list[dict] = []
    with_totals: list[dict] = []
    for pair in pairs:
        groups = grouped.get(key_of(pair), {})
        with_totals.append({**pair, "passage_totals": {kind: len(groups.get(kind, [])) for kind in caps}})
        pair_caps = _per_pair_caps(caps, n_pairs.get(pair.get("cik"), 1))
        for kind in caps:
            ranked = sorted(groups.get(kind, []), key=lambda r: _passage_rank_key(r, tokens))
            passages += [{**{k: r.get(k) for k in _PASSAGE_KEYS}, "chunk_ids": list(r.get("chunk_ids") or []),
                          "counterpart_chunk_ids": list(r.get("counterpart_chunk_ids") or []),
                          "older_accession": pair.get("older_accession"), "newer_accession": pair.get("newer_accession")}
                         for r in ranked[:pair_caps[kind]]]
    return passages, with_totals


# --- the periods a question names ------------------------------------------------------------------------------------

_MONTH_NAMES = ("January February March April May June July August September October November December").split()
_MONTH_NUMBER = {**{name.lower(): n for n, name in enumerate(_MONTH_NAMES, 1)},
                 **{name[:3].lower(): n for n, name in enumerate(_MONTH_NAMES, 1)}, "sept": 9}
_MONTH = "(?:" + "|".join(_MONTH_NAMES) + r"|Sept|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?"
_DATE_MDY = re.compile(rf"\b({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)
_DATE_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH})\s+(\d{{4}})\b", re.I)
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_YEAR = re.compile(r"(?<![\d$.,])(20\d{2})(?!\d|[\d,]*\s*(?:million|billion|trillion|thousand|%|percent)\b)", re.I)
_YEAR_RANGE = re.compile(r"(?<![\d$.,])(20\d{2})\s*(?:-|–|—|to|through|and)\s*(20\d{2})(?!\d)", re.I)
_FY_SHORT = re.compile(r"\bFY\s?'?(\d{2})\b", re.I)


def mentioned_periods(question: str) -> dict[str, list]:
    """The fiscal years and period-end dates a question names: ``{"years": [2019, 2021], "dates": ["2023-01-29"]}``.

    Recognised: full dates ("January 29, 2023", "29 Jan 2023", "2023-01-29"), "fiscal 2020" / "FY2021" / "FY21" / "in
    2019" (any standalone 20xx that is not an amount), and year ranges ("2019-2021", "between 2019 and 2021", at most
    :data:`_MAX_YEAR_RANGE` apart). A year is matched against the YEAR of a period end, so NVIDIA's "fiscal 2020" (ended
    January 2020) is 2020. At most :data:`MAX_MENTIONED_PERIODS` are kept, the newest first."""
    dates: set[str] = set()
    text = question

    def take(match: re.Match, year: int, month: int, day: int) -> str:
        try:
            dates.add(date(year, month, day).isoformat())
        except ValueError:
            pass                                       # "February 31, 2023": named, but not a date
        return " " * len(match.group(0))               # a date is not also a year

    text = _DATE_MDY.sub(lambda m: take(m, int(m.group(3)), _MONTH_NUMBER[m.group(1).rstrip(".").lower()], int(m.group(2))), text)
    text = _DATE_DMY.sub(lambda m: take(m, int(m.group(3)), _MONTH_NUMBER[m.group(2).rstrip(".").lower()], int(m.group(1))), text)
    text = _DATE_ISO.sub(lambda m: take(m, int(m.group(1)), int(m.group(2)), int(m.group(3))), text)
    years = {int(y) for y in _YEAR.findall(text)} | {2000 + int(y) for y in _FY_SHORT.findall(text)}
    for start, end in _YEAR_RANGE.findall(text):
        if 0 <= int(end) - int(start) <= _MAX_YEAR_RANGE:
            years.update(range(int(start), int(end) + 1))
    kept = sorted(({(y, str(y)) for y in years} | {(int(d[:4]), d) for d in dates}), reverse=True)[:MAX_MENTIONED_PERIODS]
    return {"years": sorted(y for y, label in kept if label.isdigit()), "dates": sorted(d for _, d in kept if not d.isdigit())}


# --- which filing pair(s) of a company the temporal layer reads --------------------------------------------------------------
# A question about a change in the risk disclosures that NAMES fiscal years ("between its FY2024 and FY2025 annual reports")
# reads the pair of annual filings between them; one that asks about several annual reports ("evolved across its recent annual
# reports", "in earlier 10-Ks") reads the newest pairs; anything else reads the current pair exactly as before. A metric
# question that names years is NOT a pair question: it needs a risk-change word AND a disclosure noun.
MAX_PAIRS_PER_COMPANY = 2
_MULTI_PAIR_INTENT_RE = re.compile(
    r"\b(?:across|over|through|throughout)\s+(?:its\s+|the\s+|their\s+)?(?:\w+\s+){0,2}?"
    r"(?:annual\s+reports|annual\s+filings|10-Ks|20-Fs)\b"
    r"|\bevolv\w*"
    r"|\b(?:earlier|previous|prior|past|older|recent|successive)\s+(?:annual\s+reports|annual\s+filings|10-Ks|20-Fs)\b", re.I)
_RISK_CHANGE_RE = re.compile(
    r"\b(?:remov\w*|drop(?:s|ped|ping)?|delet\w*|eliminat\w*|withdr\w*|no longer|stop(?:s|ped|ping)?|newly|new|added|adding|"
    r"reword\w*|chang\w*|differ\w*|appear\w*|disappear\w*)\b", re.I)
_DISCLOSURE_NOUN_RE = re.compile(
    r"\b(?:risks?|disclos\w*|10-Ks?|10-Qs?|20-Fs?|annual\s+reports?|filings?|statements?|sentences?|passages?)\b", re.I)


def pair_selection_mode(question: str, periods: Mapping[str, list]) -> str | None:
    """``"named"`` (the question names fiscal years / period-end dates AND asks about a change in a disclosure), ``"multi"``
    (it asks about several annual reports), or ``None`` (the current pair, as before). Years beat the multi-year wording."""
    if (periods.get("years") or periods.get("dates")) and _RISK_CHANGE_RE.search(question) \
            and _DISCLOSURE_NOUN_RE.search(question):
        return "named"
    return "multi" if _MULTI_PAIR_INTENT_RE.search(question) else None


def _guarded(row: Mapping) -> bool:
    """A pair the temporal query can read: the loader marked it not compared (it comes back as "comparison not available"), or
    both filings have risk items."""
    return row.get("compared") is False or bool(row.get("older_has_items") and row.get("newer_has_items"))


def _fiscal_list(years: list[int]) -> str:
    years = sorted(set(years))
    return ", ".join(map(str, years[:-1])) + f" and {years[-1]}" if len(years) > 1 else str(years[0])


def _chosen(row: Mapping, selection: str) -> dict:
    """The pair as :func:`select_temporal` takes it. A pair with a side that has no risk items (a filing the loader never
    parsed) is returned as NOT compared, with the reason, and is not asked of the graph."""
    out = {**{k: row.get(k) for k in _PAIR_KEYS}, **{k: row.get(k) for k in _PAIR_EXTRA_KEYS if k not in ("selection", "queryable")},
           "compared": row.get("compared") is not False, "not_compared_reason": row.get("not_compared_reason"),
           "selection": selection, "queryable": _guarded(row)}
    if not out["queryable"]:
        side = "older" if not row.get("older_has_items") else "newer"
        out.update(compared=False, not_compared_reason=(
            f"no risk items were loaded for the {row.get(side + '_form')} filed {row.get(side + '_date')} "
            f"(accession {row.get(side + '_accession')})"))
    return out


def select_pairs(rows: list[dict], question: str, periods: Mapping[str, list], *, mode: str | None = None,
                 max_pairs: int = MAX_PAIRS_PER_COMPANY) -> tuple[list[dict], list[dict]]:
    """Choose the filing pair(s) per company from the rows of :data:`ANNUAL_PAIRS_QUERY`: ``(pairs, notices)``.

    ``named``: the pairs whose two filings' fiscal years are both named, else whose newer filing's year is named, else whose
    older filing's year is (the year of a period end, so "FY2025" is the 10-K for the fiscal year ending in 2025); the
    ``max_pairs`` newest. ``multi``: the ``max_pairs`` newest pairs the graph can read. Anything else (or no match at all)
    is the current pair. A named year with no pair in the graph gives the current pair AND a notice saying so, never a
    silent substitute. Pairs come back oldest first per company; ``notices`` are ``{cik, company, text}``."""
    mode = mode or pair_selection_mode(question, periods) or "latest"
    named = sorted({int(y) for y in periods.get("years", [])} | {int(d[:4]) for d in periods.get("dates", [])})
    by_company: dict[int, list[dict]] = {}
    for row in rows:
        by_company.setdefault(row["cik"], []).append(row)
    chosen: list[dict] = []
    notices: list[dict] = []
    for cik, history in by_company.items():
        history = sorted(history, key=lambda r: r.get("newer_date") or "", reverse=True)
        company = history[0].get("company")
        picked: list[dict] = []
        selection = "latest"
        missed: str | None = None                 # a named year with no pair: the notice is finished once the fallback is known
        if mode == "named" and named:
            wanted = set(named)
            for keep in (lambda r: r.get("older_fy") in wanted and r.get("newer_fy") in wanted,
                         lambda r: r.get("newer_fy") in wanted, lambda r: r.get("older_fy") in wanted):
                picked = [r for r in history if keep(r)][:max_pairs]
                if picked:
                    break
            selection = "named"
            if not picked:
                loaded = [fy for r in history for fy in (r.get("older_fy"), r.get("newer_fy")) if fy is not None]
                missed = (f"no annual-filing comparison covering fiscal {_fiscal_list(named)} is in the graph for {company} "
                          f"(annual filings loaded: fiscal {', '.join(map(str, sorted(set(loaded)))) or 'unknown'})")
        elif mode == "multi":
            readable = [r for r in history if _guarded(r)]
            picked, selection = readable[:max_pairs], "multi"
            if len(readable) > max_pairs:
                notices.append({"cik": cik, "company": company, "text": (
                    f"showing the {max_pairs} most recent of {len(readable)} annual-filing comparisons for {company}")})
        if not picked:
            picked = [r for r in history if r.get("is_current") and _guarded(r)][:1]
            selection = "latest"
            if missed:                              # never promise a comparison that is not shown
                tail = ("the latest comparison is shown instead" if picked
                        else "no readable comparison of the latest annual filings is available either")
                notices.append({"cik": cik, "company": company, "text": f"{missed}; {tail}"})
        chosen += [_chosen(r, selection) for r in reversed(picked)]
    return chosen, notices


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


def _passages(driver, pairs: list[dict], question: str) -> tuple[list[dict], list[dict]]:
    """The ranked, capped passages of the pairs that were compared (no query at all when none was), and the pairs with
    their per-kind ``passage_totals``."""
    comparable = [{"cik": p["cik"], "older": p["older_accession"], "newer": p["newer_accession"]}
                  for p in pairs if p.get("compared", True)]
    rows = run_cypher(driver, PASSAGES_QUERY, pairs=comparable) if comparable else []
    return select_passages(rows, pairs, question)


def hybrid_retrieve(question: str, driver, embedder, k_chunks: int = 8,
                    hops: int = 2) -> dict:
    """Entity-first hybrid retrieval — ported from notebook 14 (v3), C2-hardened.

    anchors -> company relation subgraph + capped AFFECTED_BY rules ->
    XBRL metrics (the last fiscal periods of each metric, with units, plus the periods the question names) ->
    anchor-scoped active risks (per-anchor in-index filtered vector search) -> text-verified removed / added / reworded
    risk items and the changed passages inside surviving ones -> anchor-scoped evidence chunks (in-index ``retrievable``
    filter).

    Returns ``anchors``, ``edges``, ``metrics``, ``metric_periods`` (the years / dates the question named, for the METRICS
    block), ``risks``, ``temporal`` (ranked, capped item rows), ``temporal_pairs`` (the compared filings, whether they
    could be compared, the uncapped item totals and the uncapped passage totals, see :func:`select_temporal` and
    :func:`select_passages`), ``temporal_passages`` (ranked, capped), ``chunks`` and ``anchor_defaulted``.
    ``anchor_defaulted`` is True when no canonical entity
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
    periods = mentioned_periods(question)
    edges = _company_edges(driver, anchor_ids, edges_query) + _rule_edges(driver, anchor_ids, hops)
    metrics = run_cypher(driver, METRICS_QUERY, ids=anchor_ids, periods=METRIC_PERIODS_FETCHED,
                         years=periods["years"], dates=periods["dates"])
    risks = _active_risks(driver, anchor_ids, vec)
    mode = pair_selection_mode(question, periods)
    temporal_notices: list[dict] = []
    if mode is None:                                    # the current pair of each anchor, exactly as before
        temporal, temporal_pairs = select_temporal(run_cypher(driver, TEMPORAL_QUERY, ids=anchor_ids), question)
    else:                                               # the pair(s) the question names (see select_pairs)
        chosen, temporal_notices = select_pairs(run_cypher(driver, ANNUAL_PAIRS_QUERY, ids=anchor_ids), question, periods,
                                                mode=mode)
        newer = [p["newer_accession"] for p in chosen if p.get("queryable")]
        rows = run_cypher(driver, TEMPORAL_SELECTED_QUERY, ids=anchor_ids, newer_accessions=newer) if newer else []
        temporal, temporal_pairs = select_temporal(rows, question, pairs=chosen)
    temporal_passages, temporal_pairs = _passages(driver, temporal_pairs, question)
    chunks = run_cypher(driver, EXCERPTS_QUERY, ids=anchor_ids, vec=vec, k=k_chunks,
                        candidates=EXCERPT_CANDIDATES)
    logger.debug("hybrid_retrieve anchors=%s defaulted=%s edges=%d metrics=%d risks=%d "
                 "temporal=%d passages=%d chunks=%d", anchors, anchor_defaulted, len(edges), len(metrics),
                 len(risks), len(temporal), len(temporal_passages), len(chunks))
    return {"anchors": anchors, "edges": edges, "metrics": metrics, "metric_periods": periods, "risks": risks,
            "temporal": temporal, "temporal_pairs": temporal_pairs, "temporal_passages": temporal_passages,
            "temporal_notices": temporal_notices, "chunks": chunks, "anchor_defaulted": anchor_defaulted}


def vector_retrieve(question: str, driver, embedder, k: int = 8) -> dict:
    """Vector-only baseline — ported from notebook 14. Same context structure
    as hybrid_retrieve with the graph layers empty (only retrievable chunks)."""
    chunks = run_cypher(driver, VECTOR_QUERY, k=k, vec=embedder.encode_query(question))
    return {"anchors": {}, "edges": [], "metrics": [], "metric_periods": {"years": [], "dates": []}, "risks": [],
            "temporal": [], "temporal_pairs": [], "temporal_passages": [], "temporal_notices": [], "chunks": chunks}
