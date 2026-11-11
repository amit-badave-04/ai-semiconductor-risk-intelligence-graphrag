"""The web app's copy deck (docs/specs/m5b-redesign.md section 4, replacing tests/test_web_ask_copy.py).

Every user-facing sentence semigraph writes lives in ``web/src/copy/deck.ts``. Facts that must stay exact live in the facts file
(``web/src/pages/Method.copy.ts``, pinned by tests/test_web_method_copy.py) and in ``Company.copy.ts`` (the ``KIND_*`` labels);
the four lib files with differential tests keep their legacy ``text``. vitest cannot read the Python tree (it runs from a
mirror), so these checks live here. Five parts, as the spec numbers them:

1. the matcher is exercised (a matcher that stopped finding sentences would turn red);
2. every sentence-like literal under ``web/src`` lives in one of those places. Files the redesign has not reached yet are listed
   in ``NOT_YET_IN_THE_DECK`` with the exact number of sentences they hold, so a new sentence cannot be added to one silently;
   the list only shrinks, and is empty when R4 ends;
3. ``artifacts/m5b_copy_deck.json`` is a recorded snapshot of the deck, so every wording change is a reviewable diff
   (``UPDATE_COPY_DECK=1 uv run pytest tests/test_web_copy_deck.py`` rewrites it);
4. fact invariants: the rule notice names the Federal Register, keyword matching and "not statements"; the measured counts are
   present; no change label says "removed" or "dropped"; no "verified", "unlimited" or "AI-powered" in the deck;
5. the cost line's default form is the approved sentence (decision D-R5) and its numbers are the Settings defaults.

Until a file folded into the deck, the legacy parity checks of the old test ran on it (``Ask.copy.ts`` and the four lib
files against the legacy page).
"""

import html
import json
import os
import re
from pathlib import Path

import pytest

from semigraph.config import Settings

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "src"
DECK_TS = WEB / "copy" / "deck.ts"
METHOD_TS = WEB / "pages" / "Method.copy.ts"
COMPANY_TS = WEB / "pages" / "Company.copy.ts"
ASK_CONSTANTS_TS = WEB / "pages" / "Ask.constants.ts"
ASK_STATUS_TS = WEB / "lib" / "askStatus.ts"
ASK_COPY_TS_RETIRED = WEB / "pages" / "Ask.copy.ts"
LIB_FILES = [WEB / "lib" / name for name in ("answerMeta.ts", "steps.ts", "evidenceView.ts", "askStatus.ts")]
INDEX_HTML = ROOT / "src" / "semigraph" / "serve" / "static" / "index.html"
ROUTES_PY = ROOT / "src" / "semigraph" / "serve" / "routes.py"
SNAPSHOT = ROOT / "artifacts" / "m5b_copy_deck.json"

# Where sentences may live (paths relative to web/src).
COPY_HOMES = {"copy/deck.ts", "pages/Method.copy.ts", "lib/answerMeta.ts", "lib/steps.ts", "lib/evidenceView.ts",
              "lib/askStatus.ts"}
# Files the redesign has not reached yet, with the number of sentences each holds today (R1 to R4 empty this list).
NOT_YET_IN_THE_DECK: dict[str, int] = {}
# docs/specs/m5b-redesign.md section 4: Company.copy.ts keeps only the four KIND_* labels (pinned by the server's context layout); it
# is a copy home only while those are all it exports, so no other copy can slip into it under the sentence counter.
KIND_HOME = "pages/Company.copy.ts"
KIND_EXPORTS = {"KIND_NEW", "KIND_DROPPED", "KIND_UNSETTLED", "KIND_CHANGED"}
# Strings with spaces that are not copy: a link's rel value and a start-up error no reader sees.
NOT_COPY = {"noopener noreferrer", "#root is missing from index.html"}

_DOUBLE = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
_TEMPLATE = re.compile(r"`((?:[^`\\]|\\.)*)`")
# One left-to-right pass over the three quote kinds, so an apostrophe inside a double-quoted string never opens a literal.
_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`|\'((?:[^\'\\\n]|\\.)*)\'')
_SUBST = re.compile(r"\$\{[^}]*\}")
# JSX text: after a tag or an expression, up to the next tag or expression.
_JSX_TEXT = re.compile(r"[>}]([^<>{}`;=]*[A-Za-z][^<>{}`;=]*)(?=[<{])")
_CONST = re.compile(r"export const (\w+) =\s*((?:\"(?:[^\"\\]|\\.)*\")|\d+);", re.S)
_WORD = re.compile(r"[A-Za-z][a-z']*")
_PUNCTUATION = ".,;:!?()—…\"'"
_MIN_SENTENCE = 6  # characters of a fragment worth pinning (shorter ones are punctuation and single words)
# Bare utility classes that are also English words: they never make a class list look like a sentence.
_TAILWIND_WORDS = {"flex", "grid", "block", "inline", "hidden", "contents", "table", "underline", "uppercase", "lowercase",
                   "capitalize", "truncate", "italic", "relative", "absolute", "fixed", "sticky", "static", "border",
                   "rounded", "shadow", "outline", "grow", "shrink", "container", "invisible", "visible", "isolate",
                   "antialiased", "ring", "peer", "group", "prose", "resize", "overline", "collapse", "transparent"}
# Words that put text between a "}" and a "<" in code, not in a JSX text node ("} else return <p>").
_CODE_WORDS = {"return", "else", "const", "let", "var", "export", "function", "if", "import", "default", "await", "case", "new",
               "class", "interface", "type", "enum", "override", "static", "async", "private", "public", "protected",
               "readonly", "declare", "extends", "implements"}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _js(literal: str) -> str:
    """The text a JS string literal body denotes (escapes of quotes, backslashes and unicode)."""
    body = literal.replace("\\'", "'")
    return json.loads('"' + re.sub(r'(?<!\\)"', r'\\"', body) + '"')


def _plain_words(text: str) -> list[str]:
    stripped = (token.strip(_PUNCTUATION) for token in text.split())
    return [w for w in stripped if _WORD.fullmatch(w) and w.lower() not in _TAILWIND_WORDS]


def _is_sentence(text: str) -> bool:
    """Two or more plain English words: copy. Class lists, paths and ids have none (digits and periods do not matter)."""
    text = text.strip()
    return len(text) >= _MIN_SENTENCE and len(_plain_words(text)) >= 2


def _code_only(source: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith(("//", "import ", "* ")))
    return re.sub(r"(?<=[;,{}) ])\s//[^\n]*", "", code)


def sentence_fragments(source: str, jsx: bool = False) -> list[str]:
    """The sentence-like pieces of the string literals (and, for .tsx, the JSX text) of a TypeScript file, in order."""
    code = _code_only(source)
    pieces: list[tuple[int, str]] = []
    if jsx:
        for m in _JSX_TEXT.finditer(code):
            if not _CODE_WORDS & set(m.group(1).split()):
                pieces.append((m.start(), m.group(1)))
        # blank the JSX text, so an apostrophe in it ("Nvidia's") never opens a single-quoted literal
        code = _JSX_TEXT.sub(lambda m: m.group(0)[0] + " " * len(m.group(1)), code)
    for m in _LITERAL.finditer(code):
        double, template, single = m.groups()
        if template is not None:
            pieces += [(m.start(), _js(part)) for part in _SUBST.split(template)]
        else:
            pieces.append((m.start(), _js(double if double is not None else single)))
    ordered = sorted(pieces, key=lambda piece: piece[0])  # stable: the parts of one template keep their order
    return [text for _, text in ordered if _is_sentence(text) and text.strip() not in NOT_COPY]


def file_fragments(path: Path) -> list[str]:
    return sentence_fragments(path.read_text(encoding="utf-8"), jsx=path.suffix == ".tsx")


def source_files() -> list[Path]:
    return sorted(
        p for p in WEB.rglob("*.ts*")
        if "__tests__" not in p.parts and p.relative_to(WEB).parts[0] != "test" and not p.name.endswith((".d.ts", ".generated.ts"))
        and p.relative_to(WEB).parts[:2] != ("components", "ui")
    )


def kind_home_exports() -> set[str]:
    return set(re.findall(r"^export (?:const|function|interface|type) (\w+)", COMPANY_TS.read_text(encoding="utf-8"), re.M))


def location_problems(files: dict[str, list[str]], counted: dict[str, int] | None = None) -> list[str]:
    """Sentences outside the copy homes, except the counted ones of files the redesign has not reached yet (`counted`, the
    module's NOT_YET_IN_THE_DECK unless a test passes its own)."""
    counted = NOT_YET_IN_THE_DECK if counted is None else counted
    problems = []
    for rel, fragments in files.items():
        if rel in COPY_HOMES or not fragments or (rel == KIND_HOME and kind_home_exports() == KIND_EXPORTS):
            continue
        allowed = counted.get(rel)
        if allowed is None:
            problems.append(f"{rel}: {len(fragments)} sentence(s) outside the deck, e.g. {fragments[0]!r}")
        elif len(fragments) != allowed:
            problems.append(f"{rel}: {len(fragments)} sentence(s), the list says {allowed} (move new copy to the deck; "
                            "lower the count when copy moves out)")
    problems += [f"{rel}: holds no copy now; remove it from NOT_YET_IN_THE_DECK" for rel in counted
                 if rel in files and not files[rel]]
    return problems


def all_literals(source: str) -> list[str]:
    """Every string literal and template part, short words included, in order: the snapshot records each wording change."""
    code = _code_only(source)
    pieces = [(m.start(), _js(m.group(1))) for m in _DOUBLE.finditer(code)]
    for m in _TEMPLATE.finditer(code):
        pieces += [(m.start(), _js(part)) for part in _SUBST.split(m.group(1))]
    return [text for _, text in sorted(pieces, key=lambda piece: piece[0]) if text.strip()]


def deck_snapshot() -> dict[str, list[str]]:
    return {"web/src/copy/deck.ts": all_literals(DECK_TS.read_text(encoding="utf-8"))}


# ---------------------------------------------------------------- 1. the matcher


class TestTheMatcher:
    def test_a_sentence_in_a_literal_a_template_and_jsx_text_is_found(self):
        source = 'const a = "Ask about the chips.";\nconst b = `Free to use, up to ${n} live questions`;\nx = <p>Skip to content</p>;'
        assert sentence_fragments(source, jsx=True) == ["Ask about the chips.", "Free to use, up to ", " live questions",
                                                        "Skip to content"]

    @pytest.mark.parametrize("source", [
        'const a = "Not found.";',                        # two words and a period (the R1 review found these dropped)
        'const a = "Try again.";',
        'const a = "Filed Feb 25, 2026";',                # digits are copy too (dates, forms, fiscal years)
        'const a = "See 10-K FY2024 filings";',
        "const a = 'single quoted sentence here';",       # nothing enforces double quotes, so single quotes are scanned
        "x = <p>{name} is not available here</p>;",       # JSX text after an expression
        "x = <p>Coming soon.</p>;",
    ])
    def test_short_sentences_digits_single_quotes_and_text_after_an_expression_are_found(self, source):
        assert sentence_fragments(source, jsx=True) != [], source

    def test_an_apostrophe_inside_a_double_quoted_sentence_is_one_sentence(self):
        assert sentence_fragments('const a = "Nvidia\'s revenue rose";\nconst b = "It\'s here now";') == [
            "Nvidia's revenue rose", "It's here now"]

    def test_bare_tailwind_words_do_not_make_a_class_list_a_sentence(self):
        assert sentence_fragments('const a = "inline block sr-only";\nconst b = "flex items-center underline";') == []

    def test_class_lists_paths_comments_imports_and_single_words_are_not_sentences(self):
        source = ('// "not a sentence here"\nimport { x } from "@/lib/some path";\n/* "also not a sentence" */\n'
                  'const c = "mt-2 rounded-md px-3 py-2 text-sm";\nconst d = "hover:bg-muted focus-visible:ring-2";\n'
                  'const e = "ok";\nconst f = "/api/examples";')
        assert sentence_fragments(source, jsx=True) == []

    def test_the_legacy_matcher_finds_a_sentence_in_the_page_and_not_a_changed_one(self):
        hay = legacy_haystack("<script>const A = \"served from cache — no model call\";</script><p>Hello <b>world</b></p>")
        assert legacy_missing('const a = "served from cache — no model call";', hay) == []
        assert legacy_missing('const a = "served from cache - no model call";', hay) != []
        assert legacy_missing("const a = `Hello world ${x}`;", hay) == []

    def test_constants_are_read_with_their_escapes(self):
        assert constants('export const A = "a \\u2014 b";\nexport const N = 500;') == {"A": "a — b", "N": "500"}

    def test_a_new_sentence_in_a_component_is_a_finding_and_a_counted_file_must_match_its_count(self):
        assert location_problems({"components/New.tsx": ["Click here to begin."]}) != []
        assert location_problems({"copy/deck.ts": ["Click here to begin."]}) == []
        counted = {"pages/Example.tsx": 3}
        assert location_problems({"pages/Example.tsx": ["a", "b", "c"]}, counted) == []
        assert location_problems({"pages/Example.tsx": ["a", "b", "c", "d"]}, counted) != []


# ---------------------------------------------------------------- 2. where copy lives


def test_the_deck_exists_and_holds_the_sentences():
    assert len(file_fragments(DECK_TS)) >= 20


def test_every_sentence_lives_in_the_deck_the_facts_or_a_counted_file():
    files = {p.relative_to(WEB).as_posix(): file_fragments(p) for p in source_files()}
    assert location_problems(files) == []


def test_the_deck_is_ascii_with_unicode_escapes():
    assert DECK_TS.read_text(encoding="utf-8").isascii()


def test_the_not_yet_list_names_only_files_that_exist():
    assert [rel for rel in NOT_YET_IN_THE_DECK if not (WEB / rel).is_file()] == []


def test_the_not_yet_list_is_empty():
    # docs/specs/m5b-redesign.md R4: the copy sweep is done; every sentence the site shows lives in the deck, the facts file or the
    # KIND_* home. A new file with copy in it fails the location check above instead of joining this list.
    assert NOT_YET_IN_THE_DECK == {}


# ---------------------------------------------------------------- 3. the snapshot


def test_the_deck_matches_its_recorded_snapshot():
    current = deck_snapshot()
    if os.environ.get("UPDATE_COPY_DECK") == "1":
        SNAPSHOT.write_text(json.dumps(current, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert SNAPSHOT.is_file(), "no snapshot: run with UPDATE_COPY_DECK=1 and review the diff"
    assert json.loads(SNAPSHOT.read_text(encoding="utf-8")) == current, "the deck changed: rerun with UPDATE_COPY_DECK=1 and review the diff"


# ---------------------------------------------------------------- 4. fact invariants


def _all_text(path: Path) -> str:
    return _squash(" ".join(file_fragments(path)))


@pytest.mark.parametrize("path", [DECK_TS, METHOD_TS], ids=lambda p: p.name)
def test_the_rule_notice_names_the_federal_register_keyword_matching_and_not_statements(path):
    text = _all_text(path)  # each file on its own: the deck's newcomer wording and the facts file's technical one
    for phrase in ("Federal Register", "keyword match", "not statements"):
        assert phrase in text, phrase


def test_the_company_page_s_limit_sentences_are_the_owner_s_words():
    # docs/specs/m5b-redesign.md D-R3 and D-R4, docs/decisions/001-r3-company-copy.md (1): approved word for word
    text = _all_text(DECK_TS)
    for phrase in ("No export-control rule in our data was linked to", "by keyword matching. This is not a finding that none applies.",
                   "Summaries written by semigraph, not quotes", "Not in our data for these years.", "The latest we hold is"):
        assert phrase in text, phrase


def test_company_copy_exports_only_the_four_kind_labels():
    # R4: everything else on the Company page lives in the deck (deck.company.changes)
    assert kind_home_exports() == KIND_EXPORTS


def test_the_deck_imports_only_types_from_the_libs():
    # the deck stays a leaf at run time: a lib may take its words from the deck, never the other way round (no import cycle)
    imports = re.findall(r"^import (type )?[^;]*? from \"@/lib/[^\"]+\";", DECK_TS.read_text(encoding="utf-8"), re.M)
    assert imports and all(kind == "type " for kind in imports), imports


def test_the_measured_hand_check_counts_are_present():
    facts = _all_text(METHOD_TS)
    for count in ("4 of 4", "6 of 12"):
        assert count in facts, count


def test_no_change_label_says_removed_or_dropped():
    labels = {n: v for n, v in _CONST.findall(COMPANY_TS.read_text(encoding="utf-8")) if n.startswith("KIND_")}
    assert len(labels) == 4, labels
    assert [n for n, v in labels.items() if re.search(r"\b(removed|dropped)\b", _js(v[1:-1]), re.I)] == []


@pytest.mark.parametrize("word", ["verified", "unlimited", "AI-powered", "guarantee", "guaranteed", "accurate", "proves"])
def test_no_sentence_on_the_site_says(word):
    # The four lib files keep the legacy page's pinned text until R2 renders them through deck codes; their three uses of
    # "verified" are negations ("not verified", "cannot be verified"), checked against the legacy page below.
    said = [(p.relative_to(WEB).as_posix(), f) for p in source_files() if p not in LIB_FILES for f in file_fragments(p)
            if re.search(rf"\b{re.escape(word)}\b", f, re.I)]
    assert said == []


# ---------------------------------------------------------------- 5. the cost line


APPROVED_COST_LINE = "Free to use, up to 5 live questions every 10 minutes (20 a day)."


def test_the_default_limits_are_the_settings_defaults():
    source = ASK_STATUS_TS.read_text(encoding="utf-8")
    m = re.search(r"DEFAULT_LIMITS\s*:\s*Limits\s*=\s*\{\s*perWindow:\s*(\d+),\s*windowMinutes:\s*(\d+),\s*perDay:\s*(\d+)\s*\}", source)
    assert m, "DEFAULT_LIMITS not found in askStatus.ts"
    fields = Settings.model_fields
    assert int(m.group(1)) == fields["rate_limit_questions"].default
    assert int(m.group(2)) == fields["rate_limit_window_seconds"].default // 60
    assert int(m.group(3)) == fields["paid_per_ip_per_day"].default


def test_the_cost_line_with_the_defaults_is_the_approved_sentence():
    m = re.search(r"costLine:\s*\(\s*(\w+)\s*:\s*Limits\s*\)\s*:\s*string\s*=>([^;]*?a day\)\.`)", DECK_TS.read_text(encoding="utf-8"))
    assert m, "costLine (with its daily-cap form) not found in deck.ts"
    name = m.group(1)
    template = re.findall(r"`([^`]*)`", m.group(2))[-1]
    values = {"perWindow": "5", "windowMinutes": "10", "perDay": "20"}
    rendered = re.sub(r"\$\{" + name + r"\.(\w+)\}", lambda s: values[s.group(1)], template)
    assert rendered == APPROVED_COST_LINE


# ---------------------------------------------------------------- legacy parity, until each file folds into the deck


def legacy_haystack(page: str) -> str:
    """The legacy page as text: its script with the quotes unescaped, and its markup as the reader sees it."""
    scripts = re.findall(r"<script>(.*?)</script>", page, flags=re.S)
    markup = re.sub(r"<script>.*?</script>", "", page, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    attrs = "\n".join(html.unescape(m) for m in re.findall(r'(?:placeholder|title|aria-label|maxlength)="([^"]*)"', markup))
    return _squash("\n".join(s.replace('\\"', '"') for s in scripts) + "\n" + text + "\n" + attrs)


def constants(source: str) -> dict[str, str]:
    return {name: (literal if literal.isdigit() else _js(literal[1:-1])) for name, literal in _CONST.findall(source)}


@pytest.fixture(scope="module")
def haystack() -> str:
    return legacy_haystack(INDEX_HTML.read_text(encoding="utf-8"))


class TestTheLegacyAskWordsKeepTheirPins:
    """R4 retired pages/Ask.copy.ts. The legacy words still shown live in copy/deck.ts as top-level named exports (the error
    clauses, the footer, the server's two refusals) or in lib/askStatus.ts (the legacy status projection); the numbers and the
    footer's link are in pages/Ask.constants.ts. Each stays pinned against the legacy page, as before."""

    @pytest.fixture(scope="class")
    def copy(self) -> dict[str, str]:
        return {**constants(DECK_TS.read_text(encoding="utf-8")), **constants(ASK_STATUS_TS.read_text(encoding="utf-8")),
                **constants(ASK_CONSTANTS_TS.read_text(encoding="utf-8"))}

    def test_the_file_was_read(self, copy):
        assert not ASK_COPY_TS_RETIRED.exists()
        for name in ("CLAUSE_TOKEN_EXPIRED", "CLAUSE_NO_TOKEN", "STATUS_NETWORK", "FOOTER_LEAD", "FOOTER_AFTER_COST",
                     "FOOTER_LINK_TEXT", "FOOTER_END", "MSG_NOT_PUBLIC_EVIDENCE", "MSG_MALFORMED_EVIDENCE", "STATUS_RETRIEVING",
                     "STATUS_AGENT", "STATUS_GENERATING", "STATUS_CACHED", "STATUS_DONE", "NO_COMPANY_ANCHOR", "COST_NOTE_DEFAULT",
                     "MAX_QUESTION_CHARS", "FORBIDDEN_STATUS", "FOOTER_LINK_HREF"):
            assert name in copy, name

    def test_every_constant_is_found_in_the_legacy_page(self, copy, haystack):
        by_server = {"MSG_NOT_PUBLIC_EVIDENCE", "MSG_MALFORMED_EVIDENCE", "FOOTER_LINK_HREF"}
        numbers = {name for name, value in copy.items() if value.isdigit()}
        missing = [f"{n}: {v!r}" for n, v in copy.items() if n not in by_server | numbers and _squash(v.strip()) not in haystack]
        assert missing == []

    def test_the_footer_reads_as_one_sentence_of_the_legacy_footer(self, copy, haystack):
        footer = copy["FOOTER_LEAD"] + copy["COST_NOTE_DEFAULT"] + copy["FOOTER_AFTER_COST"] + copy["FOOTER_LINK_TEXT"] + copy["FOOTER_END"]
        assert _squash(footer) in haystack

    def test_the_footer_link_is_the_legacy_href(self, copy):
        assert f'href="{copy["FOOTER_LINK_HREF"]}"' in INDEX_HTML.read_text(encoding="utf-8")

    def test_the_numbers_are_the_legacy_ones(self, copy):
        page = INDEX_HTML.read_text(encoding="utf-8")
        assert f'maxlength="{copy["MAX_QUESTION_CHARS"]}"' in page
        assert int(copy["FORBIDDEN_STATUS"]) == 403 and "resp.status === 403" in page

    def test_the_two_refusals_the_drawer_words_are_the_servers_own(self, copy):
        routes = ROUTES_PY.read_text(encoding="utf-8")
        assert f'MSG_NOT_PUBLIC_EVIDENCE = "{copy["MSG_NOT_PUBLIC_EVIDENCE"]}"' in routes
        assert f'detail="{copy["MSG_MALFORMED_EVIDENCE"]}"' in routes


def legacy_fragments(source: str) -> list[str]:
    """The old test's loose filter: every double-quoted or template piece of 6 or more characters with a space."""
    code = _code_only(source)
    pieces = [_js(m.group(1)) for m in _DOUBLE.finditer(code)]
    for m in _TEMPLATE.finditer(code):
        pieces.extend(_js(part) for part in _SUBST.split(m.group(1)))
    return [p for p in pieces if len(p.strip()) >= _MIN_SENTENCE and " " in p.strip()]


def legacy_missing(source: str, haystack: str) -> list[str]:
    return [p for p in legacy_fragments(source) if _squash(p.strip()) not in haystack]


@pytest.mark.parametrize("path", LIB_FILES, ids=lambda p: p.name)
def test_every_legacy_text_of_the_lib_files_is_a_sentence_of_the_legacy_page(path, haystack):
    source = path.read_text(encoding="utf-8")
    assert legacy_fragments(source), f"{path.name}: no sentence found (the scan is vacuous)"
    assert legacy_missing(source, haystack) == []
