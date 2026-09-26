"""scripts/verify_graph.py: the risk-item invariants. The Cypher ones run on a real Neo4j in tests/integration/test_items_loader_neo4j.py;
here the pure parts (false-drop guard, count check, report and exit logic) and the shape of the check list."""

import importlib.util
import sys
from pathlib import Path

import lakefix
from semigraph.config import Settings

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_graph.py"
spec = importlib.util.spec_from_file_location("verify_graph", SCRIPT)
vg = importlib.util.module_from_spec(spec)
sys.modules["verify_graph"] = vg
spec.loader.exec_module(vg)

NEWER = ("We depend on a single foundry in Taiwan for our wafers. Export controls may restrict our sales to China. "
         "Tax law changes could raise our effective tax rate.")


def row(item_id="i1", headline="Export controls may restrict our sales to China.", text=None, removed_in="acc-26", section="I.1A"):
    return {"item_id": item_id, "headline": headline, "text": text or headline, "removed_in": removed_in, "section_id": section, "ticker": "T"}


def texts(mapping):
    return lambda r: mapping.get((r["removed_in"], r["section_id"]))


# --------------------------------------------------------------------------- the false-drop guard

class TestFalseDrops:
    def test_a_removed_item_whose_headline_is_still_a_sentence_of_the_newer_section_is_a_false_drop(self):
        bad = vg.false_drops([row()], texts({("acc-26", "I.1A"): NEWER}))
        assert len(bad) == 1 and bad[0]["item"] == "i1" and bad[0]["ratio"] == 100.0

    def test_case_and_punctuation_do_not_hide_a_false_drop(self):
        bad = vg.false_drops([row(headline="EXPORT CONTROLS MAY RESTRICT OUR SALES TO CHINA")], texts({("acc-26", "I.1A"): NEWER}))
        assert len(bad) == 1 and bad[0]["ratio"] >= vg.FALSE_DROP_RATIO

    def test_a_headline_that_is_gone_from_the_newer_section_is_a_true_drop(self):
        assert vg.false_drops([row(headline="The pandemic has interrupted our operations and our suppliers.")],
                              texts({("acc-26", "I.1A"): NEWER})) == []

    def test_a_similar_but_not_close_sentence_is_not_flagged(self):
        near = row(headline="Export controls may restrict our sales of accelerators to customers in several other countries too.")
        assert vg.false_drops([near], texts({("acc-26", "I.1A"): NEWER})) == []

    def test_a_paragraph_unit_is_probed_by_its_first_sentence(self):
        unit = row(headline="", text="Tax law changes could raise our effective tax rate. Nothing else here matters at all.")
        bad = vg.false_drops([unit], texts({("acc-26", "I.1A"): NEWER}))
        assert len(bad) == 1 and bad[0]["headline"].startswith("Tax law changes")

    def test_an_item_with_nothing_to_probe_is_skipped_not_matched_against_everything(self):
        assert vg.false_drops([row(headline="", text="Short.")], texts({("acc-26", "I.1A"): NEWER})) == []
        assert vg.false_drops([row(headline=None, text=None)], texts({("acc-26", "I.1A"): NEWER})) == []

    def test_an_item_whose_newer_section_text_is_unavailable_is_reported_never_passed_silently(self):
        (bad,) = vg.false_drops([row()], texts({}))
        assert bad["problem"] == "newer section text not available"

    def test_the_threshold_is_a_parameter_and_defaults_to_ninety(self):
        assert vg.FALSE_DROP_RATIO == 90.0
        soft = row(headline="Export controls may restrict our sales to customers in China.")
        assert vg.false_drops([soft], texts({("acc-26", "I.1A"): NEWER}), ratio=99.0) == []
        assert len(vg.false_drops([soft], texts({("acc-26", "I.1A"): NEWER}), ratio=80.0)) == 1


# --------------------------------------------------------------------------- counts

class TestCounts:
    def test_equal_counts_pass_and_a_difference_names_the_filer_and_both_numbers(self):
        assert vg.count_mismatches({1: 5, 2: 3}, {1: 5, 2: 3}, "RiskItem") == []
        assert vg.count_mismatches({1: 5, 2: 3}, {1: 5, 2: 2}, "RiskItem") == [
            {"what": "RiskItem", "filer_cik": 2, "parquet_rows": 3, "graph_nodes": 2}]

    def test_a_filer_missing_from_the_graph_counts_as_zero_and_so_does_an_extra_filer(self):
        got = vg.count_mismatches({1: 5}, {2: 4}, "RiskPassage")
        assert {(r["filer_cik"], r["parquet_rows"], r["graph_nodes"]) for r in got} == {(1, 5, 0), (2, 0, 4)}

    def test_the_row_counts_come_from_the_item_and_passage_parquets_per_filer(self, tmp_path):
        from semigraph.graph import items

        settings = lakefix.build_lake(tmp_path)
        items.run_align_items(settings, ["ZZZ"])
        items_by_cik, passages_by_cik = vg.lake_row_counts(settings)
        assert items_by_cik == {lakefix.CIK: 10} and passages_by_cik == {lakefix.CIK: 2}

    def test_without_a_lake_the_count_check_is_skipped_not_passed(self, tmp_path):
        settings = Settings(data_dir=tmp_path / "nothing", _env_file=None)
        assert vg.lake_row_counts(settings) is None and vg.check_counts(object(), settings) is None
        assert vg.check_false_drops(object(), settings) is None

    def test_check_counts_compares_the_graph_with_the_parquets(self, tmp_path, monkeypatch):
        from semigraph.graph import items

        settings = lakefix.build_lake(tmp_path)
        items.run_align_items(settings, ["ZZZ"])
        graph = {"RiskItem": [{"cik": lakefix.CIK, "n": 10}], "RiskPassage": [{"cik": lakefix.CIK, "n": 1}]}
        monkeypatch.setattr(vg.client, "run_cypher", lambda driver, q, **p: graph["RiskItem" if "RiskItem" in q else "RiskPassage"])
        (bad,) = vg.check_counts(object(), settings)
        assert bad == {"what": "RiskPassage", "filer_cik": lakefix.CIK, "parquet_rows": 2, "graph_nodes": 1}


class TestUnsettledCounts:
    """The graph's ``unsettled_in`` items per filer equal the older-side ``uncertain`` decisions of COMPARED pairs in the parquet."""

    @staticmethod
    def edit_decisions(settings, mutate):
        import pandas as pd

        from semigraph.graph import items

        path = items.table_path(items.alignment_dir(settings), "ZZZ", "decisions")
        frame = pd.read_parquet(path)
        mutate(frame)
        frame.to_parquet(path, index=False)

    @staticmethod
    def label(frame, item_id, side, label):
        hit = (frame["item_id"] == item_id) & (frame["side"] == side)
        assert hit.sum() == 1
        frame.loc[hit, "label"] = label

    @staticmethod
    def lake(tmp_path):
        from semigraph.graph import items

        settings = lakefix.build_lake(tmp_path)
        items.run_align_items(settings, ["ZZZ"])
        return settings

    def test_a_lake_with_no_uncertain_decision_expects_no_unsettled_item(self, tmp_path):
        assert vg.lake_unsettled_counts(self.lake(tmp_path)) == {}

    def test_only_older_side_uncertain_decisions_are_counted_per_filer(self, tmp_path):
        settings = self.lake(tmp_path)

        def mutate(frame):
            self.label(frame, f"{lakefix.ACC['24']}:I.1A:i001", "older", "uncertain")
            self.label(frame, f"{lakefix.ACC['25']}:I.1A:i000", "older", "uncertain")
            self.label(frame, f"{lakefix.ACC['25']}:I.1A:i002", "newer", "uncertain")      # the newer side is never unsettled

        self.edit_decisions(settings, mutate)
        assert vg.lake_unsettled_counts(settings) == {lakefix.CIK: 2}

    def test_a_decision_of_a_pair_that_was_not_compared_is_not_counted(self, tmp_path):
        import pandas as pd

        from semigraph.graph import items

        settings = self.lake(tmp_path)
        self.edit_decisions(settings, lambda f: self.label(f, f"{lakefix.ACC['25']}:I.1A:i000", "older", "uncertain"))
        pairs_path = items.table_path(items.alignment_dir(settings), "ZZZ", "pairs")
        pairs = pd.read_parquet(pairs_path)
        pairs.loc[pairs["newer_accession"] == lakefix.ACC["26"], "comparable"] = False
        pairs.to_parquet(pairs_path, index=False)
        assert vg.lake_unsettled_counts(settings) == {}

    def test_without_a_lake_the_check_is_skipped_not_passed(self, tmp_path):
        settings = Settings(data_dir=tmp_path / "nothing", _env_file=None)
        assert vg.lake_unsettled_counts(settings) is None and vg.check_unsettled_counts(object(), settings) is None

    def test_the_check_compares_the_graph_with_the_decisions_and_names_the_filer_and_both_numbers(self, tmp_path, monkeypatch):
        settings = self.lake(tmp_path)
        self.edit_decisions(settings, lambda f: self.label(f, f"{lakefix.ACC['24']}:I.1A:i001", "older", "uncertain"))
        graph = [{"cik": lakefix.CIK, "n": 1}]
        monkeypatch.setattr(vg.client, "run_cypher", lambda driver, q, **p: graph)
        assert vg.check_unsettled_counts(object(), settings) == []
        graph[:] = []                                                       # the loader never ran, or lost the flag
        assert vg.check_unsettled_counts(object(), settings) == [
            {"what": "unsettled_in", "filer_cik": lakefix.CIK, "parquet_rows": 1, "graph_nodes": 0}]
        graph[:] = [{"cik": lakefix.CIK, "n": 2}]
        assert vg.check_unsettled_counts(object(), settings)[0]["graph_nodes"] == 2

    def test_the_graph_side_reads_the_unsettled_in_property_and_never_writes(self, tmp_path, monkeypatch):
        settings = self.lake(tmp_path)
        seen = []
        monkeypatch.setattr(vg.client, "run_cypher", lambda driver, q, **p: seen.append(q) or [])
        vg.check_unsettled_counts(object(), settings)
        (query,) = seen
        assert "i.unsettled_in IS NOT NULL" in query and "count(i)" in query and "SET" not in query and "MERGE" not in query


class TestDecisionCounts:
    """``lake_decision_counts``: what the loader must have written, per filer, read from the decisions parquet of COMPARED pairs:
    ``removed_in`` (older ``removed``), ``is_new`` (newer ``new``), ``unsettled_in`` (older ``uncertain``) and the SUCCEEDED_BY
    edges per kind (older side, a matched item)."""

    @staticmethod
    def lake(tmp_path):
        from semigraph.graph import items

        settings = lakefix.build_lake(tmp_path)
        items.run_align_items(settings, ["ZZZ"])
        return settings

    def test_the_synthetic_lake_expects_one_removed_two_new_and_five_edges_of_three_kinds(self, tmp_path):
        counts = vg.lake_decision_counts(self.lake(tmp_path))
        assert counts["removed_in"] == {lakefix.CIK: 1} and counts["is_new"] == {lakefix.CIK: 2}
        assert counts["unsettled_in"] == {}
        assert sum(c.get(lakefix.CIK, 0) for c in counts["succeeded_by"].values()) == 5
        assert set(counts["succeeded_by"]) <= {"unchanged", "reworded", "merged"}

    def test_the_removed_and_new_items_of_a_pair_that_was_not_compared_are_not_expected(self, tmp_path):
        import pandas as pd

        from semigraph.graph import items

        settings = self.lake(tmp_path)
        path = items.table_path(items.alignment_dir(settings), "ZZZ", "pairs")
        pairs = pd.read_parquet(path)
        pairs.loc[pairs["newer_accession"] == lakefix.ACC["25"], "comparable"] = False
        pairs.to_parquet(path, index=False)
        counts = vg.lake_decision_counts(settings)
        assert counts["removed_in"] == {} and counts["is_new"] == {lakefix.CIK: 1}          # only F26's AI item is left
        assert sum(c.get(lakefix.CIK, 0) for c in counts["succeeded_by"].values()) == 3     # F25 -> F26 only

    def test_an_edge_needs_a_matched_item_and_only_the_older_side_carries_one(self, tmp_path):
        import pandas as pd

        from semigraph.graph import items

        settings = self.lake(tmp_path)
        path = items.table_path(items.alignment_dir(settings), "ZZZ", "decisions")
        frame = pd.read_parquet(path)
        older = (frame["side"] == "older") & (frame["label"].isin(["unchanged", "reworded", "merged"]))
        with_edge = int(older.sum())
        frame.loc[older.idxmax(), "matched_item_id"] = None                     # one older decision loses its counterpart
        frame.to_parquet(path, index=False)
        counts = vg.lake_decision_counts(settings)
        assert sum(c.get(lakefix.CIK, 0) for c in counts["succeeded_by"].values()) == with_edge - 1

    def test_without_a_lake_there_is_nothing_to_compare_and_the_unsettled_reader_is_a_thin_wrapper(self, tmp_path):
        settings = Settings(data_dir=tmp_path / "nothing", _env_file=None)
        assert vg.lake_decision_counts(settings) is None and vg.lake_unsettled_counts(settings) is None
        lake = self.lake(tmp_path / "lake")
        assert vg.lake_unsettled_counts(lake) == vg.lake_decision_counts(lake)["unsettled_in"] == {}


def layer_of_lake(settings):
    """The rows the REAL loader would write for the synthetic lake (``build_item_layer``): the truth a faithful graph reproduces."""
    from semigraph.graph import item_loader as il

    items_df, pairs, decisions, passages = il._read_ticker(settings, "ZZZ")
    current = {r: r.startswith(lakefix.ACC["26"]) for r in items_df["item_id"]}
    return il.build_item_layer("ZZZ", items_df, pairs, decisions, passages, current)


class GraphFromLayer:
    """``client.run_cypher`` stand-in that answers every count query from the loader's own rows (optionally sabotaged)."""

    def __init__(self, layer, *, drop=(), extra_removed=0):
        self.layer, self.drop, self.extra_removed = layer, set(drop), extra_removed

    def __call__(self, driver, query, **params):
        items, cik = self.layer.items, lakefix.CIK
        if query == vg.REMOVED_GRAPH_QUERY:
            n = 0 if "removed_in" in self.drop else sum(1 for r in items if r["removed_in"]) + self.extra_removed
            return [{"cik": cik, "n": n}] if n else []
        if query == vg.NEW_GRAPH_QUERY:
            n = 0 if "is_new" in self.drop else sum(1 for r in items if r["is_new"])
            return [{"cik": cik, "n": n}] if n else []
        if query == vg.UNSETTLED_GRAPH_QUERY:
            n = sum(1 for r in items if r["unsettled_in"])
            return [{"cik": cik, "n": n}] if n else []
        if query == vg.SUCCEEDED_GRAPH_QUERY:
            if "succeeded_by" in self.drop:
                return []
            kinds: dict[str, int] = {}
            for e in self.layer.succeeded_by:
                kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
            return [{"cik": cik, "kind": kind, "n": n} for kind, n in sorted(kinds.items())]
        if "count(x)" in query:
            return [{"cik": cik, "n": len(items if "RiskItem" in query else self.layer.passages)}]
        if "MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL" in query and "MATCH (c:Company" in query:      # the false-drop guard
            return [{"item_id": r["item_id"], "removed_in": r["removed_in"], "section_id": r["section_id"], "ticker": "ZZZ"}
                    for r in items if r["removed_in"]]
        return []


class TestLoaderParity:
    """The graph's ``removed_in`` / ``is_new`` / SUCCEEDED_BY counts per filer must equal the parquet's: a loader that dropped them FAILS."""

    @staticmethod
    def lake_and_layer(tmp_path):
        from semigraph.graph import items

        settings = lakefix.build_lake(tmp_path)
        items.run_align_items(settings, ["ZZZ"])
        return settings, layer_of_lake(settings)

    @staticmethod
    def run(settings, graph):
        out: list[str] = []
        failures = vg.run_checks(object(), settings, out=out.append)
        return failures, [line for line in out if line.startswith("[FAIL]")], out

    def test_the_faithful_graph_the_loader_would_write_passes_every_python_check(self, tmp_path, monkeypatch):
        settings, layer = self.lake_and_layer(tmp_path)
        assert sum(1 for r in layer.items if r["removed_in"]) == 1 and sum(1 for r in layer.items if r["is_new"]) == 2
        assert len(layer.succeeded_by) == 5
        monkeypatch.setattr(vg.client, "run_cypher", GraphFromLayer(layer))
        failures, fails, out = self.run(settings, None)
        assert failures == 0 and fails == [] and sum(line.startswith("[PASS]") and "counts" in line for line in out) >= 5

    def test_a_loader_that_dropped_every_removed_in_fails_the_run_and_names_the_filer_and_both_counts(self, tmp_path, monkeypatch):
        settings, layer = self.lake_and_layer(tmp_path)
        monkeypatch.setattr(vg.client, "run_cypher", GraphFromLayer(layer, drop=["removed_in"]))
        failures, fails, out = self.run(settings, None)
        assert failures == 1 and len(fails) == 1 and "removed_in" in fails[0]
        row = out[out.index(fails[0]) + 1]
        assert "'what': 'removed_in'" in row and f"'filer_cik': {lakefix.CIK}" in row and "'parquet_rows': 1" in row and "'graph_nodes': 0" in row

    def test_a_loader_that_dropped_every_is_new_flag_fails(self, tmp_path, monkeypatch):
        settings, layer = self.lake_and_layer(tmp_path)
        monkeypatch.setattr(vg.client, "run_cypher", GraphFromLayer(layer, drop=["is_new"]))
        failures, fails, _ = self.run(settings, None)
        assert failures == 1 and "is_new" in fails[0]

    def test_a_loader_that_wrote_no_succeeded_by_edge_fails_per_kind(self, tmp_path, monkeypatch):
        settings, layer = self.lake_and_layer(tmp_path)
        monkeypatch.setattr(vg.client, "run_cypher", GraphFromLayer(layer, drop=["succeeded_by"]))
        bad = vg.check_succeeded_counts(object(), settings)
        assert {r["what"] for r in bad} <= {"SUCCEEDED_BY[unchanged]", "SUCCEEDED_BY[reworded]", "SUCCEEDED_BY[merged]"}
        assert sum(r["parquet_rows"] for r in bad) == 5 and all(r["graph_nodes"] == 0 for r in bad)
        assert self.run(settings, None)[0] == 1

    def test_an_edge_of_the_wrong_kind_is_a_mismatch_even_when_the_total_is_right(self, tmp_path, monkeypatch):
        settings, layer = self.lake_and_layer(tmp_path)
        good = GraphFromLayer(layer)

        def relabelled(driver, query, **params):
            rows = good(driver, query, **params)
            if query != vg.SUCCEEDED_GRAPH_QUERY:
                return rows
            first, *rest = rows                                          # every edge of the first kind reads as another kind
            other = "merged" if first["kind"] != "merged" else "unchanged"
            return [{**first, "kind": other}, *rest]

        monkeypatch.setattr(vg.client, "run_cypher", relabelled)
        bad = vg.check_succeeded_counts(object(), settings)
        assert len(bad) >= 2 and sum(r["graph_nodes"] for r in bad) == sum(r["parquet_rows"] for r in bad)

    def test_extra_graph_removals_the_parquet_does_not_have_fail_too(self, tmp_path, monkeypatch):
        settings, layer = self.lake_and_layer(tmp_path)
        monkeypatch.setattr(vg.client, "run_cypher", GraphFromLayer(layer, extra_removed=2))
        (bad,) = vg.check_removed_counts(object(), settings)
        assert bad == {"what": "removed_in", "filer_cik": lakefix.CIK, "parquet_rows": 1, "graph_nodes": 3}

    def test_without_a_lake_each_parity_check_is_skipped_not_passed(self, tmp_path):
        settings = Settings(data_dir=tmp_path / "nothing", _env_file=None)
        for check in (vg.check_removed_counts, vg.check_new_counts, vg.check_succeeded_counts):
            assert check(object(), settings) is None

    def test_the_graph_side_queries_only_read(self):
        for query in (vg.REMOVED_GRAPH_QUERY, vg.NEW_GRAPH_QUERY, vg.SUCCEEDED_GRAPH_QUERY):
            upper = f" {query.upper()} "
            assert not any(f" {w} " in upper for w in ("CREATE", "MERGE", "DELETE", "SET", "REMOVE")), query
        assert "i.removed_in IS NOT NULL" in vg.REMOVED_GRAPH_QUERY and "i.is_new = true" in vg.NEW_GRAPH_QUERY
        assert "SUCCEEDED_BY" in vg.SUCCEEDED_GRAPH_QUERY and "s.kind" in vg.SUCCEEDED_GRAPH_QUERY


class TestCheckFalseDropsOnTheLake:
    def test_it_reads_the_removed_items_text_and_the_newer_section_from_the_lake(self, tmp_path, monkeypatch):
        settings = lakefix.build_lake(tmp_path)
        removed_acc = lakefix.ACC["25"]
        # the graph claims the TAX item of F24 was removed in F25, but F25's section text still holds its headline
        tax_item = f"{lakefix.ACC['24']}:I.1A:i001"
        graph = [{"item_id": tax_item, "removed_in": removed_acc, "section_id": "I.1A", "ticker": "ZZZ"}]
        monkeypatch.setattr(vg.client, "run_cypher", lambda driver, q, **p: graph)
        (bad,) = vg.check_false_drops(object(), settings)
        assert bad["item"] == tax_item and bad["ratio"] == 100.0
        pandemic = f"{lakefix.ACC['24']}:I.1A:i002"                 # truly gone from F25
        graph[:] = [{**graph[0], "item_id": pandemic}]
        assert vg.check_false_drops(object(), settings) == []

    def test_no_removed_item_means_nothing_to_check(self, tmp_path, monkeypatch):
        settings = lakefix.build_lake(tmp_path)
        monkeypatch.setattr(vg.client, "run_cypher", lambda driver, q, **p: [])
        assert vg.check_false_drops(object(), settings) == []


# --------------------------------------------------------------------------- the check list and the run

REQUIRED = ["chunk_ids", "removed_in only on items of pairs with items_compared", "unsettled_in only on items of pairs with items_compared",
            "no item carries both removed_in and unsettled_in",
            "no SUCCEEDED_BY, removed_in, unsettled_in, is_new or RiskPassage on a pair",
            "exactly one HAS_PASSAGE parent", "boolean items_compared", "no Deleted status remains"]


class TestCheckList:
    def test_every_required_item_invariant_is_a_check_of_the_script(self):
        names = [n for n, _, _ in vg.CHECKS] + [n for n, _ in vg.PYTHON_CHECKS]
        for needle in REQUIRED + ["counts equal the parquet row counts", "false-drop guard",
                                  "unsettled_in counts equal the older-side uncertain decisions",
                                  "removed_in counts equal the older-side removed decisions",
                                  "is_new counts equal the newer-side new decisions",
                                  "SUCCEEDED_BY counts per kind equal the older-side matched decisions"]:
            assert any(needle in n for n in names), needle

    @staticmethod
    def query(prefix):
        (query,) = [q for n, q, _ in vg.CHECKS if n.startswith(prefix)]
        return query

    def test_unsettled_in_is_only_allowed_on_an_older_item_of_a_pair_whose_items_were_compared(self):
        q = self.query("unsettled_in only on items of pairs with items_compared")
        assert "i.unsettled_in IS NOT NULL" in q
        assert "(:Filing {accession_no: i.unsettled_in})-[s:SUPERSEDES]->(:Filing {accession_no: i.accession_no})" in q
        assert "NOT coalesce(s.items_compared, false)" in q                       # no edge, or a not-compared one, is a violation

    def test_an_item_cannot_be_both_removed_and_unsettled(self):
        q = self.query("no item carries both removed_in and unsettled_in")
        assert "i.removed_in IS NOT NULL AND i.unsettled_in IS NOT NULL" in q

    def test_a_pair_that_was_not_compared_may_carry_no_unsettled_item_either(self):
        q = self.query("no SUCCEEDED_BY, removed_in, unsettled_in, is_new or RiskPassage on a pair")
        assert "'unsettled_in' AS what" in q and "unsettled_in: n.accession_no" in q

    def test_the_citable_chunks_check_covers_unsettled_items_of_the_current_pair(self):
        q = self.query("the citable chunk ids of the CURRENT pair")
        assert "i.unsettled_in = cur.accession_no" in q

    def test_the_info_report_counts_the_unsettled_items_per_filing(self):
        (query,) = [q for n, q, k in vg.CHECKS if n == "risk items and changes by filing"]
        assert "count(i.unsettled_in) AS unsettled" in query

    def test_an_annual_edge_between_item_bearing_filings_without_a_boolean_stamp_is_a_violation(self):
        """The loader clears the stamps of edges that are no longer current pairs; an edge left unstamped must fail here (an edge the
        item layer has no opinion on is not 'compared'), so a stale filing edge is reported instead of read as 'nothing changed'."""
        q = self.query("every annual->annual SUPERSEDES between filings that have risk items carries a boolean items_compared")
        assert "s.items_compared IS NULL" in q and "EXISTS { MATCH (:RiskItem {accession_no: n.accession_no}) }" in q
        assert "EXISTS { MATCH (:RiskItem {accession_no: o.accession_no}) }" in q

    def test_the_new_check_names_do_not_collide_with_the_removed_in_only_fragment_the_integration_tests_look_up(self):
        assert [n for n, _, _ in vg.CHECKS if "removed_in only" in n] == ["removed_in only on items of pairs with items_compared = true"]

    def test_check_names_are_unique_and_every_kind_is_none_or_info(self):
        names = [n for n, _, _ in vg.CHECKS]
        assert len(names) == len(set(names)) and {k for _, _, k in vg.CHECKS} == {"none", "info"}

    def test_no_check_query_writes_to_the_graph(self):
        for name, query, _ in vg.CHECKS:
            upper = f" {query.upper()} "
            assert not any(f" {w} " in upper for w in ("CREATE", "MERGE", "DELETE", "SET", "REMOVE")), name

    def test_the_risk_disclosure_status_checks_never_name_the_retired_status_as_allowed(self):
        (query,) = [q for n, q, _ in vg.CHECKS if n.startswith("no Deleted status remains")]
        assert "['Active', 'Historical']" in query and "end_date IS NOT NULL" in query


class FakeGraph:
    """run_cypher stand-in: rows keyed by a substring of the check's query."""

    def __init__(self, failing=()):
        self.failing = failing

    def __call__(self, driver, query, **params):
        return [{"offender": 1}] if any(f in query for f in self.failing) else []


class TestRun:
    def test_a_clean_graph_reports_no_failure_and_skips_the_lake_checks_when_the_lake_is_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vg.client, "run_cypher", FakeGraph())
        out = []
        failures = vg.run_checks(object(), Settings(data_dir=tmp_path / "none", _env_file=None), out=out.append)
        assert failures == 0 and sum(line.startswith("[skip]") for line in out) == len(vg.PYTHON_CHECKS) == 6
        assert all(not line.startswith("[FAIL]") for line in out)

    def test_each_failing_invariant_counts_once_and_prints_its_offending_rows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vg.client, "run_cypher", FakeGraph(failing=("HAS_PASSAGE]->(p)", "d.end_date IS NOT NULL")))
        out = []
        failures = vg.run_checks(object(), Settings(data_dir=tmp_path / "none", _env_file=None), out=out.append)
        assert failures == 2
        fails = [i for i, line in enumerate(out) if line.startswith("[FAIL]")]
        assert len(fails) == 2 and all(out[i + 1].strip() == "{'offender': 1}" for i in fails)

    def test_a_python_check_failure_fails_the_run(self, tmp_path, monkeypatch):
        from semigraph.graph import items

        settings = lakefix.build_lake(tmp_path)
        items.run_align_items(settings, ["ZZZ"])
        monkeypatch.setattr(vg.client, "run_cypher", lambda driver, q, **p: [] if "count(x)" not in q else [])
        out = []
        failures = vg.run_checks(object(), settings, out=out.append)
        fails = [line for line in out if line.startswith("[FAIL]")]
        assert failures == 4 == len(fails)                # an empty graph vs the parquets: item/passage rows, removed_in, is_new, SUCCEEDED_BY
        assert any("row counts" in line for line in fails) and any("removed_in counts" in line for line in fails)
        assert any("is_new counts" in line for line in fails) and any("SUCCEEDED_BY counts" in line for line in fails)
