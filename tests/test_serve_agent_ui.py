"""The static page and the opt-in agent: what the served HTML must (and must not) contain.

The behaviour of the page's functions is tested in Node (tests/ui/agent.test.mjs, run by tests/test_static_ui.py); this module pins
the structure that a unit test of a function cannot see: the option exists only in script (so nothing agent-related is in the
markup while the flag is off), the timeline is a polite live region, server text never reaches innerHTML, nothing moves, and the
page stays inside the CSP."""

import re
from pathlib import Path

import pytest

from semigraph.serve import routes

INDEX = Path(routes.__file__).parent / "static" / "index.html"


@pytest.fixture(scope="module")
def page() -> str:
    return INDEX.read_text(encoding="utf-8")


def script_of(page: str) -> str:
    return re.search(r"<script>(.*?)</script>", page, re.S).group(1)


def function_source(script: str, name: str) -> str:
    """The text of ``function name(...) { ... }`` up to the next top-level declaration."""
    match = re.search(rf"^function {name}\(.*?^}}", script, re.S | re.M)
    assert match, f"function {name} not found at the top level of the page script"
    return match.group(0)


def test_the_agent_option_is_created_by_script_never_present_in_the_markup(page):
    markup = re.sub(r"<script>.*?</script>", "", page, flags=re.S)
    assert 'value="agent"' not in markup                                   # the strategy select ships hybrid and vector only
    assert "Deep research" not in markup and "research agent" not in re.sub(r'aria-label="[^"]*"', "", markup)
    assert 'querySelector(\'option[value="agent"]\')' in script_of(page)


def test_the_agent_containers_are_hidden_until_the_service_reports_the_agent_on(page):
    for element in ("agentHint", "tracingNote", "agentSteps"):
        assert re.search(rf'id="{element}"[^>]*\bhidden\b', page), element


def test_the_timeline_is_a_polite_live_region_that_announces_additions(page):
    match = re.search(r'<div class="steps"([^>]*)><ol id="agentSteps"([^>]*)></ol></div>', page)
    assert match, "the timeline must be an <ol id=agentSteps> inside <div class=steps>"
    assert 'aria-live="polite"' in match.group(1) and 'aria-relevant="additions"' in match.group(1)
    assert 'aria-label="What the research agent did"' in match.group(2)


def test_server_text_of_a_step_never_reaches_innerhtml(page):
    script = script_of(page)
    for name in ("stepText", "addStep", "clearSteps", "syncAgentUi", "agentEnabled", "tracingNote"):
        body = function_source(script, name) if name not in ("agentEnabled", "tracingNote") else next(
            line for line in script.splitlines() if line.startswith(f"const {name} ="))
        assert "innerHTML" not in body and "insertAdjacentHTML" not in body and "outerHTML" not in body, name
    assert "textContent = stepText(ev)" in function_source(script, "addStep")


def test_the_stream_handler_routes_step_events_to_the_timeline_and_ask_sends_the_selected_strategy(page):
    script = script_of(page)
    assert re.search(r'if \(event === "step"\) \{ addStep\(ev\); \}\s*\n\s*else if \(event === "retrieval"\)', script)
    ask = function_source(script.replace("async function ask", "function ask"), "ask")
    assert 'const strategy = $("strategy").value' in ask and "strategy, turnstile_token" in ask and "clearSteps()" in ask


def test_the_examples_still_reset_the_strategy_to_hybrid_so_a_saved_answer_is_never_asked_live_as_the_agent(page):
    assert '$("q").value = b.dataset.q; $("strategy").value = "hybrid"; ask();' in script_of(page)


def test_nothing_in_the_new_ui_moves(page):
    css = re.search(r"\.steps ol \{[^}]*\}.*?\.steps li \{[^}]*\}", page, re.S).group(0)
    assert "transition" not in css and "animation" not in css and "transform" not in css


def test_the_page_stays_inside_the_csp_one_script_no_new_origin(page):
    assert len(re.findall(r"<script\b", page)) == 1 and "<script src" not in page
    fetches = re.findall(r"fetch\(([^,)]*)", script_of(page))
    assert fetches and all(f.strip().startswith(("\"/api/", "`/api/")) for f in fetches)     # connect-src 'self'
    assert "connect-src 'self'" in routes.CSP and "'unsafe-inline'" in routes.CSP


def test_the_new_copy_makes_no_withdrawn_claim_and_says_what_a_trace_holds(page):
    script = script_of(page)
    hint = re.search(r'const AGENT_HINT = "(.*?)";', script).group(1)
    note = re.search(r'const TRACING_NOTE = "(.*?)";', script).group(1)
    assert "same checks" in hint and "verified" not in hint + note
    assert "never the text of the question or the answer, and never your address" in note
    assert "sampling rate" in note and "small sample" not in note                          # true at any rate, including 1.0
    for part in ("length of the question", "one-way hash", "tools and models", "token counts", "cost", "timings", "fallback reason"):
        assert part in note, part


def test_the_served_page_carries_the_agent_containers_and_the_turnstile_substitution(page):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class Settings:
        turnstile_site_key = "site-key-123"

    app = FastAPI()
    app.include_router(routes.router)
    app.state.settings = Settings()
    body = TestClient(app).get("/").text
    assert 'id="agentSteps"' in body and "site-key-123" in body and "__TURNSTILE_SITE_KEY__" not in body
