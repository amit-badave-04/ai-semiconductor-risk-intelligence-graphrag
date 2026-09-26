"""The risk-item layer against a real Neo4j - opt-in (RUN_NEO4J_TESTS=1; on Community also SEMIGRAPH_ALLOW_WIPE=1).

Two scenarios, each in a throwaway database that is wiped before and after (both also cover ``unsettled_in``: an older item the
text check could not settle, i.e. an ``uncertain`` older-side decision of a compared pair):

* ``TestSyntheticLake``: a tiny lake (ticker NVDA, three annual filings; see ``tests/lakefix.py``) is loaded through the REAL loaders
  (companies, filings, evidence spans), the risk-item alignment is run and loaded with ``item_loader.load_risk_items``, and the
  graph is checked against contract L.7 (M1B_PLAN): items, SUCCEEDED_BY, passages, the SUPERSEDES flags, OF_ITEM, idempotence and
  the replacement of stale derived data, then read back with the retriever's own queries.
* ``TestRealNvidiaAndIntel``: the same on the real lake (skipped when ``data/interim/risk_items`` is absent): the flagship facts
  of NVIDIA FY25 -> FY26 and an INTC pair rendered as not compared.
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lakefix  # noqa: E402
from semigraph.config import get_settings  # noqa: E402
from semigraph.graph import item_loader, items, loaders, schema, temporal  # noqa: E402
from semigraph.retrieval.answerer import build_blocks  # noqa: E402
from semigraph.retrieval.retriever import (  # noqa: E402
    PASSAGES_QUERY,
    TEMPORAL_QUERY,
    run_cypher,
    select_passages,
    select_temporal,
)

VERIFY = Path(__file__).resolve().parents[2] / "scripts" / "verify_graph.py"
_spec = importlib.util.spec_from_file_location("verify_graph_it", VERIFY)
vg = importlib.util.module_from_spec(_spec)
sys.modules["verify_graph_it"] = vg
_spec.loader.exec_module(vg)

EMBED_DIM = 1024
NVDA_CIK, INTC_CIK = 1045810, 50863
ACC = {y: f"NVDA{lakefix.ACC[y]}" for y in ("24", "25", "26")}


class FakeEmbedder:
    """Stand-in for Embedder: a constant unit vector per chunk (the item layer never reads embeddings)."""

    def encode_chunks_cached(self, chunks_df, cache_path, batch_size=8):
        import numpy as np

        return np.tile(np.eye(1, EMBED_DIM)[0], (len(chunks_df), 1))

    def encode_passages(self, texts, batch_size=8, show_progress=False):
        import numpy as np

        return np.tile(np.eye(1, EMBED_DIM)[0], (len(texts), 1))


def rows(driver, query, **params):
    with driver.session() as session:
        return [dict(r) for r in session.run(query, **params)]


def one(driver, query, **params):
    return rows(driver, query, **params)[0]


def graph_size(driver):
    return (one(driver, "MATCH (n) RETURN count(n) AS n")["n"], one(driver, "MATCH ()-[r]->() RETURN count(r) AS n")["n"])


# --------------------------------------------------------------------------- the synthetic lake

def build_synthetic_lake(root: Path, base):
    lake = lakefix.build_lake(root, ticker="NVDA", cik=NVDA_CIK)
    lake = lake.model_copy(update={"neo4j_uri": base.neo4j_uri, "neo4j_user": base.neo4j_user,
                                   "neo4j_password": base.neo4j_password, "neo4j_database": base.neo4j_database})
    for sub, upper, lower in (("interim/section_texts", "NVDA_section_texts.parquet", "nvda_section_texts.parquet"),
                              ("processed/chunks", "NVDA_chunks.parquet", "nvda_chunks.parquet")):
        directory = root / "data" / sub
        (directory / upper).rename(directory / lower)
    (lake.raw_dir / "edgar").mkdir(parents=True, exist_ok=True)
    (lake.raw_dir / "edgar" / "company_tickers.json").write_text(
        json.dumps({"0": {"cik_str": NVDA_CIK, "ticker": "NVDA", "title": "NVIDIA"}}), encoding="utf-8")
    (lake.raw_dir / "edgar" / "manifest_universe.json").write_text(json.dumps({"NVDA": [
        {"ticker": "NVDA", "cik": NVDA_CIK, "accession_no": ACC[y], "form": "10-K", "filing_date": lakefix.DATES[y],
         "source_url": f"https://sec.example/{ACC[y]}"} for y in ("24", "25", "26")]}), encoding="utf-8")
    return lake


@pytest.fixture(scope="class")
def synthetic(scratch_database, tmp_path_factory, neo4j_base_settings):
    with scratch_database("sgitems") as (driver, _):
        lake = build_synthetic_lake(tmp_path_factory.mktemp("itemslake"), neo4j_base_settings)
        schema.apply_schema(driver)
        driver.execute_query("CALL db.awaitIndexes(120)")
        snapshot = "sgitems-synthetic"
        loaders.load_companies(driver, lake, snapshot_id=snapshot)
        loaders.load_filings_and_sections(driver, lake, ["NVDA"], snapshot_id=snapshot)
        loaders.load_evidence_spans(driver, lake, FakeEmbedder(), ["NVDA"], snapshot_id=snapshot)
        with driver.session() as session:                       # one RiskFactor per filing, evidenced by that filing's first chunk
            for y in ("24", "25", "26"):
                session.run(
                    "MATCH (c:Company {cik: $cik}), (e:EvidenceSpan {chunk_id: $chunk}) "
                    "CREATE (c)-[:DISCLOSES_RISK {status: 'Active', start_date: date($day)}]->(rf:RiskFactor {risk_id: $rid, "
                    "summary: $rid, category: 'Supply Chain', filer_cik: $cik, form: '10-K', is_current: $cur}) "
                    "CREATE (rf)-[:HAS_EVIDENCE {quote: 'q'}]->(e)", cik=NVDA_CIK, chunk=f"{ACC[y]}:I.1A:0000",
                    day=lakefix.DATES[y], rid=f"rf{y}", cur=(y == "26")).consume()
        items.run_align_items(lake, ["NVDA"])
        totals = item_loader.load_risk_items(driver, lake, ["NVDA"], snapshot_id=snapshot)
        yield {"driver": driver, "settings": lake, "snapshot": snapshot, "totals": totals}


def iid(y, n):
    return f"{ACC[y]}:I.1A:i{n:03d}"


class TestSyntheticLake:
    def test_the_counts_of_the_layer_match_the_parquets(self, synthetic):
        d, s = synthetic["driver"], synthetic["settings"]
        directory = items.alignment_dir(s)
        decisions = pd.read_parquet(items.table_path(directory, "NVDA", "decisions"))
        passages = pd.read_parquet(items.table_path(directory, "NVDA", "passages"))
        assert one(d, "MATCH (i:RiskItem) RETURN count(i) AS n")["n"] == len(pd.read_parquet(s.interim_dir / "risk_items" / "NVDA_risk_items.parquet")) == 10
        assert one(d, "MATCH (p:RiskPassage) RETURN count(p) AS n")["n"] == len(passages) == 2
        older = decisions[(decisions["side"] == "older") & decisions["label"].isin(["unchanged", "reworded", "merged"])]
        assert one(d, "MATCH ()-[r:SUCCEEDED_BY]->() RETURN count(r) AS n")["n"] == len(older) == 5

    def test_a_risk_item_carries_exactly_the_contract_properties(self, synthetic):
        node = one(synthetic["driver"], "MATCH (i:RiskItem {item_id: $id}) RETURN properties(i) AS p", id=iid("24", 2))["p"]
        assert set(node) == {"item_id", "accession_no", "filer_cik", "filing_date", "section_id", "seq", "headline", "text_hash",
                             "char_start", "char_end", "unit_kind", "chunk_ids", "is_current", "lineage_id", "removed_in",
                             "is_new", "snapshot_id"}
        assert node["removed_in"] == ACC["25"] and node["is_new"] is False and node["is_current"] is False
        assert "unsettled_in" not in node                       # null is no property: only an uncertain older item carries it
        assert node["filer_cik"] == NVDA_CIK and isinstance(node["filer_cik"], int) and node["snapshot_id"] == synthetic["snapshot"]
        assert node["filing_date"].to_native() == date(2024, 2, 21) and node["chunk_ids"] == [f"{ACC['24']}:I.1A:0002"]

    def test_removed_in_and_is_new_are_set_only_where_the_contract_says(self, synthetic):
        d = synthetic["driver"]
        assert [r["id"] for r in rows(d, "MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL RETURN i.item_id AS id")] == [iid("24", 2)]
        assert sorted(r["id"] for r in rows(d, "MATCH (i:RiskItem {is_new: true}) RETURN i.item_id AS id")) == [iid("25", 2), iid("26", 3)]
        assert {r["cur"] for r in rows(d, "MATCH (i:RiskItem {accession_no: $a}) RETURN i.is_current AS cur", a=ACC["26"])} == {True}
        assert {r["cur"] for r in rows(d, "MATCH (i:RiskItem) WHERE i.accession_no <> $a RETURN i.is_current AS cur", a=ACC["26"])} == {False}

    def test_lineages_are_carried_along_the_verified_successors(self, synthetic):
        lineage = {r["id"]: r["lin"] for r in rows(synthetic["driver"], "MATCH (i:RiskItem) RETURN i.item_id AS id, i.lineage_id AS lin")}
        assert lineage[iid("24", 0)] == lineage[iid("25", 0)] == lineage[iid("26", 0)] == f"lin:NVDA:{iid('24', 0)}"
        assert len(set(lineage.values())) == 5

    def test_succeeded_by_carries_kind_decider_and_scores(self, synthetic):
        edge = one(synthetic["driver"], "MATCH (o:RiskItem {item_id: $o})-[s:SUCCEEDED_BY]->(n:RiskItem {item_id: $n}) RETURN properties(s) AS p",
                   o=iid("24", 0), n=iid("25", 0))["p"]
        assert edge["kind"] == "reworded" and edge["decided_by"] == "headline" and edge["snapshot_id"] == synthetic["snapshot"]
        assert 0.5 < edge["sim_lex"] < 1.0 and "sim_embed" not in edge

    def test_every_passage_has_exactly_one_parent_the_contract_properties_and_chunk_ids(self, synthetic):
        d = synthetic["driver"]
        assert rows(d, "MATCH (p:RiskPassage) OPTIONAL MATCH (i:RiskItem)-[:HAS_PASSAGE]->(p) WITH p, count(i) AS parents WHERE parents <> 1 RETURN p.passage_id AS id") == []
        removed = one(d, "MATCH (i:RiskItem)-[:HAS_PASSAGE]->(p:RiskPassage {kind: 'removed'}) RETURN i.item_id AS item, properties(p) AS p")
        assert removed["item"] == iid("24", 0) and removed["p"]["item_id"] == iid("24", 0)
        assert removed["p"]["older_accession"] == ACC["24"] and removed["p"]["newer_accession"] == ACC["25"]
        assert removed["p"]["filer_cik"] == NVDA_CIK and removed["p"]["chunk_ids"] == [f"{ACC['24']}:I.1A:0000"]
        assert removed["p"]["counterpart_chunk_ids"] == [] and removed["p"]["chunk_fallback"] is False and removed["p"]["snapshot_id"] == synthetic["snapshot"]
        added = one(d, "MATCH (i:RiskItem)-[:HAS_PASSAGE]->(p:RiskPassage {kind: 'added'}) RETURN i.item_id AS item")
        assert added["item"] == iid("25", 0)                                    # an added passage hangs off the NEWER item

    def test_the_supersedes_flags_are_stamped_on_the_existing_filing_edges_and_the_kind_is_untouched(self, synthetic):
        edges = rows(synthetic["driver"], "MATCH (n:Filing)-[s:SUPERSEDES]->(o:Filing) RETURN n.accession_no AS n, o.accession_no AS o, "
                     "s.kind AS kind, s.items_compared AS compared, s.not_compared_reason AS reason ORDER BY n")
        assert edges == [{"n": ACC["25"], "o": ACC["24"], "kind": "rolled", "compared": True, "reason": None},
                         {"n": ACC["26"], "o": ACC["25"], "kind": "rolled", "compared": True, "reason": None}]

    def test_items_are_linked_to_their_section_and_evidence_spans(self, synthetic):
        d = synthetic["driver"]
        assert one(d, "MATCH (:RiskItem)-[r:IN_SECTION]->(:FilingSection) RETURN count(r) AS n")["n"] == 10
        assert one(d, "MATCH (i:RiskItem {item_id: $id})-[:SPANS]->(e:EvidenceSpan) RETURN collect(e.chunk_id) AS ids", id=iid("24", 2))["ids"] == [f"{ACC['24']}:I.1A:0002"]
        assert synthetic["totals"]["spans"] == 10 and synthetic["totals"]["spans_without_evidence_node"] == 0

    def test_risk_factors_are_attached_to_the_item_that_holds_their_evidence_chunk(self, synthetic):
        links = rows(synthetic["driver"], "MATCH (rf:RiskFactor)-[r:OF_ITEM]->(i:RiskItem) RETURN rf.risk_id AS rf, i.item_id AS item, r.snapshot_id AS snap ORDER BY rf")
        assert [(r["rf"], r["item"]) for r in links] == [("rf24", iid("24", 0)), ("rf25", iid("25", 0)), ("rf26", iid("26", 0))]
        assert {r["snap"] for r in links} == {synthetic["snapshot"]}

    def test_the_retrievers_temporal_query_reads_the_loaded_graph(self, synthetic):
        driver = synthetic["driver"]
        found, pairs = select_temporal(run_cypher(driver, TEMPORAL_QUERY, ids=[NVDA_CIK]), "What changed?")
        (pair,) = pairs
        assert (pair["older_accession"], pair["newer_accession"], pair["compared"]) == (ACC["25"], ACC["26"], True)
        assert [i["item_id"] for i in found if i["change"] == "new"] == [iid("26", 3)]
        assert [i for i in found if i["change"] == "removed"] == []                         # the pandemic item left one pair earlier
        assert pair["totals"] == {"removed": 0, "unsettled": 0, "new": 1, "reworded": 0}

    def test_the_retrievers_passage_query_reads_the_loaded_graph_for_a_compared_pair(self, synthetic):
        driver = synthetic["driver"]
        got = run_cypher(driver, PASSAGES_QUERY, pairs=[{"cik": NVDA_CIK, "older": ACC["24"], "newer": ACC["25"]}])
        assert sorted(r["kind"] for r in got) == ["added", "removed"] and all(r["chunk_ids"] for r in got)
        assert run_cypher(driver, PASSAGES_QUERY, pairs=[{"cik": NVDA_CIK, "older": ACC["25"], "newer": ACC["26"]}]) == []

    def test_loading_again_changes_nothing(self, synthetic):
        before = graph_size(synthetic["driver"])
        item_loader.load_risk_items(synthetic["driver"], synthetic["settings"], ["NVDA"], snapshot_id=synthetic["snapshot"])
        assert graph_size(synthetic["driver"]) == before

    def test_risk_disclosure_status_follows_the_current_annual_filing(self, synthetic):
        assert temporal.apply_current_status(synthetic["driver"]) == {"Active": 1, "Historical": 2}

    def failing(self, driver, fragment):
        """The names of the item-layer / status checks whose query contains ``fragment`` and that return rows."""
        return [name for name, query, kind in vg.STRUCTURE_CHECKS + vg.ITEM_LAYER_CHECKS
                if fragment in name and kind == "none" and rows(driver, query)]

    def test_every_cypher_invariant_of_verify_graph_holds_on_the_loaded_graph(self, synthetic):
        d = synthetic["driver"]
        bad = {name: rows(d, query) for name, query, kind in vg.ITEM_LAYER_CHECKS if kind == "none" and rows(d, query)}
        assert bad == {}
        assert self.failing(d, "Deleted") == [] and self.failing(d, "Active risk") == []
        assert vg.check_counts(d, synthetic["settings"]) == [] and vg.check_false_drops(d, synthetic["settings"]) == []
        for name, query, _ in vg.INFO_CHECKS[:2]:                                    # the two info queries are valid Cypher too
            assert rows(d, query), name

    def test_each_invariant_detects_its_own_violation(self, synthetic):
        d, lake = synthetic["driver"], synthetic["settings"]
        run = lambda q, **p: rows(d, q, **p)      # noqa: E731
        try:
            with d.session() as session:
                # a pair marked not compared that still has an item layer: removed_in, SUCCEEDED_BY and passages must all be flagged
                session.run("MATCH (:Filing {accession_no: $n})-[s:SUPERSEDES]->(:Filing {accession_no: $o}) SET s.items_compared = false, "
                            "s.not_compared_reason = 'x'", n=ACC["25"], o=ACC["24"]).consume()
                session.run("CREATE (:RiskPassage {passage_id: 'orphan', kind: 'removed', snapshot_id: 'x'})").consume()
                session.run("MATCH (i:RiskItem {item_id: $id}) SET i.chunk_ids = [], i.snapshot_id = null", id=iid("26", 1)).consume()
                session.run("MATCH (i:RiskItem {item_id: $id}) SET i.chunk_ids = i.chunk_ids + ['ghost:chunk']", id=iid("26", 3)).consume()
                session.run("MATCH (n:Filing {accession_no: $n})-[s:SUPERSEDES]->(:Filing {accession_no: $o}) SET s.items_compared = null",
                            n=ACC["26"], o=ACC["25"]).consume()
                session.run("MATCH (:Company)-[d:DISCLOSES_RISK]->(:RiskFactor {risk_id: 'rf24'}) SET d.status = 'Deleted'").consume()
            assert self.failing(d, "removed_in only") == ["removed_in only on items of pairs with items_compared = true"]
            assert self.failing(d, "no SUCCEEDED_BY, removed_in") and self.failing(d, "exactly one HAS_PASSAGE parent")
            assert self.failing(d, "has chunk_ids") and self.failing(d, "carries the snapshot id")
            assert self.failing(d, "boolean items_compared") and self.failing(d, "no Deleted status remains")
            assert self.failing(d, "is_new only on items of pairs with items_compared = true")     # the F26 items of the pair now unflagged
            assert self.failing(d, "resolve to evidence spans") == []                              # the current pair is no longer 'compared': not read
            problems = {r["what"] for r in run(next(q for n, q, _ in vg.ITEM_LAYER_CHECKS if n.startswith("no SUCCEEDED_BY, removed_in")))}
            assert problems == {"SUCCEEDED_BY", "removed_in", "is_new", "RiskPassage"}
            # counts: a graph missing an item no longer equals the parquet
            with d.session() as session:
                session.run("MATCH (i:RiskItem {item_id: $id}) DETACH DELETE i", id=iid("26", 2)).consume()
            mismatches = vg.check_counts(d, lake)
            assert {"what": "RiskItem", "filer_cik": NVDA_CIK, "parquet_rows": 10, "graph_nodes": 9} in mismatches
            assert {"what": "RiskPassage", "filer_cik": vg.NO_FILER, "parquet_rows": 0, "graph_nodes": 1} in mismatches      # the orphan
        finally:
            with d.session() as session:
                session.run("MATCH (p:RiskPassage {passage_id: 'orphan'}) DETACH DELETE p").consume()
            item_loader.load_risk_items(d, lake, ["NVDA"], snapshot_id=synthetic["snapshot"])
            temporal.apply_current_status(d)
        assert {name for name, query, kind in vg.ITEM_LAYER_CHECKS if kind == "none" and rows(d, query)} == set()

    def test_the_resolvable_chunk_invariant_flags_a_current_pair_item_whose_chunk_is_not_an_evidence_span(self, synthetic):
        d, lake = synthetic["driver"], synthetic["settings"]
        try:
            with d.session() as session:
                session.run("MATCH (i:RiskItem {item_id: $id}) SET i.chunk_ids = i.chunk_ids + ['ghost:chunk']", id=iid("26", 3)).consume()
            assert self.failing(d, "resolve to evidence spans") == ["the citable chunk ids of the CURRENT pair (removed / unsettled / new items, passages) resolve to evidence spans"]
        finally:
            item_loader.load_risk_items(d, lake, ["NVDA"], snapshot_id=synthetic["snapshot"])

    def test_the_false_drop_guard_catches_a_removed_flag_on_an_item_that_is_still_in_the_newer_text(self, synthetic):
        d, lake = synthetic["driver"], synthetic["settings"]
        tax = iid("24", 1)                                                    # the tax item survives in F25 verbatim
        try:
            with d.session() as session:
                session.run("MATCH (i:RiskItem {item_id: $id}) SET i.removed_in = $n", id=tax, n=ACC["25"]).consume()
            (bad,) = vg.check_false_drops(d, lake)
            assert bad["item"] == tax and bad["ratio"] >= 90
        finally:
            item_loader.load_risk_items(d, lake, ["NVDA"], snapshot_id=synthetic["snapshot"])
        assert vg.check_false_drops(d, lake) == []

    def test_an_uncertain_older_decision_becomes_unsettled_in_reaches_the_retriever_and_passes_the_invariants(self, synthetic):
        """The F25 wafer item (older side of the current pair F25 -> F26) is re-labelled ``uncertain`` in the decisions parquet."""
        d, lake = synthetic["driver"], synthetic["settings"]
        path = items.table_path(items.alignment_dir(lake), "NVDA", "decisions")
        original = pd.read_parquet(path)
        changed = original.copy()
        hit = (changed["item_id"] == iid("25", 0)) & (changed["side"] == "older")
        assert hit.sum() == 1
        changed.loc[hit, ["label", "matched_item_id"]] = ["uncertain", None]
        changed.to_parquet(path, index=False)
        try:
            totals = item_loader.load_risk_items(d, lake, ["NVDA"], snapshot_id="sgitems-unsettled")
            assert totals["unsettled"] == 1 and totals["succeeded_by"] == 4                 # the uncertain item has no successor edge
            node = one(d, "MATCH (i:RiskItem {item_id: $id}) RETURN properties(i) AS p", id=iid("25", 0))["p"]
            assert node["unsettled_in"] == ACC["26"] and "removed_in" not in node
            assert [r["id"] for r in rows(d, "MATCH (i:RiskItem) WHERE i.unsettled_in IS NOT NULL RETURN i.item_id AS id")] == [iid("25", 0)]
            assert [r["id"] for r in rows(d, "MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL RETURN i.item_id AS id")] == [iid("24", 2)]
            found, pairs = select_temporal(run_cypher(d, TEMPORAL_QUERY, ids=[NVDA_CIK]), "What changed?")
            (row,) = [i for i in found if i["change"] == "unsettled"]
            assert row["item_id"] == iid("25", 0) and row["older_chunk_ids"] == [f"{ACC['25']}:I.1A:0000"] and row["newer_chunk_ids"] == []
            assert not [i for i in found if i["change"] == "removed"]                    # unsettled is never listed as removed
            assert pairs[0]["totals"] == {"removed": 0, "unsettled": 1, "new": 1, "reworded": 0}
            block = build_blocks({"anchors": {}, "edges": [], "metrics": [], "risks": [], "chunks": [], "temporal": found,
                                  "temporal_pairs": pairs})[0].temporal_block
            assert "Not matched (the text check could not verify" in block and "- showing 1 of 1:" in block
            assert {name: rows(d, query) for name, query, kind in vg.ITEM_LAYER_CHECKS if kind == "none" and rows(d, query)} == {}
            assert vg.check_unsettled_counts(d, lake) == [] and vg.check_counts(d, lake) == []
        finally:
            original.to_parquet(path, index=False)
            item_loader.load_risk_items(d, lake, ["NVDA"], snapshot_id=synthetic["snapshot"])
        assert one(d, "MATCH (i:RiskItem) WHERE i.unsettled_in IS NOT NULL RETURN count(i) AS n")["n"] == 0    # a reload clears the flag
        assert vg.check_unsettled_counts(d, lake) == []

    def test_the_unsettled_invariants_detect_their_own_violations(self, synthetic):
        d, lake = synthetic["driver"], synthetic["settings"]
        try:
            with d.session() as session:
                # both flags on one item (the pandemic item is removed_in F25 already); a flag naming a filing that never superseded it
                session.run("MATCH (i:RiskItem {item_id: $id}) SET i.unsettled_in = $n", id=iid("24", 2), n=ACC["25"]).consume()
                session.run("MATCH (i:RiskItem {item_id: $id}) SET i.unsettled_in = 'ghost-accession'", id=iid("24", 1)).consume()
            assert self.failing(d, "no item carries both") == ["no item carries both removed_in and unsettled_in"]
            assert self.failing(d, "unsettled_in only") == ["unsettled_in only on items of pairs with items_compared = true"]
            assert {"what": "unsettled_in", "filer_cik": NVDA_CIK, "parquet_rows": 0, "graph_nodes": 2} in vg.check_unsettled_counts(d, lake)
            # an unsettled item on the older side of a pair that was not compared
            with d.session() as session:
                session.run("MATCH (i:RiskItem {item_id: $id}) SET i.unsettled_in = $n", id=iid("24", 1), n=ACC["25"]).consume()
                session.run("MATCH (:Filing {accession_no: $n})-[s:SUPERSEDES]->(:Filing {accession_no: $o}) SET s.items_compared = false, "
                            "s.not_compared_reason = 'x'", n=ACC["25"], o=ACC["24"]).consume()
            query = next(q for n, q, _ in vg.ITEM_LAYER_CHECKS if n.startswith("no SUCCEEDED_BY, removed_in, unsettled_in"))
            assert "unsettled_in" in {r["what"] for r in rows(d, query)}
            assert self.failing(d, "unsettled_in only")                                   # ... and by the per-property check
        finally:
            item_loader.load_risk_items(d, lake, ["NVDA"], snapshot_id=synthetic["snapshot"])
        assert {name for name, query, kind in vg.ITEM_LAYER_CHECKS if kind == "none" and rows(d, query)} == set()
        assert vg.check_unsettled_counts(d, lake) == []

    def test_a_changed_alignment_replaces_the_old_layer_and_leaves_no_stale_drop(self, synthetic):
        """The pair is re-classified as not compared: removed_in / is_new / SUCCEEDED_BY / passages of the previous run must go."""
        d, lake = synthetic["driver"], synthetic["settings"]
        quality_path = lake.interim_dir / "risk_items" / "NVDA_risk_items_quality.json"
        original = quality_path.read_text(encoding="utf-8")
        data = json.loads(original)
        data["filings"][1]["low_coverage"], data["filings"][1]["coverage"] = True, 0.71
        quality_path.write_text(json.dumps(data), encoding="utf-8")
        try:
            items.run_align_items(lake, ["NVDA"])
            item_loader.load_risk_items(d, lake, ["NVDA"], snapshot_id="sgitems-second")
            assert one(d, "MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL OR i.unsettled_in IS NOT NULL OR i.is_new = true RETURN count(i) AS n")["n"] == 0
            assert one(d, "MATCH ()-[r:SUCCEEDED_BY]->() RETURN count(r) AS n")["n"] == 0
            assert one(d, "MATCH (p:RiskPassage) RETURN count(p) AS n")["n"] == 0
            flags = rows(d, "MATCH (:Filing)-[s:SUPERSEDES]->(:Filing) RETURN s.kind AS kind, s.items_compared AS c, s.not_compared_reason AS r")
            assert {(f["kind"], f["c"]) for f in flags} == {("rolled", False)} and all("71.0%" in f["r"] for f in flags)
            found, pairs = select_temporal(run_cypher(d, TEMPORAL_QUERY, ids=[NVDA_CIK]), "What changed?")
            assert pairs[0]["compared"] is False and found == [] and "71.0%" in pairs[0]["not_compared_reason"]
            assert {r["snap"] for r in rows(d, "MATCH (i:RiskItem) RETURN i.snapshot_id AS snap")} == {"sgitems-second"}
        finally:
            quality_path.write_text(original, encoding="utf-8")
            items.run_align_items(lake, ["NVDA"])
            item_loader.load_risk_items(d, lake, ["NVDA"], snapshot_id=synthetic["snapshot"])
        assert one(d, "MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL RETURN count(i) AS n")["n"] == 1


# --------------------------------------------------------------------------- the real lake

REAL_ITEMS = Path("data/interim/risk_items/NVDA_risk_items.parquet")
FY25, FY26 = "0001045810-25-000023", "0001045810-26-000021"


@pytest.fixture(scope="class")
def real(scratch_database, tmp_path_factory):
    if not REAL_ITEMS.exists() or not Path("data/interim/risk_items/INTC_risk_items.parquet").exists():
        pytest.skip("the real risk-item lake (data/interim/risk_items) is not present")
    settings = get_settings()
    with scratch_database("sgitemsreal") as (driver, _):
        schema.apply_schema(driver)
        snapshot = "sgitems-real"
        loaders.load_companies(driver, settings, snapshot_id=snapshot)
        tickers = items.discover_tickers(settings)               # EVERY ticker of the lake, so the full-graph invariants are checked
        loaders.load_filings_and_sections(driver, settings, tickers, snapshot_id=snapshot)
        for ticker in tickers:                                   # minimal EvidenceSpans (no embeddings) for the risk sections: what SPANS matches
            chunks = pd.read_parquet(loaders._chunks_path(settings, ticker), columns=["chunk_id", "accession_no", "section_id", "text"])
            chunks = chunks[chunks["section_id"].isin(["I.1A", "I.3"])]
            with driver.session() as session:
                session.run("UNWIND $rows AS r MERGE (e:EvidenceSpan {chunk_id: r.chunk_id}) SET e.accession_no = r.accession_no, "
                            "e.section_id = r.section_id, e.text = r.text", rows=chunks.to_dict("records")).consume()
        out = tmp_path_factory.mktemp("real_alignment")            # never over the real data/interim/risk_alignment
        run = items.run_align_items(settings, tickers, out_dir=out)
        totals = item_loader.load_risk_items(driver, settings, tickers, snapshot_id=snapshot, alignment_directory=out)
        yield {"driver": driver, "settings": settings, "summary": run.summary, "totals": totals, "alignment": out}


class TestRealNvidiaAndIntel:
    def temporal(self, real, cik):
        return select_temporal(run_cypher(real["driver"], TEMPORAL_QUERY, ids=[cik]), "How have the risk disclosures changed?")

    def test_the_nvidia_pair_is_fy25_to_fy26_and_was_compared(self, real):
        _, pairs = self.temporal(real, NVDA_CIK)
        (pair,) = pairs
        assert (pair["older_accession"], pair["newer_accession"]) == (FY25, FY26)
        assert (pair["older_date"], pair["newer_date"], pair["compared"]) == ("2025-02-26", "2026-02-25", True)

    def test_no_nvidia_item_is_removed_and_exactly_one_is_new(self, real):
        found, pairs = self.temporal(real, NVDA_CIK)
        assert [i for i in found if i["change"] == "removed"] == [] and pairs[0]["totals"]["removed"] == 0
        new = [i for i in found if i["change"] == "new"]
        assert len(new) == 1 and pairs[0]["totals"]["new"] == 1 and new[0]["headline"].startswith("Commercial arrangements")
        assert pairs[0]["totals"]["reworded"] == 16

    def test_the_flagship_sentences_are_reported_as_removed_passages_with_chunk_ids(self, real):
        _, pairs = self.temporal(real, NVDA_CIK)
        got = run_cypher(real["driver"], PASSAGES_QUERY, pairs=[{"cik": NVDA_CIK, "older": FY25, "newer": FY26}])
        removed = [r for r in got if r["kind"] == "removed"]
        assert any("Notified Advanced Computing" in r["text"] for r in removed)
        assert any("out of China and Hong Kong" in r["text"] for r in removed)
        assert got and all(r["chunk_ids"] for r in got)                      # every passage cites a chunk (own or the recorded fallback)
        fallback = one(real["driver"], "MATCH (p:RiskPassage {filer_cik: $c, newer_accession: $n}) RETURN count(CASE WHEN p.chunk_fallback THEN 1 END) AS n",
                       c=NVDA_CIK, n=FY26)["n"]
        assert fallback == 0
        passages, pairs_out = select_passages(got, pairs, "What changed?")
        assert pairs_out[0]["passage_totals"]["removed"] >= 2 and passages

    def test_the_answer_block_renders_the_removed_passages_and_the_new_item(self, real):
        items_, pairs = self.temporal(real, NVDA_CIK)
        passages, pairs = select_passages(run_cypher(real["driver"], PASSAGES_QUERY, pairs=[{"cik": NVDA_CIK, "older": FY25, "newer": FY26}]),
                                          pairs, "What happened to the Notified Advanced Computing process for China?")
        blocks, context, valid = build_blocks({"anchors": {}, "edges": [], "metrics": [], "risks": [], "chunks": [], "temporal": items_,
                                               "temporal_pairs": pairs, "temporal_passages": passages})
        block = blocks.temporal_block
        for needle in ("that no longer appear", "Commercial arrangements"):
            assert needle in block, f"{needle!r} missing from the block: {block[:6000]!r}"
        # The block quotes a passage only up to PASSAGE_QUOTE_CHARS (300) while the stored passages are ~1,000 characters: the NAC and
        # Hong Kong sentences sit in the MIDDLE of two such passages, so their TEXT is clipped out of the rendered block (reported to
        # the retriever owner); their citation ids (chunk 0257 of the FY25 Item 1A) are in the block and citable.
        assert f"{FY25}:I.1A:0257" in valid and f"{FY25}:I.1A:0257" in block

    @pytest.mark.xfail(strict=True, reason=(
        "OPEN, reported to the retriever/answerer owner (gate H): context_layout.PASSAGE_QUOTE_CHARS = 300 quotes only the START of a "
        "removed passage while the stored passages are ~1,100 characters, so the NAC sentence (passage r006) and the China / Hong Kong "
        "sentence (passage r005) never reach the answer model. Fix: a smaller max_passage_chars in graph/passages.py, or quote the "
        "sentence of each passage that overlaps the question most. This test flips (XPASS strict) the day the flagship text is in the block."))
    def test_the_flagship_sentences_reach_the_answer_context(self, real):
        items_, pairs = self.temporal(real, NVDA_CIK)
        question = "What happened to the Notified Advanced Computing process and the transition out of China and Hong Kong?"
        passages, pairs = select_passages(run_cypher(real["driver"], PASSAGES_QUERY, pairs=[{"cik": NVDA_CIK, "older": FY25, "newer": FY26}]),
                                          pairs, question)
        block = build_blocks({"anchors": {}, "edges": [], "metrics": [], "risks": [], "chunks": [], "temporal": items_,
                              "temporal_pairs": pairs, "temporal_passages": passages})[0].temporal_block
        assert "Notified Advanced Computing" in block and "out of China and Hong Kong" in block

    def test_an_intel_pair_is_not_compared_and_the_block_says_so(self, real):
        found, pairs = self.temporal(real, INTC_CIK)
        (pair,) = pairs
        assert pair["compared"] is False and found == [] and pair["not_compared_reason"]
        block = build_blocks({"anchors": {}, "edges": [], "metrics": [], "risks": [], "chunks": [], "temporal": found,
                              "temporal_pairs": pairs})[0].temporal_block
        assert "comparison not available" in block and pair["not_compared_reason"] in block
        assert one(real["driver"], "MATCH (i:RiskItem {filer_cik: $c}) WHERE i.removed_in IS NOT NULL OR i.is_new = true RETURN count(i) AS n", c=INTC_CIK)["n"] == 0
        assert one(real["driver"], "MATCH (p:RiskPassage {filer_cik: $c}) RETURN count(p) AS n", c=INTC_CIK)["n"] == 0

    def test_the_item_layer_invariants_of_verify_graph_hold_on_the_real_layers_of_every_ticker(self, real):
        d = real["driver"]
        assert {name: rows(d, query) for name, query, kind in vg.ITEM_LAYER_CHECKS if kind == "none" and rows(d, query)} == {}

    def test_the_false_drop_guard_passes_on_the_real_removed_items(self, real):
        d = real["driver"]
        assert one(d, "MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL RETURN count(i) AS n")["n"] == 26
        assert vg.check_false_drops(d, real["settings"]) == []

    def test_every_real_item_of_every_ticker_was_loaded_and_the_counts_equal_the_parquets(self, real):
        d = real["driver"]
        expected = sum(len(pd.read_parquet(f)) for f in Path("data/interim/risk_items").glob("*_risk_items.parquet"))
        assert real["totals"]["items"] == expected == one(d, "MATCH (i:RiskItem) RETURN count(i) AS n")["n"]
        assert vg.check_counts(d, real["settings"], real["alignment"]) == []
        assert real["totals"]["succeeded_by"] == one(d, "MATCH ()-[r:SUCCEEDED_BY]->() RETURN count(r) AS n")["n"]
        # the unsettled items: as many as the decisions parquet has older-side 'uncertain' decisions of compared pairs
        assert vg.check_unsettled_counts(d, real["settings"], real["alignment"]) == []
        assert real["totals"]["unsettled"] == one(d, "MATCH (i:RiskItem) WHERE i.unsettled_in IS NOT NULL RETURN count(i) AS n")["n"]
        assert one(d, "MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL AND i.unsettled_in IS NOT NULL RETURN count(i) AS n")["n"] == 0

    def test_every_companys_current_pair_reaches_the_retriever_and_only_intel_and_asml_are_not_compared(self, real):
        ciks = sorted(int(c) for c in pd.concat([pd.read_parquet(f, columns=["filer_cik"]) for f in
                                                  Path("data/interim/risk_items").glob("*_risk_items.parquet")])["filer_cik"].unique())
        _, pairs = select_temporal(run_cypher(real["driver"], TEMPORAL_QUERY, ids=ciks), "How have the risk disclosures changed?")
        assert len(pairs) == len(ciks) == 13
        assert {p["company"] for p in pairs if not p["compared"]} == {"Intel", "ASML"}
        assert all(p["not_compared_reason"] for p in pairs if not p["compared"])
        assert all(p["older_accession"] != p["newer_accession"] and p["newer_date"] > p["older_date"] for p in pairs)

    def test_all_32_consecutive_annual_pairs_are_stamped_and_only_the_three_untrustworthy_ones_are_not_compared(self, real):
        flags = rows(real["driver"], "MATCH (:Filing)-[s:SUPERSEDES {kind: 'rolled'}]->(:Filing) WHERE s.items_compared IS NOT NULL "
                     "RETURN s.items_compared AS compared, count(*) AS n")
        assert {f["compared"]: f["n"] for f in flags} == {True: 29, False: 3}
        assert real["totals"]["supersedes"] == 32
