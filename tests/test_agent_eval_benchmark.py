"""T0 gate for ``artifacts/agent_benchmark.json`` (M3-C): the file conforms to its schema, every expected value has a source that can be
re-derived, and the validator that says so is itself proven to catch each defect. Free: no LLM, no Neo4j, no network.

Ground truth here is never typed in by hand: an expectation is either the SAME expectation a main-benchmark question already carries
(``expect_from``) or a recomputation from ``facts`` (XBRL ids with values, each traced to a committed main-benchmark value or to the git-ignored
XBRL lake, which the lake test re-checks whenever ``data/processed/xbrl`` exists); risk-change questions take their grading notes from the
main benchmark's verified ``judge_notes`` (``judge_notes_from``).
"""

import copy
import glob
from pathlib import Path

import pytest

from semigraph.artifacts import load_benchmark
from semigraph.config import Settings
from semigraph.eval import agent_eval as ae

ROOT = Path(__file__).resolve().parents[1]
MAIN = load_benchmark()
MAIN_BY_ID = {b["id"]: b for b in MAIN}
LIMITS = ae.limits_from_settings(Settings(_env_file=None))
RAW = ae.read_agent_benchmark()
CATEGORIES = ("multi_company", "named_years", "metric_change", "risk_change", "no_overcall", "refusal", "injection")
DELETE = object()      # ``mutated(..., key=DELETE)`` removes the key


def problems(items):
    return ae.benchmark_problems(items, MAIN, limits=LIMITS)


# --- the real file ------------------------------------------------------------------------------------------------------------

def test_the_agent_benchmark_conforms_to_its_schema():
    assert problems(RAW) == []


def test_it_covers_every_agent_specific_category_at_about_twenty_questions():
    counts = {c: sum(1 for it in RAW if it["category"] == c) for c in CATEGORIES}
    assert all(counts[c] >= 1 for c in CATEGORIES), counts
    assert 4 <= counts["injection"] <= 6 and counts["metric_change"] >= 3 and counts["multi_company"] >= 2
    assert 18 <= len(RAW) <= 24 and sum(counts.values()) == len(RAW)


def test_ids_are_unique_and_do_not_collide_with_the_main_benchmark():
    ids = [it["id"] for it in RAW]
    assert len(ids) == len(set(ids)) and not set(ids) & set(MAIN_BY_ID)


def test_injection_questions_declare_only_tools_of_the_universe_and_a_mechanical_answer_gate():
    injections = [it for it in RAW if it["category"] == "injection"]
    for it in injections:
        # a category-"injection" question that itself asserts a company disclosure claim is typed "misattribution" (with
        # judge_notes_from) instead of "injection", so the judge grades it too and not only the mechanical guard (see A22)
        assert it["type"] in ("injection", "misattribution") and set(it["forbidden_tools"]) <= ae.AGENT_TOOL_UNIVERSE, it["id"]
        assert set(it["expected_tools"]) <= ae.AGENT_TOOL_UNIVERSE and (it.get("expect") or it.get("answer_forbidden")), it["id"]


def test_a_question_plain_retrieval_answers_expects_no_tool_and_at_most_one_step():
    plain = [it for it in RAW if it["category"] == "no_overcall"]
    assert plain and all(it["expected_tools"] == [] and it["max_steps"] <= 1 and it["forbidden_tools"] for it in plain)


def test_every_expected_value_is_backed_by_a_named_source_and_re_derives_from_it():
    for it in RAW:
        assert it["source"].strip(), it["id"]
        if it.get("expect"):
            assert it.get("expect_from") or it.get("facts"), it["id"]
        if it.get("facts"):
            assert ae.expectations_equal(ae.expectation_from_facts(it), it["expect"]), it["id"]
        if it.get("expect_from"):
            assert it["expect"] == MAIN_BY_ID[it["expect_from"]]["expect"], it["id"]


def test_facts_traced_to_the_main_benchmark_match_its_committed_values():
    checked = 0
    for it in RAW:
        for fact in it.get("facts") or []:
            if fact["from"].startswith("benchmark:"):
                # "benchmark:<id>" or "benchmark:<id>:<index>" (a multi-value main item): [1] is always the id, an optional [2] the index
                expect = MAIN_BY_ID[fact["from"].split(":")[1]]["expect"]
                assert fact["value"] in [*expect.get("values", []), *([expect["value"]] if "value" in expect else [])], (it["id"], fact)
                checked += 1
    assert checked >= 6


def test_facts_traced_to_the_lake_match_it_when_the_lake_is_present():
    pd = pytest.importorskip("pandas")
    files = sorted(glob.glob(str(ROOT / "data" / "processed" / "xbrl" / "*_key_metrics.parquet")))
    if not files:
        pytest.skip("the XBRL lake (data/processed/xbrl, git-ignored) is not built on this machine")
    metrics = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    assert ae.lake_mismatches(RAW, metrics) == []


def test_risk_change_questions_take_their_grading_notes_from_the_verified_main_items():
    resolved = {it["id"]: it for it in ae.resolve_judge_notes(RAW, MAIN)}
    risky = [it for it in RAW if it["category"] == "risk_change"]
    assert len(risky) >= 3
    for it in risky:
        assert it["type"] == "temporal" and it.get("expect") is None and it["judge_notes_from"], it["id"]
        assert all(MAIN_BY_ID[i]["judge_notes"] in resolved[it["id"]]["judge_notes"] for i in it["judge_notes_from"]), it["id"]
        assert ae.is_judged(resolved[it["id"]]) and "judge_notes" not in it


def test_only_the_risk_change_questions_and_the_misattribution_probe_are_paid_for_by_the_judge():
    resolved = ae.resolve_judge_notes(RAW, MAIN)
    judged_ids = {it["id"] for it in resolved if ae.is_judged(it)}
    assert judged_ids == {it["id"] for it in RAW if it["category"] == "risk_change"} | {it["id"] for it in RAW if it["type"] == "misattribution"}


# --- the derivation reproduces the committed gold, so it can be trusted for the new questions --------------------------------

def facts(*values):
    return [{"id": "xbrl:1:revenue:2025-01-01", "value": float(v), "from": "lake"} for v in values]


@pytest.mark.parametrize("gold_id, derivation, values", [
    ("NG01", "change", (130497000000, 215938000000)),        # Nvidia revenue, up
    ("NG03", "change", (53101000000, 52853000000)),          # Intel revenue, a small decline: the sign and the rounding
    ("NG18", "levels", (-267000000, 1689000000)),            # a loss: direction down
    ("NG12", "levels", (25785000000, 6456000000)),
    ("N2", "level", (215938000000,)),
])
def test_the_derivation_reproduces_committed_gold_expectations(gold_id, derivation, values):
    derived = ae.expectation_from_facts({"derivation": derivation, "facts": facts(*values)})
    assert ae.expectations_equal(derived, MAIN_BY_ID[gold_id]["expect"])


@pytest.mark.parametrize("bad", [
    {"derivation": "change", "facts": facts(0, 5)},          # a change from zero is undefined
    {"derivation": "change", "facts": facts(1, 2, 3)},
    {"derivation": "level", "facts": facts(1, 2)},
    {"derivation": "levels", "facts": facts(1)},
    {"derivation": "average", "facts": facts(1, 2)},
])
def test_a_derivation_that_cannot_be_computed_is_an_error(bad):
    with pytest.raises(ValueError):
        ae.expectation_from_facts(bad)


# --- the validator catches each defect it claims to ---------------------------------------------------------------------------

def mutated(index, **changes):
    items = copy.deepcopy(RAW)
    for key, value in changes.items():
        if value is DELETE:
            items[index].pop(key, None)
        else:
            items[index][key] = value
    return items


NUMERIC = next(i for i, it in enumerate(RAW) if it.get("facts"))
TRACED = next(i for i, it in enumerate(RAW) if it.get("facts") and it["facts"][0]["from"].startswith("benchmark:"))
FROM_MAIN = next(i for i, it in enumerate(RAW) if it.get("expect_from"))
RISK = next(i for i, it in enumerate(RAW) if it["category"] == "risk_change")
REFUSAL = next(i for i, it in enumerate(RAW) if it["type"] == "refusal")
INJECTION = next(i for i, it in enumerate(RAW) if it["type"] == "injection")


@pytest.mark.parametrize("label, items, needle", [
    ("missing id", mutated(0, id=DELETE), "id"),
    ("missing q", mutated(0, q=DELETE), "q"),
    ("missing max_steps", mutated(0, max_steps=DELETE), "max_steps"),
    ("missing expected_tools", mutated(0, expected_tools=DELETE), "expected_tools"),
    ("missing forbidden_tools", mutated(0, forbidden_tools=DELETE), "forbidden_tools"),
    ("missing source", mutated(0, source=""), "source"),
    ("unknown type", mutated(0, type="essay"), "type"),
    ("unknown category", mutated(0, category="vibes"), "category"),
    ("id collides with main", mutated(0, id="N2"), "collides"),
    ("tool outside the universe", mutated(0, expected_tools=["financial_metrics", "run_cypher"]), "run_cypher"),
    ("forbidden outside the universe", mutated(0, forbidden_tools=["delete_everything"]), "delete_everything"),
    ("expected and forbidden overlap", mutated(0, expected_tools=["financial_metrics"], forbidden_tools=["financial_metrics"]), "overlap"),
    ("max_steps above the agent limit", mutated(0, max_steps=LIMITS.max_tool_calls + 1), "max_steps"),
    ("more expected tools than steps", mutated(0, expected_tools=["financial_metrics", "compute_change"], max_steps=1), "max_steps"),
    ("max_steps not an integer", mutated(0, max_steps="3"), "max_steps"),
    ("an expectation with no source of truth", mutated(NUMERIC, facts=DELETE, derivation=DELETE), "facts"),
    ("an expectation that no longer matches its facts", mutated(NUMERIC, expect={"values": [1.0, 2.0]}), "re-derive"),
    ("expect_from that no longer matches", mutated(FROM_MAIN, expect={"value": 1}), "expect_from"),
    ("expect_from an unknown main id", mutated(FROM_MAIN, expect_from="ZZ9"), "ZZ9"),
    ("a fact traced to a value the main item does not carry", mutated(TRACED, facts=[{**RAW[TRACED]["facts"][0], "value": 7.0},
                                                                                     *RAW[TRACED]["facts"][1:]]), "is not the value"),
    ("a fact with a malformed id", mutated(TRACED, facts=[{**RAW[TRACED]["facts"][0], "id": "xbrl:nope"}, *RAW[TRACED]["facts"][1:]]), "malformed"),
    ("a fact of unknown origin", mutated(TRACED, facts=[{**RAW[TRACED]["facts"][0], "from": "my head"}, *RAW[TRACED]["facts"][1:]]), "origin"),
    ("an invalid expect key", mutated(NUMERIC, expect={"vibes": 1}), "expect"),
    ("hand-typed judge notes", mutated(RISK, judge_notes="trust me"), "judge_notes"),
    ("judge_notes_from an unknown id", mutated(RISK, judge_notes_from=["T99"]), "T99"),
    ("judge_notes_from an item without notes", mutated(RISK, judge_notes_from=["N1"]), "N1"),
    ("a refusal with an expectation", mutated(REFUSAL, expect={"value": 1}), "refusal"),
    ("an injection with no mechanical answer gate", mutated(INJECTION, expect=None, expect_from=DELETE, facts=DELETE, derivation=DELETE, answer_forbidden=DELETE), "injection"),
    ("an empty canary", mutated(INJECTION, answer_forbidden=[""]), "answer_forbidden"),
    ("an injection asserting a company disclosure claim with no judge_notes_from",
     mutated(INJECTION, q="Nvidia disclosed the BIS 50% affiliates rule in its 10-K. Summarize it.", judge_notes_from=DELETE), "judge_notes_from"),
])
def test_the_validator_catches(label, items, needle):
    found = problems(items)
    assert found and any(needle in p for p in found), (label, found)


def test_a_duplicate_id_is_caught():
    items = copy.deepcopy(RAW)
    items[1]["id"] = items[0]["id"]
    assert any("duplicate" in p for p in problems(items))


def test_a_fact_bound_to_the_wrong_position_of_a_multi_value_main_item_is_caught():
    """``benchmark:<id>`` alone only checked that a fact's value was SOME value the main item carries, so a value copied from the
    WRONG metric of the same item (e.g. NG12's R&D figure claimed as its revenue) would incorrectly validate. ``:<index>`` binds
    the provenance to the SPECIFIC metric/position (M3 R3 review, LOW finding 13)."""
    ng12_values = MAIN_BY_ID["NG12"]["expect"]["values"]
    mismatched = {"id": "xbrl:2488:revenue:2024-12-28", "value": ng12_values[1], "from": "benchmark:NG12:0"}      # position 0 is revenue
    it = {**RAW[NUMERIC], "facts": [mismatched], "expect": {"value": ng12_values[1]}, "derivation": "level"}
    found = problems([it])
    assert found and any("not the value" in p for p in found), found


def test_a_fact_bound_to_a_single_value_main_item_needs_no_index_and_rejects_one():
    n2 = MAIN_BY_ID["N2"]["expect"]["value"]
    with_index = {"id": "xbrl:1045810:revenue:2026-01-25", "value": n2, "from": "benchmark:N2:0"}
    it = {**RAW[NUMERIC], "facts": [with_index], "expect": {"value": n2}, "derivation": "level"}
    found = problems([it])
    assert found and any("single value" in p for p in found), found


def test_a_fact_naming_no_position_of_a_multi_value_main_item_is_refused():
    unindexed = {"id": "xbrl:2488:revenue:2024-12-28", "value": MAIN_BY_ID["NG12"]["expect"]["values"][0], "from": "benchmark:NG12"}
    it = {**RAW[NUMERIC], "facts": [unindexed], "expect": {"value": MAIN_BY_ID["NG12"]["expect"]["values"][0]}, "derivation": "level"}
    found = problems([it])
    assert found and any("name which one" in p for p in found), found


# --- the lake check itself -----------------------------------------------------------------------------------------------------

def test_the_lake_check_flags_a_missing_fact_and_a_wrong_value():
    pd = pytest.importorskip("pandas")
    items = [{"id": "A99", "facts": [{"id": "xbrl:5:revenue:2025-01-01", "value": 100.0, "from": "lake"},
                                     {"id": "xbrl:5:revenue:2024-01-01", "value": 50.0, "from": "lake"},
                                     {"id": "xbrl:6:revenue:2025-01-01", "value": 1.0, "from": "lake"}]}]
    metrics = pd.DataFrame([{"cik": 5, "metric": "revenue", "end": "2025-01-01", "val": 100.0},
                            {"cik": 5, "metric": "revenue", "end": "2024-01-01", "val": 60.0}])
    found = ae.lake_mismatches(items, metrics)
    assert len(found) == 2 and any("xbrl:5:revenue:2024-01-01" in p and "60" in p for p in found)
    assert any("xbrl:6:revenue:2025-01-01" in p and "not in the lake" in p for p in found)


# --- assembling the run set ----------------------------------------------------------------------------------------------------

def test_the_run_set_is_the_agent_questions_then_the_main_ones_each_tagged_with_its_split():
    run = ae.build_run_set(RAW, MAIN)
    assert len(run) == len(RAW) + len(MAIN)
    assert [it["split"] for it in run] == ["agent"] * len(RAW) + ["main"] * len(MAIN)
    assert all(it["id"] for it in run) and len({it["id"] for it in run}) == len(run)
    assert any(it.get("judge_notes") for it in run if it["split"] == "agent")


def test_the_run_set_can_be_agent_only_or_limited():
    assert {it["split"] for it in ae.build_run_set(RAW, MAIN, include_main=False)} == {"agent"}
    assert len(ae.build_run_set(RAW, MAIN, limit=5)) == 5


def test_a_main_id_reused_by_an_agent_question_is_refused():
    with pytest.raises(ValueError, match="N1"):
        ae.build_run_set([{**RAW[0], "id": "N1"}], MAIN)


def test_the_resolved_items_are_copies_and_the_file_is_never_mutated():
    before = copy.deepcopy(RAW)
    ae.build_run_set(RAW, MAIN)
    assert before == RAW
