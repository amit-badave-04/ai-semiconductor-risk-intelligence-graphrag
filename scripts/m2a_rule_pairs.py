"""M2a R11 pair report: run the rule linker over what is already on a LOCAL graph and list every (company, rule) pair
that is not yet linked there.

After the hand check (see ``hand_check`` in the output file) the wider matching was deferred, so the shipped linker is the
old one and this report must show ZERO new pairs; it is kept as the guard for any future widening.

Read-only: READ sessions only, a non-local Neo4j URI is refused, nothing is written to any database. Output:
``artifacts/m2a_rule_pairs.json`` (new pairs with rule title/abstract and the matched company risk text, plus per-company
examined/matched counts and the old and new edge counts). The ``decision`` and ``hand_check`` keys of an existing output
file are carried over: they are written by hand.

    PYTHONPATH=src uv run --no-sync python scripts/m2a_rule_pairs.py
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from semigraph.config import get_settings
from semigraph.graph.client import get_driver
from semigraph.graph.export_rules import EXPORT_CONTROLS_CATEGORY, _KIND_PATTERNS, build_affected_by, normalize_rule
from semigraph.universe import FILERS

OUT = Path(__file__).resolve().parent.parent / "artifacts" / "m2a_rule_pairs.json"
CUT = 600
HAND_KEYS = ("decision", "hand_check")
RULES_CYPHER = """MATCH (x:ExportControl) RETURN x.rule_id AS document_number, x.title AS title,
    toString(x.date) AS publication_date, x.abstract AS abstract, x.kind AS kind, x.topics AS topics, x.relevant AS relevant"""
EVIDENCE_CYPHER = """MATCH (c:Company)-[:DISCLOSES_RISK]->(rf:RiskFactor)-[:HAS_EVIDENCE]->(e:EvidenceSpan)
    WHERE toLower(rf.category) CONTAINS $category AND rf.is_current = true
    RETURN DISTINCT c.cik AS cik, e.chunk_id AS chunk_id, e.text AS text ORDER BY cik, chunk_id"""
EXISTING_CYPHER = "MATCH (c:Company)-[:AFFECTED_BY]->(x:ExportControl) RETURN c.cik AS cik, x.rule_id AS rule_id"
COMPANIES_CYPHER = "MATCH (c:Company) WHERE c.ticker IN $t RETURN c.cik AS cik, c.ticker AS ticker, c.name AS name"


def _read(driver, query: str, **params) -> list[dict]:
    with driver.session(default_access_mode="READ") as session:
        return [dict(r) for r in session.run(query, **params)]


def _window(kind: str, body: str) -> tuple[str | None, str]:
    """(matched keyword, the risk text centred on it, cut to CUT chars): a whole chunk is far longer than the match."""
    m = _KIND_PATTERNS[kind].search(body) if kind in _KIND_PATTERNS else None
    if m is None:
        return None, body[:CUT]
    start = max(0, m.start() - CUT // 2)
    return m.group(0), body[start:start + CUT]


def main() -> int:
    settings = get_settings()
    host = urlparse(settings.neo4j_uri).hostname
    if host not in ("localhost", "127.0.0.1", "::1"):
        print(f"refusing non-local Neo4j host {host!r}", file=sys.stderr)
        return 2
    driver = get_driver(settings)
    try:
        rules = [normalize_rule(r) for r in _read(driver, RULES_CYPHER)]
        exposures: dict[int, list[dict]] = {}
        for r in _read(driver, EVIDENCE_CYPHER, category=EXPORT_CONTROLS_CATEGORY):
            exposures.setdefault(r["cik"], []).append({"chunk_id": r["chunk_id"], "text": r["text"]})
        companies = _read(driver, COMPANIES_CYPHER, t=list(FILERS))
        existing = {(r["cik"], r["rule_id"]) for r in _read(driver, EXISTING_CYPHER)}
    finally:
        driver.close()
    rows, counts = build_affected_by(exposures, rules, [c["cik"] for c in companies])
    by_rule = {r["document_number"]: r for r in rules}
    by_cik = {c["cik"]: c for c in companies}
    text = {(cik, c["chunk_id"]): c["text"] for cik, chunks in exposures.items() for c in chunks}
    new = [{"ticker": by_cik[r["cik"]]["ticker"], "rule_id": r["rule_id"], "title": by_rule[r["rule_id"]]["title"],
            "kind": by_rule[r["rule_id"]]["kind"], "date": r["date"],
            "abstract": (by_rule[r["rule_id"]]["abstract"] or "")[:CUT],
            "matched_chunk_ids": r["chunks"],
            **dict(zip(("matched_keyword", "matched_risk_text"),
                       _window(by_rule[r["rule_id"]]["kind"], text[(r["cik"], r["chunks"][0])])))}
           for r in rows if (r["cik"], r["rule_id"]) not in existing and r["cik"] in by_cik]
    per_company = {by_cik[cik]["ticker"]: {**c, "old_links": sum(1 for k, _ in existing if k == cik),
                                          "new_links": sum(1 for r in rows if r["cik"] == cik),
                                          "new_pairs": sum(1 for n in new if n["ticker"] == by_cik[cik]["ticker"])}
                   for cik, c in counts.items() if cik in by_cik}
    prior = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    kept = {k: prior[k] for k in HAND_KEYS if k in prior}
    OUT.write_text(json.dumps({**kept, "graph": "local, READ sessions only", "old_edges": len(existing),
                               "new_edges": len(rows), "new_pairs_total": len(new), "per_company": per_company,
                               "new_pairs": new}, indent=2, ensure_ascii=False), encoding="utf-8")
    for t, c in sorted(per_company.items()):
        print(t, c)
    print("new pairs:", len(new), "-> ", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
