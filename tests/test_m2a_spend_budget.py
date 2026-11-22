"""M2a spend budget (council decision docs/decisions/005-agent-spend-budget-m2a.md).

Pins the council plan: the four live estimates stay under their targets, the per-address share window still contains the
live share, the agent's worst-case render fits its allowances with R4 off, partial R4 is measured not to fit (so it is not
shipped), and an agent answer that cites only the current-year id on a computed note still passes the answer checks.
Offline: no model, no database, no network. The targets are literals on purpose (never read back from the module).
"""

import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from semigraph.agent.merge import without_detail_lines  # noqa: E402
from semigraph.config import Settings  # noqa: E402
from semigraph.retrieval import answerer  # noqa: E402
from semigraph.retrieval.context_layout import (  # noqa: E402
    DETAIL_LINES_KEY,
    YOY_NAMED,
    YOY_OFF,
    YOY_PRIOR_IDS_KEY,
)
from semigraph.retrieval.verify import answer_checks, failed_check_names  # noqa: E402
from semigraph.serve import estimate as est  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# Targets after the 2026-11-22 re-measurement (decision 005, Follow-ups F5): hybrid and workspace 0.755 so seven full shares leave real
# room, agent 1.0175 (the old $1.02 ceiling less a margin). Workspace: R6b off on the workspace paths.
TARGET_USD = {"hybrid": 0.755, "agent": 1.0175, "vector": 0.27, "workspace": 0.755}
# The share window's margins, in USD (decision 005 F5): the agent side keeps 2,500 micro-dollars, the hybrid side 500, and seven full
# shares plus a workspace ask keep 500 micro-dollars of the day cap.
AGENT_SIDE_MARGIN, HYBRID_SIDE_MARGIN, WORKSPACE_DAY_MARGIN = 0.0025, 0.0005, 0.0005
LIVE_SHARE, DAY_CAP, AGENT_SETTLED_BEFORE = 1.32, 10.0, 0.30
AGENT_ALLOWANCE_PER_COMPANY, AGENT_ALLOWANCE_TOTAL = 50_000, 466_624      # 4 companies x 50,000 + the shared context
LIVE_FIELDS = ("answer_model", "escalation_model", "agent_planner_model", "llm_answer_max_tokens",
               "llm_input_price_per_mtok", "llm_output_price_per_mtok", "agent_max_model_calls", "agent_max_tool_calls",
               "max_question_chars")


def live_settings() -> Settings:
    env = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))["env"]
    return Settings(_env_file=None, **{n: env.get(n.upper(), Settings.model_fields[n].default) for n in LIVE_FIELDS})


def usd(ask_type: str) -> float:
    return est.estimate_micro(ask_type, live_settings()) / 1_000_000


# --- (5) the estimates stay under their targets; the share window contains the live share -------------------------------

@pytest.mark.parametrize("ask_type", sorted(TARGET_USD))
def test_each_live_estimate_is_within_its_target(ask_type):
    assert usd(ask_type) <= TARGET_USD[ask_type], (ask_type, usd(ask_type))


def test_the_share_window_still_contains_the_live_share():
    """Rule (ii): the buyer demo's second agent ask is admitted whole: 0.30 + agent <= S. Rule (i'): seven full shares leave
    room for a hybrid ask: S <= (10 - hybrid) / 7. Both ends are rounded, so the live S must sit strictly inside."""
    lower, upper = AGENT_SETTLED_BEFORE + usd("agent"), (DAY_CAP - usd("hybrid")) / 7
    assert lower <= LIVE_SHARE <= upper, (lower, LIVE_SHARE, upper)
    assert LIVE_SHARE - lower > 0.0001 and upper - LIVE_SHARE > 0.0001, "the window has no headroom left"


def test_the_share_window_keeps_its_margins_and_seven_full_shares_still_admit_a_workspace_ask():
    """The council's intent (decision 005): real margins, not a window touched at both ends. In micro-dollars, to the unit:
    0.30 + agent <= share - 0.0025; (10 - hybrid) / 7 >= share + 0.0005; 7 x share + workspace <= 10 - 0.0005."""
    share, cap, settled = round(LIVE_SHARE * 1e6), round(DAY_CAP * 1e6), round(AGENT_SETTLED_BEFORE * 1e6)
    agent, hybrid, workspace = (est.estimate_micro(t, live_settings()) for t in ("agent", "hybrid", "workspace"))
    margin = lambda x: round(x * 1e6)   # noqa: E731
    assert settled + agent <= share - margin(AGENT_SIDE_MARGIN), (settled + agent, share)
    assert (cap - hybrid) / 7 >= share + margin(HYBRID_SIDE_MARGIN), ((cap - hybrid) / 7, share)
    assert 7 * share + workspace <= cap - margin(WORKSPACE_DAY_MARGIN), (7 * share + workspace, cap)


def test_the_agent_ceiling_is_not_raised_and_its_per_company_allowance_stays_at_50000():
    assert est.AGENT_GRAPH_CHARS_PER_COMPANY == AGENT_ALLOWANCE_PER_COMPANY
    assert usd("agent") <= TARGET_USD["agent"] and TARGET_USD["agent"] <= 1.0175


# --- (2) the agent render fits its allowances with R4 off; partial R4 does not ----------------------------------------------

def _test_module():
    import test_serve_estimate as t
    return t


def test_the_agents_worst_case_render_fits_both_allowances():
    t = _test_module()
    per_company = {p: t._company_chars(pairs=p, metric_rows=19, show_all=True, agent=True) for p in range(1, t.AGENT_PAIRS + 1)}
    assert max(per_company.values()) <= AGENT_ALLOWANCE_PER_COMPANY, per_company
    assert est.prompt_chars("agent", live_settings()) == AGENT_ALLOWANCE_TOTAL
    assert t._prompt_chars(t.agent_worst_case(t.AGENT_PAIRS)) <= est.prompt_chars("agent", live_settings())


def test_partial_r4_was_measured_for_the_agent_and_does_not_fit_so_the_agent_ships_with_r4_off():
    """The council's gate: partial R4 ships only if the worst case is <= 50,000 characters per company and <= 466,524 in all.
    Measured: 51,676 per company at the 4 pairs the agent holds (51,807 at 5), so it does not ship; R4 is off for the agent."""
    t = _test_module()
    named = t.worst_case_retrieval(2, pairs=t.AGENT_PAIRS, metric_rows=19, show_all=True)
    one = t.worst_case_retrieval(1, pairs=t.AGENT_PAIRS, metric_rows=19, show_all=True)
    switch = {DETAIL_LINES_KEY: False, YOY_PRIOR_IDS_KEY: YOY_NAMED}
    per_company = t._prompt_chars({**named, **switch}) - t._prompt_chars({**one, **switch})
    assert per_company > AGENT_ALLOWANCE_PER_COMPANY, "partial R4 now fits: re-decide R4 for the agent (decision 005)"
    shipped = without_detail_lines({})
    assert shipped[YOY_PRIOR_IDS_KEY] == YOY_OFF and shipped[DETAIL_LINES_KEY] is False


# --- partial R4 mechanics (kept for the day the budget allows it) -------------------------------------------------------------

def _series(years: int) -> list[dict]:
    return [{"cik": 1, "company": "Acme", "metric": "revenue", "value": 100.0 + 10 * (years - y), "unit": "USD",
             "period_start": f"{2024 - y}-01-28", "period_end": f"{2025 - y}-01-26"} for y in range(years)]


def test_partial_r4_gives_the_prior_id_to_the_latest_row_and_the_named_rows_only():
    rows = _series(8)
    lines, _ = answerer.metrics_lines(rows, years=[2020], yoy_prior_ids=YOY_NAMED)
    own = [ln for ln in lines if ln.startswith("  computed:")]
    inline = [ln for ln in lines if ln.startswith("- ") and " | computed:" in ln]
    assert len(own) == 2                                 # the latest row and the named 2020 row
    assert all("[xbrl:1:revenue:" in ln.split("vs fiscal year ended")[1] for ln in own)
    assert len(inline) == 3                              # rows 2 and 3 and the 2020 row's base keep today's inline note
    assert any("fiscal year ended 2025-01-26" in ln and "[xbrl:1:revenue:2025-01-26]" in ln for ln in own)


def test_r4_off_is_todays_inline_note_and_every_row_is_kept():
    rows = _series(6)
    off, _ = answerer.metrics_lines(rows, yoy_prior_ids=YOY_OFF)
    full, _ = answerer.metrics_lines(rows)
    assert not [ln for ln in off if ln.startswith("  computed:")]
    assert len([ln for ln in off if ln.startswith("- ")]) == len([ln for ln in full if ln.startswith("- ")]) == 3
    assert all(" | computed:" in ln for ln in off if ln.startswith("- "))


# --- (6) citation integrity under the shipped agent mode ------------------------------------------------------------------------

def _agent_retrieval() -> dict:
    return {"anchors": {"Acme": 1}, "edges": [], "risks": [], "chunks": [], "temporal": [], "temporal_pairs": [],
            "metrics": _series(3), "metric_periods": {}, "computed": []}


def test_an_agent_year_over_year_answer_citing_only_the_current_year_id_passes_the_answer_checks():
    """R4 is off for the agent, so the computed note sits inline on the current-year metric line and carries no base-year id.
    An answer that states the change and cites only that line's id is grounded and passes every check."""
    r = without_detail_lines(_agent_retrieval())
    _, context, valid = answerer.build_blocks(r)
    current = "xbrl:1:revenue:2025-01-26"
    assert current in valid and "xbrl:1:revenue:2024-01-26" in valid
    line = next(ln for ln in context.splitlines() if f"[{current}]" in ln and ln.startswith("- "))
    assert "computed: +" in line, "the note must ride on the current-year line"
    text = f"Acme's revenue rose 8.3% in the fiscal year ended January 26, 2025 [{current}]."
    checks = answer_checks(text, {current}, valid, context, sources=answerer.sources_from_context(context),
                           question="How did Acme's revenue change year over year?")
    assert checks.numbers_checked == 1 and failed_check_names(checks.as_dict()) == [], checks


def test_an_invented_year_over_year_percentage_is_still_flagged_in_agent_mode():
    r = without_detail_lines(_agent_retrieval())
    _, context, valid = answerer.build_blocks(r)
    current = "xbrl:1:revenue:2025-01-26"
    text = f"Acme's revenue rose 31.4% in the fiscal year ended January 26, 2025 [{current}]."
    checks = answer_checks(text, {current}, valid, context, sources=answerer.sources_from_context(context),
                           question="How did Acme's revenue change?")
    assert failed_check_names(checks.as_dict()) == ["ungrounded_number"]
