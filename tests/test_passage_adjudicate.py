"""graph/passage_adjudicate.py: the prompt, the code-enforced verdict rules, the checkpoint and cache keys, the cost guard, the
resolution of cached answers into ``BandVerdict`` values, the live probe, and the ``align-items`` CLI plumbing of the new flags.

Every model call here is a fake ``llm_json`` (or a fake completion for the probe); nothing is paid and nothing touches the network.
"""

import json

import pytest
from test_passage_bands import DISTRACTORS, HK, PARA, engine, older_band
from test_passages import P, X
from typer.testing import CliRunner

from semigraph.cli import app
from semigraph.eval import gold
from semigraph.graph import adjudicate as adj
from semigraph.graph import passage_adjudicate as pad
from semigraph.graph.align_text import SectionIndex
from semigraph.graph.passages import BandVerdict

MODEL = "openai/gpt-6-luna"
runner = CliRunner()
GOOD_QUOTE = "Advanced packaging capacity has long lead times, so responding to sudden shifts"      # a verbatim slice of PARA[1]


def pair_bands(older=None, newer=None, **params):
    """(engine, bands, older section, newer section) of a synthetic pair: an HK lookalike and a paraphrase, both in the band."""
    older = older or [[P[0], HK[0], PARA[0], X[0]]]
    newer = newer or [[P[0], HK[1], PARA[1], *DISTRACTORS[:2]]]
    pp, so, sn = engine(older, newer, **params)
    return pp, pp.band_sentences(), so, sn


def older_of(bands, text):
    return next(b for b in bands if b.side == "older" and b.text == text)


def record_for(band, verdict, candidate=None, quote=None):
    return {"verdict": verdict, "candidate": candidate, "quote": quote,
            "candidates": [{"text": c.text, "start": c.start, "end": c.end} for c in band.candidates]}


class FakeLLM:
    """A ``llm_json``-compatible stand-in that answers from a function of the prompt."""

    def __init__(self, answer=lambda prompt: {"verdict": "different"}, fail_on=None):
        self.calls, self.answer, self.fail_on = [], answer, fail_on

    def __call__(self, prompt, model_cls, *, model, max_tokens, thinking_off):
        self.calls.append({"prompt": prompt, "model": model, "max_tokens": max_tokens, "thinking_off": thinking_off})
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise RuntimeError("transient failure after retries")
        return model_cls(**self.answer(prompt))


# --------------------------------------------------------------------------- the prompt

class TestPrompt:
    def test_it_carries_the_sentence_the_numbered_candidates_and_the_answer_contract(self):
        _, bands, _, _ = pair_bands()
        band = older_of(bands, HK[0])
        prompt = pad.build_prompt(band.text, band.candidates)
        assert HK[0] in prompt and "[1] " + band.candidates[0].text in prompt
        assert '"verdict": "same" | "different"' in prompt and '"candidate"' in prompt and '"quote"' in prompt
        assert "SAME fact" in prompt and "character for character" in prompt

    def test_it_does_not_say_which_filing_is_which_so_one_answer_serves_both_sides(self):
        prompt = pad.build_prompt("A sentence.", [])
        assert "older" not in prompt.lower() and "newer" not in prompt.lower() and "FY" not in prompt

    def test_a_long_sentence_is_cut_and_says_so(self):
        prompt = pad.build_prompt("word " * 2000, [], pad.PassageAdjudicationParams(max_sentence_chars=300))
        assert "cut after the first 300 characters" in prompt and "word " * 50 in prompt and "word " * 100 not in prompt

    def test_a_missing_candidate_list_is_stated(self):
        assert "(no candidate sentence was found)" in pad.build_prompt("A sentence.", [])

    def test_the_prompt_text_is_versioned(self):
        assert pad.PROMPT_VERSION == "pas-v1"


class TestKey:
    def test_the_key_is_sentence_hash_other_section_hash_prompt_version_and_model(self):
        assert pad.task_key("s", "o", "m") == "s|o|" + pad.PROMPT_VERSION + "|m"
        assert len({pad.task_key("s", "o", "m"), pad.task_key("s2", "o", "m"), pad.task_key("s", "o2", "m"),
                    pad.task_key("s", "o", "m2"), pad.task_key("s", "o", "m", "pas-v2")}) == 5


# --------------------------------------------------------------------------- planning and cost

class TestPlan:
    def test_one_task_per_band_sentence_keyed_by_its_hashes_and_the_model(self):
        _, bands, _, _ = pair_bands()
        tasks = pad.plan_tasks("PAIR", bands, MODEL)
        assert len(tasks) == len(bands) == 4
        assert [t.key for t in tasks] == [pad.task_key(b.text_hash, b.other_hash, MODEL) for b in bands]
        assert all(t.pair_id == "PAIR" and t.prompt == pad.build_prompt(t.sentence, t.candidates) for t in tasks)
        assert [t.band_key for t in tasks] == [b.key for b in bands]

    def test_a_sentence_that_occurs_twice_against_the_same_section_is_one_task(self):
        older = [[P[0], HK[0]], [P[1], HK[0]]]
        pp, so, sn = engine(older, [[P[0], HK[1]]], older_labels={"o0": "reworded", "o1": "reworded"},
                            newer_labels={"n0": ("carried", "o0")})
        bands = pp.band_sentences()
        assert len([b for b in bands if b.side == "older"]) == 2
        tasks = pad.plan_tasks("PAIR", bands, MODEL)
        assert len(tasks) == len({t.key for t in tasks}) == len(bands) - 1

    def test_the_estimate_counts_uncached_calls_and_prices_the_worst_case(self):
        _, bands, _, _ = pair_bands()
        tasks = pad.plan_tasks("PAIR", bands, MODEL)
        est = pad.estimate_cost(tasks, {tasks[0].key}, MODEL)
        assert (est.n_items, est.n_cached, est.n_calls, est.priced, est.model) == (4, 1, 3, True, MODEL)
        assert 0 < est.likely_usd < est.worst_case_usd < 0.01
        assert est.output_tokens_cap == 3 * pad.PassageAdjudicationParams().max_output_tokens
        assert pad.estimate_cost(tasks, {t.key for t in tasks}, MODEL).n_calls == 0

    def test_an_unpriced_model_is_flagged(self):
        _, bands, _, _ = pair_bands()
        assert pad.estimate_cost(pad.plan_tasks("P", bands, "vendor/unknown"), set(), "vendor/unknown").priced is False

    def test_the_output_cap_is_small_because_the_answer_is_one_short_json_object(self):
        assert pad.PassageAdjudicationParams().max_output_tokens <= 400


class TestRun:
    def tasks(self):
        _, bands, _, _ = pair_bands()
        return pad.plan_tasks("PAIR", bands, MODEL)

    def test_the_run_refuses_before_any_call_when_the_worst_case_exceeds_the_cap(self, tmp_path):
        llm, cp = FakeLLM(), adj.Checkpoint(tmp_path / "p.jsonl")
        with pytest.raises(pad.BudgetExceeded, match="exceeds --max-usd"):
            pad.run_tasks(self.tasks(), cp, llm, model=MODEL, max_usd=1e-7)
        assert llm.calls == [] and not (tmp_path / "p.jsonl").exists()

    def test_it_stops_when_the_next_calls_bound_would_pass_the_cap_and_keeps_what_was_bought(self, tmp_path, monkeypatch):
        """The running total of per-call upper bounds is a second guard behind the up-front worst case."""
        tasks, path = self.tasks(), tmp_path / "p.jsonl"
        monkeypatch.setattr(pad, "call_cost_upper_bound", lambda prompt, model, params=None: 0.4)
        llm = FakeLLM()
        with pytest.raises(pad.BudgetExceeded, match="stopped after 1 call"):
            pad.run_tasks(tasks, adj.Checkpoint(path), llm, model=MODEL, max_usd=0.5)
        assert len(llm.calls) == 1 and len(path.read_text(encoding="utf-8").splitlines()) == 1

    def test_every_answer_is_checkpointed_with_what_the_model_saw_and_a_rerun_never_repays(self, tmp_path):
        tasks, path = self.tasks(), tmp_path / pad.CHECKPOINT_NAME
        llm = FakeLLM()
        assert pad.run_tasks(tasks, adj.Checkpoint(path), llm, model=MODEL, max_usd=0.5) == 4
        assert all(c["thinking_off"] and c["model"] == MODEL and c["max_tokens"] == pad.PassageAdjudicationParams().max_output_tokens
                   for c in llm.calls)
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [r["key"] for r in lines] == [t.key for t in tasks]
        first = lines[0]
        assert first["verdict"] == "different" and first["candidate"] is None and first["quote"] is None
        assert first["prompt_version"] == pad.PROMPT_VERSION and first["model"] == MODEL and first["sentence"] == tasks[0].sentence
        assert [c["text"] for c in first["candidates"]] == [c.text for c in tasks[0].candidates]
        assert all(set(c) == {"text", "start", "end"} for c in first["candidates"]) and first["est_usd_upper_bound"] > 0
        again = FakeLLM()
        assert pad.run_tasks(tasks, adj.Checkpoint(path), again, model=MODEL, max_usd=0.5) == 0 and again.calls == []

    def test_a_different_model_repays_because_the_model_is_part_of_the_key(self, tmp_path):
        _, bands, _, _ = pair_bands()
        path = tmp_path / "p.jsonl"
        pad.run_tasks(pad.plan_tasks("P", bands, "m1"), adj.Checkpoint(path), FakeLLM(), model="m1", max_usd=0.5)
        other = FakeLLM()
        assert pad.run_tasks(pad.plan_tasks("P", bands, "m2"), adj.Checkpoint(path), other, model="m2", max_usd=0.5) == 4

    def test_a_failing_call_aborts_and_keeps_what_was_already_bought(self, tmp_path):
        tasks, path = self.tasks(), tmp_path / "p.jsonl"
        with pytest.raises(RuntimeError, match="transient failure"):
            pad.run_tasks(tasks, adj.Checkpoint(path), FakeLLM(fail_on=3), model=MODEL, max_usd=0.5)
        assert len(path.read_text(encoding="utf-8").splitlines()) == 2
        resume = FakeLLM()
        assert pad.run_tasks(tasks, adj.Checkpoint(path), resume, model=MODEL, max_usd=0.5) == 2


# --------------------------------------------------------------------------- the rules

class TestRules:
    """Each rule of ``effective_verdict``; ``pair_bands`` puts a paraphrase of PARA[0] in the newer section as PARA[1]."""

    def setup_method(self):
        _, self.bands, self.so, self.sn = pair_bands()
        self.band = older_of(self.bands, PARA[0])
        self.index = SectionIndex(self.sn)
        self.n = 1 + [c.text for c in self.band.candidates].index(PARA[1])            # the candidate number of the paraphrase

    def check(self, verdict="same", candidate=None, quote=GOOD_QUOTE, sentence=None, text=None, record=None):
        record = record or record_for(self.band, verdict, self.n if candidate is None else candidate, quote)
        return pad.effective_verdict(record, sentence or self.band.text, text or self.sn, index=self.index)

    def test_the_floors_are_those_of_the_gold(self):
        p = pad.PassageAdjudicationParams()
        assert p.min_relatedness == gold.SENT_REWORDED_MIN_SIM == 62 and p.min_quote_chars == gold.SENT_MIN_QUOTE_CHARS == 30

    def test_different_passes_through_without_a_quote(self):
        assert self.check("different", quote=None) == (BandVerdict("different"), None)
        assert self.check("different", candidate=99, quote="whatever")[0] == BandVerdict("different")

    def test_a_verbatim_related_quote_from_the_chosen_candidate_gives_its_span(self):
        cand = self.band.candidates[self.n - 1]
        verdict, reason = self.check(quote=GOOD_QUOTE)
        assert reason is None and verdict == BandVerdict("same", (cand.start, cand.end))
        assert self.sn[cand.start:cand.end] == PARA[1]
        assert pad.relatedness(PARA[0], GOOD_QUOTE) >= 62

    def test_whitespace_and_quote_style_do_not_break_verbatim_containment(self):
        verdict, _ = self.check(quote="ADVANCED packaging  capacity\nhas long lead times, so responding to sudden shifts")
        assert verdict is not None and verdict.verdict == "same"

    def test_a_true_paraphrase_quoted_by_a_fragment_the_sentence_barely_resembles_is_not_accepted(self):
        """The 62 floor is the gold's; a fragment of the paraphrase that shares too little with the sentence leaves it band-reworded."""
        fragment = "responding to sudden shifts in customer demand is difficult for us"
        assert pad.relatedness(PARA[0], fragment) < 62
        verdict, reason = self.check(quote=fragment)
        assert verdict is None and "not related" in reason

    @pytest.mark.parametrize("candidate", [None, 0, 99, "2", True])
    def test_a_candidate_number_that_is_not_one_of_the_shown_candidates_is_no_verdict(self, candidate):
        record = record_for(self.band, "same", None, PARA[1])
        record["candidate"] = candidate
        verdict, reason = self.check(record=record)
        assert verdict is None and "candidate" in reason

    def test_a_missing_or_short_quote_is_no_verdict(self):
        for quote in (None, "", "  ", "responding to sudden shifts"):                 # 27 characters: below the floor of 30
            verdict, reason = self.check(quote=quote)
            assert verdict is None and "quote" in reason

    def test_a_fabricated_quote_is_no_verdict(self):
        verdict, reason = self.check(quote="Packaging capacity has long lead times and this quote was invented by the model.")
        assert verdict is None and "verbatim" in reason

    def test_a_quote_that_is_in_the_section_but_not_in_the_chosen_candidate_is_no_verdict(self):
        other = DISTRACTORS[0]
        verdict, reason = self.check(quote=other)
        assert verdict is None and "chosen candidate" in reason

    def test_a_verbatim_quote_that_is_unrelated_to_the_sentence_is_no_verdict(self):
        band = older_of(self.bands, HK[0])
        cands = [c.text for c in band.candidates]
        record = record_for(band, "same", 1 + cands.index(HK[1]), HK[1])
        verdict, reason = pad.effective_verdict(record, "Severe weather could interrupt component deliveries for several quarters.",
                                                self.sn, index=self.index)
        assert verdict is None and "not related" in reason

    def test_a_candidate_that_is_not_a_slice_of_the_other_section_is_no_verdict(self):
        verdict, reason = self.check(text="A different section text altogether. " + self.sn)
        assert verdict is None and "slice of the other section" in reason

    def test_an_unknown_verdict_is_unusable(self):
        verdict, reason = pad.effective_verdict({"verdict": "maybe"}, "s", "t")
        assert verdict is None and "unusable" in reason

    def test_the_index_is_optional(self):
        record = record_for(self.band, "same", self.n, PARA[1])
        assert pad.effective_verdict(record, self.band.text, self.sn)[0].verdict == "same"


# --------------------------------------------------------------------------- resolving cached answers

class TestResolve:
    def setup_method(self):
        self.pp, self.bands, self.so, self.sn = pair_bands()
        self.hk, self.para = older_of(self.bands, HK[0]), older_of(self.bands, PARA[0])

    def records(self, *pairs, model=MODEL):
        return {pad.task_key(b.text_hash, b.other_hash, model): r for b, r in pairs}

    def resolve(self, records, model=MODEL):
        return pad.resolve_verdicts(self.bands, records, model, older_text=self.so, newer_text=self.sn)

    def test_answers_become_verdicts_by_band_key_and_the_rest_is_reported_as_unanswered(self):
        n = 1 + [c.text for c in self.para.candidates].index(PARA[1])
        got = self.resolve(self.records((self.hk, record_for(self.hk, "different")), (self.para, record_for(self.para, "same", n, PARA[1]))))
        assert got.verdicts[self.hk.key] == BandVerdict("different")
        assert got.verdicts[self.para.key].verdict == "same" and got.verdicts[self.para.key].counterpart_span is not None
        assert set(got.unanswered) == {b.key for b in self.bands} - {self.hk.key, self.para.key} and not got.rejected

    def test_a_rejected_same_is_listed_with_its_reason_and_gets_no_verdict(self):
        got = self.resolve(self.records((self.para, record_for(self.para, "same", 1, "an invented quote that is nowhere at all"))))
        assert self.para.key not in got.verdicts and "verbatim" in got.rejected[self.para.key]

    def test_answers_of_another_model_or_prompt_version_are_not_used(self):
        records = self.records((self.hk, record_for(self.hk, "different")), model="another/model")
        assert self.resolve(records).verdicts == {}
        assert len(self.resolve(records, model="another/model").verdicts) == 1

    def test_the_answer_for_a_sentence_holds_wherever_that_sentence_occurs_against_the_same_section(self):
        older = [[P[0], HK[0]], [P[1], HK[0]]]
        pp, so, sn = engine(older, [[P[0], HK[1]]], older_labels={"o0": "reworded", "o1": "reworded"}, newer_labels={"n0": ("carried", "o0")})
        bands = pp.band_sentences()
        twins = [b for b in bands if b.text == HK[0]]
        assert len(twins) == 2
        key = pad.task_key(twins[0].text_hash, twins[0].other_hash, MODEL)
        got = pad.resolve_verdicts(bands, {key: record_for(twins[0], "different")}, MODEL, older_text=so, newer_text=sn)
        assert {b.key for b in twins} <= set(got.verdicts)

    def test_the_older_side_is_validated_against_the_newer_section_and_the_newer_side_against_the_older(self):
        newer_band = next(b for b in self.bands if b.side == "newer" and b.text == PARA[1])
        quote = "Long lead times for advanced packaging capacity make it difficult to respond"
        assert pad.relatedness(PARA[1], quote) >= 62
        rec = record_for(newer_band, "same", 1 + [c.text for c in newer_band.candidates].index(PARA[0]), quote)
        got = self.resolve(self.records((newer_band, rec)))
        span = got.verdicts[newer_band.key].counterpart_span
        assert self.so[span[0]:span[1]] == PARA[0]                                       # a span of the OLDER section


# --------------------------------------------------------------------------- end to end through the passage layer

class TestThroughPassages:
    """Fake model -> checkpoint -> resolved verdicts -> passages. The outcomes the layer must reproduce."""

    def run_model(self, tmp_path, answer):
        pp, bands, so, sn = pair_bands()
        tasks = pad.plan_tasks("PAIR", bands, MODEL)
        cp = adj.Checkpoint(tmp_path / "p.jsonl")
        pad.run_tasks(tasks, cp, FakeLLM(answer), model=MODEL, max_usd=0.5)
        return pp, bands, pad.resolve_verdicts(bands, cp.records, MODEL, older_text=so, newer_text=sn)

    @staticmethod
    def covers(passages, kind, text):
        """Is ``text`` inside a passage of ``kind``? (adjacent removed sentences share one passage)"""
        return any(text in p.text for p in passages if p.kind == kind)

    def test_a_model_that_says_different_removes_the_lookalike_and_it_says_so(self, tmp_path):
        pp, bands, resolved = self.run_model(tmp_path, lambda prompt: {"verdict": "different"})
        passages = pp.passages(resolved.verdicts)
        assert self.covers(passages, "removed", HK[0]) and self.covers(passages, "removed", PARA[0])
        assert not [p for p in passages if p.kind == "reworded"]
        assert all(p.decided_by == "sentence_absent_llm" and p.band_adjudicated for p in passages if p.kind == "removed")

    def test_a_model_that_finds_the_paraphrase_keeps_it_reworded_with_the_verified_counterpart(self, tmp_path):
        def answer(prompt):
            if "<<<\n" + PARA[0] + "\n>>>" in prompt:                                 # PARA[0] is the SENTENCE, not a candidate
                shown = [line for line in prompt.splitlines() if line.startswith("[") and PARA[1] in line][0]
                return {"verdict": "same", "candidate": int(shown[1:shown.index("]")]), "quote": GOOD_QUOTE}
            return {"verdict": "different"}

        pp, _, resolved = self.run_model(tmp_path, answer)
        passages = pp.passages(resolved.verdicts)
        (para,) = [p for p in passages if PARA[0] in p.text and p.kind == "reworded"]
        assert para.counterpart_text == PARA[1] and para.decided_by == "sentence_reworded_llm" and para.band_adjudicated
        assert self.covers(passages, "removed", HK[0])

    def test_a_same_with_an_invented_quote_changes_nothing_and_is_never_a_removal(self, tmp_path):
        pp, _, resolved = self.run_model(
            tmp_path, lambda prompt: {"verdict": "same", "candidate": 1, "quote": "a quote the model made up, present nowhere"})
        assert resolved.verdicts == {} and len(resolved.rejected) == 4
        passages = pp.passages(resolved.verdicts)
        assert passages == pp.passages()
        assert {p.decided_by for p in passages if p.kind == "reworded"} == {"sentence_reworded_band"}
        assert not self.covers(passages, "removed", HK[0]) and not self.covers(passages, "removed", PARA[0])

    def test_the_newer_side_is_settled_too_and_added_needs_its_own_different_verdict(self, tmp_path):
        pp, bands, resolved = self.run_model(tmp_path, lambda prompt: {"verdict": "different"})
        assert {HK[1], PARA[1]} <= {b.text for b in bands if b.side == "newer"}
        passages = pp.passages(resolved.verdicts)
        assert self.covers(passages, "added", HK[1]) and self.covers(passages, "added", PARA[1])
        older_only = pp.passages({k: v for k, v in resolved.verdicts.items() if k.startswith("o0@")})
        assert not self.covers(older_only, "added", HK[1]) and not self.covers(older_only, "added", PARA[1])


# --------------------------------------------------------------------------- the default call

class TestDefaultCall:
    def test_it_is_the_item_level_dispatch(self):
        assert pad.default_llm_json is adj.default_llm_json

    def test_a_gpt6_reply_with_a_candidate_number_validates_through_the_provider_aware_text_call(self, monkeypatch):
        replies = iter(['Sure! {"verdict": "maybe"}', '```json\n{"verdict": "same", "candidate": 2, "quote": "abc"}\n```'])
        monkeypatch.setattr("semigraph.retrieval.answerer.llm_text", lambda prompt, *, model, max_tokens: next(replies))
        out = pad.default_llm_json("p", pad.PassageVerdict, model=MODEL, max_tokens=300)
        assert (out.verdict, out.candidate, out.quote) == ("same", 2, "abc")

    def test_the_verdict_schema_accepts_null_and_rejects_other_verdicts(self):
        assert pad.PassageVerdict(verdict="different").candidate is None
        with pytest.raises(ValueError):
            pad.PassageVerdict(verdict="removed")


# --------------------------------------------------------------------------- the live probe (never called live here)

class Reply:
    def __init__(self, text, finish_reason="stop", usage=None):
        self.text, self.finish_reason, self.usage = text, finish_reason, usage or {"prompt_tokens": 300, "completion_tokens": 40}


class TestProbe:
    def good(self, calls):
        def complete(model, prompt, max_tokens):
            calls.append((model, prompt, max_tokens))
            if "out of China and Hong Kong" in prompt:
                return Reply('{"verdict": "different", "candidate": null, "quote": null}')
            line = [ln for ln in prompt.splitlines() if ln.startswith("[") and "one foundry in Taiwan" in ln][0]
            quote = line[line.index("]") + 2:]                                            # the whole candidate, verbatim
            return Reply('```json\n' + json.dumps({"verdict": "same", "candidate": int(line[1:line.index("]")]), "quote": quote})
                         + '\n```')
        return complete

    def test_exactly_two_calls_one_paraphrase_and_one_unrelated_pair_both_as_expected(self):
        calls = []
        results = pad.probe(MODEL, complete=self.good(calls))
        assert len(calls) == 2 and [c[0] for c in calls] == [MODEL, MODEL]
        assert [r.expect for r in results] == ["same", "different"] and all(r.as_expected for r in results)
        assert [r.effective for r in results] == ["same", "different"]
        assert results[0].parsed.candidate is not None and results[0].raw.startswith("```json")

    def test_a_reply_the_code_rules_reject_is_reported_with_the_reason_and_is_not_as_expected(self):
        def bad(model, prompt, max_tokens):
            return Reply('{"verdict": "same", "candidate": 1, "quote": "a quote that appears in no candidate at all, sorry"}')
        results = pad.probe(MODEL, complete=bad)
        assert results[0].effective == "rejected" and "verbatim" in results[0].reason and not results[0].as_expected
        assert not results[1].as_expected                                              # "same" was expected to be "different"

    def test_a_reply_that_is_not_json_is_reported_not_raised(self):
        results = pad.probe(MODEL, complete=lambda m, p, t: Reply("I cannot answer that.", "length"))
        assert all(r.effective == "unparsed" and r.parsed is None and r.parse_error for r in results)
        assert results[0].finish_reason == "length"

    def test_the_report_shows_the_raw_reply_the_parsed_verdict_the_rules_outcome_and_the_usage(self):
        text = pad.format_probe(pad.probe(MODEL, complete=self.good([])))
        for needle in ("raw reply", "```json", "parsed", "PassageVerdict", "code rules", "ACCEPTED", "usage", "prompt_tokens",
                       "as expected", "finish_reason"):
            assert needle in text, needle

    def test_the_probe_costs_less_than_a_tenth_of_a_cent_at_the_default_model(self):
        est = pad.estimate_probe(MODEL)
        assert est.n_calls == 2 and est.worst_case_usd < 0.001
        prompts = pad.probe_prompts()
        assert len(prompts) == 2 and all("SAME fact" in p.prompt for p in prompts)

    def test_the_probe_refuses_a_model_whose_worst_case_is_over_its_own_guard(self):
        with pytest.raises(pad.BudgetExceeded):
            pad.probe(MODEL, complete=lambda m, p, t: Reply("{}"), max_usd=1e-9)


# --------------------------------------------------------------------------- CLI plumbing

@pytest.fixture
def align_run(monkeypatch, tmp_path):
    from semigraph import cli
    from semigraph.config import Settings
    from semigraph.graph import items

    state = {"calls": [], "raises": None, "estimate": None, "passage_estimate": None, "budget": None, "used": 0}

    def fake(settings, tickers=None, **kwargs):
        state["calls"].append({"tickers": tickers, **kwargs})
        if state["raises"]:
            raise state["raises"]
        summary = [{"pair_id": "NVDA-a-b", "ticker": "NVDA", "older_date": "d", "newer_date": "e", "compared": True,
                    "not_compared_reason": None, "older_items": 2, "newer_items": 2, "older_unchanged": 1, "older_reworded": 1,
                    "older_merged": 0, "older_removed": 0, "older_uncertain": 0, "newer_carried": 2, "newer_new": 0,
                    "newer_uncertain": 0, "passages_removed": 1, "passages_reworded": 0, "passages_added": 0, "adjudicated": 0,
                    "band": 7, "band_answered": 3}]
        return items.AlignRun(summary, state["estimate"], [], bool(kwargs.get("dry_run")), state["passage_estimate"], state["budget"],
                              state["used"])

    monkeypatch.setattr(cli, "_settings", lambda: Settings(data_dir=tmp_path / "data", _env_file=None))
    monkeypatch.setattr(items, "run_align_items", fake)
    return state


def test_align_items_passes_the_passage_flag_and_prints_the_passage_estimate(align_run):
    align_run["passage_estimate"] = adj.Estimate(MODEL, 173, 20, 153, 90000, 45900, 0.03, 0.002, True)
    align_run["budget"] = 0.5
    result = runner.invoke(app, ["align-items", "--adjudicate-passages", "--dry-run", "--max-usd", "0.5"])
    assert result.exit_code == 0, result.output
    assert align_run["calls"][0]["adjudicate_passages"] is True and align_run["calls"][0]["max_usd"] == 0.5
    out = result.output
    assert "Passage adjudication estimate" in out and "173 band sentence(s) to settle (20 already answered)" in out
    assert "153 call(s)" in out and "worst case $0.0300" in out and "would be refused" not in out


def test_the_passage_estimate_warns_when_the_remaining_budget_would_refuse(align_run):
    align_run["passage_estimate"] = adj.Estimate(MODEL, 5000, 0, 5000, 3000000, 1500000, 1.2, 0.2, True)
    align_run["budget"] = 0.5
    out = runner.invoke(app, ["align-items", "--adjudicate-passages", "--dry-run"]).output
    assert "worst case $1.2000" in out and "would be refused before any call" in out


def test_without_the_flag_no_passage_estimate_is_printed_and_the_default_is_off(align_run):
    result = runner.invoke(app, ["align-items"])
    assert align_run["calls"][0]["adjudicate_passages"] is False and "Passage adjudication estimate" not in result.output


def test_replayed_verdicts_are_reported_when_the_cache_was_used_without_the_flag(align_run):
    align_run["used"] = 12
    out = runner.invoke(app, ["align-items"]).output
    assert "12 cached passage verdict(s) applied" in out and "no model was called" in out


def test_both_adjudications_can_be_requested_together(align_run):
    runner.invoke(app, ["align-items", "--adjudicate", "--adjudicate-passages"])
    call = align_run["calls"][0]
    assert call["adjudicate"] is True and call["adjudicate_passages"] is True


def test_the_passage_budget_exceeded_exits_3(align_run):
    align_run["raises"] = pad.BudgetExceeded("worst case $1.2000 for 5000 call(s) exceeds --max-usd $0.50: nothing was spent")
    result = runner.invoke(app, ["align-items", "--adjudicate-passages"])
    assert result.exit_code == 3 and "exceeds --max-usd" in result.output


def test_the_summary_table_shows_the_band_columns(align_run):
    out = runner.invoke(app, ["align-items"]).output
    assert "band" in out.splitlines()[0]


def test_the_probe_dry_run_prints_the_prompts_and_the_estimate_and_calls_nothing(align_run, monkeypatch):
    monkeypatch.setattr(pad, "probe", lambda *a, **k: pytest.fail("the probe must not call the model on --dry-run"))
    result = runner.invoke(app, ["align-items", "--probe-adjudicator", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "SAME fact" in result.output and "worst case" in result.output and "no model was called" in result.output
    assert align_run["calls"] == []                                                    # never touches the lake


def test_the_probe_prints_the_raw_reply_the_parsed_verdict_and_the_rules_outcome(align_run, monkeypatch):
    calls, real_probe = [], pad.probe

    def fake_probe(model, **kwargs):
        calls.append(model)
        return real_probe(model, complete=lambda m, p, t: Reply('{"verdict": "different", "candidate": null, "quote": null}'))

    monkeypatch.setattr(pad, "probe", fake_probe)
    result = runner.invoke(app, ["align-items", "--probe-adjudicator"])
    assert result.exit_code == 0 and calls == [MODEL] and align_run["calls"] == []
    assert "raw reply" in result.output and "PassageVerdict" in result.output and "code rules" in result.output


def test_a_probe_call_that_fails_exits_4_with_the_message(align_run, monkeypatch):
    def boom(model, **kwargs):
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(pad, "probe", boom)
    result = runner.invoke(app, ["align-items", "--probe-adjudicator"])
    assert result.exit_code == 4 and "401 unauthorized" in result.output
