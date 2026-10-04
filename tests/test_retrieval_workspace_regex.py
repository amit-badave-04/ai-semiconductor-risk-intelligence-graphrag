"""``strip_links_images`` is linear time, idempotent, and strips exactly what it always stripped (M5a I2, D3 and E3).

The old implementation chained six regexes and four of them were quadratic on adversarial input (222 ms for 10,000
characters of letters, 886 ms for 20,000). It is a security control (an answer driven by untrusted uploaded text must
never carry a link or an image) and, in the async path, it runs ON the event loop, where a thread does not help: ``re``
holds the GIL for a whole match. An answer can be thousands of characters long and a prompt-injected document can
influence it, so the cost was a latency attack surface. It also ran each pass ONCE, so when a later pass removed the
middle of a construct an earlier pass had skipped, the two halves met and formed a live link, image or tag. It now
repeats the passes until the text stops changing, up to ``MAX_STRIP_ROUNDS`` rounds, and past that cap removes every
character that could still start a construct (``_defang``). These tests pin:

1. DIFFERENTIAL: ``strip_links_images`` returns what the verbatim copy of the OLD implementation below returns,
   iterated the way the new loop iterates it: the old output itself where that is already a fixed point, the fixed
   point reached by iterating it within the cap, and ``_defang`` of where the cap stopped it beyond that. Checked on
   the existing unit cases, explicit trap cases, seeded random token, character, noisy and nested strings and every
   adversarial input at 3,000 characters.
2. IDEMPOTENCE AND SECURITY: ``strip(strip(x)) == strip(x)`` on every corpus string; no URL, image, tag, link,
   reference link or reference definition (in the shapes the old patterns match) survives in any output; a citation
   set apart from the text always survives; ``_defang`` alone is a fixed point that keeps every citation.
3. REASSEMBLY: the eight inputs a review found (a second pass removes what the first one left) and deeper nestings
   of each shape, up to depth 5,000, now end as fixed points with no live construct.
4. PERFORMANCE: a measured budget on adversarial inputs, on nests, and on the product of the cap and the most
   expensive round that does not shrink; and a linear-scaling check.

Plain pytest and no network: this file must stay importable in the CI ``serve-shipped`` job.
"""

from __future__ import annotations

import contextlib
import gc
import math
import random
import re
import time
from collections.abc import Callable

import pytest

from semigraph.retrieval import workspace as ws
from semigraph.retrieval.ids import CITE_RE

C1 = "0001045810-26-000021:I.1A:0361"
DOC1 = "doc:0123456789ab:v1:0007"
DOC2 = "doc:0123456789ab:v2:0003"
XBRL = "xbrl:1045810:revenue:2026-06-28"
FR = "fr:2026-04123"
CITES = (C1, DOC1, DOC2, XBRL, FR)


# ----------------------------------------------------------------------------------------------------------------
# The OLD implementation, copied verbatim from src/semigraph/retrieval/workspace.py at commit ec083e7 (before D3).
# Never edit this block: it is the oracle the differential tests compare against.

_LEGACY_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LEGACY_MD_LINK_RE = re.compile(r"\[([^\[\]]+)\]\(([^)]*)\)")
_LEGACY_REF_LINK_RE = re.compile(r"!?\[([^\[\]]+)\]\[([^\[\]]+)\]")
_LEGACY_REF_LINK_DEF_RE = re.compile(r"^[ \t]*\[[^\]]+\]:[ \t]*\S.*$", re.M)
_LEGACY_HTML_TAG_RE = re.compile(r"<\/?[a-zA-Z][^<>]*>")
_LEGACY_BARE_URL_RE = re.compile(r"(?:[a-z][a-z0-9+.\-]*://|//)[^\s\[\]()<>]+|\bwww\.[^\s\[\]()<>]+", re.I)


def _legacy_link_replacement(match: re.Match) -> str:
    label = match.group(1)
    return f"[{label}]" if CITE_RE.fullmatch(f"[{label}]") else ""


def _legacy_ref_link_replacement(match: re.Match) -> str:
    first, second = f"[{match.group(1)}]", f"[{match.group(2)}]"
    if CITE_RE.fullmatch(first) or CITE_RE.fullmatch(second):
        return match.group(0)
    return ""


def _legacy_strip_links_images(text: str) -> str:
    text = _LEGACY_MD_IMAGE_RE.sub("", text)
    text = _LEGACY_REF_LINK_RE.sub(_legacy_ref_link_replacement, text)
    text = _LEGACY_MD_LINK_RE.sub(_legacy_link_replacement, text)
    text = _LEGACY_REF_LINK_DEF_RE.sub("", text)
    text = _LEGACY_HTML_TAG_RE.sub("", text)
    return _LEGACY_BARE_URL_RE.sub("", text)


def _legacy_trace(text: str) -> tuple[str, bool]:
    """The old function iterated the way the new loop iterates: ``(output, reached_the_cap)``. The output is the first
    text a round leaves unchanged, when that happens within ``MAX_STRIP_ROUNDS`` rounds; otherwise it is ``_defang`` of
    the text the last allowed round produced. Only the per-round work is independent of the new code (the old patterns
    above); the loop shape and the fallback are the spec."""
    for _ in range(ws.MAX_STRIP_ROUNDS):
        stripped = _legacy_strip_links_images(text)
        if stripped == text:
            return text, False
        text = stripped
    return ws._defang(text), True


# ----------------------------------------------------------------------------------------------------------------
# Cases

# The parametrized strip cases of tests/test_retrieval_workspace.py (copied: that file is not importable as a module).
EXISTING_CASES = [
    (f"Margin was 41.5% [{DOC1}]. See ![x](https://evil.test/p.png).", f"Margin was 41.5% [{DOC1}]. See ."),
    (f"See [here](https://evil.test) for more [{DOC1}].", f"See  for more [{DOC1}]."),
    (f"Visit https://evil.test/x now, then read [{DOC1}].", f"Visit  now, then read [{DOC1}]."),
    (f"[{DOC1}](https://evil.test/x)", f"[{DOC1}]"),
    (f"See https://evil.test[{DOC1}] now.", f"See [{DOC1}] now."),
    ("See HTTPS://evil.example/login for details.", "See  for details."),
    ("visit www.evil.example/login now.", "visit  now."),
    ("<img src=//evil.example/x>", ""),
    ("<https://evil.example>", ""),
    ("![a][r]\n\nSome text.\n\n[r]: //evil.example/x.png\n", "\n\nSome text.\n\n\n"),
    ("Click [here][ref] to read more.\n\n[ref]: https://evil.example/x\n", "Click  to read more.\n\n\n"),
    (f"See [{DOC1}][{DOC2}] for the two versions.", f"See [{DOC1}][{DOC2}] for the two versions."),
    ("revenue < 5 and > 3, but x<10 stayed a plain comparison.",
     "revenue < 5 and > 3, but x<10 stayed a plain comparison."),
    (f"[{C1}] [{DOC1}] [{XBRL}] [{FR}]", f"[{C1}] [{DOC1}] [{XBRL}] [{FR}]"),
]

# Inputs that sit on the boundary of every rewritten pattern. Each one is a place a plausible "linear" rewrite goes
# wrong: a run-start anchor that forgets the digits before the first letter, a letters-only lookbehind, a give-back
# span that ends one character too early or too late, a definition spanning lines, case folding of ``[a-z]``.
TRAP_CASES = [
    # bare URL: scheme runs, www inside a run, the first letter of a run, hosts that are missing
    "1http://x", "a1http://x", "-http://x", "1.2.3+http://x y", "x.www.evil.test", "awww.evil.test", "a.b://x",
    "http://", "http:// x", "http://(x)", "www.", "www. x", "//", "// x", "//x", "a//b", "http:///x", "http:/x",
    "ftp://a b://c d", "a://b://c", "a" * 40 + "://x", "1" * 40, "a1" * 40, "1a" * 40, "a1" * 40 + "://x y",
    "ab-cd.ef+gh://ij kl", "a.www.x.y", ".www.x", "www.www.www.x", "HTTP://X", "hTtP://x/y z", "x:y://z",
    "\u212a://x", "\u017f://x", "1\u212a://evil", "\u212a.www.evil", "k\u212a://x", "http://\xa0x", "http://x\xa0y z",
    "http://x\ry", "(http://x)", "[http://x]", "<http://x>", "'http://x'", "a http://x b http://y c",
    # image
    "![a](b)", "![![a](b)", "![a]![b](c)(d)", "![a](![b](c))", "![![", "![a]x![b](c)", "![a](x", "![a](x ![b](c)",
    "![a]\n(b)", "![]()", "![](", "![]", "![", "!![a](b)", "![a](b](c)", "![a](b) ![c](d", "![a]![b]![c](d)",
    "![x![y]z](w)", "![a]\n![b](c)", "![a][b](c)", "![a](\n)", "![a](b\n)",
    # inline link
    "[a](b", "[a](b [c](d)", "[a](b) [c](d", "[[a](b)x](c)", "[a]\n(b)", "[](b)", "[a]()", "[a](b)(c)", "[a](b))",
    "[a][b](c)", "[a](b)[c](d)", f"[{DOC1}](x)", f"[{DOC1}](x", f"[{DOC1}][{DOC2}](x)", "[a [b](c) d](e)",
    # reference-style link
    "[a][b]", "![a][b]", "[a][b][c]", "[a][b", "[a][", f"[{DOC1}][b]", f"[a][{DOC2}]", "[a]\n[b]", "[a] [b]",
    # reference definition
    "[ref]: x", "[ref]:x", "[ref]:", "[ref]: ", "[ref]:\tx", "[ref]:  \t\n", "[ref]:  \t\nx", "[a\nb]: c", "[\n]: c",
    "[]: c", "  [a]: b", "\t[a]:\tb", "[a]x\n[b]: c", "[a\n[b]: c", "x [a]: b", "[a]: b\n[c]: d", "[a]:   \n[b]: c",
    "[a]\r\n: b", "[a]: b\r\n[c]: d", "[a]:b\n[c]:d\n", "[a\n\n\n]: b", "[a]\n\n[b]: c", "[a]: [b]: c", "[a]:[b]:c",
    "[a\n[b\n[c]: d", "\n\n[a]: b\n\n", "[a]: b\n  [c]\n[d]: e", " [a b]: c d e\nf",
    # html tag and autolink
    "<<b>a>", "<a><b", "<a b", "<>", "<a>>", "< a>", "</a>", "<a\n>", "<a<b>", "<a <b> c>", "<1>", "</1>", "<a/>",
    "a<b and c>d", "<a href=x", "x<a", "<https://e.test/x>", "<mailto:a@e.test>", "<a><a><a>",
    # citations next to everything
    f"[{DOC1}]", f"[{C1}][{DOC1}]", f"https://e.test[{DOC1}]", f"//e.test/x[{DOC1}]", f"www.e.test[{DOC1}]",
    f"<a>[{DOC1}]</a>", f"![x]({DOC1}) [{DOC1}]", f"[{DOC1}]: x", f"[{DOC1}]:\n[{DOC2}]: y", f"(http://x)[{DOC1}]",
]


def _fill(unit: str, n: int) -> str:
    """Exactly ``n`` characters of ``unit`` repeated (cut mid-unit when ``n`` is not a multiple)."""
    return (unit * (n // len(unit) + 1))[:n]


def _adversarial(n: int) -> dict[str, str]:
    """Inputs built to make a regex restart over and over, plus floods of VALID constructs (the give-back and the
    callback paths), each exactly ``n`` characters long."""
    cases = {
        "letters": _fill("a", n), "upper-letters": _fill("A", n), "open-bracket": _fill("[", n),
        "open-paren": _fill("(", n), "open-angle": _fill("<", n), "close-bracket": _fill("]", n),
        "http-run": _fill("http", n), "www-run": _fill("www.", n), "image-openers": _fill("![", n),
        "link-openers": _fill("[a](", n), "bracket-newline": _fill("[\n", n),
        "bracket-letter-newline": _fill("[a\n", n), "image-no-target": _fill("![a]x", n),
        "image-empty-alt": _fill("![]", n), "image-then-bracket": _fill("![a]", n),
        "bracket-closed-newline": _fill("[a]\n", n), "refdef-no-url": _fill("[a]: \n", n),
        "refdef-colon-run": _fill("[a]:", n), "angle-letter": _fill("<a", n), "angle-space": _fill("<a ", n),
        "digits": _fill("1", n), "dots": _fill(".", n), "dotted-letters": _fill("a.", n),
        "digit-letter": _fill("1a", n), "letter-digit": _fill("a1", n), "plus-minus-letter": _fill("-a", n),
        "slashes": _fill("/", n), "double-slashes": _fill("//", n), "scheme-seps": _fill("a://", n),
        "colon-slash": _fill(":/", n), "spaces": _fill(" ", n), "tabs": _fill("\t", n), "newlines": _fill("\n", n),
        "image-bracket-openers": _fill("![a][", n), "ref-openers": _fill("[a][", n),
        "mixed-openers": _fill("![a](<[http://", n), "tag-then-bracket": _fill("<a [x](", n),
        "letters-then-url": "a" * (n - 18) + "http://evil.test/x",
        "unterminated-image": "![a](" + "x" * (n - 5), "unterminated-link": "[a](" + "x" * (n - 4),
        "unterminated-tag": "<a " + "x" * (n - 3), "unterminated-bracket": "[" + "x" * (n - 1),
        "unterminated-ref-definition": "[a" + "x" * (n - 2), "long-image-alt-and-target": "![" + "x" * (n // 2 - 2) +
        "](" + "y" * (n - n // 2 - 2),
        "valid-links": _fill("[a](b)", n), "valid-images": _fill("![a](b)", n), "valid-ref-links": _fill("[a][b]", n),
        "valid-definitions": _fill("[a]: b\n", n), "valid-tags": _fill("<b>", n), "valid-urls": _fill("http://x ", n),
        "valid-www": _fill("www.x ", n), "valid-citations": _fill(f"[{DOC1}] ", n),
        "valid-citation-links": _fill(f"[{DOC1}](x)", n), "valid-adjacent-citations": _fill(f"[{DOC1}][{DOC2}]", n),
    }
    bad = {k: len(v) for k, v in cases.items() if len(v) != n}
    assert not bad, bad
    return cases


# --- seeded random corpora ---------------------------------------------------------------------------------------

_TOKENS = (
    # citations, bare and in the shapes a link or a reference link can imitate
    *(f"[{c}]" for c in CITES), f"[{DOC1}](http://evil.test/x)", f"[{C1}](//evil.test)", f"[{DOC1}][{DOC2}]",
    f"[{C1}][ref]", f"[ref][{FR}]", f"[{DOC1}]: http://evil.test", f"http://evil.test[{DOC1}]", f"<a>[{DOC1}]</a>",
    # inline links and images
    "[text](http://evil.test/p)", "[text](//evil.test/p)", "[text](www.evil.test)", "[text]()", "[a b](rel/path.html)",
    "[](x)", "[x](y z)", "[[x]](y)", "[x](y(z))", "[x](", "[x]", "[x](y", "![alt](http://evil.test/p.png)",
    "![](x.png)", "![alt text]", "![alt][ref]", "![", "![a](", "![a]![b](c)", "![![a](b)",
    # reference-style links and definitions
    "[text][ref]", "[ref]: http://evil.test/x", "  [ref]: //evil.test/x", "[ref]:", "[ref]: ", "[ref]:  \t",
    "[ref]\n: x", "[a\nb]: url", "[ref]:x",
    # html tags and autolinks, comparison operators
    "<b>", "</b>", "<img src=//evil.test/x>", '<a href="http://evil.test/x">', "<https://evil.test/x>",
    "<mailto:a@evil.test>", "<", ">", "<3", "a < b", "x > y", "<<b>a>", "<a", "</", "<a b",
    # bare, case-varied, protocol-relative and host-less URLs
    "http://evil.test/x", "HTTPS://EVIL.TEST/X", "ftp://evil.test", "www.evil.test", "WWW.EVIL.TEST/x",
    "//evil.test/p", "a+b-c.d://x", "1http://x", "x.www.evil.test", "awww.evil.test", "http://", "www.", "//",
    "http:/x", ":// ",
    # punctuation, words, whitespace
    "(", ")", "[", "]", "(x)", "]:", ":", "!", "![", "](", "][", ".", ",", ";", "'", '"', "-", "+", "/", "\\",
    "revenue", "was", "41.5%", "Q2", "TSMC", "margin", "a", "bc", "d3", "1", "22",
    " ", "  ", "\n", "\n\n", "\t", "\r\n",
)
_SEPARATORS = ("", "", "", " ", "\n")
_CHAR_ALPHABET = "![]():/<>.wWhHtp1a-+_ \n\t\r,\xa0\u212a"
_NOISE = "[]()<>!:/\n "
TOKEN_SEED, CHAR_SEED, NOISY_SEED, LONG_SEED, NESTED_SEED = 20261004, 20261005, 20261006, 20261007, 20261009
TOKEN_COUNT, CHAR_COUNT, NOISY_COUNT, LONG_COUNT, NESTED_COUNT = 20_000, 20_000, 10_000, 300, 10_000

# A frame is an (opener, closer) pair that forms a construct once whatever sits between them is removed: `<` + `<b>` +
# `img ...>` is a tag only after `<b>` goes, which is exactly how a later pass reassembles what an earlier one skipped.
_FRAMES = (
    ("<", "img src=x onerror=alert(1)>"), ("<", "script>alert(1)</script>"), ("<a", ">"), ("</", "b>"),
    ("[a ", " b](javascript:alert(1))"), ("[", "](http://evil.test)"), ("[", "x](y)"), ("![", "]()"), ("![", "](y)"),
    ("[x]", "[r]"), ("![x]", "[r]"), ("\n[x]", ":t\n"), (f"[{DOC1}]", "(javascript:alert(1))"), (f"[{DOC1}]", "(x)"),
    ("http:", "//evil.test/x"), ("ww", "w.evil.test"), ("//e", "x[r]"), ("[", "]: x"),
)
# Complete constructs that a pass removes whole: the innermost thing a frame wraps.
_REMOVABLE = ("<b>", "</i>", "[x](y)", "![x](y)", "[x][y]", "http://evil.test/x", "www.evil.test/a",
              "<https://evil.test>", "//evil.test/p", "\n[r]: u\n")
_NESTED_PREFIXES = ("", "", "See ", f"[{DOC1}] ", "\n\n", "x < 5 and ")


def _token_string(rng: random.Random, limit: int) -> str:
    target = rng.randint(1, limit)
    parts: list[str] = []
    size = 0
    while size < target:
        part = rng.choice(_TOKENS) + rng.choice(_SEPARATORS)
        parts.append(part)
        size += len(part)
    return "".join(parts)[:target]


def token_corpus(seed: int, count: int, limit: int = 300) -> list[str]:
    rng = random.Random(seed)
    return [_token_string(rng, limit) for _ in range(count)]


def char_corpus(seed: int, count: int, limit: int = 120) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choices(_CHAR_ALPHABET, k=rng.randint(1, limit))) for _ in range(count)]


def noisy_corpus(seed: int, count: int) -> list[str]:
    """Token strings with a few random structural characters spliced in, to break constructs in the middle."""
    rng = random.Random(seed)
    strings = []
    for _ in range(count):
        chars = list(_token_string(rng, 300))
        for _ in range(rng.randint(1, 4)):
            chars.insert(rng.randint(0, len(chars)), rng.choice(_NOISE))
        strings.append("".join(chars)[:300])
    return strings


def nested_corpus(seed: int, count: int) -> list[str]:
    """Frames wrapped around one another, 1 to 14 deep, with a prefix and a few structural characters spliced in. Most
    layers repeat one base frame (the frames that peel one layer per round only chain when the same one repeats; the
    cap is lower than 14, so a good share of these cannot converge within it) and some are of a random kind."""
    rng = random.Random(seed)
    strings = []
    for _ in range(count):
        text = rng.choice(_REMOVABLE)
        base = rng.choice(_FRAMES)
        for _ in range(rng.randint(1, 14)):
            opener, closer = base if rng.random() < 0.75 else rng.choice(_FRAMES)
            text = opener + text + closer
        chars = list(rng.choice(_NESTED_PREFIXES) + text)
        for _ in range(rng.randint(0, 3)):
            chars.insert(rng.randint(0, len(chars)), rng.choice(_NOISE))
        strings.append("".join(chars))
    return strings


@pytest.fixture(scope="module")
def corpora() -> dict[str, list[str]]:
    return {
        "token": token_corpus(TOKEN_SEED, TOKEN_COUNT),
        "char": char_corpus(CHAR_SEED, CHAR_COUNT),
        "noisy": noisy_corpus(NOISY_SEED, NOISY_COUNT),
        "long": token_corpus(LONG_SEED, LONG_COUNT, limit=6_000),
        "nested": nested_corpus(NESTED_SEED, NESTED_COUNT),
    }


# ----------------------------------------------------------------------------------------------------------------
# 1. Differential: new == the old function iterated like the new loop (see _legacy_trace)

def _differences(strings: list[str]) -> list[tuple[str, str, str]]:
    found = []
    for text in strings:
        new, expected = ws.strip_links_images(text), _legacy_trace(text)[0]
        if new != expected:
            found.append((text, new, expected))
    return found


def _assert_same_as_legacy(strings: list[str], what: str) -> None:
    differing = _differences(strings)
    assert not differing, (f"{len(differing)} of {len(strings)} {what} differ from the iterated legacy output; first: "
                           f"input={differing[0][0]!r} new={differing[0][1]!r} legacy={differing[0][2]!r}")


def _legacy_is_a_fixed_point(text: str) -> bool:
    once = _legacy_strip_links_images(text)
    return _legacy_strip_links_images(once) == once


@pytest.mark.parametrize("text,expected", EXISTING_CASES)
def test_the_existing_unit_cases_give_the_expected_output_and_the_legacy_output(text, expected):
    assert ws.strip_links_images(text) == expected == _legacy_strip_links_images(text)


def test_trap_cases_match_the_legacy_output():
    _assert_same_as_legacy(TRAP_CASES, "trap cases")


def test_every_trap_case_embedded_in_context_matches_the_legacy_output():
    """The same inputs behind a prefix and in front of a suffix: a span that is skipped or given back must end exactly
    where the old match would have ended, not one character early or late."""
    wrapped = [f"{head}{case}{tail}" for case in TRAP_CASES for head, tail in
               (("x ", " y"), ("\n", "\n"), ("[", "]"), ("(", ")"), ("![", "]("), ("<", ">"), ("http://", ""),
                (":", "//"))]
    _assert_same_as_legacy(wrapped, "wrapped trap cases")


def test_pairs_of_trap_cases_match_the_legacy_output():
    pairs = [a + sep + b for a in TRAP_CASES[::3] for b in TRAP_CASES[1::3] for sep in ("", " ", "\n")]
    _assert_same_as_legacy(pairs, "pairs of trap cases")


@pytest.mark.parametrize("kind", ["token", "char", "noisy"])
def test_random_strings_match_the_legacy_output(corpora, kind):
    strings = corpora[kind]
    assert len(strings) >= 10_000 and all(0 < len(s) <= 300 for s in strings)
    _assert_same_as_legacy(strings, f"{kind} strings")


def test_nested_strings_match_the_legacy_output(corpora):
    strings = corpora["nested"]
    assert len(strings) >= 10_000
    _assert_same_as_legacy(strings, "nested strings")


def test_where_one_legacy_pass_is_already_a_fixed_point_the_output_is_that_pass(corpora):
    """The strict form of the differential: wherever the old function's own output is stable, the new function returns
    exactly that output (not a stricter one). The corpora must also hold plenty of the other kind (reassembly), or the
    other half of the differential would prove nothing."""
    stable = reassembling = 0
    for text in corpora["token"] + corpora["char"] + corpora["noisy"] + corpora["nested"]:
        if _legacy_is_a_fixed_point(text):
            stable += 1
            assert ws.strip_links_images(text) == _legacy_strip_links_images(text), text
        else:
            reassembling += 1
    assert stable > 20_000 and reassembling > 3_000, (stable, reassembling)


def test_the_nested_corpus_reaches_the_fallback(corpora):
    """A guard on the generator: strings that are still changing after ``MAX_STRIP_ROUNDS`` rounds, so the fallback
    branch is exercised by the differential and idempotence checks and not only by the hand-built deep shapes."""
    hits = sum(1 for text in corpora["nested"] if _legacy_trace(text)[1])
    assert hits >= 500, f"only {hits} of {len(corpora['nested'])} nested strings reach the cap"


def test_random_long_documents_match_the_legacy_output(corpora):
    """Documents of up to 6,000 characters: skipped spans cross many constructs here."""
    assert max(len(s) for s in corpora["long"]) > 3_000
    _assert_same_as_legacy(corpora["long"], "long documents")


def test_the_random_corpora_actually_exercise_every_construct(corpora):
    """A guard on the generator, not on the function: if a pass never changes anything in the corpus, the
    differential test above proves nothing about it. The legacy patterns, not the new ones: the new image and
    definition patterns also match (and hand back) any `![` or line-start `[x`, which are not constructs."""
    passes = {
        "image": _LEGACY_MD_IMAGE_RE, "link": _LEGACY_MD_LINK_RE, "ref-link": _LEGACY_REF_LINK_RE,
        "ref-definition": _LEGACY_REF_LINK_DEF_RE, "html": _LEGACY_HTML_TAG_RE, "bare-url": _LEGACY_BARE_URL_RE,
    }
    strings = corpora["token"] + corpora["char"] + corpora["noisy"]
    for name, rx in passes.items():
        hits = sum(1 for s in strings if rx.search(s))
        assert hits >= 500, f"{name}: only {hits} of {len(strings)} strings contain a match"


@pytest.mark.parametrize("name", sorted(_adversarial(3_000)))
def test_adversarial_inputs_at_3000_characters_match_the_legacy_output(name):
    text = _adversarial(3_000)[name]
    assert ws.strip_links_images(text) == _legacy_strip_links_images(text)


# ----------------------------------------------------------------------------------------------------------------
# 2. Security: what a reader could follow must not survive, and every citation must

_HOST = r"[^\s\[\]()<>]"
_URL_FORMS = {
    "scheme url": re.compile(rf"[a-z][a-z0-9+.\-]*://{_HOST}", re.I),
    "protocol-relative url": re.compile(rf"//{_HOST}"),
    "www url": re.compile(rf"\bwww\.{_HOST}", re.I),
}
_LINK_FORMS = {
    "image": re.compile(r"!\[[^\]]*\]\([^)]*\)"),
    "html tag": re.compile(r"<\/?[a-zA-Z][^<>]*>"),
    "inline link": re.compile(r"\[[^\[\]]+\]\([^)]*\)"),
    "reference definition": re.compile(r"^[ \t]*\[[^\]]+\]:[ \t]*\S", re.M),
}
_REF_LINK = re.compile(r"!?\[([^\[\]]+)\]\[([^\[\]]+)\]")


def _ref_links_left(text: str) -> list[str]:
    """Reference-style links still in ``text``, other than two adjacent citations (which only look like one)."""
    return [m.group(0) for m in _REF_LINK.finditer(text)
            if not (CITE_RE.fullmatch(f"[{m.group(1)}]") or CITE_RE.fullmatch(f"[{m.group(2)}]"))]


def _url_forms_left(text: str) -> list[str]:
    return [name for name, form in _URL_FORMS.items() if form.search(text)]


def _link_forms_left(text: str) -> list[str]:
    left = _url_forms_left(text) + [name for name, form in _LINK_FORMS.items() if form.search(text)]
    return left + (["reference link"] if _ref_links_left(text) else [])


def _citations(text: str) -> list[str]:
    return sorted(CITE_RE.findall(text))


# Well-formed answers: every construct complete, separated from the next by whitespace. Nothing here can swallow or
# reassemble a neighbour, so EVERY form must go and EVERY citation must stay.
_PLAIN_SEGMENTS = ("Revenue was 41.5% in Q2.", "Gross margin rose to 62%.", "See the filing.", "x < 5 and y > 3.",
                   "(a note)")
_DANGEROUS_SEGMENTS = (
    "[text](http://evil.test/p)", "![alt](https://evil.test/p.png)", "[t][ref]", "![a][ref]", "<b>", "</b>",
    "<img src=//evil.test/x>", "<https://evil.test/x>", "http://evil.test/x", "HTTPS://EVIL.TEST/X", "www.evil.test/a",
    "WWW.EVIL.TEST", "//evil.test/p", "ftp://evil.test", "a+b-c://x.test", "1http://evil.test",
    "\n[ref]: https://evil.test/x\n", "\n  [ref]: //evil.test/x.png\n", "[a b](rel/path.html)", "[](x)",
    "![](x.png)", "<a href=\"http://evil.test\">",
)
_CITATION_SEGMENTS = (
    *(f"[{c}]" for c in CITES), f"[{DOC1}][{DOC2}]", f"[{C1}](http://evil.test/x)", f"[{DOC1}](//evil.test)",
    f"see http://evil.test[{DOC1}]", f"see www.evil.test[{FR}]", f"<a>[{DOC2}]", f"![x](http://e.test) [{XBRL}]",
)


def well_formed_answers(seed: int, count: int) -> list[str]:
    rng = random.Random(seed)
    segments = _PLAIN_SEGMENTS + _DANGEROUS_SEGMENTS + _CITATION_SEGMENTS
    return ["".join(rng.choice(segments) + rng.choice((" ", "\n", "\n\n")) for _ in range(rng.randint(1, 14)))
            for _ in range(count)]


def test_a_well_formed_answer_loses_every_link_and_keeps_every_citation():
    answers = well_formed_answers(20261008, 5_000)
    for answer in answers:
        stripped = ws.strip_links_images(answer)
        left = _link_forms_left(stripped)
        assert not left, f"{left} survived in {stripped!r} (from {answer!r})"
        assert _citations(stripped) == _citations(answer), f"a citation changed: {answer!r} -> {stripped!r}"


def test_the_well_formed_answers_cover_every_form_and_citation_kind():
    text = "\n".join(well_formed_answers(20261008, 5_000))
    assert set(_citations(text)) == set(CITES)
    for form in (*_URL_FORMS.values(), *_LINK_FORMS.values(), _REF_LINK):
        assert form.search(text)


CORPUS_KINDS = ["token", "char", "noisy", "long", "nested"]


@pytest.mark.parametrize("kind", CORPUS_KINDS)
def test_no_link_form_survives_in_any_random_output(corpora, kind):
    """The strong form of the security predicate, on MALFORMED input too (the old function left residues here, see
    REASSEMBLED): in no output is there a URL with a host, an image, an html tag, an inline link, a reference
    definition or a reference link, in the shapes the old patterns match. A bare ``![`` or ``<a`` that never closes is
    not one of those and is kept (see KNOWN_OPEN_GAPS)."""
    strings = corpora[kind]
    outputs = [ws.strip_links_images(s) for s in strings]
    leaking = [(s, o, _link_forms_left(o)) for s, o in zip(strings, outputs) if _link_forms_left(o)]
    assert not leaking, f"{len(leaking)} outputs still hold a link form; first: {leaking[0]!r}"
    assert sum(1 for s, o in zip(strings, outputs) if s != o) > len(strings) // 10     # and it did strip things


@pytest.mark.parametrize("name", sorted(_adversarial(3_000)))
def test_adversarial_inputs_keep_a_citation_and_leak_no_url(name):
    answer = _adversarial(3_000)[name] + f"\n\nThe margin was 41.5% [{DOC1}].\n"
    stripped = ws.strip_links_images(answer)
    assert not _url_forms_left(stripped)
    assert _citations(stripped) == _citations(answer), stripped[-60:]
    assert DOC1 in _citations(stripped)


SET_APART_PREFIX, SET_APART_SUFFIX = "doc:00000000beef:v9:0009", "fr:2099-00001"


@pytest.mark.parametrize("kind", CORPUS_KINDS)
def test_a_citation_set_apart_from_the_text_always_survives(corpora, kind):
    """The citation half of the security predicate. It cannot be 'every citation in the input survives' for malformed
    input: ``[doc:...]: x`` IS a reference definition, and the old function removed that line, citation and all, as do
    the tag, link and image forms around one. What must hold is that no round and no fallback eats a citation that
    is not inside a construct: one on its own line before the text and one after it, never touched."""
    lost = []
    for text in corpora[kind]:
        stripped = ws.strip_links_images(f"[{SET_APART_PREFIX}]\n\n{text}\n\nSee [{SET_APART_SUFFIX}] for the rule.")
        found = _citations(stripped)
        if SET_APART_PREFIX not in found or SET_APART_SUFFIX not in found:
            lost.append((text, stripped))
    assert not lost, f"{len(lost)} of {len(corpora[kind])} lost a set-apart citation; first: {lost[0]!r}"


# ----------------------------------------------------------------------------------------------------------------
# 3. Reassembly and idempotence: the passes repeat until nothing changes, and past the cap `_defang` ends it

def _nest(frame: tuple[str, str], inner: str, depth: int) -> str:
    opener, closer = frame
    return opener * depth + inner + closer * depth


def _repeat(unit: str, depth: int) -> str:
    return "\n".join([unit] * depth)


# name -> builder(depth). The first four nest for real (each round peels one layer, so depth d takes d + 1 changing
# rounds); the image shape nests too but its outer image swallows the inner ones in one round; the last three repeat a
# unit that reassembles only after a later pass has removed the middle of it.
SHAPES: dict[str, Callable[[int], str]] = {
    "tag-img": lambda depth: _nest(("<", "img src=x onerror=alert(1)>"), "<b>", depth),
    "tag-script": lambda depth: _nest(("<", "script>alert(1)</script>"), "<b>", depth),
    "tag-a": lambda depth: _nest(("<a", ">"), "<b>", depth),
    "link": lambda depth: _nest(("[a ", " b](javascript:alert(1))"), "[x](y)", depth),
    "image": lambda depth: _nest(("![", "]()"), "[x][x]", depth),
    "citation-link": lambda depth: _nest(("", "(a)"), f"[{DOC1}](http://e.test)", depth),
    "ref-definition": lambda depth: _repeat("//x[x]:t", depth),
    "ref-link": lambda depth: _repeat("[x]www.e[r]", depth),
}
SHAPE_DEPTHS = (1, 2, 5, 50, 5_000)

# The eight inputs a previous review found (all reproduced on the old function): (input, output now). Each used to come
# out as something that a second pass removed.
REASSEMBLED = [
    ("<<b>img src=x onerror=alert(1)>", ""),
    ("<<b>script>alert(1)</script>", "alert(1)"),
    ("<a<b>>", ""),
    ("[a [x](y) b](javascript:alert(1))", ")"),
    (f"[{DOC1}](http://e.test)(javascript:alert(1))", f"[{DOC1}])"),
    ("![[x][x]]()", ""),
    ("//x[x]:t", ""),
    ("[x]www.e[r]", ""),
]


@pytest.mark.parametrize("text,expected", REASSEMBLED)
def test_a_reassembled_construct_is_removed_and_the_output_is_a_fixed_point(text, expected):
    assert _legacy_strip_links_images(_legacy_strip_links_images(text)) != _legacy_strip_links_images(text)
    stripped = ws.strip_links_images(text)
    assert stripped == expected
    assert ws.strip_links_images(stripped) == stripped
    assert not _link_forms_left(stripped)


@pytest.mark.parametrize("depth", SHAPE_DEPTHS)
@pytest.mark.parametrize("name", sorted(SHAPES))
def test_a_reassembly_shape_at_any_depth_ends_as_a_fixed_point_without_a_live_construct(name, depth):
    text = SHAPES[name](depth)
    stripped = ws.strip_links_images(text)
    assert ws.strip_links_images(stripped) == stripped
    assert not _link_forms_left(stripped), stripped[:200]
    assert _citations(stripped) == ([DOC1] if name == "citation-link" else [])


def _counting_defang(monkeypatch) -> list[str]:
    """Replaces ``_defang`` with a recorder that still defangs; returns the list of texts it was called with."""
    real, calls = ws._defang, []
    monkeypatch.setattr(ws, "_defang", lambda text: calls.append(text) or real(text))
    return calls


def test_the_cap_is_the_number_of_rounds_and_the_fallback_runs_only_past_it(monkeypatch):
    """Depth d of the tag shape takes d + 1 changing rounds and one more to see that nothing changes. The deepest one
    the cap still verifies is exact (the empty string, everything removed, no fallback). One layer deeper uses every
    round to strip its last layer and never gets the round that confirms it, so it goes to ``_defang`` (which finds
    nothing left to do). Two layers deeper still has a layer left at the cap, which ``_defang`` cannot peel."""
    calls = _counting_defang(monkeypatch)
    assert ws.strip_links_images(SHAPES["tag-img"](ws.MAX_STRIP_ROUNDS - 2)) == "" and not calls
    assert ws.strip_links_images(SHAPES["tag-img"](ws.MAX_STRIP_ROUNDS - 1)) == "" and calls == [""]
    calls.clear()
    assert ws.strip_links_images(SHAPES["tag-img"](ws.MAX_STRIP_ROUNDS)) != "" and len(calls) == 1


def test_the_loop_never_runs_more_rounds_than_the_cap(monkeypatch):
    rounds = []
    real = ws._MD_IMAGE_RE

    class CountingRegex:
        def sub(self, repl, text, *args, **kwargs):
            rounds.append(len(text))
            return real.sub(repl, text, *args, **kwargs)

    monkeypatch.setattr(ws, "_MD_IMAGE_RE", CountingRegex())
    ws.strip_links_images(SHAPES["tag-img"](5_000))
    assert len(rounds) == ws.MAX_STRIP_ROUNDS
    assert rounds == sorted(rounds, reverse=True)      # a round never adds a character


@pytest.mark.parametrize("kind", CORPUS_KINDS)
def test_strip_is_idempotent_on_every_corpus(corpora, kind):
    not_idempotent = []
    for text in corpora[kind]:
        once = ws.strip_links_images(text)
        if ws.strip_links_images(once) != once:
            not_idempotent.append((text, once))
    assert not not_idempotent, (f"{len(not_idempotent)} of {len(corpora[kind])} changed on a second pass; "
                                f"first: {not_idempotent[0]!r}")


def test_the_idempotence_corpora_hold_at_least_50000_strings(corpora):
    assert sum(len(strings) for strings in corpora.values()) >= 50_000


@pytest.mark.parametrize("kind", CORPUS_KINDS)
def test_defang_alone_is_a_fixed_point_keeps_every_citation_and_leaves_no_link_form(corpora, kind):
    """What makes the function idempotent beyond the cap: on ANY text, not only one that reached the cap. A fixed point
    of ``strip_links_images`` is a text no round changes, so it is exactly the property that a round finds nothing to
    do. Every citation survives (``_defang`` only drops brackets that are not part of one, a URL, a tag opener, and the
    ``(`` / ``:`` that would make a citation a link or a definition)."""
    broken = []
    for text in corpora[kind]:
        defanged = ws._defang(text)
        if (ws.strip_links_images(defanged) != defanged or _link_forms_left(defanged)
                or _citations(defanged) != _citations(text)):
            broken.append((text, defanged))
    assert not broken, f"{len(broken)} of {len(corpora[kind])} broke a _defang property; first: {broken[0]!r}"


# Fragments the old function leaves in place and the loop keeps, on purpose. They are NOT in the shapes the old patterns
# match, so the form checks above pass, and removing them would change what the function strips for inputs that are
# already stable (the differential above forbids it): an owner decision, not a side effect of idempotence. They stay
# listed here so the gap stays visible: a change that closes one must change this table in the same commit.
KNOWN_OPEN_GAPS = [
    ("<img src=x onerror=alert(1) <1>", "a tag with a `<` in its attributes: the pattern stops there, a parser not"),
    ("[a [b] c](javascript:alert(1))", "balanced brackets inside link text: the link pattern never matches it"),
    ("[[x]](y)", "the same, with a non-URL target"),
    ("<a", "an unclosed tag opener"),
    ("![alt", "an unclosed image opener"),
]


@pytest.mark.parametrize("text,why", KNOWN_OPEN_GAPS)
def test_known_open_gaps_are_kept_as_the_old_function_kept_them(text, why):
    assert _legacy_strip_links_images(text) == text, why
    assert ws.strip_links_images(text) == text, why


# ----------------------------------------------------------------------------------------------------------------
# 4. Performance (the loop-blocking time of the pure-Python regex work; it holds the GIL, a thread does not help)

# p95 over REPS runs at 20,000 characters. Measured: one round of anything is about 1 to 2 ms, a nest 3 to 5 ms, and
# the worst input there is (a nest deeper than the cap before the most expensive filler no round shrinks) about 9 ms.
BUDGET_MS = 20.0
REPS = 10
SCALING_LIMIT = 3.0        # t(40,000) / t(20,000): linear is 2, quadratic is 4
SCALING_FLOOR_MS = 1.0     # below this a sub-millisecond ratio is timer noise, not a trend


@contextlib.contextmanager
def _quiet_gc():
    """A collection pause in the middle of a 1 ms measurement is noise; keep it out of the sample."""
    was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def _samples_ms(text: str, reps: int) -> list[float]:
    samples = []
    with _quiet_gc():
        for _ in range(reps):
            started = time.perf_counter()
            ws.strip_links_images(text)
            samples.append((time.perf_counter() - started) * 1000)
    return samples


def _p95(samples: list[float]) -> float:
    """Nearest-rank 95th percentile (with 10 samples this is the slowest run)."""
    return sorted(samples)[math.ceil(0.95 * len(samples)) - 1]


ADVERSARIAL_NAMES = sorted(_adversarial(100))


@pytest.mark.parametrize("name", ADVERSARIAL_NAMES)
def test_a_20000_character_input_costs_under_the_budget(name):
    text = _adversarial(20_000)[name]
    p95 = _p95(_samples_ms(text, REPS))
    assert p95 < BUDGET_MS, f"{name}: p95 over {REPS} runs was {p95:.2f} ms, budget {BUDGET_MS} ms"


def _scaling_ratio(small: str, large: str, reps: int = 5) -> tuple[float, float, float]:
    t_small, t_large = min(_samples_ms(small, reps)), min(_samples_ms(large, reps))
    return t_small, t_large, t_large / max(t_small, SCALING_FLOOR_MS)


def _assert_scales(small: str, large: str, limit: float, what: str, reps: int = 5) -> None:
    """One retry: one noisy sample must not flake it."""
    t_small, t_large, ratio = _scaling_ratio(small, large, reps)
    if ratio > limit:
        t_small, t_large, ratio = _scaling_ratio(small, large, reps)
    assert ratio <= limit, (f"{what}: {len(small):,} chars {t_small:.2f} ms, {len(large):,} chars {t_large:.2f} ms "
                            f"(ratio {ratio:.2f} against the {SCALING_FLOOR_MS} ms floor, limit {limit})")


@pytest.mark.parametrize("name", ADVERSARIAL_NAMES)
def test_cost_grows_linearly_with_length(name):
    """Doubling the input must not much more than double the time."""
    _assert_scales(_adversarial(20_000)[name], _adversarial(40_000)[name], SCALING_LIMIT, name)


# The loop multiplies the cost of one round by at most MAX_STRIP_ROUNDS, and it runs every round only when the text
# keeps changing. A nest deeper than the cap is tiny, so an attacker pays almost nothing to make every round run, and
# can fill the rest of the answer with the one thing a round does not shrink: the worst case is the cap times the most
# expensive round, then the fallback. The nest goes FIRST so that nothing after it can swallow it.
def _nest_then_flood(name: str, length: int) -> str:
    nest = SHAPES["tag-img"](ws.MAX_STRIP_ROUNDS + 5)
    return nest + _adversarial(length - len(nest))[name]


@pytest.mark.parametrize("name", ADVERSARIAL_NAMES)
def test_a_nest_deeper_than_the_cap_before_a_flood_costs_under_the_budget(name, monkeypatch):
    text = _nest_then_flood(name, 20_000)
    assert len(text) == 20_000
    calls = _counting_defang(monkeypatch)
    ws.strip_links_images(text)
    assert len(calls) == 1, "the nest must push this input to the cap, or the budget below is not the worst case"
    monkeypatch.undo()
    p95 = _p95(_samples_ms(text, REPS))
    assert p95 < BUDGET_MS, f"{name}: p95 over {REPS} runs was {p95:.2f} ms, budget {BUDGET_MS} ms"


@pytest.mark.parametrize("name", ADVERSARIAL_NAMES)
def test_a_nest_deeper_than_the_cap_before_a_flood_costs_linearly_with_length(name):
    _assert_scales(_nest_then_flood(name, 20_000), _nest_then_flood(name, 40_000), SCALING_LIMIT, name)


def _shape_of_length(name: str, length: int) -> str:
    per_level = len(SHAPES[name](2)) - len(SHAPES[name](1))
    return SHAPES[name](max(1, length // per_level))


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_a_reassembly_shape_of_20000_characters_costs_under_the_budget(name):
    text = _shape_of_length(name, 20_000)
    assert 19_000 <= len(text) <= 21_000
    p95 = _p95(_samples_ms(text, REPS))
    assert p95 < BUDGET_MS, f"{name} ({len(text):,} chars): p95 over {REPS} runs was {p95:.2f} ms, budget {BUDGET_MS}"


DEPTH_SCALING_LIMIT = 6.0    # t(depth 20,000) / t(depth 5,000): linear is 4, quadratic is 16


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_cost_grows_linearly_with_nesting_depth(name):
    """Depth 5,000 to 20,000 (the tag shapes are then 140,000 to 560,000 characters): four times the input must cost
    about four times as much, not sixteen. The cap is what keeps a deeper nest from costing a round per layer."""
    _assert_scales(SHAPES[name](5_000), SHAPES[name](20_000), DEPTH_SCALING_LIMIT, name, reps=3)
