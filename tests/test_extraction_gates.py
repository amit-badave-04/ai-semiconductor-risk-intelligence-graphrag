"""Extraction gate + checkpoint tests — no network, no LLM spend.

Pure tests for normalize/quote_in_chunk (smart quotes, whitespace runs, case)
and normalize_category, plus run_extraction's gate + critic + checkpoint
behavior with a mocked llm callable and a tiny synthetic chunk parquet.
"""

import json

import pandas as pd
import pytest

from semigraph.config import Settings
from semigraph.extraction import (
    ChunkExtraction,
    CriticVerdict,
    Product,
    Relation,
    RiskFactor,
    normalize,
    normalize_category,
    quote_in_chunk,
    run_extraction,
)

# --- normalize / quote_in_chunk (ported gate from notebooks 07/12) --------------

def test_normalize_collapses_whitespace_runs_and_casefolds():
    assert normalize("  Foo\n\nBar\t BAZ  ") == "foo bar baz"


def test_normalize_unifies_smart_and_straight_quotes():
    assert normalize("company’s “edge” risk") == normalize("company's \"edge\" risk")


def test_quote_in_chunk_verbatim_passes():
    chunk = "We purchase memory from SK Hynix, Micron, and Samsung."
    assert quote_in_chunk("We purchase memory from SK Hynix", chunk)


def test_quote_in_chunk_survives_smart_quotes_whitespace_and_case():
    chunk = "Demand for our “Hopper”  architecture\nremains strong."
    assert quote_in_chunk('demand for our "Hopper" architecture remains strong', chunk)


def test_quote_in_chunk_rejects_fabricated_quote():
    chunk = "We purchase memory from SK Hynix."
    assert not quote_in_chunk("We rely exclusively on a single foundry.", chunk)


# --- normalize_category ---------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Supply Chain", "Supply Chain"),           # exact enum
    ("supply chain", "Supply Chain"),           # case-insensitive enum
    ("legal/regulatory", "Legal/Regulatory"),
    ("supply chain disruption", "Supply Chain"),
    ("macroeconomic/geopolitical", "Geopolitical"),
    ("trade policy/tariffs", "Geopolitical"),
    ("export controls and sanctions", "Export Controls"),
    ("competitive pressure from custom silicon", "Competition"),
    ("cybersecurity", "Technology"),
    ("litigation and regulatory proceedings", "Legal/Regulatory"),
    ("currency exchange rate fluctuations", "Financial"),
    ("customer concentration", "Demand"),
    ("zebra stampede", "Other"),                # unmappable free text
    ("", "Other"),
    ("   ", "Other"),
])
def test_normalize_category(raw, expected):
    assert normalize_category(raw) == expected


# --- run_extraction: gate + critic + checkpoint ---------------------------------

CHUNK_TEXT = ('We purchase memory from SK Hynix. Our business depends on '
              '“advanced packaging” capacity from TSMC.')

GOOD_REL = Relation(
    source_entity="Nvidia", relation="DEPENDS_ON", target_entity="TSMC",
    evidence_quote='Our business depends on "advanced packaging" capacity from TSMC.',
)  # straight quotes vs the chunk's smart quotes — must survive the gate
FABRICATED_REL = Relation(
    source_entity="Nvidia", relation="DEPENDS_ON", target_entity="Samsung",
    evidence_quote="We rely exclusively on a single foundry.",
)  # not in the chunk — must be dropped BEFORE the critic


def make_settings(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    settings.chunks_dir.mkdir(parents=True)
    pd.DataFrame([{
        "chunk_id": "ACC1:I.1A:0000", "ticker": "NVDA", "form": "10-K",
        "filing_date": "2026-01-25", "accession_no": "ACC1", "section_id": "I.1A",
        "section_title": "Item 1A. Risk Factors", "sub_heading": None,
        "text": CHUNK_TEXT, "n_tokens": 30,
    }]).to_parquet(settings.chunks_dir / "nvda_chunks.parquet", index=False)
    return settings


class ScriptedLLM:
    """Mock for the injectable llm callable; records every call."""

    def __init__(self, extraction, verdict):
        self.extraction, self.verdict = extraction, verdict
        self.calls = []

    def __call__(self, prompt, model_cls, **kwargs):
        self.calls.append({"prompt": prompt, "model_cls": model_cls, **kwargs})
        return self.extraction if model_cls is ChunkExtraction else self.verdict


def test_gate_critic_and_checkpoint(tmp_path):
    settings = make_settings(tmp_path)
    llm = ScriptedLLM(
        extraction=ChunkExtraction(
            relations=[GOOD_REL, FABRICATED_REL],
            risk_factors=[RiskFactor(
                summary="Reliance on advanced packaging capacity.",
                category="supply chain disruption",  # free text -> post-mapped
                evidence_quote="We purchase memory from SK Hynix.")],
            products=[Product(name="Blackwell", type="GPU")],
        ),
        verdict=CriticVerdict(verdicts=[True]),
    )

    summary = run_extraction(settings, tickers=["NVDA"], llm=llm)
    assert summary == {"NVDA": 1}

    # one extractor call + one critic call
    assert len(llm.calls) == 2
    extractor_call, critic_call = llm.calls
    assert extractor_call["model"] == settings.llm_model
    # battle scars: critic on Haiku, thinking NOT disabled, 600-token cap
    assert critic_call["model"] == settings.critic_model
    assert critic_call["max_tokens"] == 600
    assert critic_call["thinking_off"] is False
    # the fabricated relation was gated out BEFORE the critic saw the claims
    assert "1. Nvidia DEPENDS_ON TSMC" in critic_call["prompt"]
    assert "2." not in critic_call["prompt"].split("CLAIMS:")[1]
    # extractor prompt carries the filer display name for we/our resolution
    assert "Nvidia" in extractor_call["prompt"]

    out = settings.extractions_dir / "nvda_extractions.jsonl"
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["chunk_id"] == "ACC1:I.1A:0000"
    assert record["ticker"] == "NVDA" and record["accession_no"] == "ACC1"
    # only the verbatim-quoted relation survived
    assert record["relations"] == [GOOD_REL.model_dump()]
    # free-text category normalized onto the enum at persist time
    assert record["risk_factors"][0]["category"] == "Supply Chain"
    assert record["products"] == [{"name": "Blackwell", "type": "GPU"}]

    # --- resume: already-done chunk_ids are skipped without calling the llm ---
    llm2 = ScriptedLLM(extraction=None, verdict=None)
    summary2 = run_extraction(settings, tickers=["NVDA"], llm=llm2)
    assert summary2 == {"NVDA": 0}
    assert llm2.calls == []  # interruptions never re-bill
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1


def test_critic_verdict_count_mismatch_keeps_all_relations(tmp_path):
    settings = make_settings(tmp_path)
    second_rel = Relation(
        source_entity="Nvidia", relation="CUSTOMER_OF", target_entity="SK Hynix",
        evidence_quote="We purchase memory from SK Hynix.",
    )
    llm = ScriptedLLM(
        extraction=ChunkExtraction(relations=[GOOD_REL, second_rel]),
        verdict=CriticVerdict(verdicts=[False]),  # 1 verdict for 2 claims
    )
    run_extraction(settings, tickers=["NVDA"], llm=llm)
    record = json.loads(
        (settings.extractions_dir / "nvda_extractions.jsonl").read_text(encoding="utf-8")
    )
    # mismatch -> keep all (as in the notebooks), even though the verdict says False
    assert record["relations"] == [GOOD_REL.model_dump(), second_rel.model_dump()]
