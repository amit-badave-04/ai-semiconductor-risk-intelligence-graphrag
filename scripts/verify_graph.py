"""Read-only verification of a built semigraph graph: counts, freshness invariants, index health.

    set -a; . ./.env.community; set +a
    PYTHONPATH=src python scripts/verify_graph.py

Exits non-zero when any invariant fails. Uses NEO4J_URI / NEO4J_PASSWORD / NEO4J_DATABASE.
Every check is one Cypher query returning the offending rows (expected: none) or a value.
"""

import sys

from semigraph.config import get_settings
from semigraph.graph import client

# (name, cypher, kind): "none" = must return no rows; "info" = printed only.
CHECKS = [
    ("no current span under a non-current filing (except an overlay amendment's owner)", """
        MATCH (e:EvidenceSpan {is_current: true})-[:FROM_SECTION]->(:FilingSection)<-[:HAS_SECTION]-(f:Filing)
        WHERE f.is_current = false RETURN f.accession_no AS accession, count(e) AS spans""", "none"),
    ("every span carries the freshness contract", """
        MATCH (e:EvidenceSpan)
        WHERE e.is_current IS NULL OR e.retrievable IS NULL OR e.filer_cik IS NULL OR e.valid_from IS NULL
           OR e.valid_to IS NULL OR e.status IS NULL OR e.content_hash IS NULL OR e.snapshot_id IS NULL
        RETURN e.chunk_id AS chunk LIMIT 10""", "none"),
    ("filer_cik is an integer everywhere (filtered search compares by type)", """
        MATCH (n) WHERE (n:EvidenceSpan OR n:RiskFactor) AND NOT n.filer_cik IS :: INTEGER
        RETURN labels(n)[0] AS label, count(n) AS n""", "none"),
    ("at most one current quarterly per company", """
        MATCH (c:Company)-[:FILED]->(f:Filing {is_current: true, form: '10-Q'})
        WITH c, count(f) AS n WHERE n > 1 RETURN c.name AS company, n""", "none"),
    ("current annual exists for every SEC filer", """
        MATCH (c:Company {sec_filer: true})
        WHERE NOT (c)-[:FILED]->(:Filing {is_current: true})
        RETURN c.name AS company""", "none"),
    ("a corrected section is never retrievable", """
        MATCH (e:EvidenceSpan {status: 'corrected'}) WHERE e.retrievable = true OR e.is_current = true
        RETURN e.chunk_id AS chunk LIMIT 10""", "none"),
    ("superseded non-risk annual text is not retrievable by default", """
        MATCH (e:EvidenceSpan {status: 'superseded', retrievable: true})
        WHERE NOT e.section_id IN ['I.1A', 'I.3']
        RETURN e.form AS form, e.section_id AS section, count(e) AS spans""", "none"),
    ("every Active risk has evidence", """
        MATCH (:Company)-[:DISCLOSES_RISK {status: 'Active'}]->(rf:RiskFactor)
        WHERE NOT (rf)-[:HAS_EVIDENCE]->(:EvidenceSpan) RETURN rf.risk_id AS risk LIMIT 10""", "none"),
    ("AFFECTED_BY only for relevant rules", """
        MATCH (:Company)-[:AFFECTED_BY]->(x:ExportControl) WHERE x.relevant = false
        RETURN x.rule_id AS rule LIMIT 10""", "none"),
    ("vector indexes online", """
        SHOW INDEXES YIELD name, type, state WHERE type = 'VECTOR' AND state <> 'ONLINE'
        RETURN name, state""", "none"),
    ("node counts", """
        MATCH (n) WHERE NOT any(l IN labels(n) WHERE l STARTS WITH 'Svc')
        RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC""", "info"),
    ("relationship counts", "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n ORDER BY n DESC", "info"),
    ("filings by status", """
        MATCH (f:Filing) RETURN f.form AS form, f.status AS status, f.is_current AS current, count(*) AS n
        ORDER BY form, status""", "info"),
    ("spans by status", """
        MATCH (e:EvidenceSpan) RETURN e.status AS status, e.is_current AS current, e.retrievable AS retrievable,
               count(*) AS spans ORDER BY spans DESC""", "info"),
    ("AMD: filings", """
        MATCH (:Company {ticker: 'AMD'})-[:FILED]->(f:Filing)
        RETURN f.form AS form, toString(f.filing_date) AS filed, f.status AS status, f.is_current AS current,
               f.corrected_sections AS corrected_sections, f.accession_no AS accession ORDER BY filed""", "info"),
    ("AMD: the 10-K/A restates only Item 7 — spans of the two FY2025 annuals by section", """
        MATCH (f:Filing) WHERE f.accession_no IN ['0000002488-26-000018', '0000002488-26-000021']
        MATCH (f)-[:HAS_SECTION]->(:FilingSection)<-[:FROM_SECTION]-(e:EvidenceSpan)
        RETURN f.form AS form, e.section_id AS section, e.status AS status, e.is_current AS current,
               e.retrievable AS retrievable, count(e) AS spans ORDER BY form, section""", "info"),
    ("snapshot", "MATCH (s:Snapshot) RETURN s.id AS id, toString(s.as_of) AS as_of, s.code_version AS code", "info"),
]


def main() -> int:
    driver = client.get_driver(get_settings())
    failures = 0
    try:
        for name, query, kind in CHECKS:
            rows = client.run_cypher(driver, query)
            if kind == "none":
                status = "PASS" if not rows else "FAIL"
                failures += bool(rows)
                print(f"[{status}] {name}")
                for row in rows[:10]:
                    print(f"        {row}")
            else:
                print(f"[info] {name}")
                for row in rows[:30]:
                    print(f"        {row}")
    finally:
        driver.close()
    print(f"\n{failures} invariant failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
