"""The lexical band of the passage layer (M1b): sentences whose best counterpart is too similar to call them removed and too
different to trust as reworded (``reword_min <= similarity < reword_confident``).

Pure tests of the enumeration (``band_sentences``), of how verdicts are applied (``compute_passages(..., band_verdicts=...)``) and
of the safe-direction guarantee: a band sentence WITHOUT a verdict never becomes a removed or added passage. The verdicts here are
hand-built ``BandVerdict`` values (the model and the code rules that produce them are tested in ``test_passage_adjudicate.py``).
The last class runs the real Nvidia FY25 -> FY26 pair and skips when the local data lake is absent.
"""

import dataclasses

import pytest
from rapidfuzz import fuzz
from rapidfuzz.utils import default_process
from test_passages import PAIRS, REWORDED, REWORDED_OLD, CARRIED_NEW, X, Y, P, decisions, filing, load_pair

from semigraph.graph import align_text as at
from semigraph.graph.passages import (
    DECIDED_BY,
    BandCandidate,
    BandSentence,
    BandVerdict,
    PairPassages,
    PassageParams,
    band_key,
    band_sentences,
    compute_passages,
)
from semigraph.hashing import content_hash

# A pair whose word-level similarity is inside the band [0.35, 0.60) but whose FACTS differ (the Nvidia lookalike).
HK = ("Following these export controls, we transitioned certain testing, validation and distribution operations out of China and Hong Kong.",
      "Certain of our testing and distribution operations remain in China and Hong Kong where we lease warehouse space for finished products.")
# A genuine paraphrase whose word-level similarity is also inside the band.
PARA = ("Long lead times for advanced packaging capacity make it difficult to respond to sudden changes in demand.",
        "Advanced packaging capacity has long lead times, so responding to sudden shifts in customer demand is difficult for us.")

BAND = dict(present_min_ratio=85.0, reword_min=0.35, reword_confident=0.60, max_passage_chars=1200, decompose_uncertain=False,
            suppress_added_with_counterpart=True)


def lex(a: str, b: str) -> float:
    return at.lex_exact(at.word_tokens(a), at.word_tokens(b))


def engine(older_items, newer_items, older_labels=REWORDED_OLD, newer_labels=CARRIED_NEW, **params):
    so, o_rows = filing("o", older_items)
    sn, n_rows = filing("n", newer_items)
    pp = PairPassages(o_rows, n_rows, decisions(older_labels, newer_labels), so, sn, (), (), PassageParams(**{**BAND, **params}))
    return pp, so, sn


def span_of(text: str, sentence: str) -> tuple[int, int]:
    start = text.index(sentence)
    return start, start + len(sentence)


def test_the_fixture_pairs_sit_where_the_tests_need_them():
    for a, b in (HK, PARA):
        assert 0.35 <= lex(a, b) < 0.60
        assert at.SectionIndex(f"{b} {X[0]}").probe(a, 85.0, 400) is None         # not PRESENT
    assert lex(*REWORDED[0]) >= 0.60                                              # a confident rewording, for contrast
    assert all(lex(s, t) < 0.35 for s in (HK[0], HK[1], PARA[0], PARA[1]) for t in P + X + Y)


# --------------------------------------------------------------------------
# enumeration
# --------------------------------------------------------------------------

def test_only_sentences_inside_the_lexical_band_are_band_sentences():
    pp, so, sn = engine([[P[0], HK[0], REWORDED[0][0], X[0]]], [[P[0], HK[1], REWORDED[0][1]]])
    older = [b for b in pp.band_sentences() if b.side == "older"]
    assert [b.text for b in older] == [HK[0]]                                     # confident reworded and removed are not band
    (band,) = older
    assert band.item_id == "o0" and (band.start, band.end) == span_of(so, HK[0])
    assert band.similarity == pytest.approx(lex(*HK)) and band.route == "lexical"


def test_the_band_has_a_stable_key_and_hashes_of_the_text_and_of_the_other_section():
    pp, so, sn = engine([[P[0], HK[0]]], [[P[0], HK[1]]])
    band = next(b for b in pp.band_sentences() if b.side == "older")
    assert band.key == band_key("o0", so.index(HK[0])) == f"o0@{so.index(HK[0])}"
    assert band.text_hash == content_hash(HK[0]) and band.other_hash == content_hash(sn)
    pp2, _, sn2 = engine([[P[0], HK[0]]], [[P[0], HK[1], P[1]]])                   # the OTHER section changed
    changed = next(b for b in pp2.band_sentences() if b.side == "older")
    assert changed.key == band.key and changed.text_hash == band.text_hash and changed.other_hash != band.other_hash


def test_enumeration_is_deterministic_and_the_function_equals_the_engine():
    so, o_rows = filing("o", [[P[0], HK[0], PARA[0]]])
    sn, n_rows = filing("n", [[P[0], HK[1], PARA[1]]])
    res = decisions(REWORDED_OLD, CARRIED_NEW)
    params = PassageParams(**BAND)
    first = band_sentences(o_rows, n_rows, res, so, sn, params)
    assert first == band_sentences(o_rows, n_rows, res, so, sn, params)
    assert first == PairPassages(o_rows, n_rows, res, so, sn, (), (), params).band_sentences()
    assert [b.side for b in first] == ["older", "older", "newer", "newer"]        # older side first, then newer, in text order
    assert [b.text for b in first[:2]] == [HK[0], PARA[0]]


def test_newer_band_sentences_are_listed_whether_or_not_an_older_passage_already_quotes_them():
    """The older side's verdicts change which newer sentences are covered, so the enumeration must not depend on it."""
    pp, _, _ = engine([[P[0], HK[0]]], [[P[0], HK[1]]])
    assert [b.text for b in pp.band_sentences() if b.side == "newer"] == [HK[1]]
    quoted = pp.passages()                                                        # the older side quotes HK[1] as a counterpart
    assert [p.kind for p in quoted] == ["reworded"] and quoted[0].counterpart_text == HK[1]


def test_newer_band_sentences_need_suppression_because_otherwise_the_sentence_is_added_anyway():
    pp, _, _ = engine([[P[0], HK[0]]], [[P[0], HK[1]]], suppress_added_with_counterpart=False)
    assert [b.side for b in pp.band_sentences()] == ["older"]


def test_a_band_of_none_or_an_empty_band_lists_nothing():
    for params in ({"reword_confident": None}, {"reword_confident": 0.35}, {"reword_confident": 0.20}):
        pp, _, _ = engine([[P[0], HK[0]]], [[P[0], HK[1]]], **params)
        assert pp.band_sentences() == ()


def test_present_and_short_sentences_and_items_that_are_not_decomposed_are_never_band_sentences():
    short = "Hong Kong operations moved."
    pp, _, _ = engine([[P[0], short]], [[P[0], HK[1]]])
    assert pp.band_sentences() == ()
    assert engine([[P[0], HK[0]]], [[P[0], HK[1]]], older_labels={"o0": "removed"}, newer_labels={"n0": ("new", None)})[0].band_sentences() == ()


# --------------------------------------------------------------------------
# candidates
# --------------------------------------------------------------------------

DISTRACTORS = (
    "Our common stock price has been volatile and may continue to be volatile for reasons unrelated to our operations.",
    "We lease office space for our headquarters and several regional design centers under long term operating leases.",
    "Changes in accounting standards could affect the way we report revenue and could reduce our reported earnings.",
    "Our credit facility contains covenants that restrict our ability to pay dividends or to repurchase our shares.",
    "Employees who hold significant amounts of unvested equity may leave the company after the awards have vested.",
    "The effects of climate change may increase the cost of operating our facilities and those of our suppliers.",
)


def test_candidates_are_verbatim_sentences_of_the_other_section_the_lexical_best_first_and_at_most_five():
    pp, so, sn = engine([[P[0], HK[0]]], [[P[0], *DISTRACTORS[:3], HK[1], *DISTRACTORS[3:]]])
    band = next(b for b in pp.band_sentences() if b.side == "older")
    assert 1 <= len(band.candidates) <= 5 and all(isinstance(c, BandCandidate) for c in band.candidates)
    assert band.candidates[0].text == HK[1]                                      # the lexical best, which decided the band
    assert len({(c.start, c.end) for c in band.candidates}) == len(band.candidates)
    assert all(sn[c.start:c.end] == c.text for c in band.candidates)
    assert band.candidates[0].lex_sim == pytest.approx(lex(*HK))


def test_candidates_contain_the_best_by_partial_ratio_as_well_as_the_best_by_word_similarity():
    pp, so, sn = engine([[P[0], HK[0]]], [[P[0], *DISTRACTORS[:3], HK[1], *DISTRACTORS[3:]]])
    band = next(b for b in pp.band_sentences() if b.side == "older")
    sentences = [sn[a:b] for a, b in at.split_sentences(sn) if b - a >= 40]
    best_partial = max(sentences, key=lambda s: (fuzz.partial_ratio(HK[0], s, processor=default_process), -sn.index(s)))
    best_lexical = max(sentences, key=lambda s: (lex(HK[0], s), -sn.index(s)))
    texts = [c.text for c in band.candidates]
    assert best_partial in texts and best_lexical in texts
    assert all(c.partial >= 0 for c in band.candidates)


def test_the_candidate_search_can_be_skipped_when_only_keys_and_hashes_are_needed():
    pp, _, _ = engine([[P[0], HK[0]]], [[P[0], HK[1]]])
    full, light = pp.band_sentences(), pp.band_sentences(candidates=False)
    assert [b.candidates for b in light] == [(), ()] and all(b.candidates for b in full)
    assert [(b.key, b.side, b.text_hash, b.other_hash) for b in light] == [(b.key, b.side, b.text_hash, b.other_hash) for b in full]


def test_a_sentence_that_occurs_twice_in_the_other_section_is_one_candidate():
    heading = "Risks Related to Demand, Supply, and Manufacturing and Other Operations"
    pp, _, _ = engine([[P[0], HK[0]]], [[P[0], heading, HK[1], DISTRACTORS[0], heading]])
    texts = [c.text for c in next(b for b in pp.band_sentences() if b.side == "older").candidates]
    assert len(texts) == len(set(texts)) and texts.count(heading) == 1


def test_the_number_of_candidates_is_a_parameter():
    pp, _, _ = engine([[P[0], HK[0]]], [[P[0], *DISTRACTORS[:3], HK[1], *DISTRACTORS[3:]]], band_candidates=2)
    assert len(next(b for b in pp.band_sentences() if b.side == "older").candidates) == 2


# --------------------------------------------------------------------------
# applying verdicts
# --------------------------------------------------------------------------

def older_band(pp):
    return next(b for b in pp.band_sentences() if b.side == "older")


def newer_band(pp):
    return next(b for b in pp.band_sentences() if b.side == "newer")


def test_without_a_verdict_a_band_sentence_stays_reworded_and_says_it_is_only_band_reworded():
    pp, _, _ = engine([[P[0], HK[0]]], [[P[0], HK[1]]])
    (p,) = pp.passages()
    assert (p.kind, p.text, p.counterpart_text) == ("reworded", HK[0], HK[1])
    assert p.decided_by == "sentence_reworded_band" and p.band_adjudicated is False
    assert p.similarity == pytest.approx(lex(*HK))


def test_a_different_verdict_turns_an_older_band_sentence_into_a_removed_passage():
    pp, so, _ = engine([[P[0], HK[0]]], [[P[0], HK[1]]])
    (p,) = pp.passages({older_band(pp).key: BandVerdict("different")})
    assert (p.kind, p.text, p.counterpart_text, p.counterpart_span, p.similarity) == ("removed", HK[0], None, None, None)
    assert p.decided_by == "sentence_absent_llm" and p.band_adjudicated is True
    assert p.passage_id == "o0:r000" and so[p.char_start:p.char_end] == HK[0]


def test_a_same_verdict_with_a_verified_span_is_reworded_with_exactly_that_counterpart():
    older, newer = [[P[0], HK[0]]], [[P[0], HK[1], DISTRACTORS[0]]]
    pp, _, sn = engine(older, newer)
    span = span_of(sn, DISTRACTORS[0])                                            # the model picked ANOTHER candidate
    (p,) = pp.passages({older_band(pp).key: BandVerdict("same", span)})
    assert p.kind == "reworded" and p.counterpart_span == span and p.counterpart_text == DISTRACTORS[0]
    assert p.decided_by == "sentence_reworded_llm" and p.band_adjudicated is True
    assert 0.0 <= p.similarity <= 1.0 and sn[p.counterpart_span[0]:p.counterpart_span[1]] == p.counterpart_text


@pytest.mark.parametrize("verdict", [
    BandVerdict("same"),                                                          # no span: nothing was verified
    BandVerdict("same", (10_000, 10_050)),                                        # outside the section
    BandVerdict("same", (5, 5)),                                                  # empty
    BandVerdict("same", (-3, 20)),
])
def test_a_same_verdict_without_a_usable_span_is_ignored_and_the_sentence_stays_band_reworded(verdict):
    pp, _, _ = engine([[P[0], HK[0]]], [[P[0], HK[1]]])
    (p,) = pp.passages({older_band(pp).key: verdict})
    assert (p.kind, p.counterpart_text, p.decided_by, p.band_adjudicated) == ("reworded", HK[1], "sentence_reworded_band", False)


def test_a_verdict_for_a_sentence_that_is_not_a_band_sentence_is_ignored():
    older, newer = [[P[0], REWORDED[0][0], X[0]]], [[P[0], REWORDED[0][1]]]
    pp, so, _ = engine(older, newer)
    keys = {f"o0@{so.index(REWORDED[0][0])}": BandVerdict("different"), f"o0@{so.index(X[0])}": BandVerdict("same", (0, 40)),
            "elsewhere@3": BandVerdict("different")}
    plain = pp.passages()
    assert pp.passages(keys) == plain
    assert [(p.kind, p.decided_by) for p in plain] == [("reworded", "sentence_reworded"), ("removed", "sentence_absent")]


def test_no_verdict_never_creates_a_removed_or_added_passage_from_a_band_sentence():
    """The safe direction, at the shipped defaults: without answers the result equals the band-less classification."""
    older, newer = [[P[0], HK[0], PARA[0], X[0]]], [[P[0], HK[1], PARA[1], Y[0]]]
    so, o_rows = filing("o", older)
    sn, n_rows = filing("n", newer)
    res = decisions(REWORDED_OLD, CARRIED_NEW)
    shipped = compute_passages(o_rows, n_rows, res, so, sn, (), (), PassageParams())
    off = compute_passages(o_rows, n_rows, res, so, sn, (), (), PassageParams(reword_confident=None))
    shape = lambda ps: [(p.kind, p.text, p.counterpart_text, p.char_start, p.passage_id) for p in ps]     # noqa: E731
    assert shape(shipped) == shape(off)
    band_texts = {b.text for b in band_sentences(o_rows, n_rows, res, so, sn, PassageParams())}
    assert {HK[0], PARA[0], HK[1], PARA[1]} <= band_texts
    assert not any(p.kind in ("removed", "added") and p.text in band_texts for p in shipped)
    assert [p.text for p in shipped if p.kind != "reworded"] == [X[0], Y[0]]         # only the unrelated sentences are claims
    assert {p.decided_by for p in shipped if p.kind == "reworded"} == {"sentence_reworded_band"}


def test_a_newer_band_sentence_is_added_only_on_a_different_verdict_and_never_when_an_older_passage_quotes_it():
    older_items, newer_items = [[P[0], HK[0]]], [[P[0], HK[1]]]
    pp, _, _ = engine(older_items, newer_items)
    o_key, n_key = older_band(pp).key, newer_band(pp).key
    # older side unresolved -> it quotes HK[1] as a counterpart -> the newer sentence is covered whatever the model says of it
    assert [p.kind for p in pp.passages({n_key: BandVerdict("different")})] == ["reworded"]
    # the older side is settled as removed -> nothing quotes HK[1] any more -> now its own verdict decides
    both = pp.passages({o_key: BandVerdict("different"), n_key: BandVerdict("different")})
    assert [(p.kind, p.item_id, p.text, p.decided_by, p.band_adjudicated) for p in both] == [
        ("removed", "o0", HK[0], "sentence_absent_llm", True), ("added", "n0", HK[1], "sentence_absent_llm", True)]
    only_older = pp.passages({o_key: BandVerdict("different")})
    assert [p.kind for p in only_older] == ["removed"]                             # no answer for the newer one: suppressed
    same = pp.passages({o_key: BandVerdict("different"), n_key: BandVerdict("same", span_of(engine(older_items, newer_items)[1], HK[0]))})
    assert [p.kind for p in same] == ["removed"]


def test_a_newer_band_sentence_that_no_older_passage_can_quote_is_added_on_a_different_verdict():
    older_items, newer_items = [[P[0], HK[0]]], [[P[0], HK[1]]]
    pp, _, _ = engine(older_items, newer_items, older_labels={"o0": "removed"}, newer_labels={"n0": ("carried", None)})
    assert pp.passages() == ()                                                    # HK[1] has a counterpart: suppressed
    (band,) = pp.band_sentences()
    assert band.side == "newer"
    (p,) = pp.passages({band.key: BandVerdict("different")})
    assert (p.kind, p.text, p.item_id, p.decided_by) == ("added", HK[1], "n0", "sentence_absent_llm")


def test_a_run_takes_the_weakest_provenance_of_its_sentences_and_grouping_does_not_change():
    older = [[HK[0], REWORDED[0][0], P[0]]]
    newer = [[HK[1], REWORDED[0][1], P[0]]]
    pp, _, _ = engine(older, newer)
    (mixed,) = pp.passages()                                                       # one passage: adjacent counterparts
    assert mixed.text == f"{HK[0]} {REWORDED[0][0]}" and mixed.decided_by == "sentence_reworded_band"
    span = span_of(pp._newer.text, HK[1])
    (llm,) = pp.passages({older_band(pp).key: BandVerdict("same", span)})
    assert llm.text == mixed.text and llm.decided_by == "sentence_reworded_llm" and llm.band_adjudicated is True
    removed_run = engine([[X[0], HK[0], P[0]]], [[P[0], HK[1]]])[0]
    (a,) = removed_run.passages({older_band(removed_run).key: BandVerdict("different")})
    assert a.text == f"{X[0]} {HK[0]}" and a.decided_by == "sentence_absent_llm" and a.band_adjudicated is True


def test_a_verified_counterpart_that_is_not_adjacent_ends_the_run():
    older = [[HK[0], REWORDED[0][0]]]
    newer = [[HK[1], P[0], REWORDED[0][1], DISTRACTORS[0]]]
    pp, _, sn = engine(older, newer)
    far = span_of(sn, DISTRACTORS[0])
    passages = pp.passages({older_band(pp).key: BandVerdict("same", far)})
    assert [p.text for p in passages if p.kind == "reworded"] == [HK[0], REWORDED[0][0]]          # two passages, not one


def test_compute_passages_takes_the_verdicts_as_a_keyword_and_matches_the_engine():
    so, o_rows = filing("o", [[P[0], HK[0]]])
    sn, n_rows = filing("n", [[P[0], HK[1]]])
    res = decisions(REWORDED_OLD, CARRIED_NEW)
    params = PassageParams(**BAND)
    key = band_sentences(o_rows, n_rows, res, so, sn, params)[0].key
    verdicts = {key: BandVerdict("different")}
    got = compute_passages(o_rows, n_rows, res, so, sn, (), (), params, band_verdicts=verdicts)
    assert got == PairPassages(o_rows, n_rows, res, so, sn, (), (), params).passages(verdicts)
    assert [p.kind for p in got] == ["removed"]
    assert compute_passages(o_rows, n_rows, res, so, sn, (), (), params, band_verdicts=None) == compute_passages(
        o_rows, n_rows, res, so, sn, (), (), params)


def test_the_engine_can_be_asked_repeatedly_and_stays_consistent():
    pp, _, _ = engine([[P[0], HK[0], PARA[0]]], [[P[0], HK[1], PARA[1]]])
    bands = pp.band_sentences()
    assert pp.band_sentences() == bands and pp.passages() == pp.passages()
    key = older_band(pp).key
    assert pp.passages({key: BandVerdict("different")}) != pp.passages()
    assert pp.passages() == pp.passages(None) == pp.passages({})


# --------------------------------------------------------------------------
# parameters and value objects
# --------------------------------------------------------------------------

def test_the_shipped_band_is_060_and_none_switches_it_off():
    p = PassageParams()
    assert p.reword_confident == 0.60 and p.reword_min == 0.35 and p.band_candidates == 5
    assert PassageParams(reword_confident=None).reword_confident is None
    assert "sentence_reworded_band" in DECIDED_BY and "sentence_absent_llm" in DECIDED_BY and "sentence_reworded_llm" in DECIDED_BY


@pytest.mark.parametrize("kwargs", [{"reword_confident": 0.0}, {"reword_confident": 1.2}, {"reword_confident": float("nan")},
                                    {"band_candidates": 0}, {"band_candidates": 2.5}, {"max_candidate_chars": 10}])
def test_invalid_band_parameters_are_rejected(kwargs):
    with pytest.raises(ValueError):
        PassageParams(**kwargs)


def test_a_confident_threshold_at_or_below_reword_min_is_accepted_and_means_an_empty_band():
    assert PassageParams(reword_min=0.99, reword_confident=0.60)                   # the legacy tests rely on this
    assert PassageParams(reword_min=1.0)


def test_verdicts_and_band_objects_are_frozen_and_validated():
    with pytest.raises(ValueError):
        BandVerdict("maybe")
    v = BandVerdict("same", (1, 9))
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.verdict = "different"  # type: ignore[misc]
    pp, _, _ = engine([[P[0], HK[0]]], [[P[0], HK[1]]])
    assert isinstance(older_band(pp), BandSentence)
    with pytest.raises(dataclasses.FrozenInstanceError):
        older_band(pp).text = "x"  # type: ignore[misc]


# --------------------------------------------------------------------------
# real data (skipped when the local data lake is absent)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def nvda():
    pair = load_pair(*PAIRS[1])
    o, n = pair["o"], pair["n"]
    pp = PairPassages(o["rows"], n["rows"], pair["result"], o["text"], n["text"], o["spans"], n["spans"], PassageParams())
    return pp, pp.band_sentences(), pp.passages()


NEEDLES = {"NAC": "Notified Advanced Computing", "HK": "out of China and Hong Kong"}


def test_real_nvidia_flagship_sentences_are_band_or_confidently_removed(nvda):
    pp, bands, passages = nvda
    for name, needle in NEEDLES.items():
        as_band = [b for b in bands if b.side == "older" and needle in b.text]
        as_removed = [p for p in passages if p.kind == "removed" and needle in p.text]
        assert as_band or as_removed, name


def test_real_nvidia_hong_kong_sentence_becomes_removed_on_a_different_verdict(nvda):
    pp, bands, passages = nvda
    hk = [b for b in bands if b.side == "older" and NEEDLES["HK"] in b.text]
    if not hk:
        pytest.skip("the Hong Kong sentence is already confidently removed at these parameters")
    verdicts = {b.key: BandVerdict("different") for b in hk}
    settled = pp.passages(verdicts)
    assert any(p.kind == "removed" and NEEDLES["HK"] in p.text and p.band_adjudicated for p in settled)
    without = pp.passages()
    assert not any(p.kind == "removed" and NEEDLES["HK"] in p.text for p in without)             # the strict xfail's reason
    assert all(p.chunk_ids for p in settled)


def test_real_nvidia_band_sentences_have_verbatim_candidates_and_unique_keys(nvda):
    pp, bands, _ = nvda
    assert bands and len({b.key for b in bands}) == len(bands)
    for b in bands:
        own = pp._older if b.side == "older" else pp._newer
        other = pp._newer if b.side == "older" else pp._older
        assert own.text[b.start:b.end] == b.text and b.text_hash == content_hash(b.text) and b.other_hash == content_hash(other.text)
        assert 1 <= len(b.candidates) <= 5
        assert all(other.text[c.start:c.end].startswith(c.text) for c in b.candidates)
    assert pp.band_sentences() == bands
