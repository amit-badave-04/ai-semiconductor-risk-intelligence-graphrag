"""Company dossier + risk-change data (M4, docs/v2/M4_PLAN.md 4.5): D8, read-only, no LLM, no embedder — two plain
shapings over existing graph queries for ``GET /api/company/{ticker}/dossier`` and
``GET /api/company/{ticker}/risk-changes``. Pure functions over ``retriever.py`` constants (read, never edited) plus
one small graph read of its own (the filings timeline and the plain, un-vectorised active-risks list, both things
the retriever's SEARCH-based ``hybrid_retrieve`` path has no reason to expose as-is).

Passage-to-item join note: ``graph/passages.py``'s ``compute_passages`` computes "removed / reworded / added passages
of the items that CHANGED between two consecutive filings" — i.e. only items the aligner MATCHED across versions
(``changed`` / reworded items) ever get a ``RiskPassage``. A wholly ``dropped`` or wholly ``new`` item never has one
(there is no "before" or "after" text to diff against), so those kinds always carry ``passages: []`` here — not a
join failure, the data genuinely has none. Within a changed item's pair, ``graph/item_loader.py`` says the OLDER
item owns the ``removed`` / ``reworded``-kind sentence passages and the NEWER item owns the ``added``-kind ones,
while :data:`semigraph.retrieval.retriever.TEMPORAL_QUERY`'s own ``reworded`` row reports only the NEWER item's id.
So the join for ``added`` passages is exact (same newer item id on both sides); for ``removed`` / ``reworded``
passages it falls back to matching the passage's ``item_headline`` against the temporal row's ``older_headline`` —
the only field both sides carry for that same older item. ``older_item_id`` is left ``None`` for a changed item
rather than invented (the older item's own id is not part of this query's output at all).
"""

from __future__ import annotations

from ..graph.client import run_cypher
from ..universe import FILERS
from .retriever import (
    METRICS_QUERY,
    PASSAGE_CAPS,
    PASSAGES_QUERY,
    RULE_EDGES_QUERY,
    RULES_PER_COMPANY,
    TEMPORAL_CAPS,
    TEMPORAL_QUERY,
    company_edges_query,
    select_passages,
    select_temporal,
)

RISK_CHANGES_LIMIT_CAP = 50
DOSSIER_METRIC_PERIODS = 2   # "the latest two fiscal years" (docs/v2/M4_PLAN.md 4.5)

COMPANY_QUERY = "MATCH (c:Company {ticker: $ticker}) RETURN c.cik AS cik, c.name AS name"

FILINGS_QUERY = """MATCH (c:Company {cik: $cik})-[:FILED]->(f:Filing)
RETURN f.accession_no AS accession_no, f.form AS form, toString(f.filing_date) AS filing_date,
       f.is_current AS is_current, f.status AS status, f.superseded_by AS superseded_by
ORDER BY f.filing_date DESC"""

# The exact query docs/v2/M4_PLAN.md 4.5 gives for "active_risks": a plain graph traversal, deliberately NOT the
# vector-SEARCH `ACTIVE_RISKS_QUERY` of retriever.py (no `$vec`, no embedder — D8).
DOSSIER_ACTIVE_RISKS_QUERY = """MATCH (c:Company {cik: $cik})-[d:DISCLOSES_RISK {status: 'Active'}]->(rf:RiskFactor)
WHERE rf.is_current
RETURN rf.risk_id AS risk_id, rf.summary AS summary, rf.category AS category,
       toString(d.last_evidenced_at) AS last_evidenced_at
ORDER BY d.last_evidenced_at DESC LIMIT 25"""

# TEMPORAL_QUERY's own change vocabulary -> the plan's dossier vocabulary. "unsettled" (the text check could not
# settle whether an older item was really removed) is kept as its own kind rather than folded into "dropped": this
# codebase is careful never to overclaim a removal (see retriever.py's TEMPORAL_QUERY docstring).
_KIND_MAP = {"removed": "dropped", "new": "new", "reworded": "changed", "unsettled": "unsettled"}


def _resolve_company(driver, ticker: str) -> dict | None:
    rows = run_cypher(driver, COMPANY_QUERY, ticker=ticker)
    return rows[0] if rows else None


def get_dossier(driver, ticker: str, *, data_as_of: str | None = None) -> dict | None:
    """None for a ticker outside :data:`semigraph.universe.FILERS` or absent from the graph (the route 404s either)."""
    if ticker not in FILERS:
        return None
    company = _resolve_company(driver, ticker)
    if company is None:
        return None
    cik = company["cik"]
    return {
        "company": {"ticker": ticker, "name": company["name"], "cik": cik},
        "data_as_of": data_as_of,
        "filings": run_cypher(driver, FILINGS_QUERY, cik=cik),
        "metrics": run_cypher(driver, METRICS_QUERY, ids=[cik], periods=DOSSIER_METRIC_PERIODS, years=[], dates=[]),
        "active_risks": run_cypher(driver, DOSSIER_ACTIVE_RISKS_QUERY, cik=cik),
        "edges": run_cypher(driver, company_edges_query(1), ids=[cik]),
        "rules": run_cypher(driver, RULE_EDGES_QUERY, ids=[cik], include_neighbours=False,
                            per_company=RULES_PER_COMPANY),
    }


def _passage_entry(row: dict) -> dict:
    side = "older" if row.get("kind") in ("removed", "reworded") else "newer"
    chunk_ids = row.get("chunk_ids") or []
    return {"quote": row.get("text"), "chunk_id": chunk_ids[0] if chunk_ids else None, "side": side}


def _item_passages(item: dict, by_item_id: dict, by_older_headline: dict) -> list[dict]:
    """Only a ``reworded`` (dossier kind ``changed``) item ever has passages — a whole-item ``removed``, ``new`` or
    ``unsettled`` row has no "before" and "after" text to diff, so ``compute_passages`` never produces one for it
    (see the module docstring)."""
    if item.get("change") != "reworded":
        return []
    older_side = by_older_headline.get(item.get("older_headline"), [])
    newer_side = by_item_id.get(item.get("item_id"), [])
    return [_passage_entry(p) for p in older_side if p.get("kind") in ("removed", "reworded")] + \
        [_passage_entry(p) for p in newer_side if p.get("kind") == "added"]


def _index_passages(passages: list[dict]) -> tuple[dict, dict]:
    """``by_item_id``: every passage keyed by its OWN item id (exact for ``added``, whose owner is the newer item —
    the same id :data:`semigraph.retrieval.retriever.TEMPORAL_QUERY` reports for a reworded row). ``by_older_headline``:
    ``removed`` / ``reworded``-kind passages keyed by the OLDER item's headline, the fallback join (see module
    docstring)."""
    by_item_id: dict = {}
    by_older_headline: dict = {}
    for p in passages:
        by_item_id.setdefault(p.get("item_id"), []).append(p)
        if p.get("kind") in ("removed", "reworded") and p.get("item_headline"):
            by_older_headline.setdefault(p.get("item_headline"), []).append(p)
    return by_item_id, by_older_headline


def _shape_item(item: dict, by_item_id: dict, by_older_headline: dict) -> dict:
    change = item.get("change")
    return {
        "kind": _KIND_MAP.get(change, change),
        "headline": item.get("headline") or item.get("older_headline"),
        "older_item_id": item.get("item_id") if change == "removed" else None,
        "newer_item_id": item.get("item_id") if change in ("new", "reworded") else None,
        "passages": _item_passages(item, by_item_id, by_older_headline),
    }


def _shape_pair(pair: dict, pair_items: list[dict], by_item_id: dict, by_older_headline: dict) -> dict:
    return {
        "older": {"accession_no": pair.get("older_accession"), "form": pair.get("older_form"),
                  "filing_date": pair.get("older_date")},
        "newer": {"accession_no": pair.get("newer_accession"), "form": pair.get("newer_form"),
                  "filing_date": pair.get("newer_date")},
        "compared": pair.get("compared"),
        "not_compared_reason": pair.get("not_compared_reason"),
        "items": [_shape_item(it, by_item_id, by_older_headline) for it in pair_items],
    }


def get_risk_changes(driver, ticker: str, *, limit: int = 20) -> dict | None:
    """None for a ticker outside FILERS or absent from the graph. ``limit`` is capped at
    :data:`RISK_CHANGES_LIMIT_CAP`; pairs the loader could not compare are kept, with their reason, never dropped."""
    if ticker not in FILERS:
        return None
    company = _resolve_company(driver, ticker)
    if company is None:
        return None
    cik = company["cik"]
    capped = min(max(1, limit), RISK_CHANGES_LIMIT_CAP)
    caps = {kind: capped for kind in TEMPORAL_CAPS}
    passage_caps = {kind: capped for kind in PASSAGE_CAPS}   # PASSAGE_CAPS (4-8) would otherwise silently
                                                              # truncate a caller's larger limit (e.g. 50)

    rows = run_cypher(driver, TEMPORAL_QUERY, ids=[cik])
    items, pairs = select_temporal(rows, "", caps=caps)
    comparable = [{"cik": p["cik"], "older": p["older_accession"], "newer": p["newer_accession"]}
                  for p in pairs if p.get("compared", True)]
    passage_rows = run_cypher(driver, PASSAGES_QUERY, pairs=comparable) if comparable else []
    passages, pairs_with_totals = select_passages(passage_rows, pairs, "", caps=passage_caps)
    by_item_id, by_older_headline = _index_passages(passages)

    items_by_pair: dict = {}
    for it in items:
        items_by_pair.setdefault((it.get("cik"), it.get("newer_accession")), []).append(it)
    shaped_pairs = [_shape_pair(pair, items_by_pair.get((pair.get("cik"), pair.get("newer_accession")), []),
                                by_item_id, by_older_headline)
                    for pair in pairs_with_totals]
    return {"company": {"ticker": ticker, "name": company["name"], "cik": cik}, "pairs": shaped_pairs}
