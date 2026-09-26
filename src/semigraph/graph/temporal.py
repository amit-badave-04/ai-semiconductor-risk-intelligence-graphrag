"""Current-filing status of the risk layer (M1b step 4). Replaces the bitemporal lineage clustering of notebook 13.

What was retired, and why: the old pass clustered every RiskFactor's LLM SUMMARY by embedding similarity (greedy, 0.75 cosine),
called each cluster a "lineage", backdated ``start_date`` to its first member and marked a lineage ``Deleted`` (with an
``end_date``) when the company's latest annual filing had no member of it. That reported risks as dropped whose text was still in
the newer filing (a live audit on 2026-09-26 found the NVIDIA "dropped" examples still present; an unequal extraction between
years alone produced a "drop"). No text was ever consulted. It is gone, with its ``Deleted`` status, ``end_date``, and the
RiskFactor properties ``lineage_id`` / ``first_seen`` / ``last_seen``.

What replaces it:

* what CHANGED between two annual filings is read from the text-grounded item layer: ``graph/items.py`` (``semigraph align-items``)
  decides per risk item, against the whole newer section text, and ``graph/item_loader.py`` loads it (``RiskItem``, ``SUCCEEDED_BY``,
  ``removed_in``, ``RiskPassage``, ``SUPERSEDES.items_compared``); the lineage of an item is carried along its verified successors;
* :func:`apply_current_status` sets ``DISCLOSES_RISK.status`` to ``Active`` when the risk's filing is the company's CURRENT annual
  filing and ``Historical`` otherwise: a statement about the filing, never about whether a risk disappeared (there is no ``Deleted``);
* :func:`risks_active_as_of` answers "which risks did the company disclose on a date" from the annual filing that was current then.

Decisions worth knowing: ``DISCLOSES_RISK.start_date`` (set once by the loader: the filing date of the risk's filing) is kept and
``end_date`` is dropped (nothing reads it and, without closure, nothing defines it); risks evidenced only in the current 10-Q are
``Historical`` under this definition (they are not in the current annual), and so leave the retriever's active-risk query, which
reads ``status: 'Active'``; a risk in a section a later amendment restated is not ``Active`` even though its filing is current
(``rf.is_current`` already encodes the section's ownership).

The category-spelling fix (:func:`normalize_categories`) is unchanged.
"""

import logging

from .client import run_cypher

logger = logging.getLogger("semigraph.graph.temporal")

# Annual reports: the forms whose current filing makes a risk 'Active'.
ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A")
# A filing counts as an annual of its period only when effective: never a `corrected` original (its parsed amendment stands in
# for it) nor an inert `amendment` (unparsed 10-K/A) - see semigraph.versions.
EFFECTIVE_FILING_STATUSES = ("current", "superseded")
STATUS_ACTIVE, STATUS_HISTORICAL = "Active", "Historical"

# Graph-side canonical category spellings (notebook 13 cell 2 - includes
# 'Cybersecurity', which the extractor invented often enough to canonize).
CANONICAL_CATEGORIES = [
    "Supply Chain", "Geopolitical", "Export Controls", "Demand", "Competition",
    "Technology", "Legal/Regulatory", "Financial", "Cybersecurity", "Other",
]


def category_mapping(distinct: list[str]) -> list[dict]:
    """Map free-text category spellings to canonical ones (case-insensitive
    hit -> canonical spelling; miss -> Title Case). Returns only changes."""
    canon_by_lower = {c.lower(): c for c in CANONICAL_CATEGORIES}
    mapping = [
        {"old": c, "new": canon_by_lower.get(c.strip().lower(), c.strip().title())}
        for c in distinct if c
    ]
    return [m for m in mapping if m["old"] != m["new"]]


def normalize_categories(driver) -> int:
    """Idempotent category-spelling fix (notebook 13 cell 2): the extractor
    ignored the enum's casing, so case-sensitive category filters silently
    miss half the data without this."""
    distinct = [r["c"] for r in run_cypher(
        driver, "MATCH (rf:RiskFactor) RETURN DISTINCT rf.category AS c")]
    changes = category_mapping(distinct)
    if changes:
        with driver.session() as s:
            s.run("""UNWIND $rows AS row
                MATCH (rf:RiskFactor {category: row.old}) SET rf.category = row.new""",
                  rows=changes)
    logger.info("normalized %d category spellings", len(changes))
    return len(changes)


# Every disclosure edge is rewritten (never a filtered subset), so a 'Deleted' or an outdated status of an earlier build cannot
# survive; the properties of the retired closure are removed in the same pass. ``rf.is_current`` is the risk's evidence-section
# freshness (an amendment-restated section is not current); the Filing join is the "current ANNUAL filing" of the definition.
_STATUS_CYPHER = """MATCH (:Company)-[d:DISCLOSES_RISK]->(rf:RiskFactor)
    SET d.status = CASE WHEN rf.is_current = true AND EXISTS {
            MATCH (rf)-[:HAS_EVIDENCE]->(e:EvidenceSpan)
            MATCH (f:Filing {accession_no: e.accession_no})
            WHERE f.is_current = true AND f.form IN $annual
        } THEN 'Active' ELSE 'Historical' END
    REMOVE d.end_date, rf.lineage_id, rf.first_seen, rf.last_seen
    RETURN d.status AS status, count(*) AS n"""


def apply_current_status(driver) -> dict:
    """Set ``DISCLOSES_RISK.status``: ``Active`` iff the RiskFactor's filing is the company's current annual filing (and the
    risk's section is current), else ``Historical``. Idempotent; returns ``{status: edge count}``."""
    rows = run_cypher(driver, _STATUS_CYPHER, annual=list(ANNUAL_FORMS))
    counts = {r["status"]: r["n"] for r in rows}
    logger.info("risk disclosure status: %s", counts)
    return counts


# The annual filing current on a date: per company the latest effective annual filed on or before it (an overlay amendment such
# as an Item-7-only 10-K/A is never the annual itself; the sections it owns and that were filed by the date belong to the annual
# it amends).
_AS_OF_CYPHER = """MATCH (c:Company)-[:FILED]->(f:Filing)
    WHERE f.form IN $forms AND f.status IN $statuses AND f.filing_date <= date($asof)
      AND NOT (f)-[:AMENDS]->(:Filing){where_ticker}
    WITH c, f ORDER BY f.filing_date DESC, f.accession_no DESC
    WITH c, head(collect(f)) AS annual
    OPTIONAL MATCH (amend:Filing)-[:AMENDS]->(annual)
    WHERE amend.filing_date <= date($asof)
    WITH c, annual, [annual.accession_no] + collect(amend.accession_no) AS accessions
    MATCH (c)-[:DISCLOSES_RISK]->(rf:RiskFactor)-[:HAS_EVIDENCE]->(e:EvidenceSpan)
    WHERE e.accession_no IN accessions AND e.status IN $statuses
    RETURN DISTINCT c.name AS company, c.ticker AS ticker, annual.accession_no AS accession_no,
           toString(annual.filing_date) AS filing_date, rf.risk_id AS risk_id, rf.summary AS summary,
           rf.category AS category
    ORDER BY company, risk_id"""


def risks_active_as_of(driver, asof: str, ticker: str | None = None) -> list[dict]:
    """Time-travel query: the RiskFactors of the annual filing that was current on ``asof`` (ISO date), i.e. the latest effective
    annual filing filed on or before it, per company (or for one ``ticker``)."""
    query = _AS_OF_CYPHER.replace("{where_ticker}", "\n      AND c.ticker = $ticker" if ticker else "")
    return run_cypher(driver, query, asof=asof, forms=list(ANNUAL_FORMS), statuses=list(EFFECTIVE_FILING_STATUSES),
                      **({"ticker": ticker} if ticker else {}))
