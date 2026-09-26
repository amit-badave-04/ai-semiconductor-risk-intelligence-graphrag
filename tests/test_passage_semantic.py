"""The passage adjudicator's SEMANTIC candidates, the stand-in candidates of a dry run, the per-zone estimates and the prompt versions
(``graph/passage_adjudicate.py`` with ``graph/sentence_embed.py``). Fake model, fake deterministic embedder (``embedfix``), nothing paid."""

import json

import pytest
from embedfix import make_embed
from test_passage_adjudicate import MODEL, FakeLLM, pair_bands
from test_passage_bands import BELOW, BELOW2, DISTRACTORS, ON, TARIFF, TARIFF_QUOTE, engine
from test_passages import P, Y

from semigraph.graph import adjudicate as adj
from semigraph.graph import passage_adjudicate as pad
from semigraph.graph import sentence_embed as se
from semigraph.graph.align_text import norm
from semigraph.graph.passages import BandCandidate, BandVerdict


def cand(text, start, lex=0.0, partial=0.0, cosine=None):
    return BandCandidate(text, start, start + len(text), lex, partial, cosine)


CROWD = [*DISTRACTORS, *P[1:], *Y]                       # thirteen sentences unrelated to the BELOW / TARIFF sentences


def crowded(older_pair=BELOW, older_pair2=BELOW2, **params):
    """Older sentences whose paraphrases sit among a crowd in the newer section (the lexical top five misses them)."""
    older = [[P[0], older_pair[0], older_pair2[0]]]
    newer = [[P[0], *CROWD[:7], older_pair[1], *CROWD[7:], older_pair2[1]]]
    pp, so, sn = engine(older, newer, **{**ON, **params})
    bands = pp.with_candidates(pp.band_sentences(candidates=False))
    return pp, bands, so, sn


def neighbours(so, sn, embed=None):
    return se.PairNeighbours(("OLD", so), ("NEW", sn), embed or make_embed(), None, min_chars=40)


def older_band(bands, text):
    return next(b for b in bands if b.side == "older" and b.text == text)


# --------------------------------------------------------------------------- combining candidates

class TestCombine:
    """``combine_candidates``: lexical best, partial best, then the embedding ranks; de-duplicated, capped, deterministic."""

    LEX = [cand("Aaa lexical winner sentence of the other filing.", 0, lex=0.9, partial=40),
           cand("Bbb partial winner sentence of the other filing.", 100, lex=0.2, partial=95),
           cand("Ccc filler sentence of the other filing goes here.", 200, lex=0.5, partial=50)]

    def test_the_order_is_lexical_best_then_partial_best_then_semantic_by_rank(self):
        sem = [cand(f"Semantic {k} sentence of the other filing text.", 1000 + 100 * k, cosine=0.9 - k / 10) for k in range(3)]
        got = pad.combine_candidates(self.LEX, sem)
        assert [c.start for c in got] == [0, 100, 1000, 1100, 1200]
        assert "Ccc filler" not in " ".join(c.text for c in got)                     # only the two lexical bests are kept

    def test_a_semantic_candidate_that_repeats_a_lexical_one_is_listed_once_and_the_next_rank_refills_the_slot(self):
        sem = [cand(self.LEX[0].text.upper(), 5000, cosine=0.99), cand(self.LEX[1].text + "  ", 5100, cosine=0.9),
               cand("A different sentence of the other filing here.", 6000)]
        got = pad.combine_candidates(self.LEX, sem)
        assert [c.start for c in got] == [0, 100, 6000]
        assert len({norm(c.text) for c in got}) == len(got)

    def test_the_list_is_capped_at_max_candidates(self):
        sem = [cand(f"Semantic {k} sentence of the other filing text.", 1000 + 100 * k) for k in range(20)]
        assert len(pad.combine_candidates(self.LEX, sem)) == 8 == pad.PassageAdjudicationParams().max_candidates
        assert len(pad.combine_candidates(self.LEX, sem, pad.PassageAdjudicationParams(max_candidates=3))) == 3

    def test_ties_go_to_the_earlier_sentence_and_the_result_is_deterministic(self):
        tied = [cand("Zzz later sentence of the other filing here.", 500, lex=0.7, partial=80),
                cand("Yyy earlier sentence of the other filing here.", 50, lex=0.7, partial=80)]
        got = pad.combine_candidates(tied, [])
        assert [c.start for c in got] == [50]                                        # both bests are the earlier sentence: listed once
        assert got == pad.combine_candidates(tied, []) == pad.combine_candidates(list(reversed(tied)), [])

    def test_no_candidate_at_all_is_an_empty_tuple(self):
        assert pad.combine_candidates([], []) == ()

    def test_only_semantic_candidates_still_work_for_a_sentence_with_no_lexical_list(self):
        sem = [cand("Semantic sentence of the other filing here.", 10, cosine=0.5)]
        assert pad.combine_candidates([], sem) == tuple(sem)

    def test_max_candidates_needs_at_least_a_lexical_and_a_partial_slot(self):
        with pytest.raises(ValueError):
            pad.PassageAdjudicationParams(max_candidates=1)


# --------------------------------------------------------------------------- semantic candidates

class TestSemanticCandidates:
    def test_the_paraphrase_the_lexical_top_five_misses_is_shown_to_the_model(self):
        pp, bands, so, sn = crowded()
        older = older_band(bands, BELOW[0])
        assert BELOW[1] not in [c.text for c in older.candidates]                        # the diagnosis: lexical alone misses it
        (combined,) = pad.with_semantic_candidates([older], neighbours(so, sn).neighbours, so, sn)
        texts = [c.text for c in combined.candidates]
        assert BELOW[1] in texts and texts.index(BELOW[1]) <= 2                          # ... and the embedding ranks it first
        assert len(texts) == 8 and len({norm(t) for t in texts}) == 8
        assert all(sn[c.start:c.end].startswith(c.text) for c in combined.candidates)   # verbatim slices of the other section
        assert next(c for c in combined.candidates if c.text == BELOW[1]).cosine > 0.5

    def test_the_first_two_are_the_lexical_best_and_the_partial_best_of_the_lexical_list(self):
        pp, bands, so, sn = crowded()
        older = older_band(bands, BELOW2[0])
        (combined,) = pad.with_semantic_candidates([older], neighbours(so, sn).neighbours, so, sn)
        lexical = pad.combine_candidates(older.candidates, [])
        assert combined.candidates[:len(lexical)] == lexical
        assert BELOW2[1] in [c.text for c in combined.candidates]

    def test_the_newer_side_is_matched_against_the_older_section(self):
        pp, bands, so, sn = crowded()
        newer = next(b for b in bands if b.side == "newer" and b.text == BELOW[1])
        (combined,) = pad.with_semantic_candidates([newer], neighbours(so, sn).neighbours, so, sn)
        assert BELOW[0] in [c.text for c in combined.candidates] and all(so[c.start:c.end].startswith(c.text) for c in combined.candidates)

    def test_the_result_is_deterministic_and_the_input_is_not_mutated(self):
        pp, bands, so, sn = crowded()
        before = list(bands)
        a = pad.with_semantic_candidates(bands, neighbours(so, sn).neighbours, so, sn)
        b = pad.with_semantic_candidates(bands, neighbours(so, sn, make_embed()).neighbours, so, sn)
        assert a == b and bands == before and a != before

    def test_a_listed_candidate_is_cut_but_keeps_the_whole_sentence_span_and_still_verifies(self):
        pp, bands, so, sn = crowded()
        (combined,) = pad.with_semantic_candidates([older_band(bands, BELOW[0])], neighbours(so, sn).neighbours, so, sn, max_chars=30)
        hit = next(c for c in combined.candidates if sn[c.start:c.end].startswith(BELOW[1][:30]))
        assert len(hit.text) == 30 and hit.end - hit.start == len(BELOW[1])
        record = {"verdict": "same", "candidate": 1 + list(combined.candidates).index(hit), "quote": BELOW[1][:30] + " x",
                  "candidates": [{"text": c.text, "start": c.start, "end": c.end} for c in combined.candidates]}
        assert pad._candidate_of(record, sn)[0] is not None

    def test_a_verified_same_on_a_semantic_candidate_makes_a_below_sentence_reworded_through_the_passage_layer(self):
        pp, bands, so, sn = crowded(TARIFF, BELOW2)
        older = older_band(bands, TARIFF[0])
        (combined,) = pad.with_semantic_candidates([older], neighbours(so, sn).neighbours, so, sn)
        n = 1 + [c.text for c in combined.candidates].index(TARIFF[1])
        record = {"verdict": "same", "candidate": n, "quote": TARIFF_QUOTE,
                  "candidates": [{"text": c.text, "start": c.start, "end": c.end} for c in combined.candidates]}
        verdict, reason = pad.effective_verdict(record, TARIFF[0], sn)
        assert reason is None and verdict.verdict == "same"                               # the real 62 relatedness floor accepts it
        passages = pp.passages({older.key: verdict})
        assert any(p.kind == "reworded" and p.text == TARIFF[0] and p.counterpart_text == TARIFF[1] for p in passages)
        assert not any(p.kind == "removed" and TARIFF[0] in p.text for p in passages)


class TestProxyCandidates:
    """A dry run embeds nothing: stand-ins that are a true upper bound on the prompt, and a mean-length 'likely'."""

    def test_the_stand_ins_fill_up_to_max_candidates_with_the_longest_other_sentences_after_the_lexical_bests(self):
        pp, bands, so, sn = crowded()
        proxied, saving = pad.with_proxy_candidates(bands, so, sn)
        older = older_band(proxied, BELOW[0])
        assert len(older.candidates) == 8 and len({norm(c.text) for c in older.candidates}) == 8
        lexical = pad.combine_candidates(next(b for b in bands if b.key == older.key).candidates, [])
        assert older.candidates[:len(lexical)] == lexical
        sizes = [c.end - c.start for c in older.candidates[len(lexical):]]
        assert sizes == sorted(sizes, reverse=True) and saving[older.key] >= 0

    def test_the_stand_in_prompt_is_never_shorter_than_any_real_semantic_prompt(self):
        pp, bands, so, sn = crowded()
        proxied, _ = pad.with_proxy_candidates(bands, so, sn)
        real = pad.with_semantic_candidates(bands, neighbours(so, sn).neighbours, so, sn)
        for stand_in, actual in zip(proxied, real):
            assert len(pad.build_prompt(stand_in.text, stand_in.candidates)) >= len(pad.build_prompt(actual.text, actual.candidates))

    def test_the_likely_estimate_is_below_the_worst_case_and_the_input_is_not_mutated(self):
        pp, bands, so, sn = crowded()
        before = list(bands)
        proxied, saving = pad.with_proxy_candidates(bands, so, sn)
        assert bands == before and set(saving) >= {b.key for b in bands}
        tasks = pad.plan_tasks("P", proxied, MODEL, likely_saving=saving)
        est = pad.estimate_cost(tasks, set(), MODEL)
        assert 0 < est.likely_usd < est.worst_case_usd and any(t.likely_chars is not None for t in tasks)
        plain = pad.estimate_cost(pad.plan_tasks("P", proxied, MODEL), set(), MODEL)
        assert plain.likely_usd >= est.likely_usd and plain.worst_case_usd == est.worst_case_usd

    def test_a_section_with_too_few_long_sentences_just_lists_what_it_has(self):
        pp, bands, so, sn = crowded()
        proxied, _ = pad.with_proxy_candidates(bands, so, "Short one. " + P[0], max_chars=200, min_chars=40)
        assert len(next(b for b in proxied if b.side == "older").candidates) <= 8

    def test_the_stand_ins_are_cut_at_max_chars_but_keep_whole_sentence_spans(self):
        pp, bands, so, sn = crowded()
        proxied, _ = pad.with_proxy_candidates(bands, so, sn, max_chars=40)
        older = next(b for b in proxied if b.side == "older")
        stand_ins = older.candidates[len(pad.combine_candidates(bands[0].candidates, [])):]
        assert stand_ins and all(len(c.text) <= 40 and c.end - c.start > 40 and sn.startswith(c.text, c.start) for c in stand_ins)


# --------------------------------------------------------------------------- zones, estimates, versions

def zone_tasks():
    pp, bands, so, sn = crowded()
    return pad.plan_tasks("PAIR", bands, MODEL), bands


class TestZonesAndEstimates:
    def test_a_task_carries_the_zone_of_its_sentence_and_the_record_stores_it(self, tmp_path):
        tasks, bands = zone_tasks()
        assert {t.zone for t in tasks} == {"below"} and [t.zone for t in tasks] == [b.zone for b in bands]
        _, band_bands, _, _ = pair_bands()
        assert {t.zone for t in pad.plan_tasks("P", band_bands, MODEL)} == {"band"}
        path = tmp_path / "p.jsonl"
        pad.run_tasks(tasks[:2], adj.Checkpoint(path), FakeLLM(), model=MODEL, max_usd=0.5)
        assert [json.loads(line)["zone"] for line in path.read_text(encoding="utf-8").splitlines()] == ["below", "below"]

    def test_each_zone_is_estimated_separately_and_the_zones_add_up_to_the_total(self):
        _, band_bands, _, _ = pair_bands()
        below_tasks, _ = zone_tasks()
        tasks = pad.plan_tasks("P", band_bands, MODEL) + below_tasks
        cached = {tasks[0].key}
        zones = pad.estimates_by_zone(tasks, cached, MODEL)
        total = pad.estimate_cost(tasks, cached, MODEL)
        assert list(zones) == ["band", "below"] == list(pad.ZONES)
        assert zones["band"].n_items == len(band_bands) and zones["below"].n_items == len(below_tasks)
        assert zones["band"].n_cached == 1 and zones["below"].n_cached == 0
        assert sum(z.n_calls for z in zones.values()) == total.n_calls
        assert sum(z.worst_case_usd for z in zones.values()) == pytest.approx(total.worst_case_usd, abs=2e-6)
        assert sum(z.output_tokens_cap for z in zones.values()) == total.output_tokens_cap

    def test_an_empty_zone_is_an_estimate_of_zero_calls_and_only_the_requested_zones_are_listed(self):
        tasks, _ = zone_tasks()
        zones = pad.estimates_by_zone(tasks, set(), MODEL)
        assert zones["band"].n_calls == 0 and zones["band"].worst_case_usd == 0 and zones["below"].n_calls == len(tasks)
        assert list(pad.estimates_by_zone(tasks, set(), MODEL, zones=("below",))) == ["below"]

    def test_the_worst_case_of_one_call_is_its_prompt_bound_plus_the_output_cap(self):
        tasks, _ = zone_tasks()
        worst = pad.estimate_cost(tasks[:1], set(), MODEL).worst_case_usd
        assert worst == pytest.approx(pad.call_cost_upper_bound(tasks[0].prompt, MODEL), abs=2e-6)

    def test_answers_are_looked_up_under_the_prompt_version_asked_for(self):
        pp, bands, so, sn = crowded()
        older = older_band(bands, BELOW[0])
        record = {"verdict": "different", "candidates": []}
        v2 = {pad.task_key(older.text_hash, older.other_hash, MODEL, pad.LEGACY_PROMPT_VERSION): record}

        def resolve(records, **kw):
            return pad.resolve_verdicts([older], records, MODEL, older_text=so, newer_text=sn, **kw)

        assert resolve(v2).verdicts == {} and resolve(v2).unanswered == (older.key,)               # the default is pas-v3
        assert resolve(v2, prompt_version=pad.LEGACY_PROMPT_VERSION).verdicts == {older.key: BandVerdict("different")}
        v3 = {pad.task_key(older.text_hash, older.other_hash, MODEL): record}
        assert resolve(v3).verdicts == {older.key: BandVerdict("different")}
        assert resolve(v3, prompt_version=pad.LEGACY_PROMPT_VERSION).unanswered == (older.key,)

    def test_every_new_answer_is_recorded_under_the_current_version(self, tmp_path):
        tasks, _ = zone_tasks()
        path = tmp_path / "p.jsonl"
        pad.run_tasks(tasks[:1], adj.Checkpoint(path), FakeLLM(), model=MODEL, max_usd=0.5)
        (record,) = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert record["prompt_version"] == "pas-v3" and record["key"].split("|")[2] == "pas-v3"
