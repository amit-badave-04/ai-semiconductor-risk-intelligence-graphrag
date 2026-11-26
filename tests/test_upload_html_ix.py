"""I4a / R14: the HTML parser drops only ``ix:header`` and hidden elements and keeps visible inline-XBRL text."""

from __future__ import annotations

import time

from semigraph.uploads import parse_worker as pw

IX_FILING = """<html><body>
<div style="display:none"><ix:header><ix:hidden><ix:nonNumeric name="dei:Hidden">HIDDEN-FACT-TEXT</ix:nonNumeric></ix:hidden>
<ix:resources><xbrli:context id="c1"><xbrli:identifier>0001045810</xbrli:identifier></xbrli:context></ix:resources></ix:header></div>
<h2>Item 1A. Risk Factors</h2>
<p><ix:nonNumeric name="us-gaap:RiskText" contextRef="c1">Supply of advanced packaging is constrained.</ix:nonNumeric></p>
<p>Revenue was <ix:nonFraction name="us-gaap:Revenues" contextRef="c1" unitRef="usd">26,974</ix:nonFraction> million.</p>
<p hidden>HIDDEN-ATTR-TEXT</p>
<div style="color:red; display: none ;"><p>HIDDEN-STYLE-TEXT</p></div>
<p>Visible after the hidden blocks.</p>
</body></html>"""


def _texts(html: str) -> str:
    return "\n".join(b["text"] for b in pw._BlockHTMLParser().parse(html))


def test_ix_header_stripped_visible_ix_text_kept():
    text = _texts(IX_FILING)
    assert "HIDDEN-FACT-TEXT" not in text and "0001045810" not in text
    assert "Supply of advanced packaging is constrained." in text
    assert "Revenue was 26,974 million." in text


def test_hidden_elements_are_stripped_and_following_text_is_kept():
    text = _texts(IX_FILING)
    assert "HIDDEN-ATTR-TEXT" not in text and "HIDDEN-STYLE-TEXT" not in text
    assert "Visible after the hidden blocks." in text


def test_ix_header_without_a_hidden_wrapper_is_still_stripped():
    html = "<body><ix:header><ix:references>SCHEMA-REF</ix:references></ix:header><p>Body text.</p></body>"
    assert _texts(html) == "Body text."


def test_void_elements_inside_a_hidden_block_do_not_unbalance_the_stack():
    html = '<div style="display:none">a<br>b<img src="x">c</div><p>kept</p>'
    assert _texts(html) == "kept"


def test_stray_end_tags_never_unhide_or_crash():
    html = '</span></div><div hidden>gone</div></div></p><p>kept</p>'
    assert _texts(html) == "kept"


def test_deep_and_mismatched_nesting_is_linear():
    html = "<div>" * 50_000 + "<p>text</p>" + "</span>" * 50_000
    started = time.perf_counter()
    pw._BlockHTMLParser().parse(html)
    assert time.perf_counter() - started < 3.0


def test_s07_hidden_text_past_the_open_element_cap_stays_hidden():
    """S-07: more than 10,000 nested elements must not make a hidden element's text leak into the extraction."""
    html = "<div>" * (pw._HTML_MAX_OPEN_ELEMENTS + 50) + '<div style="display:none">HIDDEN-DEEP-TEXT</div><p>visible-deep</p>'
    text = _texts(html)
    assert "HIDDEN-DEEP-TEXT" not in text and "visible-deep" in text


def test_s07_a_hidden_element_opened_past_the_cap_ends_with_its_end_tag():
    html = ("<div>" * (pw._HTML_MAX_OPEN_ELEMENTS + 5) + '<section hidden><p>SECRET-DEEP</p></section><p>after</p>')
    text = _texts(html)
    assert "SECRET-DEEP" not in text and "after" in text
