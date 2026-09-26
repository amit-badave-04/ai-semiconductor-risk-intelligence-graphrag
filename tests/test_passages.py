"""Pure tests for the passage layer (M1b plan L.2): the change layer below the item.

Synthetic sections cover every branch of the classification (present incl. tense-only edits,
reworded, removed, added, grouping, cap split, short sentences, chunk mapping, ignored decisions,
determinism, validation). Sentence pools are chosen so that no two unrelated sentences are near each
other (checked by ``test_fixture_pools_behave_as_designed``). The last classes run the real
Nvidia FY25 -> FY26 Item 1A pair (plus the other development pairs) and skip when the local data lake
(``data/interim/risk_items``, ``data/interim/section_texts``) is absent.
"""

import dataclasses
import itertools
import random
import time
from collections.abc import Sequence
from difflib import SequenceMatcher
from pathlib import Path

import pytest

from semigraph.extraction.gates import quote_in_chunk
from semigraph.graph import align_text as at
from semigraph.graph.alignment import (
    AlignmentResult,
    AlignParams,
    Evidence,
    NewerDecision,
    OlderDecision,
    align,
)
from semigraph.graph.passages import (
    KIND_CODE,
    Passage,
    PassageParams,
    compute_passages,
    summarize_passages,
)

# --------------------------------------------------------------------------
# sentence pools
# --------------------------------------------------------------------------

P = (                                                        # present in both filings, unchanged
    "Malicious actors regularly attempt to breach our networks and steal confidential design information.",
    "Changes in international tax law may increase our effective rate and reduce net income in future years.",
    "Competition for experienced chip designers is intense and equity awards may lose retention value quickly.",
    "Our wafers are fabricated by a single foundry located in Taiwan and capacity is allocated at its discretion.",
    "Any failure to protect our intellectual property could allow competitors to copy our architectures without compensation.",
)
X = (                                                        # only in the older filing
    "Severe weather and earthquakes near our suppliers could interrupt component deliveries for several quarters.",
    "Patent infringement claims by competitors could require costly settlements or force product redesigns.",
    "Fluctuations in foreign currency exchange rates may adversely affect reported revenue from overseas sales.",
    "Our board has authorized a share repurchase program whose execution depends on market conditions and cash needs.",
    "Labor disputes at ports and shipping companies could delay delivery of finished products to distributors.",
)
Y = (                                                        # only in the newer filing
    "New state laws regulating artificial intelligence took effect in January and may restrict how we train models.",
    "Tariffs imposed on imported semiconductors could increase our costs and reduce demand from price sensitive buyers.",
    "A ransomware incident at one of our logistics partners disrupted shipping schedules for several weeks.",
)
TENSE_OLD = "These restrictions impact exports of certain advanced chips to customers in China and other regions."
TENSE_NEW = "These restrictions impacted exports of certain advanced chips to customers in China and other regions."
REWORDED = (                                                 # (older wording, newer wording): edited, not identical
    ("A small number of large customers account for a significant portion of our revenue and the loss of any one could harm results.",
     "A small number of large cloud customers account for a substantial share of our revenue and losing any one of them could harm our results."),
    ("Governments continue to consider global minimum tax reforms that may raise our cash tax payments in coming years.",
     "Several governments are now weighing global minimum tax reforms which could significantly raise our cash tax payments over coming years."),
    ("Long lead times for advanced packaging capacity make it difficult to respond to sudden changes in demand.",
     "Extended lead times for advanced packaging capacity make it difficult for us to respond quickly to sudden swings in demand for our products."),
)
SHORT = "Supply chain risks."                                # under 40 characters


def filing(prefix: str, items: Sequence[Sequence[str]]) -> tuple[str, list[dict]]:
    """A section text plus item rows whose text is a slice of it starting at ``char_start``."""
    section, rows = "", []
    for k, sentences in enumerate(items):
        if k:
            section += "\n\n"
        text = " ".join(sentences)
        rows.append({"item_id": f"{prefix}{k}", "text": text, "char_start": len(section)})
        section += text
    return section, rows


def decisions(older: dict[str, str], newer: dict[str, tuple[str, str | None]]) -> AlignmentResult:
    return AlignmentResult(
        tuple(OlderDecision(i, label, None, "body", Evidence()) for i, label in older.items()),
        tuple(NewerDecision(i, label, partner, "body", Evidence()) for i, (label, partner) in newer.items()),
        AlignParams())


# The starting values the behavioural tests below were written for; the shipped defaults were tuned later on the frozen gold.
LEGACY = dict(present_min_ratio=85.0, reword_min=0.60, max_passage_chars=1200, decompose_uncertain=False,
              suppress_added_with_counterpart=False)


def run(older_items: Sequence[Sequence[str]], newer_items: Sequence[Sequence[str]],
        older_labels: dict[str, str], newer_labels: dict[str, tuple[str, str | None]],
        *, o_spans=(), n_spans=(), **params) -> tuple[Passage, ...]:
    so, o_rows = filing("o", older_items)
    sn, n_rows = filing("n", newer_items)
    return compute_passages(o_rows, n_rows, decisions(older_labels, newer_labels), so, sn, o_spans, n_spans,
                            PassageParams(**{**LEGACY, **params}))


def kinds(passages: Sequence[Passage]) -> list[str]:
    return [p.kind for p in passages]


REWORDED_OLD = {"o0": "reworded"}
CARRIED_NEW = {"n0": ("carried", "o0")}


PAD = "Filler text without any relevance to the risks discussed elsewhere in this synthetic section."


def _near(needle: str, other: str) -> bool:
    """Would ``needle`` be PRESENT in a section that contains ``other`` (padded so it is the longer text)?"""
    return at.SectionIndex(f"{other} {PAD}").probe(needle, 85.0, 400) is not None


def test_fixture_pools_behave_as_designed():
    """The tests below rely on: unrelated sentences never match, tense edits are present, rewordings are not."""
    pool = list(P + X + Y) + [TENSE_OLD, TENSE_NEW] + [a for a, _ in REWORDED] + [b for _, b in REWORDED]
    twins = {frozenset(p) for p in REWORDED} | {frozenset((TENSE_OLD, TENSE_NEW))}
    bad = [(a[:40], b[:40]) for a, b in itertools.permutations(pool, 2)
           if (_near(a, b) or at.lex_exact(at.word_tokens(a), at.word_tokens(b)) >= 0.6)
           and frozenset((a, b)) not in twins]
    assert not bad
    assert _near(TENSE_OLD, TENSE_NEW) and _near(TENSE_NEW, TENSE_OLD)
    for old, new in REWORDED:
        assert not _near(old, new) and not _near(new, old)
        assert at.lex_exact(at.word_tokens(old), at.word_tokens(new)) >= 0.6
    assert all(len(s) >= 40 for s in pool) and len(SHORT) < 40


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def test_every_kind_in_one_item_pair_and_tense_only_edits_are_not_reported():
    older = [[P[0], TENSE_OLD, X[0], REWORDED[0][0], P[1]]]
    newer = [[P[0], TENSE_NEW, REWORDED[0][1], P[1], Y[0]]]
    passages = run(older, newer, REWORDED_OLD, CARRIED_NEW)
    assert [(p.kind, p.item_id, p.seq) for p in passages] == [
        ("removed", "o0", 0), ("reworded", "o0", 0), ("added", "n0", 0)]
    removed, reworded, added = passages
    assert removed.text == X[0] and removed.decided_by == "sentence_absent"
    assert removed.counterpart_text is None and removed.counterpart_span is None and removed.similarity is None
    assert reworded.text == REWORDED[0][0] and reworded.decided_by == "sentence_reworded"
    assert reworded.counterpart_text == REWORDED[0][1]
    assert reworded.similarity == pytest.approx(at.lex_exact(at.word_tokens(REWORDED[0][0]),
                                                             at.word_tokens(REWORDED[0][1])))
    assert added.text == Y[0] and added.decided_by == "sentence_absent" and added.counterpart_text is None
    assert [p.passage_id for p in passages] == ["o0:r000", "o0:w000", "n0:a000"]
    assert not any(TENSE_OLD in p.text or TENSE_NEW in p.text for p in passages)


def test_texts_and_counterparts_are_verbatim_slices_of_their_own_section():
    older = [[P[0], X[0]], [X[1], REWORDED[1][0], P[2]]]
    newer = [[P[0], Y[1]], [REWORDED[1][1], P[2], Y[2]]]
    so, o_rows = filing("o", older)
    sn, n_rows = filing("n", newer)
    res = decisions({"o0": "reworded", "o1": "merged"}, {"n0": ("carried", "o0"), "n1": ("carried", "o1")})
    passages = compute_passages(o_rows, n_rows, res, so, sn, (), (), PassageParams(**LEGACY))
    assert len(passages) == 5
    for p in passages:
        own = sn if p.kind == "added" else so
        other = so if p.kind == "added" else sn
        assert own[p.char_start:p.char_end] == p.text
        assert quote_in_chunk(p.text, own)
        if p.kind == "reworded":
            a, b = p.counterpart_span
            assert other[a:b] == p.counterpart_text and quote_in_chunk(p.counterpart_text, other)


def test_a_section_offset_is_item_start_plus_the_offset_inside_the_item():
    older = [[P[0]], [P[1], X[2], P[2]]]
    passages = run(older, [[P[0]], [P[1], P[2]]], {"o1": "reworded"}, {"n1": ("carried", "o1")})
    so, o_rows = filing("o", older)
    (removed,) = passages
    assert removed.char_start == o_rows[1]["char_start"] + len(P[1]) + 1
    assert removed.char_end == removed.char_start + len(X[2])


def test_a_sentence_reworded_elsewhere_in_the_section_is_reworded_not_removed():
    older = [[P[0], REWORDED[2][0]]]
    newer = [[P[0]], [P[1], REWORDED[2][1]]]                 # the counterpart lives in ANOTHER newer item
    (passage,) = run(older, newer, REWORDED_OLD, {"n0": ("carried", "o0"), "n1": ("new", None)})
    assert passage.kind == "reworded" and passage.counterpart_text == REWORDED[2][1]


def test_near_verbatim_text_moved_to_another_item_is_present_not_removed():
    older = [[P[0], X[0]]]
    newer = [[P[0]], [P[1], X[0]]]
    assert run(older, newer, REWORDED_OLD, {"n0": ("carried", "o0"), "n1": ("new", None)}) == ()


def test_removed_needs_no_counterpart_above_the_reword_threshold():
    older = [[P[0], REWORDED[0][0]]]
    newer = [[P[0], REWORDED[0][1]]]
    (loose,) = run(older, newer, REWORDED_OLD, CARRIED_NEW)
    assert loose.kind == "reworded"
    strict = run(older, newer, REWORDED_OLD, CARRIED_NEW, reword_min=0.99)
    assert kinds(strict) == ["removed", "added"] and all(p.similarity is None for p in strict)


def test_present_threshold_is_a_parameter():
    older, newer = [[P[0], TENSE_OLD]], [[P[0], TENSE_NEW]]
    assert run(older, newer, REWORDED_OLD, CARRIED_NEW) == ()
    strict = run(older, newer, REWORDED_OLD, CARRIED_NEW, present_min_ratio=100.0)
    assert kinds(strict) == ["reworded"] and strict[0].counterpart_text == TENSE_NEW


def test_a_newer_sentence_with_an_older_counterpart_is_not_added():
    older = [[P[0], REWORDED[0][0], REWORDED[1][0]]]
    newer = [[P[0], REWORDED[0][1], REWORDED[1][1], Y[0]]]
    passages = run(older, newer, REWORDED_OLD, CARRIED_NEW)
    assert kinds(passages) == ["reworded", "added"]
    assert passages[1].text == Y[0]


def test_a_variant_of_an_older_sentence_that_is_still_present_is_added():
    """The counterpart older sentence survives verbatim elsewhere, so nothing reports the variant as reworded."""
    old_sentence, variant = REWORDED[0]
    older = [[P[0], old_sentence]]
    newer = [[P[0], old_sentence, variant]]
    passages = run(older, newer, REWORDED_OLD, CARRIED_NEW)
    assert [(p.kind, p.text) for p in passages] == [("added", variant)]


def test_a_variant_of_a_sentence_from_a_removed_older_item_is_added_because_nothing_quotes_it():
    """Its older counterpart lives in an item the passage layer does not decompose (item level owns it)."""
    older = [[P[0]], [REWORDED[0][0], P[1]]]
    newer = [[P[0], REWORDED[0][1]]]
    passages = run(older, newer, {"o0": "reworded", "o1": "removed"}, CARRIED_NEW)
    assert [(p.kind, p.text) for p in passages] == [("added", REWORDED[0][1])]


def test_a_newer_sentence_is_never_both_quoted_as_a_counterpart_and_added():
    older = [[P[0], REWORDED[0][0], REWORDED[1][0], X[0]]]
    newer = [[P[0], REWORDED[0][1], Y[0], REWORDED[1][1]]]
    passages = run(older, newer, REWORDED_OLD, CARRIED_NEW)
    quoted = [p.counterpart_text for p in passages if p.kind == "reworded"]
    added = [p.text for p in passages if p.kind == "added"]
    assert quoted == [REWORDED[0][1], REWORDED[1][1]] and added == [Y[0]]


def test_ties_between_equal_counterparts_go_to_the_earliest_sentence():
    older = [[P[0], REWORDED[0][0]]]
    newer = [[P[0], REWORDED[0][1], P[0], REWORDED[0][1]]]
    reworded, added = run(older, newer, REWORDED_OLD, CARRIED_NEW)
    section = filing("n", newer)[0]
    assert reworded.kind == "reworded" and reworded.counterpart_span[0] == section.index(REWORDED[0][1])
    assert (added.kind, added.text) == ("added", REWORDED[0][1])      # the second copy is quoted by nobody


# --------------------------------------------------------------------------
# which decisions are decomposed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["unchanged", "removed", "uncertain"])
def test_older_items_other_than_reworded_or_merged_are_ignored(label):
    older = [[P[0], X[0], X[1]]]
    newer = [[P[0], Y[0]]]
    assert run(older, newer, {"o0": label}, {"n0": ("new", None)}) == ()


def test_merged_older_items_are_decomposed_too():
    passages = run([[P[0], X[0]]], [[P[0]]], {"o0": "merged"}, {"n0": ("carried", "o0")})
    assert kinds(passages) == ["removed"]


@pytest.mark.parametrize("label", ["new", "uncertain"])
def test_newer_items_that_are_not_carried_are_never_decomposed(label):
    assert run([[P[0]]], [[P[0], Y[0], Y[1]]], {"o0": "removed"}, {"n0": (label, None)}) == ()


def test_a_carried_newer_item_with_an_unchanged_partner_is_skipped_and_one_without_a_partner_is_not():
    older, newer = [[P[0], P[1], P[2]]], [[P[0], P[1]], [P[2], Y[0]]]
    assert run(older, newer, {"o0": "unchanged"}, {"n0": ("carried", "o0"), "n1": ("new", None)}) == ()
    passages = run(older, newer, {"o0": "unchanged"}, {"n0": ("carried", "o0"), "n1": ("carried", None)})
    assert [(p.kind, p.item_id, p.text) for p in passages] == [("added", "n1", Y[0])]


def test_added_passages_come_after_older_side_passages_each_in_filing_order():
    older = [[P[0], X[0]], [P[1], X[1]]]
    newer = [[P[0], Y[0]], [P[1], Y[1]]]
    passages = run(older, newer, {"o0": "reworded", "o1": "reworded"},
                   {"n0": ("carried", "o0"), "n1": ("carried", "o1")})
    assert [(p.kind, p.item_id) for p in passages] == [
        ("removed", "o0"), ("removed", "o1"), ("added", "n0"), ("added", "n1")]


# --------------------------------------------------------------------------
# grouping, cap, short sentences
# --------------------------------------------------------------------------

def test_consecutive_sentences_of_one_kind_form_one_passage_and_a_present_sentence_splits_runs():
    older = [[X[0], X[1], P[0], X[2], X[3], P[1], X[4]]]
    passages = run(older, [[P[0], P[1]]], REWORDED_OLD, CARRIED_NEW)
    assert [p.text for p in passages] == [f"{X[0]} {X[1]}", f"{X[2]} {X[3]}", X[4]]
    assert [p.passage_id for p in passages] == ["o0:r000", "o0:r001", "o0:r002"]
    assert [p.seq for p in passages] == [0, 1, 2]


def test_different_kinds_never_share_a_passage_and_seq_counts_per_kind():
    older = [[X[0], REWORDED[0][0], X[1], REWORDED[1][0]]]
    newer = [[REWORDED[0][1], REWORDED[1][1]]]
    passages = run(older, newer, REWORDED_OLD, CARRIED_NEW)
    assert [(p.passage_id, p.kind) for p in passages] == [
        ("o0:r000", "removed"), ("o0:w000", "reworded"), ("o0:r001", "removed"), ("o0:w001", "reworded")]


def test_a_run_is_split_at_a_sentence_boundary_once_it_exceeds_the_cap():
    older = [[X[0], X[1], X[2], P[0]]]
    cap = len(X[0]) + 1 + len(X[1]) + 5
    passages = run(older, [[P[0]]], REWORDED_OLD, CARRIED_NEW, max_passage_chars=cap)
    assert [p.text for p in passages] == [f"{X[0]} {X[1]}", X[2]]
    assert all(len(p.text) <= cap for p in passages)


def test_a_single_sentence_longer_than_the_cap_stays_whole():
    older = [[X[3], P[0]]]
    (passage,) = run(older, [[P[0]]], REWORDED_OLD, CARRIED_NEW, max_passage_chars=60, min_sentence_chars=40)
    assert passage.text == X[3] and len(passage.text) > 60


def test_short_sentences_are_never_classified():
    assert run([[P[0], SHORT, P[1]]], [[P[0], P[1]]], REWORDED_OLD, CARRIED_NEW) == ()
    assert run([[P[0], P[1]]], [[P[0], SHORT, P[1]]], REWORDED_OLD, CARRIED_NEW) == ()


def test_a_short_sentence_between_two_removed_ones_rides_along_but_never_starts_or_ends_a_passage():
    older = [[SHORT, X[0], SHORT, X[1], SHORT, P[0]]]
    (passage,) = run(older, [[P[0]]], REWORDED_OLD, CARRIED_NEW)
    assert passage.text == f"{X[0]} {SHORT} {X[1]}"


def test_the_minimum_sentence_length_is_a_parameter():
    older = [[P[0], SHORT]]
    assert run(older, [[P[0]]], REWORDED_OLD, CARRIED_NEW) == ()
    (passage,) = run(older, [[P[0]]], REWORDED_OLD, CARRIED_NEW, min_sentence_chars=10)
    assert passage.text == SHORT and passage.kind == "removed"


def test_adjacent_rewordings_share_one_passage_whose_counterpart_is_one_contiguous_slice():
    older = [[P[0], REWORDED[0][0], REWORDED[1][0], P[1]]]
    newer = [[P[0], REWORDED[0][1], REWORDED[1][1], P[1]]]
    (passage,) = run(older, newer, REWORDED_OLD, CARRIED_NEW)
    assert passage.text == f"{REWORDED[0][0]} {REWORDED[1][0]}"
    assert passage.counterpart_text == f"{REWORDED[0][1]} {REWORDED[1][1]}"
    weakest = min(at.lex_exact(at.word_tokens(a), at.word_tokens(b)) for a, b in REWORDED[:2])
    assert passage.similarity == pytest.approx(weakest)


def test_rewordings_whose_counterparts_are_not_adjacent_do_not_share_a_passage():
    older = [[REWORDED[0][0], REWORDED[1][0], P[0]]]
    newer = [[REWORDED[0][1], P[0], REWORDED[1][1]]]
    passages = run(older, newer, REWORDED_OLD, CARRIED_NEW)
    assert [(p.kind, p.text) for p in passages] == [
        ("reworded", REWORDED[0][0]), ("reworded", REWORDED[1][0])]
    reversed_newer = [[REWORDED[1][1], REWORDED[0][1], P[0]]]
    assert kinds(run(older, reversed_newer, REWORDED_OLD, CARRIED_NEW)) == ["reworded", "reworded"]


# --------------------------------------------------------------------------
# chunk ids
# --------------------------------------------------------------------------

def test_chunk_ids_are_the_own_filings_chunks_that_overlap_the_passage_in_text_order():
    older, newer = [[P[0], X[0], X[1], P[1]]], [[P[0], P[1], Y[0]]]
    so, _ = filing("o", older)
    start, end = so.index(X[0]), so.index(X[1]) + len(X[1])
    mid = (start + end) // 2
    o_spans = [("c3", end, len(so)), ("c1", 0, start), ("c2", start, mid), ("c2b", mid, end)]      # unsorted on purpose
    sn, _ = filing("n", newer)
    n_spans = [("n0", 0, sn.index(Y[0]) + 5), ("n1", sn.index(Y[0]) + 5, len(sn))]
    removed, added = run(older, newer, REWORDED_OLD, CARRIED_NEW, o_spans=o_spans, n_spans=n_spans)
    assert (removed.char_start, removed.char_end) == (start, end)
    assert removed.chunk_ids == ("c2", "c2b")                 # touching chunks (end == start) do not overlap
    assert added.chunk_ids == ("n0", "n1")                    # newer passage: newer chunks only, spanning two


def test_no_chunk_spans_or_no_overlap_gives_empty_chunk_ids():
    older, newer = [[P[0], X[0]]], [[P[0], Y[0]]]
    passages = run(older, newer, REWORDED_OLD, CARRIED_NEW)
    assert len(passages) == 2 and all(p.chunk_ids == () for p in passages)
    inside = len(P[0]) + 10                                     # inside both passages
    degenerate = [("c0", 10_000, 20_000), ("c1", inside, inside)]      # elsewhere, and a zero-length chunk
    assert all(p.chunk_ids == () for p in run(older, newer, REWORDED_OLD, CARRIED_NEW,
                                              o_spans=degenerate, n_spans=degenerate))


# --------------------------------------------------------------------------
# determinism, aggregation, validation
# --------------------------------------------------------------------------

def test_the_result_is_deterministic_and_independent_of_chunk_span_order():
    older = [[P[0], X[0], REWORDED[0][0]], [X[1], P[1]]]
    newer = [[P[0], REWORDED[0][1], Y[0]], [P[1], Y[1]]]
    so, _ = filing("o", older)
    spans = [(f"c{k}", k * 60, k * 60 + 90) for k in range(len(so) // 60 + 1)]
    labels = ({"o0": "reworded", "o1": "reworded"}, {"n0": ("carried", "o0"), "n1": ("carried", "o1")})
    first = run(older, newer, *labels, o_spans=spans, n_spans=spans)
    shuffled = spans[:]
    random.Random(7).shuffle(shuffled)
    assert first == run(older, newer, *labels, o_spans=shuffled, n_spans=shuffled) == run(
        older, newer, *labels, o_spans=spans, n_spans=spans)
    assert len(first) >= 4


def test_summarize_passages_counts_kinds_items_and_characters():
    older = [[P[0], X[0], X[1]], [P[1], X[2], REWORDED[0][0]]]
    newer = [[P[0]], [P[1], REWORDED[0][1], Y[0]]]
    passages = run(older, newer, {"o0": "reworded", "o1": "reworded"},
                   {"n0": ("carried", "o0"), "n1": ("carried", "o1")})
    summary = summarize_passages(passages)
    assert summary["total"] == len(passages) == 4
    assert summary["by_kind"] == {"removed": 2, "reworded": 1, "added": 1}
    assert summary["by_item"] == {"o0": {"removed": 1}, "o1": {"removed": 1, "reworded": 1}, "n1": {"added": 1}}
    assert summary["chars"] == sum(len(p.text) for p in passages)
    assert summarize_passages(()) == {"total": 0, "by_kind": {"removed": 0, "reworded": 0, "added": 0},
                                      "by_item": {}, "chars": 0}


def test_passages_are_frozen_value_objects():
    (passage,) = run([[P[0], X[0]]], [[P[0]]], REWORDED_OLD, CARRIED_NEW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        passage.text = "x"  # type: ignore[misc]
    assert isinstance(passage.chunk_ids, tuple)


def test_empty_inputs_give_no_passages():
    assert compute_passages([], [], decisions({}, {}), "", "", (), ()) == ()


def test_default_params_are_the_values_chosen_on_the_development_gold():
    p = PassageParams()
    assert (p.present_min_ratio, p.reword_min, p.min_sentence_chars, p.max_passage_chars, p.max_probe_chars) == (
        75.0, 0.35, 40, 450, 600)
    assert p.decompose_uncertain is True and p.suppress_added_with_counterpart is True and p.partial_min == 0.0
    assert p.reword_confident == 0.50 and p.has_band and not PassageParams(**LEGACY).has_band          # the legacy dict has no band
    assert p.max_probe_chars == AlignParams().max_term_chars
    legacy = PassageParams(**LEGACY)
    assert legacy.present_min_ratio == AlignParams().absence_min_ratio          # the starting value was the aligner's rule
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.reword_min = 0.5  # type: ignore[misc]


@pytest.mark.parametrize("kwargs", [
    {"present_min_ratio": 0.0}, {"present_min_ratio": 100.5}, {"present_min_ratio": float("nan")},
    {"reword_min": 0.0}, {"reword_min": 1.01}, {"reword_min": -0.2},
    {"min_sentence_chars": 0}, {"min_sentence_chars": 40.5}, {"min_sentence_chars": True},
    {"max_passage_chars": 39}, {"max_probe_chars": 39}, {"partial_min": -1.0}, {"partial_min": 100.1},
])
def test_invalid_params_are_rejected(kwargs):
    with pytest.raises(ValueError):
        PassageParams(**kwargs)


def test_boundary_params_are_accepted():
    assert PassageParams(present_min_ratio=100.0, reword_min=1.0, min_sentence_chars=1,
                         max_passage_chars=1, max_probe_chars=1)


def test_an_item_whose_text_is_not_a_slice_of_its_section_is_rejected():
    so, o_rows = filing("o", [[P[0], X[0]]])
    sn, n_rows = filing("n", [[P[0]]])
    res = decisions(REWORDED_OLD, CARRIED_NEW)
    shifted = [dict(o_rows[0], char_start=o_rows[0]["char_start"] + 3)]
    with pytest.raises(ValueError, match="not a slice"):
        compute_passages(shifted, n_rows, res, so, sn, (), (), PassageParams(**LEGACY))
    edited = [dict(o_rows[0], text=o_rows[0]["text"] + " extra")]
    with pytest.raises(ValueError, match="not a slice"):
        compute_passages(edited, n_rows, res, so, sn, (), (), PassageParams(**LEGACY))


def test_a_missing_offset_or_row_is_rejected():
    so, o_rows = filing("o", [[P[0], X[0]]])
    sn, n_rows = filing("n", [[P[0]]])
    res = decisions(REWORDED_OLD, CARRIED_NEW)
    with pytest.raises(ValueError, match="char_start"):
        compute_passages([{"item_id": "o0", "text": o_rows[0]["text"]}], n_rows, res, so, sn, (), ())
    with pytest.raises(ValueError, match="o0"):
        compute_passages([], n_rows, res, so, sn, (), ())


def test_items_that_are_not_decomposed_are_not_validated():
    so, _ = filing("o", [[P[0]]])
    sn, n_rows = filing("n", [[P[0]]])
    junk = [{"item_id": "o0", "text": "does not appear", "char_start": 0}]
    assert compute_passages(junk, n_rows, decisions({"o0": "removed"}, {"n0": ("new", None)}), so, sn, (), ()) == ()


# --------------------------------------------------------------------------
# composition with the real aligner on synthetic data
# --------------------------------------------------------------------------

def test_passages_compose_with_align_and_use_exactly_the_aligners_absence_rule():
    older = [[*P, TENSE_OLD, X[0], REWORDED[0][0]]]
    newer = [[*P, TENSE_NEW, REWORDED[0][1], Y[0]]]
    so, o_rows = filing("o", older)
    sn, n_rows = filing("n", newer)
    res = align(o_rows, n_rows, sn, older_section_text=so)
    (old_decision,) = res.older
    assert old_decision.label == "reworded" and old_decision.decided_by == "body"
    passages = compute_passages(o_rows, n_rows, res, so, sn, (), (), PassageParams(**LEGACY))
    assert [(p.kind, p.text) for p in passages] == [
        ("removed", X[0]), ("reworded", REWORDED[0][0]), ("added", Y[0])]
    absent = {s for p in passages if p.kind != "added" for s in at.sentence_texts(p.text)}
    assert absent == set(old_decision.evidence.dropped_sentences)


# --------------------------------------------------------------------------
# real data (skipped when the local data lake is absent)
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
PAIRS = [
    ("NVDA", "2024-02-21", "2025-02-26"), ("NVDA", "2025-02-26", "2026-02-25"),
    ("AMD", "2025-02-05", "2026-02-04"), ("META", "2025-01-30", "2026-01-29"),
    ("MU", "2024-10-04", "2025-10-03"), ("TSM", "2025-04-17", "2026-04-16"),
]


def _lake_file(subdir: str, filename: str) -> Path | None:
    """The file in ``data/<subdir>`` whose name matches case-insensitively (the lake mixes NVDA/nvda)."""
    base = ROOT / "data" / subdir
    if not base.is_dir():
        return None
    return next((p for p in base.iterdir() if p.name.lower() == filename.lower()), None)


def load_pair(ticker: str, older_date: str, newer_date: str) -> dict:
    """Items, section texts and chunk spans of one consecutive annual pair, aligned with the real aligner."""
    files = {name: _lake_file(sub, f"{ticker}_{name}.parquet") for name, sub in (
        ("risk_items", "interim/risk_items"), ("section_texts", "interim/section_texts"),
        ("chunks", "processed/chunks"))}
    missing = [n for n, p in files.items() if p is None]
    if missing:
        pytest.skip(f"local data lake not present for {ticker}: missing {missing} "
                    "(data/interim/risk_items, data/interim/section_texts, data/processed/chunks)")
    import pandas as pd
    items = pd.read_parquet(files["risk_items"])
    items["day"] = items.filing_date.astype(str).str[:10]
    sections, chunks = pd.read_parquet(files["section_texts"]), pd.read_parquet(files["chunks"])
    side = {}
    for name, day in (("older", older_date), ("newer", newer_date)):
        rows = items[items.day == day].sort_values("seq")
        if rows.empty:
            pytest.skip(f"local data lake has no {ticker} items for {day}")
        acc, section_id = rows.accession_no.iloc[0], rows.section_id.iloc[0]
        text = sections[(sections.accession_no == acc) & (sections.section_id == section_id)].text.iloc[0]
        c = chunks[(chunks.accession_no == acc) & (chunks.section_id == section_id)]
        side[name] = {"rows": rows.to_dict("records"), "text": text, "accession": acc,
                      "spans": list(zip(c.chunk_id, c.char_start.astype(int), c.char_end.astype(int)))}
    o, n = side["older"], side["newer"]
    result = align(o["rows"], n["rows"], n["text"], older_section_text=o["text"])
    return {"o": o, "n": n, "result": result}


def passages_of(pair: dict, params: PassageParams = PassageParams(**LEGACY)) -> tuple[Passage, ...]:
    o, n = pair["o"], pair["n"]
    return compute_passages(o["rows"], n["rows"], pair["result"], o["text"], n["text"],
                            o["spans"], n["spans"], params)


@pytest.fixture(scope="module")
def nvda_pair():
    pair = load_pair(*PAIRS[1])
    start = time.perf_counter()
    pair["passages"] = passages_of(pair)
    pair["seconds"] = time.perf_counter() - start
    return pair


class TestRealNvidiaFy25ToFy26:
    def test_the_flagship_passages_are_reported_as_removed(self, nvda_pair):
        removed = [p.text for p in nvda_pair["passages"] if p.kind == "removed"]
        assert any("Notified Advanced Computing" in t for t in removed)
        assert any("out of China and Hong Kong" in t for t in removed)
        assert any("AI Diffusion IFR" in t for t in removed)

    def test_tense_only_edits_are_not_reported(self, nvda_pair):
        for p in nvda_pair["passages"]:
            assert "These restrictions impact exports of certain chips" not in p.text
        fy26 = at.SectionIndex(nvda_pair["n"]["text"])
        for p in nvda_pair["passages"]:
            if p.kind == "added":
                continue
            for sentence in at.sentence_texts(p.text):
                if len(sentence) >= 40:            # nothing near-verbatim in FY26 may be reported
                    assert fy26.probe(sentence[:600], 85.0, 400) is None

    def test_no_reworded_passage_differs_from_its_counterpart_by_two_words_or_fewer(self, nvda_pair):
        """Independent of the implementation's own probe: a tense-only edit changes one word token."""
        reworded = [p for p in nvda_pair["passages"] if p.kind == "reworded"]
        assert reworded
        for p in reworded:
            a, b = at.word_tokens(p.text), at.word_tokens(p.counterpart_text)
            ops = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
            changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal")
            assert changed > 2, p.passage_id

    def test_reworded_passages_have_counterparts_in_the_newer_section(self, nvda_pair):
        reworded = [p for p in nvda_pair["passages"] if p.kind == "reworded"]
        assert reworded
        fy26 = nvda_pair["n"]["text"]
        for p in reworded:
            a, b = p.counterpart_span
            assert fy26[a:b] == p.counterpart_text and quote_in_chunk(p.counterpart_text, fy26)
            assert p.similarity >= PassageParams(**LEGACY).reword_min

    def test_every_text_is_a_verbatim_slice_of_its_own_section(self, nvda_pair):
        for p in nvda_pair["passages"]:
            own = nvda_pair["n" if p.kind == "added" else "o"]["text"]
            assert own[p.char_start:p.char_end] == p.text and quote_in_chunk(p.text, own)
            assert len(p.text) <= PassageParams(**LEGACY).max_passage_chars or len(at.split_sentences(p.text)) == 1

    def test_chunk_ids_belong_to_the_passages_own_filing(self, nvda_pair):
        for p in nvda_pair["passages"]:
            side = nvda_pair["n" if p.kind == "added" else "o"]
            allowed = {c for c, _, _ in side["spans"]}
            assert p.chunk_ids and set(p.chunk_ids) <= allowed
            assert p.chunk_ids == tuple(sorted(p.chunk_ids))       # chunk ids are positional, so text order

    def test_absent_sentences_equal_the_aligners_dropped_sentences(self, nvda_pair):
        by_item = {}
        for p in nvda_pair["passages"]:
            if p.kind != "added":
                by_item.setdefault(p.item_id, set()).update(
                    s for s in at.sentence_texts(p.text) if len(s) >= 40)
        checked = 0
        for d in nvda_pair["result"].older:
            if d.label == "reworded" and d.decided_by != "hash":
                assert by_item.get(d.item_id, set()) == set(d.evidence.dropped_sentences)
                checked += 1
        assert checked >= 10

    def test_unchanged_items_and_new_items_produce_no_passages(self, nvda_pair):
        unchanged = {d.item_id for d in nvda_pair["result"].older if d.label == "unchanged"}
        not_carried = {d.item_id for d in nvda_pair["result"].newer if d.label != "carried"}
        assert unchanged and not ({p.item_id for p in nvda_pair["passages"]} & (unchanged | not_carried))

    def test_the_run_is_fast_and_deterministic(self, nvda_pair):
        assert nvda_pair["seconds"] < 60
        assert passages_of(nvda_pair) == nvda_pair["passages"]
        assert summarize_passages(nvda_pair["passages"])["total"] == len(nvda_pair["passages"])


@pytest.mark.parametrize("pair_key", PAIRS, ids=lambda k: f"{k[0]}-{k[1]}-{k[2]}")
def test_real_development_pairs_keep_the_invariants(pair_key):
    pair = load_pair(*pair_key)
    passages = passages_of(pair)
    assert passages and passages == passages_of(pair)
    for p in passages:
        own = pair["n" if p.kind == "added" else "o"]["text"]
        assert own[p.char_start:p.char_end] == p.text
        assert (p.counterpart_text is not None) == (p.kind == "reworded")
        assert p.passage_id == f"{p.item_id}:{KIND_CODE[p.kind]}{p.seq:03d}"
    assert len({p.passage_id for p in passages}) == len(passages)
    _assert_every_absent_newer_sentence_is_reported(pair, passages)


def _assert_every_absent_newer_sentence_is_reported(pair: dict, passages: Sequence[Passage]) -> None:
    """Each newer sentence (40+ chars, in a decomposed newer item) absent from the older section is either in an
    ``added`` passage or inside a reworded passage's counterpart span: nothing is silently dropped."""
    older_index = at.SectionIndex(pair["o"]["text"])
    spans = [p.counterpart_span for p in passages if p.kind == "reworded"]
    added = [(p.char_start, p.char_end) for p in passages if p.kind == "added"]
    old_by_id = {d.item_id: d for d in pair["result"].older}
    rows = {r["item_id"]: r for r in pair["n"]["rows"]}
    checked = 0
    for d in pair["result"].newer:
        partner = old_by_id.get(d.matched_older_id)
        if d.label != "carried" or (partner is not None and partner.label == "unchanged"):
            continue
        row = rows[d.item_id]
        for a, b in at.split_sentences(row["text"]):
            sentence, pos = row["text"][a:b], row["char_start"] + a
            if len(sentence) < 40 or older_index.probe(sentence[:600], 85.0, 400) is not None:
                continue
            checked += 1
            assert any(s <= pos < e for s, e in spans + added), (d.item_id, sentence[:80])
    assert checked


class TestRealNvidiaFy25ToFy26WithTheShippedDefaults:
    """The tuned defaults (chosen on the development gold) must keep the flagship facts and stay small enough to quote."""

    @pytest.fixture(scope="class")
    def tuned(self, nvda_pair):
        return passages_of(nvda_pair, PassageParams())

    @pytest.mark.xfail(reason="KNOWN (2026-09-26): at reword_min 0.35 the Hong-Kong-transition sentence gets a false lexical "
                              "counterpart (sim 0.367, a different sentence about HK warehousing) and is called reworded, not removed. "
                              "Built: the lexical band [0.35, 0.60) is settled by cached model verdicts (`semigraph align-items "
                              "--adjudicate-passages`, code-checked in graph/passage_adjudicate.py); WITHOUT verdicts the sentence stays "
                              "band-reworded by design (never a removal it cannot support), so this stays xfail here. With a `different` "
                              "verdict it is removed: tests/test_passage_bands.py.", strict=True)
    def test_the_nac_and_hong_kong_sentences_are_in_removed_passages_no_longer_than_the_cap(self, tuned):
        removed = [p for p in tuned if p.kind == "removed"]
        nac = [p for p in removed if "Notified Advanced Computing" in p.text]
        hk = [p for p in removed if "out of China and Hong Kong" in p.text]
        assert nac and hk
        for p in nac + hk:
            assert len(p.text) <= PassageParams().max_passage_chars or len(at.split_sentences(p.text)) == 1

    def test_no_passage_exceeds_the_cap_unless_it_is_a_single_long_sentence(self, tuned):
        cap = PassageParams().max_passage_chars
        assert all(len(p.text) <= cap or len(at.split_sentences(p.text)) == 1 for p in tuned)

    def test_tense_only_edits_are_still_not_reported_and_every_text_is_verbatim(self, tuned, nvda_pair):
        for p in tuned:
            assert "These restrictions impact exports of certain chips" not in p.text
            own = nvda_pair["n" if p.kind == "added" else "o"]["text"]
            assert own[p.char_start:p.char_end] == p.text
