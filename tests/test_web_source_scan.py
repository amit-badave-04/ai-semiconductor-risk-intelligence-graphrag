"""The authored web source against the HTML-sink and copy rules (docs/specs/m5b-frontend.md S1, S15; I1 part 3).

S1: ``web/src/**`` has no HTML sink and no code-from-string path, so nothing a model, a document or the server sends can
become markup or script. The copied-in ``components/ui/**`` is scanned too, with no exclusion; a component that holds a sink is
not installed (``chart`` is the one of shadcn's Base UI set that does). ``package.json`` carries no markdown or HTML renderer.
The built bundle is NOT grepped: react-dom contains some of these identifiers itself.

S15: public copy states strengths only, so a banned-phrase list (the 13 withdrawn claims plus the user-count claims) finds
nothing in ``web/src``.

Two legs for each: the scanner is exercised on small bad trees (a scanner that stopped looking would turn red), then it runs on
the real tree, which must not be empty.
"""

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
SRC = WEB / "src"
SCANNED = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".html", ".json", ".svg"}

# Identifiers that are a sink or a code-from-string path wherever they appear in authored source.
FORBIDDEN = {
    "dangerouslySetInnerHTML": r"dangerouslySetInnerHTML",
    "innerHTML": r"\binnerHTML\b",
    "outerHTML": r"\bouterHTML\b",
    "insertAdjacentHTML": r"\binsertAdjacentHTML\b",
    "document.write": r"\bdocument\s*\.\s*write(?:ln)?\s*\(",
    "eval": r"(?<![\w.$])eval\s*\(",
    "new Function": r"\bnew\s+Function\b",
    "Function(": r"(?<![\w.$])Function\s*\(",
    "string setTimeout": r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]",
    "srcdoc": r"\bsrcdoc\b",
    "createContextualFragment": r"\bcreateContextualFragment\b",
    "DOMParser": r"\bDOMParser\b",
}
# Navigation and setAttribute forms: allowed only with a string-literal argument.
LITERAL_ONLY_CALLS = {
    "window.open": r"\bwindow\s*\.\s*open\s*\(",
    "location.assign": r"\blocation\s*\.\s*assign\s*\(",
    "location.replace": r"\blocation\s*\.\s*replace\s*\(",
}
LITERAL = r"""\s*(?:"[^"\\\n]*"|'[^'\\\n]*'|`[^`$\\\n]*`)\s*[,)]"""
LOCATION_HREF_ASSIGN = re.compile(r"\blocation\s*\.\s*href\s*=(?!=)")
SET_ATTRIBUTE = re.compile(r"\bsetAttribute(?:NS)?\s*\(")
JAVASCRIPT_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
DEFAULT_SUFFIX = ".ts"
# After one of these words a `/` starts a regular expression; after any other word, a number, a string or a closing bracket it divides.
REGEX_AFTER_WORD = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else", "yield", "await"}
DIVISION_AFTER_PUNCTUATION = {")", "]", "}"}
IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
NUMBER = re.compile(r"\d[\w.]*")

DENY_PACKAGES = re.compile(
    r"^(?:react-markdown|marked|markdown-it|dompurify|isomorphic-dompurify|sanitize-html|streamdown|showdown|snarkdown|micromark"
    r"|rehype-.*|remark-.*|@types/dompurify|@types/marked|@types/markdown-it)$")
# Not in the S1 list but forbidden by the spec elsewhere: the AI SDK (decision 10), a Turnstile wrapper and a fetch-event-source
# that auto-retries (R1, R17), react-router-dom (gone in v8), web fonts (Scope out).
DENY_OTHER = re.compile(r"^(?:ai|@ai-sdk/.*|@microsoft/fetch-event-source|eventsource-client|@marsidev/react-turnstile"
                        r"|react-router-dom|@fontsource.*|@fontsource-variable/.*)$")

# The 13 withdrawn claims of tests/test_static_ui.py::test_page_never_carries_the_withdrawn_claims, plus the user-count claims.
BANNED_PHRASES = (
    "draft verified", "all citations verified", "95% correct", "75% correct", "100% correct", "one Claude call",
    "dropped risk lineages", "deleted_risk_lineages", "text-verified removed", "text verified absent", "verified absent",
    "removed lists do not support", "stop disclosing in its latest", "1,000 users", "1000 concurrent",
)


def source_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SCANNED and "node_modules" not in p.parts)


VALUE = object()          # the last token was a string, a number, a regex or a whole template: a `/` after it divides


def scan_template(text: str, i: int) -> tuple[int, bool]:
    """From just after a backtick, or just after the `}` that closes a template expression: the index after the closing
    backtick (False), or after the next `${` (True: code follows, up to its matching `}`)."""
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
        elif text[i] == "`":
            return i + 1, False
        elif text[i] == "$" and text[i + 1:i + 2] == "{":
            return i + 2, True
        else:
            i += 1
    return n, False


def scan_regex(text: str, i: int) -> int | None:
    """The index after a regular-expression literal (flags included) that starts at ``text[i] == '/'``, or None when the line
    holds no closing slash: then the slash was a division and the caller treats it as one."""
    n, in_class, j = len(text), False, i + 1
    while j < n and text[j] != "\n":
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < n and text[j].isalpha():
                j += 1
            return j
        j += 1
    return None


def regex_may_start(last: object) -> bool:
    """Whether a `/` after the last significant token begins a regular expression (else it divides)."""
    if last is None:
        return True
    if last is VALUE:
        return False
    assert isinstance(last, str)
    if last in REGEX_AFTER_WORD:
        return True
    if last[0].isalpha() or last[0] in "_$":
        return False                                                       # any other identifier
    return last not in DIVISION_AFTER_PUNCTUATION


def strip_js_comments(text: str) -> str:
    """``text`` with every real comment replaced by one space (the newline after a line comment is kept). Strings, template
    literals (with their ``${...}`` code) and regular-expression literals are skipped over, so a ``//`` or a ``/*`` inside one
    never opens a comment. A string or a regex ends at the end of its line, so a stray apostrophe in JSX text cannot swallow the
    file. The only mistake that matters is reading CODE as a comment: every other one leaves a comment's own text in the output,
    where the scan reports it (a false positive is fine, a blind spot is not). So a ``/*`` with no ``*/`` after it is not a
    comment, and neither is a ``//`` right after a colon (a URL in JSX text)."""
    out: list[str] = []
    n, i = len(text), 0
    expressions: list[int] = []          # one entry per open `${`: how many `{` are open inside it
    last: object = None                  # the last significant token: VALUE, an identifier, or one punctuation character
    while i < n:
        ch, pair = text[i], text[i:i + 2]
        if ch in " \t\r\n":
            out.append(ch)
            i += 1
        elif pair == "/*" and (close := text.find("*/", i + 2)) >= 0:
            out.append(" ")
            i = close + 2
        elif pair == "//" and not (i > 0 and text[i - 1] == ":"):
            out.append(" ")
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif ch in "'\"":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j, n)
            j += 1 if j < n and text[j] == ch else 0
            out.append(text[i:j])
            i, last = j, VALUE
        elif ch == "`" or (ch == "}" and expressions and expressions[-1] == 0):
            if ch == "}":
                expressions.pop()
            j, opened = scan_template(text, i + 1)
            out.append(text[i:j])
            if opened:
                expressions.append(0)
            i, last = j, (None if opened else VALUE)
        elif ch == "/" and regex_may_start(last) and (end := scan_regex(text, i)) is not None:
            out.append(text[i:end])
            i, last = end, VALUE
        elif word := IDENTIFIER.match(text, i):
            out.append(word.group())
            i, last = word.end(), word.group()
        elif number := NUMBER.match(text, i):
            out.append(number.group())
            i, last = number.end(), VALUE
        else:
            if expressions and ch == "{":
                expressions[-1] += 1
            elif expressions and ch == "}":
                expressions[-1] -= 1
            out.append(ch)
            i, last = i + 1, ch
    return "".join(out)


def strip_comments(text: str, suffix: str = DEFAULT_SUFFIX) -> str:
    """Comments are not code, but only a JavaScript file has JavaScript comments: ``.css``, ``.html``, ``.json`` and ``.svg``
    are scanned raw (``url(//host/x)`` is not a line comment, and a sink hidden after it would be a blind spot)."""
    return strip_js_comments(text) if suffix.lower() in JAVASCRIPT_SUFFIXES else text


def scan_sinks(text: str, suffix: str = DEFAULT_SUFFIX) -> list[str]:
    """The sink findings of one file's text (comments are not code, so a JavaScript file's are removed first)."""
    code = strip_comments(text, suffix)
    found = [f"{label}" for label, pattern in FORBIDDEN.items() if re.search(pattern, code)]
    for label, pattern in LITERAL_ONLY_CALLS.items():
        for match in re.finditer(pattern, code):
            if not re.match(LITERAL, code[match.end():]):
                found.append(f"{label}(non-literal)")
    if LOCATION_HREF_ASSIGN.search(code):
        for match in LOCATION_HREF_ASSIGN.finditer(code):
            if not re.match(r"\s*(?:\"[^\"\\\n]*\"|'[^'\\\n]*'|`[^`$\\\n]*`)\s*(?:[;,)\n]|$)", code[match.end():]):
                found.append("location.href=(non-literal)")
    for match in SET_ATTRIBUTE.finditer(code):
        rest = code[match.end():]
        name = re.match(r"""\s*(?:"([^"\\\n]*)"|'([^'\\\n]*)'|`([^`$\\\n]*)`)\s*,""", rest)
        if not name:
            found.append("setAttribute(non-literal name)")
            continue
        attribute = (name.group(1) or name.group(2) or name.group(3) or "").lower()
        if attribute.startswith("on"):
            found.append(f"setAttribute({attribute})")
        elif attribute in ("href", "src", "srcset", "action", "formaction", "xlink:href", "style"):
            if not re.match(r"""\s*(?:"[^"\\\n]*"|'[^'\\\n]*'|`[^`$\\\n]*`)\s*\)""", rest[name.end():]):
                found.append(f"setAttribute({attribute}, non-literal)")
    return found


def scan_tree(root: Path) -> dict[str, list[str]]:
    out = {}
    for path in source_files(root):
        findings = scan_sinks(path.read_text(encoding="utf-8", errors="replace"), path.suffix)
        if findings:
            out[path.relative_to(root).as_posix()] = findings
    return out


def scan_copy(root: Path) -> dict[str, list[str]]:
    out = {}
    for path in source_files(root):
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        hits = [phrase for phrase in BANNED_PHRASES if phrase.lower() in text]
        if hits:
            out[path.relative_to(root).as_posix()] = hits
    return out


# ---------------------------------------------------------------- the scanner is not vacuous

@pytest.mark.parametrize("label, code", [
    ("dangerouslySetInnerHTML", "<div dangerouslySetInnerHTML={{ __html: x }} />"),
    ("innerHTML", "el.innerHTML = x"),
    ("outerHTML", "el.outerHTML = x"),
    ("insertAdjacentHTML", "el.insertAdjacentHTML('beforeend', x)"),
    ("document.write", "document.write(x)"),
    ("document.write", "document . writeln (x)"),
    ("eval", "eval(x)"),
    ("new Function", "const f = new Function('return 1')"),
    ("Function(", "const f = Function('return 1')"),
    ("string setTimeout", "setTimeout('alert(1)', 10)"),
    ("string setTimeout", "setInterval(`alert(1)`, 10)"),
    ("srcdoc", "<iframe srcdoc={x} />"),
    ("createContextualFragment", "range.createContextualFragment(x)"),
    ("DOMParser", "new DOMParser().parseFromString(x, 'text/html')"),
    ("window.open(non-literal)", "window.open(url)"),
    ("location.assign(non-literal)", "location.assign(next)"),
    ("location.replace(non-literal)", "window.location.replace(next + '/')"),
    ("location.href=(non-literal)", "location.href = next"),
    ("location.href=(non-literal)", "window.location.href = base + path"),
    ("setAttribute(non-literal name)", "el.setAttribute(name, 'x')"),
    ("setAttribute(onclick)", "el.setAttribute('onclick', 'x()')"),
    ("setAttribute(href, non-literal)", "el.setAttribute('href', url)"),
    ("setAttribute(src, non-literal)", "el.setAttribute(\"src\", `${base}/x`)"),
])
def test_the_sink_scanner_catches_each_forbidden_form(label, code):
    assert label in scan_sinks(code), scan_sinks(code)


@pytest.mark.parametrize("code", [
    "window.open('https://example.com/')",
    "location.assign(\"/v2/\")",
    "location.href = '/v2/'",
    "if (location.href === x) {}",
    "el.setAttribute('aria-live', 'polite')",
    "el.setAttribute('href', '/v2/')",
    "const label = 'the eval harness'; // eval(x) in a comment is not code",
    "/* innerHTML = x */ const a = 1",
    "const evaluate = (x) => x; retrieval(x); medieval(x)",
    "const text = `https://example.com/${path}`",
])
def test_the_sink_scanner_leaves_safe_code_alone(code):
    assert scan_sinks(code) == [], scan_sinks(code)


# I1 review finding: the old comment stripper was two regular expressions, so a `//` or a `/*` inside a string opened a comment
# that was not one and hid every sink up to the end of the line (or up to the next `*/`). oxlint has no rule for these sinks,
# so this scan is S1's only automated guard for them. Each row is a real bypass, or the lexical shape that could become one.
BYPASSES = [
    ("a // inside a double-quoted string", 'const u = "a//b"; el.innerHTML = u;'),
    ("a // inside a single-quoted string", "const u = 'a//b'; el.innerHTML = u;"),
    ("a /* inside a string, a later doc comment",
     'const pages = import.meta.glob("./pages/*.tsx");\nfunction f() { el.innerHTML = x; }\n/** docs */'),
    ("a /* inside a string, a later line comment", "const g = '/*'; el.innerHTML = x; // */"),
    ("a comment opener built with no gap", "const f = new/**/Function('x')"),
    ("a regex literal with //", r"const re = /a\/\/b/; el.innerHTML = x;"),
    ("a regex literal with an escaped comment opener", r'x.replace(/\/\*/g, ""); el.innerHTML = x; /* c */'),
    ("a regex literal with a quote", "const re = /\"/; el.innerHTML = x; // \""),
    ("a regex literal with an apostrophe", "const re = /'/; el.innerHTML = x; // '"),
    ("a regex literal after return", "return /\"/.test(s); el.innerHTML = x; /* \" */"),
    ("a slash inside a regex class", "const re = /[/*]/; el.innerHTML = x; /* c */"),
    ("a template literal with // and /*", "const t = `//${a}/*`; el.innerHTML = t; /* c */"),
    ("a nested template literal", "const t = `a ${ b ? `//` : '/*' } c`; el.innerHTML = t; /* c */"),
    ("a template expression with braces", "const t = `${ {a: '//'}.a } /*`; el.innerHTML = t; /* c */"),
    ("an escaped quote in a string", "const s = 'it\\'s //'; el.innerHTML = s;"),
    ("an apostrophe in JSX text", "<p>Don't //</p>;\nel.innerHTML = x; /* c */"),
    ("quotes inside comments", "/* it's */ el.innerHTML = x; // don't"),
    ("a division, not a regex", "const r = a / b; el.innerHTML = x; // c"),
    ("a URL in JSX text", "<a>https://example.com</a>; el.innerHTML = x;"),
]
COMMENTED = [
    ("a // inside a string, then a real comment", 'const u = "a//b"; // el.innerHTML = u;'),
    ("a /* inside a string, then a real block comment", "const g = '/*'; /* el.innerHTML = x; */"),
    ("a regex with //, then a real comment", r"const re = /a\/\/b/; // el.innerHTML = x"),
    ("a template with //, then a real comment", "const t = `//`; /* el.innerHTML = t */"),
    ("a multi-line block comment", "/* line one\n el.innerHTML = x;\n line three */ const a = 1;"),
    ("a JSX comment", "<p>{/* el.innerHTML = x */}</p>"),
]


@pytest.mark.parametrize("what, code", BYPASSES, ids=[w for w, _ in BYPASSES])
def test_the_sink_scanner_cannot_be_blinded_by_a_string_a_regex_or_a_template(what, code):
    assert "innerHTML" in scan_sinks(code) or "new Function" in scan_sinks(code), (what, scan_sinks(code))


@pytest.mark.parametrize("what, code", COMMENTED, ids=[w for w, _ in COMMENTED])
def test_a_sink_inside_a_real_comment_is_still_not_code(what, code):
    assert scan_sinks(code) == [], (what, scan_sinks(code))


TRICKY_PREFIXES = ['"a//b"', "'a//b'", '"/*"', "'/*'", "`//${a}/*`", "`a ${ `//` } /*`", r"/a\/\/b/", r"/\/\*/g", "/[/*]/", "/'/", '/"/',
                   "// it's a \"comment\"\n", "/* it's a \"comment\" */", "<p>Don't</p>", "<a>https://x.test</a>"]
TRAILERS = ["", "\n/** docs */", "\n// end", "\n/* a */ const b = 1; /* b */"]


@pytest.mark.parametrize("prefix", TRICKY_PREFIXES)
@pytest.mark.parametrize("trailer", TRAILERS)
def test_a_sink_after_any_tricky_lexical_shape_is_found_and_one_inside_a_following_comment_is_not(prefix, trailer):
    assert "innerHTML" in scan_sinks(f"const v = {prefix};\nel.innerHTML = x;{trailer}"), (prefix, trailer)
    assert scan_sinks(f"const v = {prefix};\n// el.innerHTML = x;{trailer}") == [], (prefix, trailer)
    assert scan_sinks(f"const v = {prefix};\n/* el.innerHTML = x; */{trailer}") == [], (prefix, trailer)


def test_an_unterminated_block_comment_hides_nothing():
    """`/*` with no `*/` after it is a syntax error that `tsc -b` rejects, but the scanner must not read it as a comment that
    runs to the end of the file: the rest stays code."""
    assert "innerHTML" in scan_sinks("const a = 1; /* never closed\nel.innerHTML = x;")


def test_a_comment_is_replaced_by_a_space_not_by_nothing():
    assert strip_comments("a/**/b // c\nd") == "a b  \nd"
    assert "new Function" in scan_sinks("new/* gap */Function('x')")


def test_non_javascript_files_are_scanned_raw():
    """`.css`, `.html`, `.json` and `.svg` have no JavaScript comment syntax: `url(//host/x)` is not a line comment, and a
    sink after it must still be found. (A sink word inside such a file's own comment is therefore reported: conservative.)"""
    assert "srcdoc" in scan_sinks('a{background:url(//h/x)} <iframe srcdoc="x">', ".css")
    assert "srcdoc" in scan_sinks('<!-- x --><iframe srcdoc="x">', ".html")
    assert scan_sinks("a{color:red} /* innerHTML */", ".css") == ["innerHTML"]
    assert scan_sinks("// innerHTML", ".tsx") == []
    for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        assert scan_sinks("// innerHTML\n", suffix) == [], suffix


def test_the_tree_scan_applies_the_javascript_comment_rules_by_file_type(tmp_path):
    (tmp_path / "blind.tsx").write_text('const u = "a//b"; el.innerHTML = u;\n', encoding="utf-8")
    (tmp_path / "ok.tsx").write_text("// el.innerHTML = u;\n", encoding="utf-8")
    (tmp_path / "raw.css").write_text("a{background:url(//h/x)}\n.b::after{content:'srcdoc'}\n", encoding="utf-8")
    assert scan_tree(tmp_path) == {"blind.tsx": ["innerHTML"], "raw.css": ["srcdoc"]}


def test_the_tree_scan_walks_every_authored_file_type(tmp_path):
    for name, body in {"a.ts": "el.innerHTML = x", "b/c.tsx": "eval(x)", "d.mjs": "document.write(x)",
                       "e.html": "<iframe srcdoc=x>", "components/ui/f.tsx": "new Function('')"}.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(body, encoding="utf-8")
    assert set(scan_tree(tmp_path)) == {"a.ts", "b/c.tsx", "d.mjs", "e.html", "components/ui/f.tsx"}


def test_the_copy_scanner_finds_each_banned_phrase_case_insensitively(tmp_path):
    for n, phrase in enumerate(BANNED_PHRASES):
        (tmp_path / f"{n}.tsx").write_text(f"<p>{phrase.upper()}</p>", encoding="utf-8")
    assert len(scan_copy(tmp_path)) == len(BANNED_PHRASES)


def test_the_banned_phrases_include_the_thirteen_withdrawn_claims():
    tree = ast.parse((ROOT / "tests" / "test_static_ui.py").read_text(encoding="utf-8"))
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "test_page_never_carries_the_withdrawn_claims")
    withdrawn = ast.literal_eval(function.decorator_list[0].args[1])
    assert len(withdrawn) == 13, withdrawn
    assert set(withdrawn) <= set(BANNED_PHRASES)


# ---------------------------------------------------------------- the real tree

def test_the_web_source_tree_is_not_empty():
    names = {p.relative_to(SRC).as_posix() for p in source_files(SRC)}
    assert "main.tsx" in names and "lib/ids.generated.ts" in names, sorted(names)
    assert any(n.startswith("components/ui/") for n in names), "the copied-in components must be scanned too (no exclusion)"
    assert any(n.endswith(".css") for n in names)


def test_s1_the_authored_web_source_has_no_html_sink():
    assert scan_tree(SRC) == {}


def test_s1_the_copied_in_components_are_in_the_scan_and_chart_is_not_installed():
    ui = SRC / "components" / "ui"
    assert ui.is_dir() and list(ui.glob("*.tsx"))
    assert not (ui / "chart.tsx").exists()
    assert "chart" not in {p.stem for p in ui.glob("*")}


def test_s1_package_json_carries_no_markdown_or_html_renderer():
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    names = {*package.get("dependencies", {}), *package.get("devDependencies", {})}
    assert names
    assert sorted(n for n in names if DENY_PACKAGES.match(n)) == []


def test_package_json_carries_none_of_the_other_packages_the_spec_rules_out():
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    names = {*package.get("dependencies", {}), *package.get("devDependencies", {})}
    assert sorted(n for n in names if DENY_OTHER.match(n)) == []


@pytest.mark.parametrize("name, banned", [("react-markdown", True), ("marked", True), ("markdown-it", True), ("dompurify", True),
                                          ("streamdown", True), ("rehype-raw", True), ("remark-gfm", True), ("react", False),
                                          ("markedly", False), ("eventsource-parser", False)])
def test_the_package_deny_list_matches_exactly(name, banned):
    assert bool(DENY_PACKAGES.match(name)) is banned


def test_s15_public_copy_in_web_src_states_no_withdrawn_claim():
    assert scan_copy(SRC) == {}


# RD1 (docs/specs/m5b-redesign.md): one type scale, 13/15/17/21/28/44 px, as the --text-* tokens in web/src/index.css. An
# arbitrary Tailwind font size (text-[0.9em], text-[11px], text-[length:...]) steps off it; sizes go through the tokens.
ARBITRARY_FONT_SIZE = re.compile(r"\btext-\[(?:length:)?[0-9.]+(?:em|rem|px|%|vw|ch)\]")


def off_scale_font_sizes(root: Path) -> list[str]:
    found = []
    for path in source_files(root):
        if path.suffix not in {".ts", ".tsx"} or "__tests__" in path.parts or ".test." in path.name:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ARBITRARY_FONT_SIZE.search(line):
                found.append(f"{path.relative_to(root).as_posix()}:{number}")
    return found


def test_rd1_font_sizes_stay_on_the_type_scale():
    assert off_scale_font_sizes(SRC) == []


@pytest.mark.parametrize("cls, off", [("text-[0.9em]", True), ("text-[11px]", True), ("text-[length:1.2rem]", True),
                                      ("text-sm", False), ("text-[var(--x)]", False), ("size-[1em]", False)])
def test_the_type_scale_check_matches_exactly(cls, off):
    assert bool(ARBITRARY_FONT_SIZE.search(f'className="{cls}"')) is off
