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

from semigraph.artifacts import load_canonical_entities
from semigraph.config import Settings
from semigraph.serve import estimate as est

ROOT = Path(__file__).resolve().parents[1]
LUNA, SONNET = "openai/gpt-6-luna", "anthropic/claude-sonnet-5"
ASK_TYPES = ("hybrid", "vector", "agent", "workspace")

# USD per million tokens, written here independently of llm_shape.KNOWN_PRICES_PER_MTOK (a test pins the two together).
LUNA_IN, LUNA_OUT = Fraction("0.10"), Fraction("0.50")
SONNET_IN, SONNET_OUT = Fraction(2), Fraction(10)

# What the estimate assumes about the prompt, per ask type (the arithmetic is in the comments; question = 500 chars).
#   chars per token 2.5; one excerpt 12,000 chars + 64 framing; 8 excerpts = 96,512; the RELATIONSHIPS allowance 150
#   lines x 300 = 45,000 and 6 risk lines x 700 = 4,200 (49,200 shared by every company count); one anchor's blocks
#   42,000; the anchor cap 4 (retriever.MAX_ANCHORS) = 168,000; the note naming the dropped companies 300; templates
#   9,200 (answer) / 10,300 (workspace).
#   vector:    9,200 + 500 + 96,512                                   = 106,212 chars -> 42,485 tokens
#   hybrid:    9,200 + 500 + 96,512 + 49,200 + 168,000 + 300          = 323,712 chars -> 129,485 tokens
#   workspace: 10,300 + 500 + 96,512 + 49,200 + 168,000 + 300 + 6 x (1,800 + 64) = 11,184
#                                                                     = 335,996 chars -> 134,399 tokens
#   agent:     9,200 + 500 + 96,512 + 8 more excerpts (96,512) + 49,200 + 40 edges x 300 + 6 more risks x 700 + 4
#              computed x 400 + 4 companies' blocks (the agent covers no more companies than a plain ask: its tools
#              are refused past the anchor cap) x 50,000 + 300         = 470,024 chars -> 188,010 tokens
PROMPT_TOKENS = {"vector": 42_485, "hybrid": 129_485, "workspace": 134_399, "agent": 188_010}
OUT_TOKENS = 2_400                    # fly.toml LLM_ANSWER_MAX_TOKENS
PLANNER_IN_TOKENS, PLANNER_OUT_TOKENS = 23_400, 400   # (8,000 fixed + 500 question + 50,000 growth) / 2.5; the cap
PLANNER_CALLS = 3                     # agent_max_model_calls
MAX_ANCHORS = 4                       # retriever.MAX_ANCHORS: twice the most any recorded question names (2)
AGENT_COMPANIES = MAX_ANCHORS         # the agent's merge refuses a tool call past the anchor cap (agent/merge.py)
AGENT_PAIRS = 5                       # filing pairs per company the agent allowance covers (the graph holds at most 4)
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
            "agent_max_model_calls": PLANNER_CALLS, "agent_max_tool_calls": 4, "max_question_chars": 500}
    return Settings(_env_file=None, **{**base, **override})


LIVE_FIELDS = ("answer_model", "escalation_model", "agent_planner_model", "llm_answer_max_tokens",
               "llm_input_price_per_mtok", "llm_output_price_per_mtok", "agent_max_model_calls", "agent_max_tool_calls",
               "max_question_chars")


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
    # hybrid: Luna 129,485 x 0.10 + 2,400 x 0.50 = 14,148.5; Sonnet 2 x (129,485 x 2 + 2,400 x 10) = 565,940 -> 580,089
    assert [expected_micro(t) for t in ("hybrid", "vector", "workspace")] == [580_089, 223_389, 600_236]
    # agent: Luna 20,001 + Sonnet 2 x (188,010 x 2 + 24,000) = 800,040 + planner 7,620
    assert expected_micro("agent") == 827_661


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


def test_no_ceiling_reaches_the_long_context_tier_the_agents_included():
    """ASSUMPTION, unverified for Sonnet 5: providers price a prompt above about 200,000 tokens in a dearer tier (the
    KNOWN_PRICES comment records one for Luna above short contexts). Every ceiling stays below it with the anchor cap
    at 4 (the table test below shows where the plain ones would reach it). The agent's does too only because its tools
    are refused past the same cap: before that cap it priced 13 companies and was 368,010 tokens."""
    assert {t: est.prompt_tokens(t, make_settings()) for t in ASK_TYPES} == PROMPT_TOKENS
    assert max(PROMPT_TOKENS.values()) == PROMPT_TOKENS["agent"] < 200_000


@pytest.mark.parametrize("tool_calls", [0, 1, 4, 16])
def test_the_agent_ceiling_does_not_depend_on_how_many_tool_calls_it_may_make(tool_calls):
    """The tool-call limit used to scale the company count (4 companies per call); the cap makes it irrelevant."""
    settings = make_settings(agent_max_tool_calls=tool_calls)
    assert est.prompt_tokens("agent", settings) == PROMPT_TOKENS["agent"]
    assert est.estimate_micro("agent", settings) == expected_micro("agent")
    # the optional ``settings`` of the three public helpers is accepted and changes nothing
    assert est.context_chars("agent", settings) == est.context_chars("agent")
    assert est.company_blocks("agent", settings) == est.agent_company_blocks(settings) == est.agent_company_blocks()


# --- the anchor cap: where the ceiling is now a bound -----------------------------------------------------------------

ALL_26 = list(load_canonical_entities())


def test_the_estimate_reads_the_anchor_bound_from_the_retriever_and_assumes_none(monkeypatch):
    from semigraph.retrieval import retriever
    assert retriever.MAX_ANCHORS == est.max_anchors() == MAX_ANCHORS
    assert not hasattr(est, "ANCHORS_ASSUMED")
    before = {t: est.context_chars(t) for t in ASK_TYPES}
    monkeypatch.setattr(retriever, "MAX_ANCHORS", MAX_ANCHORS + 3)
    assert est.max_anchors() == MAX_ANCHORS + 3
    assert est.context_chars("hybrid") - before["hybrid"] == 3 * 42_000
    assert est.context_chars("agent") - before["agent"] == 3 * 50_000        # the agent's merge reads the same cap
    assert est.context_chars("vector") == before["vector"]                    # no company blocks at all
    assert est.agent_company_blocks() == est.company_blocks("agent") == MAX_ANCHORS + 3
    assert est.company_blocks("vector") == 0


def test_the_allowances_are_the_measured_worst_cases_rounded_up():
    """The measured figures are in the tests below; these are the numbers the estimate rests on."""
    assert (est.GRAPH_CHARS_PER_ANCHOR, est.AGENT_GRAPH_CHARS_PER_COMPANY) == (42_000, 50_000)
    assert (est.EDGE_LINES_ALLOWED, est.EDGE_LINE_CHARS, est.ANCHOR_NOTE_CHARS) == (150, 300, 300)


# The worst case, built from the retriever's own caps and rendered through the real code (answerer.build_blocks and
# render_prompt, workspace.build_workspace_prompt): every list full, every string at the longest the code or the corpus
# allows. Measured maxima it rests on: a RiskItem headline 542 characters (data/interim/risk_items; the layout cuts an
# item's label at 240 but NOT a reworded item's earlier wording), a Federal Register title 234 (built at 300), a chunk
# id 31 characters (built at 31), 19 annual rows of one metric of one company (data/processed/xbrl), 4 metrics.
QUESTION = "Q" * 500
EDGE_LINES_BUILT = 150                # the RELATIONSHIPS lines the estimate allows (graph-bounded, not capped in code)
OLDER_HEADLINE_CHARS, RULE_TITLE_CHARS, NOTICE_CHARS = 542, 300, 330


def _company(n: int) -> str:
    return f"Company {n:02d} " + "N" * 36        # 50 characters, distinct per company (a metric series is keyed by it)


def _ids(base: int) -> list[str]:
    return [f"0001045810-26-{(base + k) % 1_000_000:06d}:I.1A:{(base + k) % 10_000:04d}" for k in range(3)]


def _per_pair(caps: dict[str, int], pairs: int) -> dict[str, int]:
    return dict(caps) if pairs <= 1 else {kind: max(1, cap // pairs) for kind, cap in caps.items()}


def _temporal(cik: int, pairs: int, first: int = 0) -> tuple[list, list, list, list]:
    """One company's temporal layer with every list full: ``(items, pairs, passages, notices)``. The ``pairs`` pairs are
    numbered from ``first`` (their accessions and chunk ids differ from those of another ``first``)."""
    from semigraph.retrieval import retriever
    pair_rows, items, passages, seed = [], [], [], cik * 1_000 + first * 100
    for p in range(first, first + pairs):
        accessions = {"older_accession": f"0001045810-25-{p:06d}", "newer_accession": f"0001045810-26-{p:06d}"}
        pair_rows.append({"company": _company(cik), "cik": cik, "older_form": "10-K", "older_date": "2025-02-26",
                          "newer_form": "10-K", "newer_date": "2026-02-25", "compared": True,
                          "not_compared_reason": None, "selection": None if pairs == 1 else "multi",
                          "older_period_end": "2025-01-26", "newer_period_end": "2026-01-25",
                          "totals": dict.fromkeys(retriever.TEMPORAL_CAPS, 99),
                          "passage_totals": dict.fromkeys(retriever.PASSAGE_CAPS, 99), **accessions})
        for change, cap in _per_pair(retriever.TEMPORAL_CAPS, pairs).items():
            for i in range(cap):
                seed += 1
                items.append({"change": change, "headline": "H" * 240, "older_headline": "O" * OLDER_HEADLINE_CHARS,
                              "decided_by": "text_check", "older_chunk_ids": _ids(seed),
                              "newer_chunk_ids": _ids(seed + 500), "cik": cik,
                              "unit_kind": "paragraph" if i == 0 else "headline", **accessions})
        for kind, cap in _per_pair(retriever.PASSAGE_CAPS, pairs).items():
            for _ in range(cap):
                seed += 1
                passages.append({"kind": kind, "item_headline": "P" * 100, "text": "T" * 600,
                                 "counterpart_text": "C" * 600, "chunk_ids": _ids(seed),
                                 "counterpart_chunk_ids": _ids(seed + 700), "cik": cik, "section_id": "I.1A",
                                 "item_unit_kind": "headline", **accessions})
    notices = [{"cik": cik, "company": _company(cik), "text": "n" * NOTICE_CHARS}] if pairs > 1 else []
    return items, pair_rows, passages, notices


def _metrics(cik: int, rows: int) -> list[dict]:
    return [{"cik": cik, "company": _company(cik), "metric": metric, "value": 900_000_000_000.0 - y * 7e10,
             "unit": "USD", "period_start": f"{2024 - y}-01-27", "period_end": f"{2025 - y}-01-26"}
            for metric in ("capex", "net_income", "revenue", "rnd") for y in range(rows)]


def _rules(cik: int) -> list[dict]:
    from semigraph.retrieval import retriever
    return [{"source": _company(cik), "relation": "AFFECTED_BY", "target": "R" * RULE_TITLE_CHARS, "status": "Active",
             "quote": "q", "chunk_ids": [], "rule_id": f"2024-{10_000 + cik * 10 + j}", "date": f"2024-10-0{j + 1}",
             "url": "u", "kind": "rule", "link_source": "federal_register", "link_method": "keyword",
             "external": True} for j in range(retriever.RULES_PER_COMPANY)]


def worst_case_retrieval(companies: int, *, pairs: int = 1, metric_rows: int = 14, show_all: bool = False,
                         chunks: int = 8, risks: int = 6, edge_lines: int = EDGE_LINES_BUILT, extra_edges: int = 0,
                         computed: int = 0, dropped: list[str] | None = None) -> dict:
    """A retrieval dict at its caps for ``companies`` anchors (the shape ``hybrid_retrieve`` returns)."""
    from semigraph.retrieval import retriever
    named = list(range(2007, 2026)) if show_all else [2017, 2019, 2021, 2023]     # 4 named years, each with its prior
    r = {"anchors": {f"A{i}": i for i in range(1, companies + 1)}, "anchor_defaulted": False, "temporal": [],
         "metric_periods": {"years": named if companies else [], "dates": []}, "metrics": [], "temporal_pairs": [],
         "temporal_passages": [], "temporal_notices": [], "computed": ["c" * 400] * computed,
         "edges": [{"source": _company(90), "relation": "COMPETES_WITH", "target": _company(91), "status": "Active",
                    "quote": "q", "chunk_ids": _ids(i)} for i in range(edge_lines + extra_edges)]}
    for cik in range(1, companies + 1):
        items, pair_rows, passages, notices = _temporal(cik, pairs)
        r["edges"] += _rules(cik)
        r["metrics"] += _metrics(cik, metric_rows)
        r["temporal"] += items
        r["temporal_pairs"] += pair_rows
        r["temporal_passages"] += passages
        r["temporal_notices"] += notices
    if dropped:
        r["temporal_notices"] = [*r["temporal_notices"], retriever._dropped_notice(dropped)]
    r["risks"] = [{"company": _company(92), "category": "C" * 77, "summary": "S" * 348, "chunk_id": _ids(900 + i)[0]}
                  for i in range(risks)]
    r["chunks"] = [{"chunk_id": _ids(800 + i)[0], "text": "X" * 12_000, "score": 0.5} for i in range(chunks)]
    return r


def _prompt_chars(r: dict) -> int:
    from semigraph.retrieval import answerer
    return len(answerer.render_prompt(QUESTION, answerer.build_blocks(r)[0]))


def _company_chars(*, pairs: int, metric_rows: int, show_all: bool) -> int:
    """What one more company's rules, metrics and temporal block add to the rendered prompt."""
    def one(n):
        return _prompt_chars(worst_case_retrieval(n, pairs=pairs, metric_rows=metric_rows, show_all=show_all))
    return one(2) - one(1)


def agent_worst_case(pairs: int) -> dict:
    """The agent's dearest merged context: the anchor cap's companies, each with ``pairs`` filing pairs and every annual
    metric row shown, all 16 excerpts, 12 risks, 40 added edges, 4 computed lines and the note for a question that
    named all 26 companies."""
    return worst_case_retrieval(AGENT_COMPANIES, pairs=pairs, metric_rows=19, show_all=True, chunks=16, risks=12,
                                extra_edges=40, computed=4, dropped=ALL_26[MAX_ANCHORS:])


def rendered_worst_case_chars(ask_type: str) -> int:
    """The characters of the prompt the writer is sent for the dearest context the caps allow, per ask type."""
    from semigraph.retrieval import workspace
    dropped = ALL_26[MAX_ANCHORS:]            # a question naming all 26 companies keeps 4: the note names the other 22
    if ask_type == "vector":
        return _prompt_chars(worst_case_retrieval(0, risks=0, edge_lines=0))
    if ask_type == "agent":
        return _prompt_chars(agent_worst_case(AGENT_PAIRS))
    r_sec = worst_case_retrieval(MAX_ANCHORS, pairs=2, dropped=dropped)
    if ask_type == "hybrid":
        return _prompt_chars(r_sec)
    docs = [{"chunk_id": f"workspace-document-chunk-{i:036d}", "text": "D" * 1_800} for i in range(6)]
    return len(workspace.build_workspace_prompt(QUESTION, r_sec, {"doc_chunks": docs, "stale_ids": []},
                                                "<<<DOC-ABCDEFGHIJKL>>>")[0])


def writer_floor_micro(ask_type: str, chars: int) -> Fraction:
    """The cost of the writer calls (and, for the agent, the planner's) on the live models for a prompt of ``chars``."""
    tokens = math.ceil(Fraction(chars) / Fraction(5, 2))
    floor = (cost_micro(tokens, OUT_TOKENS, LUNA_IN, LUNA_OUT, DRAFT_CALLS)
             + cost_micro(tokens, OUT_TOKENS, SONNET_IN, SONNET_OUT, STRONG_CALLS))
    if ask_type == "agent":
        floor += cost_micro(PLANNER_IN_TOKENS, PLANNER_OUT_TOKENS, LUNA_IN, LUNA_OUT, PLANNER_CALLS)
    return floor


@pytest.mark.parametrize("ask_type", ASK_TYPES)
def test_every_estimate_covers_the_worst_case_prompt_rendered_at_the_caps_on_the_live_models(ask_type):
    """THE BOUND. The estimate of each ask type, on the fly.toml models, is at least the cost of the worst-case prompt
    rendered through build_blocks / render_prompt (recomputed here from the retriever's own caps), and not much more:
    an estimate that is too loose reserves headroom it never needs."""
    live = live_settings()
    chars = rendered_worst_case_chars(ask_type)
    floor = writer_floor_micro(ask_type, chars)
    assert est.estimate_micro(ask_type, live) >= math.ceil(floor)
    assert est.prompt_tokens(ask_type, live) >= math.ceil(Fraction(chars) / Fraction(5, 2))
    assert est.estimate_micro(ask_type, live) <= 1.15 * float(floor), (ask_type, chars)


def test_the_rendered_worst_cases_are_the_prompts_the_hand_arithmetic_describes():
    """The rendered sizes (characters), so a change to a layout or a cap shows here first."""
    assert {t: rendered_worst_case_chars(t) for t in ASK_TYPES} == {
        "vector": 105_820, "hybrid": 298_060, "workspace": 310_377, "agent": 447_830}


@pytest.mark.parametrize("pairs", [1, 2])
def test_one_plain_anchors_blocks_at_their_caps_fit_the_per_anchor_allowance(pairs):
    """MEASURED through the real code: rules (8), metrics (4 series, the latest 3 years and 4 named years with their
    priors = 11 rows each) and the temporal block (26 items, 16 passages; a question that names fiscal years reads two
    pairs that share the same line budget). 39,686 characters at two pairs, 37,772 at one."""
    measured = _company_chars(pairs=pairs, metric_rows=14, show_all=False)
    assert measured > 35_000, "the synthetic company must really be near the caps for this test to mean anything"
    assert measured <= est.GRAPH_CHARS_PER_ANCHOR


def test_one_agent_companys_blocks_fit_the_agent_allowance_up_to_the_pair_count_the_allowance_covers():
    """The agent's tools add metrics (every annual row: 19 per series, all shown) and temporal pairs (up to 2 per
    company per call, so 4 calls could offer one company 8). The company count AND the pairs a company holds are capped
    in the code (agent/merge.py): the allowance covers exactly AGENT_PAIRS (5), the cap, one more than the graph holds
    (4 consecutive comparisons, see the data test below); 6 and more do not fit it, and 8, what 4 calls offer, is 66,410
    characters (what the cap keeps out)."""
    cost = {p: _company_chars(pairs=p, metric_rows=19, show_all=True) for p in range(1, 9)}
    assert cost == {1: 44_578, 2: 46_490, 3: 42_177, 4: 48_510, 5: 48_650, 6: 54_570, 7: 60_490, 8: 66_410}
    assert all(cost[p] <= est.AGENT_GRAPH_CHARS_PER_COMPANY for p in range(1, AGENT_PAIRS + 1))
    assert cost[AGENT_PAIRS + 1] > est.AGENT_GRAPH_CHARS_PER_COMPANY


def merged_agent_context(companies: int, *, pair_cap: int | None = None) -> dict:
    """The dearest context the agent can BUILD, through the real merge: the prefetch holds 2 pairs of each company (the
    most one read gives), then 4 ``risk_changes`` calls (agent_max_tool_calls) each offer 2 MORE pairs of every company,
    10 offered per company in all. ``pair_cap`` overrides the merge's own cap (the control that shows the cap matters)."""
    from semigraph.agent import merge
    cap = {} if pair_cap is None else {"pair_cap": pair_cap}
    r = worst_case_retrieval(companies, pairs=2, metric_rows=19, show_all=True, chunks=16, risks=12, extra_edges=40,
                             computed=4, dropped=ALL_26[MAX_ANCHORS:])
    for call in range(4):
        layers = [_temporal(cik, 2, first=2 + 2 * call) for cik in range(1, companies + 1)]
        items, pairs, passages, notices = ([row for layer in layers for row in layer[i]] for i in range(4))
        r = merge.merge_temporal(r, items=items, pairs=pairs, passages=passages, notices=notices, question=QUESTION, **cap)
    return r


def test_the_agent_estimate_covers_every_pair_count_up_to_the_cap():
    live = est.estimate_micro("agent", live_settings())
    for pairs in range(1, AGENT_PAIRS + 1):
        assert live >= math.ceil(writer_floor_micro("agent", _prompt_chars(agent_worst_case(pairs)))), pairs


def test_the_agent_estimate_is_a_bound_the_code_enforces_even_when_four_calls_offer_ten_pairs_of_every_company():
    """THE CLOSED RESIDUAL. Without a cap on the pairs of a company, four companies at 8 pairs each rendered 518,870
    characters, 207,548 tokens and cost 907,767 micro-dollars of writer and planner calls at the live prices, above the
    827,661 the agent estimate holds and past the 200,000-token tier (it needed 9 annual filings of one company; the
    graph has at most 5). The real merge now holds each company to 5 pairs: the context it builds renders within the
    estimate's prompt ceiling, and one company's blocks within AGENT_GRAPH_CHARS_PER_COMPANY."""
    from semigraph.agent import merge
    live = live_settings()
    r = merged_agent_context(AGENT_COMPANIES)
    held = {cik: sum(1 for p in r["temporal_pairs"] if p["cik"] == cik) for cik in range(1, AGENT_COMPANIES + 1)}
    assert held == dict.fromkeys(range(1, AGENT_COMPANIES + 1), merge.MAX_PAIRS_HELD_PER_COMPANY)
    chars = _prompt_chars(r)
    assert chars - _prompt_chars(agent_worst_case(AGENT_PAIRS)) <= AGENT_COMPANIES * 300, "5 pairs each, plus a cap note each"
    assert est.prompt_tokens("agent", live) >= math.ceil(Fraction(chars) / Fraction(5, 2))
    assert est.estimate_micro("agent", live) >= math.ceil(writer_floor_micro("agent", chars))
    one_company = _prompt_chars(merged_agent_context(2)) - _prompt_chars(merged_agent_context(1))
    assert 48_000 < one_company <= est.AGENT_GRAPH_CHARS_PER_COMPANY


def test_without_the_cap_the_same_calls_cost_more_than_the_agent_estimate_holds():
    """The control, so the test above cannot pass on its own: the same four calls through a merge whose cap is out of
    reach end with 10 pairs of every company, 566,790 characters and 986,356 micro-dollars of writer and planner calls
    at the live prices, above the 827,661 the estimate holds. The cap, not the data, is what keeps it a bound."""
    r = merged_agent_context(AGENT_COMPANIES, pair_cap=100)
    assert sum(1 for p in r["temporal_pairs"] if p["cik"] == 1) == 10
    assert (_prompt_chars(r), math.ceil(writer_floor_micro("agent", _prompt_chars(r)))) == (566_790, 986_356)
    assert math.ceil(writer_floor_micro("agent", _prompt_chars(r))) > est.estimate_micro("agent", live_settings())


def test_the_graph_holds_at_most_five_annual_filings_of_any_company_so_at_most_four_pairs():
    """The agent's cap on the pairs of a company (merge.MAX_PAIRS_HELD_PER_COMPANY) is above what the graph can give, so
    on today's data it never cuts a comparison: ANNUAL_PAIRS_QUERY reads SUPERSEDES edges of kind 'rolled' between
    consecutive annual filings (10-K, 10-K/A, 20-F, 20-F/A), so n filings give at most n - 1 pairs. Measured on the
    ingested chunk tables: 5 filings at most (AMD: four 10-Ks and a 10-K/A), 2 to 4 for the others."""
    pd = pytest.importorskip("pandas", reason="reading the chunk tables needs pandas (pipeline-time, not shipped)")
    files = sorted((RUN_FILES / "chunks").glob("*.parquet"))
    if not files:
        pytest.skip("data/processed is git-ignored: the chunk tables exist only on the machine that ingested them")
    annual = {"10-K", "10-K/A", "20-F", "20-F/A"}
    filings = []
    for path in files:
        table = pd.read_parquet(path, columns=["form", "accession_no"])
        filings.append(int(table.loc[table["form"].isin(annual), "accession_no"].nunique()))
    assert max(filings) == 5 and min(filings) >= 2
    assert max(filings) - 1 <= AGENT_PAIRS


def test_the_note_at_its_longest_fits_its_allowance():
    """A question naming all 26 companies keeps 4 and the note names the 22 others (the temporal block adds a blank
    line before it)."""
    from semigraph.retrieval import context_layout, retriever
    assert len(ALL_26) == 26
    block, _ = context_layout.temporal_block([], [], notices=[retriever._dropped_notice(ALL_26[MAX_ANCHORS:])])
    assert 250 < len("\n\n" + block) == 276 <= est.ANCHOR_NOTE_CHARS


def test_the_ceiling_by_anchor_cap_is_the_table_the_cap_was_chosen_from(monkeypatch):
    """Prompt tokens of a hybrid ask at 1..10 anchors (live models, 2.5 characters per token): 42,000 characters, 16,800
    tokens, about $0.069 an anchor. The 200,000-token tier the estimate assumes ends after 8 anchors for a hybrid ask
    and after 7 for a workspace ask; the cap is 4."""
    from semigraph.retrieval import retriever
    tokens = {"hybrid": {}, "workspace": {}}
    for anchors in range(1, 11):
        monkeypatch.setattr(retriever, "MAX_ANCHORS", anchors)
        for ask_type in tokens:
            tokens[ask_type][anchors] = est.prompt_tokens(ask_type, make_settings())
    assert tokens["hybrid"] == {1: 79_085, 2: 95_885, 3: 112_685, 4: 129_485, 5: 146_285, 6: 163_085, 7: 179_885,
                                8: 196_685, 9: 213_485, 10: 230_285}
    assert [n for n, t in tokens["hybrid"].items() if t < 200_000] == list(range(1, 9))
    assert [n for n, t in tokens["workspace"].items() if t < 200_000] == list(range(1, 8))


# --- the structure: escalation, planner, workspace -------------------------------------------------------------------

def test_an_escalation_model_adds_the_strong_models_full_input_and_output():
    with_escalation = est.estimate("hybrid", make_settings())
    parts = {c.name: c for c in with_escalation.components}
    assert set(parts) == {"draft", "strong"}
    strong = parts["strong"]
    assert (strong.model, strong.input_tokens, strong.output_tokens, strong.calls) == (
        SONNET, PROMPT_TOKENS["hybrid"], OUT_TOKENS, STRONG_CALLS)
    assert strong.micro == math.ceil(cost_micro(PROMPT_TOKENS["hybrid"], OUT_TOKENS, SONNET_IN, SONNET_OUT,
                                                 STRONG_CALLS))
    draft = parts["draft"]
    assert (draft.model, draft.input_tokens, draft.output_tokens, draft.calls) == (
        LUNA, PROMPT_TOKENS["hybrid"], OUT_TOKENS, DRAFT_CALLS)
    assert est.estimate_micro("hybrid", make_settings(escalation_model="")) < with_escalation.micro


def test_without_an_escalation_model_the_answer_model_is_the_only_call_and_keeps_the_stream_retry():
    only = est.estimate("hybrid", make_settings(escalation_model=""))
    assert [c.name for c in only.components] == ["answer"]
    assert only.components[0].calls == STRONG_CALLS and only.components[0].model == LUNA
    assert only.micro == math.ceil(cost_micro(PROMPT_TOKENS["hybrid"], OUT_TOKENS, LUNA_IN, LUNA_OUT, STRONG_CALLS))


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
    assert e.micro == math.ceil(cost_micro(PROMPT_TOKENS["hybrid"], OUT_TOKENS, Fraction(3), Fraction(9),
                                           STRONG_CALLS))


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


DATA = ROOT / "data"
SKIP_DATA = "data/ is git-ignored: these tables exist only on the machine that built the graph"


def test_the_relationships_allowance_is_twice_the_graphs_distinct_company_relations():
    """The RELATIONSHIPS block has no cap in the code: it is bounded by the graph. The loader MERGEs one edge per
    (source, relation, target) between canonical companies, so the extractions give the count: 73 (22 companies)."""
    pytest.importorskip("pandas", reason="graph.loaders is a pipeline-time module (pandas)")
    from semigraph.graph.loaders import _normalize_name, _resolve
    files = sorted((RUN_FILES / "extractions").glob("*_extractions.jsonl"))
    if not files:
        pytest.skip(SKIP_DATA)
    canonical = load_canonical_entities()
    lookup = {_normalize_name(a): name for name, spec in canonical.items() for a in [name, *spec["aliases"]]}
    edges = set()
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            for rel in json.loads(line)["relations"]:
                source, target = _resolve(rel["source_entity"], lookup), _resolve(rel["target_entity"], lookup)
                if source and target and source != target:
                    edges.add((source, rel["relation"], target))
    assert len(edges) == 73
    assert 2 * len(edges) <= est.EDGE_LINES_ALLOWED == EDGE_LINES_BUILT


def test_the_longest_metric_series_and_headline_and_rule_title_are_the_worst_case_basis():
    pd = pytest.importorskip("pandas", reason="reading the parquet tables needs pandas (pipeline-time, not shipped)")
    xbrl = sorted((RUN_FILES / "xbrl").glob("*_key_metrics.parquet"))
    items = sorted((DATA / "interim" / "risk_items").glob("*_risk_items.parquet"))
    rules = DATA / "raw" / "federal_register_bis_rules.json"
    if not (xbrl and items and rules.exists()):
        pytest.skip(SKIP_DATA)
    rows = max(int(pd.read_parquet(f).drop_duplicates(["metric", "end"]).groupby("metric").size().max()) for f in xbrl)
    assert rows == 19                     # annual rows of one metric of one company (the agent worst case builds 19)
    headline = max(int(pd.read_parquet(f, columns=["headline"])["headline"].dropna().str.len().max()) for f in items)
    assert headline == OLDER_HEADLINE_CHARS == 542
    title = max(len(rule["title"]) for rule in json.loads(rules.read_text(encoding="utf-8"))["results"])
    assert title == 234 <= RULE_TITLE_CHARS


# --- the live configuration ------------------------------------------------------------------------------------------

def test_the_live_configuration_is_what_the_estimates_assume():
    s = live_settings()
    assert (s.answer_model, s.escalation_model, s.llm_answer_max_tokens) == (LUNA, SONNET, OUT_TOKENS)
    assert s.agent_planner_model == LUNA and s.agent_max_model_calls == PLANNER_CALLS and s.max_question_chars == 500
    assert est.estimate_micro("hybrid", s) == expected_micro("hybrid")      # the live reading is the hand-computed one


def test_the_live_estimates_are_cents_not_dollars_and_not_zero():
    """The band found: from the dearest recorded ask (about 7 cents) to under a dollar. Hybrid about 58 cents, vector
    22, workspace 60, agent 83 (they carry the escalation's retry allowance and a context two to eleven times the
    largest recorded; the agent's also three planner calls)."""
    s = live_settings()
    worst_recorded = {"hybrid": V2E_ROW_MAX_USD, "vector": VECTOR_ROW_MAX_USD, "workspace": V2E_ROW_MAX_USD,
                      "agent": AGENT_ROW_MAX_USD}
    for ask_type in ASK_TYPES:
        usd = est.estimate_usd(ask_type, s)
        assert worst_recorded[ask_type] < usd < 1.0, (ask_type, usd)
    assert round(est.estimate_usd("hybrid", s), 2) == 0.58
    assert [round(est.estimate_usd(t, s), 2) for t in ("vector", "workspace", "agent")] == [0.22, 0.60, 0.83]


def test_the_agent_estimate_is_83_cents_because_its_companies_are_capped_at_the_anchor_cap():
    """OWNER DECISION (option b): capping the companies in the agent's merge at MAX_ANCHORS (agent/merge.py and
    agent/tools.py) took the estimate from $1.57 (13 companies' blocks, a 368,010-token prompt, past the long-context
    tier) to $0.83 (4 companies, 188,010 tokens). ``agent_company_blocks`` is the one place that reads that cap."""
    from semigraph.agent import merge
    live = live_settings()
    assert est.estimate_micro("agent", live) == 827_661 and est.estimate_usd("agent", live) < 1.0
    assert est.agent_company_blocks() == merge.company_cap() == AGENT_COMPANIES == MAX_ANCHORS


def test_the_agents_tools_cannot_cover_more_companies_than_the_estimate_prices():
    """Behavioural pin (not a name search): all 26 companies are offered, one per call, to a tool that adds a company,
    from a prefetch of one: the context ends with exactly ``agent_company_blocks()`` of them and every other call is
    refused (``ok`` False, the retrieval dict unchanged). ``add_anchors``, the backstop, refuses all 26 at once."""
    from agent_fakes import FakeDriver, FakeEmbedder

    from semigraph.agent import merge
    from semigraph.agent.tools import Toolbox
    from semigraph.retrieval.retriever import hybrid_retrieve
    question, driver, embedder = "How much revenue did Nvidia report?", FakeDriver.world(), FakeEmbedder()
    r = hybrid_retrieve(question, driver, embedder)
    box, refused = Toolbox(driver, embedder, question), 0
    for name in ALL_26:
        outcome = box.execute("financial_metrics", {"companies": [name]}, r)
        refused += not outcome.ok
        assert outcome.ok or outcome.r is r
        r = outcome.r
    covered = est.agent_company_blocks()
    assert len(r["anchors"]) == len(merge.covered_companies(r)) == covered and refused == len(ALL_26) - covered
    with pytest.raises(merge.CompanyCapError):
        merge.add_anchors({"anchors": {}, "anchor_defaulted": False}, {name: i for i, name in enumerate(ALL_26, 1)})


def test_which_cap_binds_first_the_150_ask_count_or_the_10_dollar_estimate():
    """The reserve rule denies an ask when spend + its estimate exceeds the cap, where a settled ask counts at its
    ACTUAL cost and only a lease in flight counts at its estimate. So the estimate is headroom, not a per-ask charge:
      - every ask at the estimate (a pile of in-flight leases): 150 hybrid asks would be $87.01, the cap would stop the
        18th, but the live in-flight cap is 2, so that never happens;
      - asks that settle at the recorded mean (1.5 cents): the 150-ask count binds first, with $2.19 spent;
      - asks that all cost the dearest straight-to-Sonnet ask (6.06 cents): still all 150 granted;
      - asks that all cost the dearest recorded ask (6.6 cents, a rejected draft plus the strong answer): the $10 cap
        binds first, at the 145th ask (the 144th was the last granted);
      - break-even: an average above 6.32 cents per ask makes the $10 cap bind before the count.
    The anchor cap moved the hybrid estimate from $0.52 to $0.58 and the break-even from 6.36 to 6.32 cents: the
    estimate is headroom, so the cap that binds first is still the count at every recorded cost but the dearest.
    The agent, with its companies capped ($0.83 rather than $1.57): asks at the recorded mean (1.4 cents) meet the count
    first ($2.17 spent); asks that all cost the dearest recorded one (7.07 cents) meet the $10 cap first, at the 131st
    (130 granted, where $1.57 granted 120); the break-even is 6.16 cents per ask. A pile of 12 agent leases at their
    estimate fills the day, but the live in-flight cap is 2."""
    s = live_settings()
    hybrid, agent = est.estimate_micro("hybrid", s), est.estimate_micro("agent", s)
    assert (hybrid, agent) == (580_089, 827_661)
    assert 150 * hybrid / 1_000_000 == pytest.approx(87.01, abs=0.01)
    assert CAP_MICRO // hybrid == 17                       # at the estimate the 18th ask cannot reserve
    dearest, mean = round(V2E_ROW_MAX_USD * 1_000_000), round(V2E_MEAN_ROW_USD * 1_000_000)
    assert asks_granted(mean, hybrid) == COUNT_CAP
    assert 150 * mean / 1_000_000 == pytest.approx(2.19, abs=0.01)
    assert asks_granted(round(V2E_STRONG_ROW_MAX_USD * 1_000_000), hybrid) == COUNT_CAP
    assert asks_granted(dearest, hybrid) == 144            # the 145th is denied by the $10 cap
    assert asks_granted(dearest, hybrid, in_flight=1) == 135     # one other lease in flight (the live cap is 2)
    assert (CAP_MICRO - hybrid) // (COUNT_CAP - 1) == 63_220       # the break-even cost per ask: the dearest that still
    assert asks_granted(63_220, hybrid) == COUNT_CAP and asks_granted(63_221, hybrid) == COUNT_CAP - 1   # gets all 150
    agent_dearest, agent_mean = round(AGENT_ROW_MAX_USD * 1_000_000), round(AGENT_MEAN_ROW_USD * 1_000_000)
    assert asks_granted(agent_mean, agent) == COUNT_CAP and asks_granted(agent_dearest, agent) == 130
    assert 150 * agent_mean / 1_000_000 == pytest.approx(2.17, abs=0.01)
    assert (CAP_MICRO - agent) // (COUNT_CAP - 1) == 61_559        # the agent's break-even cost per ask
    assert asks_granted(61_559, agent) == COUNT_CAP and asks_granted(61_560, agent) == COUNT_CAP - 1
    assert CAP_MICRO // agent == 12       # twelve agent asks at their estimate fill the day (a ceiling, not a charge)


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


def test_the_per_company_caps_are_the_ones_the_worst_case_was_built_from():
    """Raising a cap (or a layout cut) changes what one company's blocks can weigh: re-measure the allowances, then edit
    the literals here and the estimate's."""
    from semigraph.retrieval import context_layout, retriever
    assert retriever.TEMPORAL_CAPS == {"removed": 8, "unsettled": 6, "new": 8, "reworded": 4}
    assert retriever.PASSAGE_CAPS == {"removed": 8, "added": 4, "reworded": 4}
    assert (retriever.RULES_PER_COMPANY, retriever.MAX_PAIRS_PER_COMPANY, retriever.METRIC_PERIODS_SHOWN,
            retriever.MAX_MENTIONED_PERIODS) == (8, 2, 3, 4)
    assert (context_layout.HEADLINE_MAX_CHARS, context_layout.PASSAGE_QUOTE_CHARS,
            context_layout.PASSAGE_WHERE_CHARS, context_layout.MAX_CHUNK_IDS_PER_ITEM) == (240, 450, 100, 3)
    assert retriever.MAX_ANCHORS == MAX_ANCHORS
    assert est.EDGE_LINE_CHARS == 300 and est.RISK_LINE_CHARS == 700


def test_the_agent_code_is_what_the_agent_allowance_was_measured_against():
    from semigraph.agent import merge, sanitize
    from semigraph.retrieval import retriever
    assert merge.company_cap() == est.agent_company_blocks() == retriever.MAX_ANCHORS
    assert retriever.MAX_PAIRS_PER_COMPANY == 2          # the pairs a single call can offer per company
    assert Settings.model_fields["agent_max_tool_calls"].default == 4     # 4 calls x 2 pairs offered (the cap keeps 5 in all)
    assert merge.MAX_PAIRS_HELD_PER_COMPANY == AGENT_PAIRS == 5          # the pairs the allowance was measured at, held by the merge
    assert set(sanitize.KNOWN_METRICS) == {"revenue", "net_income", "rnd", "capex"}


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
    for fragment in ("usd=0.580089", "micro=580089", "prompt_tokens=129485", "anchors_max=4", "company_blocks=4", LUNA,
                     SONNET, "x2", "unpriced=none"):
        assert fragment in hybrid, fragment
    assert "anchors_assumed" not in hybrid
    assert "company_blocks=4" in by_type["agent"] and "company_blocks=0" in by_type["vector"]
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
            "from semigraph.agent import merge, planner, sanitize, tools; from semigraph.uploads import units; "
            "print(semigraph.serve.estimate.max_anchors(), semigraph.serve.estimate.agent_company_blocks())")
    assert _run_python(code) == "4 4"       # and the estimate reads the anchor cap without either of them
