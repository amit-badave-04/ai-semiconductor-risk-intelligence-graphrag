"""Read-only verification of a built semigraph graph: counts, freshness invariants, the risk-item layer, index health.

    set -a; . ./.env.community; set +a
    PYTHONPATH=src python scripts/verify_graph.py

Exits non-zero when any invariant fails. Uses NEO4J_URI / NEO4J_PASSWORD / NEO4J_DATABASE.
Most checks are one Cypher query returning the offending rows (expected: none) or a value. The risk-item invariants
(M1B_PLAN L.7) add checks that need the data lake (skipped, and said so, when it is absent): the RiskItem / RiskPassage
counts equal the parquet row counts; PER FILER the graph's ``removed_in``, ``is_new`` and ``unsettled_in`` items and its SUCCEEDED_BY
edges (per kind) equal what the decisions parquet of COMPARED pairs says the loader must have written (a loader that drops any of them
fails); and the FALSE-DROP GUARD: no removed item's headline (or, for a paragraph unit, its first sentence) fuzzy-matches (rapidfuzz
ratio >= 90 after default_process) any sentence of the newer filing's section text. ``unsettled_in`` (an older item the text check
could not settle: not verified removed, not verified present) may sit only on items of compared pairs, never together with
``removed_in``.
"""

import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from semigraph.config import Settings, get_settings
from semigraph.graph import client
from semigraph.graph.align_text import split_sentences
from semigraph.graph.temporal import ANNUAL_FORMS

FALSE_DROP_RATIO = 90.0      # a removed item whose headline is this close to a sentence of the newer section is a false drop
MIN_PROBE_CHARS = 20         # a shorter first sentence of a paragraph unit proves nothing
NO_FILER = -1                # the count key of graph nodes that have no filer_cik (they belong to no parquet row)
UNSETTLED_LABEL = "uncertain"    # the older-side decision the loader turns into RiskItem.unsettled_in
REMOVED_LABEL, NEW_LABEL = "removed", "new"        # the older-side decision behind removed_in, the newer-side one behind is_new
EDGE_KINDS = ("unchanged", "reworded", "merged")   # the older-side decisions the loader turns into SUCCEEDED_BY edges (kind = the label)
UNSETTLED_GRAPH_QUERY = "MATCH (i:RiskItem) WHERE i.unsettled_in IS NOT NULL RETURN i.filer_cik AS cik, count(i) AS n"
REMOVED_GRAPH_QUERY = "MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL RETURN i.filer_cik AS cik, count(i) AS n"
NEW_GRAPH_QUERY = "MATCH (i:RiskItem) WHERE i.is_new = true RETURN i.filer_cik AS cik, count(i) AS n"
SUCCEEDED_GRAPH_QUERY = "MATCH (o:RiskItem)-[s:SUCCEEDED_BY]->(:RiskItem) RETURN o.filer_cik AS cik, s.kind AS kind, count(s) AS n"

_ANNUAL = "[" + ", ".join(f"'{f}'" for f in ANNUAL_FORMS) + "]"

# (name, cypher, kind): "none" = must return no rows; "info" = printed only.
STRUCTURE_CHECKS = [
    ("no current span under a non-current filing (except an overlay amendment's owner)", """
        MATCH (e:EvidenceSpan {is_current: true})-[:FROM_SECTION]->(:FilingSection)<-[:HAS_SECTION]-(f:Filing)
        WHERE f.is_current = false RETURN f.accession_no AS accession, count(e) AS spans""", "none"),
    ("every span carries the freshness contract", """
        MATCH (e:EvidenceSpan)
        WHERE e.is_current IS NULL OR e.retrievable IS NULL OR e.filer_cik IS NULL OR e.valid_from IS NULL
           OR e.valid_to IS NULL OR e.status IS NULL OR e.content_hash IS NULL OR e.snapshot_id IS NULL
        RETURN e.chunk_id AS chunk LIMIT 10""", "none"),
    ("filer_cik is an integer everywhere (filtered search compares by type)", """
        MATCH (n) WHERE (n:EvidenceSpan OR n:RiskFactor OR n:RiskItem OR n:RiskPassage) AND NOT n.filer_cik IS :: INTEGER
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
    ("no Deleted status remains: every risk disclosure is Active or Historical and has no end_date", """
        MATCH (:Company)-[d:DISCLOSES_RISK]->(:RiskFactor)
        WHERE d.status IS NULL OR NOT d.status IN ['Active', 'Historical'] OR d.end_date IS NOT NULL
        RETURN d.status AS status, count(*) AS n""", "none"),
    ("an Active risk is in the current annual filing", """
        MATCH (:Company)-[:DISCLOSES_RISK {status: 'Active'}]->(rf:RiskFactor)
        WHERE NOT rf.is_current = true OR NOT rf.form IN %s
        RETURN rf.risk_id AS risk, rf.form AS form LIMIT 10""" % _ANNUAL, "none"),
]

# The text-grounded risk-item layer (contract: docs/v2/M1B_PLAN.md L.7). A pair is "not compared" when its SUPERSEDES edge says
# items_compared = false: nothing item-level may exist for it.
ITEM_LAYER_CHECKS = [
    ("every RiskItem has chunk_ids (none is chunk-less)", """
        MATCH (i:RiskItem) WHERE i.chunk_ids IS NULL OR size(i.chunk_ids) = 0
        RETURN i.item_id AS item LIMIT 10""", "none"),
    ("every RiskItem and RiskPassage carries the snapshot id; every RiskItem is in its section", """
        MATCH (n) WHERE (n:RiskItem OR n:RiskPassage) AND n.snapshot_id IS NULL
        RETURN labels(n)[0] AS problem, count(*) AS n
        UNION ALL
        MATCH (i:RiskItem) WHERE NOT (i)-[:IN_SECTION]->(:FilingSection)
        RETURN 'RiskItem without IN_SECTION' AS problem, count(i) AS n""", "none"),
    ("removed_in only on items of pairs with items_compared = true", """
        MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL
        OPTIONAL MATCH (:Filing {accession_no: i.removed_in})-[s:SUPERSEDES]->(:Filing {accession_no: i.accession_no})
        WITH i, s WHERE s IS NULL OR NOT coalesce(s.items_compared, false)
        RETURN i.item_id AS item, i.removed_in AS removed_in LIMIT 10""", "none"),
    ("unsettled_in only on items of pairs with items_compared = true", """
        MATCH (i:RiskItem) WHERE i.unsettled_in IS NOT NULL
        OPTIONAL MATCH (:Filing {accession_no: i.unsettled_in})-[s:SUPERSEDES]->(:Filing {accession_no: i.accession_no})
        WITH i, s WHERE s IS NULL OR NOT coalesce(s.items_compared, false)
        RETURN i.item_id AS item, i.unsettled_in AS unsettled_in LIMIT 10""", "none"),
    ("no item carries both removed_in and unsettled_in", """
        MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL AND i.unsettled_in IS NOT NULL
        RETURN i.item_id AS item, i.removed_in AS removed_in, i.unsettled_in AS unsettled_in LIMIT 10""", "none"),
    ("is_new only on items of pairs with items_compared = true", """
        MATCH (i:RiskItem {is_new: true})
        OPTIONAL MATCH (:Filing {accession_no: i.accession_no})-[s:SUPERSEDES {items_compared: true}]->(:Filing)
        WITH i, count(s) AS compared WHERE compared = 0
        RETURN i.item_id AS item LIMIT 10""", "none"),
    ("no SUCCEEDED_BY, removed_in, unsettled_in, is_new or RiskPassage on a pair that was not compared", """
        MATCH (n:Filing)-[:SUPERSEDES {items_compared: false}]->(o:Filing)
        MATCH (a:RiskItem {accession_no: o.accession_no})-[:SUCCEEDED_BY]->(:RiskItem {accession_no: n.accession_no})
        RETURN 'SUCCEEDED_BY' AS what, a.item_id AS item LIMIT 5
        UNION ALL
        MATCH (n:Filing)-[:SUPERSEDES {items_compared: false}]->(o:Filing)
        MATCH (a:RiskItem {accession_no: o.accession_no, removed_in: n.accession_no})
        RETURN 'removed_in' AS what, a.item_id AS item LIMIT 5
        UNION ALL
        MATCH (n:Filing)-[:SUPERSEDES {items_compared: false}]->(o:Filing)
        MATCH (a:RiskItem {accession_no: o.accession_no, unsettled_in: n.accession_no})
        RETURN 'unsettled_in' AS what, a.item_id AS item LIMIT 5
        UNION ALL
        MATCH (n:Filing)-[:SUPERSEDES {items_compared: false}]->(:Filing)
        MATCH (b:RiskItem {accession_no: n.accession_no, is_new: true})
        RETURN 'is_new' AS what, b.item_id AS item LIMIT 5
        UNION ALL
        MATCH (n:Filing)-[:SUPERSEDES {items_compared: false}]->(o:Filing)
        MATCH (p:RiskPassage {newer_accession: n.accession_no, older_accession: o.accession_no})
        RETURN 'RiskPassage' AS what, p.passage_id AS item LIMIT 5""", "none"),
    ("every RiskPassage has exactly one HAS_PASSAGE parent", """
        MATCH (p:RiskPassage) OPTIONAL MATCH (i:RiskItem)-[:HAS_PASSAGE]->(p)
        WITH p, count(i) AS parents WHERE parents <> 1
        RETURN p.passage_id AS passage, parents LIMIT 10""", "none"),
    ("every annual->annual SUPERSEDES between filings that have risk items carries a boolean items_compared", """
        MATCH (n:Filing)-[s:SUPERSEDES {kind: 'rolled'}]->(o:Filing)
        WHERE n.form IN %(annual)s AND o.form IN %(annual)s
          AND EXISTS { MATCH (:RiskItem {accession_no: n.accession_no}) }
          AND EXISTS { MATCH (:RiskItem {accession_no: o.accession_no}) }
          AND (s.items_compared IS NULL OR NOT s.items_compared IN [true, false])
        RETURN n.accession_no AS newer, o.accession_no AS older LIMIT 10""" % {"annual": _ANNUAL}, "none"),
    ("the citable chunk ids of the CURRENT pair (removed / unsettled / new items, passages) resolve to evidence spans", """
        MATCH (:Company)-[:FILED]->(cur:Filing {is_current: true})-[:SUPERSEDES {kind: 'rolled', items_compared: true}]->(prev:Filing)
        MATCH (i:RiskItem)
        WHERE (i.accession_no = prev.accession_no AND i.removed_in = cur.accession_no)
           OR (i.accession_no = prev.accession_no AND i.unsettled_in = cur.accession_no)
           OR (i.accession_no = cur.accession_no AND i.is_new = true)
        UNWIND i.chunk_ids AS cid OPTIONAL MATCH (e:EvidenceSpan {chunk_id: cid})
        WITH i, cid, e WHERE e IS NULL
        RETURN 'item' AS what, i.item_id AS item, cid LIMIT 5
        UNION ALL
        MATCH (:Company)-[:FILED]->(cur:Filing {is_current: true})-[:SUPERSEDES {kind: 'rolled', items_compared: true}]->(prev:Filing)
        MATCH (p:RiskPassage {newer_accession: cur.accession_no, older_accession: prev.accession_no})
        UNWIND (p.chunk_ids + coalesce(p.counterpart_chunk_ids, [])) AS cid OPTIONAL MATCH (e:EvidenceSpan {chunk_id: cid})
        WITH p, cid, e WHERE e IS NULL
        RETURN 'passage' AS what, p.passage_id AS item, cid LIMIT 5""", "none"),
]

INFO_CHECKS = [
    ("risk items and changes by filing", """
        MATCH (i:RiskItem)
        RETURN i.accession_no AS accession, count(*) AS items, count(i.removed_in) AS removed,
               count(i.unsettled_in) AS unsettled,
               count(CASE WHEN i.is_new THEN 1 END) AS new, count(CASE WHEN i.is_current THEN 1 END) AS current
        ORDER BY accession""", "info"),
    ("filing pairs and whether their items were compared", """
        MATCH (n:Filing)-[s:SUPERSEDES {kind: 'rolled'}]->(o:Filing) WHERE s.items_compared IS NOT NULL
        RETURN n.accession_no AS newer, o.accession_no AS older, s.items_compared AS compared,
               s.not_compared_reason AS reason ORDER BY newer""", "info"),
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

CHECKS = STRUCTURE_CHECKS + ITEM_LAYER_CHECKS + INFO_CHECKS


# --------------------------------------------------------------------------- invariants that need the data lake

def lake_row_counts(settings: Settings, align_dir: Path | None = None) -> tuple[dict[int, int], dict[int, int]] | None:
    """``(items per filer_cik, passages per filer_cik)`` from the risk-item and risk-alignment parquets; None without a lake."""
    items_dir = settings.interim_dir / "risk_items"
    align_dir = align_dir or settings.interim_dir / "risk_alignment"
    item_files = sorted(items_dir.glob("*_risk_items.parquet"))
    if not item_files:
        return None
    items = pd.concat([pd.read_parquet(f, columns=["filer_cik"]) for f in item_files], ignore_index=True)
    passage_files = sorted(align_dir.glob("*_passages.parquet"))
    passages = (pd.concat([pd.read_parquet(f, columns=["filer_cik"]) for f in passage_files], ignore_index=True)
                if passage_files else pd.DataFrame({"filer_cik": []}))
    return ({int(k): int(v) for k, v in items["filer_cik"].value_counts().items()},
            {int(k): int(v) for k, v in passages["filer_cik"].value_counts().items()})


def count_mismatches(expected: Mapping[int, int], actual: Mapping[int, int], what: str) -> list[dict]:
    """One row per filer whose graph count differs from the parquet row count (a filer missing from the graph counts as 0)."""
    return [{"what": what, "filer_cik": cik, "parquet_rows": expected.get(cik, 0), "graph_nodes": actual.get(cik, 0)}
            for cik in sorted(set(expected) | set(actual)) if expected.get(cik, 0) != actual.get(cik, 0)]


def check_counts(driver, settings: Settings, align_dir: Path | None = None) -> list[dict] | None:
    """RiskItem / RiskPassage counts equal the parquet row counts, per filer. None when the lake is absent."""
    lake = lake_row_counts(settings, align_dir)
    if lake is None:
        return None
    graph = {label: {NO_FILER if r["cik"] is None else int(r["cik"]): int(r["n"]) for r in client.run_cypher(
        driver, f"MATCH (x:{label}) RETURN x.filer_cik AS cik, count(x) AS n")} for label in ("RiskItem", "RiskPassage")}
    return count_mismatches(lake[0], graph["RiskItem"], "RiskItem") + count_mismatches(lake[1], graph["RiskPassage"], "RiskPassage")


def lake_decision_counts(settings: Settings, align_dir: Path | None = None) -> dict | None:
    """What the loader must have written, per ``filer_cik``, read from the decisions of COMPARED pairs in the risk-alignment parquets
    (one pass): ``removed_in`` (older items labelled removed), ``is_new`` (newer items labelled new), ``unsettled_in`` (older items
    labelled uncertain) and ``succeeded_by`` = ``{kind: {cik: n}}`` (older-side decisions of an edge kind that name a counterpart:
    distinct (old, new) pairs). None without a lake. An alignment file that is absent expects none; a decision whose item is not in
    the item parquet counts under ``NO_FILER`` (a stale alignment, reported as a mismatch)."""
    items_dir = settings.interim_dir / "risk_items"
    align_dir = align_dir or settings.interim_dir / "risk_alignment"
    item_files = sorted(items_dir.glob("*_risk_items.parquet"))
    if not item_files:
        return None
    filer_of = pd.concat([pd.read_parquet(f, columns=["item_id", "filer_cik"]) for f in item_files]).set_index("item_id")["filer_cik"]

    def cik_of(item_id: str) -> int:
        return NO_FILER if item_id not in filer_of.index else int(filer_of[item_id])

    flags: dict[str, set] = {"removed_in": set(), "is_new": set(), "unsettled_in": set()}
    edges: dict[str, set] = {kind: set() for kind in EDGE_KINDS}
    for decisions_file in sorted(align_dir.glob("*_decisions.parquet")):
        pairs_file = decisions_file.with_name(decisions_file.name.replace("_decisions.parquet", "_pairs.parquet"))
        pairs = pd.read_parquet(pairs_file, columns=["pair_id", "comparable"])
        compared = set(pairs.loc[pairs["comparable"].astype(bool), "pair_id"])
        decisions = pd.read_parquet(decisions_file)
        decisions = decisions[decisions["pair_id"].isin(compared)]
        older, newer = decisions[decisions["side"] == "older"], decisions[decisions["side"] == "newer"]
        flags["removed_in"] |= set(older.loc[older["label"] == REMOVED_LABEL, "item_id"])
        flags["unsettled_in"] |= set(older.loc[older["label"] == UNSETTLED_LABEL, "item_id"])
        flags["is_new"] |= set(newer.loc[newer["label"] == NEW_LABEL, "item_id"])
        for r in older[older["label"].isin(EDGE_KINDS)].itertuples():
            if isinstance(r.matched_item_id, str) and r.matched_item_id:
                edges[r.label].add((r.item_id, r.matched_item_id))
    counts: dict = {name: _per_filer(ids, cik_of) for name, ids in flags.items()}
    counts["succeeded_by"] = {kind: _per_filer((old for old, _ in found), cik_of) for kind, found in edges.items()}
    return counts


def _per_filer(item_ids, cik_of: Callable[[str], int]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for item_id in item_ids:
        cik = cik_of(item_id)
        counts[cik] = counts.get(cik, 0) + 1
    return counts


def lake_unsettled_counts(settings: Settings, align_dir: Path | None = None) -> dict[int, int] | None:
    """``filer_cik -> older-side decisions labelled uncertain in COMPARED pairs`` (how many items must carry ``unsettled_in``)."""
    counts = lake_decision_counts(settings, align_dir)
    return None if counts is None else counts["unsettled_in"]


def _graph_counts(driver, query: str) -> dict[int, int]:
    return {NO_FILER if r["cik"] is None else int(r["cik"]): int(r["n"]) for r in client.run_cypher(driver, query)}


def _check_flag_counts(driver, settings: Settings, align_dir: Path | None, name: str, query: str) -> list[dict] | None:
    counts = lake_decision_counts(settings, align_dir)
    return None if counts is None else count_mismatches(counts[name], _graph_counts(driver, query), name)


def check_unsettled_counts(driver, settings: Settings, align_dir: Path | None = None) -> list[dict] | None:
    """The graph's ``unsettled_in`` items equal the decisions parquet's older-side ``uncertain`` decisions of compared pairs, per
    filer. None when the lake is absent."""
    return _check_flag_counts(driver, settings, align_dir, "unsettled_in", UNSETTLED_GRAPH_QUERY)


def check_removed_counts(driver, settings: Settings, align_dir: Path | None = None) -> list[dict] | None:
    """The graph's ``removed_in`` items equal the older-side ``removed`` decisions of compared pairs, per filer (None: no lake)."""
    return _check_flag_counts(driver, settings, align_dir, "removed_in", REMOVED_GRAPH_QUERY)


def check_new_counts(driver, settings: Settings, align_dir: Path | None = None) -> list[dict] | None:
    """The graph's ``is_new`` items equal the newer-side ``new`` decisions of compared pairs, per filer (None: no lake)."""
    return _check_flag_counts(driver, settings, align_dir, "is_new", NEW_GRAPH_QUERY)


def check_succeeded_counts(driver, settings: Settings, align_dir: Path | None = None) -> list[dict] | None:
    """The graph's SUCCEEDED_BY edges, per filer and KIND, equal the older-side matched decisions of compared pairs (None: no lake).
    An edge of an unexpected kind is a mismatch of its own (expected 0)."""
    counts = lake_decision_counts(settings, align_dir)
    if counts is None:
        return None
    graph: dict[str, dict[int, int]] = {}
    for r in client.run_cypher(driver, SUCCEEDED_GRAPH_QUERY):
        graph.setdefault(str(r["kind"]), {})[NO_FILER if r["cik"] is None else int(r["cik"])] = int(r["n"])
    out: list[dict] = []
    for kind in sorted(set(counts["succeeded_by"]) | set(graph)):
        out += count_mismatches(counts["succeeded_by"].get(kind, {}), graph.get(kind, {}), f"SUCCEEDED_BY[{kind}]")
    return out


def probe_of(headline: str | None, text: str | None) -> str:
    """What identifies a removed item in the newer text: its headline, or (a paragraph unit has none) its first sentence."""
    if isinstance(headline, str) and headline.strip():
        return headline.strip()
    if isinstance(text, str):
        spans = split_sentences(text)
        if spans:
            first = text[spans[0][0]:spans[0][1]]
            return first if len(first) >= MIN_PROBE_CHARS else ""
    return ""


def false_drops(removed: Sequence[Mapping], section_text_of: Callable[[Mapping], str | None],
                ratio: float = FALSE_DROP_RATIO) -> list[dict]:
    """The removed items whose probe fuzzy-matches (``fuzz.ratio`` >= ``ratio`` after ``default_process``) a sentence of the newer
    section: each row names the item, the matching sentence and the score. ``removed``: rows with ``item_id``, ``headline`` and
    ``text`` (plus whatever ``section_text_of`` needs); ``section_text_of(row)`` returns the newer filing's section text, or None
    (an item whose newer text is unavailable is reported, never passed silently)."""
    out: list[dict] = []
    for row in removed:
        text = section_text_of(row)
        if text is None:
            out.append({"item": row["item_id"], "problem": "newer section text not available"})
            continue
        probe = probe_of(row.get("headline"), row.get("text"))
        if not probe:
            continue
        needle = default_process(probe)
        best = max(((fuzz.ratio(needle, default_process(text[a:b])), text[a:b]) for a, b in split_sentences(text)),
                   default=(0.0, ""), key=lambda scored: scored[0])
        if best[0] >= ratio:
            out.append({"item": row["item_id"], "headline": probe[:120], "ratio": round(best[0], 1), "newer_sentence": best[1][:160]})
    return out


def _lake_items(settings: Settings) -> pd.DataFrame:
    files = sorted((settings.interim_dir / "risk_items").glob("*_risk_items.parquet"))
    frames = [pd.read_parquet(f, columns=["item_id", "filer_cik", "headline", "text"]) for f in files]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["item_id", "filer_cik", "headline", "text"])


def check_false_drops(driver, settings: Settings) -> list[dict] | None:
    """The false-drop guard over every removed item of the graph. None when the lake is absent."""
    if lake_row_counts(settings) is None:
        return None
    removed = client.run_cypher(driver, """
        MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL
        MATCH (c:Company {cik: i.filer_cik})
        RETURN i.item_id AS item_id, i.removed_in AS removed_in, i.section_id AS section_id, c.ticker AS ticker""")
    if not removed:
        return []
    lake = _lake_items(settings)
    by_id = lake.set_index("item_id")[["headline", "text"]].to_dict("index")
    cache: dict[str, pd.DataFrame] = {}

    def section_text_of(row: Mapping) -> str | None:
        ticker, accession, section_id = row["ticker"], row["removed_in"], row["section_id"]
        if ticker not in cache:
            name = "nvda_section_texts.parquet" if ticker == "NVDA" else f"{ticker}_section_texts.parquet"
            path = settings.interim_dir / "section_texts" / name
            cache[ticker] = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=["accession_no", "section_id", "text"])
        frame = cache[ticker]
        rows = frame[(frame["accession_no"] == accession) & (frame["section_id"] == section_id)]
        return None if rows.empty else str(rows.iloc[0]["text"])

    return false_drops([{**r, **by_id.get(r["item_id"], {})} for r in removed], section_text_of)


PYTHON_CHECKS = [
    ("RiskItem / RiskPassage counts equal the parquet row counts", check_counts),
    ("unsettled_in counts equal the older-side uncertain decisions of compared pairs in the decisions parquet", check_unsettled_counts),
    ("removed_in counts equal the older-side removed decisions of compared pairs in the decisions parquet", check_removed_counts),
    ("is_new counts equal the newer-side new decisions of compared pairs in the decisions parquet", check_new_counts),
    ("SUCCEEDED_BY counts per kind equal the older-side matched decisions of compared pairs in the decisions parquet",
     check_succeeded_counts),
    ("false-drop guard: no removed item's headline matches a sentence of the newer section text", check_false_drops),
]


def run_checks(driver, settings: Settings, out=print) -> int:
    """Run every check, print the report through ``out`` and return the number of failed invariants."""
    failures = 0
    for name, query, kind in CHECKS:
        rows = client.run_cypher(driver, query)
        if kind == "none":
            failures += bool(rows)
            out(f"[{'PASS' if not rows else 'FAIL'}] {name}")
            for row in rows[:10]:
                out(f"        {row}")
        else:
            out(f"[info] {name}")
            for row in rows[:30]:
                out(f"        {row}")
    for name, check in PYTHON_CHECKS:
        rows = check(driver, settings)
        if rows is None:
            out(f"[skip] {name}: the data lake (data/interim/risk_items) is not present")
            continue
        failures += bool(rows)
        out(f"[{'PASS' if not rows else 'FAIL'}] {name}")
        for row in rows[:10]:
            out(f"        {row}")
    return failures


def main() -> int:
    settings = get_settings()
    driver = client.get_driver(settings)
    try:
        failures = run_checks(driver, settings)
    finally:
        driver.close()
    print(f"\n{failures} invariant failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
