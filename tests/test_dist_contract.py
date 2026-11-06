"""web/dist against the CSP the server sends, unchanged (docs/specs/m5b-frontend.md R4, R12, R30(1); I1 part 3).

The server's CSP (``routes.CSP``) has ``'unsafe-inline'`` for scripts and styles, no ``font-src`` (so a ``data:`` font is
blocked) and ``base-uri 'none'`` (so a ``<base>`` is refused), and the same built page is served at ``/v2/`` and, flipped, at
``/``. So the build output must have: no inline script or style, no ``style=``, no ``<base>``, no robots meta, no ``data:``
font and no ``@font-face``, no absolute URL in the page (``https://challenges.cloudflare.com`` aside) or in the CSS, and, because
nothing moves (R30), no ``transition``, ``animation`` or ``@keyframes`` declaration in any CSS file.

The JavaScript bundles are NOT scanned for URLs: react-dom itself contains ``http://www.w3.org/2000/svg`` and
``https://react.dev/errors/``. "No other origin" is enforced at runtime by the Playwright origin check (S5).

Two legs. The checker is exercised on small bad dists, so a check that stopped looking would turn red here; and the real
``web/dist`` (or ``$WEB_DIST``) is checked when it exists. With no ``web/dist`` the real leg SKIPS and says so (the CI ``web``
job runs it after the build and fails on any skip).
"""

import os
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIST = Path(os.environ.get("WEB_DIST") or ROOT / "web" / "dist")
BASE = "/v2/"
ALLOWED_ORIGIN = "https://challenges.cloudflare.com"
ABSOLUTE_URL = re.compile(r"""(?:https?:)?//[A-Za-z0-9][^\s"'<>)]*""", re.I)
CSS_ABSOLUTE_URL = re.compile(r"""url\(\s*["']?\s*(?:https?:|//)""", re.I)
DATA_FONT = re.compile(r"data:\s*(?:font/|application/(?:x-)?font|application/vnd\.ms-fontobject|application/octet-stream)", re.I)
# A declaration, not a substring: ``(?<![\w-])`` keeps a custom property such as ``--tw-transition`` from tripping the scan.
MOTION = re.compile(r"(?<![\w-])(?:transition|animation)(?:-[a-z-]+)?\s*:|@(?:-webkit-)?keyframes\b", re.I)
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self.references: list[str] = []
        self.has_title = self.has_viewport = False
        self.lang = ""

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = attrs.get("lang", "")
        if tag == "script":
            if "src" in attrs:
                self.references.append(attrs["src"])
            else:
                self.problems.append("inline <script>")
        if tag == "style":
            self.problems.append("<style> element")
        if tag == "base":
            self.problems.append("<base> element (base-uri 'none' refuses it)")
        if tag == "meta" and attrs.get("name", "").lower() == "robots":
            self.problems.append('<meta name="robots"> (the same page is served at / once promoted)')
        if tag == "meta" and attrs.get("name", "").lower() == "viewport":
            self.has_viewport = True
        if tag == "title":
            self.has_title = True
        if "style" in attrs:
            self.problems.append(f"style= attribute on <{tag}>")
        if tag == "link" and attrs.get("href"):
            self.references.append(attrs["href"])
        if tag in ("img", "source", "iframe") and attrs.get("src"):
            self.references.append(attrs["src"])
        if tag == "a" and attrs.get("href"):
            self.references.append(attrs["href"])


def check_page(html: str) -> tuple[list[str], list[str]]:
    """(violations, referenced paths) of ``dist/index.html``."""
    parser = _Page()
    parser.feed(html)
    parser.close()
    problems = list(parser.problems)
    for url in ABSOLUTE_URL.findall(html):
        if not url.startswith(ALLOWED_ORIGIN):
            problems.append(f"absolute URL {url}")
    if DATA_FONT.search(html):
        problems.append("data: font in the page")
    if not parser.has_title:
        problems.append("no <title>")
    if not parser.has_viewport:
        problems.append("no viewport meta")
    if not parser.lang:
        problems.append("<html> has no lang")
    return problems, parser.references


def check_css(css: str, name: str = "css") -> list[str]:
    stripped = CSS_COMMENT.sub("", css)
    problems = []
    if re.search(r"@font-face\b", stripped, re.I):
        problems.append(f"{name}: @font-face (system font stack only)")
    if DATA_FONT.search(stripped):
        problems.append(f"{name}: data: font (the CSP has no font-src)")
    if CSS_ABSOLUTE_URL.search(stripped):
        problems.append(f"{name}: absolute url()")
    problems += [f"{name}: motion declaration {m.group(0)!r}" for m in MOTION.finditer(stripped)]
    return problems


def check_dist(dist: Path) -> list[str]:
    index = dist / "index.html"
    if not index.is_file():
        return [f"no {index}"]
    problems, references = check_page(index.read_text(encoding="utf-8"))
    for ref in references:
        if not ref.startswith(BASE):
            problems.append(f"{ref} does not start with the baked-in base {BASE} (R12)")
            continue
        if not (dist / ref.removeprefix(BASE)).is_file():
            problems.append(f"{ref} is referenced but not in the dist")
    for css in sorted(dist.rglob("*.css")):
        problems += check_css(css.read_text(encoding="utf-8"), css.relative_to(dist).as_posix())
    for font in [*dist.rglob("*.woff"), *dist.rglob("*.woff2"), *dist.rglob("*.ttf"), *dist.rglob("*.otf")]:
        problems.append(f"font file {font.relative_to(dist).as_posix()} (web fonts are out of scope)")
    return problems


GOOD_HTML = ('<!doctype html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">'
             '<title>x</title><script type="module" crossorigin src="/v2/assets/index-abc.js"></script>'
             '<link rel="stylesheet" crossorigin href="/v2/assets/index-abc.css"></head><body><div id="root"></div></body></html>')
GOOD_CSS = ":root{--tw-transition:none;--duration:0s}.a{color:red}"


def make_dist(root: Path, html: str = GOOD_HTML, css: str = GOOD_CSS) -> Path:
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(html, encoding="utf-8")
    (root / "assets" / "index-abc.js").write_text("export {}", encoding="utf-8")
    (root / "assets" / "index-abc.css").write_text(css, encoding="utf-8")
    return root


# ---------------------------------------------------------------- the checker is not vacuous

def test_a_clean_dist_passes(tmp_path):
    assert check_dist(make_dist(tmp_path)) == []


def test_a_custom_property_named_like_a_transition_is_not_a_declaration(tmp_path):
    assert check_css(":root{--tw-transition:none;--animation-name:x}") == []


@pytest.mark.parametrize("fragment, expected", [
    ("<script>alert(1)</script>", "inline <script>"),
    ('<script type="module">import("/x")</script>', "inline <script>"),
    ("<style>a{}</style>", "<style> element"),
    ('<div style="color:red"></div>', "style= attribute"),
    ('<base href="/v2/">', "<base>"),
    ('<meta name="robots" content="noindex">', "<meta name=\"robots\">"),
    ('<meta name="ROBOTS" content="noindex">', "<meta name=\"robots\">"),
    ('<link rel="preload" href="https://cdn.example.com/x.woff2">', "absolute URL https://cdn.example.com/x.woff2"),
    ('<img src="//evil.example/p.png">', "absolute URL //evil.example/p.png"),
    ('<img src="data:font/woff2;base64,AAAA">', "data: font"),
    ('<script src="/v2/assets/missing.js"></script>', "not in the dist"),
    ('<script src="/assets/index-abc.js"></script>', "baked-in base"),
    ('<script src="./assets/index-abc.js"></script>', "baked-in base"),
])
def test_the_page_checker_catches_each_forbidden_construct(tmp_path, fragment, expected):
    html = GOOD_HTML.replace("</head>", fragment + "</head>")
    problems = check_dist(make_dist(tmp_path, html=html))
    assert any(expected in p for p in problems), problems


def test_the_cloudflare_origin_is_the_one_absolute_url_the_page_may_name(tmp_path):
    html = GOOD_HTML.replace("</head>", '<link rel="preconnect" href="https://challenges.cloudflare.com"></head>')
    problems = check_page(html)[0]
    assert not [p for p in problems if "absolute URL" in p]


@pytest.mark.parametrize("missing", ["<title>x</title>", ' lang="en"', '<meta name="viewport" content="width=device-width">'])
def test_the_page_needs_a_title_a_language_and_a_viewport(missing):
    assert check_page(GOOD_HTML.replace(missing, ""))[0], missing


@pytest.mark.parametrize("css, expected", [
    ("@font-face{font-family:x;src:url(/v2/assets/x.woff2)}", "@font-face"),
    (".a{src:url(data:font/woff2;base64,AAAA)}", "data: font"),
    (".a{src:url(data:application/x-font-woff;base64,AAAA)}", "data: font"),
    (".a{background:url(https://cdn.example.com/p.png)}", "absolute url()"),
    (".a{background:url( '//cdn.example.com/p.png')}", "absolute url()"),
    (".a{transition:opacity .2s}", "transition:"),
    (".a{transition-property:color}", "transition-property"),
    (".a{transition-duration:.15s}", "transition-duration"),
    (".a{animation:spin 1s}", "animation:"),
    (".a{animation-name:spin}", "animation-name"),
    (".a{animation-duration:1s}", "animation-duration"),
    ("@keyframes spin{to{opacity:0}}", "@keyframes"),
    ("@-webkit-keyframes spin{to{opacity:0}}", "keyframes"),
    (".a{TRANSITION : none}", "transition"),
])
def test_the_css_checker_catches_each_forbidden_construct(css, expected):
    problems = check_css(css)
    assert any(expected.lower() in p.lower() for p in problems), problems


def test_a_commented_out_declaration_is_not_a_finding():
    assert check_css("/* transition: all 1s; @keyframes x{} */.a{color:red}") == []


def test_a_font_file_in_the_dist_is_a_finding(tmp_path):
    dist = make_dist(tmp_path)
    (dist / "assets" / "inter.woff2").write_bytes(b"wOF2")
    assert any("font file" in p for p in check_dist(dist))


def test_a_dist_without_an_index_is_a_finding(tmp_path):
    assert check_dist(tmp_path) == [f"no {tmp_path / 'index.html'}"]


def test_css_in_a_nested_directory_is_checked_too(tmp_path):
    dist = make_dist(tmp_path)
    (dist / "assets" / "nested").mkdir()
    (dist / "assets" / "nested" / "late.css").write_text(".a{transition:none}", encoding="utf-8")
    assert any("late.css" in p for p in check_dist(dist))


# ---------------------------------------------------------------- the real dist

@pytest.fixture(scope="module")
def real_dist() -> Path:
    if not (DIST / "index.html").is_file():
        pytest.skip(f"no built app at {DIST} (run `npm run build` in web/, or set WEB_DIST); the CI web job runs this after the build")
    return DIST


def test_the_built_app_meets_the_csp_contract(real_dist):
    assert check_dist(real_dist) == []


def test_the_built_app_has_one_entry_script_and_stylesheet_under_the_base(real_dist):
    problems, references = check_page((real_dist / "index.html").read_text(encoding="utf-8"))
    assert problems == []
    assert any(r.startswith(BASE + "assets/") and r.endswith(".js") for r in references)
    assert any(r.startswith(BASE + "assets/") and r.endswith(".css") for r in references)


def test_the_built_app_ships_css_and_javascript(real_dist):
    assert list(real_dist.rglob("*.css")) and list((real_dist / "assets").glob("*.js"))


def test_the_built_css_carries_no_keyframes_from_tw_animate_css_or_shadcn(real_dist):
    css = "\n".join(CSS_COMMENT.sub("", p.read_text(encoding="utf-8")) for p in real_dist.rglob("*.css"))
    assert css and "@keyframes" not in css
