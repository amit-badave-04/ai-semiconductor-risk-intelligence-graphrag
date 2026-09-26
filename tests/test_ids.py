"""The citation-id grammar: one module, three id forms (chunk, XBRL fact, Federal Register rule).

Pseudo-citations the audit found in live answers ("[Reported Metrics]", "[<accession> lineage data]") must never
count as citations, and existing chunk ids must keep matching exactly as before.
"""

import re

import pytest

from semigraph.retrieval import ids

CHUNK = "0001045810-26-000021:I.1A:0361"
XBRL = "xbrl:1045810:revenue:2026-01-25"
FR = "fr:2026-19537"
FR_CORRECTION = "fr:C1-2026-16628"


def test_cite_re_finds_every_id_form_in_square_brackets():
    text = f"a [{CHUNK}] b [{XBRL}] c [{FR}] d [{FR_CORRECTION}]"
    assert ids.CITE_RE.findall(text) == [CHUNK, XBRL, FR, FR_CORRECTION]


@pytest.mark.parametrize("text", [
    "[Reported Metrics]",
    "[Dropped Risk Lineages]",
    "[0001045810-26-000021 lineage data]",
    "[xbrl:notacik:revenue:2026-01-25]",
    "[xbrl:1045810:Revenue:2026-01-25]",       # metric names are lower snake case
    "[fr:12]",
    "[0001045810-26-000021:I.1A]",              # no sequence number
])
def test_pseudo_and_malformed_citations_do_not_match(text):
    assert ids.CITE_RE.findall(text) == []


def test_chunk_ids_match_exactly_as_the_original_grammar_did():
    """Backward compatibility: the pre-M1b CITE_RE for chunk ids."""
    legacy = re.compile(r"\[([0-9\-]+:[IVX]+\.[0-9A-Z]+:[0-9]{4})\]")
    sample = f"x [{CHUNK}] [0000002488-26-000021:II.7:0009] [0001628280-26-025362:I.3:0001] [bad:id]"
    assert ids.CITE_RE.findall(sample) == legacy.findall(sample)


@pytest.mark.parametrize("value,kind", [(CHUNK, "chunk"), (XBRL, "xbrl"), (FR, "fr"), (FR_CORRECTION, "fr"),
                                        ("Reported Metrics", None), ("", None), (CHUNK + "x", None)])
def test_classify_id(value, kind):
    assert ids.classify_id(value) == kind


def test_anchored_patterns_validate_route_parameters():
    assert ids.CHUNK_ID_RE.match(CHUNK) and not ids.CHUNK_ID_RE.match(XBRL)
    assert ids.XBRL_ID_RE.match(XBRL) and not ids.XBRL_ID_RE.match(FR)
    assert ids.FR_ID_RE.match(FR) and ids.FR_ID_RE.match(FR_CORRECTION) and not ids.FR_ID_RE.match(CHUNK)
    assert not ids.CHUNK_ID_RE.match("1234:I.1:0001\n../../etc")


def test_xbrl_id_is_the_metric_node_id_with_a_prefix_and_round_trips():
    assert ids.xbrl_id(1045810, "revenue", "2026-01-25") == XBRL
    assert ids.metric_id_of(XBRL) == "1045810:revenue:2026-01-25"
    with pytest.raises(ValueError):
        ids.metric_id_of(CHUNK)


def test_fr_id_wraps_the_document_number_and_round_trips():
    assert ids.fr_id("2026-19537") == FR and ids.rule_id_of(FR) == "2026-19537"
    assert ids.fr_id("C1-2026-16628") == FR_CORRECTION and ids.rule_id_of(FR_CORRECTION) == "C1-2026-16628"
    with pytest.raises(ValueError):
        ids.rule_id_of(XBRL)


def test_the_answerer_and_the_route_use_this_grammar():
    from semigraph.retrieval import answerer
    from semigraph.serve import routes

    assert answerer.CITE_RE is ids.CITE_RE
    assert routes.CHUNK_ID_RE is ids.CHUNK_ID_RE


# --- review LOW: a trailing newline must never pass an anchored id (Python's ``$`` matches before a final "\n") ---

@pytest.mark.parametrize("value", [CHUNK, XBRL, FR, FR_CORRECTION])
@pytest.mark.parametrize("suffix", ["\n", "\r\n", " ", "\n../../etc"])
def test_an_id_followed_by_a_newline_or_anything_else_is_not_an_id(value, suffix):
    assert ids.classify_id(value + suffix) is None
    for pattern in (ids.CHUNK_ID_RE, ids.XBRL_ID_RE, ids.FR_ID_RE):
        assert pattern.match(value + suffix) is None
    with pytest.raises(ValueError):
        ids.metric_id_of(XBRL + suffix)
    with pytest.raises(ValueError):
        ids.rule_id_of(FR + suffix)
