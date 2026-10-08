"""The per-ask paid-call meter (serve/meter.py; Wave 2 step 0, docs/v2/research/m5-councils/council4/verdict.md, option C).

One meter per ask records every paid model call the moment it STARTS, with the most that call could cost (its bound),
and the usage the provider reports when it ends. The ask's lease is then settled at ``min(estimate, the sum of each
call's reported cost, or its bound while it has none)``: nothing when no paid call started, the whole estimate when a
record cannot be trusted. Pure: no litellm, no I/O, prices from ``estimate.resolve_price``.

The expected numbers are written out as literals (token counts, the prices in micro-dollars per million tokens, the
arithmetic in the comments), never read back from the module, except where a test pins the meter to ``estimate``.

Runs on the CI ``serve-shipped`` job (no pandas, no Neo4j); ``Settings(_env_file=None, ...)`` reads no ``.env``."""

import logging
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from semigraph.config import Settings
from semigraph.serve import estimate as est
from semigraph.serve.meter import ROLES, CallRecord, PaidMeter

ROOT = Path(__file__).resolve().parents[1]
LUNA, SONNET = "openai/gpt-6-luna", "anthropic/claude-sonnet-5"
# micro-dollars per million tokens: Luna $0.10 in / $0.50 out, Sonnet $2 in / $10 out
LUNA_IN, LUNA_OUT, SONNET_IN, SONNET_OUT = 100_000, 500_000, 2_000_000, 10_000_000
ASK_TYPES = ("hybrid", "vector", "agent", "workspace")
LOGGER = "semigraph.serve.meter"


def make_settings(**override) -> Settings:
    base = {"answer_model": LUNA, "escalation_model": SONNET, "agent_planner_model": LUNA,
            "llm_answer_max_tokens": 2_400, "llm_input_price_per_mtok": 2.0, "llm_output_price_per_mtok": 10.0,
            "agent_max_model_calls": 3, "agent_max_tool_calls": 4, "max_question_chars": 500}
    return Settings(_env_file=None, **{**base, **override})


def meter() -> PaidMeter:
    return PaidMeter(make_settings())


def start_sonnet(m: PaidMeter, *, chars=100_000, out=1_200, attempts=1, role="strong") -> int:
    """100,000 chars = 50,000 tokens at Sonnet's 2.0 chars per token: 50,000 x 2 + 1,200 x 10 = 112,000 micro-dollars
    (it was 40,000 tokens and 92,000 at the single 2.5 the estimate used before it had a figure per model)."""
    return m.start(role=role, model=SONNET, prompt_chars=chars, max_output_tokens=out, attempts=attempts)


SONNET_BOUND = 112_000
HYBRID_ESTIMATE = 709_573      # the live hybrid estimate (tests/test_serve_estimate.py): an estimate no single call reaches


# ----------------------------------------------------------------------------------------------- the bound

def test_a_started_call_is_bounded_by_its_prompt_and_its_output_cap_at_its_models_price():
    m = meter()
    # Sonnet: ceil(100,000 / 2.0) = 50,000 in x $2 + 1,200 out x $10 = 100,000 + 12,000
    call = start_sonnet(m)
    # Luna: ceil(100,001 / 2.5) = 40,001 in x $0.10 + 2,400 out x $0.50 = 4,000.1 + 1,200 -> rounded UP to 5,201
    luna = m.start(role="draft", model=LUNA, prompt_chars=100_001, max_output_tokens=2_400)
    records = {r.call_id: r for r in m.calls}
    assert (records[call].bound_micro, records[call].role, records[call].model) == (SONNET_BOUND, "strong", SONNET)
    assert records[luna].bound_micro == 5_201 and records[luna].prompt_chars == 100_001


def test_the_bound_counts_the_tokens_of_each_model_at_that_models_characters_per_token():
    """The same 100,001 characters are 50,001 tokens on Sonnet (2.0 per token, rounded up) and 40,001 on Luna (2.5); a
    model with no figure of its own gets the default 2.5, and the planner's role changes nothing for a real model."""
    m = meter()
    sonnet = m.start(role="strong", model=SONNET, prompt_chars=100_001, max_output_tokens=0)
    luna = m.start(role="strong", model=LUNA, prompt_chars=100_001, max_output_tokens=0)
    planner = m.start(role="planner", model=LUNA, prompt_chars=100_001, max_output_tokens=0)
    unlisted = m.start(role="strong", model="acme/unknown-1", prompt_chars=100_001, max_output_tokens=0)
    bounds = {r.call_id: r.bound_micro for r in m.calls}
    assert bounds[sonnet] == 50_001 * 2                    # $2 per million input tokens: micro-dollars = tokens x 2
    assert bounds[luna] == bounds[planner] == 4_001        # 40,001 x $0.10 = 4,000.1 micro-dollars, rounded up
    assert bounds[unlisted] == 40_001 * 2                  # the configured $2: 2.5 characters per token, the default


def test_every_provider_attempt_a_call_may_make_is_inside_its_bound():
    m = meter()
    one, three = start_sonnet(m), start_sonnet(m, attempts=3)
    bounds = {r.call_id: r.bound_micro for r in m.calls}
    assert bounds[one] == SONNET_BOUND and bounds[three] == 3 * SONNET_BOUND


def test_a_retried_calls_reported_usage_does_not_forget_the_attempts_before_it():
    """A provider retry reports the usage of its LAST attempt only; the earlier ones may have been billed too. A call that
    declared ``attempts`` keeps (attempts - 1) one-attempt bounds on top of what it reports (the whole bound when it
    reports nothing), so a retried call is never charged less than its worst case minus one attempt."""
    m = meter()
    call = start_sonnet(m, attempts=3)
    assert m.charge_micro(HYBRID_ESTIMATE) == 3 * SONNET_BOUND                         # nothing reported: the whole bound
    m.complete(call, {"prompt_tokens": 30_000, "completion_tokens": 500})       # 65,000 for the attempt that reported
    assert m.charge_micro(HYBRID_ESTIMATE) == 65_000 + 2 * SONNET_BOUND
    assert m.charge_micro(100_000) == 100_000 and next(iter(m.calls)).reported_micro == 65_000


def test_the_extra_attempts_of_a_rounded_bound_add_up_to_the_bound_when_the_call_reports_its_cap():
    m = meter()
    call = m.start(role="draft", model=LUNA, prompt_chars=100_001, max_output_tokens=2_400, attempts=2)
    (record,) = m.calls
    assert record.bound_micro == 10_401                                        # ceil(2 x 5,200.1)
    m.complete(call, {"prompt_tokens": 40_001, "completion_tokens": 2_400})     # one attempt at its cap: 5,201
    assert m.charge_micro(10**9) == record.bound_micro


def test_a_single_attempt_call_keeps_exactly_its_reported_cost():
    m = meter()
    call = start_sonnet(m, attempts=1)
    m.complete(call, {"prompt_tokens": 30_000, "completion_tokens": 500})
    assert m.charge_micro(HYBRID_ESTIMATE) == 65_000


def test_the_bound_is_rounded_up_once_per_call_and_a_call_is_never_free():
    m = meter()
    tiny = m.start(role="draft", model=LUNA, prompt_chars=1, max_output_tokens=1)    # 1 token in, 1 out: 0.6 -> 1
    assert next(r for r in m.calls if r.call_id == tiny).bound_micro == 1


def test_a_mock_model_is_priced_as_the_real_model_of_its_role_and_an_unlisted_one_at_the_configured_list_price():
    m = PaidMeter(make_settings(llm_input_price_per_mtok=3.0, llm_output_price_per_mtok=15.0))
    mock = m.start(role="strong", model="openai/mock-sonnet", prompt_chars=100_000, max_output_tokens=1_200)
    by_role = m.start(role="strong", model="openai/mock-anything", prompt_chars=100_000, max_output_tokens=1_200)
    unlisted = m.start(role="strong", model="acme/unknown-1", prompt_chars=100_000, max_output_tokens=1_200)
    bounds = {r.call_id: r.bound_micro for r in m.calls}
    assert bounds[mock] == bounds[by_role] == SONNET_BOUND
    assert bounds[unlisted] == 40_000 * 3 + 1_200 * 15                  # 138,000: the configured list prices


def test_the_roles_are_the_three_the_estimate_prices():
    assert ROLES == ("draft", "strong", "planner")


# ---------------------------------------------------------------------------------------------- the charge

def test_no_call_started_charges_zero():
    assert meter().charge_micro(HYBRID_ESTIMATE) == 0


def test_a_started_but_incomplete_call_charges_its_bound():
    m = meter()
    start_sonnet(m)
    assert m.charge_micro(HYBRID_ESTIMATE) == SONNET_BOUND


def test_a_completed_call_charges_its_reported_cost_not_its_bound():
    m = meter()
    call = start_sonnet(m)
    m.complete(call, {"prompt_tokens": 30_000, "completion_tokens": 500})
    # 30,000 x $2 + 500 x $10 = 60,000 + 5,000
    assert m.charge_micro(HYBRID_ESTIMATE) == 65_000
    assert next(iter(m.calls)).reported_micro == 65_000


def test_the_reported_cost_is_rounded_up_to_the_micro():
    m = meter()
    call = m.start(role="draft", model=LUNA, prompt_chars=100_000, max_output_tokens=2_400)
    m.complete(call, {"prompt_tokens": 1_001, "completion_tokens": 3})        # 100.1 + 1.5 = 101.6 -> 102
    assert m.charge_micro(10**9) == 102


def test_the_charge_adds_the_reported_cost_of_finished_calls_and_the_bound_of_running_ones():
    m = meter()
    draft = m.start(role="draft", model=LUNA, prompt_chars=100_000, max_output_tokens=2_400)
    m.complete(draft, {"prompt_tokens": 40_000, "completion_tokens": 600})      # 4,000 + 300 = 4,300
    start_sonnet(m)                                                             # the escalation: started, not finished
    assert m.charge_micro(HYBRID_ESTIMATE) == 4_300 + SONNET_BOUND


@pytest.mark.parametrize("estimate", [0, 1, 4_300, 50_000, SONNET_BOUND, 4_300 + SONNET_BOUND, HYBRID_ESTIMATE])
@pytest.mark.parametrize("prompt_tokens, completion_tokens", [(0, 0), (10_000, 100), (40_000, 1_200), (60_000, 3_000)])
def test_the_charge_is_never_below_the_reported_cost_and_the_estimate_caps_only_what_is_not_reported(
        estimate, prompt_tokens, completion_tokens):
    """Council 4 test (d), as the verifier's B1 corrected it: the ledger never records LESS than the provider reported
    (the unconditional half), and the estimate caps only the part that is not a report: the bound of a call still running.
    ``charge = max(reported, min(estimate, reported + running bounds))``. A report above the estimate is therefore
    charged as reported (the estimate was a ceiling that the provider's own bill exceeded; the day's spend must show
    it)."""
    m = meter()
    done = m.start(role="draft", model=LUNA, prompt_chars=100_000, max_output_tokens=2_400)
    m.complete(done, {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens})
    start_sonnet(m)                                                             # one call still running at its bound
    reported = next(iter(m.calls)).reported_micro
    charge = m.charge_micro(estimate)
    assert charge >= reported
    assert charge == max(reported, min(estimate, reported + SONNET_BOUND))
    if reported <= estimate:
        assert charge <= estimate


def test_an_estimated_usage_keeps_the_bound():
    m = meter()
    call = start_sonnet(m)
    m.complete(call, {"prompt_tokens": 1, "completion_tokens": 1, "estimated": True})
    assert next(iter(m.calls)).reported_micro is None and m.charge_micro(HYBRID_ESTIMATE) == SONNET_BOUND


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 5}, {"completion_tokens": 5},
                                   {"prompt_tokens": None, "completion_tokens": 3},
                                   {"prompt_tokens": -1, "completion_tokens": 3},
                                   {"prompt_tokens": "7", "completion_tokens": 3},
                                   {"prompt_tokens": 7.5, "completion_tokens": 3},
                                   {"prompt_tokens": True, "completion_tokens": 3}, "usage", 7, []])
def test_a_usage_that_is_not_two_whole_token_counts_keeps_the_bound_and_is_not_a_fault(usage):
    m = meter()
    call = start_sonnet(m)
    m.complete(call, usage)
    assert m.charge_micro(HYBRID_ESTIMATE) == SONNET_BOUND and m.faults == ()


def test_a_reported_cost_above_its_bound_is_charged_as_reported_even_above_the_estimate_and_logged(caplog):
    m = meter()
    call = start_sonnet(m, chars=100_000, out=1_200)                              # bound 92,000
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        m.complete(call, {"prompt_tokens": 60_000, "completion_tokens": 1_200})   # 132,000: the prompt was denser
    assert m.charge_micro(HYBRID_ESTIMATE) == 132_000 and m.charge_micro(100_000) == 132_000
    assert any(r.getMessage().startswith("meter_over_bound") for r in caplog.records)


def test_a_finished_ask_whose_reported_cost_is_above_its_estimate_is_charged_the_reported_cost(caplog):
    """B1, the verifier's probe: reported 70,000 micro-dollars against an estimate of 60,000. The ledger got 60,000 (the
    meter capped the report at the estimate); HEAD, and an unmetered twin, record 70,000. A report is what the provider
    billed: the day's spend and the address's share must hold it."""
    m = meter()
    call = start_sonnet(m, chars=100_000, out=1_200)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        m.complete(call, {"prompt_tokens": 30_000, "completion_tokens": 1_000})   # 60,000 + 10,000 = 70,000
    assert next(iter(m.calls)).reported_micro == 70_000
    assert m.charge_micro(60_000) == 70_000
    assert m.charge_micro(70_000) == 70_000 and m.charge_micro(70_001) == 70_000    # at and under the report: the same
    warning = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("meter_over_estimate"))
    assert "reported_micro=70000" in warning and "estimate_micro=60000" in warning
    assert "the reported cost is charged" in warning and "the estimate is charged" not in warning


def test_a_report_above_the_estimate_is_charged_in_full_and_a_running_call_adds_nothing_past_the_estimate():
    """The estimate caps what is NOT a report: a running call's bound. With a finished call at 70,000 against an estimate of
    60,000 the running call's bound cannot raise the charge (the estimate is already spent by the report), and with a
    report under the estimate the running call fills the headroom and no more."""
    m = meter()
    done = start_sonnet(m)
    m.complete(done, {"prompt_tokens": 30_000, "completion_tokens": 1_000})          # reported 70,000
    start_sonnet(m)                                                                  # running, bound 92,000
    assert m.charge_micro(60_000) == 70_000
    assert m.charge_micro(100_000) == 100_000                                        # headroom filled: the estimate
    assert m.charge_micro(200_000) == 70_000 + SONNET_BOUND                          # estimate above both: report + bound


def test_a_fault_charges_the_estimate_but_never_less_than_what_was_reported():
    m = meter()
    call = start_sonnet(m)
    m.complete(call, {"prompt_tokens": 30_000, "completion_tokens": 1_000})          # 70,000 reported
    m.complete(99, None)                                                             # a record that cannot be trusted
    assert m.faults == ("complete_unknown_call",)
    assert m.charge_micro(60_000) == 70_000 and m.charge_micro(HYBRID_ESTIMATE) == HYBRID_ESTIMATE
    assert m.charge_micro(70_000) == 70_000


def test_a_reported_ratio_is_logged_for_the_preview_to_check_the_chars_per_token_assumption(caplog):
    m = meter()
    call = start_sonnet(m)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        m.complete(call, {"prompt_tokens": 38_000, "completion_tokens": 10})
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("meter_ratio")]
    assert line == f"meter_ratio model={SONNET} role=strong prompt_chars=100000 prompt_tokens=38000"


def test_an_estimated_or_missing_usage_logs_no_ratio(caplog):
    m = meter()
    first, second = start_sonnet(m), start_sonnet(m)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        m.complete(first, {"prompt_tokens": 1, "completion_tokens": 1, "estimated": True})
        m.complete(second, None)
    assert [r for r in caplog.records if r.getMessage().startswith("meter_ratio")] == []


# ------------------------------------------------------------------------------------- records that cannot be trusted

def _complete_an_unknown_call(m: PaidMeter) -> None:
    m.complete(99, {"prompt_tokens": 1, "completion_tokens": 1})


def _complete_before_any_start(m: PaidMeter) -> None:
    m.complete(1, None)                                          # the id the first start WILL get: still a fault


def _complete_a_non_integer_id(m: PaidMeter) -> None:
    start_sonnet(m)
    m.complete("1", None)


def _complete_the_same_call_twice(m: PaidMeter) -> None:
    call = start_sonnet(m)
    m.complete(call, {"prompt_tokens": 1_000, "completion_tokens": 10})
    m.complete(call, {"prompt_tokens": 1_000, "completion_tokens": 10})


@pytest.mark.parametrize("record, fault", [
    (_complete_an_unknown_call, "complete_unknown_call"),
    (_complete_before_any_start, "complete_unknown_call"),
    (_complete_a_non_integer_id, "complete_unknown_call"),
    (_complete_the_same_call_twice, "complete_twice"),
])
def test_an_inconsistent_record_charges_the_full_estimate(record, fault, caplog):
    """A completion with no matching start, or a second one, means the meter's picture of the ask is wrong: fail closed.
    It holds even when no call started at all (the 'nothing started, charge 0' rule never hides a fault)."""
    m = meter()
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        record(m)
    assert m.charge_micro(HYBRID_ESTIMATE) == HYBRID_ESTIMATE and m.faults == (fault,)
    assert any(r.levelno == logging.ERROR and f"reason={fault}" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("fields, fault", [
    (dict(role="writer"), "bad_role"),
    (dict(role=None), "bad_role"),
    (dict(prompt_chars=-1), "bad_prompt_chars"),
    (dict(prompt_chars="100"), "bad_prompt_chars"),
    (dict(prompt_chars=True), "bad_prompt_chars"),
    (dict(prompt_chars=1.5), "bad_prompt_chars"),
    (dict(max_output_tokens=-1), "bad_max_output_tokens"),
    (dict(max_output_tokens=None), "bad_max_output_tokens"),
    (dict(attempts=0), "bad_attempts"),
    (dict(attempts=-1), "bad_attempts"),
    (dict(attempts=1.0), "bad_attempts"),
    (dict(model=""), "bad_model"),
    (dict(model=None), "bad_model"),
])
def test_a_call_whose_bound_cannot_be_computed_is_counted_and_charges_the_full_estimate(fields, fault):
    """A bound that is not a whole number of micro-dollars is not a bound: the ask is charged its estimate. The meter
    never raises into the answer path (the call still goes ahead; the settle is what fails closed)."""
    m = meter()
    request = dict(role="strong", model=SONNET, prompt_chars=100_000, max_output_tokens=1_200, attempts=1) | fields
    call = m.start(**request)
    assert isinstance(call, int) and len(m.calls) == 1 and m.faults == (fault,)
    assert m.charge_micro(HYBRID_ESTIMATE) == HYBRID_ESTIMATE
    assert m.calls[0].bound_micro == 0


def test_a_price_that_cannot_be_read_is_a_fault_and_never_an_exception_in_the_answer_path():
    no_prices = PaidMeter(SimpleNamespace())                       # an unlisted model needs the configured list prices
    call = no_prices.start(role="strong", model="acme/unknown-1", prompt_chars=100, max_output_tokens=10)
    assert isinstance(call, int) and no_prices.faults == ("bad_price",) and no_prices.charge_micro(777) == 777
    negative = PaidMeter(make_settings(llm_input_price_per_mtok=-1.0))
    negative.start(role="strong", model="acme/unknown-1", prompt_chars=100, max_output_tokens=10)
    assert negative.faults == ("bad_price",) and negative.charge_micro(777) == 777


def test_a_fault_does_not_hide_behind_a_later_good_call():
    m = meter()
    m.complete(7, None)
    start_sonnet(m)
    assert m.charge_micro(HYBRID_ESTIMATE) == HYBRID_ESTIMATE


def test_the_estimate_must_be_a_whole_number_of_micro_dollars():
    m = meter()
    for bad in (-1, 1.5, "10", None, True):
        with pytest.raises((TypeError, ValueError)):
            m.charge_micro(bad)


# ------------------------------------------------------------------------------------------ after the settlement

def test_a_start_after_close_is_counted_and_logged(caplog):
    m = meter()
    first = start_sonnet(m)
    m.complete(first, {"prompt_tokens": 1_000, "completion_tokens": 10})
    m.close()
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        late = start_sonnet(m, role="planner")
    records = {r.call_id: r for r in m.calls}
    assert records[late].late is True and records[first].late is False and m.closed is True
    assert any("paid call after settlement" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)
    assert m.charge_micro(HYBRID_ESTIMATE) == 2_000 + 10 * 10 + SONNET_BOUND       # the late call's bound is in the next charge


def test_close_is_idempotent_and_a_known_call_may_still_complete_after_it():
    m = meter()
    call = start_sonnet(m)
    m.close()
    m.close()
    m.complete(call, {"prompt_tokens": 1_000, "completion_tokens": 10})
    assert m.faults == () and m.charge_micro(HYBRID_ESTIMATE) == 2_100


def test_calls_are_a_frozen_ordered_snapshot():
    m = meter()
    first = start_sonnet(m)
    snapshot = m.calls
    start_sonnet(m)
    assert isinstance(snapshot, tuple) and len(snapshot) == 1 and len(m.calls) == 2
    assert [r.call_id for r in m.calls] == sorted(r.call_id for r in m.calls) and snapshot[0].call_id == first
    with pytest.raises(AttributeError):
        snapshot[0].bound_micro = 0
    assert isinstance(snapshot[0], CallRecord)


# ------------------------------------------------------------------------- the worst case, for every ask type

def worst_prompt_chars(ask_type: str, settings: Settings) -> int:
    template = est.WORKSPACE_TEMPLATE_CHARS if ask_type == "workspace" else est.ANSWER_TEMPLATE_CHARS
    return template + settings.max_question_chars + est.context_chars(ask_type, settings)


def meter_the_worst_ask(ask_type: str, settings: Settings) -> tuple[PaidMeter, list[int]]:
    """Every call the estimate prices, started at its worst-case prompt: the draft once and each attempt of the strong
    stream separately (or, with no escalation model, the sole answer model's attempts as the strong role), and each
    planner call of an agent ask."""
    m = PaidMeter(settings)
    chars = worst_prompt_chars(ask_type, settings)
    out = settings.llm_answer_max_tokens
    if settings.escalation_model and settings.escalation_model != settings.answer_model:
        ids = [m.start(role="draft", model=settings.answer_model, prompt_chars=chars, max_output_tokens=out)
               for _ in range(est.DRAFT_ATTEMPTS)]
        strong_model = settings.escalation_model
    else:
        ids, strong_model = [], settings.answer_model
    ids += [m.start(role="strong", model=strong_model, prompt_chars=chars, max_output_tokens=out)
            for _ in range(est.STREAM_ATTEMPTS)]
    if ask_type == "agent":
        planner_chars = est.PLANNER_FIXED_CHARS + settings.max_question_chars + est.PLANNER_GROWTH_CHARS
        ids += [m.start(role="planner", model=settings.agent_planner_model, prompt_chars=planner_chars,
                        max_output_tokens=est.PLANNER_MAX_TOKENS) for _ in range(settings.agent_max_model_calls)]
    return m, ids


SHAPES = {                    # the configurations the estimate prices, as Settings overrides
    "live: Luna draft, Sonnet strong": {},
    "sole Luna (no escalation model)": {"escalation_model": ""},
    "sole Sonnet (the rollback)": {"answer_model": SONNET, "escalation_model": ""},
    "staging mocks": {"answer_model": "openai/mock-draft", "escalation_model": "openai/mock-strong",
                      "agent_planner_model": "openai/mock-planner"},
}


def tokens_at(chars: int, model: str) -> int:
    """Tokens at the model's characters per token, written here as literals: Sonnet 2.0, anything else 2.5."""
    return -(-chars // 2) if model == SONNET else -(-chars * 2 // 5)


def priced_as(record: CallRecord) -> str:
    """The real model a call is sized and priced as: a staging mock stands for the real model of its role."""
    if record.model.startswith("openai/mock-"):
        return SONNET if record.role == "strong" else LUNA
    return record.model


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("ask_type", ASK_TYPES)
def test_the_worst_case_prompt_bounds_sum_to_the_estimate_for_every_ask_type(ask_type, shape):
    """Verdict test (g), the arithmetic half: at the worst-case prompt the bounds of the calls the estimate prices add up
    to the estimate (each call rounds up on its own, the estimate rounds once, so the sum may exceed it by less than one
    micro-dollar per call), and the charge of an ask whose every call is still running is the estimate itself. It holds
    for every configuration the estimate prices, the sole answer model and the staging mocks included, now that each
    model has its own characters per token. Whether a real prompt stays inside its bound is the chars-per-token
    assumption; the meter logs it (``meter_ratio``)."""
    settings = make_settings(**SHAPES[shape])
    estimate = est.estimate_micro(ask_type, settings)
    chars = worst_prompt_chars(ask_type, settings)
    assert est.prompt_tokens(ask_type, settings, LUNA) == tokens_at(chars, LUNA)         # chars / 2.5, up
    assert est.prompt_tokens(ask_type, settings, SONNET) == tokens_at(chars, SONNET)     # chars / 2.0, up
    m, ids = meter_the_worst_ask(ask_type, settings)
    total = sum(r.bound_micro for r in m.calls)
    assert estimate <= total <= estimate + len(ids)
    assert m.charge_micro(estimate) == estimate and m.faults == ()


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("ask_type", ASK_TYPES)
def test_a_worst_case_ask_that_reports_exactly_its_caps_is_charged_what_it_reported(ask_type, shape):
    """Every call reports its caps exactly (the prompt at its model's ratio, the whole output budget): the ledger gets
    the sum of those reports. That is the estimate to within the rounding of each call (under one micro-dollar a call),
    and it can be a micro-dollar or two ABOVE it: a report is never capped (B1)."""
    settings = make_settings(**SHAPES[shape])
    estimate = est.estimate_micro(ask_type, settings)
    m, ids = meter_the_worst_ask(ask_type, settings)
    for record in m.calls:
        tokens_in = tokens_at(record.prompt_chars, priced_as(record))
        tokens_out = est.PLANNER_MAX_TOKENS if record.role == "planner" else settings.llm_answer_max_tokens
        m.complete(record.call_id, {"prompt_tokens": tokens_in, "completion_tokens": tokens_out})
    assert all(r.reported_micro == r.bound_micro for r in m.calls) and m.faults == ()
    assert m.charge_micro(estimate) == sum(r.reported_micro for r in m.calls)
    assert estimate <= m.charge_micro(estimate) <= estimate + len(ids)


# ------------------------------------------------------------------------------------------------ threads

def test_start_and_complete_are_safe_from_two_threads():
    m = meter()
    per_thread, errors = 400, []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=30)
            for _ in range(per_thread):
                call = start_sonnet(m, chars=2_500, out=10)
                m.complete(call, {"prompt_tokens": 1_000, "completion_tokens": 5})
                m.charge_micro(10**9)                                              # readers run alongside the writers
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == [] and len(m.calls) == 2 * per_thread
    assert len({r.call_id for r in m.calls}) == 2 * per_thread and m.faults == ()
    assert m.charge_micro(10**9) == 2 * per_thread * (1_000 * 2 + 5 * 10)            # 2,050 micro-dollars each


# ------------------------------------------------------------------------------------------------- purity

def test_the_meter_imports_no_litellm_and_no_state_backend():
    code = ("import sys; import semigraph.serve.meter; "
            "bad = [m for m in ('litellm', 'neo4j') if m in sys.modules]; "
            "print(','.join(bad)); sys.exit(1 if bad else 0)")
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
