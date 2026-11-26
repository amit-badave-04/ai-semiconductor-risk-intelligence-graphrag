"""M2-Bench harness (docs/specs/m2-search.md section 7): gold scope, quote binding, split, metrics, registration."""

import hashlib
import importlib.util
import json
import math
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "m2_bench.py"
spec = importlib.util.spec_from_file_location("m2_bench", SCRIPT)
mb = importlib.util.module_from_spec(spec)
sys.modules["m2_bench"] = mb
spec.loader.exec_module(mb)


def chunks(*rows):
    cols = ["chunk_id", "ticker", "cik", "form", "filing_date", "accession_no", "section_id", "text"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows])


LAKE = chunks(
    ("k23:I.1A:0001", "NV", 1, "10-K", "2025-02-26", "k23", "I.1A", "We depend on   foundry partners in Taiwan."),
    ("k24:I.1A:0001", "NV", 1, "10-K", "2026-02-25", "k24", "I.1A", "We depend on foundry partners in Taiwan."),
    ("q1:II.1A:0001", "NV", 1, "10-Q", "2026-05-20", "q1", "II.1A", "We depend on foundry partners in Taiwan. More."),
    ("q0:II.1A:0001", "NV", 1, "10-Q", "2025-11-19", "q0", "II.1A", "We depend on foundry partners in Taiwan."),
    ("a20:I.3:0001", "TS", 2, "20-F", "2026-04-16", "a20", "I.3", "Our fabs are in Taiwan."),
)


def test_scope_is_the_newest_annual_plus_later_quarterlies():
    scope = mb.in_scope_accessions(LAKE)
    assert scope == {"k24", "q1", "a20"}


def test_a_quote_binds_to_every_verbatim_copy_with_grades():
    gold = mb.bind_quote("We depend on foundry partners in Taiwan", LAKE)
    assert gold == [{"chunk_id": "k24:I.1A:0001", "grade": 2}, {"chunk_id": "q1:II.1A:0001", "grade": 2},
                    {"chunk_id": "k23:I.1A:0001", "grade": 1}, {"chunk_id": "q0:II.1A:0001", "grade": 1}]


def test_binding_normalises_whitespace_case_and_curly_quotes():
    lake = chunks(("k:I.1A:0001", "NV", 1, "10-K", "2026-02-25", "k", "I.1A", "the “AI Diffusion”   Rule restricts exports"))
    assert mb.bind_quote('The "AI diffusion" rule restricts exports', lake) == [{"chunk_id": "k:I.1A:0001", "grade": 2}]


def test_an_unbound_quote_binds_to_nothing():
    assert mb.bind_quote("this sentence is not in any filing at all", LAKE) == []


def test_the_split_is_deterministic_by_family():
    fams = [f"fam{i}" for i in range(200)]
    first = [mb.split_of(f) for f in fams]
    assert first == [mb.split_of(f) for f in fams]
    assert set(first) == {"dev", "test"}


def test_success_at_k_counts_grade_2_only():
    gold = [{"chunk_id": "a", "grade": 2}, {"chunk_id": "b", "grade": 1}]
    assert mb.success_at(["x", "b"], gold, k=8) is False
    assert mb.success_at(["x", "a"], gold, k=8) is True
    assert mb.success_at(["x"] * 8 + ["a"], gold, k=8) is False


def test_type_d_needs_a_gold_chunk_for_every_anchor():
    gold = [{"chunk_id": "a", "grade": 2, "anchor": 1}, {"chunk_id": "b", "grade": 2, "anchor": 2}]
    assert mb.success_at(["a"], gold, k=8, per_anchor=True) is False
    assert mb.success_at(["b", "a"], gold, k=8, per_anchor=True) is True


def test_ndcg_uses_the_grades():
    gold = [{"chunk_id": "a", "grade": 2}, {"chunk_id": "b", "grade": 1}]
    assert mb.ndcg_at(["a", "b"], gold, k=10) == pytest.approx(1.0)
    assert mb.ndcg_at(["b", "a"], gold, k=10) < 1.0
    assert mb.ndcg_at(["x"], gold, k=10) == 0.0


@pytest.mark.parametrize(("wins", "losses", "p"), [(6, 0, 0.03125), (5, 0, 0.0625), (12, 3, 0.03515625), (0, 0, 1.0)])
def test_the_sign_test_is_the_exact_two_sided_binomial(wins, losses, p):
    assert mb.sign_test(wins, losses) == pytest.approx(p)


def test_a_bench_file_that_changed_since_registration_is_refused(tmp_path):
    f = tmp_path / "bench.json"
    f.write_text('{"questions": []}\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="registered"):
        mb.registered_bench(f, expected_sha256="0" * 64)
    assert mb.registered_bench(f, expected_sha256=mb.file_sha256(f)) == {"questions": []}


def lake_for_sampling():
    rows = []
    for i in range(40):
        rows.append((f"k:I.1A:{i:04d}", "NV", 1, "10-K", "2026-02-25", "k", "I.1A",
                     f"We rely on partners for item {i}. Revenue was $1.{i} billion, up 12% in fiscal 2026. "
                     f"Uses CoWoS and H{i}00 in our supply chain, which we expect to keep for several more years."))
        rows.append((f"q:II.1A:{i:04d}", "AM", 2, "10-Q", "2026-05-20", "q", "II.1A",
                     f"For the three months ended March 2026 we saw export limits on item {i} across our supply chain, "
                     f"and we expect the same limits on item {i} to continue in later quarters."))
    rows.append(("a:I.1A:0000", "AM", 2, "10-K", "2026-02-01", "a", "I.1A", "Annual filing text. " * 10))
    return chunks(*rows)


def test_sampling_is_deterministic_and_draws_only_in_scope_chunks():
    lake = lake_for_sampling()
    a = mb.sample_passages(lake, seed=7, per_type=5, aliases={1: ["nvidia"], 2: ["amd"]})
    assert a == mb.sample_passages(lake, seed=7, per_type=5, aliases={1: ["nvidia"], 2: ["amd"]})
    assert set(a) == set(mb.TYPES)
    scope = mb.in_scope_accessions(lake)
    for t, items in a.items():
        for item in items:
            for cid in item["chunk_ids"]:
                assert cid.split(":")[0] in scope, (t, cid)


def test_type_d_items_pair_two_different_filers():
    lake = lake_for_sampling()
    d = mb.sample_passages(lake, seed=1, per_type=3, aliases={1: ["nvidia"], 2: ["amd"]})["D"]
    assert d and all(len(set(x["ciks"])) == 2 for x in d)


def test_type_a_passages_do_not_name_their_filer():
    lake = chunks(("k:I.1A:0001", "NV", 1, "10-K", "2026-02-25", "k", "I.1A", "We rely on foundries. " * 10),
                  ("k:I.1A:0002", "NV", 1, "10-K", "2026-02-25", "k", "I.1A", "NVIDIA relies on foundries. We " * 10))
    a = mb.sample_passages(lake, seed=1, per_type=5, aliases={1: ["nvidia"]})["A"]
    assert [x["chunk_ids"] for x in a] == [["k:I.1A:0001"]]


# Review of 3436f3b (code-reviewer): fixes before the bench is frozen.
def test_type_d_fails_when_an_anchor_has_only_older_gold():
    gold = [{"chunk_id": "a", "grade": 2, "anchor": 1}, {"chunk_id": "b", "grade": 1, "anchor": 2}]
    assert mb.success_at(["a", "b"], gold, k=8, per_anchor=True) is False


def test_a_short_quote_is_refused():
    with pytest.raises(ValueError, match="too short"):
        mb.bind_quote("supply chain", LAKE)


def test_the_newest_annual_breaks_date_ties_by_accession():
    lake = chunks(("b:I.1A:0001", "NV", 1, "10-K", "2026-02-25", "b", "I.1A", "x " * 100),
                  ("a:I.1A:0001", "NV", 1, "10-K", "2026-02-25", "a", "I.1A", "y " * 100))
    assert mb.in_scope_accessions(lake) == {"b"}
    assert mb.in_scope_accessions(lake.iloc[::-1]) == {"b"}


def test_normalise_maps_dashes_ellipses_and_zero_width_characters():
    assert mb.normalise("Part II \u2013 Item\u200b 8 \u2014 risks\u2026") == "part ii - item 8 - risks..."


def test_ndcg_ignores_a_repeated_chunk_id():
    gold = [{"chunk_id": "a", "grade": 2}]
    assert mb.ndcg_at(["a", "a"], gold, k=10) == pytest.approx(1.0)


def test_the_split_puts_about_a_third_of_families_in_dev():
    share = sum(mb.split_of(f"fam{i}") == "dev" for i in range(3000)) / 3000
    assert 0.30 < share < 0.37


def test_the_sampler_draws_one_copy_of_text_repeated_across_filings():
    text = "We rely on a small number of foundries for wafers and we expect to keep doing so. " * 3
    lake = chunks(("k:I.1A:0001", "NV", 1, "10-K", "2026-02-25", "k", "I.1A", text),
                  ("q:II.1A:0001", "NV", 1, "10-Q", "2026-05-20", "q", "II.1A", text))
    drawn = [c for items in mb.sample_passages(lake, seed=3, per_type=5, aliases={1: ["nvidia"]}).values()
             for it in items for c in it["chunk_ids"]]
    assert len(drawn) == 1


def test_us_in_capitals_is_not_the_pronoun_and_fiscal_year_labels_are_not_ids():
    assert not mb.WE.search("Changes in US export rules apply.")
    assert mb.WE.search("We expect changes.")
    assert not mb.ID_LIKE.search("Revenue in FY2026 and FY24 grew.")
    assert mb.ID_LIKE.search("Shipments of H200 and CoWoS capacity under ECCN 3A090.")


def test_a_second_round_skips_families_already_drawn_and_takes_counts_per_type():
    lake = lake_for_sampling()
    first = mb.sample_passages(lake, seed=7, per_type=3, aliases={1: ["nvidia"], 2: ["amd"]})
    drawn = {f for items in first.values() for it in items for f in it["family"].split("+")}
    second = mb.sample_passages(lake, seed=8, per_type={"A": 2, "B": 0, "C": 0, "D": 0, "E": 1, "F": 0},
                                aliases={1: ["nvidia"], 2: ["amd"]}, exclude_families=drawn)
    assert [len(second[t]) for t in mb.TYPES] == [2, 0, 0, 0, 1, 0]
    again = {f for items in second.values() for it in items for f in it["family"].split("+")}
    assert not again & drawn


def topic_lake():
    long = " in addition we describe further details of our approach and the risks involved for several more years."
    return chunks(
        ("n:I.1A:0001", "NV", 1, "10-K", "2026-02-25", "n", "I.1A", "Export controls on advanced chips to China limit our sales" + long),
        ("a:I.1A:0001", "AM", 2, "10-K", "2026-02-01", "a", "I.1A", "Export controls on advanced chips to China restrict our products" + long),
        ("a:I.1A:0002", "AM", 2, "10-K", "2026-02-01", "a", "I.1A", "Our HBM memory supply depends on a few suppliers" + long),
        ("m:I.1A:0001", "MU", 3, "10-K", "2026-02-10", "m", "I.1A", "Demand for HBM memory from AI customers grew strongly" + long),
        ("n:I.1A:0002", "NV", 1, "10-K", "2026-02-25", "n", "I.1A", "Our HBM memory supply depends on a few suppliers too" + long),
    )


def test_topic_pairs_share_a_specific_topic_and_differ_in_filer():
    pairs = mb.sample_topic_pairs(topic_lake(), seed=1, n=3, topics=("export controls", "hbm"))
    assert pairs == mb.sample_topic_pairs(topic_lake(), seed=1, n=3, topics=("export controls", "hbm"))
    assert pairs
    lake = topic_lake().set_index("chunk_id")
    for it in pairs:
        a, b = it["chunk_ids"]
        assert lake.loc[a, "cik"] != lake.loc[b, "cik"]
        assert it["topic"] in lake.loc[a, "text"].lower() and it["topic"] in lake.loc[b, "text"].lower()


def test_topic_pairs_prefer_the_most_similar_passages_and_skip_excluded_families():
    pairs = mb.sample_topic_pairs(topic_lake(), seed=1, n=1, topics=("hbm",))
    assert sorted(pairs[0]["chunk_ids"]) == ["a:I.1A:0002", "n:I.1A:0002"]
    fam = mb._family("AM", topic_lake().set_index("chunk_id").loc["a:I.1A:0002", "text"])
    again = mb.sample_topic_pairs(topic_lake(), seed=1, n=1, topics=("hbm",), exclude_families={fam})
    assert "a:I.1A:0002" not in again[0]["chunk_ids"]


def test_the_registered_hash_ignores_crlf_line_endings_so_a_windows_checkout_matches_the_git_blob(tmp_path):
    lf, crlf = tmp_path / "lf.json", tmp_path / "crlf.json"
    lf.write_bytes(b'{\n "questions": []\n}\n')
    crlf.write_bytes(b'{\r\n "questions": []\r\n}\r\n')
    assert mb.file_sha256(lf) == mb.file_sha256(crlf)


def test_the_frozen_bench_files_match_their_registered_hashes():
    for split, sha in mb.REGISTERED.items():
        assert mb.registered_bench(mb.ROOT / "artifacts" / "m2_bench" / f"m2_bench_{split}.json", sha)["split"] == split


def test_scoring_a_run_reports_excerpt_and_context_hits_per_question():
    bench = [{"id": "Q1", "type": "A", "gold": [{"chunk_id": "a", "grade": 2, "anchor": 1}]},
             {"id": "Q2", "type": "D", "gold": [{"chunk_id": "b", "grade": 2, "anchor": 1},
                                               {"chunk_id": "c", "grade": 2, "anchor": 2}]}]
    run = {"Q1": {"excerpts": ["x", "a"], "context_ids": ["x", "a"]},
           "Q2": {"excerpts": ["b"], "context_ids": ["b", "c"]}}
    scored = mb.score_run(bench, run)
    assert scored["Q1"] == {"type": "A", "success8": True, "context_hit": True, "ndcg10": pytest.approx(1 / math.log2(3))}
    assert scored["Q2"]["success8"] is False and scored["Q2"]["context_hit"] is True
    assert mb.summarise(scored) == {"n": 2, "success8": 1, "context_hit": 2, "misses": 1}


def test_arms_map_to_fusion_configs_and_the_baseline_is_todays_path():
    assert mb.arm_config("baseline") is None
    cfgs = {a: mb.arm_config(a) for a in mb.ARMS if a != "baseline"}
    assert {a: (c.mention, c.filer, c.period, c.keyword, c.keyword_context) for a, c in cfgs.items()} == {
        "mention": (True, False, False, False, False), "filer": (True, True, False, False, False),
        "period": (True, False, True, False, False), "filer+period": (True, True, True, False, False),
        "keyword": (True, False, False, True, False), "keyword_ctx": (True, False, False, True, True),
        "all": (True, True, True, True, True), "all+rerank_minilm": (True, True, True, True, True),
        "all+rerank_mxbai": (True, True, True, True, True)}
    assert {a: (c.rerank, c.rerank_depth) for a, c in cfgs.items() if c.rerank} == {"all+rerank_minilm": ("minilm", 20),
                                                                                  "all+rerank_mxbai": ("mxbai", 20)}
    assert all(c.current_only for c in cfgs.values())            # decision 011: the new legs are current-only in every arm
    with pytest.raises(SystemExit):
        mb.arm_config("bm25")


def test_keyword_arms_need_an_index_and_the_context_setting_follows_the_arm():
    assert [a for a in mb.ARMS if mb.arm_config(a) is not None and mb.arm_config(a).keyword] == ["keyword", "keyword_ctx", "all", "all+rerank_minilm",
                                                                                                         "all+rerank_mxbai"]
    rows = [{"chunk_id": "c1", "text": "export controls", "context": "NVIDIA NVDA 10-K 2025"}]
    assert mb.index_for(mb.arm_config("mention"), rows) is None
    plain, ctx = mb.index_for(mb.arm_config("keyword"), rows), mb.index_for(mb.arm_config("all"), rows)
    assert plain.with_context is False and ctx.with_context is True
    assert plain.search("nvda") == [] and [h.chunk_id for h in ctx.search("nvda")] == ["c1"]


def test_timing_summary_records_the_wall_seconds_per_question():
    assert mb.timing_summary([]) == {"n": 0, "mean_s": 0.0, "p95_s": 0.0, "max_s": 0.0}
    got = mb.timing_summary([0.1] * 18 + [0.5, 1.0])
    assert got["n"] == 20 and got["max_s"] == 1.0 and got["p95_s"] == 0.5 and got["mean_s"] == pytest.approx(0.165)


# ---- I6: the one-time test run (spec section 7: primary test, no-loss gates, R21) ----

def sc(success8, ctx=None, ndcg=0.0, rec=0.0, typ="B"):
    return {"type": typ, "success8": success8, "context_hit": success8 if ctx is None else ctx, "ndcg10": ndcg, "recall20": rec}


def test_strata_follow_the_baseline_context_alone():
    base = {"q1": sc(False, ctx=False), "q2": sc(False, ctx=True), "q3": sc(True, ctx=True)}
    assert mb.classify_strata(base) == {"q1": "headroom", "q2": "control", "q3": "control"}


def wl(pairs, typ="B"):
    """Scores for questions whose (baseline, candidate) success8 are the given pairs."""
    base = {f"q{i}": sc(b, typ=typ) for i, (b, _) in enumerate(pairs)}
    cand = {f"q{i}": sc(c, typ=typ) for i, (_, c) in enumerate(pairs)}
    return base, cand, list(base)


def test_paired_outcomes_count_wins_and_losses_and_drop_ties():
    base, cand, ids = wl([(False, True)] * 3 + [(True, False)] + [(True, True), (False, False)])
    assert mb.paired_outcomes(base, cand, ids) == (3, 1)


def test_h2_runs_only_after_h1_passes():
    base, cand, ids = wl([(False, True)] * 12)             # 12-0: p = 2/4096
    _, filer, _ = wl([(False, True)] * 5 + [(False, False)] * 7)
    out = mb.primary_test(base, cand, filer, ids)
    assert out["h1"]["passes"] and out["h2"]["run"] and out["h2"]["wins"] == 7 and out["h2"]["losses"] == 0
    assert out["h2"]["passes"] is True and out["h1"]["alpha"] == 0.05
    weak_b, weak_c, wids = wl([(False, True)] * 3 + [(True, False)] * 2)
    weak = mb.primary_test(weak_b, weak_c, weak_b, wids)
    assert weak["h1"]["passes"] is False and weak["h2"] == {"run": False}


def test_h1_with_type_a_excluded_is_a_sensitivity_result_reported_either_way():
    base = {f"a{i}": sc(False, typ="A") for i in range(10)} | {f"b{i}": sc(False) for i in range(2)}
    cand = {f"a{i}": sc(True, typ="A") for i in range(10)} | {f"b{i}": sc(True) for i in range(2)}
    out = mb.primary_test(base, cand, base, list(base))
    assert out["h1"]["wins"] == 12 and out["h1_without_type_a"]["wins"] == 2
    assert out["h1_without_type_a"]["passes"] is False


def test_the_per_type_gate_needs_wins_minus_losses_at_least_zero_in_every_type():
    base = {"a": sc(False, typ="A"), "b1": sc(True, typ="B"), "b2": sc(False, typ="B")}
    cand = {"a": sc(True, typ="A"), "b1": sc(False, typ="B"), "b2": sc(True, typ="B")}
    assert mb.per_type_gate(base, cand, list(base))["pass"] is True
    cand["b2"] = sc(False, typ="B")
    gate = mb.per_type_gate(base, cand, list(base))
    assert gate["pass"] is False and gate["by_type"]["B"] == -1


def test_the_control_gate_allows_two_losses_and_ignores_headroom_losses():
    ids = [f"q{i}" for i in range(4)]
    base = {i: sc(True) for i in ids}
    strata = {i: "control" for i in ids}
    two = {**base, "q0": sc(False), "q1": sc(False)}
    assert mb.control_gate(base, two, ids, strata)["pass"] is True
    three = {**two, "q2": sc(False)}
    assert mb.control_gate(base, three, ids, strata)["pass"] is False
    assert mb.control_gate(base, three, ids, {**strata, "q0": "headroom"})["pass"] is True


def test_the_control_gate_also_reports_context_hit_losses_beside_the_success8_losses():
    ids = ["q0", "q1"]
    base = {i: sc(True, ctx=True) for i in ids}
    cand = {"q0": sc(True, ctx=False), "q1": sc(True, ctx=True)}      # still Success@8 in the fixture, but the context hit is gone
    out = mb.control_gate(base, cand, ids, {i: "control" for i in ids})
    assert out["losses"] == 0 and out["context_hit_losses"] == 1 and out["pass"] is True


def test_the_tripwire_stays_unchanged_only_if_every_baseline_hit_stays_a_hit():
    base = [{"id": "D2", "hit": True}, {"id": "R1", "hit": True}, {"id": "R2", "hit": False}]
    ok = mb.tripwire_gate(base, [{"id": "D2", "hit": True}, {"id": "R1", "hit": True}, {"id": "R2", "hit": True}])
    assert ok["pass"] is True and ok["baseline_hits"] == 2 and ok["candidate_hits"] == 3
    lost = mb.tripwire_gate(base, [{"id": "D2", "hit": False}, {"id": "R1", "hit": True}, {"id": "R2", "hit": False}])
    assert lost["pass"] is False and lost["lost"] == ["D2"]


def test_known_ids_come_from_metric_ids_facts_and_judge_notes_by_regex():
    q = {"metric_ids": ["xbrl:1045810:revenue:2026-01-25"], "facts": {"a": "see 0001045810-26-000021:I.1A:0355 here"},
         "judge_notes": "cites [fr:2025-19001] and xbrl:2488:rnd:2025-12-27, not prose like fr: or I.1"}
    assert mb.known_ids(q) == {"xbrl:1045810:revenue:2026-01-25", "0001045810-26-000021:I.1A:0355", "fr:2025-19001",
                               "xbrl:2488:rnd:2025-12-27"}
    assert mb.known_ids({"id": "N9", "q": "text only"}) == set()


def test_the_old_sixty_gate_fails_on_a_context_miss_or_any_known_id_the_baseline_held_and_the_candidate_lost():
    qs = [{"id": "a", "metric_ids": ["xbrl:1:revenue:2026-01-25"]}, {"id": "b", "q": "no ids"},
          {"id": "c", "judge_notes": "fr:2025-00636"},
          {"id": "d", "judge_notes": "fr:2025-00001 fr:2025-00002"}]
    base = {"a": ["xbrl:1:revenue:2026-01-25"], "b": [], "c": ["other"], "d": ["fr:2025-00001", "fr:2025-00002"]}
    cand = {"a": ["xbrl:1:revenue:2026-01-25"], "b": [], "c": ["other"], "d": ["fr:2025-00001"]}
    out = mb.old_sixty_gate(qs, base, cand)
    assert out["pass"] is False and out["checked"] == 3 and out["skipped"] == 1
    assert out["misses"] == ["c"] and out["baseline_misses"] == ["c"]
    assert out["lost"] == {"d": ["fr:2025-00002"]}          # d still hits any-of, but lost one of its two known ids
    clean = mb.old_sixty_gate(qs[:1], base, cand)
    assert clean["pass"] is True and clean["lost"] == {}


def test_the_bootstrap_is_deterministic_and_resamples_by_family():
    diffs = {f"f{i}": [1.0] for i in range(8)} | {"g": [0.0, 0.0]}
    a = mb.paired_bootstrap(diffs, resamples=500, seed=7)
    assert (a == mb.paired_bootstrap(diffs, resamples=500, seed=7)).all()
    assert not (a == mb.paired_bootstrap(diffs, resamples=500, seed=8)).all()
    assert a.min() >= 0.0 and a.max() <= 1.0 and len(a) == 500


def test_holm_steps_down_across_three_metrics_and_widens_intervals_by_rank():
    out = mb.holm({"s": 0.001, "n": 0.03, "r": 0.04}, alpha=0.05)
    assert out["s"] == {"significant": True, "alpha": 0.05 / 3}
    assert out["n"] == {"significant": False, "alpha": 0.05 / 2} and out["r"]["significant"] is False


def test_secondary_metrics_give_an_interval_per_metric_with_holm_levels():
    fam = {f"q{i}": f"fam{i}" for i in range(20)}
    base = {k: sc(False, ndcg=0.1, rec=0.0) for k in fam}
    cand = {k: sc(True, ndcg=0.6, rec=0.5) for k in fam}
    out = mb.secondary_metrics(base, cand, list(fam), fam, resamples=300, seed=1)
    assert set(out) == {"success8", "ndcg10", "recall20"}
    assert out["ndcg10"]["mean_diff"] == pytest.approx(0.5) and out["ndcg10"]["ci"][0] > 0
    assert out["recall20"]["holm"]["significant"] is True


def test_recall_at_20_is_the_share_of_grade_2_gold_in_the_first_twenty():
    gold = [{"chunk_id": "g1", "grade": 2}, {"chunk_id": "g2", "grade": 2}, {"chunk_id": "o", "grade": 1}]
    ranked = ["g1"] + [f"x{i}" for i in range(19)] + ["g2"]
    assert mb.recall_at(ranked, gold, 20) == 0.5
    assert mb.recall_at(ranked, gold, 21) == 1.0
    assert mb.recall_at(ranked, [{"chunk_id": "o", "grade": 1}], 20) == 0.0


def test_the_reranker_trigger_counts_dev_misses_with_gold_in_the_fused_top_50_beyond_rank_8():
    legs = {"mention": [f"m{i}" for i in range(10)], "keyword:1": ["g"] + [f"k{i}" for i in range(9)]}
    qs = {"miss_deep": {"success8": False, "leg_ranks": {"mention": [f"m{i}" for i in range(20)] + ["g"]}},
          "miss_none": {"success8": False, "leg_ranks": legs},
          "hit": {"success8": True, "leg_ranks": legs}}
    gold = {"miss_deep": [{"chunk_id": "g", "grade": 2}], "miss_none": [{"chunk_id": "zzz", "grade": 2}],
            "hit": [{"chunk_id": "m0", "grade": 2}]}
    out = mb.reranker_trigger(qs, gold, ids=list(qs), rrf_k=60)
    assert out == {"misses": 2, "gold_in_top50_beyond_8": 1, "fraction": 0.5}


def test_the_fused_ranking_is_reciprocal_rank_fusion_over_the_leg_lists():
    fused = mb.fused_ranking({"a": ["x", "y"], "b": ["y", "z"]}, rrf_k=60)
    assert fused[0] == "y" and set(fused) == {"x", "y", "z"}


@pytest.mark.parametrize("name", ["test_report.json", "test_strata.json", "test_started.json"])
def test_run_test_refuses_to_run_twice_if_any_run_once_file_exists(tmp_path, name):
    mb.refuse_if_already_run(tmp_path)          # nothing yet: fine
    (tmp_path / name).write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="once"):
        mb.refuse_if_already_run(tmp_path)


def test_claiming_the_run_is_an_exclusive_create_that_a_second_claim_cannot_win(tmp_path):
    mb.claim_test_run(tmp_path, {"head": "abc"})
    assert json.loads((tmp_path / "test_started.json").read_text(encoding="utf-8"))["head"] == "abc"
    with pytest.raises(SystemExit, match="once"):
        mb.claim_test_run(tmp_path, {"head": "def"})
    assert json.loads((tmp_path / "test_started.json").read_text(encoding="utf-8"))["head"] == "abc"


def test_the_frozen_settings_must_match_the_registered_hash(tmp_path):
    real = mb.BENCH_DIR / "frozen_settings.json"
    assert mb.require_frozen_settings(real) == mb.FROZEN_SETTINGS_SHA256
    tampered = tmp_path / "frozen_settings.json"
    tampered.write_bytes(real.read_bytes().replace(b'"rerank_depth": 20', b'"rerank_depth": 30'))
    with pytest.raises(SystemExit, match="frozen"):
        mb.require_frozen_settings(tampered)


def test_the_reranker_file_must_match_the_frozen_hash_prefix(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"weights")
    prefix = hashlib.sha256(b"weights").hexdigest()[:16]
    assert mb.check_reranker_hash(model, prefix) == prefix
    with pytest.raises(SystemExit, match="reranker"):
        mb.check_reranker_hash(model, "0" * 16)
    with pytest.raises(SystemExit, match="reranker"):
        mb.check_reranker_hash(tmp_path / "missing.onnx", prefix)


@pytest.mark.parametrize("uri", ["bolt://db.example.com:7687", "neo4j+s://xyz.databases.neo4j.io", "bolt://neo4j:pw@localhost:7687"])
def test_the_local_graph_guard_refuses_remote_hosts_and_credentials_in_the_uri(uri):
    with pytest.raises(SystemExit, match="local graph"):
        mb.require_local_graph(uri)
    mb.require_local_graph("bolt://localhost:7687")
    mb.require_local_graph("bolt://127.0.0.1:7687")


def test_run_dev_uses_the_same_local_graph_guard(monkeypatch, tmp_path):
    import semigraph.config as cfg

    monkeypatch.setattr(cfg, "get_settings", lambda: types.SimpleNamespace(neo4j_uri="bolt://prod.example.com:7687"))
    with pytest.raises(SystemExit, match="local graph"):
        mb.run_dev(tmp_path / "out.json", "baseline")


def test_the_candidate_is_the_frozen_config_and_the_filer_period_arm_is_the_i2_arm(tmp_path):
    frozen = tmp_path / "f.json"
    frozen.write_text('{"config": {"mention": true, "filer": true, "keyword": true, "keyword_context": true, '
                      '"rerank": "minilm", "rerank_depth": 20, "depth": 20}}', encoding="utf-8")
    cand = mb.frozen_candidate(frozen)
    assert cand.rerank == "minilm" and cand.rerank_depth == 20 and cand.keyword_context and cand.filer
    assert mb.arm_config("filer+period").period is True


def test_the_verdict_line_names_h1_h2_and_every_failed_gate():
    primary = {"h1": {"passes": True, "wins": 20, "losses": 2, "p": 0.0001},
               "h2": {"run": True, "passes": False, "wins": 4, "losses": 3, "p": 1.0}}
    gates = {"per_type": {"pass": True}, "control": {"pass": False}, "tripwire": {"pass": True}, "old_sixty": {"pass": True}}
    line = mb.verdict_line(primary, gates)
    assert "H1 pass 20-2" in line and "H2 fail 4-3" in line and "control" in line and "HOLD" in line


def test_analyse_test_uses_main_questions_only_and_wires_primary_secondary_and_gates():
    bench = [{"id": f"m{i}", "role": "main", "family": f"f{i}", "type": "B"} for i in range(12)]
    bench += [{"id": "r0", "role": "reserve", "family": "fr", "type": "B"}]
    base = {q["id"]: sc(False, typ="B", ndcg=0.1) for q in bench}
    cand = {q["id"]: sc(True, typ="B", ndcg=0.5) for q in bench}
    cand["r0"] = sc(False)                       # a reserve loss must not count
    strata = {q["id"]: "headroom" for q in bench}
    out = mb.analyse_test(bench, {"baseline": base, "filer+period": base, "candidate": cand}, strata)
    assert out["primary"]["h1"]["wins"] == 12 and out["primary"]["h1"]["losses"] == 0
    assert out["gates"]["per_type"]["pass"] and out["gates"]["control"]["control_n"] == 0
    assert set(out["secondary_vs_baseline"]) == {"success8", "ndcg10", "recall20"}


def test_the_dev_trigger_and_old_sixty_extraction_run_on_the_real_files_offline():
    import json
    dev = json.loads((mb.BENCH_DIR / "all_dev.json").read_text(encoding="utf-8"))
    bench = mb.registered_bench(mb.BENCH_DIR / "m2_bench_dev.json", mb.REGISTERED["dev"])["questions"]
    out = mb.reranker_trigger(dev["questions"], {q["id"]: q["gold"] for q in bench},
                              [q["id"] for q in bench if q["role"] == "main"], 60)
    assert out["misses"] == dev["summary_main"]["misses"] and 0.0 <= out["fraction"] <= 1.0
    old = json.loads((mb.ROOT / "src" / "semigraph" / "artifacts" / "benchmark.json").read_text(encoding="utf-8"))
    assert sum(1 for q in old if mb.known_ids(q)) == 36


# ---- run_test orchestration, dry run: fake driver, embedder and retrieval; no database, no network, no model ----

MARKER = "CONTEXT_TEXT_MARKER"


class _Rows(list):
    def single(self):
        return self[0]


class _Session:
    def __init__(self, gold_ids):
        self.gold_ids = gold_ids

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        if "Snapshot" in query:
            return _Rows([{"id": "snap-1"}])
        return _Rows([{"chunk_id": c, "mention_ciks": [1]} for c in params["ids"] if c not in self.gold_ids["absent"]])


class FakeDriver:
    gold_ids = {"absent": set()}

    def session(self, **_):
        return _Session(self.gold_ids)

    def close(self):
        self.closed = True


@pytest.fixture
def dry(monkeypatch, tmp_path):
    """run_test wired to fakes. ``events`` records the order of the graph calls; test files are copied into tmp_path."""
    import shutil

    import semigraph.config
    import semigraph.embeddings
    import semigraph.graph.client as client
    import semigraph.retrieval.keyword

    for name in ("frozen_settings.json", "m2_bench_test.json"):
        shutil.copy(mb.BENCH_DIR / name, tmp_path / name)
    bench = mb.registered_bench(tmp_path / "m2_bench_test.json", mb.REGISTERED["test"])["questions"]
    gold = {q["id"]: [g["chunk_id"] for g in q["gold"] if g["grade"] == 2] for q in bench}
    old = json.loads((mb.ROOT / "src" / "semigraph" / "artifacts" / "benchmark.json").read_text(encoding="utf-8"))
    known = {q["id"]: sorted(mb.known_ids(q)) for q in old}
    trip = mb._load_script("compare_retrieval").registered_gold(mb.ROOT / "artifacts" / "m2_tripwire_gold.json")["questions"]
    trip_gold = {q["question"]: [g["chunk_id"] for g in q["gold"]] for q in trip}
    events: list = []

    def fake_ids(items, driver, embedder, fusion, index):
        items = list(items)
        if items[0]["id"] in gold:
            events.append(("test", "baseline" if fusion is None else "arm",
                           (tmp_path / "test_strata.json").exists(), (tmp_path / "test_started.json").exists()))
            return {i["id"]: {"excerpts": [] if fusion is None else gold[i["id"]], "context_ids": []} for i in items}
        events.append(("old60",))
        return {i["id"]: {"excerpts": [], "context_ids": known[i["id"]]} for i in items}

    def fake_context(question, driver, embedder, fusion, index):
        events.append(("tripwire",))
        return MARKER + " " + " ".join(f"[{c}]" for c in trip_gold[question])

    monkeypatch.setattr(semigraph.config, "get_settings", lambda: types.SimpleNamespace(neo4j_uri="bolt://localhost:7687"))
    monkeypatch.setattr(semigraph.embeddings, "Embedder", lambda: types.SimpleNamespace(backend="fake"))
    monkeypatch.setattr(client, "get_driver", lambda settings: FakeDriver())
    monkeypatch.setattr(semigraph.retrieval.keyword, "load_rows", lambda driver: [])
    monkeypatch.setattr(mb, "index_for", lambda cfg, rows: None)
    monkeypatch.setattr(mb, "require_reranker", lambda fusion: None)
    monkeypatch.setattr(mb, "check_frozen_reranker", lambda frozen: "fakeprefix")
    monkeypatch.setattr(mb, "_retrieve_ids", fake_ids)
    monkeypatch.setattr(mb, "_retrieve_context", fake_context)
    return types.SimpleNamespace(out=tmp_path, events=events)


def test_the_dry_run_writes_strata_before_the_first_candidate_arm_and_the_report_has_no_context_text(dry):
    report = mb.run_test(dry.out)
    arm_calls = [e for e in dry.events if e[0] == "test"]
    assert arm_calls[0][1] == "baseline" and [e[1] for e in arm_calls[1:]] == ["arm", "arm"]
    assert arm_calls[0][3] is True                         # the claim exists before the first test question is asked
    assert all(e[2] for e in arm_calls[1:])                # test_strata.json exists when each candidate arm runs
    assert set(report) >= {"primary", "gates", "secondary_vs_baseline", "provenance", "reranker_trigger_dev", "verdict", "notes"}
    assert report["provenance"]["frozen_settings_sha256"] == mb.FROZEN_SETTINGS_SHA256 and report["provenance"]["head"]
    assert report["gates"]["tripwire"]["pass"] and report["gates"]["old_sixty"]["checked"] == 36
    for name in ("test_report.json", "test_strata.json", "test_runs.json", "test_noloss_runs.json", "test_started.json"):
        assert MARKER not in (dry.out / name).read_text(encoding="utf-8")


def test_a_second_run_is_refused_even_after_a_crash_in_the_analysis(dry, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("analysis crashed")

    monkeypatch.setattr(mb, "analyse_test", boom)
    with pytest.raises(RuntimeError):
        mb.run_test(dry.out)
    assert not (dry.out / "test_report.json").exists()
    assert (dry.out / "test_runs.json").exists()           # the raw ids survive for a later offline analysis
    before = len(dry.events)
    with pytest.raises(SystemExit, match="once"):
        mb.run_test(dry.out)
    assert len(dry.events) == before                        # no query was made on the second attempt


def test_an_offline_failure_before_the_claim_leaves_the_run_unclaimed(dry, monkeypatch):
    monkeypatch.setattr(mb, "check_frozen_reranker", lambda frozen: (_ for _ in ()).throw(SystemExit("reranker mismatch")))
    with pytest.raises(SystemExit, match="reranker"):
        mb.run_test(dry.out)
    assert not (dry.out / "test_started.json").exists() and dry.events == []


def test_a_tripwire_gold_chunk_missing_from_the_graph_is_excluded_from_the_gate_and_listed(dry):
    """Decision 006: a tripwire question whose any-of gold has a chunk the graph does not hold (R2) is not a clean measurement;
    the run goes on, and the no-loss gate leaves that question out and lists it (the gold is never edited)."""
    trip = mb._load_script("compare_retrieval").registered_gold(mb.ROOT / "artifacts" / "m2_tripwire_gold.json")["questions"]
    FakeDriver.gold_ids = {"absent": {trip[0]["gold"][0]["chunk_id"]}}
    try:
        mb.run_test(dry.out)
    finally:
        FakeDriver.gold_ids = {"absent": set()}
    gate = json.loads((dry.out / "test_report.json").read_text(encoding="utf-8"))["gates"]["tripwire"]
    assert gate["excluded"] == [trip[0]["id"]] and gate["n"] == len(trip) - 1


def test_a_remote_graph_is_refused_by_run_test_before_any_query(dry, monkeypatch):
    import semigraph.config

    monkeypatch.setattr(semigraph.config, "get_settings", lambda: types.SimpleNamespace(neo4j_uri="bolt://prod.example.com:7687"))
    with pytest.raises(SystemExit, match="local graph"):
        mb.run_test(dry.out)
    assert dry.events == [] and not (dry.out / "test_started.json").exists()


def test_the_tripwire_gate_excludes_a_question_whose_gold_is_not_all_in_the_graph_and_lists_it():
    """Decision 006 recorded R2 as not a clean measurement (one any-of gold chunk has no span in the graph); the test run's
    no-loss gate leaves such questions out and lists them, instead of refusing the whole run."""
    base = [{"id": "D2", "hit": True, "missing_from_graph": []}, {"id": "R2", "hit": True, "missing_from_graph": ["x"]}]
    cand = [{"id": "D2", "hit": True, "missing_from_graph": []}, {"id": "R2", "hit": False, "missing_from_graph": ["x"]}]
    gate = mb.tripwire_gate(base, cand)
    assert gate["pass"] and gate["excluded"] == ["R2"] and gate["n"] == 1
    lost = mb.tripwire_gate(base, [{"id": "D2", "hit": False, "missing_from_graph": []}, cand[1]])
    assert not lost["pass"] and lost["lost"] == ["D2"]
