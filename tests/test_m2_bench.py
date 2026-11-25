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
    lake = chunks(("k:I.1A:0001", "NV", 1, "10-K", "2026-02-25", "k", "I.1A", "the “AI Diffusion”   Rule"))
    assert mb.bind_quote('The "AI diffusion" rule', lake) == [{"chunk_id": "k:I.1A:0001", "grade": 2}]


def test_an_unbound_quote_binds_to_nothing():
    assert mb.bind_quote("not in any filing", LAKE) == []


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
