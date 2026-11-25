"""The retrieval fingerprint/diff used by the M1 before/after gate (pure functions)."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_retrieval.py"
spec = importlib.util.spec_from_file_location("compare_retrieval", SCRIPT)
cr = importlib.util.module_from_spec(spec)
sys.modules["compare_retrieval"] = cr
spec.loader.exec_module(cr)


def retrieval(**over):
    base = {
        "anchors": {"Nvidia": 1045810},
        "edges": [{"source": "Nvidia", "relation": "DEPENDS_ON", "target": "TSMC"}],
        "metrics": [{"company": "Nvidia", "metric": "revenue", "period_end": "2026-01-25", "value": 1.0, "unit": "USD"}],
        "risks": [{"chunk_id": "c1"}], "temporal": [{"company": "Nvidia", "lineage": "1045810:3"}],
        "chunks": [{"chunk_id": "c2"}, {"chunk_id": "c3"}],
    }
    return {**base, **over}


def test_summary_is_order_independent_and_json_safe():
    a = cr.summarize(retrieval(chunks=[{"chunk_id": "c2"}, {"chunk_id": "c3"}]))
    b = cr.summarize(retrieval(chunks=[{"chunk_id": "c3"}, {"chunk_id": "c2"}]))
    assert a == b


def test_metrics_without_a_unit_are_treated_as_usd():
    m = {"company": "Nvidia", "metric": "revenue", "period_end": "2026-01-25", "value": 1.0}
    assert cr.summarize(retrieval(metrics=[m]))["metrics"] == cr.summarize(retrieval())["metrics"]


def test_identical_retrievals_have_an_empty_diff():
    s = cr.summarize(retrieval())
    d = cr.diff_summaries(s, s)
    assert all(not d[layer]["lost"] and not d[layer]["gained"] for layer in cr.LAYERS)
    assert d["anchors_changed"] is False


def test_diff_reports_lost_and_gained_items_per_layer():
    before = cr.summarize(retrieval())
    after = cr.summarize(retrieval(chunks=[{"chunk_id": "c3"}, {"chunk_id": "c9"}]))
    d = cr.diff_summaries(before, after)
    assert d["chunks"] == {"before": 2, "after": 2, "lost": ["c2"], "gained": ["c9"]}
    assert not d["edges"]["lost"]


def test_unit_change_is_visible_as_a_metric_difference():
    usd = cr.summarize(retrieval())
    twd = cr.summarize(retrieval(metrics=[{"company": "Nvidia", "metric": "revenue", "period_end": "2026-01-25",
                                           "value": 1.0, "unit": "TWD"}]))
    d = cr.diff_summaries(usd, twd)
    assert len(d["metrics"]["lost"]) == 1 and len(d["metrics"]["gained"]) == 1


def test_diff_snapshots_only_compares_questions_present_in_both():
    s = cr.summarize(retrieval())
    out = cr.diff_snapshots({"N1": s, "old-only": s}, {"N1": s, "new-only": s})
    assert list(out) == ["N1"]


def test_render_marks_identical_and_changed_questions():
    s = cr.summarize(retrieval())
    changed = cr.summarize(retrieval(chunks=[{"chunk_id": "cX"}]))
    text = cr.render_diff(cr.diff_snapshots({"N1": s, "N2": s}, {"N1": s, "N2": changed}))
    assert "N1: identical" in text and "N2: CHANGED chunks" in text and "lost: c2" in text and "gained: cX" in text


@pytest.mark.parametrize("n", [1, 3])
def test_render_truncates_long_lists(n):
    s = cr.summarize(retrieval(chunks=[]))
    many = cr.summarize(retrieval(chunks=[{"chunk_id": f"c{i}"} for i in range(10)]))
    text = cr.render_diff(cr.diff_snapshots({"Q": s}, {"Q": many}), limit=n)
    assert f"and {10 - n} more" in text


# --- M2a I0 (docs/specs/m2a-answer-integrity.md section 8): the $0 measurement helpers -----------------------------------

def test_edge_rows_keep_retrieval_order_ids_and_quote_length():
    r = retrieval(edges=[
        {"source": "Nvidia", "relation": "DEPENDS_ON", "target": "TSMC", "status": "Active", "quote": "abc",
         "chunk_ids": ["c1", "c2"]},
        {"source": "Nvidia", "relation": "AFFECTED_BY", "target": "Rule", "status": "Active", "quote": None,
         "chunk_ids": ["c3"], "rule_id": "2026-00789"},
    ])
    rows = cr.edge_rows(r)
    assert [row["rank"] for row in rows] == [1, 2]
    assert rows[0] == {"rank": 1, "edge": "Nvidia -DEPENDS_ON-> TSMC", "relation": "DEPENDS_ON", "status": "Active",
                       "chunk_ids": ["c1", "c2"], "quote_chars": 3, "rule_id": None}
    assert rows[1]["quote_chars"] == 0 and rows[1]["rule_id"] == "2026-00789"


def test_chunk_ranks_keep_order_and_scores():
    r = retrieval(chunks=[{"chunk_id": "c2", "score": 0.9}, {"chunk_id": "c3"}])
    assert cr.chunk_ranks(r) == [{"rank": 1, "chunk_id": "c2", "score": 0.9},
                                 {"rank": 2, "chunk_id": "c3", "score": None}]


def stored(**over):
    return {"id": "Q", "cited": ["a", "b", "zz"], "hallucinated": ["zz"], **over}


def test_a_replay_is_accepted_when_every_non_hallucinated_cited_id_is_in_the_replayed_context():
    assert cr.replay_check(stored(), {"a", "b", "c"}) == {"accepted": True, "missing": [], "error": False}


def test_a_replay_is_rejected_and_names_the_ids_it_lacks():
    assert cr.replay_check(stored(), {"a"}) == {"accepted": False, "missing": ["b"], "error": False}


def test_an_errored_stored_row_is_flagged_not_silently_accepted():
    out = cr.replay_check(stored(cited=[], hallucinated=[], error="timeout"), set())
    assert out == {"accepted": True, "missing": [], "error": True}


def test_distribution_uses_nearest_rank_and_handles_empty():
    assert cr.distribution([]) == {"n": 0}
    assert cr.distribution([5, 1, 3, 2, 4]) == {"n": 5, "min": 1, "p50": 3, "p90": 5, "max": 5}
    assert cr.distribution(range(1, 11))["p90"] == 9


def test_block_sizes_count_characters_per_named_block():
    from semigraph.retrieval.answerer import ContextBlocks

    blocks = ContextBlocks("ab", "", "abcd", "x", "yy", "zzz")
    assert cr.block_sizes(blocks) == {"edges_block": 2, "external_block": 0, "metrics_block": 4, "risks_block": 1,
                                      "temporal_block": 2, "chunks_block": 3, "total": 12}


def test_metric_chars_are_grouped_under_each_company_header():
    block = ("Qualcomm: fiscal year ended 2023-09-24\n- net_income ... [xbrl:1]\n"
             "Qualcomm: fiscal year ended 2022-09-25\n- net_income ... [xbrl:2]\n"
             "Apple: fiscal year ended 2025-09-27\n- revenue ... [xbrl:3]")
    out = cr.metric_chars_by_company(block)
    assert set(out) == {"Qualcomm", "Apple"}
    assert out["Apple"] == len("Apple: fiscal year ended 2025-09-27") + len("- revenue ... [xbrl:3]")


def test_edge_chars_count_each_line_once_per_anchor_it_touches():
    block = "- Nvidia DEPENDS_ON TSMC (status=Active) [c1]\n- AMD COMPETES_WITH Intel (status=Active) [c2]"
    out = cr.edge_chars_by_anchor(block, ["Nvidia", "Intel"])
    first, second = block.splitlines()
    assert out == {"Nvidia": len(first), "Intel": len(second)}


def test_ids_shown_reports_which_ids_appear_bracketed_in_the_context():
    ctx = "- revenue ... [xbrl:1:revenue:2022-12-31] | computed"
    assert cr.ids_shown(ctx, ["xbrl:1:revenue:2022-12-31", "xbrl:1:revenue:2021-12-31"]) == {
        "xbrl:1:revenue:2022-12-31": True, "xbrl:1:revenue:2021-12-31": False}


@pytest.mark.parametrize("source", ["SK HYNIX INC", "Taiwan Semiconductor (TSMC)", "Hon Hai CUSTOMER Co"])
def test_edge_chars_parse_multi_word_all_caps_and_bracketed_names(source):
    line = f"- {source} SUPPLIES_TO Nvidia (status=Active) [c1]"
    assert cr.edge_chars_by_anchor(line, [source, "Nvidia"]) == {source: len(line), "Nvidia": len(line)}


def test_replay_summary_keeps_errored_rows_out_of_the_accepted_count_and_names_unpaired_ones():
    dump = {"questions": {"A": {"valid_ids": ["a"]}, "B": {"valid_ids": []}, "D": {"valid_ids": []}}}
    runs = [{"id": "A", "cited": ["a"], "hallucinated": []},
            {"id": "B", "cited": [], "hallucinated": [], "error": "timeout"},
            {"id": "C", "cited": ["x"], "hallucinated": []}]
    out = cr._replay_summary(dump, runs)
    assert out["pairs"] == 2 and out["accepted"] == 1 and out["rejected"] == {}
    assert out["errored_rows"] == ["B"]
    assert out["unpaired"] == {"stored_only": ["C"], "replay_only": ["D"]}


@pytest.mark.parametrize("uri", ["bolt://localhost:7699", "neo4j://127.0.0.1:7699", "bolt://[::1]:7687"])
def test_only_a_local_graph_is_measured(uri):
    assert cr.local_graph(uri) == uri


@pytest.mark.parametrize("uri", ["bolt://semigraph-neo4j.internal:7687", "neo4j+s://x.databases.neo4j.io",
                                 "bolt://user:pw@localhost.evil.com:7687", "bolt://neo4j:secret@localhost:7699"])
def test_a_remote_graph_or_credentials_in_the_uri_are_refused(uri):
    with pytest.raises(SystemExit):
        cr.local_graph(uri)


def test_the_dump_writes_only_under_data_backups():
    root = cr.ROOT
    assert cr.backups_path(root / "data" / "backups" / "x.json") == (root / "data" / "backups" / "x.json").resolve()
    for bad in (root / "artifacts" / "x.json", root / "data" / "backups" / ".." / "x.json"):
        with pytest.raises(SystemExit):
            cr.backups_path(bad)


def test_the_scripts_own_queries_run_in_read_sessions():
    seen = {}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def run(self, query, **params):
            return [{"n": 1}]

    class Driver:
        def session(self, **config):
            seen.update(config)
            return Session()

    assert cr._session_rows(Driver(), "RETURN 1 AS n") == [{"n": 1}]
    assert seen == {"default_access_mode": "READ"}


# Decision 006 (docs/decisions/006-m2-after-m2a.md): the M2 reopen trigger, applied at $0 by the `tripwire` mode.
NV = 1045810


def gold_q(*gold, anchors=((("Nvidia", NV),))):
    return {"id": "Q", "anchors": [{"name": n, "cik": c} for n, c in anchors],
            "gold": [{"chunk_id": cid, "filer_cik": cik} for cid, cik in gold]}


def test_a_gold_id_anywhere_in_the_context_is_a_hit():
    q = gold_q(("a:I.1A:0001", NV), ("a:I.1A:0002", NV))
    v = cr.tripwire_verdict(q, "RISKS\n[a:I.1A:0002]\nwe rely on ...", mentions={})
    assert v["hit"] and v["present"] == ["a:I.1A:0002"] and not v["filer_own_miss"]


def test_a_miss_with_a_filer_own_unmentioning_gold_chunk_is_a_filer_own_miss():
    q = gold_q(("a:I.1A:0001", NV))
    v = cr.tripwire_verdict(q, "[b:I.1A:0009]", mentions={"a:I.1A:0001": []})
    assert not v["hit"] and v["filer_own_unmentioned"] == ["a:I.1A:0001"] and v["filer_own_miss"]


def test_a_miss_whose_gold_mentions_the_anchor_or_is_another_filers_is_not_a_filer_own_miss():
    q = gold_q(("a:I.1A:0001", NV), ("c:I.1A:0003", 2488))
    v = cr.tripwire_verdict(q, "", mentions={"a:I.1A:0001": [NV], "c:I.1A:0003": []})
    assert not v["hit"] and v["filer_own_unmentioned"] == [] and not v["filer_own_miss"]


def test_a_gold_chunk_missing_from_the_graph_is_reported_not_guessed():
    q = gold_q(("a:I.1A:0001", NV))
    v = cr.tripwire_verdict(q, "", mentions={})
    assert v["missing_from_graph"] == ["a:I.1A:0001"] and not v["filer_own_miss"]


def test_the_trigger_reopens_m2_at_two_filer_own_misses():
    assert not cr.tripwire_reopens([{"filer_own_miss": True}, {"filer_own_miss": False}])
    assert cr.tripwire_reopens([{"filer_own_miss": True}, {"filer_own_miss": True}])


def test_the_tripwire_refuses_a_gold_file_that_changed_since_it_was_registered(tmp_path):
    p = tmp_path / "gold.json"
    p.write_text('{"questions": []}\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="registered"):
        cr.registered_gold(p, expected_sha256="0" * 64)
    assert cr.registered_gold(p, expected_sha256=cr.file_sha256(p)) == {"questions": []}


def test_the_registered_gold_hash_is_the_committed_files():
    assert cr.file_sha256(cr.ROOT / "artifacts" / "m2_tripwire_gold.json") == cr.TRIPWIRE_GOLD_SHA256


def test_known_gap_excerpts_keep_only_passages_that_mention_an_anchor():
    """Characterization of the structural gap decision 006 deferred: the excerpt query keeps a passage only if it MENTIONS an
    anchor company, so a filer's own "we ..." passage cannot be an excerpt. If this fails, the gap was closed: update decision
    006 and the tripwire."""
    from semigraph.retrieval import retriever as R

    assert "MATCH (node)-[:MENTIONS]->(c:Company) WHERE c.cik IN $ids" in R.EXCERPTS_QUERY


def test_any_gold_id_present_is_a_hit_even_when_another_gold_chunk_is_filer_own_and_unmentioning():
    q = gold_q(("a:I.1A:0001", NV), ("a:I.1A:0002", NV))
    v = cr.tripwire_verdict(q, " [a:I.1A:0002]", mentions={"a:I.1A:0001": [], "a:I.1A:0002": []})
    assert v["hit"] and v["filer_own_unmentioned"] == ["a:I.1A:0001", "a:I.1A:0002"] and not v["filer_own_miss"]


def test_ciks_compare_as_integers_whatever_type_the_graph_returns():
    q = gold_q(("a:I.1A:0001", NV))
    v = cr.tripwire_verdict(q, "", mentions={"a:I.1A:0001": [str(NV)]})
    assert v["filer_own_unmentioned"] == [] and not v["filer_own_miss"]


def test_a_run_with_a_gold_chunk_missing_from_the_graph_is_not_a_valid_measurement():
    verdicts = [{"id": "D2", "missing_from_graph": ["a:I.1A:0001"]}, {"id": "D3", "missing_from_graph": []}]
    assert cr.invalid_questions(verdicts) == ["D2"]
