"""Loads the text-grounded risk-item layer (``align-items`` output) into Neo4j (M1b step 4; contract: M1B_PLAN L.7).

Nodes and edges (every one stamped with the build's ``snapshot_id``):

* ``(:RiskItem {item_id, accession_no, filer_cik, filing_date, section_id, seq, headline, text_hash, char_start, char_end,
  unit_kind, chunk_ids, is_current, lineage_id, removed_in, is_new, snapshot_id})`` from the risk-item parquet rows (NOT from the
  decisions: an item appears in two pairs, once as the newer and once as the older side).
  ``removed_in`` = the newer accession, set only on an older item labelled ``removed`` in a COMPARED pair; ``is_new`` = a newer
  item labelled ``new`` in a compared pair (an item of the first filing of a ticker is never new); ``is_current`` = the item's
  section is current under ``semigraph.versions`` (the rule that sets ``RiskFactor.is_current``).
* ``(item)-[:IN_SECTION]->(:FilingSection)``, ``(item)-[:SPANS]->(:EvidenceSpan)`` per chunk id (MATCH only: a chunk that is
  not an EvidenceSpan gets no edge; the ``chunk_ids`` property keeps every id), ``(older)-[:SUCCEEDED_BY {kind, decided_by,
  sim_embed, sim_lex}]->(newer)`` for a decision ``unchanged`` / ``reworded`` / ``merged`` that names its counterpart (an
  ``uncertain`` item is present but carries no edge; a ``merged`` one whose absorbing item is unknown has none either).
* ``(:RiskPassage {passage_id, kind, item_id, older_accession, newer_accession, filer_cik, text, counterpart_text, similarity,
  char_start, char_end, chunk_ids, counterpart_chunk_ids, chunk_fallback, decided_by, snapshot_id})`` with
  ``(item)-[:HAS_PASSAGE]->(passage)`` (the older item for removed / reworded, the newer for added).
* ``SUPERSEDES {items_compared, not_compared_reason}`` is SET on the EXISTING ``(newer)-[:SUPERSEDES]->(older)`` filing edge of
  EVERY consecutive annual pair (compared or not); ``kind`` is never touched and no edge is invented (a pair with no edge
  raises: the manifest and the item files disagree). A not-compared pair gets no SUCCEEDED_BY / removed_in / is_new / passage.
* ``(:RiskFactor)-[:OF_ITEM]->(:RiskItem)`` for every item whose ``chunk_ids`` contain the risk's evidence chunk (many-to-many:
  a chunk can sit inside two items).

``lineage_id`` = ``lin:<ticker>:<item_id of the first item of the chain>``; a chain is followed along ``unchanged`` and
``reworded`` successors only (one-to-one). A ``merged`` or ``uncertain`` decision ends the chain: the item that absorbed
another starts or continues its OWN lineage.

The loader REPLACES the item layer of the tickers it loads (their passages, SUCCEEDED_BY / IN_SECTION / SPANS / OF_ITEM edges,
items no longer in the parquet and the ``removed_in`` / ``is_new`` flags are rewritten), so a re-run after a changed alignment
never keeps a drop it no longer makes. Missing or stale alignment files stop it with "run semigraph align-items first".
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from neo4j import Driver

from ..config import Settings, get_settings
from .client import run_cypher
from .freshness import span_freshness
from .items import (
    AlignItemsError,
    alignment_dir,
    check_alignment_consistent,
    items_dir,
    require_alignment,
    table_path,
)
from .loaders import _load_manifest, _read_chunks, _resolve_snapshot_id, _run_batched, _ticker_freshness

logger = logging.getLogger("semigraph.graph.item_loader")

CHAIN_KINDS = ("unchanged", "reworded")          # successors that carry the lineage id
EDGE_KINDS = ("unchanged", "reworded", "merged")  # SUCCEEDED_BY kinds
LINEAGE_PREFIX = "lin"


@dataclass(frozen=True)
class ItemLayer:
    """The rows one ticker contributes to the graph (all plain Python values, ready for UNWIND)."""

    ticker: str
    items: list[dict]
    in_section: list[dict]
    spans: list[dict]
    succeeded_by: list[dict]
    passages: list[dict]
    supersedes: list[dict]


def _none(value: Any) -> Any:
    """NaN / pandas NA -> None (NaN is not a Cypher null and ``coalesce`` would not catch it)."""
    return None if value is None or (isinstance(value, float) and value != value) or value is pd.NA else value


def _clean(row: Mapping[str, Any], ints: Iterable[str] = (), lists: Iterable[str] = ()) -> dict:
    out = {k: _none(v) for k, v in row.items()}
    for k in ints:
        out[k] = None if out[k] is None else int(out[k])
    for k in lists:
        out[k] = [str(c) for c in (out[k] if out[k] is not None else [])]
    return out


def lineage_ids(ticker: str, items: Sequence[Mapping[str, Any]], edges: Sequence[tuple[str, str, str]]) -> dict[str, str]:
    """``item_id -> lineage id`` for one ticker.

    ``items``: rows with ``item_id``, ``filing_date``, ``accession_no``, ``char_start``; ``edges``: ``(older, newer, kind)``.
    Items are visited in filing order; an item with a ``unchanged`` / ``reworded`` predecessor inherits its lineage (with several,
    the predecessor with the smallest ``(filing_date, item_id)``), any other item starts its own: ``lin:<ticker>:<item_id>``."""
    order = sorted(items, key=lambda r: (str(r["filing_date"]), r["accession_no"], r["char_start"], r["item_id"]))
    date_of = {r["item_id"]: (str(r["filing_date"]), r["item_id"]) for r in items}
    predecessors: dict[str, list[str]] = {}
    for old, new, kind in edges:
        if kind in CHAIN_KINDS:
            predecessors.setdefault(new, []).append(old)
    lineage: dict[str, str] = {}
    for row in order:
        item_id = row["item_id"]
        primary = min((p for p in predecessors.get(item_id, []) if p in lineage), key=lambda p: date_of[p], default=None)
        lineage[item_id] = lineage[primary] if primary else f"{LINEAGE_PREFIX}:{ticker}:{item_id}"
    return lineage


def build_item_layer(ticker: str, items: pd.DataFrame, pairs: pd.DataFrame, decisions: pd.DataFrame, passages: pd.DataFrame,
                     current: Mapping[str, bool]) -> ItemLayer:
    """The graph rows of one ticker (pure). ``current``: item id -> whether its section is current."""
    check_alignment_consistent(ticker, items, pairs, decisions, passages)
    newer_accession = dict(zip(pairs["pair_id"], pairs["newer_accession"]))
    older = decisions[decisions["side"] == "older"]
    removed_in = {r.item_id: newer_accession[r.pair_id] for r in older.itertuples() if r.label == "removed"}
    is_new = set(decisions.loc[(decisions["side"] == "newer") & (decisions["label"] == "new"), "item_id"])
    edges = [(r.item_id, r.matched_item_id, r.label) for r in older.itertuples()
             if r.label in EDGE_KINDS and isinstance(r.matched_item_id, str) and r.matched_item_id]
    lineage = lineage_ids(ticker, items.to_dict("records"), edges)
    rows = [_clean(r, ints=("filer_cik", "char_start", "char_end", "seq"), lists=("chunk_ids",))
            for r in items.sort_values(["filing_date", "accession_no", "char_start", "item_id"], kind="stable").to_dict("records")]
    item_rows = [{
        "item_id": r["item_id"], "accession_no": r["accession_no"], "filer_cik": r["filer_cik"],
        "filing_date": str(r["filing_date"])[:10], "section_id": r["section_id"], "seq": r["seq"],
        "headline": r["headline"] or "", "text_hash": r["text_hash"], "char_start": r["char_start"], "char_end": r["char_end"],
        "unit_kind": r["unit_kind"], "chunk_ids": r["chunk_ids"], "is_current": bool(current.get(r["item_id"], False)),
        "lineage_id": lineage[r["item_id"]], "removed_in": removed_in.get(r["item_id"]), "is_new": r["item_id"] in is_new}
        for r in rows]
    edge_keys = set(edges)
    succeeded = [{"old": r.item_id, "new": r.matched_item_id, "kind": r.label, "decided_by": r.decided_by,
                  "sim_embed": _none(r.sim_embed), "sim_lex": _none(r.sim_lex)}
                 for r in older.itertuples() if (r.item_id, r.matched_item_id, r.label) in edge_keys]
    passage_rows = [_clean(r, ints=("filer_cik", "char_start", "char_end"), lists=("chunk_ids", "counterpart_chunk_ids"))
                    for r in passages.to_dict("records")]
    passage_keys = ("passage_id", "kind", "item_id", "older_accession", "newer_accession", "filer_cik", "text", "counterpart_text",
                    "similarity", "char_start", "char_end", "chunk_ids", "counterpart_chunk_ids", "chunk_fallback", "decided_by")
    supersedes = [{"newer": p.newer_accession, "older": p.older_accession, "items_compared": bool(p.comparable),
                   "reason": None if p.comparable else _none(p.not_compared_reason)} for p in pairs.itertuples()]
    return ItemLayer(
        ticker, item_rows,
        [{"item_id": r["item_id"], "section_key": f"{r['accession_no']}:{r['section_id']}"} for r in item_rows],
        [{"item_id": r["item_id"], "chunk_id": c} for r in item_rows for c in r["chunk_ids"]],
        succeeded, [{k: p[k] for k in passage_keys} for p in passage_rows], supersedes)


def of_item_rows(risk_evidence: Iterable[Mapping[str, Any]], items: Sequence[Mapping[str, Any]]) -> list[dict]:
    """``(risk_id, item_id)`` for every risk whose evidence chunk is in an item's ``chunk_ids`` (many-to-many, deterministic)."""
    by_chunk: dict[str, list[str]] = {}
    for r in items:
        for cid in r["chunk_ids"]:
            by_chunk.setdefault(cid, []).append(r["item_id"])
    return sorted(({"risk_id": e["risk_id"], "item_id": item} for e in risk_evidence
                   for item in by_chunk.get(e["chunk_id"], [])), key=lambda r: (r["risk_id"], r["item_id"]))


# --------------------------------------------------------------------------
# Cypher
# --------------------------------------------------------------------------

_CLEAR = (
    "MATCH (p:RiskPassage {filer_cik: $cik}) DETACH DELETE p",
    "MATCH (:RiskItem {filer_cik: $cik})-[r:SUCCEEDED_BY|IN_SECTION|SPANS]->() DELETE r",
    "MATCH (:RiskFactor)-[r:OF_ITEM]->(:RiskItem {filer_cik: $cik}) DELETE r",
    "MATCH (i:RiskItem {filer_cik: $cik}) WHERE NOT i.item_id IN $keep DETACH DELETE i",
)

_ITEM_CYPHER = """UNWIND $rows AS row
    MERGE (i:RiskItem {item_id: row.item_id})
    SET i.accession_no = row.accession_no, i.filer_cik = row.filer_cik, i.filing_date = date(row.filing_date),
        i.section_id = row.section_id, i.seq = row.seq, i.headline = row.headline, i.text_hash = row.text_hash,
        i.char_start = row.char_start, i.char_end = row.char_end, i.unit_kind = row.unit_kind,
        i.chunk_ids = row.chunk_ids, i.is_current = row.is_current, i.lineage_id = row.lineage_id,
        i.removed_in = row.removed_in, i.is_new = row.is_new, i.snapshot_id = $snapshot_id"""

_IN_SECTION_CYPHER = """UNWIND $rows AS row
    MATCH (i:RiskItem {item_id: row.item_id}), (s:FilingSection {section_key: row.section_key})
    MERGE (i)-[r:IN_SECTION]->(s) SET r.snapshot_id = $snapshot_id"""

_SPANS_CYPHER = """UNWIND $rows AS row
    MATCH (i:RiskItem {item_id: row.item_id}), (e:EvidenceSpan {chunk_id: row.chunk_id})
    MERGE (i)-[r:SPANS]->(e) SET r.snapshot_id = $snapshot_id"""

_SUCCEEDED_CYPHER = """UNWIND $rows AS row
    MATCH (o:RiskItem {item_id: row.old}), (n:RiskItem {item_id: row.new})
    MERGE (o)-[s:SUCCEEDED_BY]->(n)
    SET s.kind = row.kind, s.decided_by = row.decided_by, s.sim_embed = row.sim_embed, s.sim_lex = row.sim_lex,
        s.snapshot_id = $snapshot_id"""

_PASSAGE_CYPHER = """UNWIND $rows AS row
    MATCH (i:RiskItem {item_id: row.item_id})
    MERGE (p:RiskPassage {passage_id: row.passage_id})
    SET p.kind = row.kind, p.item_id = row.item_id, p.older_accession = row.older_accession,
        p.newer_accession = row.newer_accession, p.filer_cik = row.filer_cik, p.text = row.text,
        p.counterpart_text = row.counterpart_text, p.similarity = row.similarity, p.char_start = row.char_start,
        p.char_end = row.char_end, p.chunk_ids = row.chunk_ids, p.counterpart_chunk_ids = row.counterpart_chunk_ids,
        p.chunk_fallback = row.chunk_fallback, p.decided_by = row.decided_by, p.snapshot_id = $snapshot_id
    MERGE (i)-[h:HAS_PASSAGE]->(p) SET h.snapshot_id = $snapshot_id"""

# MATCH only: the filing edge (kind 'rolled') comes from the version rules; this stamps what the item layer knows about it.
_SUPERSEDES_CYPHER = """UNWIND $rows AS row
    MATCH (n:Filing {accession_no: row.newer})-[s:SUPERSEDES]->(o:Filing {accession_no: row.older})
    SET s.items_compared = row.items_compared, s.not_compared_reason = row.reason
    RETURN row.newer AS newer, row.older AS older"""

_RISK_EVIDENCE_CYPHER = """MATCH (rf:RiskFactor)-[:HAS_EVIDENCE]->(e:EvidenceSpan)
    WHERE rf.filer_cik = $cik RETURN rf.risk_id AS risk_id, e.chunk_id AS chunk_id"""

_OF_ITEM_CYPHER = """UNWIND $rows AS row
    MATCH (rf:RiskFactor {risk_id: row.risk_id}), (i:RiskItem {item_id: row.item_id})
    MERGE (rf)-[r:OF_ITEM]->(i) SET r.snapshot_id = $snapshot_id"""


def _clear_layer(driver: Driver, cik: int, keep: Sequence[str]) -> None:
    with driver.session() as session:
        for statement in _CLEAR:
            session.run(statement, cik=cik, keep=list(keep)).consume()


def _stamp_supersedes(driver: Driver, rows: list[dict]) -> list[dict]:
    """SET the flags on the existing edges (batched); returns the pairs that have no SUPERSEDES edge."""
    matched: set[tuple[str, str]] = set()
    with driver.session() as session:
        for i in range(0, len(rows), 100):
            matched |= {(r["newer"], r["older"]) for r in session.run(_SUPERSEDES_CYPHER, rows=rows[i:i + 100])}
    return [r for r in rows if (r["newer"], r["older"]) not in matched]


def _read_ticker(settings: Settings, ticker: str,
                 directory: Path | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    directory = directory or alignment_dir(settings)
    items = pd.read_parquet(items_dir(settings) / f"{ticker}_risk_items.parquet")
    return (items, *(pd.read_parquet(table_path(directory, ticker, name)) for name in ("pairs", "decisions", "passages")))


def _current_by_item(settings: Settings, manifest: dict, ticker: str, items: pd.DataFrame) -> dict[str, bool]:
    ch = _read_chunks(settings, ticker)
    if ch is None:
        raise AlignItemsError(f"{ticker}: no chunks: run `semigraph ingest` first")
    _, states = _ticker_freshness(manifest, ticker, ch)
    unknown = sorted(set(items["accession_no"]) - set(states))
    if unknown:
        raise AlignItemsError(f"{ticker}: risk items of filing(s) {unknown} that are not in the manifest")
    return {r.item_id: span_freshness(states[r.accession_no], r.section_id).is_current for r in items.itertuples()}


def load_risk_items(driver: Driver, settings: Settings | None = None, tickers: Sequence[str] | None = None, *,
                    snapshot_id: str | None = None, alignment_directory: Path | None = None) -> dict[str, int]:
    """Load the item layer of ``tickers`` (default: every ticker with a risk-item file). Requires ``align-items`` output (read from
    ``alignment_directory``, default the lake's ``risk_alignment``), the Filing / FilingSection / EvidenceSpan nodes (and, for
    ``OF_ITEM``, the RiskFactors). Returns counts."""
    settings = settings or get_settings()
    snapshot_id = _resolve_snapshot_id(settings, snapshot_id)
    tickers = list(tickers) if tickers else sorted(p.name.split("_")[0] for p in items_dir(settings).glob("*_risk_items.parquet"))
    if not tickers:
        raise AlignItemsError(f"no risk items in {items_dir(settings)}: run `semigraph risk-items` first")
    require_alignment(settings, tickers, alignment_directory)
    manifest = _load_manifest(settings)
    totals = {"items": 0, "spans": 0, "spans_without_evidence_node": 0, "succeeded_by": 0, "passages": 0, "supersedes": 0, "of_item": 0}
    for ticker in tickers:
        items, pairs, decisions, passages = _read_ticker(settings, ticker, alignment_directory)
        layer = build_item_layer(ticker, items, pairs, decisions, passages, _current_by_item(settings, manifest, ticker, items))
        cik = int(items["filer_cik"].iloc[0])
        _clear_layer(driver, cik, [r["item_id"] for r in layer.items])
        _run_batched(driver, _ITEM_CYPHER, layer.items, snapshot_id=snapshot_id)
        _run_batched(driver, _IN_SECTION_CYPHER, layer.in_section, snapshot_id=snapshot_id)
        _run_batched(driver, _SPANS_CYPHER, layer.spans, snapshot_id=snapshot_id)
        _run_batched(driver, _SUCCEEDED_CYPHER, layer.succeeded_by, snapshot_id=snapshot_id)
        _run_batched(driver, _PASSAGE_CYPHER, layer.passages, snapshot_id=snapshot_id)
        missing = _stamp_supersedes(driver, layer.supersedes)
        if missing:
            raise AlignItemsError(f"{ticker}: no SUPERSEDES edge between the filings of {len(missing)} pair(s), e.g. "
                                  f"{missing[0]['older']} -> {missing[0]['newer']}: the manifest and the risk-item files disagree")
        linked = run_cypher(driver, "MATCH (:RiskItem {filer_cik: $cik})-[r:SPANS]->() RETURN count(r) AS n", cik=cik)[0]["n"]
        evidence = run_cypher(driver, _RISK_EVIDENCE_CYPHER, cik=cik)
        of_item = of_item_rows(evidence, layer.items)
        _run_batched(driver, _OF_ITEM_CYPHER, of_item, snapshot_id=snapshot_id)
        for key, value in (("items", len(layer.items)), ("spans", linked), ("spans_without_evidence_node", len(layer.spans) - linked),
                           ("succeeded_by", len(layer.succeeded_by)), ("passages", len(layer.passages)),
                           ("supersedes", len(layer.supersedes)), ("of_item", len(of_item))):
            totals[key] += value
        logger.info("%s: %d items, %d SUCCEEDED_BY, %d passages, %d SUPERSEDES stamped, %d OF_ITEM", ticker, len(layer.items),
                    len(layer.succeeded_by), len(layer.passages), len(layer.supersedes), len(of_item))
    if totals["spans_without_evidence_node"]:
        logger.warning("%d item chunk ids have no EvidenceSpan node (older filings outside the span scope): the chunk_ids "
                       "property keeps them, the SPANS edge does not exist", totals["spans_without_evidence_node"])
    return totals
