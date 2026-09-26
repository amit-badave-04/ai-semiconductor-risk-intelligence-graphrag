"""graph/item_loader.py: the graph rows of the item layer (pure) and the order and shape of the Cypher it runs (fake driver)."""

import pandas as pd
import pytest

import lakefix
from semigraph.graph import item_loader as il
from semigraph.graph import items

ZZZ = "ZZZ"
A24, A25, A26 = (lakefix.ACC[y] for y in ("24", "25", "26"))
P1, P2 = f"ZZZ-{A24}-{A25}", f"ZZZ-{A25}-{A26}"


def iid(acc, n):
    return f"{acc}:I.1A:i{n:03d}"


@pytest.fixture
def lake(tmp_path):
    settings = lakefix.build_lake(tmp_path)
    items.run_align_items(settings, [ZZZ])
    return settings


def layer_of(settings, mutate=None):
    frames = list(il._read_ticker(settings, ZZZ))
    if mutate:
        mutate(frames)
    it, pairs, dec, pas = frames
    return il.build_item_layer(ZZZ, it, pairs, dec, pas, {r: r.startswith(A26) for r in it["item_id"]})


# --------------------------------------------------------------------------- lineage ids

class TestLineageIds:
    def rows(self, *ids):
        return [{"item_id": i, "filing_date": d, "accession_no": d, "char_start": n} for n, (i, d) in enumerate(ids)]

    def test_an_item_with_no_predecessor_starts_its_own_lineage_named_after_it(self):
        assert il.lineage_ids("T", self.rows(("a", "2024")), []) == {"a": "lin:T:a"}

    def test_unchanged_and_reworded_successors_carry_the_first_items_id_along_the_chain(self):
        rows = self.rows(("a24", "2024"), ("a25", "2025"), ("a26", "2026"))
        got = il.lineage_ids("T", rows, [("a24", "a25", "reworded"), ("a25", "a26", "unchanged")])
        assert set(got.values()) == {"lin:T:a24"}

    def test_a_merged_or_uncertain_decision_ends_the_chain(self):
        rows = self.rows(("a24", "2024"), ("b24", "2024"), ("m25", "2025"))
        got = il.lineage_ids("T", rows, [("a24", "m25", "unchanged"), ("b24", "m25", "merged")])
        assert got["m25"] == got["a24"] == "lin:T:a24" and got["b24"] == "lin:T:b24"     # b24's lineage ends at the merge
        assert il.lineage_ids("T", rows, [("a24", "m25", "merged")])["m25"] == "lin:T:m25"

    def test_several_chain_predecessors_pick_the_smallest_date_then_id_whatever_the_edge_order(self):
        rows = self.rows(("z23", "2023"), ("a24", "2024"), ("n25", "2025"))
        edges = [("a24", "n25", "reworded"), ("z23", "n25", "reworded")]
        assert il.lineage_ids("T", rows, edges)["n25"] == "lin:T:z23"
        assert il.lineage_ids("T", rows, edges[::-1]) == il.lineage_ids("T", rows, edges)

    def test_the_result_does_not_depend_on_the_order_the_items_are_given_in(self):
        rows = self.rows(("a24", "2024"), ("a25", "2025"), ("a26", "2026"))
        edges = [("a24", "a25", "reworded"), ("a25", "a26", "unchanged")]
        assert il.lineage_ids("T", rows[::-1], edges) == il.lineage_ids("T", rows, edges)


# --------------------------------------------------------------------------- the rows

class TestItemRows:
    def test_every_item_of_the_parquet_becomes_one_node_row_with_the_contract_properties(self, lake):
        layer = layer_of(lake)
        assert len(layer.items) == 10
        row = next(r for r in layer.items if r["item_id"] == iid(A24, 0))
        assert set(row) == {"item_id", "accession_no", "filer_cik", "filing_date", "section_id", "seq", "headline", "text_hash",
                            "char_start", "char_end", "unit_kind", "chunk_ids", "is_current", "lineage_id", "removed_in", "unsettled_in",
                            "is_new"}
        assert (row["filer_cik"], row["filing_date"], row["unit_kind"]) == (lakefix.CIK, "2024-02-21", "headline")
        assert row["chunk_ids"] == [f"{A24}:I.1A:0000"] and row["removed_in"] is None and row["is_new"] is False
        assert row["unsettled_in"] is None

    def test_removed_in_is_the_newer_accession_and_only_on_the_older_item_of_a_compared_pair(self, lake):
        removed = {r["item_id"]: r["removed_in"] for r in layer_of(lake).items if r["removed_in"]}
        assert removed == {iid(A24, 2): A25}

    def test_is_new_marks_only_newer_items_labelled_new_and_never_an_item_of_the_first_filing(self, lake):
        new = {r["item_id"] for r in layer_of(lake).items if r["is_new"]}
        assert new == {iid(A25, 2), iid(A26, 3)}

    def test_is_current_comes_from_the_current_mapping(self, lake):
        assert {r["accession_no"] for r in layer_of(lake).items if r["is_current"]} == {A26}

    def test_lineages_follow_the_verified_successors(self, lake):
        lineage = {r["item_id"]: r["lineage_id"] for r in layer_of(lake).items}
        assert lineage[iid(A24, 0)] == lineage[iid(A25, 0)] == lineage[iid(A26, 0)] == f"lin:ZZZ:{iid(A24, 0)}"
        assert lineage[iid(A24, 2)] == f"lin:ZZZ:{iid(A24, 2)}"                 # the removed pandemic item: a lineage of one
        assert lineage[iid(A25, 2)] == lineage[iid(A26, 2)] and lineage[iid(A26, 3)] == f"lin:ZZZ:{iid(A26, 3)}"

    def test_every_value_is_a_plain_python_value_the_driver_can_send(self, lake):
        layer = layer_of(lake)
        for rows in (layer.items, layer.passages, layer.succeeded_by, layer.spans, layer.in_section, layer.supersedes):
            for row in rows:
                for key, value in row.items():
                    assert type(value) in (str, int, float, bool, list, type(None)), (key, type(value))
                    if isinstance(value, float):
                        assert value == value                                   # NaN is not null
                    if isinstance(value, list):
                        assert all(type(c) is str for c in value)

    def test_a_paragraph_unit_keeps_an_empty_headline_never_null(self, lake):
        def paragraphs(frames):
            frames[0]["headline"] = None
            frames[0]["unit_kind"] = "paragraph"

        assert {r["headline"] for r in layer_of(lake, paragraphs).items} == {""}


class TestUnsettled:
    """``unsettled_in`` = the newer accession, on an older item the text check could not settle (label ``uncertain``) in a
    COMPARED pair: a separate category from ``removed_in``, never both, never on the newer side, never on a pair not compared."""

    @staticmethod
    def label(frames, item_id, side, label, matched="keep"):
        dec = frames[2]
        hit = (dec["item_id"] == item_id) & (dec["side"] == side)
        assert hit.sum() == 1
        dec.loc[hit, "label"] = label
        if matched != "keep":
            dec.loc[hit, "matched_item_id"] = matched

    def unsettled(self, layer):
        return {r["item_id"]: r["unsettled_in"] for r in layer.items if r["unsettled_in"]}

    def test_a_lake_with_no_uncertain_older_item_has_no_unsettled_item(self, lake):
        assert self.unsettled(layer_of(lake)) == {}

    def test_an_older_item_labelled_uncertain_gets_the_newer_accession_of_its_own_pair(self, lake):
        def mutate(frames):
            self.label(frames, iid(A24, 1), "older", "uncertain")
            self.label(frames, iid(A25, 0), "older", "uncertain")           # the older side of the SECOND pair

        assert self.unsettled(layer_of(lake, mutate)) == {iid(A24, 1): A25, iid(A25, 0): A26}

    def test_the_aligners_candidate_does_not_matter_and_an_uncertain_item_still_has_no_successor_edge(self, lake):
        def mutate(frames):
            self.label(frames, iid(A24, 1), "older", "uncertain", matched=iid(A25, 1))       # a candidate: still unsettled

        layer = layer_of(lake, mutate)
        assert self.unsettled(layer) == {iid(A24, 1): A25}
        assert not any(e["old"] == iid(A24, 1) for e in layer.succeeded_by)

    def test_an_item_is_removed_or_unsettled_never_both_and_the_removed_ones_are_unchanged(self, lake):
        def mutate(frames):
            self.label(frames, iid(A24, 1), "older", "uncertain")

        layer = layer_of(lake, mutate)
        removed = {r["item_id"] for r in layer.items if r["removed_in"]}
        assert removed == {iid(A24, 2)} and removed.isdisjoint(self.unsettled(layer))
        assert not any(r["removed_in"] and r["unsettled_in"] for r in layer.items)

    def test_an_uncertain_newer_item_is_not_unsettled_and_neither_is_a_removed_or_merged_older_one(self, lake):
        def mutate(frames):
            self.label(frames, iid(A25, 2), "newer", "uncertain")            # the newer side of pair 1
            self.label(frames, iid(A24, 0), "older", "merged")

        assert self.unsettled(layer_of(lake, mutate)) == {}

    def test_an_unsettled_item_is_present_for_the_lineage_but_ends_its_chain(self, lake):
        def mutate(frames):
            self.label(frames, iid(A24, 1), "older", "uncertain")

        lineage = {r["item_id"]: r["lineage_id"] for r in layer_of(lake, mutate).items}
        assert lineage[iid(A24, 1)] == f"lin:ZZZ:{iid(A24, 1)}"
        assert lineage[iid(A25, 1)] == f"lin:ZZZ:{iid(A25, 1)}"              # the newer tax item starts its own lineage

    def test_a_pair_that_was_not_compared_carries_no_unsettled_item(self, tmp_path):
        settings = lakefix.build_lake(tmp_path, quality={"25": {"low_coverage": True, "coverage": 0.71}})
        items.run_align_items(settings, ["ZZZ"])
        assert not any(r["unsettled_in"] for r in layer_of(settings).items)

    def test_decisions_of_a_pair_that_was_not_compared_are_refused_so_no_unsettled_flag_can_come_from_one(self, lake):
        def mutate(frames):
            self.label(frames, iid(A24, 1), "older", "uncertain")
            frames[1].loc[0, "comparable"] = False

        with pytest.raises(items.AlignItemsError, match="not compared"):
            layer_of(lake, mutate)


class TestSucceededBy:
    def test_one_edge_per_older_item_with_a_counterpart_carrying_kind_decider_and_scores(self, lake):
        edges = {(e["old"], e["new"]): e for e in layer_of(lake).succeeded_by}
        assert len(edges) == 5
        reworded = edges[(iid(A24, 0), iid(A25, 0))]
        assert reworded["kind"] == "reworded" and reworded["decided_by"] == "headline"
        assert reworded["sim_embed"] is None and isinstance(reworded["sim_lex"], float)
        assert edges[(iid(A24, 1), iid(A25, 1))]["kind"] == "unchanged"
        assert not any(e["old"] == iid(A24, 2) for e in edges.values())          # removed: no successor

    def test_an_uncertain_item_is_present_but_carries_no_edge_and_is_never_removed(self, lake):
        def uncertain(frames):
            dec = frames[2]
            hit = (dec["item_id"] == iid(A24, 1)) & (dec["side"] == "older")
            dec.loc[hit, "label"] = "uncertain"

        layer = layer_of(lake, uncertain)
        assert not any(e["old"] == iid(A24, 1) for e in layer.succeeded_by)
        assert {r["item_id"] for r in layer.items if r["removed_in"]} == {iid(A24, 2)}

    def test_a_merged_item_gets_an_edge_of_kind_merged_only_when_its_absorbing_item_is_known(self, lake):
        def merged(frames):
            dec = frames[2]
            hit = (dec["item_id"] == iid(A24, 1)) & (dec["side"] == "older")
            dec.loc[hit, "label"] = "merged"
            gone = (dec["item_id"] == iid(A24, 0)) & (dec["side"] == "older")
            dec.loc[gone, ["label", "matched_item_id"]] = ["merged", None]

        edges = {e["old"]: e["kind"] for e in layer_of(lake, merged).succeeded_by}
        assert edges[iid(A24, 1)] == "merged" and iid(A24, 0) not in edges


class TestPassagesAndSupersedes:
    def test_passages_keep_their_contract_properties_and_chunk_lists(self, lake):
        (removed,) = [p for p in layer_of(lake).passages if p["kind"] == "removed"]
        assert set(removed) == {"passage_id", "kind", "item_id", "older_accession", "newer_accession", "filer_cik", "text",
                                "counterpart_text", "similarity", "char_start", "char_end", "chunk_ids", "counterpart_chunk_ids",
                                "chunk_fallback", "decided_by"}
        assert removed["item_id"] == iid(A24, 0) and removed["chunk_ids"] == [f"{A24}:I.1A:0000"]
        assert removed["counterpart_text"] is None and removed["similarity"] is None and removed["counterpart_chunk_ids"] == []

    def test_every_consecutive_pair_gets_a_supersedes_row_with_the_comparison_flag(self, lake):
        rows = layer_of(lake).supersedes
        assert rows == [{"newer": A25, "older": A24, "items_compared": True, "reason": None},
                        {"newer": A26, "older": A25, "items_compared": True, "reason": None}]

    def test_a_pair_that_was_not_compared_carries_the_reason_and_nothing_else(self, tmp_path):
        settings = lakefix.build_lake(tmp_path, quality={"25": {"low_coverage": True, "coverage": 0.71}})
        items.run_align_items(settings, [ZZZ])
        layer = layer_of(settings)
        assert [(r["items_compared"], "71.0%" in r["reason"]) for r in layer.supersedes] == [(False, True), (False, True)]
        assert layer.succeeded_by == [] and layer.passages == []
        assert not any(r["removed_in"] or r["is_new"] for r in layer.items)
        assert len({r["lineage_id"] for r in layer.items}) == len(layer.items)      # no verified successor: every item its own lineage

    def test_spans_and_sections_are_derived_from_the_items(self, lake):
        layer = layer_of(lake)
        assert {"item_id": iid(A24, 0), "chunk_id": f"{A24}:I.1A:0000"} in layer.spans
        assert {"item_id": iid(A24, 0), "section_key": f"{A24}:I.1A"} in layer.in_section
        assert len(layer.in_section) == 10 and len(layer.spans) == 10


class TestStaleAlignment:
    def test_decisions_naming_an_unknown_item_mean_the_alignment_predates_the_items(self, lake):
        def stale(frames):
            frames[2].loc[0, "item_id"] = "gone:I.1A:i000"

        with pytest.raises(items.AlignItemsError, match="run `semigraph align-items` first"):
            layer_of(lake, stale)

    def test_a_pair_between_filings_the_items_no_longer_have_is_stale(self, lake):
        def stale(frames):
            frames[1].loc[0, "older_accession"] = "0000000-00-000000"

        with pytest.raises(items.AlignItemsError, match="unknown filings"):
            layer_of(lake, stale)

    def test_decisions_of_a_pair_that_was_not_compared_are_refused(self, lake):
        def stale(frames):
            frames[1].loc[0, "comparable"] = False

        with pytest.raises(items.AlignItemsError, match="not compared"):
            layer_of(lake, stale)


class TestOfItem:
    def test_a_risk_links_to_every_item_whose_chunks_contain_its_evidence_chunk(self):
        rows = [{"item_id": "i1", "chunk_ids": ["c1", "c2"]}, {"item_id": "i2", "chunk_ids": ["c2"]}]
        evidence = [{"risk_id": "r1", "chunk_id": "c2"}, {"risk_id": "r2", "chunk_id": "c1"}, {"risk_id": "r3", "chunk_id": "c9"}]
        assert il.of_item_rows(evidence, rows) == [{"risk_id": "r1", "item_id": "i1"}, {"risk_id": "r1", "item_id": "i2"},
                                                   {"risk_id": "r2", "item_id": "i1"}]


# --------------------------------------------------------------------------- the Cypher, with a fake driver

class Rows(list):
    """What ``session.run`` returns: iterable rows with ``consume()``."""

    def consume(self):
        return None


class Tx:
    """A managed transaction: every statement it runs is logged on the driver and on the transaction itself."""

    def __init__(self, driver):
        self.driver, self.statements, self.state = driver, [], "open"

    def run(self, query, parameters=None, **params):
        text = " ".join(query.split())
        self.driver.log.append((text, params))
        self.statements.append((text, params))
        drv = self.driver
        if drv.fail_on and drv.fail_on in text and drv.fail_tx in (None, drv.transactions.index(self)):
            raise RuntimeError(f"injected failure at: {drv.fail_on}")
        return Rows(drv.reply(text, params))


class Session:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, parameters=None, **params):        # an auto-commit statement: outside any transaction
        text = " ".join(query.split())
        self.driver.log.append((text, params))
        self.driver.autocommit.append(text)
        return Rows(self.driver.reply(text, params))

    def execute_write(self, work, *args, **kwargs):
        tx = Tx(self.driver)
        self.driver.transactions.append(tx)
        try:
            result = work(tx, *args, **kwargs)
        except BaseException:
            tx.state = "rolled back"
            raise
        tx.state = "committed"
        return result


class FakeDriver:
    def __init__(self, missing_supersedes=(), risk_evidence=(), fail_on=None, fail_tx=None):
        self.log, self.missing, self.evidence = [], set(missing_supersedes), list(risk_evidence)
        self.transactions, self.autocommit, self.fail_on, self.fail_tx = [], [], fail_on, fail_tx

    def session(self, **config):
        return Session(self)

    def reply(self, text, params):
        if "SET s.items_compared" in text:
            return [{"newer": r["newer"], "older": r["older"]} for r in params["rows"] if (r["newer"], r["older"]) not in self.missing]
        if text.startswith("MATCH (:RiskItem {filer_cik: $cik})-[r:SPANS]->() RETURN count(r)"):
            return [{"n": 7}]
        if "RETURN rf.risk_id AS risk_id" in text:
            return self.evidence
        if "IN $ciks" in text and "DETACH DELETE" in text:
            return [{"n": 4 if "RiskItem" in text else 2}]
        return []

    def statements(self):
        return [t for t, _ in self.log]


@pytest.fixture
def loader_env(lake, monkeypatch):
    monkeypatch.setattr(il, "_load_manifest", lambda settings: {})
    monkeypatch.setattr(il, "_current_by_item", lambda settings, manifest, ticker, items_df: {r: r.startswith(A26) for r in items_df["item_id"]})
    return lake


def position(statements, needle):
    return next(i for i, s in enumerate(statements) if needle in s)


class TestLoadRiskItems:
    def test_it_replaces_the_layer_first_then_writes_nodes_edges_passages_and_the_supersedes_flags(self, loader_env):
        driver = FakeDriver()
        totals = il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")
        st = driver.statements()
        clear = [i for i, s in enumerate(st) if "DELETE" in s]
        assert clear == sorted(clear) and max(clear) < position(st, "MERGE (i:RiskItem")
        assert position(st, "MERGE (i:RiskItem") < position(st, "MERGE (i)-[r:IN_SECTION]") < position(st, "MERGE (i)-[r:SPANS]") < position(st, "MERGE (o)-[s:SUCCEEDED_BY]")
        assert position(st, "MERGE (o)-[s:SUCCEEDED_BY]") < position(st, "MERGE (p:RiskPassage") < position(st, "SET s.items_compared")
        assert totals["items"] == 10 and totals["succeeded_by"] == 5 and totals["passages"] == 2 and totals["supersedes"] == 2
        assert totals["spans"] == 7 and totals["spans_without_evidence_node"] == 3

    def test_stale_derived_data_of_a_previous_run_is_removed_not_merged_over(self, loader_env):
        driver = FakeDriver()
        il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")
        deletes = [(t, p) for t, p in driver.log if "DELETE" in t]
        assert any("RiskPassage" in t and "DETACH DELETE p" in t for t, _ in deletes)
        assert any("SUCCEEDED_BY|IN_SECTION|SPANS" in t for t, _ in deletes) and any("OF_ITEM" in t for t, _ in deletes)
        gone = next(p for t, p in deletes if "NOT i.item_id IN $keep" in t)
        assert gone["cik"] == lakefix.CIK and len(gone["keep"]) == 10

    def test_the_item_write_always_sets_unsettled_in_so_a_stale_flag_of_a_previous_run_is_cleared_with_null(self, loader_env):
        driver = FakeDriver()
        il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")
        text, params = next((t, p) for t, p in driver.log if "MERGE (i:RiskItem" in t)
        assert "i.unsettled_in = row.unsettled_in" in text and "i.removed_in = row.removed_in" in text
        assert all("unsettled_in" in row and row["unsettled_in"] is None for row in params["rows"])

    def test_the_totals_count_the_unsettled_items(self, loader_env, monkeypatch):
        driver = FakeDriver()
        assert il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")["unsettled"] == 0
        real = il._read_ticker

        def with_one_uncertain(settings, ticker, directory=None):
            frames = list(real(settings, ticker, directory))
            dec = frames[2]
            dec.loc[(dec["item_id"] == iid(A24, 1)) & (dec["side"] == "older"), "label"] = "uncertain"
            return tuple(frames)

        monkeypatch.setattr(il, "_read_ticker", with_one_uncertain)
        driver = FakeDriver()
        assert il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")["unsettled"] == 1
        text, params = next((t, p) for t, p in driver.log if "MERGE (i:RiskItem" in t)
        assert [r["unsettled_in"] for r in params["rows"] if r["item_id"] == iid(A24, 1)] == [A25]

    def test_the_supersedes_write_matches_the_existing_edge_and_never_creates_one_or_touches_its_kind(self, loader_env):
        driver = FakeDriver()
        il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")
        text = next(t for t in driver.statements() if "SET s.items_compared" in t)
        assert "MATCH (n:Filing" in text and "MERGE" not in text and "s.kind" not in text

    def test_a_pair_with_no_supersedes_edge_stops_the_load_loudly(self, loader_env):
        driver = FakeDriver(missing_supersedes=[(A26, A25)])
        with pytest.raises(items.AlignItemsError, match="no SUPERSEDES edge between the filings of 1 pair"):
            il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")

    def test_every_write_carries_the_snapshot_id_and_risk_factors_are_linked_to_their_items(self, loader_env):
        driver = FakeDriver(risk_evidence=[{"risk_id": "r1", "chunk_id": f"{A26}:I.1A:0003"}])
        totals = il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")
        writes = [(t, p) for t, p in driver.log if "$snapshot_id" in t]
        assert writes and all(p["snapshot_id"] == "snap-x" for _, p in writes)
        of_item = next(p for t, p in driver.log if "OF_ITEM" in t and "MERGE" in t)
        assert of_item["rows"] == [{"risk_id": "r1", "item_id": iid(A26, 3)}] and totals["of_item"] == 1

    def test_missing_alignment_files_stop_it_before_any_write(self, tmp_path):
        settings = lakefix.build_lake(tmp_path)
        driver = FakeDriver()
        with pytest.raises(items.AlignItemsError, match="run `semigraph align-items` first"):
            il.load_risk_items(driver, settings, [ZZZ], snapshot_id="snap-x")
        assert driver.log == []

    def test_a_stale_alignment_stops_it_before_any_write(self, loader_env):
        pairs_path = items.table_path(items.alignment_dir(loader_env), ZZZ, "pairs")
        frame = pd.read_parquet(pairs_path)
        frame.loc[0, "older_accession"] = "0000000-00-000000"
        frame.to_parquet(pairs_path, index=False)
        driver = FakeDriver()
        with pytest.raises(items.AlignItemsError, match="unknown filings"):
            il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")
        assert driver.log == []


# --------------------------------------------------------------------------- one transaction per ticker, stale stamps, orphans

class TestAtomicLoad:
    """A ticker's load is ONE transaction: the clear, every write, the SUPERSEDES stamps and the OF_ITEM links commit together or not
    at all (a crash after the clear used to leave a half-loaded layer stamped with the new snapshot)."""

    def test_one_ticker_is_one_transaction_holding_the_clear_and_every_write_and_it_commits(self, loader_env):
        driver = FakeDriver(risk_evidence=[{"risk_id": "r1", "chunk_id": f"{A26}:I.1A:0003"}])
        il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")
        (tx,) = driver.transactions
        assert tx.state == "committed" and driver.autocommit == [] and len(tx.statements) == len(driver.log)
        texts = [t for t, _ in tx.statements]
        for needle in ("DETACH DELETE p", "MERGE (i:RiskItem", "MERGE (o)-[s:SUCCEEDED_BY]", "MERGE (p:RiskPassage", "SET s.items_compared"):
            assert any(needle in t for t in texts), needle
        # the SPANS count and the RiskFactor evidence read must see the transaction's own uncommitted writes
        assert any("RETURN count(r)" in t for t in texts) and any("RETURN rf.risk_id" in t for t in texts)
        assert any("OF_ITEM" in t and "MERGE" in t for t in texts)

    def test_a_failure_after_the_clear_rolls_the_ticker_back_and_nothing_commits(self, loader_env):
        driver = FakeDriver(fail_on="MERGE (p:RiskPassage")
        with pytest.raises(RuntimeError, match="injected"):
            il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")
        (tx,) = driver.transactions
        assert tx.state == "rolled back" and driver.autocommit == []
        assert any("DELETE" in t for t, _ in tx.statements)                # the clear ran INSIDE the transaction that was rolled back

    def test_a_missing_supersedes_edge_rolls_the_whole_ticker_back(self, loader_env):
        driver = FakeDriver(missing_supersedes=[(A26, A25)])
        with pytest.raises(items.AlignItemsError, match="no SUPERSEDES edge"):
            il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")
        (tx,) = driver.transactions
        assert tx.state == "rolled back" and driver.autocommit == []

    def test_the_writes_never_leave_the_transaction_through_the_helpers_that_open_their_own_session(self):
        import inspect

        source = inspect.getsource(il)
        assert "_run_batched(" not in source and "run_cypher(" not in source and "_clear_layer" not in source

    @pytest.fixture
    def two(self, tmp_path, monkeypatch):
        settings = lakefix.build_lake(tmp_path)
        lakefix.build_lake(tmp_path, ticker="YYY", cik=1000)
        items.run_align_items(settings, ["YYY", ZZZ])
        monkeypatch.setattr(il, "_load_manifest", lambda s: {})
        monkeypatch.setattr(il, "_current_by_item", lambda s, m, t, df: {r: False for r in df["item_id"]})
        return settings

    def test_each_ticker_has_its_own_transaction_and_a_failure_in_the_second_leaves_the_first_committed(self, two):
        driver = FakeDriver(fail_on="MERGE (p:RiskPassage", fail_tx=1)
        with pytest.raises(RuntimeError, match="injected"):
            il.load_risk_items(driver, two, ["YYY", ZZZ], snapshot_id="snap-x")
        assert [tx.state for tx in driver.transactions] == ["committed", "rolled back"]

    def test_the_stale_comparison_stamps_of_the_tickers_filing_edges_are_removed_before_the_current_pairs_are_stamped(self, loader_env):
        driver = FakeDriver()
        il.load_risk_items(driver, loader_env, [ZZZ], snapshot_id="snap-x")
        st = driver.statements()
        clear = position(st, "REMOVE s.items_compared, s.not_compared_reason")
        assert clear < position(st, "SET s.items_compared")
        assert "(:Company {cik: $cik})-[:FILED]->(:Filing)-[s:SUPERSEDES]->(:Filing)" in st[clear]
        assert "s.kind" not in st[clear] and "MERGE" not in st[clear] and "DELETE" not in st[clear]

    def test_a_full_load_deletes_the_item_layer_of_a_ticker_whose_risk_item_file_is_gone_in_its_own_transaction_after_the_loads(self, two):
        driver = FakeDriver()
        totals = il.load_risk_items(driver, two, None, snapshot_id="snap-x")
        assert [tx.state for tx in driver.transactions] == ["committed"] * 3
        purge = driver.transactions[-1].statements
        assert len(purge) == 3 and all("$ciks" in t or "NOT c.cik" in t for t, _ in purge)
        assert all(params["ciks"] == [lakefix.CIK, 1000] for _, params in purge)
        assert totals["purged_items"] == 4 and totals["purged_passages"] == 2
        deleting = [t for t, _ in purge if "DETACH DELETE" in t]
        assert any("RiskPassage" in t for t in deleting) and any("RiskItem" in t for t in deleting)
        assert all("filer_cik IS NULL OR NOT" in t for t in deleting)                 # a node with no filer is nobody's layer either

    def test_a_load_of_named_tickers_never_deletes_another_tickers_layer(self, two):
        driver = FakeDriver()
        totals = il.load_risk_items(driver, two, [ZZZ], snapshot_id="snap-x")
        assert len(driver.transactions) == 1 and not any("$ciks" in t for t, _ in driver.log)
        assert totals["purged_items"] == totals["purged_passages"] == 0

    def test_pruning_can_be_forced_or_forbidden_explicitly(self, two):
        forced = FakeDriver()
        il.load_risk_items(forced, two, [ZZZ], snapshot_id="snap-x", prune_missing=True)
        assert any("$ciks" in t for t, _ in forced.log)
        forbidden = FakeDriver()
        il.load_risk_items(forbidden, two, None, snapshot_id="snap-x", prune_missing=False)
        assert not any("$ciks" in t for t, _ in forbidden.log) and len(forbidden.transactions) == 2

    def test_a_failed_ticker_stops_the_load_before_anything_is_pruned(self, two):
        driver = FakeDriver(fail_on="MERGE (p:RiskPassage", fail_tx=1)
        with pytest.raises(RuntimeError):
            il.load_risk_items(driver, two, None, snapshot_id="snap-x")
        assert not any("$ciks" in t for t, _ in driver.log)
