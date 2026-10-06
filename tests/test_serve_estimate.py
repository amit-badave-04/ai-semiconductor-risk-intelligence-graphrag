"""Per-ask spend estimates for the estimate-based daily cap (serve/estimate.py; M5a decision 5, plan step 0-I4).

The expected numbers here are written out as literals on purpose (token counts, prices, the arithmetic in the comments),
never read back from the module: a test that imports the module's constants to compute its own expectation proves
nothing. Changing an assumption in the module therefore means changing the matching literal here, which is the point:
these numbers decide when paid asks stop for the day.

Runs on the CI ``serve-shipped`` job (no pandas, no Neo4j): ``data/processed`` is git-ignored, so the recorded paid runs
are pinned below as constants with their provenance, and the tests that re-read the run files skip when the files are
absent. ``fly.toml`` is read with tomllib; no ``.env`` file is ever opened (``Settings(_env_file=None, ...)``).
"""

import inspect
import json
import logging
import math
import re
import subprocess
import sys
import tomllib
from fractions import Fraction
from pathlib import Path

import pytest

from semigraph.config import Settings
from semigraph.serve import estimate as est

ROOT = Path(__file__).resolve().parents[1]
LUNA, SONNET = "openai/gpt-6-luna", "anthropic/claude-sonnet-5"
ASK_TYPES = ("hybrid", "vector", "agent", "workspace")

# USD per million tokens, written here independently of llm_shape.KNOWN_PRICES_PER_MTOK (a test pins the two together).
LUNA_IN, LUNA_OUT = Fraction("0.10"), Fraction("0.50")
SONNET_IN, SONNET_OUT = Fraction(2), Fraction(10)

# What the estimate assumes about the prompt, per ask type (the arithmetic is in the comments; question = 500 chars).
#   chars per token 2.5; one excerpt 12,000 chars + 64 framing; 8 excerpts = 96,512; three companies' graph blocks at
#   60,000 = 180,000; templates 9,200 (answer) / 10,300 (workspace).
#   vector:    9,200 + 500 + 96,512                                   = 106,212 chars -> 42,485 tokens
#   hybrid:    9,200 + 500 + 96,512 + 180,000                         = 286,212 chars -> 114,485 tokens
#   workspace: 10,300 + 500 + 276,512 + 6 x (1,800 + 64) = 11,184     = 298,496 chars -> 119,399 tokens
#   agent:     9,200 + 500 + 276,512 + 8 more excerpts (96,512) + 40 edges x 300 + 6 risks x 700 + 4 computed x 400
#                                                                     = 400,524 chars -> 160,210 tokens
PROMPT_TOKENS = {"vector": 42_485, "hybrid": 114_485, "workspace": 119_399, "agent": 160_210}
OUT_TOKENS = 2_400                    # fly.toml LLM_ANSWER_MAX_TOKENS
PLANNER_IN_TOKENS, PLANNER_OUT_TOKENS = 23_400, 400   # (8,000 fixed + 500 question + 50,000 growth) / 2.5; the cap
PLANNER_CALLS = 3                     # agent_max_model_calls
DRAFT_CALLS, STRONG_CALLS = 1, 2      # the draft is forced to one attempt; the strong stream keeps the default two

# The recorded paid runs (provenance in each comment; the file-reading test below re-derives them when the files exist).
V2E_ROW_MAX_USD = 0.06578             # eval_deployed.v2e.jsonl, row R1 (escalated); 60 rows, hybrid, Luna + Sonnet
V2E_STRONG_ROW_MAX_USD = 0.060608     # same file, row T14 (routed straight to Sonnet)
V2E_MEAN_ROW_USD = 0.014616433333333335   # artifacts/eval_report.v2e-deployed.json avg_cost_usd (60 rows)
VECTOR_ROW_MAX_USD = 0.040676         # eval_runs.v2-baseline.jsonl, system=vector, row Q2: Sonnet only, 1,200-token cap
VECTOR_LONGEST_CONTEXT_CHARS = 48_925     # same rows, row Q2
AGENT_ROW_MAX_USD = 0.070735          # eval_agent.v2.jsonl, row R2 (escalated); 82 rows; cost_usd includes the planner
AGENT_MEAN_ROW_USD = 0.014459353658536586   # same file, mean cost_usd
AGENT_PLANNER_MAX_USD = 0.000732      # largest planner_cost_usd of the two agent runs (eval_agent.jsonl row NG15)
LARGEST_RECORDED_CALL_TOKENS = 27_184   # eval_agent.v2.jsonl row T14: the largest single-call prompt of any run
LARGEST_RECORDED_CONTEXT_CHARS = 64_427   # eval_runs.jsonl, system=hybrid, row R2: the longest recorded context string
LARGEST_CHUNK_CHARS = 10_217          # data/processed/chunks, 5,894 chunks of 13 filers: the longest (AMD)

CAP_MICRO, COUNT_CAP = 10_000_000, 150   # decision 5: $10 a day, 150 paid asks a day


def make_settings(**override) -> Settings:
    """Every field the estimate reads is explicit, so neither a stray environment variable nor a .env changes a test."""
    base = {"answer_model": LUNA, "escalation_model": SONNET, "agent_planner_model": LUNA,
            "llm_answer_max_tokens": OUT_TOKENS, "llm_input_price_per_mtok": 2.0, "llm_output_price_per_mtok": 10.0,
            "agent_max_model_calls": PLANNER_CALLS, "max_question_chars": 500}
    return Settings(_env_file=None, **{**base, **override})


LIVE_FIELDS = ("answer_model", "escalation_model", "agent_planner_model", "llm_answer_max_tokens",
               "llm_input_price_per_mtok", "llm_output_price_per_mtok", "agent_max_model_calls", "max_question_chars")


def live_settings() -> Settings:
    """The production configuration: fly.toml's [env] block, and the code default of every field it does not set.

    Fly secrets cannot be read here, so a secret that overrides one of these fields is not seen by this test."""
    env = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))["env"]
    values = {name: env.get(name.upper(), Settings.model_fields[name].default) for name in LIVE_FIELDS}
    return Settings(_env_file=None, **values)


def cost_micro(tokens_in: int, tokens_out: int, price_in: Fraction, price_out: Fraction, calls: int = 1) -> Fraction:
    """Exact micro-dollars: tokens x (USD per million tokens) is micro-dollars with no scaling."""
    return calls * (tokens_in * price_in + tokens_out * price_out)


def expected_micro(ask_type: str, *, out_tokens: int = OUT_TOKENS) -> int:
    """The estimate of the live shape (Luna draft, Sonnet strong, Luna planner) from the literals above, rounded up."""
    total = (cost_micro(PROMPT_TOKENS[ask_type], out_tokens, LUNA_IN, LUNA_OUT, DRAFT_CALLS)
             + cost_micro(PROMPT_TOKENS[ask_type], out_tokens, SONNET_IN, SONNET_OUT, STRONG_CALLS))
    if ask_type == "agent":
        total += cost_micro(PLANNER_IN_TOKENS, PLANNER_OUT_TOKENS, LUNA_IN, LUNA_OUT, PLANNER_CALLS)
    return math.ceil(total)


def asks_granted(cost: int, estimate: int, *, in_flight: int = 0) -> int:
    """How many sequential asks of ``cost`` micro-dollars each the reserve rule admits (plan section 2, in-process
    backend): an ask is denied when the day's spend plus its own estimate exceeds the cap (the spend holds settled asks
    at their ACTUAL cost and each lease still in flight at its estimate), or when the count cap is reached."""
    granted = 0
    while granted < COUNT_CAP and granted * cost + in_flight * estimate + estimate <= CAP_MICRO:
        granted += 1
    return granted


# --- the arithmetic, from first principles ---------------------------------------------------------------------------

@pytest.mark.parametrize("ask_type", ASK_TYPES)
def test_prompt_ceiling_is_the_documented_assumption(ask_type):
    assert est.prompt_tokens(ask_type, make_settings()) == PROMPT_TOKENS[ask_type]


@pytest.mark.parametrize("ask_type", ASK_TYPES)
def test_live_shape_estimate_equals_the_hand_computation(ask_type):
    assert est.estimate_micro(ask_type, make_settings()) == expected_micro(ask_type)


def test_hand_computed_live_numbers_are_the_ones_reported_to_the_owner():
    # hybrid: Luna 114,485 x 0.10 + 2,400 x 0.50 = 12,648.5; Sonnet 2 x (114,485 x 2 + 2,400 x 10) = 505,940 -> 518,589
    assert [expected_micro(t) for t in ("hybrid", "vector", "workspace")] == [518_589, 223_389, 538_736]
    assert expected_micro("agent") == 713_681     # 17,221 + 688,840 + planner 3 x 2,540 = 7,620


def test_the_ask_types_are_the_four_the_route_meters():
    assert est.ASK_TYPES == ASK_TYPES


def test_the_listed_prices_are_the_ones_the_hand_computation_uses():
    from semigraph.llm_shape import KNOWN_PRICES_PER_MTOK
    assert KNOWN_PRICES_PER_MTOK[LUNA] == (float(LUNA_IN), float(LUNA_OUT))
    assert KNOWN_PRICES_PER_MTOK[SONNET] == (float(SONNET_IN), float(SONNET_OUT))


@pytest.mark.parametrize("ask_type", ASK_TYPES)
def test_estimate_covers_the_largest_recorded_prompt_on_both_models_at_the_output_cap(ask_type):
    """An independent floor: the biggest prompt any recorded run sent, on the draft AND the strong model, each with a
    full 2,400-token answer, plus (agent) three planner calls of the size the runs showed (about 2,300 input tokens)."""
    floor = (cost_micro(LARGEST_RECORDED_CALL_TOKENS, OUT_TOKENS, LUNA_IN, LUNA_OUT)
             + cost_micro(LARGEST_RECORDED_CALL_TOKENS, OUT_TOKENS, SONNET_IN, SONNET_OUT))
    if ask_type == "agent":
        floor += cost_micro(2_300, 100, LUNA_IN, LUNA_OUT, 3)
    assert est.estimate_micro(ask_type, make_settings()) >= floor


def test_every_prompt_ceiling_leaves_headroom_over_the_largest_recorded_call():
    for ask_type in ASK_TYPES:
        assert est.prompt_tokens(ask_type, make_settings()) >= 1.5 * LARGEST_RECORDED_CALL_TOKENS, ask_type


def test_the_sec_context_ceiling_is_above_the_longest_recorded_context():
    # the vector ceiling is eight excerpts at their longest possible size (a bound, not an assumption): about twice the
    # longest recorded vector context (48,925 characters, whose excerpts are shorter than the longest chunk)
    ceiling = {t: est.context_chars(t) for t in ASK_TYPES}
    assert ceiling["hybrid"] >= 2 * LARGEST_RECORDED_CONTEXT_CHARS
    assert ceiling["vector"] >= 1.9 * VECTOR_LONGEST_CONTEXT_CHARS


def test_no_ceiling_reaches_the_long_context_tier():
    """ASSUMPTION, unverified for Sonnet 5: providers price a prompt above about 200,000 tokens in a dearer tier (the
    KNOWN_PRICES comment records one for Luna above short contexts). Every ceiling stays below it."""
    assert max(est.prompt_tokens(t, make_settings()) for t in ASK_TYPES) < 200_000


# --- the company-count assumption: where the ceiling is NOT a bound --------------------------------------------------

def test_the_ceiling_assumes_three_companies_per_question():
    assert est.ANCHORS_ASSUMED == 3 and est.GRAPH_CHARS_PER_ANCHOR == 60_000


def _temporal_block_at_the_caps() -> str:
    """One company's temporal block through the real layout code with every list full: 26 items (headlines at their
    240-character cut, three ids each side), 16 passages (quotes at their 450-character cut). The earlier wording of a
    reworded item is NOT cut by the layout: 240 characters is an assumption about real headlines, not a cap."""
    from semigraph.retrieval import context_layout, retriever

    def ids(n):
        return [f"0001045810-26-{n:06d}:I.1A:{i:04d}" for i in range(3)]

    accessions = {"older_accession": "0001045810-25-000023", "newer_accession": "0001045810-26-000021"}
    pair = {"company": "NVIDIA Corporation", "cik": 1, "older_form": "10-K", "older_date": "2025-02-26",
            "newer_form": "10-K", "newer_date": "2026-02-25", "compared": True, **accessions,
            "totals": dict.fromkeys(retriever.TEMPORAL_CAPS, 99),
            "passage_totals": dict.fromkeys(retriever.PASSAGE_CAPS, 99)}
    items = [{"change": change, "headline": "H" * 240, "older_headline": "O" * 240, "decided_by": "embedding+lexical",
              "older_chunk_ids": ids(i), "newer_chunk_ids": ids(i + 50), "cik": 1, "unit_kind": "headline",
              **accessions}
             for change, cap in retriever.TEMPORAL_CAPS.items() for i in range(cap)]
    passages = [{"kind": kind, "item_headline": "P" * 100, "text": "T" * 600, "counterpart_text": "C" * 600,
                 "chunk_ids": ids(i), "counterpart_chunk_ids": ids(i + 70), "cik": 1, "section_id": "I.1A",
                 "item_unit_kind": "headline", **accessions}
                for kind, cap in retriever.PASSAGE_CAPS.items() for i in range(cap)]
    block, _ = context_layout.temporal_block(items, [pair], passages)
    return block


def test_the_per_anchor_graph_allowance_covers_the_temporal_block_at_its_caps():
    """MEASURED through the real layout: the temporal block of one company at its caps is 24,608 characters. Allowed on
    top (rounded up, not measured): 8 rules x 300, 4 metrics x 11 rows x 200 (the graph loads the curated key_metrics
    tables: capex, net_income, revenue and rnd, at most 4 per company), and 20,000 for the company edges (uncapped,
    two hops) and the risk lines. The sum has to fit the 60,000 the estimate allows per company, so raising a cap in
    the retriever without revisiting the estimate fails here."""
    temporal = len(_temporal_block_at_the_caps())
    assert temporal > 20_000, "the synthetic block must really be near the caps for this test to mean anything"
    rules, metrics, edges_and_risks = 8 * 300, 4 * 11 * 200, 20_000
    assert temporal + rules + metrics + edges_and_risks <= est.GRAPH_CHARS_PER_ANCHOR


def test_a_question_naming_every_filer_is_priced_above_its_estimate(monkeypatch):
    """THE ESTIMATE IS NOT A BOUND on the company count. 13 filers have filing text and a 500-character question can
    name all 13 (26 company ids are detectable): the same arithmetic with 13 companies gives 886,212 characters ->
    354,485 tokens (above the 200,000-token tier the next test assumes away) -> $1.50 for a hybrid ask, 2.9 times the
    three-company figure. Nothing in the writer stops it."""
    three = est.estimate_micro("hybrid", make_settings())
    monkeypatch.setattr(est, "ANCHORS_ASSUMED", 13)
    thirteen = est.estimate_micro("hybrid", make_settings())
    # Luna 354,485 x 0.10 + 1,200 = 36,648.5; Sonnet 2 x (354,485 x 2 + 24,000) = 1,465,940 -> 1,502,588.5
    assert thirteen == 1_502_589
    assert round(thirteen / three, 1) == 2.9
    assert est.prompt_tokens("hybrid", make_settings()) == 354_485


# --- the structure: escalation, planner, workspace -------------------------------------------------------------------

def test_an_escalation_model_adds_the_strong_models_full_input_and_output():
    with_escalation = est.estimate("hybrid", make_settings())
    parts = {c.name: c for c in with_escalation.components}
    assert set(parts) == {"draft", "strong"}
    strong = parts["strong"]
    assert (strong.model, strong.input_tokens, strong.output_tokens, strong.calls) == (
        SONNET, 114_485, OUT_TOKENS, STRONG_CALLS)
    assert strong.micro == math.ceil(cost_micro(114_485, OUT_TOKENS, SONNET_IN, SONNET_OUT, STRONG_CALLS))
    draft = parts["draft"]
    assert (draft.model, draft.input_tokens, draft.output_tokens, draft.calls) == (
        LUNA, 114_485, OUT_TOKENS, DRAFT_CALLS)
    assert est.estimate_micro("hybrid", make_settings(escalation_model="")) < with_escalation.micro


def test_without_an_escalation_model_the_answer_model_is_the_only_call_and_keeps_the_stream_retry():
    only = est.estimate("hybrid", make_settings(escalation_model=""))
    assert [c.name for c in only.components] == ["answer"]
    assert only.components[0].calls == STRONG_CALLS and only.components[0].model == LUNA
    assert only.micro == math.ceil(cost_micro(114_485, OUT_TOKENS, LUNA_IN, LUNA_OUT, STRONG_CALLS))


def test_an_escalation_model_equal_to_the_answer_model_is_one_model_in_both_roles():
    """answerer.stream_answer_for_prompt drops the escalation when it equals the answering model (the documented
    rollback)."""
    same = est.estimate("hybrid", make_settings(answer_model=SONNET, escalation_model=SONNET))
    assert [c.name for c in same.components] == ["answer"]
    assert same.micro == est.estimate_micro("hybrid", make_settings(answer_model=SONNET, escalation_model=""))


def test_the_agent_estimate_includes_the_planner_calls():
    agent = est.estimate("agent", make_settings())
    planner = {c.name: c for c in agent.components}["planner"]
    assert (planner.model, planner.calls, planner.input_tokens, planner.output_tokens) == (
        LUNA, PLANNER_CALLS, PLANNER_IN_TOKENS, PLANNER_OUT_TOKENS)
    exact = cost_micro(PLANNER_IN_TOKENS, PLANNER_OUT_TOKENS, LUNA_IN, LUNA_OUT, PLANNER_CALLS)
    assert planner.micro == math.ceil(exact)
    # one planner call is 23,400 x 0.10 + 400 x 0.50 = 2,540 micro-dollars exactly, so two more calls add exactly 5,080
    one_call = est.estimate_micro("agent", make_settings(agent_max_model_calls=1))
    assert est.estimate_micro("agent", make_settings()) - one_call == 2 * 2_540
    hybrid_parts = {c.name for c in est.estimate("hybrid", make_settings()).components}
    assert hybrid_parts == {"draft", "strong"}      # no planner outside the agent


def test_the_planner_is_priced_at_its_own_model():
    dear = est.estimate_micro("agent", make_settings(agent_planner_model=SONNET))
    assert dear - est.estimate_micro("agent", make_settings()) > 100_000      # 3 calls x 23,400 tokens at $2 is ~$0.14


def test_a_workspace_ask_costs_at_least_a_hybrid_ask_and_adds_the_document_excerpts():
    s = make_settings()
    assert est.estimate_micro("workspace", s) > est.estimate_micro("hybrid", s)
    assert est.context_chars("workspace") - est.context_chars("hybrid") == 6 * (1_800 + 64)


def test_ceilings_are_ordered_by_how_much_each_ask_can_carry():
    s = make_settings()
    assert est.estimate_micro("vector", s) < est.estimate_micro("hybrid", s) < est.estimate_micro("workspace", s)
    assert est.estimate_micro("hybrid", s) < est.estimate_micro("agent", s)


def test_an_unknown_ask_type_is_an_error():
    with pytest.raises(ValueError):
        est.estimate("graph", make_settings())


# --- monotonic, rounding, finite -------------------------------------------------------------------------------------

@pytest.mark.parametrize("ask_type", ASK_TYPES)
def test_raising_the_answer_token_budget_raises_the_estimate_strictly(ask_type):
    values = [est.estimate_micro(ask_type, make_settings(llm_answer_max_tokens=n)) for n in (1_200, 2_400, 4_800)]
    assert values[0] < values[1] < values[2]
    assert values[1] == expected_micro(ask_type)


def test_the_answer_token_budget_is_priced_on_every_call_that_can_produce_an_answer():
    low, high = (est.estimate_micro("hybrid", make_settings(llm_answer_max_tokens=n)) for n in (1_000, 2_000))
    # +1,000 output tokens: Luna once at $0.50 (500), Sonnet twice at $10 per million tokens (20,000): exactly 20,500
    assert high - low == 20_500


def test_micro_dollars_round_up_to_an_integer():
    s = make_settings(answer_model="acme/unknown-1", escalation_model="", llm_input_price_per_mtok=0.333333,
                      llm_output_price_per_mtok=0.777777, llm_answer_max_tokens=1_001, max_question_chars=777)
    e = est.estimate("hybrid", s)
    exact = sum(cost_micro(c.input_tokens, c.output_tokens, Fraction("0.333333"), Fraction("0.777777"), c.calls)
                for c in e.components)
    assert isinstance(e.micro, int) and not isinstance(e.micro, bool)
    assert exact != int(exact), "the case must actually need rounding"
    assert e.micro == math.ceil(exact) and exact <= e.micro < exact + 1
    assert est.estimate_micro("hybrid", s) == e.micro and est.estimate_usd("hybrid", s) == e.micro / 1_000_000


def test_a_price_with_more_than_six_decimals_rounds_up_never_down():
    s = make_settings(answer_model="acme/unknown-1", escalation_model="", llm_input_price_per_mtok=0.1234567,
                      llm_output_price_per_mtok=0.0000001)
    e = est.estimate("vector", s)
    c = e.components[0]
    assert c.input_price == 123_457 and c.output_price == 1      # micro-dollars per million tokens, ceiling
    exact = c.calls * (c.input_tokens * Fraction("0.1234567") + c.output_tokens * Fraction("0.0000001"))
    assert e.micro >= math.ceil(exact)


@pytest.mark.parametrize("ask_type", ASK_TYPES)
def test_estimates_are_finite_positive_and_the_two_functions_agree(ask_type):
    s = live_settings()
    micro, usd = est.estimate_micro(ask_type, s), est.estimate_usd(ask_type, s)
    assert isinstance(micro, int) and micro > 0
    assert math.isfinite(usd) and usd > 0 and usd == micro / 1_000_000


def test_a_zero_estimate_is_refused_because_the_cap_would_never_bind():
    s = make_settings(answer_model="acme/unknown-1", escalation_model="", llm_input_price_per_mtok=0.0,
                      llm_output_price_per_mtok=0.0)
    with pytest.raises(ValueError, match="not positive"):
        est.estimate("hybrid", s)


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_a_negative_or_non_finite_configured_price_is_refused(bad):
    s = make_settings(answer_model="acme/unknown-1", escalation_model="", llm_input_price_per_mtok=bad)
    with pytest.raises(ValueError, match="price"):
        est.estimate("hybrid", s)


# --- unknown and mock models -----------------------------------------------------------------------------------------

def test_an_unknown_model_is_flagged_and_priced_at_the_configured_list_prices():
    s = make_settings(answer_model="acme/unknown-1", escalation_model="", llm_input_price_per_mtok=3.0,
                      llm_output_price_per_mtok=9.0)
    e = est.estimate("hybrid", s)
    assert e.unpriced == ("acme/unknown-1",)
    assert (e.components[0].input_price, e.components[0].output_price) == (3_000_000, 9_000_000)
    assert e.micro == math.ceil(cost_micro(114_485, OUT_TOKENS, Fraction(3), Fraction(9), STRONG_CALLS))


def test_known_models_are_not_flagged_and_each_unknown_model_is_flagged_once():
    assert est.estimate("agent", make_settings()).unpriced == ()
    s = make_settings(answer_model="acme/a", escalation_model="acme/b", agent_planner_model="acme/a")
    assert est.estimate("agent", s).unpriced == ("acme/a", "acme/b")


def test_the_configured_prices_are_not_used_for_a_model_with_a_listed_price():
    cheap = est.estimate_micro("hybrid", make_settings(llm_input_price_per_mtok=0.0, llm_output_price_per_mtok=0.0))
    assert cheap == est.estimate_micro("hybrid", make_settings())


@pytest.mark.parametrize("ask_type", ASK_TYPES)
def test_mock_models_are_priced_like_the_real_models_of_their_role(ask_type):
    mock = make_settings(answer_model="openai/mock-draft", escalation_model="openai/mock-strong",
                         agent_planner_model="openai/mock-planner")
    assert est.estimate_micro(ask_type, mock) == est.estimate_micro(ask_type, make_settings())
    assert est.estimate(ask_type, mock).unpriced == ()


def test_mock_models_that_name_a_real_model_are_priced_as_that_model():
    named = make_settings(answer_model="openai/mock-luna", escalation_model="openai/mock-sonnet",
                          agent_planner_model="openai/mock-luna")
    assert est.estimate_micro("agent", named) == est.estimate_micro("agent", make_settings())
    swapped = make_settings(answer_model="openai/mock-sonnet", escalation_model="openai/mock-luna")
    assert est.estimate_micro("hybrid", swapped) == est.estimate_micro(
        "hybrid", make_settings(answer_model=SONNET, escalation_model=LUNA))


def test_a_mock_model_that_is_the_only_answer_model_is_priced_as_the_strong_model():
    only = make_settings(answer_model="openai/mock-x", escalation_model="")
    assert est.estimate_micro("hybrid", only) == est.estimate_micro(
        "hybrid", make_settings(answer_model=SONNET, escalation_model=""))


# --- the recorded paid runs ------------------------------------------------------------------------------------------

def test_every_estimate_covers_the_dearest_recorded_ask_of_its_kind():
    s = live_settings()
    micro = {t: est.estimate_micro(t, s) for t in ASK_TYPES}
    hybrid_worst = round(max(V2E_ROW_MAX_USD, V2E_STRONG_ROW_MAX_USD) * 1_000_000)
    assert micro["hybrid"] >= hybrid_worst
    # the vector row was answered by Sonnet alone with a 1,200-token cap: reprice it at the live 2,400-token cap
    vector_at_live_cap = round((VECTOR_ROW_MAX_USD + (OUT_TOKENS - 1_200) * 10 / 1_000_000) * 1_000_000)
    assert micro["vector"] >= vector_at_live_cap
    # a workspace ask escalates on the same SEC context as a hybrid ask (the recorded workspace asks were all
    # cheap-model answers, see the next test), so the hybrid maximum is its nearest recorded kind
    assert micro["workspace"] >= hybrid_worst
    assert micro["agent"] >= round(AGENT_ROW_MAX_USD * 1_000_000)      # cost_usd already includes the planner


def test_the_workspace_estimate_covers_the_recorded_workspace_asks():
    smoke = json.loads((ROOT / "artifacts" / "workspace_smoke.json").read_text(encoding="utf-8"))
    asks = [step for name, step in smoke["steps"].items() if name.startswith("ask")]
    assert len(asks) == 3
    worst_usd = max(a["cost_usd"] for a in asks)
    worst_chars = max(a["context_chars"] for a in asks)
    assert est.estimate_usd("workspace", live_settings()) >= worst_usd
    assert est.context_chars("workspace") >= 2 * worst_chars
    assert est.prompt_tokens("workspace", live_settings()) >= 2 * max(a["usage"]["prompt_tokens"] for a in asks)


RUN_FILES = ROOT / "data" / "processed"
RUN_FILES_PRESENT = all((RUN_FILES / name).exists() for name in (
    "eval_deployed.v2e.jsonl", "eval_runs.v2-baseline.jsonl", "eval_runs.jsonl", "eval_agent.v2.jsonl"))


def _rows(name: str) -> list[dict]:
    return [json.loads(line) for line in (RUN_FILES / name).read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.skipif(not RUN_FILES_PRESENT,
                    reason="data/processed is git-ignored: the recorded paid runs exist only where they were run")
def test_the_pinned_maxima_are_still_what_the_run_files_say():
    v2e = _rows("eval_deployed.v2e.jsonl")
    top = max(v2e, key=lambda r: r["cost_usd"])
    assert (top["id"], top["cost_usd"]) == ("R1", V2E_ROW_MAX_USD)
    assert max(r["cost_usd"] for r in v2e if r["routed"] == "strong") == V2E_STRONG_ROW_MAX_USD
    assert sum(r["cost_usd"] for r in v2e) / len(v2e) == pytest.approx(V2E_MEAN_ROW_USD)
    for name in ("eval_deployed.jsonl", "eval_deployed.v2.jsonl", "eval_runs.v2-deployed.jsonl",
                 "eval_runs.v2b-deployed.jsonl", "eval_runs.v2c-deployed.jsonl", "eval_runs.v2d-deployed.jsonl"):
        assert max(r["cost_usd"] for r in _rows(name)) <= V2E_ROW_MAX_USD, name
    baseline = _rows("eval_runs.v2-baseline.jsonl")
    vector = [r for r in baseline if r["system"] == "vector"]
    assert max((r["cost_usd"], r["id"]) for r in vector) == (VECTOR_ROW_MAX_USD, "Q2")
    assert max(len(r["context"]) for r in vector) == VECTOR_LONGEST_CONTEXT_CHARS
    hybrid_contexts = [len(r["context"]) for r in _rows("eval_runs.jsonl") if r["system"] == "hybrid"]
    assert max(hybrid_contexts) == LARGEST_RECORDED_CONTEXT_CHARS
    agent = _rows("eval_agent.v2.jsonl")
    assert max((r["cost_usd"], r["id"]) for r in agent) == (AGENT_ROW_MAX_USD, "R2")
    assert sum(r["cost_usd"] for r in agent) / len(agent) == pytest.approx(AGENT_MEAN_ROW_USD)
    runs = [_rows(name) for name in ("eval_agent.v2.jsonl", "eval_agent.jsonl")]
    assert max(r["agent"]["planner_cost_usd"] for rows in runs for r in rows) == AGENT_PLANNER_MAX_USD
    per_call = max(r["usage"]["prompt_tokens"] / (2 if r["escalated"] else 1) for rows in runs for r in rows)
    assert per_call == LARGEST_RECORDED_CALL_TOKENS


def test_the_longest_ingested_chunk_is_the_excerpt_allowance_basis():
    pd = pytest.importorskip("pandas", reason="reading the chunk tables needs pandas (pipeline-time, not shipped)")
    files = sorted((RUN_FILES / "chunks").glob("*.parquet"))
    if not files:
        pytest.skip("data/processed is git-ignored: the chunk tables exist only on the machine that ingested them")
    longest = max(int(pd.read_parquet(f, columns=["text"])["text"].str.len().max()) for f in files)
    assert longest == LARGEST_CHUNK_CHARS <= est.CHUNK_TEXT_MAX_CHARS


# --- the live configuration ------------------------------------------------------------------------------------------

def test_the_live_configuration_is_what_the_estimates_assume():
    s = live_settings()
    assert (s.answer_model, s.escalation_model, s.llm_answer_max_tokens) == (LUNA, SONNET, OUT_TOKENS)
    assert s.agent_planner_model == LUNA and s.agent_max_model_calls == PLANNER_CALLS and s.max_question_chars == 500
    assert est.estimate_micro("hybrid", s) == expected_micro("hybrid")      # the live reading is the hand-computed one


def test_the_live_estimates_are_cents_not_dollars_and_not_zero():
    """The band found: from the dearest recorded ask (about 7 cents) to under a dollar. Hybrid about 52 cents, vector
    22, workspace 54, agent 71 (they carry the escalation's retry allowance and a context two to four times the
    largest recorded)."""
    s = live_settings()
    worst_recorded = {"hybrid": V2E_ROW_MAX_USD, "vector": VECTOR_ROW_MAX_USD, "workspace": V2E_ROW_MAX_USD,
                      "agent": AGENT_ROW_MAX_USD}
    for ask_type in ASK_TYPES:
        usd = est.estimate_usd(ask_type, s)
        assert worst_recorded[ask_type] < usd < 1.0, (ask_type, usd)
    assert round(est.estimate_usd("hybrid", s), 2) == 0.52


def test_which_cap_binds_first_the_150_ask_count_or_the_10_dollar_estimate():
    """The reserve rule denies an ask when spend + its estimate exceeds the cap, where a settled ask counts at its
    ACTUAL cost and only a lease in flight counts at its estimate. So the estimate is headroom, not a per-ask charge:
      - every ask at the estimate (a pile of in-flight leases): 150 hybrid asks would be $77.79, the cap would stop the
        20th, but the live in-flight cap is 2, so that never happens;
      - asks that settle at the recorded mean (1.5 cents): the 150-ask count binds first, with $2.19 spent;
      - asks that all cost the dearest straight-to-Sonnet ask (6.06 cents): still all 150 granted;
      - asks that all cost the dearest recorded ask (6.6 cents, a rejected draft plus the strong answer): the $10 cap
        binds first, at the 146th ask;
      - break-even: an average above 6.36 cents per ask makes the $10 cap bind before the count."""
    s = live_settings()
    hybrid, agent = est.estimate_micro("hybrid", s), est.estimate_micro("agent", s)
    assert (hybrid, agent) == (518_589, 713_681)
    assert 150 * hybrid / 1_000_000 == pytest.approx(77.79, abs=0.01)
    assert CAP_MICRO // hybrid == 19                       # at the estimate the 20th ask cannot reserve
    dearest, mean = round(V2E_ROW_MAX_USD * 1_000_000), round(V2E_MEAN_ROW_USD * 1_000_000)
    assert asks_granted(mean, hybrid) == COUNT_CAP
    assert 150 * mean / 1_000_000 == pytest.approx(2.19, abs=0.01)
    assert asks_granted(round(V2E_STRONG_ROW_MAX_USD * 1_000_000), hybrid) == COUNT_CAP
    assert asks_granted(dearest, hybrid) == 145            # the 146th is denied by the $10 cap
    assert asks_granted(dearest, hybrid, in_flight=1) == 137     # one other lease in flight (the live cap is 2)
    assert (CAP_MICRO - hybrid) // (COUNT_CAP - 1) == 63_633       # the break-even cost per ask: the dearest that still
    assert asks_granted(63_633, hybrid) == COUNT_CAP and asks_granted(63_634, hybrid) == COUNT_CAP - 1   # gets all 150
    agent_dearest, agent_mean = round(AGENT_ROW_MAX_USD * 1_000_000), round(AGENT_MEAN_ROW_USD * 1_000_000)
    assert asks_granted(agent_mean, agent) == COUNT_CAP and asks_granted(agent_dearest, agent) == 132


# --- the assumptions are the code's ----------------------------------------------------------------------------------

def test_the_attempt_counts_are_what_the_streams_do():
    from semigraph.retrieval import answerer, answerer_async
    sync_attempts = inspect.signature(answerer.TextStream.__init__).parameters["attempts"].default
    async_attempts = inspect.signature(answerer_async.AsyncTextStream.__init__).parameters["attempts"].default
    assert sync_attempts == async_attempts == est.STREAM_ATTEMPTS == 2
    assert answerer._draft_kwargs({})["attempts"] == est.DRAFT_ATTEMPTS == 1
    assert answerer_async._draft_kwargs({})["attempts"] == est.DRAFT_ATTEMPTS


def test_the_route_passes_the_configured_token_budget_and_escalation_model_to_every_writer():
    """Every ask type goes through the one call in stream_runtime: if it stops passing the configured budget the writers
    fall back to their own default (1,200) and the estimate's output term no longer describes what is billed."""
    source = (ROOT / "src" / "semigraph" / "serve" / "stream_runtime.py").read_text(encoding="utf-8")
    assert re.search(r"max_tokens=s\.llm_answer_max_tokens,\s*escalation_model=s\.escalation_model or None", source)


def test_the_retrieval_defaults_are_what_the_ceiling_counts():
    from semigraph.retrieval import retriever
    assert inspect.signature(retriever.hybrid_retrieve).parameters["k_chunks"].default == est.K_CHUNKS == 8
    assert inspect.signature(retriever.vector_retrieve).parameters["k"].default == est.K_CHUNKS
    assert retriever.ACTIVE_RISKS_TOP == est.ACTIVE_RISKS_TOP


def test_the_prompt_templates_fit_the_template_allowance():
    from semigraph.retrieval import answerer, workspace
    assert len(answerer.ANSWER_PROMPT) <= est.ANSWER_TEMPLATE_CHARS
    assert len(workspace.WORKSPACE_PROMPT) <= est.WORKSPACE_TEMPLATE_CHARS
    assert workspace.DEFAULT_K_DOC_CHUNKS == est.K_DOC_CHUNKS


def test_the_agent_caps_are_the_ones_the_agent_estimate_counts():
    from semigraph.agent import merge, planner, tools
    assert (merge.MAX_CHUNKS, merge.MAX_EDGES_ADDED, merge.MAX_RISKS, merge.MAX_COMPUTED) == (
        est.AGENT_MAX_CHUNKS, est.AGENT_MAX_EDGES_ADDED, est.AGENT_MAX_RISKS, est.AGENT_MAX_COMPUTED)
    assert planner.PLANNER_MAX_TOKENS == est.PLANNER_MAX_TOKENS
    fixed = len(planner.system_prompt()) + len(json.dumps(tools.tool_specs()))
    assert fixed <= est.PLANNER_FIXED_CHARS, "the planner's fixed prompt outgrew the allowance"


def test_the_upload_chunk_size_is_the_documents_allowance():
    pytest.importorskip("rapidfuzz", reason="uploads.units imports the aligner")
    from semigraph.uploads import units
    assert units.DEFAULT_MAX_CHARS == est.DOC_CHUNK_MAX_CHARS


def test_the_excerpt_allowance_rests_on_the_chunkers_token_cap():
    pytest.importorskip("pandas", reason="the chunker module is a pipeline-time module (pandas)")
    from semigraph.parsing import chunker
    assert chunker.MAX_TOKENS == 1_100       # the cap CHUNK_TEXT_MAX_CHARS was sized against (corpus maximum 10,217)


# --- boot log and import ---------------------------------------------------------------------------------------------

def test_boot_estimates_logs_one_info_line_per_ask_type_with_the_number_and_its_components(caplog):
    with caplog.at_level(logging.INFO, logger="semigraph.serve.estimate"):
        out = est.boot_estimates(live_settings())
    assert set(out) == set(ASK_TYPES)
    lines = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(lines) == 4 and not [r for r in caplog.records if r.levelno > logging.INFO]
    by_type = {t: next(r.getMessage() for r in lines if f"ask_type={t} " in r.getMessage()) for t in ASK_TYPES}
    hybrid = by_type["hybrid"]
    for fragment in ("usd=0.518589", "micro=518589", "prompt_tokens=114485", "anchors_assumed=3", LUNA, SONNET, "x2",
                     "unpriced=none"):
        assert fragment in hybrid, fragment
    assert "planner" in by_type["agent"] and "planner" not in hybrid
    row = out["agent"]
    assert row["micro"] == expected_micro("agent") and row["usd"] == row["micro"] / 1_000_000 and row["unpriced"] == []
    assert [c["name"] for c in row["components"]] == ["draft", "strong", "planner"]
    json.dumps(out)      # the dict is plain data: it can be logged or served as it is


def test_boot_estimates_warns_once_per_unpriced_model(caplog):
    s = make_settings(answer_model="acme/unknown-1", escalation_model="", agent_planner_model="acme/unknown-1")
    with caplog.at_level(logging.INFO, logger="semigraph.serve.estimate"):
        out = est.boot_estimates(s)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "acme/unknown-1" in warnings[0]
    assert out["hybrid"]["unpriced"] == ["acme/unknown-1"]
    hybrid_line = next(r.getMessage() for r in caplog.records if "ask_type=hybrid " in r.getMessage())
    assert "unpriced=['acme/unknown-1']" in hybrid_line


def _run_python(code: str) -> str:
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=ROOT,
                          check=True)
    return done.stdout.strip()


def test_importing_the_module_loads_no_heavy_dependency_and_reads_nothing():
    """The CI serve-shipped job has no pandas or sentence-transformers; the agent package stays unimported when the flag
    is off; and the module must be pure (no litellm, no answerer: their import reads prompt files)."""
    code = ("import sys; import semigraph.serve.estimate; "
            "bad = [m for m in ('pandas', 'sentence_transformers', 'litellm', 'neo4j', 'langgraph', 'semigraph.agent', "
            "'semigraph.retrieval.answerer') if m in sys.modules]; print(','.join(bad))")
    assert _run_python(code) == ""


def test_every_module_this_file_inspects_imports_without_pandas_or_sentence_transformers():
    """The serve-shipped CI job cannot import either (a ``None`` in sys.modules makes the import raise, as absence
    would): a transitive import of one in the modules checked above would fail there and pass on a developer machine."""
    pytest.importorskip("rapidfuzz", reason="uploads.units imports the aligner")
    code = ("import sys; sys.modules['pandas'] = None; sys.modules['sentence_transformers'] = None; "
            "import semigraph.serve.estimate, semigraph.llm_shape; "
            "from semigraph.retrieval import answerer, answerer_async, retriever, workspace, context_layout; "
            "from semigraph.agent import merge, planner, tools; from semigraph.uploads import units; print('ok')")
    assert _run_python(code) == "ok"
