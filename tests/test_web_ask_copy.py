"""The Ask page copy of the web app is the legacy page's copy, verbatim (docs/specs/m5b-frontend.md R22, R30, S15).

``web/src/pages/Ask.copy.ts`` holds the strings the Ask page and the evidence drawer show; the pure functions of the port
(``web/src/lib/answerMeta.ts``, ``steps.ts``, ``evidenceView.ts``, ``askStatus.ts``) build the rest of the sentences. The web
port is a port, not a rewrite, so every sentence must equal the legacy text of ``src/semigraph/serve/static/index.html``
character for character, and the web port adds no public sentence of its own. vitest cannot do this check (it runs from a
mirror outside the Python tree), so it lives here. Strings are found by their content, never by a line number.

Three legs: the matcher is exercised on a changed string (a matcher that stopped comparing would turn red), the constants of
``Ask.copy.ts`` are matched to the places they come from, and every sentence-like literal of the four lib files is found in
the legacy page.
"""

import html
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "src"
COPY_TS = WEB / "pages" / "Ask.copy.ts"
LIB_FILES = [WEB / "lib" / name for name in ("answerMeta.ts", "steps.ts", "evidenceView.ts", "askStatus.ts")]
INDEX_HTML = ROOT / "src" / "semigraph" / "serve" / "static" / "index.html"
ROUTES_PY = ROOT / "src" / "semigraph" / "serve" / "routes.py"

_CONST = re.compile(r"export const (\w+) =\s*((?:\"(?:[^\"\\]|\\.)*\")|\d+);", re.S)
_DOUBLE = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
_TEMPLATE = re.compile(r"`((?:[^`\\]|\\.)*)`")
_SUBST = re.compile(r"\$\{[^}]*\}")
_MIN_SENTENCE = 6  # characters of a fragment worth pinning (shorter ones are punctuation and single words)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _js(literal: str) -> str:
    """The text a double-quoted JS literal body denotes (escapes of quotes, backslashes and unicode)."""
    return json.loads('"' + re.sub(r'(?<!\\)"', r'\\"', literal) + '"')


def constants(source: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, literal in _CONST.findall(source):
        found[name] = literal if literal.isdigit() else _js(literal[1:-1])
    return found


def legacy_haystack(page: str) -> str:
    """The legacy page as text: its script with the quotes unescaped, and its markup as the reader sees it (tags dropped, entities decoded)."""
    scripts = re.findall(r"<script>(.*?)</script>", page, flags=re.S)
    markup = re.sub(r"<script>.*?</script>", "", page, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    attrs = "\n".join(html.unescape(m) for m in re.findall(r'(?:placeholder|title|aria-label|maxlength)="([^"]*)"', markup))
    return _squash("\n".join(s.replace('\\"', '"') for s in scripts) + "\n" + text + "\n" + attrs)


def sentence_fragments(source: str) -> list[str]:
    """The sentence-like pieces of the string literals of a TypeScript file (comments and imports left out)."""
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith(("//", "import ")))
    code = re.sub(r"(?<=[;,{}) ])\s//[^\n]*", "", code)
    pieces: list[str] = []
    for literal in _DOUBLE.findall(code):
        pieces.append(_js(literal))
    for literal in _TEMPLATE.findall(code):
        pieces.extend(_js(part) for part in _SUBST.split(literal))
    return [p for p in pieces if len(p.strip()) >= _MIN_SENTENCE and " " in p.strip()]


def missing_fragments(source: str, haystack: str) -> list[str]:
    return [p for p in sentence_fragments(source) if _squash(p.strip()) not in haystack]


@pytest.fixture(scope="module")
def haystack() -> str:
    return legacy_haystack(INDEX_HTML.read_text(encoding="utf-8"))


class TestTheMatcher:
    def test_a_sentence_that_is_in_the_page_is_found_and_a_changed_one_is_not(self):
        page = "<script>const A = \"served from cache \u2014 no model call\";</script><p>Hello <b>world</b></p>"
        hay = legacy_haystack(page)
        assert missing_fragments('const a = "served from cache \u2014 no model call";', hay) == []
        assert missing_fragments('const a = "served from cache - no model call";', hay) != []
        assert missing_fragments("const a = `Hello world ${x}`;", hay) == []

    def test_comments_imports_and_short_words_are_not_pinned(self):
        source = '// "not a sentence here"\nimport { x } from "@/lib/some path";\nconst a = "ok";\n/* "also not a sentence" */'
        assert sentence_fragments(source) == []

    def test_constants_are_read_with_their_escapes(self):
        assert constants('export const A = "a \\u2014 b";\nexport const N = 500;') == {"A": "a \u2014 b", "N": "500"}


class TestAskCopy:
    @pytest.fixture(scope="class")
    def copy(self) -> dict[str, str]:
        return constants(COPY_TS.read_text(encoding="utf-8"))

    def test_the_file_was_read(self, copy):
        assert len(copy) >= 40

    def test_every_constant_is_found_in_the_legacy_page(self, copy, haystack):
        # the three strings of the server are checked against routes.py below; the rest are legacy text, a number or a status code
        by_server = {"MSG_NOT_PUBLIC_EVIDENCE", "MSG_MALFORMED_EVIDENCE", "FOOTER_LINK_HREF"}  # the href is checked below
        numbers = {name for name, value in copy.items() if value.isdigit()}
        missing = [
            f"{name}: {value!r}"
            for name, value in copy.items()
            if name not in by_server | numbers and _squash(value.strip()) not in haystack
        ]
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


@pytest.mark.parametrize("path", LIB_FILES, ids=lambda p: p.name)
def test_every_sentence_of_the_port_is_a_sentence_of_the_legacy_page(path, haystack):
    assert sentence_fragments(path.read_text(encoding="utf-8")), f"{path.name}: no sentence found (the scan is vacuous)"
    assert missing_fragments(path.read_text(encoding="utf-8"), haystack) == []
