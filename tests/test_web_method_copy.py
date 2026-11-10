"""The Method & Evidence copy of the web app is the legacy page's copy, verbatim (docs/specs/m5b-frontend.md R27, D7, S15).

``web/src/pages/Method.copy.ts`` holds four passages copied from ``src/semigraph/serve/static/index.html``: the header
sentence, the withdrawn-accuracy notice, the reliability note and the checks hint. The web port is a port, not a rewrite, so
each passage must equal the legacy text character for character. vitest cannot do this check (it runs from a mirror outside
the Python tree), so it lives here. The passages are found by their content, never by a line number.

Two legs: the matcher is exercised on a one-character change (a matcher that stopped comparing would turn red), then it runs
on the real files.
"""

import html
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPY_TS = ROOT / "web" / "src" / "pages" / "Method.copy.ts"
INDEX_HTML = ROOT / "src" / "semigraph" / "serve" / "static" / "index.html"

_TEXT = re.compile(r'text: "((?:[^"\\]|\\.)*)"')
_BOLD = re.compile(r'bold: true, text: "((?:[^"\\]|\\.)*)"')
_HREF = re.compile(r'href: "((?:[^"\\]|\\.)*)"')
_HINT_TS = re.compile(r'CHECKS_HINT =\s*"((?:[^"\\]|\\.)*)"')
_HINT_JS = re.compile(r'const hint = "((?:[^"\\]|\\.)*)"')
SEGMENT_EXPORTS = ("INTRO", "WITHDRAWN", "RELIABILITY")
# docs/specs/m5b-redesign.md section 4: the two notes about the research agent moved here from Ask.copy.ts and stay pinned
# against the constants of the same names in the legacy page's script.
STRING_EXPORTS = ("AGENT_HINT", "TRACING_NOTE")
_LITERAL = r'"((?:[^"\\]|\\.)*)"'


def _ts_string(source: str, name: str) -> re.Match[str] | None:
    return re.search(name + r" =\s*" + _LITERAL, source)


def _js_string(page: str, name: str) -> re.Match[str] | None:
    return re.search(r"const " + name + r" = " + _LITERAL, page)


def _js(literal: str) -> str:
    """A JS string literal body (quotes and unicode escapes only) as the text it denotes."""
    return json.loads('"' + literal + '"')


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _block(source: str, name: str) -> str:
    start = source.index(f"export const {name}")
    return source[start : source.index("\n];", start)]


def _page_text(page: str) -> str:
    """The page as the reader sees it: tags dropped, entities decoded, whitespace squashed."""
    return _squash(html.unescape(re.sub(r"<[^>]+>", "", page)))


def _bold_runs(page: str) -> set[str]:
    return {_squash(html.unescape(re.sub(r"<[^>]+>", "", m))) for m in re.findall(r"<b>(.*?)</b>", page, flags=re.S)}


def mismatches(copy_source: str, page: str) -> list[str]:
    """What differs between the TS copy and the page; an empty list means verbatim."""
    flat = _page_text(page)
    bold = _bold_runs(page)
    problems: list[str] = []
    for name in SEGMENT_EXPORTS:
        block = _block(copy_source, name)
        whole = "".join(_js(t) for t in _TEXT.findall(block))
        if _squash(whole) not in flat:
            problems.append(f"{name}: the text is not in index.html")
        for run in _BOLD.findall(block):
            if _squash(_js(run)) not in bold:
                problems.append(f"{name}: bold run {_js(run)!r} is not a <b> run of index.html")
        for href in _HREF.findall(block):
            if f'href="{_js(href)}"' not in page:
                problems.append(f"{name}: link {_js(href)} is not an href of index.html")
    ts_hint = _HINT_TS.search(copy_source)
    js_hint = _HINT_JS.search(page)
    if ts_hint is None or js_hint is None:
        problems.append("CHECKS_HINT: not found")
    elif _js(ts_hint.group(1)) != _js(js_hint.group(1)):
        problems.append("CHECKS_HINT: differs from the hint string of index.html")
    for name in STRING_EXPORTS:
        ts = _ts_string(copy_source, name)
        js = _js_string(page, name)
        if ts is None or js is None:
            problems.append(f"{name}: not found")
        elif _js(ts.group(1)) != _js(js.group(1)):
            problems.append(f"{name}: differs from the {name} string of index.html")
    return problems


@pytest.fixture(scope="module")
def sources() -> tuple[str, str]:
    return COPY_TS.read_text(encoding="utf-8"), INDEX_HTML.read_text(encoding="utf-8")


def test_the_matcher_catches_a_one_character_change(sources):
    copy_source, page = sources
    assert mismatches(copy_source, page) == []
    for needle in ("bitemporal knowledge graph", "withdrawn: the benchmark", "4 of 4 checked cases", "a bare number such as",
                   "a few extra graph lookups", "one-way hash"):
        assert needle in copy_source, needle
        changed = copy_source.replace(needle, needle[:-1] + "X", 1)
        assert mismatches(changed, page) != [], needle


def test_the_matcher_catches_a_changed_link_and_a_dropped_bold(sources):
    copy_source, page = sources
    assert mismatches(copy_source.replace("M1B_PLAN.md", "M1B_PLAN2.md", 1), page) != []
    assert mismatches(copy_source.replace('bold: true, text: "keyword matches', 'bold: true, text: "keyword matchez', 1), page) != []


def test_method_copy_is_verbatim_against_the_legacy_page(sources):
    copy_source, page = sources
    assert mismatches(copy_source, page) == []


def test_every_passage_is_present_and_non_trivial(sources):
    copy_source, _page = sources
    for name in SEGMENT_EXPORTS:
        assert len("".join(_js(t) for t in _TEXT.findall(_block(copy_source, name)))) > 100, name
    assert len(_js(_HINT_TS.search(copy_source).group(1))) > 300
    for name in STRING_EXPORTS:
        assert len(_js(_ts_string(copy_source, name).group(1))) > 100, name


def test_the_copy_file_is_ascii_with_unicode_escapes(sources):
    copy_source, _page = sources
    assert copy_source.isascii()


# ---------------------------------------------------------------- RD13: the hedge phrases and their hand-check counts
# docs/specs/m5b-redesign.md RD13: "Read with care" is shown when an answer holds one of the pinned hedge phrases, and cites
# the measured counts. The phrases are the server's own wording (src/semigraph/retrieval/context_layout.py), and each group's
# counts are the ones the facts file states for that same wording.

CONTEXT_LAYOUT = ROOT / "src" / "semigraph" / "retrieval" / "context_layout.py"
_HEDGE_BLOCK = re.compile(r"export const HEDGES = \{(.*?)\n\} as const;", re.S)
_HEDGE_ENTRY = re.compile(r"(\w+): \{ phrase: " + _LITERAL + r", counts: \[([0-9, ]+)\] \}")


def hedges(copy_source: str) -> dict[str, tuple[str, list[int]]]:
    block = _HEDGE_BLOCK.search(copy_source)
    assert block, "HEDGES not found in Method.copy.ts"
    return {k: (_js(p), [int(n) for n in c.split(",")]) for k, p, c in _HEDGE_ENTRY.findall(block.group(1))}


def test_the_hedge_groups_are_the_three_change_claims(sources):
    assert set(hedges(sources[0])) == {"removed", "unmatchedNew", "passageRemoved"}


def test_every_hedge_phrase_is_the_server_s_own_wording(sources):
    layout = CONTEXT_LAYOUT.read_text(encoding="utf-8").lower()
    for name, (phrase, _counts) in hedges(sources[0]).items():
        assert len(phrase) >= 20, name
        assert phrase.lower() in layout, f"{name}: {phrase!r} is not in context_layout.py"


def test_each_hedge_group_cites_the_counts_the_facts_file_states_for_that_wording(sources):
    copy_source, _page = sources
    text = _squash(" ".join(_js(t) for t in _TEXT.findall(_block(copy_source, "RELIABILITY"))))
    expected = {"removed": "4 of 4 checked cases were gone as a standalone risk factor, but 2 of the 4",
                "unmatchedNew": "only 6 of 12 checked cases were really new",
                "passageRemoved": "6 of the 51 sentences flagged as missing from the newer filing"}
    for name, (phrase, counts) in hedges(copy_source).items():
        assert expected[name] in text, name
        numbers = [int(n) for n in re.findall(r"\d+", expected[name])]
        assert counts == numbers, f"{name}: {counts} != {numbers} (the facts file's counts for this wording)"

