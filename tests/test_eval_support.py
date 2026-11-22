"""R8 claim support (eval-only): hand-built contexts modelled on the three 2026-09 defects (no stored context exists)."""

import pytest

from semigraph.eval import support
from semigraph.retrieval.answerer import build_blocks, sources_from_context

CH_RISK, CH_TEXT = "0001045810-26-000021:I.1:0321", "0001045810-26-000021:II.7:0002"
XBRL = "xbrl:1045810:revenue:2026-01-25"
RULE = "fr:2026-14132"


def retrieval(*, with_text=True):
    return {
        "edges": [{"source": "Nvidia", "relation": "AFFECTED_BY", "target": "Enhanced Favorable Treatment for the UAE", "status": "Active",
                   "rule_id": "2026-14132", "date": "2026-07-14", "link_method": "keyword", "chunk_ids": []}],
        "metrics": [{"company": "Nvidia", "cik": 1045810, "metric": "revenue", "period_start": "2025-01-27", "period_end": "2026-01-25",
                     "value": 215938000000.0}],
        "risks": [{"company": "Nvidia", "category": "Supply", "summary": "Relies on TSMC for 60% of wafers.", "chunk_id": CH_RISK}],
        "temporal": [], "chunks": [{"chunk_id": CH_TEXT, "text": "Data center grew 80% on strong demand." if with_text else ""}]}


def sources(**kw):
    return sources_from_context(build_blocks(retrieval(**kw))[1])


def test_supported_when_figure_and_company_are_in_the_cited_text():                       # D4 shape
    r = support.claim_support(f"Nvidia revenue was $215.9 billion [{XBRL}].", sources())
    assert r["unsupported"] == 0 and r["supported"] == 1 and r["rate"] == 1.0


def test_a_figure_not_in_the_cited_text_is_unsupported():
    r = support.claim_support(f"Nvidia revenue was $99.9 billion [{XBRL}].", sources())
    assert r["unsupported"] == 1 and r["rate"] == 0.0


def test_a_cited_id_with_no_text_behind_it_is_unsupported():                               # D1 shape
    r = support.claim_support(f"Nvidia relies on a foundry [{CH_TEXT}].", sources(with_text=False))
    assert r["unsupported"] == 1
    r = support.claim_support("Nvidia relies on a foundry [0009999999-26-000001:I.1:0001].", sources())
    assert r["unsupported"] == 1


def test_a_negative_claim_has_no_checkable_claim():                                         # X4 shape
    r = support.claim_support(f"The provisions were not provided in the context [{RULE}].", sources())
    assert r["supported"] == 0 and r["unsupported"] == 0 and r["rate"] is None


def test_a_sentence_without_a_citation_is_not_checked():
    assert support.claim_support("Nvidia revenue was $1 billion.", sources())["rate"] is None


def test_a_company_absent_from_the_cited_text_is_unsupported():
    r = support.claim_support(f"AMD relies on TSMC for 60% of wafers [{CH_RISK}].", sources())
    assert r["unsupported"] == 1
    ok = support.claim_support(f"Nvidia relies on TSMC for 60% of wafers [{CH_RISK}].", sources())
    assert ok["supported"] == 1


def test_an_alias_counts_as_the_company():
    ok = support.claim_support(f"Nvidia Corporation relies on TSMC for 60% of wafers [{CH_RISK}].", sources())
    assert ok["supported"] == 1


def test_rate_over_rows_and_a_row_without_context_raises():
    ctx = build_blocks(retrieval())[1]
    rows = [{"id": "a", "answer": f"Revenue was $215.9 billion [{XBRL}].", "context": ctx},
            {"id": "b", "answer": f"Revenue was $1 billion [{XBRL}].", "context": ctx},
            {"id": "c", "answer": "", "error": "boom"}]
    res = support.claim_support_rate(rows)
    assert res["rate"] == 0.5 and list(res["unsupported_by_row"]) == ["b"]
    with pytest.raises(ValueError, match="no context"):
        support.claim_support_rate([{"id": "d", "answer": "x"}])


def test_an_id_seen_only_on_a_relationships_edge_line_has_no_text():                       # D1 shape, edge-only
    from semigraph.retrieval.context_layout import CONTEXT_HEADERS
    edge_id = "0001045810-26-000021:I.1:0777"
    edge_line = f"- Nvidia SUPPLIED_BY TSMC (status=active) [{edge_id}]"
    ctx = "".join(h + b for h, b in zip(CONTEXT_HEADERS, [edge_line, "(none)", "(none)", "(none)", "(none)", "(none)"], strict=True))
    row = {"id": "e", "answer": f"Nvidia relies on TSMC [{edge_id}].", "context": ctx}
    res = support.claim_support_rate([row])
    assert res["unsupported"] == 1 and res["supported"] == 0


@pytest.mark.parametrize("bullet", ["- **TSMC** [{i}]", "- __TSMC__ [{i}]", "* `TSMC` [{i}]", "1. *TSMC* [{i}]"])
def test_markdown_emphasis_and_list_markers_do_not_hide_a_company_name_d3_shape(bullet):
    i = "0000002488-26-000018:I.1:0024"
    sources = {i: "AMD relies on TSMC to manufacture its products."}
    assert support.claim_support(bullet.format(i=i), sources)["rate"] == 1.0
    assert support.claim_support("- **Intel** [%s]" % i, sources)["unsupported"] == 1       # still checked, not waved through
