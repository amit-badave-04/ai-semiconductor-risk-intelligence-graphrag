"""The public page (serve/static/index.html): truthful copy, one citation grammar, and its Node unit tests.

The page logic lives in pure functions at the top of the inline script and is unit-tested with ``node --test``
(tests/ui/*.test.mjs, no npm packages); this module runs those tests and pins the copy rules that must never regress.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from semigraph.retrieval import ids

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "src" / "semigraph" / "serve" / "static" / "index.html"
UI_TESTS = sorted((ROOT / "tests" / "ui").glob("*.test.mjs"))


@pytest.fixture(scope="module")
def page() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_node_unit_tests_pass():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the page's pure functions are tested with `node --test tests/ui/*.test.mjs`")
    assert UI_TESTS, "tests/ui/*.test.mjs missing"
    proc = subprocess.run([node, "--test", *map(str, UI_TESTS)], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", timeout=180)
    assert proc.returncode == 0, f"node --test failed:\n{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}"


@pytest.mark.parametrize("stale", [
    "draft verified",                       # wrong for a routed answer: nothing was drafted, nothing was verified
    "all citations verified",               # the check only proves the cited ids were retrieved
    "95% correct", "75% correct", "100% correct",   # measured by an instrument that could not see false drops
    "one Claude call",                      # the answer model is configured, not always Claude
    "dropped risk lineages",                # the old stat counted edges, not lineages
    "deleted_risk_lineages",
    "text-verified removed",                # held-out gold: 2 of 4 "removed" items were merged into another risk factor
    "text verified absent", "verified absent",
    "removed lists do not support",         # the claim-check copy now names the "no longer appears" lists
    "stop disclosing in its latest",        # the placeholder question presupposed the claim the service cannot make
])
def test_page_never_carries_the_withdrawn_claims(page, stale):
    assert stale not in page


def test_header_separates_verbatim_evidence_from_keyword_matched_rules(page):
    assert "verbatim filing excerpt" in page
    assert "keyword matches to external Federal Register rules" in page


def test_page_reads_the_new_stat_and_says_accuracy_is_withdrawn(page):
    assert "removed_risk_items" in page                       # the API key is the contract; only the copy changed
    assert "risk factors that no longer stand alone (text check)" in page
    assert "paragraphs that no longer stand alone (text check)" in page
    assert "withdrawn" in page and "re-measurement" in page


def test_page_states_how_reliable_change_claims_are_in_plain_words_with_the_measured_counts(page):
    """The held-out gold (2 blind annotators, ties adjudicated): the four classes of change claim and what each measured.
    The counts are small samples and are stated as counts, never as percentages (the page must never carry "100% correct")."""
    match = re.search(r'<p id="reliability">(.*?)</p>', page, re.S)
    assert match and "How reliable are change claims?" in match.group(1)
    assert page.index('<p id="reliability">') > page.index("<footer>")                      # the method / limits area
    note = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1)))
    for fragment in (
        "no longer appears as a separate risk factor", "4 of 4", "folded into another risk factor",   # removed items
        "Not matched", "could not settle", "never shown as removed",                                  # unsettled items
        "No matching risk factor found in the earlier filing", "6 of 12", "already disclosed earlier",   # new or restructured
        "wording was not found", "6 of the 51", "1 of 48", "different words",                         # sentences
        "small",                                                                                     # the samples are small
    ):
        assert fragment in note, fragment
    assert "%" not in note and "verified" not in note.replace("could not be verified", "")   # counts, not percentages; no "verified"


def test_the_placeholder_question_asks_for_risk_factors_that_no_longer_appear_not_ones_the_company_stopped_disclosing(page):
    placeholder = re.search(r'id="q"[^>]*placeholder="([^"]*)"', page).group(1)
    assert "no longer appear" in placeholder and "stop disclosing" not in placeholder


def test_vector_option_is_kept_and_labelled_as_a_baseline(page):
    assert 'value="vector"' in page
    assert "comparison baseline" in page and "no temporal reasoning" in page


def test_only_the_existing_external_script_and_inline_script_are_used(page):
    # Inline script is allowed by the CSP; the only external script is Cloudflare Turnstile, created at run time.
    assert len(re.findall(r"<script\b", page)) == 1
    assert "<script src" not in page
    externals = set(re.findall(r"https://[a-z0-9.\-]+/[^\"' )]*\.js[^\"' )]*", page))
    assert externals == {"https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onTurnstile"}


def test_turnstile_placeholder_is_substituted_exactly_once(page):
    assert page.count("__TURNSTILE_SITE_KEY__") == 1


@pytest.mark.parametrize("name,pattern", [
    ("CHUNK_ID", ids.CHUNK_ID_PATTERN), ("XBRL_ID", ids.XBRL_ID_PATTERN), ("FR_ID", ids.FR_ID_PATTERN),
    ("DOC_ID", ids.DOC_ID_PATTERN),
])
def test_page_citation_grammar_matches_retrieval_ids(page, name, pattern):
    """The page must recognise exactly the ids the answerer is allowed to cite (retrieval/ids.py is the source)."""
    match = re.search(rf"const {name} = /(.*?)/\.source;", page)
    assert match, f"{name} not defined as a regex literal in the page"
    assert match.group(1) == pattern


# ---- M5b I0b: what 208364a left open on the live page (keyboard path of the chips, status line, question label, landmark).
# The behaviour is tested in Node (tests/ui/a11y.test.mjs, against a focus-aware fake DOM); these pins are the part a vm cannot
# see: the markup and CSS the browser parses. Nothing here adds a script, an inline style attribute or an origin (the CSP is
# untouched: tests/test_serve_agent_ui.py pins it).


def script_of(page: str) -> str:
    return re.search(r"<script>(.*?)</script>", page, re.S).group(1)


def markup_of(page: str) -> str:
    return re.sub(r"<script>.*?</script>", "", page, flags=re.S)


def open_tag(page: str, element_id: str) -> str:
    match = re.search(rf'<[a-z0-9]+\s[^>]*\bid="{element_id}"[^>]*>', markup_of(page))
    assert match, f"no element with id={element_id!r} in the page markup"
    return match.group(0)


def css_rule(page: str, selector: str) -> str:
    """The declarations of the rule for exactly ``selector`` (rules may share a line)."""
    match = re.search(rf"(?:^|\}})\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", page, re.M)
    assert match, f"no CSS rule for {selector}"
    return match.group(1)


def test_every_citation_chip_the_script_builds_is_a_focusable_button_made_by_one_helper(page):
    chips = re.findall(r'<span class="cite"[^>]*>', script_of(page))
    assert len(chips) == 1, "one helper builds the chip of the answer and of the what-changed view"
    assert 'role="button"' in chips[0] and 'tabindex="0"' in chips[0]
    assert "data-id=" in chips[0] and "title=" in chips[0]


def test_a_chip_has_a_visible_focus_ring_and_both_chip_containers_handle_click_keydown_and_keyup(page):
    assert "outline" in css_rule(page, ".cite:focus-visible")
    script = script_of(page)
    for container in ("answer", "wsChanges"):
        for event in ("click", "keydown", "keyup"):
            assert re.search(rf'\$\("{container}"\)\.addEventListener\("{event}"', script), (container, event)


def test_the_status_line_is_a_polite_live_region_and_the_streamed_answer_is_not(page):
    status = open_tag(page, "status")
    assert 'role="status"' in status and 'aria-live="polite"' in status
    answer = open_tag(page, "answer")
    assert not re.search(r"aria-(live|atomic|relevant)|role=\"(status|alert|log)\"", answer)
    assert 'aria-live="polite" aria-relevant="additions"' in markup_of(page)        # the steps timeline keeps its own region


def test_the_question_box_has_a_label_that_is_hidden_visually_but_not_from_assistive_technology(page):
    label = re.search(r'<label\b([^>]*\bfor="q"[^>]*)>(.*?)</label>', markup_of(page), re.S)
    assert label, "no <label for=q>"
    assert re.sub(r"<[^>]+>", "", label.group(2)).strip()
    assert "style=" not in label.group(1) and not re.search(r"\bhidden\b|aria-hidden", label.group(1))
    declarations = css_rule(page, "." + re.search(r'class="([^"]+)"', label.group(1)).group(1)).replace(" ", "")
    assert "position:absolute" in declarations and "clip" in declarations         # out of sight, still in the accessibility tree
    assert "display:none" not in declarations and "visibility:hidden" not in declarations
    assert "placeholder=" in open_tag(page, "q")                                    # the example question stays


def test_the_evidence_drawer_is_a_named_complementary_landmark(page):
    tag = open_tag(page, "drawer")
    assert tag.startswith("<aside ") and 'aria-label="Evidence"' in tag
    assert "role=" not in tag, "a role override would stop it being the complementary landmark"


def test_a_stale_opener_falls_back_to_a_heading_that_script_can_focus_but_that_is_not_a_tab_stop(page):
    heading = open_tag(page, "askHeading")
    assert heading.startswith("<h2 ") and 'tabindex="-1"' in heading
    assert '$("askHeading")' in script_of(page)


def test_no_attribute_in_the_markup_is_glued_to_the_one_before_it(page):
    """A browser recovers from `id="q"maxlength="500"`, but it is a parse error, and an edit of the markup is how it gets in."""
    assert not re.findall(r'<[a-z0-9]+\s[^>]*?="[^"]*"[a-zA-Z][^>]*>', markup_of(page))


def test_the_header_counts_the_sec_filers_it_covers(page):
    """D8: Samsung is in the 14-company universe but has no SEC filings, so the line says 13 (the never-overclaim rule)."""
    assert "SEC filings of 13 keystone companies" in page
    assert "14 keystone" not in page
