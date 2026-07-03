"""Entity-resolution tests — pure logic ported from notebooks 08/12, no network.

Covers: exact alias hit, legal-suffix-stripped hit, fuzzy hit at/above the 0.90
threshold, miss stays unresolved, filer-aware self-reference resolution, and
the file-level resolve_extractions round trip (jsonl in -> resolved jsonl +
resolution report parquet out).
"""

import json

import pandas as pd
import pytest

from semigraph.artifacts import load_canonical_entities
from semigraph.config import Settings
from semigraph.extraction import (
    build_alias_lookup,
    normalize_name,
    resolve_entity,
    resolve_extractions,
    resolved_jsonl_path,
)


@pytest.fixture(scope="module")
def lookup():
    return build_alias_lookup(load_canonical_entities())


# --- normalization ------------------------------------------------------------

def test_normalize_name_strips_legal_suffixes_and_punctuation():
    assert normalize_name("Micron Technology, Inc.") == "micron technology"
    assert normalize_name("Taiwan Semiconductor Manufacturing Company Limited") \
        == "taiwan semiconductor manufacturing"
    assert normalize_name("ASML Holding NV") == "asml"


# --- resolve_entity -----------------------------------------------------------

def test_exact_alias_hit(lookup):
    assert resolve_entity("TSMC", lookup) == "TSMC"
    assert resolve_entity("Amazon Web Services", lookup) == "Amazon"
    assert resolve_entity("google cloud", lookup) == "Alphabet"


def test_legal_suffix_stripped_hit(lookup):
    # notebook 08's own unit checks
    assert resolve_entity("Taiwan Semiconductor Manufacturing Company Limited", lookup) == "TSMC"
    assert resolve_entity("Micron Technology, Inc.", lookup) == "Micron"
    assert resolve_entity("SK hynix Inc.", lookup) == "SK Hynix"
    assert resolve_entity("Advanced Micro Devices, Inc.", lookup) == "AMD"


def test_fuzzy_hit_above_threshold(lookup):
    # "advanced micro device" vs alias "advanced micro devices": ratio ~0.98 >= 0.90
    assert resolve_entity("Advanced Micro Device", lookup) == "AMD"


def test_fuzzy_below_threshold_stays_unresolved(lookup):
    # "advanced micro" vs "advanced micro devices": ratio ~0.78 < 0.90
    assert resolve_entity("Advanced Micro", lookup) is None


def test_miss_stays_unresolved(lookup):
    assert resolve_entity("Some Unknown Widget Maker", lookup) is None
    assert resolve_entity("", lookup) is None
    assert resolve_entity("Inc.", lookup) is None  # normalizes to empty


def test_self_reference_resolves_to_filer(lookup):
    assert resolve_entity("we", lookup, filer="Nvidia") == "Nvidia"
    assert resolve_entity("Our", lookup, filer="TSMC") == "TSMC"
    assert resolve_entity("the Company", lookup, filer="Intel") == "Intel"


def test_self_reference_without_filer_stays_unresolved(lookup):
    assert resolve_entity("we", lookup) is None
    assert resolve_entity("our", lookup) is None


# --- resolve_extractions (file-level round trip) -------------------------------

def test_resolve_extractions_round_trip(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    settings.extractions_dir.mkdir(parents=True)
    record = {
        "chunk_id": "ACC1:I.1:0000", "ticker": "NVDA",
        "accession_no": "ACC1", "section_id": "I.1",
        "relations": [
            {  # kept: self-reference -> Nvidia, long form -> TSMC
                "source_entity": "we", "relation": "DEPENDS_ON",
                "target_entity": "Taiwan Semiconductor Manufacturing Company Limited",
                "evidence_quote": "We utilize foundries, such as TSMC."},
            {  # dropped: unresolvable target
                "source_entity": "Nvidia", "relation": "SUPPLIES_TO",
                "target_entity": "Obscure Widget Maker GmbH",
                "evidence_quote": "irrelevant"},
            {  # dropped: self-loop after canonicalization
                "source_entity": "Micron Technology, Inc.", "relation": "COMPETES_WITH",
                "target_entity": "Micron", "evidence_quote": "irrelevant"},
        ],
        "risk_factors": [{"summary": "Tariffs may hurt.", "category": "trade policy/tariffs",
                          "evidence_quote": "tariffs"}],
        "products": [{"name": "Blackwell", "type": "GPU"}],
    }
    src = settings.extractions_dir / "nvda_extractions.jsonl"
    src.write_text(json.dumps(record) + "\n", encoding="utf-8")

    result = resolve_extractions(settings, tickers=["NVDA"])

    assert result["kept"] == 1 and result["dropped"] == 2
    out = resolved_jsonl_path(settings, "NVDA")
    assert out.name == "nvda_extractions_resolved.jsonl" and out.exists()
    resolved = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    (kept_rel,) = resolved["relations"]
    assert kept_rel["source_canonical"] == "Nvidia"
    assert kept_rel["target_canonical"] == "TSMC"
    # free-text category post-mapped onto the enum at persist time
    assert resolved["risk_factors"][0]["category"] == "Geopolitical"
    assert resolved["products"] == record["products"]

    report = pd.read_parquet(settings.extractions_dir / "resolution_report_universe.parquet")
    assert len(report) == 2
    assert set(report["target_entity"]) == {"Obscure Widget Maker GmbH", "Micron"}
    assert {"src_resolved", "tgt_resolved", "ticker", "chunk_id"} <= set(report.columns)
