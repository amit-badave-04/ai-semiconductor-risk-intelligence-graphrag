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
    script = script_of(page)
    # Round-7 verification: the click handler moved into askExample(), which also re-syncs the agent hint at once.
    assert 'b.addEventListener("click", () => askExample(b.dataset.q))' in script
    assert re.search(r'\$\("q"\)\.value = question; \$\("strategy"\)\.value = "hybrid"; syncAgentUi\(lastAgentStats\); ask\(\);',
                     function_source(script, "askExample"))


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


# ---- keyboard and assistive-technology structure of the evidence drawer and the upload drop zone (M5 decisions 2.3).
# The behaviour is tested in Node (tests/ui/a11y.test.mjs, against a focus-aware fake DOM built from this markup); these pins
# are the part a vm cannot see: the markup the browser parses before any script runs, and the CSS fallback.


def open_tag(page: str, element_id: str) -> str:
    """The opening tag of the element with this id, from the markup outside the script."""
    markup = re.sub(r"<script>.*?</script>", "", page, flags=re.S)
    match = re.search(rf'<[a-z0-9]+\s[^>]*\bid="{element_id}"[^>]*>', markup)
    assert match, f"no element with id={element_id!r} in the page markup"
    return match.group(0)


def css_rule(page: str, selector: str) -> str:
    """The declarations of the rule for exactly ``selector`` (rules may share a line)."""
    match = re.search(rf"(?:^|\}})\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", page, re.M)
    assert match, f"no CSS rule for {selector}"
    return match.group(1)


def test_the_closed_evidence_drawer_is_inert_and_aria_hidden_in_the_markup_so_first_load_is_right_without_script(page):
    tag = open_tag(page, "drawer")
    assert re.search(r"\sinert[\s>]", tag), "inert removes a closed drawer's links and buttons from the tab order"
    assert 'aria-hidden="true"' in tag
    assert 'aria-label="Close evidence"' in open_tag(page, "drawerClose")      # the "×" alone is not a name


def test_the_closed_drawer_is_also_visibility_hidden_in_css_as_a_fallback_for_browsers_without_inert(page):
    closed, opened = css_rule(page, "#drawer"), css_rule(page, "#drawer.open")
    assert "visibility:hidden" in closed.replace(" ", "")
    assert "visibility:visible" in opened.replace(" ", "")
    assert "visibility" in re.search(r"transition:([^;]*)", closed).group(1), "the slide-out finishes before it is hidden"


def test_the_drawer_is_opened_and_closed_only_through_openDrawer_and_closeDrawer_so_inert_aria_and_focus_cannot_drift(page):
    script = script_of(page)
    assert script.count('classList.add("open")') == 1 and script.count('classList.remove("open")') == 1
    opening, closing = function_source(script, "openDrawer"), function_source(script, "closeDrawer")
    assert 'classList.add("open")' in opening and 'removeAttribute("inert")' in opening and 'removeAttribute("aria-hidden")' in opening
    assert '$("drawerClose").focus()' in opening, "opening moves focus into the drawer"
    assert 'classList.remove("open")' in closing and 'setAttribute("inert"' in closing and 'setAttribute("aria-hidden", "true")' in closing
    assert ".focus()" in closing, "closing returns focus to the opener"
    assert '$("drawerClose").addEventListener("click", closeDrawer)' in script
    assert re.search(r'e\.key === "Escape"\) closeDrawer\(\)', script)
    assert "openDrawer(opener)" in function_source(script.replace("async function openCitation", "function openCitation"), "openCitation")
    assert "openCitation(chip.dataset.id, chip)" in script            # both delegated chip handlers pass the opener


def test_the_upload_drop_zone_is_a_focusable_button_with_a_visible_focus_style_and_keeps_its_text(page):
    tag = open_tag(page, "wsDrop")
    assert tag.startswith("<div ") and 'role="button"' in tag and 'tabindex="0"' in tag
    # a <label> may not carry role="button" (ARIA in HTML), so the zone is a div; the file input stays its SIBLING (a click on
    # a descendant input would bubble back into the zone's own click handler)
    zone = re.search(r'<div id="wsDrop".*?</div>', page, re.S).group(0)
    assert "<input" not in zone and 'type="file"' in open_tag(page, "wsFile") and 'accept="' in open_tag(page, "wsFile")
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", zone)).strip()
    assert text == "Drag a PDF, DOCX, Markdown, TXT or HTML file here, or click to choose one (up to 30 pages)."
    assert "outline" in css_rule(page, ".drop:focus-visible")
    script = script_of(page)
    assert re.search(r'drop\.addEventListener\("keydown"', script) and '"Enter"' in script and '" "' in script
    assert re.search(r'drop\.addEventListener\("click", \(\) => \$\("wsFile"\)\.click\(\)\)', script)


def test_the_chunk_evidence_fields_the_page_reads_are_fields_the_api_returns(page):
    """/api/evidence returns {"type": "chunk", **EVIDENCE_QUERY's columns}; a field the page reads that no response carries is
    dead (the page used to read a singular ``headline``; only ``item_headlines`` exists)."""
    clause = re.sub(r"\s+", " ", routes.EVIDENCE_QUERY.split("RETURN", 1)[1])
    columns = {"type"}
    for item in (part.strip() for part in clause.split(",")):
        alias = re.search(r"\bAS (\w+)$", item)
        columns.add(alias.group(1) if alias else item)
    script = script_of(page)
    chunk_branch = re.search(r"\} else \{\n\s+const mentions = .*?\n  \}\n  return view;", function_source(script, "evidenceView"), re.S)
    assert chunk_branch, "the chunk branch of evidenceView moved: update this pin"
    read = set(re.findall(r"\bd\.(\w+)", chunk_branch.group(0))) | set(re.findall(r"\bd\.(\w+)", function_source(script, "freshnessHtml")))
    assert read and read <= columns, f"the page reads chunk-evidence fields the API does not return: {sorted(read - columns)}"


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
