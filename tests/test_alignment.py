"""Pure tests for the risk-item alignment core (M1b step 2, plan section B.2-B.5).

No Neo4j, no network, no model: embeddings are injected fakes. Two kinds of
fixtures: a small synthetic corpus of realistic risk paragraphs, and short REAL
excerpts (<= 600 chars each) copied from the Nvidia FY25 and FY26 annual
filings' Item 1A text (``data/interim/section_texts/nvda_section_texts.parquet``).
The last block runs the whole FY25 -> FY26 sections and skips when the local
data lake is absent.
"""

import dataclasses
import math
import re
import time
import zlib
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import pytest
from rapidfuzz import fuzz

from semigraph.extraction.gates import normalize as gates_normalize
from semigraph.extraction.gates import quote_in_chunk
from semigraph.graph import align_text as at
from semigraph.graph import alignment as al
from semigraph.graph.alignment import (
    AlignParams,
    align,
    apply_adjudication,
    split_sentences,
    summarize,
)
from semigraph.hashing import content_hash

# --------------------------------------------------------------------------
# synthetic corpus: (headline, body) pairs with clearly distinct vocabularies
# --------------------------------------------------------------------------

SUPPLY = (
    "We depend on a single foundry partner to manufacture substantially all of our wafers and any disruption could harm our business.",
    "Our products are fabricated by third party foundries located in Taiwan, and we do not control their capacity allocation or pricing decisions. "
    "A natural disaster, power outage or geopolitical event affecting the region could interrupt wafer supply for several quarters. "
    "We may be unable to qualify alternative suppliers quickly enough to prevent shortfalls in shipments to customers. "
    "Long lead times for advanced packaging capacity make it difficult to respond to sudden changes in demand.",
)
EXPORT = (
    "Export control restrictions on advanced accelerators could limit our ability to sell into China and other regions.",
    "The government has imposed licensing requirements on shipments of our highest performance data center products to certain countries. "
    "These rules are complex, change frequently and may be applied retroactively to existing customer orders. "
    "If we fail to obtain licenses on acceptable terms our revenue in affected regions could decline materially. "
    "Competitors that are not subject to the same restrictions may gain market share at our expense.",
)
CUSTOMERS = (
    "A small number of large customers account for a significant portion of our revenue and the loss of any one could harm our results.",
    "Sales to our two largest cloud service provider customers represented more than one third of total revenue in the most recent fiscal year. "
    "These customers negotiate aggressively on price and may reduce purchases with little notice. "
    "Consolidation among customers could further concentrate our sales. "
    "Any delay in a large customer deployment could cause our quarterly results to fall short of expectations.",
)
CYBER = (
    "Cybersecurity incidents could disrupt our operations, expose confidential information and damage our reputation with customers.",
    "We rely on complex information technology systems and on third party service providers to store and process sensitive data. "
    "Malicious actors including nation state groups regularly attempt to gain unauthorized access to our networks. "
    "A successful attack could result in theft of intellectual property, interruption of design and manufacturing activities and costly remediation. "
    "Insurance coverage may not be sufficient to cover all losses arising from such an incident.",
)
TAX = (
    "Changes in tax laws and audits by tax authorities could increase our effective tax rate and reduce our operating results.",
    "We are subject to income taxes in the United States and many foreign jurisdictions with differing rules and rates. "
    "Significant judgment is required in determining our worldwide provision for income taxes and related liabilities. "
    "Governments continue to consider reforms including global minimum tax regimes that may raise our cash tax payments. "
    "An unfavorable outcome in any audit could require us to pay additional amounts with interest and penalties.",
)
TALENT = (
    "Our success depends on attracting and retaining highly skilled engineers and executives in a competitive labor market.",
    "Demand for experienced chip designers and software developers far exceeds supply in the regions where we operate. "
    "Competitors and start ups may offer larger equity grants or more flexible working arrangements than we can. "
    "The loss of key personnel or an inability to hire quickly could delay product roadmaps. "
    "Stock price declines can reduce the retention value of our equity awards.",
)
AI_REG = (
    "Regulation of artificial intelligence in the European Union and other jurisdictions could increase our compliance costs and limit product features.",
    "Lawmakers are adopting rules that classify certain systems as high risk and impose documentation, testing and transparency duties on providers. "
    "Our customers may require us to certify conformity with these rules before purchasing our products. "
    "Penalties for violations can reach a meaningful percentage of global turnover. "
    "Uncertainty about how regulators will interpret the requirements makes planning difficult.",
)


def make(item_id, pair, *, kind="headline"):
    """An item row as the unit detector emits it: headline + body in ``text``."""
    headline, body = pair
    if kind == "paragraph":
        headline, text = "", body
    else:
        text = f"{headline} {body}".strip()
    return {"item_id": item_id, "headline": headline, "text": text,
            "text_hash": content_hash(text), "unit_kind": kind}


def section_of(*rows):
    return "\n\n".join(r["text"] for r in rows)


def by_id(decisions):
    return {d.item_id: d for d in decisions}


def bow_embed(texts):
    """Deterministic bag-of-words hashing embedding (words longer than 3 chars)."""
    out = np.zeros((len(texts), 2048))
    for i, text in enumerate(texts):
        for word in re.findall(r"\w+", text.lower()):
            if len(word) > 3:
                out[i, zlib.crc32(word.encode()) % 2048] += 1.0
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.where(norms == 0, 1.0, norms)


class TableEmbed:
    """Scripted embedding: text -> chosen 2-d vector; unseen texts get a fresh orthogonal axis."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []
        self._next_axis = 2

    def __call__(self, texts):
        self.calls.append(list(texts))
        rows = []
        for text in texts:
            if text not in self.table:
                vec = np.zeros(64)
                vec[self._next_axis] = 1.0
                self._next_axis += 1
                self.table[text] = vec
            rows.append(np.pad(np.asarray(self.table[text], dtype=float), (0, 64 - len(self.table[text]))))
        return np.vstack(rows)


def cos_pair(older_text, newer_text, cos):
    """A TableEmbed that gives the two texts a cosine similarity of exactly ``cos``."""
    return TableEmbed({older_text: [1.0, 0.0], newer_text: [cos, math.sqrt(1 - cos * cos)]})


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

def test_norm_matches_the_extraction_gate_normaliser():
    for sample in ["  Hello   WORLD ", "It’s a “quoted”\n\nline", 'He said "yes" and it\'s fine.']:
        assert at.norm(sample) == gates_normalize(sample)


def test_split_sentences_handles_glued_text_bullets_and_abbreviations():
    text = ("Our cash flows.Further, changes in tax law matter.•Competition could hurt us. "
            "The U.S. Department of Commerce sets rules.\nA new line starts here.")
    parts = [text[a:b] for a, b in split_sentences(text)]
    assert parts == [
        "Our cash flows.",
        "Further, changes in tax law matter.",
        "Competition could hurt us.",
        "The U.S. Department of Commerce sets rules.",
        "A new line starts here.",
    ]


def test_split_sentences_returns_offsets_into_the_original_text():
    text = "  First one here.   Second one there.\n\n  Third."
    spans = split_sentences(text)
    assert [text[a:b] for a, b in spans] == ["First one here.", "Second one there.", "Third."]
    assert split_sentences("") == []
    assert split_sentences("   \n  ") == []


# --------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------

def test_default_params_are_the_plans_thresholds():
    p = AlignParams()
    assert p.headline_min_ratio == 90.0
    assert (p.embed_accept, p.lex_accept, p.embed_reject) == (0.85, 0.45, 0.65)
    assert p.absence_min_ratio == 85.0
    assert p.n_longest_sentences == 2
    assert p.min_probe_hits == 2


def test_params_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        AlignParams().embed_accept = 0.5  # type: ignore[misc]


@pytest.mark.parametrize("kwargs", [
    {"headline_min_ratio": 0.0},
    {"headline_min_ratio": 101.0},
    {"embed_accept": 1.5},
    {"embed_reject": -0.1},
    {"embed_accept": 0.6, "embed_reject": 0.7},
    {"lex_accept": -0.01},
    {"lex_accept": 1.01},
    {"lex_only_accept": 0.2, "lex_only_reject": 0.3},
    {"absence_min_ratio": 0.0},
    {"absence_min_ratio": 100.5},
    {"min_term_chars": 0},
    {"n_longest_sentences": 0},
    {"min_body_tokens": -1},
    {"min_headline_tokens": 0},
    {"max_dropped_sentences": -1},
    {"embed_accept": float("nan")},
    {"min_probe_hits": 0},
    {"max_quote_chars": 0},
    {"lex_only_accept": 1.5},
])
def test_invalid_params_are_rejected(kwargs):
    with pytest.raises(ValueError):
        AlignParams(**kwargs)


# --------------------------------------------------------------------------
# step 2: exact hash
# --------------------------------------------------------------------------

def test_identical_items_are_unchanged_by_hash():
    older = [make("o1", SUPPLY), make("o2", EXPORT)]
    newer = [make("n1", SUPPLY), make("n2", EXPORT)]
    res = align(older, newer, section_of(*newer))
    o = by_id(res.older)
    assert (o["o1"].label, o["o1"].decided_by, o["o1"].matched_newer_id) == ("unchanged", "hash", "n1")
    assert (o["o2"].label, o["o2"].decided_by, o["o2"].matched_newer_id) == ("unchanged", "hash", "n2")
    n = by_id(res.newer)
    assert (n["n1"].label, n["n1"].matched_older_id, n["n1"].decided_by) == ("carried", "o1", "hash")
    assert (n["n2"].label, n["n2"].matched_older_id) == ("carried", "o2")


def test_text_hash_is_computed_when_missing_and_nan_headlines_are_tolerated():
    older = [{"item_id": "o1", "headline": float("nan"), "text": SUPPLY[1]}]
    newer = [{"item_id": "n1", "headline": None, "text": SUPPLY[1], "unit_kind": "paragraph"}]
    res = align(older, newer, SUPPLY[1])
    assert res.older[0].label == "unchanged"
    assert res.older[0].matched_newer_id == "n1"


def test_embedding_is_not_called_when_everything_matches_cheaply():
    older = [make("o1", SUPPLY), make("o2", EXPORT)]
    newer = [make("n1", EXPORT), make("n2", SUPPLY)]
    embed = TableEmbed()
    align(older, newer, section_of(*newer), embed=embed)
    assert embed.calls == []


def test_results_keep_input_order_and_are_deterministic():
    older = [make("o1", SUPPLY), make("o2", EXPORT), make("o3", CYBER)]
    newer = [make("n1", EXPORT), make("n2", SUPPLY)]
    r1 = align(older, newer, section_of(*newer))
    r2 = align(older, newer, section_of(*newer))
    assert r1 == r2
    assert [d.item_id for d in r1.older] == ["o1", "o2", "o3"]
    assert [d.item_id for d in r1.newer] == ["n1", "n2"]
    flipped = align(older, list(reversed(newer)), section_of(*newer))
    assert {d.item_id: (d.label, d.matched_newer_id) for d in flipped.older} == \
           {d.item_id: (d.label, d.matched_newer_id) for d in r1.older}


def test_duplicate_texts_pair_in_document_order_and_the_extra_copy_is_merged_not_removed():
    older = [make("o1", SUPPLY), make("o2", SUPPLY)]
    one_copy = [make("n1", SUPPLY)]
    res = align(older, one_copy, section_of(*one_copy))
    o = by_id(res.older)
    assert o["o1"].label == "unchanged" and o["o1"].matched_newer_id == "n1"
    assert o["o2"].label == "merged"          # its text is in the section: never removed
    assert o["o2"].evidence.quote
    two_copies = [make("n1", SUPPLY), make("n2", SUPPLY)]
    res = align(older, two_copies, section_of(*two_copies))
    o = by_id(res.older)
    assert (o["o1"].matched_newer_id, o["o2"].matched_newer_id) == ("n1", "n2")
    assert {d.label for d in res.older} == {"unchanged"}


def test_duplicate_item_ids_and_missing_fields_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        align([make("x", SUPPLY), make("x", EXPORT)], [], "")
    with pytest.raises(ValueError, match="item_id"):
        align([{"text": "abc"}], [], "")
    with pytest.raises(ValueError, match="text"):
        align([], [{"item_id": "n1"}], "")


def test_empty_inputs():
    res = align([], [], "")
    assert res.older == () and res.newer == ()
    only_new = align([], [make("n1", SUPPLY)], SUPPLY[1])
    assert [(d.label, d.decided_by) for d in only_new.newer] == [("new", "unmatched")]
    only_old = align([make("o1", SUPPLY)], [], "")
    d = only_old.older[0]
    assert d.label == "removed" and d.decided_by == "text_check" and d.evidence.search_terms


# --------------------------------------------------------------------------
# step 3: headline match
# --------------------------------------------------------------------------

def test_same_headline_edited_body_is_reworded_by_headline():
    edited = (SUPPLY[0], SUPPLY[1].replace(
        "A natural disaster, power outage or geopolitical event affecting the region could interrupt wafer supply for several quarters.",
        "Earthquakes, typhoons or political tension near the island could stop shipments of finished wafers for months at a time."))
    older, newer = [make("o1", SUPPLY)], [make("n1", edited)]
    res = align(older, newer, section_of(*newer))
    d = res.older[0]
    assert (d.label, d.decided_by, d.matched_newer_id) == ("reworded", "headline", "n1")
    assert d.evidence.headline_ratio == 100.0
    assert d.evidence.quote and quote_in_chunk(d.evidence.quote, section_of(*newer))
    # the replaced sentence is verifiably gone from the newer section
    assert any("natural disaster" in s for s in d.evidence.dropped_sentences)
    assert res.newer[0].label == "carried" and res.newer[0].matched_older_id == "o1"


def test_slightly_reworded_headline_still_matches():
    newer_headline = SUPPLY[0].replace("a single foundry partner", "one foundry partner")
    older, newer = [make("o1", SUPPLY)], [make("n1", (newer_headline, SUPPLY[1] + " Prices may rise."))]
    res = align(older, newer, section_of(*newer))
    assert res.older[0].label == "reworded" and res.older[0].decided_by == "headline"
    assert res.older[0].evidence.headline_ratio >= 90.0


def test_a_headline_whose_tokens_are_a_subset_of_the_other_is_not_auto_reworded():
    # token_set_ratio scores 100 for any token subset; the plain ratio is far below the threshold
    sub = ("We depend on a single foundry partner", SUPPLY[1])
    assert fuzz.token_set_ratio(sub[0], SUPPLY[0]) == 100.0
    assert fuzz.ratio(sub[0], SUPPLY[0]) < 90.0
    o, n = make("o1", sub), make("n1", (SUPPLY[0], EXPORT[1]))
    res = align([o], [n], n["text"])
    assert res.older[0].decided_by != "headline" and res.older[0].matched_newer_id is None


def test_short_generic_headlines_never_match():
    o = make("o1", ("Competition", SUPPLY[1]))
    n = make("n1", ("Competition", EXPORT[1]))
    res = align([o], [n], n["text"])
    assert res.older[0].decided_by != "headline"


def test_case_and_trailing_punctuation_alone_do_not_change_a_headline():
    shouted = SUPPLY[0].upper().rstrip(".")
    edited_body = SUPPLY[1].replace("Long lead times", "Extended lead times")
    n = make("n1", (shouted, edited_body))
    res = align([make("o1", SUPPLY)], [n], n["text"])
    d = res.older[0]
    assert (d.label, d.decided_by) == ("reworded", "headline")
    assert d.evidence.headline_ratio == 100.0
    same_body = make("n1", (shouted, SUPPLY[1]))
    res = align([make("o1", SUPPLY)], [same_body], same_body["text"])
    assert (res.older[0].label, res.older[0].decided_by) == ("unchanged", "hash")


def test_headline_matches_are_one_to_one():
    older = [make("o1", (SUPPLY[0], SUPPLY[1])), make("o2", (SUPPLY[0], EXPORT[1]))]
    newer = [make("n1", (SUPPLY[0], SUPPLY[1] + " Extra words appear here."))]
    res = align(older, newer, section_of(*newer))
    paired = [d for d in res.older if d.label in ("unchanged", "reworded") and d.matched_newer_id == "n1"]
    assert len(paired) == 1
    assert paired[0].item_id == "o1"                  # equal headlines: the closer body wins the tie


def test_identical_body_with_a_rewritten_headline_is_reworded_by_hash():
    rewritten = ("Reliance on contract manufacturers in East Asia exposes us to shortages of finished silicon.", SUPPLY[1])
    res = align([make("o1", SUPPLY)], [make("n1", rewritten)], rewritten[1])
    d = res.older[0]
    assert (d.label, d.decided_by, d.matched_newer_id) == ("reworded", "hash", "n1")


# real excerpts (Nvidia FY25 vs FY26 Item 1A)
SCRUTINY_H25 = "Increased scrutiny from shareholders, regulators and others regarding our corporate sustainability practices could result in additional costs or risks and adversely impact our reputation and willingness of customers and suppliers to do business with us."
SCRUTINY_H26 = "Scrutiny from shareholders, regulators and others regarding our corporate sustainability practices could result in additional costs or risks and adversely impact our reputation and willingness of customers and suppliers to do business with us."
SCRUTINY_BODY = "Certain shareholder advocacy groups, investment funds, shareholders and other market participants, customers and government regulators have focused on corporate sustainability practices and disclosures, including those associated with climate change and human rights. Stakeholders may not be satisfied with our corporate sustainability practices and goals or the speed of their adoption. Further, there are state-level initiatives in the U.S. that may differ from other regulatory requirements or our various stakeholders’ expectations."
LEAD_H25 = "Long manufacturing lead times and uncertain supply and component availability, combined with a failure to estimate customer demand accurately, has led and could lead to mismatches between supply and demand."
LEAD_H26 = "Long manufacturing lead times and uncertain supply and capacity availability, combined with a failure to estimate customer demand accurately, has led and could lead to mismatches between supply and demand."


def test_real_headline_tweak_with_identical_body_is_unchanged():
    o, n = make("o", (SCRUTINY_H25, SCRUTINY_BODY)), make("n", (SCRUTINY_H26, SCRUTINY_BODY))
    res = align([o], [n], n["text"])
    d = res.older[0]
    assert (d.label, d.decided_by, d.matched_newer_id) == ("unchanged", "hash", "n")
    assert d.evidence.headline_ratio >= 90.0


def test_real_headline_only_items_with_one_word_changed_are_reworded():
    o = {"item_id": "o", "headline": LEAD_H25, "text": LEAD_H25, "unit_kind": "headline"}
    n = {"item_id": "n", "headline": LEAD_H26, "text": LEAD_H26, "unit_kind": "headline"}
    res = align([o], [n], LEAD_H26)
    d = res.older[0]
    # empty bodies must not count as hash-equal bodies
    assert (d.label, d.decided_by) == ("reworded", "headline")


# --------------------------------------------------------------------------
# step 4: body assignment
# --------------------------------------------------------------------------

def test_headline_rewritten_body_near_identical_is_reworded_with_a_high_embedding():
    new_pair = ("Reliance on contract manufacturers in East Asia exposes us to shortages of finished silicon.",
                SUPPLY[1].replace("Long lead times", "Extended lead times"))
    o, n = make("o1", SUPPLY), make("n1", new_pair)
    res = align([o], [n], n["text"], embed=cos_pair(o["text"], n["text"], 0.93))
    d = res.older[0]
    assert (d.label, d.decided_by, d.matched_newer_id) == ("reworded", "body", "n1")
    assert d.evidence.embed_sim == pytest.approx(0.93, abs=1e-6)
    assert d.evidence.lex_sim >= 0.45
    assert d.evidence.quote and quote_in_chunk(d.evidence.quote, n["text"])


def test_lexical_only_mode_accepts_near_identical_bodies():
    new_pair = ("Reliance on contract manufacturers in East Asia exposes us to shortages of finished silicon.",
                SUPPLY[1].replace("Long lead times", "Extended lead times"))
    o, n = make("o1", SUPPLY), make("n1", new_pair)
    res = align([o], [n], n["text"])
    d = res.older[0]
    assert (d.label, d.decided_by, d.evidence.embed_sim) == ("reworded", "body", None)
    assert d.evidence.lex_sim >= 0.75


def test_a_low_embedding_vetoes_a_pairing_but_the_text_check_still_prevents_removal():
    new_pair = ("Reliance on contract manufacturers in East Asia exposes us to shortages of finished silicon.",
                SUPPLY[1].replace("Long lead times", "Extended lead times"))
    o, n = make("o1", SUPPLY), make("n1", new_pair)
    res = align([o], [n], n["text"], embed=cos_pair(o["text"], n["text"], 0.40))
    d = res.older[0]
    assert d.label == "merged" and d.decided_by == "text_check"
    assert d.evidence.embed_sim == pytest.approx(0.40, abs=1e-6)


def test_high_embedding_but_low_lexical_overlap_is_uncertain_not_accepted():
    o, n = make("o1", SUPPLY), make("n1", CYBER)
    res = align([o], [n], n["text"], embed=cos_pair(o["text"], n["text"], 0.92))
    assert res.older[0].label == "uncertain"


def test_ambiguous_embedding_band_is_uncertain_and_keeps_its_candidate():
    heavy = ("Dependence on outsourced chip fabrication concentrates operational exposure",
             "Most silicon is produced by external plants in one region. Shortages or shutdowns there would delay revenue. "
             "Replacement vendors take a long time to certify, so buffers are limited.")
    o, n = make("o1", SUPPLY), make("n1", heavy)
    res = align([o], [n], n["text"], embed=cos_pair(o["text"], n["text"], 0.75))
    d = res.older[0]
    assert (d.label, d.decided_by, d.matched_newer_id) == ("uncertain", "uncertain", "n1")
    assert d.evidence.embed_sim == pytest.approx(0.75, abs=1e-6)
    assert res.newer[0].label == "uncertain" and res.newer[0].matched_older_id == "o1"


def test_lexical_only_band_is_uncertain():
    partly = (SUPPLY[0].replace("a single foundry partner", "contract manufacturers in Asia"),
              "Our products are fabricated by third party foundries located in Taiwan. "
              "Outages could interrupt wafer supply. Buffers of finished goods are small.")
    o, n = make("o1", SUPPLY), make("n1", partly)
    res = align([o], [n], n["text"])
    d = res.older[0]
    assert d.label == "uncertain" and 0.30 <= d.evidence.lex_sim < 0.75


def test_two_older_items_resembling_one_newer_item_is_one_to_one():
    p1 = make("p1", SUPPLY, kind="paragraph")
    p2_body = SUPPLY[1].replace(SUPPLY[1].split(". ")[2] + ".", "Insurance rarely covers such losses.").replace(
        SUPPLY[1].split(". ")[3], "Buffers of inventory are limited.")
    p2 = make("p2", (SUPPLY[0], p2_body), kind="paragraph")
    n = make("n1", (SUPPLY[0], SUPPLY[1].replace("Long lead times", "Extended lead times")), kind="paragraph")
    for embed in (None, bow_embed):
        res = align([p1, p2], [n], n["text"], embed=embed)
        o = by_id(res.older)
        paired = [d for d in res.older if d.label in ("unchanged", "reworded") and d.matched_newer_id == "n1"]
        assert len(paired) == 1 and paired[0].item_id == "p1"
        assert o["p2"].label != "removed"
        assert res.newer[0].matched_older_id == "p1"


def test_very_short_items_cannot_be_body_matched():
    o = {"item_id": "o1", "headline": "", "text": "Not applicable.", "unit_kind": "paragraph"}
    n = {"item_id": "n1", "headline": "", "text": "Not applicable here.", "unit_kind": "paragraph"}
    res = align([o], [n], n["text"])
    d = res.older[0]
    # no searchable term either: it can neither be paired nor proven absent
    assert d.label == "uncertain" and d.matched_newer_id is None and d.evidence.search_terms == ()


def test_paragraph_units_without_headlines_are_aligned_by_body():
    o = make("o1", (SUPPLY[0], SUPPLY[1]), kind="paragraph")
    n = make("n1", (SUPPLY[0], SUPPLY[1].replace("Taiwan", "Taiwan and Korea")), kind="paragraph")
    res = align([o], [n], n["text"])
    assert (res.older[0].label, res.older[0].decided_by) == ("reworded", "body")


def test_real_tax_paragraph_edit_is_reworded_by_body_and_not_dropped():
    o = make("o1", ("", TAX_FY25), kind="paragraph")
    n = make("n1", ("", TAX_FY26), kind="paragraph")
    res = align([o], [n], n["text"])
    d = res.older[0]
    assert (d.label, d.decided_by) == ("reworded", "body")
    assert d.evidence.dropped_sentences == ()      # both sentences survive with a one-word change


TAX_FY25 = "Most of our income is taxable in the U.S., with a significant portion qualifying for preferential treatment as foreign-derived intangible income, or FDII. If U.S. tax rates increase or the FDII deduction is reduced, our provision for income taxes, results of operations, net income, and cash flows would be adversely affected."
TAX_FY26 = "Most of our income is taxable in the U.S., with a significant portion qualifying for preferential treatment as foreign-derived deduction eligible income, or FDDEI. If U.S. tax rates increase or the FDDEI deduction is reduced, our provision for income taxes, results of operations, net income, and cash flows would be adversely affected."


# --------------------------------------------------------------------------
# step 5: text-grounded absence check
# --------------------------------------------------------------------------

def test_item_absorbed_into_another_newer_item_is_merged_not_removed():
    absorber = (EXPORT[0], EXPORT[1] + " " + CUSTOMERS[1])
    older = [make("o1", EXPORT), make("o2", CUSTOMERS)]
    newer = [make("n1", absorber)]
    res = align(older, newer, section_of(*newer))
    o = by_id(res.older)
    assert o["o1"].label == "reworded" and o["o1"].matched_newer_id == "n1"
    d = o["o2"]
    assert (d.label, d.decided_by, d.matched_newer_id) == ("merged", "text_check", "n1")
    assert d.evidence.quote and quote_in_chunk(d.evidence.quote, section_of(*newer))
    assert d.evidence.quote_item_id == "n1"
    assert res.newer[0].matched_older_id == "o1"      # a merge target does not steal the pairing


def test_an_unpaired_newer_item_that_absorbed_older_text_is_carried_not_new():
    absorber = ("Combined disclosure about concentrated customers and tax exposure appears here.",
                CUSTOMERS[1] + " " + TAX[1])
    older = [make("o1", CUSTOMERS), make("o2", TAX)]
    newer = [make("n1", absorber)]
    res = align(older, newer, section_of(*newer))
    o = by_id(res.older)
    assert o["o1"].label in ("merged", "uncertain", "reworded") and o["o1"].label != "removed"
    assert o["o2"].label != "removed"
    assert res.newer[0].label in ("carried", "uncertain")


def test_genuinely_deleted_item_is_removed_and_records_the_search_terms_tried():
    older = [make("o1", SUPPLY), make("o2", CYBER)]
    newer = [make("n1", SUPPLY)]
    res = align(older, newer, section_of(*newer))
    d = by_id(res.older)["o2"]
    assert (d.label, d.decided_by, d.matched_newer_id) == ("removed", "text_check", None)
    body_sentences = sorted(re.split(r"(?<=[.])\s+", CYBER[1]), key=len, reverse=True)[:2]
    assert d.evidence.search_terms == (CYBER[0], *body_sentences)
    assert d.evidence.quote is None


def test_brand_new_newer_item_is_new():
    older = [make("o1", SUPPLY)]
    newer = [make("n1", SUPPLY), make("n2", AI_REG)]
    res = align(older, newer, section_of(*newer))
    n = by_id(res.newer)
    assert (n["n2"].label, n["n2"].matched_older_id, n["n2"].decided_by) == ("new", None, "unmatched")
    assert n["n1"].label == "carried"


def test_new_items_are_verified_against_the_older_section_when_it_is_supplied():
    moved = make("n2", CUSTOMERS)                     # in the old filing's text but missed by its segmentation
    older_text = section_of(make("o1", SUPPLY), make("x", CUSTOMERS))
    res = align([make("o1", SUPPLY)], [make("n1", SUPPLY), moved], section_of(make("n1", SUPPLY), moved),
                older_section_text=older_text)
    n2 = by_id(res.newer)["n2"]
    assert (n2.label, n2.decided_by) == ("carried", "text_check")
    assert n2.evidence.quote and quote_in_chunk(n2.evidence.quote, older_text)
    truly_new = make("n3", AI_REG)
    res = align([make("o1", SUPPLY)], [make("n1", SUPPLY), truly_new], section_of(make("n1", SUPPLY), truly_new),
                older_section_text=older_text)
    n3 = by_id(res.newer)["n3"]
    assert (n3.label, n3.decided_by) == ("new", "text_check")
    assert n3.evidence.search_terms


def _guard_section(term_variant):
    filler = " ".join(f"{make('f', c)['text']}" for c in (EXPORT, CUSTOMERS, TAX))
    return f"{filler}\n\n{term_variant}\n\n{make('g', TALENT)['text']}"


@pytest.mark.parametrize("variant", [
    lambda s: s,                                                          # verbatim
    lambda s: s.replace("could", "might").replace(" and ", " or "),        # a few edited words
    lambda s: s.upper(),                                                  # case
    lambda s: s.replace(" ", "  ").replace(",", "’"),                     # spacing and quote characters
    lambda s: s[: len(s) // 2] + "\n" + s[len(s) // 2:],                  # a line break inside the sentence
    lambda s: "Preceding sentence ends here.Then " + s,                   # glued to the previous sentence
    lambda s: s + " More text follows immediately in the same paragraph, adding detail.",
])
@pytest.mark.parametrize("which", ["headline", "sentence"])
def test_false_drop_guard_headline_or_longest_sentence_anywhere_in_the_section_blocks_removal(variant, which):
    """Stated guarantee: an item whose headline or one of its two longest sentences appears (fuzzily)
    anywhere in the newer section is never labelled ``removed``."""
    longest = sorted(re.split(r"(?<=[.])\s+", CYBER[1]), key=len, reverse=True)[0]
    term = CYBER[0] if which == "headline" else longest
    section = _guard_section(variant(term))
    res = align([make("o1", CYBER)], [make("n1", EXPORT)], section)
    d = res.older[0]
    assert d.label != "removed", (which, d)
    assert d.label in ("merged", "uncertain")
    if d.label == "merged":
        assert d.evidence.quote and quote_in_chunk(d.evidence.quote, section)


def test_short_generic_terms_and_sentence_fragments_cannot_rescue_a_removed_item():
    section = _guard_section("China. Customers. Cybersecurity incidents could disrupt our operations.")
    res = align([make("o1", CYBER)], [make("n1", EXPORT)], section)
    assert res.older[0].label == "removed"


def test_an_item_whose_text_is_nowhere_in_the_section_is_removed():
    res = align([make("o1", CYBER)], [make("n1", EXPORT)], _guard_section(make("z", AI_REG)["text"]))
    assert res.older[0].label == "removed"


def test_text_check_verified_quotes_are_verbatim_and_span_offsets_are_correct():
    older = [make("o1", EXPORT), make("o2", CUSTOMERS), make("o3", TAX)]
    absorber = (EXPORT[0], EXPORT[1] + " " + CUSTOMERS[1].replace("could", "might"))
    newer = [make("n1", absorber)]
    section = "Risk Factors Summary\n" + section_of(*newer)
    res = align(older, newer, section)
    seen = 0
    for d in res.older:
        if d.evidence.quote:
            seen += 1
            assert quote_in_chunk(d.evidence.quote, section)
            if d.evidence.quote_span:
                a, b = d.evidence.quote_span
                assert section[a:b] == d.evidence.quote
    assert seen >= 2


def test_one_probe_hit_is_uncertain_and_two_probe_hits_are_merged():
    other = make("n1", EXPORT)
    one = _guard_section(CYBER[0])
    res = align([make("o1", CYBER)], [other], one)
    d = res.older[0]
    assert d.label == "uncertain" and d.decided_by == "uncertain" and d.matched_newer_id is None
    assert d.evidence.hit_terms == (CYBER[0],) and len(d.evidence.search_terms) == 3
    assert d.evidence.quote and quote_in_chunk(d.evidence.quote, one)
    longest = sorted(re.split(r"(?<=[.])\s+", CYBER[1]), key=len, reverse=True)[0]
    two = _guard_section(CYBER[0] + "\n\n" + longest)
    res = align([make("o1", CYBER)], [other], two)
    d = res.older[0]
    assert d.label == "merged" and d.decided_by == "text_check" and len(d.evidence.hit_terms) == 2


def test_an_item_with_a_single_probe_needs_a_verbatim_hit_to_be_merged():
    sentence = "Regulators in several jurisdictions may impose new requirements on our data centre products worldwide."
    lone = make("o1", ("", sentence), kind="paragraph")
    other = make("n1", EXPORT)
    verbatim = _guard_section(sentence)
    d = align([lone], [other], verbatim).older[0]
    assert d.label == "merged" and d.evidence.quote_score == 100.0
    edited = _guard_section(sentence.replace("several", "many").replace("may impose", "could adopt"))
    d = align([lone], [other], edited).older[0]
    assert d.label == "uncertain" and d.evidence.quote_score < 100.0 and len(d.evidence.hit_terms) == 1


B1 = "Our insurance coverage may not be sufficient to cover all of the losses that we could incur as a result of any of these events or incidents at any of our facilities."
B2 = "Any of these factors could materially and adversely affect our business, financial condition, results of operations and prospects in ways that are difficult to predict."
U_HEAD = "Water shortages at our assembly sites could interrupt production for extended periods."
U_SENT = "Drought conditions in the regions where our contractors operate have reduced water allocations."


def test_boilerplate_sentences_are_ignored_as_probes():
    older = [make("o1", (U_HEAD, f"{U_SENT} {B1} {B2}"))]
    newer = [make("n1", CYBER)]
    section = section_of(*newer) + f"\n\n{B1} {B2}"      # the generic sentences recur elsewhere in the filing
    rescued = align(older, newer, section)
    assert rescued.older[0].label == "merged"          # two generic sentences would falsely rescue the item
    ignored = align(older, newer, section, boilerplate_sentences={B1, B2.upper()})
    d = ignored.older[0]
    assert d.label == "removed"
    assert d.evidence.search_terms == (U_HEAD, U_SENT)


def test_a_weak_pairing_is_rejected_even_when_it_is_the_only_candidate():
    o, n = make("o1", SUPPLY, kind="paragraph"), make("n1", CYBER, kind="paragraph")
    for embed in (None, cos_pair(o["text"], n["text"], 0.15)):
        res = align([o], [n], n["text"], embed=embed)
        d = res.older[0]
        assert d.matched_newer_id is None and d.label == "removed"
        assert res.newer[0].label == "new"


def test_lexical_score_is_symmetric_word_level_difflib():
    a = tuple("we depend on a single foundry partner for wafers".split())
    b = tuple("we depend on one foundry partner only for wafers today".split())
    expected = 0.5 * (SequenceMatcher(None, a, b, autojunk=False).ratio()
                      + SequenceMatcher(None, b, a, autojunk=False).ratio())
    assert at.lex_exact(a, b) == pytest.approx(expected)
    assert at.lex_exact(a, b) == pytest.approx(at.lex_exact(b, a))
    assert at.lex_exact(a, a) == 1.0
    assert at.lex_exact(a, tuple("zzz yyy".split())) == 0.0
    assert at.lex_exact((), a) == 0.0


def test_lexical_prefilter_is_a_pure_speedup():
    rng = np.random.default_rng(3)
    vocab = [f"w{i}" for i in range(25)]
    toks = [tuple(rng.choice(vocab, size=rng.integers(8, 30))) for _ in range(14)]
    floor = 0.30
    matrix = al._lex_matrix(toks[:7], toks[7:], floor)
    for i, a in enumerate(toks[:7]):
        for j, b in enumerate(toks[7:]):
            exact = at.lex_exact(a, b)
            if np.isnan(matrix[i, j]):
                assert exact < floor
            else:
                assert matrix[i, j] == pytest.approx(exact)


# --------------------------------------------------------------------------
# adjudication
# --------------------------------------------------------------------------

def _uncertain_pair(older_pair=SUPPLY, newer_pair=None, cos=0.75):
    newer_pair = newer_pair or (
        "Dependence on outsourced chip fabrication concentrates operational exposure",
        "Most silicon is produced by external plants in one region. Shortages or shutdowns there would delay revenue. "
        "Replacement vendors take a long time to certify, so buffers are limited.")
    o, n = make("o1", older_pair), make("n1", newer_pair)
    return align([o], [n], n["text"], embed=cos_pair(o["text"], n["text"], cos))


@pytest.mark.parametrize("verdict,label", [("same", "unchanged"), ("reworded", "reworded")])
def test_adjudication_same_or_reworded_pairs_the_candidate(verdict, label):
    res = _uncertain_pair()
    out = apply_adjudication(res, {"o1": verdict})
    d = out.older[0]
    assert (d.label, d.decided_by, d.matched_newer_id) == (label, "llm", "n1")
    n = out.newer[0]
    assert (n.label, n.matched_older_id, n.decided_by) == ("carried", "o1", "llm")
    assert res.older[0].label == "uncertain"          # the input result is untouched


def test_adjudication_removed_without_a_text_hit_is_removed_and_the_candidate_becomes_new():
    res = _uncertain_pair()
    assert res.older[0].evidence.quote is None
    out = apply_adjudication(res, {"o1": "removed"})
    d = out.older[0]
    assert (d.label, d.decided_by, d.matched_newer_id) == ("removed", "llm", None)
    assert d.evidence.search_terms                    # the absence check was performed and is recorded
    assert (out.newer[0].label, out.newer[0].matched_older_id) == ("new", None)


def test_adjudication_cannot_overrule_the_text_check_false_drop_guard():
    absorber = ("Consolidated commentary on foundry dependence and export licensing",
                SUPPLY[1] + " " + EXPORT[1] + " " + CUSTOMERS[1])
    o, n = make("o1", SUPPLY), make("n1", absorber)
    res = align([o], [n], n["text"], embed=cos_pair(o["text"], n["text"], 0.75))
    d = res.older[0]
    assert d.label == "uncertain" and d.evidence.quote and len(d.evidence.hit_terms) == 2
    out = apply_adjudication(res, {"o1": "removed"})
    g = out.older[0]
    assert g.label == "merged" and g.decided_by == "text_check"
    assert g.evidence.quote == d.evidence.quote
    assert out.newer[0].label == "carried"                     # it holds the absorbed text


def test_adjudication_removed_is_blocked_by_a_single_probe_hit():
    partial = ("Consolidated commentary on foundry dependence",
               "Unrelated words about gardens and orchids fill this paragraph for length and nothing more. " + SUPPLY[0])
    o, n = make("o1", SUPPLY), make("n1", partial)
    res = align([o], [n], n["text"], embed=cos_pair(o["text"], n["text"], 0.75))
    d = res.older[0]
    assert d.label == "uncertain" and len(d.evidence.hit_terms) == 1
    out = apply_adjudication(res, {"o1": "removed"})
    assert out.older[0].label == "uncertain"                   # a text hit is never overruled into `removed`


def test_adjudication_validates_its_input():
    res = _uncertain_pair()
    with pytest.raises(ValueError, match="unknown"):
        apply_adjudication(res, {"zzz": "same"})
    with pytest.raises(ValueError, match="verdict"):
        apply_adjudication(res, {"o1": "maybe"})
    done = apply_adjudication(res, {"o1": "same"})
    with pytest.raises(ValueError, match="uncertain"):
        apply_adjudication(done, {"o1": "removed"})
    assert apply_adjudication(res, {}) == res
    no_candidate = align([make("o1", ("", "Not applicable."), kind="paragraph")], [], "")
    with pytest.raises(ValueError, match="candidate"):
        apply_adjudication(no_candidate, {"o1": "same"})


def test_summarize_counts_labels_and_deciders():
    older = [make("o1", SUPPLY), make("o2", EXPORT), make("o3", CYBER), make("o4", CUSTOMERS)]
    edited = (EXPORT[0], EXPORT[1].replace("These rules are complex", "These regulations are intricate"))
    newer = [make("n1", SUPPLY), make("n2", edited), make("n3", AI_REG)]
    res = align(older, newer, section_of(*newer))
    s = summarize(res)
    assert s["older"] == {"total": 4, "unchanged": 1, "reworded": 1, "merged": 0, "removed": 2, "uncertain": 0}
    assert s["newer"] == {"total": 3, "carried": 2, "new": 1, "uncertain": 0}
    assert s["decided_by"]["hash"] == 1 and s["decided_by"]["headline"] == 1 and s["decided_by"]["text_check"] == 2


# --------------------------------------------------------------------------
# real excerpts: present risks must not be marked removed, removed ones must be
# --------------------------------------------------------------------------

PRIV_FY25 = "These state laws allow for statutory fines for noncompliance. For example, the California Consumer Privacy Act of 2018, as amended by the California Privacy Rights Act of 2020, or CPRA, or collectively the CCPA, gives California residents the right to access, delete and opt-out of certain sharing of their personal information, and to receive detailed information about how it is used and shared. The CCPA provides for substantial fines for intentional violation and the law created a private right of action for certain data breaches."
PRIV_FY26 = PRIV_FY25          # verified identical in the FY26 filing
NAC_FY25 = "The USG evaluates license requests in a closed process that does not have clear standards or an opportunity for review. For example, the Notified Advanced Computing, or “NAC,” process has not resulted in approvals for exports of products to customers in China. The license process for exports to D1 and D4 countries has been time-consuming and resulted in license conditions that are onerous, even for small-sized systems that are not able to train frontier AI models."
EXPORT_FY26 = "The licensing process may not be resolved before significant business opportunities evaporate. Even if the USG grants any requested licenses, the licenses have already and may in the future be temporary, impose burdensome conditions regarding the installation, maintenance, and use of such products, or include financial or economic requirements that we or our customers or end users cannot or choose not to fulfill."


def _fy26_excerpt_section():
    return "\n\n".join([EXPORT_FY26, PRIV_FY26, TAX_FY26, SCRUTINY_H26, SCRUTINY_BODY])


def test_real_privacy_text_present_in_the_newer_section_is_never_removed():
    """The audited failure: text still disclosed in FY26 but missed by item segmentation."""
    older = [make("o1", ("", PRIV_FY25), kind="paragraph")]
    res = align(older, [], _fy26_excerpt_section())          # newer items list lost it
    d = res.older[0]
    assert d.label == "merged" and d.decided_by == "text_check"
    assert "statutory fines" in d.evidence.quote or "CCPA" in d.evidence.quote
    assert quote_in_chunk(d.evidence.quote, _fy26_excerpt_section())


def test_real_privacy_text_present_as_a_newer_item_is_unchanged():
    o, n = make("o1", ("", PRIV_FY25), kind="paragraph"), make("n1", ("", PRIV_FY26), kind="paragraph")
    res = align([o], [n], _fy26_excerpt_section())
    assert res.older[0].label == "unchanged"


def test_real_nac_text_absent_from_the_newer_section_is_removed_with_terms_recorded():
    older = [make("o1", ("", NAC_FY25), kind="paragraph")]
    res = align(older, [make("n1", ("", EXPORT_FY26), kind="paragraph")], _fy26_excerpt_section())
    d = res.older[0]
    assert (d.label, d.decided_by) == ("removed", "text_check")
    assert len(d.evidence.search_terms) == 2
    assert any("Notified Advanced Computing" in t for t in d.evidence.search_terms)
    assert any("D1 and D4" in t for t in d.evidence.search_terms)
    assert res.newer[0].label == "new"


# --------------------------------------------------------------------------
# edge cases and internals
# --------------------------------------------------------------------------

def test_uncertain_ids_lists_the_items_awaiting_adjudication():
    assert _uncertain_pair().uncertain_ids == ("o1",)
    assert align([], [], "").uncertain_ids == ()


def test_the_body_is_the_text_after_the_headline_even_when_whitespace_differs():
    wrapped = {"item_id": "o1", "headline": "Alpha   headline\nwrapped", "text": "Alpha headline wrapped Body one. Body two."}
    assert al._prepare([wrapped], "older")[0].body == "Body one. Body two."
    not_a_prefix = {"item_id": "o1", "headline": "Something else", "text": "Some other text."}
    assert al._prepare([not_a_prefix], "older")[0].body == "Some other text."


def test_normalisation_offsets_map_every_character_back_to_the_source():
    messy = "  It’s   a “Quoted”\n\nLine  ONE.  "
    norm, offsets = at.norm_with_offsets(messy)
    assert norm == at.norm(messy) and len(offsets) == len(norm)
    for ch, src in zip(norm, offsets):
        assert ch == " " or messy[src].lower() == ch


def test_lexical_matrix_handles_empty_sides_and_the_embedder_shape_is_validated():
    assert al._lex_matrix([], [("a", "b")], 0.3).shape == (0, 1)
    o, n = make("o1", SUPPLY), make("n1", CYBER)
    with pytest.raises(ValueError, match="one row per text"):
        align([o], [n], n["text"], embed=lambda texts: np.zeros((1, 4)))
    with pytest.raises(TypeError):
        align([], [], None)


def test_equal_bodies_without_headlines_are_unchanged_even_if_the_supplied_hashes_differ():
    o = {"item_id": "o1", "text": SUPPLY[1], "text_hash": "hash-a", "unit_kind": "paragraph"}
    n = {"item_id": "n1", "text": SUPPLY[1], "text_hash": "hash-b", "unit_kind": "paragraph"}
    d = align([o], [n], SUPPLY[1]).older[0]
    assert (d.label, d.decided_by) == ("unchanged", "hash")


def test_probes_are_unique_specific_and_ordered_longest_first():
    sentence = "Regulators in several jurisdictions may impose new requirements on our data centre products."
    body = f"{sentence} {sentence} Short one. {SUPPLY[1]}"
    item = al._prepare([make("o1", (SUPPLY[0], body))], "older")[0]
    probes = al._probes(item, frozenset(), AlignParams())
    assert probes[0] == SUPPLY[0] and len(probes) == 3
    assert len({at.norm(p) for p in probes}) == 3
    assert all(len(p) >= 40 for p in probes)
    assert len(probes[1]) >= len(probes[2])


def test_a_blank_needle_never_hits():
    assert at.SectionIndex("some section text").probe("   ", 85.0, 100) is None


def test_the_quote_falls_back_to_the_matched_range_inside_a_giant_sentence():
    giant = "word " * 400
    index = at.SectionIndex(f"Intro. {giant}the distinctive phrase sits deep inside this giant sentence without a period {giant}")
    hit = index.probe("the distinctive phrase sits deep inside this giant sentence without a period", 85.0, 120)
    assert hit and len(hit.quote) <= 120 and "distinctive phrase" in hit.quote
    assert index.text[hit.span[0]:hit.span[1]] == hit.quote


def test_partner_quote_is_absent_when_the_newer_text_is_not_in_the_section():
    edited = (SUPPLY[0], SUPPLY[1].replace("Taiwan", "Korea"))
    res = align([make("o1", SUPPLY)], [make("n1", edited)], "a completely unrelated section text")
    d = res.older[0]
    assert d.label == "reworded" and d.evidence.quote is None


def test_the_dropped_sentence_audit_can_be_disabled_or_capped():
    edited = (SUPPLY[0], "Entirely new first sentence about wafer allocation and pricing policies at our suppliers. "
                         "Entirely new second sentence about facility outages and regional disruptions to shipping. "
                         + SUPPLY[1].split(". ", 2)[2])
    o, n = make("o1", SUPPLY), make("n1", edited)
    full = align([o], [n], n["text"]).older[0].evidence.dropped_sentences
    assert len(full) == 2
    assert align([o], [n], n["text"], params=AlignParams(max_dropped_sentences=0)).older[0].evidence.dropped_sentences == ()
    assert len(align([o], [n], n["text"], params=AlignParams(max_dropped_sentences=1)).older[0].evidence.dropped_sentences) == 1


def test_a_newer_item_with_one_probe_in_the_older_section_is_uncertain():
    n = make("n1", CYBER)
    older_text = section_of(make("x", SUPPLY)) + "\n\n" + CYBER[0]
    res = align([make("o1", SUPPLY)], [n], n["text"], older_section_text=older_text)
    d = res.newer[0]
    assert d.label == "uncertain" and d.matched_older_id is None and len(d.evidence.hit_terms) == 1


CORPUS = [SUPPLY, EXPORT, CUSTOMERS, CYBER, TAX, TALENT, AI_REG]


def _random_scenario(seed):
    rng = np.random.default_rng(seed)
    older_pairs = [CORPUS[k] for k in rng.choice(len(CORPUS), size=rng.integers(1, 7))]      # may repeat
    newer_pairs = []
    for k in rng.choice(len(CORPUS), size=rng.integers(1, 7)):
        headline, body = CORPUS[k]
        mode = int(rng.integers(0, 5))
        if mode == 1:
            body = body.replace(" the ", " a ").replace("could", "might")
        elif mode == 2:
            headline = "Reworded heading about " + headline.lower()
        elif mode == 3:
            body = body + " " + CORPUS[int(rng.integers(0, len(CORPUS)))][1]                # absorbs another body
        newer_pairs.append((headline, body))
    older = [make(f"o{i}", p) for i, p in enumerate(older_pairs)]
    newer = [make(f"n{i}", p) for i, p in enumerate(newer_pairs)]
    return older, newer


@pytest.mark.parametrize("embed", [None, bow_embed])
def test_invariants_hold_on_random_scenarios(embed):
    for seed in range(40):
        older, newer = _random_scenario(seed)
        section = section_of(*newer)
        res = align(older, newer, section, embed=embed)
        assert [d.item_id for d in res.older] == [o["item_id"] for o in older]
        assert [d.item_id for d in res.newer] == [n["item_id"] for n in newer]
        claimed = [d.matched_newer_id for d in res.older
                   if d.label in ("unchanged", "reworded") or (d.label == "uncertain" and d.matched_newer_id)]
        assert len(claimed) == len(set(claimed)), (seed, claimed)                    # one-to-one
        n_by_id = by_id(res.newer)
        for d in res.older:
            assert d.label in al.OLDER_LABELS and d.decided_by in al.DECIDED_BY
            if d.label in ("unchanged", "reworded", "merged") and d.matched_newer_id:
                assert n_by_id[d.matched_newer_id].matched_older_id is not None
            if d.label == "removed":                                                # the guard
                assert d.evidence.hit_terms == () and d.evidence.search_terms, (seed, d)
            if d.evidence.quote:
                assert quote_in_chunk(d.evidence.quote, section)
        for n in res.newer:
            assert n.label in al.NEWER_LABELS and n.decided_by in al.DECIDED_BY
        assert res == align(older, newer, section, embed=embed)                      # deterministic


# --------------------------------------------------------------------------
# performance shape: <= ~60 items per side, ~100k-char section
# --------------------------------------------------------------------------

def _random_item(rng, item_id, n_sentences=9, vocab=None):
    words = vocab
    sentences = [" ".join(rng.choice(words, size=rng.integers(14, 26))) + "." for _ in range(n_sentences)]
    sentences = [s[0].upper() + s[1:] for s in sentences]
    headline, body = sentences[0], " ".join(sentences[1:])
    return make(item_id, (headline, body))


def test_a_full_size_pair_aligns_quickly():
    rng = np.random.default_rng(7)
    vocab = np.array([f"w{i:04d}x" for i in range(3000)])
    older = [_random_item(rng, f"o{i}", vocab=vocab) for i in range(60)]
    newer = [older[i] | {"item_id": f"n{i}"} for i in range(0, 30)] + \
            [_random_item(rng, f"n{i}", vocab=vocab) for i in range(30, 60)]
    section = section_of(*newer)
    start = time.perf_counter()
    res = align(older, newer, section, embed=bow_embed)
    elapsed = time.perf_counter() - start
    assert elapsed < 60
    s = summarize(res)
    assert s["older"]["unchanged"] == 30 and s["older"]["total"] == 60
    assert s["older"]["removed"] + s["older"]["merged"] + s["older"]["uncertain"] + s["older"]["reworded"] == 30


# --------------------------------------------------------------------------
# whole real sections (skipped when the local data lake is absent)
# --------------------------------------------------------------------------

PARQUET = Path(__file__).resolve().parents[1] / "data" / "interim" / "section_texts" / "nvda_section_texts.parquet"
FY25_ACC, FY26_ACC = "0001045810-25-000023", "0001045810-26-000021"


@pytest.fixture(scope="module")
def nvda_sections():
    if not PARQUET.exists():
        pytest.skip("local data lake (nvda_section_texts.parquet) not present")
    import pandas as pd
    df = pd.read_parquet(PARQUET)
    pick = lambda acc: df[(df.accession_no == acc) & (df.section_id == "I.1A")].text.iloc[0]
    return pick(FY25_ACC), pick(FY26_ACC)


def _units(section_text, prefix):
    rows = [ln.strip() for ln in section_text.split("\n") if len(ln.strip()) >= 60]
    return [{"item_id": f"{prefix}{i:03d}", "headline": "", "text": t, "text_hash": content_hash(t),
             "unit_kind": "paragraph"} for i, t in enumerate(rows)]


def test_real_nvda_privacy_and_nac_against_the_full_fy26_section(nvda_sections):
    fy25, fy26 = nvda_sections
    older = [make("priv", ("", PRIV_FY25), kind="paragraph"), make("nac", ("", NAC_FY25), kind="paragraph")]
    res = align(older, [], fy26)
    o = by_id(res.older)
    assert o["priv"].label == "merged" and quote_in_chunk(o["priv"].evidence.quote, fy26)
    assert o["nac"].label == "removed" and o["nac"].evidence.search_terms


def test_real_nvda_paragraph_units_fy25_to_fy26(nvda_sections):
    fy25, fy26 = nvda_sections
    older, newer = _units(fy25, "a"), _units(fy26, "b")
    start = time.perf_counter()
    res = align(older, newer, fy26, embed=bow_embed, older_section_text=fy25)
    elapsed = time.perf_counter() - start
    s = summarize(res)
    assert elapsed < 120
    assert s["older"]["total"] == len(older) and s["newer"]["total"] == len(newer)
    fy26_norm = at.norm(fy26)
    for d in res.older:
        if d.label == "removed":                      # independent check of the guarantee
            unit = next(u for u in older if u["item_id"] == d.item_id)
            assert at.norm(unit["text"])[:80] not in fy26_norm
            assert d.evidence.search_terms
    kept = s["older"]["unchanged"] + s["older"]["reworded"]
    assert kept >= 0.8 * len(older)
    # the FY25 paragraph holding the NAC sentences survives as a (longer) FY26 paragraph:
    # item-level alignment cannot call it removed, but the audit trail records the dropped sentences
    nac_unit = next(u for u in older if "Notified Advanced Computing" in u["text"])
    d = by_id(res.older)[nac_unit["item_id"]]
    assert d.label == "reworded" and d.decided_by == "body"
    dropped = d.evidence.dropped_sentences
    assert any("Notified Advanced Computing" in sentence for sentence in dropped)      # the audited NAC text
    assert any("we transitioned some operations" in sentence for sentence in dropped)  # the audited Hong Kong text
    assert all(at.norm(sentence) not in fy26_norm for sentence in dropped)
    # without an embedding that heavily edited 26k-char paragraph (lexical 0.72) is left to adjudication
    lexical_only = by_id(align(older, newer, fy26).older)[nac_unit["item_id"]]
    assert lexical_only.label == "uncertain" and lexical_only.matched_newer_id == d.matched_newer_id


SCRUTINY_LINES = ("Scrutiny from shareholders", "Certain shareholder advocacy groups")


def _drop_lines(text, prefixes):
    return "\n".join(ln for ln in text.split("\n") if not ln.strip().startswith(prefixes))


def test_real_nvda_paragraph_hidden_from_the_items_but_present_in_the_section_is_not_removed(nvda_sections):
    fy25, fy26 = nvda_sections
    older = _units(fy25, "a")
    newer = [u for u in _units(fy26, "b") if not u["text"].startswith(SCRUTINY_LINES)]   # segmentation loss
    res = align(older, newer, fy26)
    victims = [u["item_id"] for u in older if u["text"].startswith(("Increased scrutiny", SCRUTINY_LINES[1]))]
    assert len(victims) == 2
    for item_id in victims:
        d = by_id(res.older)[item_id]
        assert d.label in ("merged", "uncertain") and d.label != "removed"
        assert d.evidence.quote and quote_in_chunk(d.evidence.quote, fy26)


def test_real_nvda_paragraph_deleted_from_the_newer_section_is_removed(nvda_sections):
    fy25, fy26 = nvda_sections
    older = _units(fy25, "a")
    cut = _drop_lines(fy26, SCRUTINY_LINES)
    newer = [u for u in _units(fy26, "b") if not u["text"].startswith(SCRUTINY_LINES)]
    res = align(older, newer, cut, older_section_text=fy25)
    victims = [u["item_id"] for u in older if u["text"].startswith(("Increased scrutiny", SCRUTINY_LINES[1]))]
    for item_id in victims:
        d = by_id(res.older)[item_id]
        assert d.label == "removed" and d.decided_by == "text_check" and d.evidence.search_terms
    assert summarize(res)["older"]["removed"] == 2
