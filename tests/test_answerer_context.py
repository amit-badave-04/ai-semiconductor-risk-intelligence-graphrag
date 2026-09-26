"""Answer-context tests - the M1b goldens, units (C2.1), anchor_defaulted (C2.5).

PROVENANCE OF THE GOLDENS (changed deliberately in M1b; the v1 note said "do not regenerate from the current code"):

- ``GOLDEN_EDGES`` (the two company relations), ``GOLDEN_RISKS`` and ``GOLDEN_CHUNKS`` are the v1 literals, captured from
  the pre-C2 code and unchanged: those blocks must stay byte-identical to what the benchmark was run against.
- ``GOLDEN_EXTERNAL``, ``GOLDEN_METRICS``, ``GOLDEN_TEMPORAL``, ``GOLDEN_CONTEXT`` and the header names are NEW in M1b.
  They were written BY HAND from ``docs/v2/M1B_PLAN.md`` section D and the live-test audit BEFORE the implementation
  existed (percentages and differences computed independently with plain arithmetic), never pasted from a run.
  What changed and why: the AFFECTED_BY row moved out of RELATIONSHIPS into its own EXTERNAL REGULATORY EVENTS block
  (it is a Federal Register rule linked by keyword, not the company's disclosure); METRICS is grouped by fiscal year with
  a citable ``xbrl:`` id per line and code-computed year-over-year; the DROPPED RISK LINEAGES block became the text-verified
  RISK FACTORS REMOVED / ADDED / REWORDED block; the answer prompt got new rules and a sixth placeholder.

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
)
from semigraph.retrieval.answerer import CONTEXT_HEADERS, LEGACY_CONTEXT_HEADERS, _metric_amount, render_prompt

CID_A = "0001045810-26-000021:I.1:0320"
CID_B = "0001045810-26-000021:I.1A:0345"
CID_C = "0001045810-24-000029:I.1A:0152"
CID_D = "0001046179-26-000007:I.1A:0011"
OLD_ACC, NEW_ACC = "0001045810-25-000023", "0001045810-26-000021"
QUESTION = "How does Nvidia depend on TSMC?"


def golden_retrieval() -> dict:
    """Fixture: one row of every shape build_blocks prints. Metric rows cover 4 Nvidia fiscal years (the 4th is only the
    base of the 3rd's year-over-year), a missing ``unit`` key/None (USD), a negative prior (n/m) and a TWD filer."""
    def m(company, cik, metric, value, start, end, **kw):
        return {"company": company, "cik": cik, "metric": metric, "value": value,
                "period_start": start, "period_end": end, **kw}

    return {
        "anchors": {"Nvidia": 1045810},
        "edges": [
            {"source": "Nvidia", "relation": "DEPENDS_ON", "target": "TSMC", "status": "Active",
             "quote": "We utilize foundries", "chunk_ids": [CID_A, CID_B, CID_C, CID_D]},
            {"source": "Nvidia", "relation": "COMPETES_WITH", "target": "AMD", "status": "Active",
             "quote": None, "chunk_ids": None},
            {"source": "Nvidia", "relation": "AFFECTED_BY",
             "target": "Implementation of Additional Export Controls: Certain Advanced Computing Items",
             "status": "Active", "quote": None, "chunk_ids": [CID_B], "rule_id": "2026-19537",
             "date": "2026-03-12", "url": "https://www.federalregister.gov/d/2026-19537", "kind": "entity_list",
             "link_source": "federal_register", "link_method": "keyword", "external": True},
        ],
        "metrics": [
            m("Nvidia", 1045810, "revenue", 215938000000.0, "2025-01-27", "2026-01-25"),
            m("Nvidia", 1045810, "revenue", 130497000000.0, "2024-01-29", "2025-01-26"),
            m("Nvidia", 1045810, "revenue", 60922000000.0, "2023-01-30", "2024-01-28"),
            m("Nvidia", 1045810, "revenue", 26974000000.0, "2022-01-31", "2023-01-29"),
            m("Nvidia", 1045810, "rnd", 18497000000.0, "2025-01-27", "2026-01-25", unit="USD"),
            m("Nvidia", 1045810, "rnd", 12914000000.0, "2024-01-29", "2025-01-26", unit="USD"),
            m("AMD", 2488, "net_income", 1641000000.4, "2024-01-01", "2024-12-28", unit=None),
            m("Intel", 50863, "net_income", -267000000.0, "2024-12-29", "2025-12-27"),
            m("Intel", 50863, "net_income", -18756000000.0, "2023-12-31", "2024-12-28"),
            m("TSMC", 1046179, "revenue", 2894308000000.0, "2024-01-01", "2024-12-31", unit="TWD"),
            m("TSMC", 1046179, "revenue", 2161736000000.0, "2023-01-01", "2023-12-31", unit="TWD"),
        ],
        "risks": [
            {"company": "Nvidia", "category": "regulatory", "chunk_id": CID_B, "score": 0.91,
             "summary": "Export controls could restrict sales of our data-center products to China."},
            {"company": "TSMC", "category": "supply_chain", "chunk_id": CID_D, "score": 0.83,
             "summary": "Geographic concentration of manufacturing in Taiwan."},
        ],
        "temporal": [
            {"company": "Nvidia", "cik": 1045810, "change": "removed", "lineage": "1045810:3", "item_id": "i1",
             "headline": "We may not be able to sell to China without an export license", "older_headline": None,
             "unit_kind": "headline", "section_id": "I.1A", "seq": 3, "decided_by": None,
             "older_chunk_ids": [f"{OLD_ACC}:I.1A:0210", f"{OLD_ACC}:I.1A:0211"], "newer_chunk_ids": []},
            {"company": "Nvidia", "cik": 1045810, "change": "removed", "lineage": "1045810:4", "item_id": "i2",
             "headline": "Our Hong Kong operations may face transition risks", "older_headline": None,
             "unit_kind": "headline", "section_id": "I.1A", "seq": 4, "decided_by": None,
             "older_chunk_ids": [f"{OLD_ACC}:I.1A:0230"], "newer_chunk_ids": []},
            {"company": "Nvidia", "cik": 1045810, "change": "new", "lineage": "1045810:40", "item_id": "i3",
             "headline": "We depend on a small number of customers for a large share of revenue",
             "older_headline": None, "unit_kind": "headline", "section_id": "I.1A", "seq": 40, "decided_by": None,
             "older_chunk_ids": [], "newer_chunk_ids": [f"{NEW_ACC}:I.1A:0350"]},
            {"company": "Nvidia", "cik": 1045810, "change": "reworded", "lineage": "1045810:9", "item_id": "i4",
             "headline": "Acquisitions and strategic investments may not deliver expected benefits",
             "older_headline": "We may not realize the benefits of acquisitions", "unit_kind": "headline",
             "section_id": "I.1A", "seq": 9, "decided_by": "luna",
             "older_chunk_ids": [f"{OLD_ACC}:I.1A:0140"], "newer_chunk_ids": [f"{NEW_ACC}:I.1A:0347"]},
        ],
        "temporal_pairs": [
            {"company": "Nvidia", "cik": 1045810, "older_accession": OLD_ACC, "older_form": "10-K",
             "older_date": "2025-02-26", "newer_accession": NEW_ACC, "newer_form": "10-K", "newer_date": "2026-02-25",
             "totals": {"removed": 21, "new": 12, "reworded": 9}},
        ],
        "chunks": [
            {"chunk_id": CID_C, "score": 0.88,
             "text": "Export controls affect our China sales.\nSecond line.",
             "source_url": "https://www.sec.gov/x"},
            {"chunk_id": CID_A, "score": 0.80, "text": "We rely on third-party foundries.",
             "source_url": "https://www.sec.gov/y"},
        ],
    }


# --- the goldens (see the module docstring for their provenance) ---

GOLDEN_EDGES = (
    '- Nvidia DEPENDS_ON TSMC (status=Active) [0001045810-26-000021:I.1:0320] [0001045810-26-000021:I.1A:0345] [0001045810-24-000029:I.1A:0152]\n'
    '- Nvidia COMPETES_WITH AMD (status=Active) '
)

GOLDEN_EXTERNAL = (
    '- 2026-03-12 [fr:2026-19537] Implementation of Additional Export Controls: Certain Advanced Computing Items '
    '(linked to Nvidia by keyword match)'
)

GOLDEN_METRICS = (
    'Nvidia: fiscal year ended 2026-01-25\n'
    '- revenue for period 2025-01-27..2026-01-25: 215,938,000,000 USD [xbrl:1045810:revenue:2026-01-25] | computed: +65.5% vs fiscal year ended 2025-01-26 (change +85,441,000,000 USD)\n'
    '- rnd for period 2025-01-27..2026-01-25: 18,497,000,000 USD [xbrl:1045810:rnd:2026-01-25] | computed: +43.2% vs fiscal year ended 2025-01-26 (change +5,583,000,000 USD)\n'
    'Nvidia: fiscal year ended 2025-01-26\n'
    '- revenue for period 2024-01-29..2025-01-26: 130,497,000,000 USD [xbrl:1045810:revenue:2025-01-26] | computed: +114.2% vs fiscal year ended 2024-01-28 (change +69,575,000,000 USD)\n'
    '- rnd for period 2024-01-29..2025-01-26: 12,914,000,000 USD [xbrl:1045810:rnd:2025-01-26]\n'
    'Nvidia: fiscal year ended 2024-01-28\n'
    '- revenue for period 2023-01-30..2024-01-28: 60,922,000,000 USD [xbrl:1045810:revenue:2024-01-28] | computed: +125.9% vs fiscal year ended 2023-01-29 (change +33,948,000,000 USD)\n'
    'AMD: fiscal year ended 2024-12-28\n'
    '- net_income for period 2024-01-01..2024-12-28: 1,641,000,000 USD [xbrl:2488:net_income:2024-12-28]\n'
    'Intel: fiscal year ended 2025-12-27\n'
    '- net_income for period 2024-12-29..2025-12-27: -267,000,000 USD [xbrl:50863:net_income:2025-12-27] | computed: n/m vs fiscal year ended 2024-12-28 (change +18,489,000,000 USD; percentage not meaningful, prior value not positive)\n'
    'Intel: fiscal year ended 2024-12-28\n'
    '- net_income for period 2023-12-31..2024-12-28: -18,756,000,000 USD [xbrl:50863:net_income:2024-12-28]\n'
    'TSMC: fiscal year ended 2024-12-31\n'
    '- revenue for period 2024-01-01..2024-12-31: 2,894,308,000,000 TWD [xbrl:1046179:revenue:2024-12-31] | computed: +33.9% vs fiscal year ended 2023-12-31 (change +732,572,000,000 TWD)\n'
    'TSMC: fiscal year ended 2023-12-31\n'
    '- revenue for period 2023-01-01..2023-12-31: 2,161,736,000,000 TWD [xbrl:1046179:revenue:2023-12-31]'
)

GOLDEN_RISKS = (
    '- Nvidia (regulatory): Export controls could restrict sales of our data-center products to China. [0001045810-26-000021:I.1A:0345]\n'
    '- TSMC (supply_chain): Geographic concentration of manufacturing in Taiwan. [0001046179-26-000007:I.1A:0011]'
)

GOLDEN_TEMPORAL = (
    'Nvidia: 10-K filed 2025-02-26 (accession 0001045810-25-000023) compared with 10-K filed 2026-02-25 (accession 0001045810-26-000021)\n'
    'Removed - showing 2 of 21 risk factors (text verified absent from the later filing):\n'
    '- "We may not be able to sell to China without an export license" [0001045810-25-000023:I.1A:0210] [0001045810-25-000023:I.1A:0211]\n'
    '- "Our Hong Kong operations may face transition risks" [0001045810-25-000023:I.1A:0230]\n'
    'Added - showing 1 of 12 risk factors (new in the later filing):\n'
    '- "We depend on a small number of customers for a large share of revenue" [0001045810-26-000021:I.1A:0350]\n'
    'Reworded - showing 1 of 9 risk factors (still disclosed, wording changed):\n'
    '- "Acquisitions and strategic investments may not deliver expected benefits" (earlier wording: "We may not realize the benefits of acquisitions"; decided by luna) earlier [0001045810-25-000023:I.1A:0140] later [0001045810-26-000021:I.1A:0347]'
)

GOLDEN_CHUNKS = (
    '[0001045810-24-000029:I.1A:0152]\n'
    'Export controls affect our China sales.\n'
    'Second line.\n'
    '\n'
    '[0001045810-26-000021:I.1:0320]\n'
    'We rely on third-party foundries.\n'
    ''
)

GOLDEN_CONTEXT = (
    'RELATIONSHIPS:\n' + GOLDEN_EDGES + '\n\n'
    "EXTERNAL REGULATORY EVENTS (Federal Register rules linked by keyword; not the company's disclosure):\n"
    + GOLDEN_EXTERNAL + '\n\n'
    'METRICS:\n' + GOLDEN_METRICS + '\n\n'
    'ACTIVE RISKS:\n' + GOLDEN_RISKS + '\n\n'
    'RISK FACTORS REMOVED / ADDED / REWORDED between annual filings (text-verified):\n' + GOLDEN_TEMPORAL + '\n\n'
    'EXCERPTS:\n' + GOLDEN_CHUNKS
)

GOLDEN_VALID_IDS = sorted([
    CID_C, CID_A, CID_B, CID_D,
    "xbrl:1045810:revenue:2026-01-25", "xbrl:1045810:rnd:2026-01-25", "xbrl:1045810:revenue:2025-01-26",
    "xbrl:1045810:rnd:2025-01-26", "xbrl:1045810:revenue:2024-01-28", "xbrl:2488:net_income:2024-12-28",
    "xbrl:50863:net_income:2025-12-27", "xbrl:50863:net_income:2024-12-28",
    "xbrl:1046179:revenue:2024-12-31", "xbrl:1046179:revenue:2023-12-31",
    "fr:2026-19537",
    f"{OLD_ACC}:I.1A:0210", f"{OLD_ACC}:I.1A:0211", f"{OLD_ACC}:I.1A:0230", f"{NEW_ACC}:I.1A:0350",
    f"{OLD_ACC}:I.1A:0140", f"{NEW_ACC}:I.1A:0347",
])


# --- the shared template: ONE definition of the headers (eval/bakeoff inverts it) ---

def test_context_headers_are_the_single_definition_of_the_template():
    assert CONTEXT_HEADERS == (
        "RELATIONSHIPS:\n",
        "\n\nEXTERNAL REGULATORY EVENTS (Federal Register rules linked by keyword; not the company's disclosure):\n",
        "\n\nMETRICS:\n",
        "\n\nACTIVE RISKS:\n",
        "\n\nRISK FACTORS REMOVED / ADDED / REWORDED between annual filings (text-verified):\n",
        "\n\nEXCERPTS:\n",
    )
    assert LEGACY_CONTEXT_HEADERS == ("RELATIONSHIPS:\n", "\n\nMETRICS:\n", "\n\nACTIVE RISKS:\n",
                                      "\n\nDROPPED RISK LINEAGES:\n", "\n\nEXCERPTS:\n")
    blocks, full_context, _ = build_blocks(golden_retrieval())
    rebuilt = CONTEXT_HEADERS[0] + blocks[0]
    for header, block in zip(CONTEXT_HEADERS[1:], blocks[1:], strict=True):
        rebuilt += header + block
    assert rebuilt == full_context                      # the template IS the headers, joined with the blocks


# --- the goldens ---

def test_blocks_match_the_goldens_byte_for_byte():
    blocks, _, _ = build_blocks(golden_retrieval())
    assert tuple(blocks) == (GOLDEN_EDGES, GOLDEN_EXTERNAL, GOLDEN_METRICS, GOLDEN_RISKS, GOLDEN_TEMPORAL,
                             GOLDEN_CHUNKS)
    assert (blocks.edges_block, blocks.external_block, blocks.metrics_block, blocks.risks_block,
            blocks.temporal_block, blocks.chunks_block) == tuple(blocks)


def test_full_context_matches_the_golden_byte_for_byte():
    _, full_context, valid_ids = build_blocks(golden_retrieval())
    assert full_context == GOLDEN_CONTEXT
    assert sorted(valid_ids) == GOLDEN_VALID_IDS


def test_the_unchanged_v1_blocks_are_still_byte_identical_to_v1():
    """The relations (minus the moved AFFECTED_BY row), the risks and the excerpts are what the benchmark ran on."""
    blocks, _, _ = build_blocks(golden_retrieval())
    assert blocks.edges_block == GOLDEN_EDGES and blocks.risks_block == GOLDEN_RISKS
    assert blocks.chunks_block == GOLDEN_CHUNKS


def test_the_prompt_places_each_block_under_its_own_heading_in_order():
    blocks, _, _ = build_blocks(golden_retrieval())
    prompt = render_prompt(QUESTION, blocks)
    assert prompt == ANSWER_PROMPT.format(question=QUESTION, **blocks._asdict())
    positions = [prompt.index(h) for h in (
        "=== KNOWN RELATIONSHIPS ===", "=== EXTERNAL REGULATORY EVENTS", "=== REPORTED METRICS",
        "=== DISCLOSED RISKS", "=== RISK FACTORS REMOVED / ADDED / REWORDED", "=== SOURCE EXCERPTS ===")]
    assert positions == sorted(positions)
    for block in (GOLDEN_EDGES, GOLDEN_EXTERNAL, GOLDEN_METRICS, GOLDEN_RISKS, GOLDEN_TEMPORAL, GOLDEN_CHUNKS):
        assert block in prompt
    assert f"QUESTION: {QUESTION}" in prompt


def test_usd_metric_lines_render_identically_with_or_without_unit_key():
    with_unit = golden_retrieval()
    without_unit = copy.deepcopy(with_unit)
    for row in with_unit["metrics"]:
        if row["company"] != "TSMC":
            row["unit"] = "USD"
    for row in without_unit["metrics"]:
        if row["company"] != "TSMC":
            row.pop("unit", None)
    assert build_blocks(with_unit)[1] == build_blocks(without_unit)[1] == GOLDEN_CONTEXT


# --- C2.1: non-USD metrics are labelled with their own unit ---

def test_non_usd_lines_never_say_usd_and_carry_their_unit_in_value_and_change():
    r = golden_retrieval()
    r["metrics"] = [row for row in r["metrics"] if row["company"] == "TSMC"]
    blocks, _, _ = build_blocks(r)
    assert "USD" not in blocks.metrics_block
    assert "2,894,308,000,000 TWD" in blocks.metrics_block and "+732,572,000,000 TWD" in blocks.metrics_block


@pytest.mark.parametrize("metric, expected", [
    ({"company": "Nvidia", "metric": "revenue", "value": 60922000000.0,
      "period_start": "2023-01-30", "period_end": "2024-01-28"},
     "- revenue for period 2023-01-30..2024-01-28: 60,922,000,000 USD"),
    ({"company": "Nvidia", "metric": "revenue", "value": 60922000000.0, "unit": "USD",
      "period_start": "2023-01-30", "period_end": "2024-01-28"},
     "- revenue for period 2023-01-30..2024-01-28: 60,922,000,000 USD"),
    ({"company": "Nvidia", "metric": "revenue", "value": 60922000000.0, "unit": None,
      "period_start": "2023-01-30", "period_end": "2024-01-28"},
     "- revenue for period 2023-01-30..2024-01-28: 60,922,000,000 USD"),
    ({"company": "Nvidia", "metric": "revenue", "value": 60922000000.0, "unit": "",
      "period_start": "2023-01-30", "period_end": "2024-01-28"},
     "- revenue for period 2023-01-30..2024-01-28: 60,922,000,000 USD"),
    ({"company": "TSMC", "metric": "capex", "value": 1234567.6, "unit": "TWD",
      "period_start": "2024-01-01", "period_end": "2024-12-31"},
     "- capex for period 2024-01-01..2024-12-31: 1,234,568 TWD"),
    ({"company": "ASML", "metric": "rnd", "value": 4304000000.0, "unit": "EUR",
      "period_start": "2024-01-01", "period_end": "2024-12-31"},
     "- rnd for period 2024-01-01..2024-12-31: 4,304,000,000 EUR"),
])
def test_a_metric_row_renders_its_own_unit_in_the_metrics_block(metric, expected):
    """Replaces ``test_format_metric_line`` (the review removed ``format_metric_line``: exported and tested, used by
    nothing in production). The same unit rules, checked where production renders them: the METRICS block."""
    r = golden_retrieval()
    r["metrics"] = [metric]
    assert expected in build_blocks(r)[0].metrics_block.splitlines()
    assert _metric_amount(metric) == expected.rsplit(": ", 1)[1]



# --- C2.5: anchor honesty - the retrieval event carries anchor_defaulted (additive) ---

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


def test_retrieval_event_counts_keep_their_five_keys(monkeypatch):
    """``edges`` still counts the AFFECTED_BY rows and ``temporal`` the shown item rows: consumers are unchanged."""
    r = golden_retrieval()
    r["anchor_defaulted"] = True
    ev = _stream_events(monkeypatch, r)[0]
    assert ev["counts"] == {"edges": 3, "metrics": 11, "risks": 2, "temporal": 4, "chunks": 2}
    assert set(ev) == {"event", "anchors", "counts", "anchor_defaulted"}


def test_answer_result_carries_anchor_defaulted_in_raw_retrieval(monkeypatch):
    r = golden_retrieval()
    r["anchors"], r["anchor_defaulted"] = {}, True
    monkeypatch.setattr(answerer_mod, "hybrid_retrieve", lambda *a, **kw: r)
    out = answer(QUESTION, None, None, llm=lambda p: "No context.")
    assert out["retrieval"]["anchor_defaulted"] is True
    assert out["context"] == GOLDEN_CONTEXT
