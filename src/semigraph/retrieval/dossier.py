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
item owns the ``removed`` / ``reworded``-kind sentence passages and the NEWER item owns the ``added``-kind ones.
The join for ``added`` passages is exact: :data:`semigraph.retrieval.retriever.TEMPORAL_QUERY`'s ``reworded`` row
reports the NEWER item's own id, and so does an ``added`` passage's ``item_id``.

For ``removed`` / ``reworded`` passages there is no equally exact join available HERE: a passage's own ``item_id``
IS the true older item's id (``graph/item_loader.py``), but the ``reworded`` temporal row never reports that id —
only the newer item's (plus, separately, that older item's ``chunk_ids``, copied onto the row as
``older_chunk_ids``). The precise fix is a :data:`semigraph.retrieval.retriever.TEMPORAL_QUERY` change (add
``o.item_id AS older_item_id`` to the ``reworded`` UNION member) — out of this file's ownership, reported as a
seam. Absent that, this module GROUPS removed/reworded passages by their own true ``item_id`` (each such group is
one real older item, whatever id a temporal row does or doesn't report) and assigns each group to the reworded
item whose ``older_chunk_ids`` has the best Jaccard overlap with that group's own chunk ids — a greedy one-to-one
matching, never "any overlap claims it" (:func:`_assign_older_owners`). Plain "any overlap" would double-attach a
group to every item sharing so much as one evidence chunk, which happens routinely: two ADJACENT paragraph items
can share one evidence chunk when a fixed-size chunk window straddles their boundary (see retriever.py's
``_LEAD_TEXT``, which exists precisely because one lead chunk can hold several paragraphs' worth of text). An item
with no chunk-id match at all (no ``older_chunk_ids``, or no group overlaps it) falls back to the OLDER headline —
the one join both sides still carry, and the only join possible before this file even existed. Scoping is always
to ONE pair (same ``cik``, ``older_accession``, ``newer_accession``, :func:`_pair_key`), so identical chunk ids or
headlines in two different pairs can never cross-attach either way. ``older_item_id`` is left ``None`` for a
changed item rather than invented (the older item's own id is not part of ``TEMPORAL_QUERY``'s output at all).
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


def _pair_key(row: dict) -> tuple:
    """The (cik, older_accession, newer_accession) a passage or item belongs to — the scope every join below stays
    inside, so identical chunk ids or headlines in two different pairs can never cross-attach."""
    return row.get("cik"), row.get("older_accession"), row.get("newer_accession")


def _index_passages(passages: list[dict]) -> dict:
    """Every passage keyed by its own pair (see :func:`_pair_key`) — every join below stays scoped to one pair."""
    by_pair: dict = {}
    for p in passages:
        by_pair.setdefault(_pair_key(p), []).append(p)
    return by_pair


def _group_passages_by_true_owner(pair_passages: list[dict]) -> dict:
    """``removed`` / ``reworded``-kind passages of ONE pair, grouped by their own true owner id
    (``passage.item_id`` — the older item's EXACT id; see the module docstring). A ``reworded`` temporal row never
    reports this id itself, which is exactly why the grouping (rather than a direct lookup) is needed."""
    owners: dict = {}
    for p in pair_passages:
        if p.get("kind") in ("removed", "reworded") and p.get("item_id"):
            owners.setdefault(p["item_id"], []).append(p)
    return owners


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _assign_older_owners(reworded_items: list[dict], owners: dict) -> dict[int, list[dict]]:
    """Best-overlap, one-to-one assignment of each older-passage owner (see :func:`_group_passages_by_true_owner`)
    to at most one of ``reworded_items`` (by position), and each item to at most one owner — a greedy
    maximum-Jaccard matching over chunk ids, never "any overlap claims it" (see the module docstring: two adjacent
    items can legitimately share one evidence chunk). Returns ``{item_index: passages}`` only for items an owner
    was actually matched to; an unassigned reworded item falls back to the older headline (its caller's concern)."""
    candidates = []
    for idx, item in enumerate(reworded_items):
        item_ids = set(item.get("older_chunk_ids") or [])
        if not item_ids:
            continue
        for owner_id, owner_passages in owners.items():
            owner_ids = {cid for p in owner_passages for cid in (p.get("chunk_ids") or [])}
            score = _jaccard(item_ids, owner_ids)
            if score > 0:
                candidates.append((score, idx, owner_id))
    candidates.sort(key=lambda c: c[0], reverse=True)
    assigned: dict[int, list[dict]] = {}
    claimed_owners: set = set()
    for _score, idx, owner_id in candidates:
        if idx in assigned or owner_id in claimed_owners:
            continue
        assigned[idx] = owners[owner_id]
        claimed_owners.add(owner_id)
    return assigned


def _headline_fallback_older(item: dict, pair_passages: list[dict]) -> list[dict]:
    """Used only for a reworded item no chunk-id overlap ever assigned an owner to (no ``older_chunk_ids`` at all,
    or none of the pair's owners overlap it) — the older headline, the one join both sides still carry."""
    older_headline = item.get("older_headline")
    if not older_headline:
        return []
    return [p for p in pair_passages
            if p.get("kind") in ("removed", "reworded") and p.get("item_headline") == older_headline]


def _item_passages(item: dict, assigned_older: list[dict] | None, pair_passages: list[dict]) -> list[dict]:
    """Only a ``reworded`` (dossier kind ``changed``) item ever has passages — a whole-item ``removed``, ``new`` or
    ``unsettled`` row has no "before" and "after" text to diff, so ``compute_passages`` never produces one for it
    (see the module docstring)."""
    if item.get("change") != "reworded":
        return []
    older_side = assigned_older if assigned_older is not None else _headline_fallback_older(item, pair_passages)
    newer_side = [p for p in pair_passages if p.get("kind") == "added" and p.get("item_id") == item.get("item_id")]
    return [_passage_entry(p) for p in older_side] + [_passage_entry(p) for p in newer_side]


def _shape_item(item: dict, assigned_older: list[dict] | None, pair_passages: list[dict]) -> dict:
    change = item.get("change")
    return {
        "kind": _KIND_MAP.get(change, change),
        "headline": item.get("headline") or item.get("older_headline"),
        "older_item_id": item.get("item_id") if change == "removed" else None,
        "newer_item_id": item.get("item_id") if change in ("new", "reworded") else None,
        "passages": _item_passages(item, assigned_older, pair_passages),
    }


def _shape_pair(pair: dict, pair_items: list[dict], by_pair: dict) -> dict:
    pair_passages = by_pair.get(_pair_key(pair), [])
    owners = _group_passages_by_true_owner(pair_passages)
    reworded_indices = [i for i, it in enumerate(pair_items) if it.get("change") == "reworded"]
    assigned = _assign_older_owners([pair_items[i] for i in reworded_indices], owners)
    assigned_by_item_index = {reworded_indices[local]: passages for local, passages in assigned.items()}
    return {
        "older": {"accession_no": pair.get("older_accession"), "form": pair.get("older_form"),
                  "filing_date": pair.get("older_date")},
        "newer": {"accession_no": pair.get("newer_accession"), "form": pair.get("newer_form"),
                  "filing_date": pair.get("newer_date")},
        "compared": pair.get("compared"),
        "not_compared_reason": pair.get("not_compared_reason"),
        "items": [_shape_item(it, assigned_by_item_index.get(i), pair_passages)
                 for i, it in enumerate(pair_items)],
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
    by_pair = _index_passages(passages)

    items_by_pair: dict = {}
    for it in items:
        items_by_pair.setdefault((it.get("cik"), it.get("newer_accession")), []).append(it)
    shaped_pairs = [_shape_pair(pair, items_by_pair.get((pair.get("cik"), pair.get("newer_accession")), []), by_pair)
                    for pair in pairs_with_totals]
    return {"company": {"ticker": ticker, "name": company["name"], "cik": cik}, "pairs": shaped_pairs}
