"""Bitemporal versioning — ported from notebook 13 (Milestone M5).

Notebook 12 loads risk factors from multiple annual filings per company with
every DISCLOSES_RISK edge 'Active'; this module operates the bitemporal
pattern that made temporal questions score 100% vs 0% for vector-only RAG:

1. Cluster each company's risks across annual filings by embedding
   similarity — "the same risk, re-disclosed each year" becomes one lineage
2. Backdate start_date to the lineage's first disclosure
3. Close lineages absent from the company's latest annual filing:
   status='Deleted', end_date = latest filing date
4. As-of / time-travel queries

The clustering + state computation is pure (no Neo4j) so it is unit-testable;
appliers are thin Cypher writes. All updates are idempotent.
"""

import logging

import numpy as np
import pandas as pd

from ..config import Settings, get_settings
from .client import run_cypher

logger = logging.getLogger("semigraph.graph.temporal")

# cosine similarity above which two summaries are the same recurring risk
SAME_RISK_SIM = 0.75

# Graph-side canonical category spellings (notebook 13 cell 2 — includes
# 'Cybersecurity', which the extractor invented often enough to canonize).
CANONICAL_CATEGORIES = [
    "Supply Chain", "Geopolitical", "Export Controls", "Demand", "Competition",
    "Technology", "Legal/Regulatory", "Financial", "Cybersecurity", "Other",
]


# --------------------------------------------------------------------------
# pure core
# --------------------------------------------------------------------------

def category_mapping(distinct: list[str]) -> list[dict]:
    """Map free-text category spellings to canonical ones (case-insensitive
    hit -> canonical spelling; miss -> Title Case). Returns only changes."""
    canon_by_lower = {c.lower(): c for c in CANONICAL_CATEGORIES}
    mapping = [
        {"old": c, "new": canon_by_lower.get(c.strip().lower(), c.strip().title())}
        for c in distinct if c
    ]
    return [m for m in mapping if m["old"] != m["new"]]


def cluster_lineages(embeddings: np.ndarray,
                     threshold: float = SAME_RISK_SIM) -> list[list[int]]:
    """Greedy lineage clustering over date-ordered risk embeddings.

    Each risk joins the most similar existing lineage above the threshold,
    else starts a new one; centroids are renormalized means. Deterministic
    and easy to audit (notebook 13 cell 6)."""
    lineages: list[dict] = []
    for i in range(len(embeddings)):
        best, best_sim = None, 0.0
        for lin in lineages:
            sim = float(embeddings[i] @ lin["centroid"])
            if sim > best_sim:
                best, best_sim = lin, sim
        if best is not None and best_sim >= threshold:
            best["members"].append(i)
            member_vecs = embeddings[best["members"]]
            centroid = member_vecs.mean(axis=0)
            best["centroid"] = centroid / np.linalg.norm(centroid)
        else:
            lineages.append({"centroid": embeddings[i], "members": [i]})
    return [lin["members"] for lin in lineages]


def compute_temporal_states(risks: pd.DataFrame,
                            threshold: float = SAME_RISK_SIM
                            ) -> tuple[pd.DataFrame, list[dict]]:
    """Compute per-risk temporal states from annual-filing risk disclosures.

    ``risks`` columns: cik, company, risk_id, summary, category, embedding
    (list or ndarray), filing_date (datetime-like), accession_no.

    Returns (updates_df, lineage_stats): one update row per risk node with
    lineage_id / first_seen / last_seen / status / end_date, and per-company
    lineage statistics. A lineage is closed when the company's latest annual
    no longer discloses it AND the company has more than one annual filing.
    """
    risks = risks.copy()
    risks["filing_date"] = pd.to_datetime(risks["filing_date"])
    updates, lineage_stats = [], []
    for (cik, company), grp in risks.groupby(["cik", "company"]):
        grp = grp.sort_values("filing_date")
        latest_annual = grp["filing_date"].max()
        n_annuals = grp["accession_no"].nunique()
        embs = np.vstack(grp["embedding"].to_numpy())
        lineages = cluster_lineages(embs, threshold)
        n_closed = 0
        for li, members_idx in enumerate(lineages):
            members = grp.iloc[members_idx]
            first_seen = members["filing_date"].min()
            last_seen = members["filing_date"].max()
            closed = (last_seen < latest_annual) and (n_annuals > 1)
            n_closed += int(closed)
            for _, m in members.iterrows():
                updates.append({
                    "risk_id": m["risk_id"], "lineage_id": f"{cik}:{li}",
                    "first_seen": str(first_seen.date()),
                    "last_seen": str(last_seen.date()),
                    "status": "Deleted" if closed else "Active",
                    "end_date": str(latest_annual.date()) if closed else None,
                })
        lineage_stats.append({
            "company": company, "annuals": n_annuals, "risk_nodes": len(grp),
            "lineages": len(lineages), "closed_lineages": n_closed,
        })
    return pd.DataFrame(updates), lineage_stats


# --------------------------------------------------------------------------
# thin Cypher appliers
# --------------------------------------------------------------------------

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


def fetch_annual_risks(driver) -> pd.DataFrame:
    """Risk disclosures evidenced by annual filings (10-K/20-F), with
    embeddings, for lineage clustering (notebook 13 cell 4)."""
    rows = run_cypher(driver, """
        MATCH (c:Company)-[:DISCLOSES_RISK]->(rf:RiskFactor)-[:HAS_EVIDENCE]->(e:EvidenceSpan)
              -[:FROM_SECTION]->(:FilingSection)<-[:HAS_SECTION]-(f:Filing)
        WHERE f.form IN ['10-K', '20-F'] AND rf.embedding IS NOT NULL
        RETURN DISTINCT c.cik AS cik, c.name AS company, rf.risk_id AS risk_id,
               rf.summary AS summary, rf.category AS category, rf.embedding AS embedding,
               toString(f.filing_date) AS filing_date, f.accession_no AS accession_no
    """)
    return pd.DataFrame(rows)


def write_temporal_states(driver, updates_df: pd.DataFrame) -> dict:
    """Write lineage + bitemporal state to nodes and DISCLOSES_RISK edges
    (notebook 13 cell 8; idempotent)."""
    with driver.session() as s:
        s.run("""UNWIND $rows AS row
            MATCH (:Company)-[d:DISCLOSES_RISK]->(rf:RiskFactor {risk_id: row.risk_id})
            SET rf.lineage_id = row.lineage_id, rf.first_seen = date(row.first_seen),
                rf.last_seen = date(row.last_seen),
                d.start_date = date(row.first_seen), d.status = row.status,
                d.end_date = CASE WHEN row.end_date IS NULL THEN null ELSE date(row.end_date) END""",
              rows=updates_df.to_dict("records"))
        counts = s.run(
            "MATCH ()-[d:DISCLOSES_RISK]->() RETURN d.status AS status, count(*) AS n"
        ).data()
    return {c["status"]: c["n"] for c in counts}


def apply_closure(driver, settings: Settings | None = None) -> dict:
    """Full bitemporal pass: normalize categories, cluster lineages, write
    states. Safe to re-run — recomputes the same states."""
    settings = settings or get_settings()
    normalize_categories(driver)
    risks = fetch_annual_risks(driver)
    if risks.empty:
        logger.warning("no annual-filing risk disclosures found — nothing to close")
        return {}
    updates_df, stats = compute_temporal_states(risks)
    status_counts = write_temporal_states(driver, updates_df)
    n_deleted = int((updates_df["status"] == "Deleted").sum())
    logger.info("%d risk states written (%d deleted) across %d companies",
                len(updates_df), n_deleted, len(stats))
    return {"states_written": len(updates_df), "deleted": n_deleted,
            "companies": len(stats), "edge_status_counts": status_counts}


def risks_active_as_of(driver, asof: str, ticker: str | None = None) -> list[dict]:
    """Time-travel query: risk lineages active on a date
    (start <= asof AND (no end OR end > asof)) — notebook 13 cell 11."""
    where_ticker = "AND c.ticker = $ticker" if ticker else ""
    return run_cypher(driver, f"""
        MATCH (c:Company)-[d:DISCLOSES_RISK]->(rf:RiskFactor)
        WHERE d.start_date <= date($asof) AND (d.end_date IS NULL OR d.end_date > date($asof))
        {where_ticker}
        RETURN c.name AS company, count(DISTINCT rf.lineage_id) AS active_risk_lineages
        ORDER BY active_risk_lineages DESC""",
        asof=asof, **({"ticker": ticker} if ticker else {}))
