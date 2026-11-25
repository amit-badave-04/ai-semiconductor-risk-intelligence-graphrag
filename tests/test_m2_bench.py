"""M2-Bench harness (docs/specs/m2-search.md section 7): gold scope, quote binding, split, metrics, registration."""

import importlib.util
import sys
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
