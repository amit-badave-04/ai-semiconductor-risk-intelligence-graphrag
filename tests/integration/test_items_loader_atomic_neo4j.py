"""The item loader's atomicity and clean-up against a real Neo4j - opt-in (RUN_NEO4J_TESTS=1; on Community also SEMIGRAPH_ALLOW_WIPE=1).

Written for the M1b review (LOW findings) and NOT run in the session that wrote them (a real graph was serving on another port):
the pure tests in ``tests/test_item_loader_pure.py`` cover the statement order and the transaction boundaries with a fake driver;
these check what only a database can:

* a crash after the clear rolls the whole ticker back (the previous layer, its snapshot id and its stamps are still there);
* a ``SUPERSEDES.items_compared`` stamp on a pair that is no longer consecutive is cleared (and ``verify_graph`` then reports the
  leftover annual -> annual edge, deliberately: an edge the item layer has no opinion on is not "compared");
* a full load deletes the layer of a filer that has no risk-item file any more, a load of named tickers keeps it;
* ``verify_graph``'s parquet-vs-graph parity checks (``removed_in``, ``is_new``, SUCCEEDED_BY per kind) pass on the loaded layer and
  fail on a graph that lost every ``removed_in``.

The synthetic lake is ``tests/lakefix.py`` under the ticker NVDA (three annual filings; one item removed, two new, five edges).
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from neo4j.exceptions import Neo4jError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lakefix  # noqa: E402
from semigraph.graph import item_loader, items, loaders, schema  # noqa: E402

VERIFY = Path(__file__).resolve().parents[2] / "scripts" / "verify_graph.py"
_spec = importlib.util.spec_from_file_location("verify_graph_atomic_it", VERIFY)
vg = importlib.util.module_from_spec(_spec)
sys.modules["verify_graph_atomic_it"] = vg
_spec.loader.exec_module(vg)

EMBED_DIM = 1024
NVDA_CIK = 1045810
ORPHAN_CIK = 424242
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


def run(driver, query, **params):
    with driver.session() as session:
        session.run(query, **params).consume()


def build_lake(root: Path, base):
    """The NVDA synthetic lake (the same construction as tests/integration/test_items_loader_neo4j.py)."""
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


@pytest.fixture
def world(scratch_database, tmp_path, neo4j_base_settings):
    """A fresh database with the base layers loaded and the alignment computed; the TEST runs ``load_risk_items`` itself."""
    with scratch_database("sgatomic") as (driver, _):
        lake = build_lake(tmp_path, neo4j_base_settings)
        schema.apply_schema(driver)
        driver.execute_query("CALL db.awaitIndexes(120)")
        loaders.load_companies(driver, lake, snapshot_id="sgatomic-base")
        loaders.load_filings_and_sections(driver, lake, ["NVDA"], snapshot_id="sgatomic-base")
        loaders.load_evidence_spans(driver, lake, FakeEmbedder(), ["NVDA"], snapshot_id="sgatomic-base")
        items.run_align_items(lake, ["NVDA"])
        yield {"driver": driver, "settings": lake}


def load(world, snapshot, tickers=("NVDA",)):
    return item_loader.load_risk_items(world["driver"], world["settings"], list(tickers) if tickers else None, snapshot_id=snapshot)


def fingerprint(driver) -> dict:
    """Everything a half-applied load could change: counts, snapshot ids, the removed flags and the SUPERSEDES stamps."""
    counts = {name: one(driver, query)["n"] for name, query in (
        ("items", "MATCH (i:RiskItem) RETURN count(i) AS n"), ("passages", "MATCH (p:RiskPassage) RETURN count(p) AS n"),
        ("succeeded", "MATCH ()-[r:SUCCEEDED_BY]->() RETURN count(r) AS n"), ("in_section", "MATCH ()-[r:IN_SECTION]->() RETURN count(r) AS n"),
        ("spans", "MATCH (:RiskItem)-[r:SPANS]->() RETURN count(r) AS n"), ("has_passage", "MATCH ()-[r:HAS_PASSAGE]->() RETURN count(r) AS n"),
        ("of_item", "MATCH ()-[r:OF_ITEM]->() RETURN count(r) AS n"))}
    return {"counts": counts,
            "snapshots": sorted(r["s"] for r in rows(driver, "MATCH (n) WHERE n:RiskItem OR n:RiskPassage RETURN DISTINCT n.snapshot_id AS s")),
            "removed": sorted(r["id"] for r in rows(driver, "MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL RETURN i.item_id AS id")),
            "stamps": rows(driver, "MATCH (n:Filing)-[s:SUPERSEDES]->(o:Filing) RETURN n.accession_no AS n, o.accession_no AS o, "
                                   "s.items_compared AS compared ORDER BY n")}


class TestOneTransactionPerTicker:
    def test_a_crash_after_the_clear_leaves_the_previous_layer_exactly_as_it_was(self, world, monkeypatch):
        load(world, "snap-1")
        before = fingerprint(world["driver"])
        assert before["counts"]["items"] == 10 and before["snapshots"] == ["snap-1"] and before["removed"]
        # the SPANS statement runs AFTER the clear, the item MERGE and the IN_SECTION edges: break it, and the load dies half-way
        monkeypatch.setattr(item_loader, "_SPANS_CYPHER", "THIS IS NOT CYPHER")
        with pytest.raises(Neo4jError):
            load(world, "snap-2")
        assert fingerprint(world["driver"]) == before                  # nothing was cleared, nothing carries snap-2
        monkeypatch.undo()
        load(world, "snap-3")                                          # and a healthy re-run replaces the layer completely
        after = fingerprint(world["driver"])
        assert after["snapshots"] == ["snap-3"] and after["counts"] == before["counts"] and after["removed"] == before["removed"]

    def test_a_missing_supersedes_edge_rolls_the_written_layer_back(self, world):
        load(world, "snap-1")
        before = fingerprint(world["driver"])
        run(world["driver"], "MATCH (:Filing {accession_no: $n})-[s:SUPERSEDES]->(:Filing {accession_no: $o}) DELETE s",
            n=ACC["26"], o=ACC["25"])
        broken = fingerprint(world["driver"])
        with pytest.raises(items.AlignItemsError, match="no SUPERSEDES edge"):
            load(world, "snap-2")
        assert fingerprint(world["driver"]) == broken                   # the failed load changed nothing, not even the stamps
        assert broken["counts"] == before["counts"] and broken["snapshots"] == ["snap-1"]


class TestStaleStamps:
    def test_a_stamp_on_a_pair_that_is_no_longer_consecutive_is_cleared_and_verify_graph_names_the_leftover_edge(self, world):
        d = world["driver"]
        load(world, "snap-1")
        run(d, "MATCH (n:Filing {accession_no: $n}), (o:Filing {accession_no: $o}) "
               "CREATE (n)-[:SUPERSEDES {kind: 'rolled', items_compared: true, not_compared_reason: 'stale'}]->(o)",
            n=ACC["26"], o=ACC["24"])                                    # F26 -> F24 skips F25: not a consecutive pair
        load(world, "snap-2")
        stale = one(d, "MATCH (:Filing {accession_no: $n})-[s:SUPERSEDES]->(:Filing {accession_no: $o}) "
                       "RETURN s.kind AS kind, s.items_compared AS compared, s.not_compared_reason AS reason", n=ACC["26"], o=ACC["24"])
        assert stale == {"kind": "rolled", "compared": None, "reason": None}       # the stamp is gone, the filing edge is not touched
        current = rows(d, "MATCH (n:Filing)-[s:SUPERSEDES]->(o:Filing) WHERE NOT (n.accession_no = $a26 AND o.accession_no = $a24) "
                          "RETURN s.items_compared AS compared", a26=ACC["26"], a24=ACC["24"])
        assert [r["compared"] for r in current] == [True, True]
        (check,) = [q for name, q, _ in vg.ITEM_LAYER_CHECKS if name.startswith("every annual->annual SUPERSEDES")]
        assert rows(d, check) == [{"newer": ACC["26"], "older": ACC["24"]}]        # deliberate: an unstamped edge is not "compared"


class TestOrphanedFilers:
    @staticmethod
    def plant_orphans(driver):
        run(driver, "CREATE (:RiskItem {item_id: 'orphan-item', filer_cik: $cik, accession_no: 'orphan-1', snapshot_id: 'old'}) "
                    "CREATE (:RiskItem {item_id: 'orphan-no-filer', snapshot_id: 'old'}) "
                    "CREATE (p:RiskPassage {passage_id: 'orphan-passage', filer_cik: $cik, snapshot_id: 'old'}) "
                    "WITH p MATCH (i:RiskItem {item_id: 'orphan-item'}) CREATE (i)-[:HAS_PASSAGE]->(p)", cik=ORPHAN_CIK)
        run(driver, "CREATE (c:Company {cik: $cik, name: 'Orphan Corp', ticker: 'ORPH'}) "
                    "CREATE (a:Filing {accession_no: 'orphan-1'}) CREATE (b:Filing {accession_no: 'orphan-0'}) "
                    "CREATE (c)-[:FILED]->(a) CREATE (c)-[:FILED]->(b) "
                    "CREATE (a)-[:SUPERSEDES {kind: 'rolled', items_compared: true}]->(b)", cik=ORPHAN_CIK)

    @staticmethod
    def orphans(driver) -> dict:
        return {"items": sorted(r["id"] for r in rows(driver, "MATCH (i:RiskItem) WHERE i.item_id STARTS WITH 'orphan' RETURN i.item_id AS id")),
                "passages": one(driver, "MATCH (p:RiskPassage {passage_id: 'orphan-passage'}) RETURN count(p) AS n")["n"],
                "stamp": one(driver, "MATCH (:Filing {accession_no: 'orphan-1'})-[s:SUPERSEDES]->(:Filing) RETURN s.items_compared AS c")["c"]}

    def test_a_load_of_named_tickers_keeps_another_filers_layer_and_a_full_load_deletes_it(self, world):
        d = world["driver"]
        load(world, "snap-1")
        real = fingerprint(d)
        self.plant_orphans(d)
        totals = load(world, "snap-2", tickers=("NVDA",))                # named ticker: the orphan is not this load's business
        assert totals["purged_items"] == totals["purged_passages"] == 0
        assert self.orphans(d) == {"items": ["orphan-item", "orphan-no-filer"], "passages": 1, "stamp": True}
        totals = load(world, "snap-3", tickers=None)                     # every ticker with a risk-item file: NVDA only
        assert (totals["purged_items"], totals["purged_passages"]) == (2, 1)
        assert self.orphans(d) == {"items": [], "passages": 0, "stamp": None}       # the filing edge stays, its stamp is gone
        after = fingerprint(d)
        assert after["counts"] == real["counts"] and after["removed"] == real["removed"] and after["snapshots"] == ["snap-3"]


class TestVerifyGraphParity:
    def test_the_parity_checks_pass_on_the_loaded_layer_and_fail_on_a_graph_that_lost_every_removed_in(self, world):
        d, lake = world["driver"], world["settings"]
        load(world, "snap-1")
        for check in (vg.check_removed_counts, vg.check_new_counts, vg.check_succeeded_counts, vg.check_unsettled_counts, vg.check_counts):
            assert check(d, lake) == [], check.__name__
        run(d, "MATCH (i:RiskItem) SET i.removed_in = null")                                # a loader that dropped every removed_in
        (bad,) = vg.check_removed_counts(d, lake)
        assert bad == {"what": "removed_in", "filer_cik": NVDA_CIK, "parquet_rows": 1, "graph_nodes": 0}
        run(d, "MATCH ()-[r:SUCCEEDED_BY {kind: 'reworded'}]->() DELETE r")                 # and one that lost a kind of edge
        assert {r["what"] for r in vg.check_succeeded_counts(d, lake)} == {"SUCCEEDED_BY[reworded]"}
