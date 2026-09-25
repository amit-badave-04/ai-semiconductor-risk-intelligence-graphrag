"""Answer-context tests — C2.1 (units), C2.5 (anchor_defaulted), C2.6 (byte-parity).

The GOLDEN_* literals below were captured by running the v1 (pre-C2) ``build_blocks``
and ``ANSWER_PROMPT.format`` (git HEAD) on ``golden_retrieval()`` BEFORE answerer.py was
edited for C2 — they were pasted from that run, not written by hand. They are the only
honest proof that the prompt-visible context for existing data is byte-identical to the
benchmarked v1 wording; the sole permitted difference is the unit label of non-USD metrics.

No Neo4j, no network, no real LLM.
"""

import copy

import pytest

import semigraph.retrieval.answerer as answerer_mod
from semigraph.retrieval import (
    ANSWER_PROMPT,
    answer,
    answer_stream,
    build_blocks,
    format_metric_line,
)

CID_A = "0001045810-26-000021:I.1:0320"
CID_B = "0001045810-26-000021:I.1A:0345"
CID_C = "0001045810-24-000029:I.1A:0152"
CID_D = "0001046179-26-000007:I.1A:0011"
QUESTION = "How does Nvidia depend on TSMC?"


def golden_retrieval() -> dict:
    """Fixture: one row of every shape build_blocks prints. Metric rows deliberately cover
    a missing ``unit`` key, ``unit='USD'`` and ``unit=None`` (all must render as v1: USD)."""
    return {
        "anchors": {"Nvidia": 1045810},
        "edges": [
            {"source": "Nvidia", "relation": "DEPENDS_ON", "target": "TSMC", "status": "Active",
             "quote": "We utilize foundries", "chunk_ids": [CID_A, CID_B, CID_C, CID_D]},
            {"source": "Nvidia", "relation": "COMPETES_WITH", "target": "AMD", "status": "Active",
             "quote": None, "chunk_ids": None},
            {"source": "Nvidia", "relation": "AFFECTED_BY",
             "target": "Implementation of Additional Export Controls: Certain Advanced Computing Items",
             "status": "Active", "quote": None, "chunk_ids": [CID_B]},
        ],
        "metrics": [
            {"company": "Nvidia", "metric": "revenue", "value": 130497000000.0,
             "period_start": "2024-01-29", "period_end": "2025-01-26"},
            {"company": "Nvidia", "metric": "rnd", "value": 12914000000.0, "unit": "USD",
             "period_start": "2024-01-29", "period_end": "2025-01-26"},
            {"company": "AMD", "metric": "net_income", "value": 1641000000.4,
             "period_start": "2024-01-01", "period_end": "2024-12-28", "unit": None},
        ],
        "risks": [
            {"company": "Nvidia", "category": "regulatory", "chunk_id": CID_B, "score": 0.91,
             "summary": "Export controls could restrict sales of our data-center products to China."},
            {"company": "TSMC", "category": "supply_chain", "chunk_id": CID_D, "score": 0.83,
             "summary": "Geographic concentration of manufacturing in Taiwan."},
        ],
        "temporal": [
            {"company": "Nvidia", "lineage": "1045810:3", "first_seen": "2023-02-24",
             "last_seen": "2025-02-26",
             "example": "COVID-related supply disruption risk that was disclosed in earlier annual "
                        "reports and dropped from the latest one, with a long tail of detail here."},
        ],
        "chunks": [
            {"chunk_id": CID_C, "score": 0.88,
             "text": "Export controls affect our China sales.\nSecond line.",
             "source_url": "https://www.sec.gov/x"},
            {"chunk_id": CID_A, "score": 0.80, "text": "We rely on third-party foundries.",
             "source_url": "https://www.sec.gov/y"},
        ],
    }


# --- v1 goldens (captured from the pre-C2 code; do not regenerate from the current code) ---

GOLDEN_BLOCKS = (
    (
        '- Nvidia DEPENDS_ON TSMC (status=Active) [0001045810-26-000021:I.1:0320] [0001045810-26-000021:I.1A:0345] [0001045810-24-000029:I.1A:0152]\n'
        '- Nvidia COMPETES_WITH AMD (status=Active) \n'
        '- Nvidia AFFECTED_BY Implementation of Additional Export Controls: Certain Advanced Computing Items (status=Active) [0001045810-26-000021:I.1A:0345]'
    ),
    (
        '- Nvidia revenue for period 2024-01-29..2025-01-26: 130,497,000,000 USD\n'
        '- Nvidia rnd for period 2024-01-29..2025-01-26: 12,914,000,000 USD\n'
        '- AMD net_income for period 2024-01-01..2024-12-28: 1,641,000,000 USD'
    ),
    (
        '- Nvidia (regulatory): Export controls could restrict sales of our data-center products to China. [0001045810-26-000021:I.1A:0345]\n'
        '- TSMC (supply_chain): Geographic concentration of manufacturing in Taiwan. [0001046179-26-000007:I.1A:0011]'
    ),
    (
        '- Nvidia: disclosed 2023-02-24 through 2025-02-26, then dropped — e.g. COVID-related supply disruption risk that was disclosed in earlier annual reports and dropped from the latest one, with '
    ),
    (
        '[0001045810-24-000029:I.1A:0152]\n'
        'Export controls affect our China sales.\n'
        'Second line.\n'
        '\n'
        '[0001045810-26-000021:I.1:0320]\n'
        'We rely on third-party foundries.\n'
        ''
    ),
)

GOLDEN_CONTEXT = (
    'RELATIONSHIPS:\n'
    '- Nvidia DEPENDS_ON TSMC (status=Active) [0001045810-26-000021:I.1:0320] [0001045810-26-000021:I.1A:0345] [0001045810-24-000029:I.1A:0152]\n'
    '- Nvidia COMPETES_WITH AMD (status=Active) \n'
    '- Nvidia AFFECTED_BY Implementation of Additional Export Controls: Certain Advanced Computing Items (status=Active) [0001045810-26-000021:I.1A:0345]\n'
    '\n'
    'METRICS:\n'
    '- Nvidia revenue for period 2024-01-29..2025-01-26: 130,497,000,000 USD\n'
    '- Nvidia rnd for period 2024-01-29..2025-01-26: 12,914,000,000 USD\n'
    '- AMD net_income for period 2024-01-01..2024-12-28: 1,641,000,000 USD\n'
    '\n'
    'ACTIVE RISKS:\n'
    '- Nvidia (regulatory): Export controls could restrict sales of our data-center products to China. [0001045810-26-000021:I.1A:0345]\n'
    '- TSMC (supply_chain): Geographic concentration of manufacturing in Taiwan. [0001046179-26-000007:I.1A:0011]\n'
    '\n'
    'DROPPED RISK LINEAGES:\n'
    '- Nvidia: disclosed 2023-02-24 through 2025-02-26, then dropped — e.g. COVID-related supply disruption risk that was disclosed in earlier annual reports and dropped from the latest one, with \n'
    '\n'
    'EXCERPTS:\n'
    '[0001045810-24-000029:I.1A:0152]\n'
    'Export controls affect our China sales.\n'
    'Second line.\n'
    '\n'
    '[0001045810-26-000021:I.1:0320]\n'
    'We rely on third-party foundries.\n'
    ''
)

GOLDEN_VALID_IDS = ['0001045810-24-000029:I.1A:0152', '0001045810-26-000021:I.1:0320', '0001045810-26-000021:I.1A:0345', '0001046179-26-000007:I.1A:0011']

GOLDEN_PROMPT = (
    'You are a semiconductor supply-chain analyst. Answer the question using ONLY the context below,\n'
    'retrieved from SEC filings via a knowledge graph.\n'
    '\n'
    'Rules:\n'
    '- Cite evidence after every factual sentence using [chunk_id] (ids appear in the context).\n'
    '- KNOWN RELATIONSHIPS, REPORTED METRICS and DROPPED RISK LINEAGES come from the knowledge graph.\n'
    '- If the context does not contain the answer, say so plainly — never fill gaps from memory.\n'
    '- Be concise. Use bullet lists for enumerations.\n'
    '\n'
    'QUESTION: How does Nvidia depend on TSMC?\n'
    '\n'
    '=== KNOWN RELATIONSHIPS ===\n'
    '- Nvidia DEPENDS_ON TSMC (status=Active) [0001045810-26-000021:I.1:0320] [0001045810-26-000021:I.1A:0345] [0001045810-24-000029:I.1A:0152]\n'
    '- Nvidia COMPETES_WITH AMD (status=Active) \n'
    '- Nvidia AFFECTED_BY Implementation of Additional Export Controls: Certain Advanced Computing Items (status=Active) [0001045810-26-000021:I.1A:0345]\n'
    '\n'
    '=== REPORTED METRICS (deterministic, from XBRL) ===\n'
    '- Nvidia revenue for period 2024-01-29..2025-01-26: 130,497,000,000 USD\n'
    '- Nvidia rnd for period 2024-01-29..2025-01-26: 12,914,000,000 USD\n'
    '- AMD net_income for period 2024-01-01..2024-12-28: 1,641,000,000 USD\n'
    '\n'
    '=== DISCLOSED RISKS (currently active, semantically ranked) ===\n'
    '- Nvidia (regulatory): Export controls could restrict sales of our data-center products to China. [0001045810-26-000021:I.1A:0345]\n'
    '- TSMC (supply_chain): Geographic concentration of manufacturing in Taiwan. [0001046179-26-000007:I.1A:0011]\n'
    '\n'
    '=== RISK LINEAGES DROPPED FROM THE LATEST ANNUAL REPORT (bitemporal layer) ===\n'
    '- Nvidia: disclosed 2023-02-24 through 2025-02-26, then dropped — e.g. COVID-related supply disruption risk that was disclosed in earlier annual reports and dropped from the latest one, with \n'
    '\n'
    '=== SOURCE EXCERPTS ===\n'
    '[0001045810-24-000029:I.1A:0152]\n'
    'Export controls affect our China sales.\n'
    'Second line.\n'
    '\n'
    '[0001045810-26-000021:I.1:0320]\n'
    'We rely on third-party foundries.\n'
    '\n'
    ''
)



def render_prompt(blocks: tuple[str, str, str, str, str]) -> str:
    e_b, m_b, k_b, t_b, c_b = blocks
    return ANSWER_PROMPT.format(question=QUESTION, edges_block=e_b, metrics_block=m_b,
                                risks_block=k_b, temporal_block=t_b, chunks_block=c_b)


# --- C2.6: byte-parity with v1 for existing (USD / unit-less) data ---

def test_blocks_match_v1_golden_byte_for_byte():
    blocks, _, _ = build_blocks(golden_retrieval())
    assert blocks == GOLDEN_BLOCKS


def test_full_context_matches_v1_golden_byte_for_byte():
    _, full_context, valid_ids = build_blocks(golden_retrieval())
    assert full_context == GOLDEN_CONTEXT
    assert sorted(valid_ids) == GOLDEN_VALID_IDS


def test_prompt_matches_v1_golden_byte_for_byte():
    blocks, _, _ = build_blocks(golden_retrieval())
    assert render_prompt(blocks) == GOLDEN_PROMPT


def test_usd_metric_lines_render_identically_with_or_without_unit_key():
    with_unit = golden_retrieval()
    without_unit = copy.deepcopy(with_unit)
    for m in with_unit["metrics"]:
        m["unit"] = "USD"
    for m in without_unit["metrics"]:
        m.pop("unit", None)
    assert build_blocks(with_unit)[1] == build_blocks(without_unit)[1] == GOLDEN_CONTEXT


# --- C2.1: non-USD metrics are labelled with their own unit (the only allowed change) ---

def test_non_usd_units_change_only_the_unit_token():
    r = golden_retrieval()
    r["metrics"][0]["unit"] = "TWD"      # Nvidia revenue row, relabelled
    r["metrics"][2]["unit"] = "EUR"      # AMD net income row, relabelled
    _, full_context, _ = build_blocks(r)
    expected = (GOLDEN_CONTEXT
                .replace("2025-01-26: 130,497,000,000 USD", "2025-01-26: 130,497,000,000 TWD")
                .replace("2024-12-28: 1,641,000,000 USD", "2024-12-28: 1,641,000,000 EUR"))
    assert expected != GOLDEN_CONTEXT
    assert full_context == expected
    assert "TWD" in render_prompt(build_blocks(r)[0])


def test_non_usd_line_never_says_usd():
    r = golden_retrieval()
    r["metrics"] = [{"company": "TSMC", "metric": "revenue", "value": 3809054000000.0,
                     "unit": "TWD", "period_start": "2024-01-01", "period_end": "2024-12-31"}]
    (_, m_block, *_rest), _, _ = build_blocks(r)
    assert m_block == "- TSMC revenue for period 2024-01-01..2024-12-31: 3,809,054,000,000 TWD"
    assert "USD" not in m_block


@pytest.mark.parametrize("metric, expected", [
    ({"company": "Nvidia", "metric": "revenue", "value": 60922000000.0,
      "period_start": "2023-01-30", "period_end": "2024-01-28"},
     "- Nvidia revenue for period 2023-01-30..2024-01-28: 60,922,000,000 USD"),
    ({"company": "Nvidia", "metric": "revenue", "value": 60922000000.0, "unit": "USD",
      "period_start": "2023-01-30", "period_end": "2024-01-28"},
     "- Nvidia revenue for period 2023-01-30..2024-01-28: 60,922,000,000 USD"),
    ({"company": "Nvidia", "metric": "revenue", "value": 60922000000.0, "unit": None,
      "period_start": "2023-01-30", "period_end": "2024-01-28"},
     "- Nvidia revenue for period 2023-01-30..2024-01-28: 60,922,000,000 USD"),
    ({"company": "Nvidia", "metric": "revenue", "value": 60922000000.0, "unit": "",
      "period_start": "2023-01-30", "period_end": "2024-01-28"},
     "- Nvidia revenue for period 2023-01-30..2024-01-28: 60,922,000,000 USD"),
    ({"company": "TSMC", "metric": "capex", "value": 1234567.6, "unit": "TWD",
      "period_start": "2024-01-01", "period_end": "2024-12-31"},
     "- TSMC capex for period 2024-01-01..2024-12-31: 1,234,568 TWD"),
    ({"company": "ASML", "metric": "rnd", "value": 4304000000.0, "unit": "EUR",
      "period_start": "2024-01-01", "period_end": "2024-12-31"},
     "- ASML rnd for period 2024-01-01..2024-12-31: 4,304,000,000 EUR"),
])
def test_format_metric_line(metric, expected):
    assert format_metric_line(metric) == expected


# --- C2.5: anchor honesty — the retrieval event carries anchor_defaulted (additive) ---

def _stream_events(monkeypatch, retrieval: dict) -> list[dict]:
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve", lambda *a, **kw: retrieval)
    return list(answer_stream("q", None, None, llm_stream=lambda p: iter(["ok"])))


def test_retrieval_event_reports_anchor_defaulted_true(monkeypatch):
    r = golden_retrieval()
    r["anchors"], r["anchor_defaulted"] = {}, True
    ev = _stream_events(monkeypatch, r)[0]
    assert ev["event"] == "retrieval"
    assert ev["anchor_defaulted"] is True
    assert ev["anchors"] == {}


def test_retrieval_event_reports_anchor_defaulted_false(monkeypatch):
    r = golden_retrieval()
    r["anchor_defaulted"] = False
    ev = _stream_events(monkeypatch, r)[0]
    assert ev["anchor_defaulted"] is False
    assert ev["anchors"] == {"Nvidia": 1045810}


def test_retrieval_event_defaults_anchor_defaulted_false_when_key_absent(monkeypatch):
    """Older/mocked retrievers (and vector_retrieve) do not carry the key."""
    ev = _stream_events(monkeypatch, golden_retrieval())[0]
    assert ev["anchor_defaulted"] is False


def test_retrieval_event_counts_are_unchanged(monkeypatch):
    r = golden_retrieval()
    r["anchor_defaulted"] = True
    ev = _stream_events(monkeypatch, r)[0]
    assert ev["counts"] == {"edges": 3, "metrics": 3, "risks": 2, "temporal": 1, "chunks": 2}
    assert set(ev) == {"event", "anchors", "counts", "anchor_defaulted"}


def test_answer_result_carries_anchor_defaulted_in_raw_retrieval(monkeypatch):
    r = golden_retrieval()
    r["anchors"], r["anchor_defaulted"] = {}, True
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve", lambda *a, **kw: r)
    out = answer(QUESTION, None, None, llm=lambda p: "No context.")
    assert out["retrieval"]["anchor_defaulted"] is True
    assert out["context"] == GOLDEN_CONTEXT
