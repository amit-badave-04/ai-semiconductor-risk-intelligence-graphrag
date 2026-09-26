"""graph/adjudicate.py: candidate passages, the prompt, the code-enforced verdict rules, the checkpoint and the cost guard.

Every model call here is a fake ``llm_json``; nothing is paid and nothing touches the network.
"""

import json

import pytest

from semigraph.graph import adjudicate as adj
from semigraph.graph.align_text import SectionIndex
from semigraph.graph.alignment import AlignmentResult, AlignParams, Evidence, NewerDecision, OlderDecision, align

N1 = ("Our wafer supply depends on a single foundry in Taiwan. Any disruption at that foundry would delay shipments "
      "to our largest customers for several quarters.")
N2 = ("We rely on outsourced assembly and test providers located in Southeast Asia. A regional shutdown could "
      "interrupt our product deliveries.")
N3 = ("Changes in tax law in the jurisdictions where we operate could raise our effective tax rate. Audits by tax "
      "authorities may also result in additional payments.")
FILLER = ("Our common stock price has been volatile and may continue to be volatile for reasons unrelated to our "
          "operating performance.")
NEWER = "\n".join([N1, N2, N3, FILLER])


def rows(*texts, start=0):
    out, pos = [], start
    for n, text in enumerate(texts):
        out.append({"item_id": f"n{n}", "text": text, "char_start": pos, "char_end": pos + len(text)})
        pos += len(text) + 1
    return out


NEWER_ROWS = rows(N1, N2, N3, FILLER)
A_TEXT = ("Wafer supply concentration. Our wafer supply depends on a single foundry in Taiwan. Any disruption at "
          "that foundry would delay shipments to our largest customers for several quarters.")
B_TEXT = ("Pandemic disruption. The pandemic has interrupted our operations and our suppliers in unforeseen ways "
          "and could do so again in the coming fiscal year.")
OLDER_ROWS = [{"item_id": "a", "headline": "Wafer supply concentration.", "text": A_TEXT, "text_hash": "ha", "unit_kind": "headline"},
              {"item_id": "b", "headline": "Pandemic disruption.", "text": B_TEXT, "text_hash": "hb", "unit_kind": "headline"}]


def older(item_id, label, matched=None, evidence=None, decided_by="text_check"):
    return OlderDecision(item_id, label, matched, decided_by, evidence or Evidence())


def newer(item_id, label, matched=None, decided_by="unmatched"):
    return NewerDecision(item_id, label, matched, decided_by, Evidence())


ABSENT = Evidence(search_terms=("p1", "p2", "p3"))                       # probes tried, none found: absent
PARTIAL = Evidence(search_terms=("p1", "p2", "p3"), hit_terms=("p1",), quote_score=90.0)


def result(older_decisions, newer_decisions):
    return AlignmentResult(tuple(older_decisions), tuple(newer_decisions), AlignParams())


def settle(res, records, older_rows=OLDER_ROWS):
    return adj.settle(res, older_rows, NEWER_ROWS, NEWER, records)


def by_id(decisions):
    return {d.item_id: d for d in decisions}


# --------------------------------------------------------------------------- candidate passages and the prompt

class TestCandidates:
    def test_the_best_candidate_is_the_window_that_holds_the_item_and_every_candidate_is_verbatim(self):
        cands = adj.candidate_passages(A_TEXT, NEWER)
        assert cands[0].text.startswith("Our wafer supply depends on a single foundry")
        assert all(NEWER[c.start:c.end] == c.text for c in cands)

    def test_candidates_never_overlap_and_are_capped(self):
        cands = adj.candidate_passages(A_TEXT, NEWER, adj.AdjudicationParams(n_candidates=2))
        assert len(cands) <= 2
        spans = sorted((c.start, c.end) for c in adj.candidate_passages(A_TEXT, NEWER))
        assert all(a_end <= b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:]))

    def test_the_selection_is_deterministic(self):
        assert adj.candidate_passages(A_TEXT, NEWER) == adj.candidate_passages(A_TEXT, NEWER)

    def test_an_item_that_shares_no_word_with_the_section_has_no_candidate(self):
        assert adj.candidate_passages("Zzzz qqqq xxxx.", NEWER) == []

    def test_an_empty_section_has_no_candidate(self):
        assert adj.candidate_passages(A_TEXT, "") == []

    def test_a_single_long_sentence_is_cut_but_stays_a_verbatim_slice(self):
        long_sentence = "wafer supply foundry " * 200
        cands = adj.candidate_passages(A_TEXT, long_sentence + ". Something else.")
        assert cands and all(len(c.text) <= 1500 for c in cands)
        assert cands[0].text in long_sentence + ". Something else."


class TestPrompt:
    def test_it_carries_the_item_the_numbered_passages_and_the_answer_contract(self):
        prompt = adj.build_prompt(A_TEXT, adj.candidate_passages(A_TEXT, NEWER))
        assert A_TEXT in prompt and "[1] " in prompt and '"verdict"' in prompt and "verbatim" in prompt
        assert "same" in prompt and "reworded" in prompt and "removed" in prompt

    def test_a_long_item_is_cut_and_says_so(self):
        prompt = adj.build_prompt("word " * 2000, [])
        assert "cut after the first 2500 characters" in prompt and "(no similar passage was found)" in prompt

    def test_the_prompt_version_is_part_of_the_checkpoint_key(self):
        assert adj.task_key("h", "s", "m", "adj-v1") != adj.task_key("h", "s", "m", "adj-v2")
        assert adj.task_key("h", "s", "m1") != adj.task_key("h", "s", "m2")


# --------------------------------------------------------------------------- the rules: verdict validation

class TestValidateVerdict:
    index = SectionIndex(NEWER)

    def check(self, verdict, quote, older_row=OLDER_ROWS[0]):
        return adj.validate_verdict({"verdict": verdict, "quote": quote}, older_row, self.index)

    def test_removed_passes_through_without_a_quote(self):
        assert self.check("removed", None) == ("removed", None, None)

    def test_a_verbatim_related_quote_is_accepted_and_returns_its_span_in_the_section(self):
        effective, hit, reason = self.check("same", "Our wafer supply depends on a single foundry in Taiwan.")
        assert (effective, reason) == ("same", None)
        assert NEWER[hit.span[0]:hit.span[1]] == hit.quote

    def test_whitespace_and_quote_style_do_not_break_verbatim_containment(self):
        effective, _, _ = self.check("reworded", "our  wafer supply\ndepends on a single foundry in taiwan.")
        assert effective == "reworded"

    def test_a_fabricated_quote_falls_back_to_uncertain(self):
        effective, hit, reason = self.check("same", "Our wafers are made everywhere on earth by several vendors.")
        assert effective == "uncertain" and hit is None and "verbatim" in reason

    def test_a_missing_or_too_short_quote_falls_back_to_uncertain(self):
        assert self.check("reworded", None)[0] == "uncertain"
        assert self.check("reworded", "single foundry")[0] == "uncertain"

    def test_a_verbatim_quote_that_is_unrelated_to_the_item_falls_back_to_uncertain(self):
        effective, _, reason = self.check("same", "Changes in tax law in the jurisdictions where we operate could raise our effective tax rate.",
                                          older_row=OLDER_ROWS[1])
        assert effective == "uncertain" and "not related" in reason

    def test_an_unknown_verdict_is_unusable(self):
        assert self.check("maybe", "x")[0] == "uncertain"


# --------------------------------------------------------------------------- the rules: settling

class TestSettle:
    def test_an_aligner_removed_item_stays_removed_only_when_the_model_agrees(self):
        res = result([older("a", "merged", "n0", decided_by="text_check"), older("b", "removed", evidence=ABSENT)],
                     [newer("n0", "carried", "a"), newer("n1", "new"), newer("n2", "new"), newer("n3", "new")])
        out = settle(res, {"b": {"verdict": "removed", "quote": None}})
        b = by_id(out.result.older)["b"]
        assert (b.label, b.decided_by, b.matched_newer_id) == ("removed", "llm", None) and out.adjudicated == {"b"}

    def test_an_aligner_removed_item_that_the_model_says_survives_is_present_never_removed(self):
        res = result([older("b", "removed", evidence=ABSENT)], [newer("n1", "new")])
        out = settle(res, {"b": {"verdict": "reworded", "quote": "not in the section at all, made up entirely by the model"}},
                     older_rows=[OLDER_ROWS[1]])
        b = by_id(out.result.older)["b"]
        assert b.label == "uncertain" and b.matched_newer_id is None and "rejected" in out.notes["b"]

    def test_a_valid_quote_in_another_newer_item_merges_the_item_into_it_and_the_newer_item_is_carried(self):
        res = result([older("a", "removed", evidence=ABSENT)],
                     [newer("n0", "new"), newer("n1", "new"), newer("n2", "new"), newer("n3", "new")])
        out = settle(res, {"a": {"verdict": "reworded", "quote": "Any disruption at that foundry would delay shipments to our largest customers"}})
        a = by_id(out.result.older)["a"]
        assert (a.label, a.matched_newer_id, a.decided_by) == ("merged", "n0", "llm")
        assert a.evidence.quote and a.evidence.quote_span and a.evidence.quote_item_id == "n0"
        n = by_id(out.result.newer)
        assert (n["n0"].label, n["n0"].matched_older_id, n["n0"].decided_by) == ("carried", "a", "llm")
        assert n["n1"].label == "new"

    def test_a_valid_quote_in_the_aligners_own_candidate_pairs_the_two_items_by_the_verdict(self):
        res = result([older("a", "uncertain", "n0", evidence=PARTIAL, decided_by="uncertain")],
                     [newer("n0", "uncertain", "a", decided_by="uncertain"), newer("n1", "new")])
        same = settle(res, {"a": {"verdict": "same", "quote": "Our wafer supply depends on a single foundry in Taiwan."}})
        a = by_id(same.result.older)["a"]
        assert (a.label, a.matched_newer_id, a.decided_by) == ("unchanged", "n0", "llm")
        assert by_id(same.result.newer)["n0"].label == "carried"
        reworded = settle(res, {"a": {"verdict": "reworded", "quote": "Our wafer supply depends on a single foundry in Taiwan."}})
        assert by_id(reworded.result.older)["a"].label == "reworded"

    def test_a_valid_quote_that_lies_in_no_single_newer_item_leaves_the_item_uncertain(self):
        rows_split = rows(N1[:30], N1[30:])                          # the sentence straddles two newer items
        res = result([older("a", "removed", evidence=ABSENT)], [newer("n0", "new"), newer("n1", "new")])
        out = adj.settle(res, OLDER_ROWS[:1], rows_split, NEWER, {"a": {"verdict": "same", "quote": "Our wafer supply depends on a single foundry in Taiwan."}})
        a = by_id(out.result.older)["a"]
        assert a.label == "uncertain" and a.matched_newer_id is None
        assert {n.label for n in out.result.newer} == {"new"}

    def test_a_removed_verdict_on_an_uncertain_item_still_meets_the_false_drop_guard(self):
        blocked = result([older("a", "uncertain", "n0", evidence=PARTIAL, decided_by="uncertain")], [newer("n0", "uncertain", "a")])
        assert by_id(settle(blocked, {"a": {"verdict": "removed", "quote": None}}).result.older)["a"].label == "uncertain"
        absent = result([older("a", "uncertain", "n0", evidence=ABSENT, decided_by="uncertain")], [newer("n0", "uncertain", "a")])
        assert by_id(settle(absent, {"a": {"verdict": "removed", "quote": None}}).result.older)["a"].label == "removed"

    def test_an_item_without_a_record_keeps_the_aligners_label_and_is_not_adjudicated(self):
        res = result([older("a", "uncertain", "n0", evidence=PARTIAL), older("b", "removed", evidence=ABSENT)],
                     [newer("n0", "uncertain", "a")])
        out = settle(res, {})
        assert out.result == res and out.adjudicated == frozenset()

    def test_only_uncertain_and_removed_items_may_have_a_record(self):
        res = result([older("a", "unchanged", "n0")], [newer("n0", "carried", "a")])
        with pytest.raises(ValueError, match="only uncertain / removed"):
            settle(res, {"a": {"verdict": "same", "quote": "x"}})

    def test_settling_does_not_mutate_its_input(self):
        res = result([older("b", "removed", evidence=ABSENT)], [newer("n1", "new")])
        before = repr(res)
        settle(res, {"b": {"verdict": "removed", "quote": None}})
        assert repr(res) == before

    def test_the_newer_side_does_not_depend_on_how_many_verdicts_the_pair_holds(self):
        """A left-behind candidate stays uncertain and a merge host is carried by the model, with or without other verdicts."""
        assembly = ("Outsourced assembly and test. We rely on outsourced assembly and test providers located in Southeast Asia. "
                    "A regional shutdown could interrupt our product deliveries.")
        older_rows = [OLDER_ROWS[0], {"item_id": "b", "headline": "Outsourced assembly and test.", "text": assembly, "text_hash": "hb2",
                                      "unit_kind": "headline"}]
        newer_ids = [newer("n0", "new"), newer("n1", "uncertain", "b", decided_by="uncertain"),
                     newer("n2", "uncertain", "a", decided_by="uncertain"), newer("n3", "new")]
        wafer_quote = "Any disruption at that foundry would delay shipments to our largest customers"
        assembly_quote = "We rely on outsourced assembly and test providers located in Southeast Asia."
        both = result([older("a", "uncertain", "n2", evidence=PARTIAL, decided_by="uncertain"),
                       older("b", "uncertain", "n1", evidence=PARTIAL, decided_by="uncertain")], newer_ids)
        # "a" is re-paired to a DIFFERENT newer item (its quote lies in n0): merged into n0; n2, its old candidate, is left behind
        alone = adj.settle(both, older_rows, NEWER_ROWS, NEWER, {"a": {"verdict": "reworded", "quote": wafer_quote}})
        with_other = adj.settle(both, older_rows, NEWER_ROWS, NEWER, {"a": {"verdict": "reworded", "quote": wafer_quote},
                                                                      "b": {"verdict": "reworded", "quote": assembly_quote}})
        for out in (alone, with_other):
            n = by_id(out.result.newer)
            assert (n["n0"].label, n["n0"].matched_older_id, n["n0"].decided_by) == ("carried", "a", "llm")      # the host, always 'llm'
            assert n["n2"] == newer("n2", "uncertain", "a", decided_by="uncertain")       # left behind: exactly as the aligner left it
            assert n["n3"].label == "new"
            a = by_id(out.result.older)["a"]
            assert (a.label, a.matched_newer_id, a.decided_by) == ("merged", "n0", "llm")
        assert by_id(alone.result.newer)["n1"].label == "uncertain"                   # b had no verdict: still pending
        n1 = by_id(with_other.result.newer)["n1"]
        assert (n1.label, n1.matched_older_id) == ("carried", "b") and by_id(with_other.result.older)["b"].label == "reworded"

    def test_it_works_on_the_result_of_the_real_aligner(self):
        rows_old = [{"item_id": "a", "headline": "Wafer supply concentration.", "text": A_TEXT, "text_hash": "ha", "unit_kind": "headline"},
                    {"item_id": "b", "headline": "Pandemic disruption.", "text": B_TEXT, "text_hash": "hb", "unit_kind": "headline"}]
        aligned = align(rows_old, NEWER_ROWS and [{**r, "headline": "", "unit_kind": "paragraph", "text_hash": f"t{i}"}
                                                  for i, r in enumerate(NEWER_ROWS)], NEWER)
        removed = [d.item_id for d in aligned.older if d.label == "removed"]
        assert "b" in removed
        out = adj.settle(aligned, rows_old, NEWER_ROWS, NEWER, {"b": {"verdict": "removed", "quote": None}})
        assert by_id(out.result.older)["b"].label == "removed"


# --------------------------------------------------------------------------- tasks, estimate, checkpoint, budget

class FakeLLM:
    def __init__(self, answers=None, fail_on=None):
        self.calls, self.answers, self.fail_on = [], answers or {}, fail_on

    def __call__(self, prompt, model_cls, *, model, max_tokens, thinking_off):
        self.calls.append({"prompt": prompt, "model": model, "max_tokens": max_tokens, "thinking_off": thinking_off})
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise RuntimeError("transient failure after retries")
        return model_cls(verdict="removed")


def tasks_for(n=3, model="openai/gpt-6-luna"):
    res = result([older(f"o{i}", "removed", evidence=ABSENT) for i in range(n)], [newer("n0", "new")])
    rows_ = [{"item_id": f"o{i}", "headline": f"H{i}.", "text": f"H{i}. body {i} " + B_TEXT, "text_hash": f"th{i}"} for i in range(n)]
    return adj.plan_tasks("PAIR", res, rows_, NEWER, model)


class TestTasksAndBudget:
    def test_one_task_per_uncertain_or_removed_item_keyed_by_item_hash_section_hash_prompt_version_and_model(self):
        tasks = tasks_for(2)
        assert [t.item_id for t in tasks] == ["o0", "o1"] and len({t.key for t in tasks}) == 2
        assert tasks[0].key == adj.task_key("th0", adj.section_hash(NEWER), "openai/gpt-6-luna")

    def test_items_the_aligner_settled_are_not_sent(self):
        res = result([older("a", "unchanged", "n0"), older("b", "reworded", "n1"), older("c", "merged", "n2")], [newer("n0", "carried", "a")])
        assert adj.plan_tasks("P", res, OLDER_ROWS, NEWER, "m") == []

    def test_the_estimate_counts_uncached_calls_and_prices_the_worst_case(self):
        tasks = tasks_for(3)
        est = adj.estimate_cost(tasks, {tasks[0].key}, "openai/gpt-6-luna")
        assert (est.n_items, est.n_cached, est.n_calls, est.priced) == (3, 1, 2, True)
        assert 0 < est.likely_usd < est.worst_case_usd < 0.05
        assert est.output_tokens_cap == 2 * adj.AdjudicationParams().max_output_tokens

    def test_an_unpriced_model_is_estimated_with_the_configured_list_prices_and_flagged(self):
        assert adj.estimate_cost(tasks_for(1, "vendor/unknown"), set(), "vendor/unknown").priced is False

    def test_the_run_refuses_before_any_call_when_the_worst_case_exceeds_the_cap(self, tmp_path):
        llm, cp = FakeLLM(), adj.Checkpoint(tmp_path / "a.jsonl")
        with pytest.raises(adj.BudgetExceeded, match="exceeds --max-usd"):
            adj.run_tasks(tasks_for(3), cp, llm, model="openai/gpt-6-luna", max_usd=0.0001)
        assert llm.calls == [] and not (tmp_path / "a.jsonl").exists()

    def test_every_answer_is_checkpointed_and_a_rerun_never_repays(self, tmp_path):
        tasks, path = tasks_for(3), tmp_path / "adjudications.jsonl"
        llm = FakeLLM()
        assert adj.run_tasks(tasks, adj.Checkpoint(path), llm, model="openai/gpt-6-luna", max_usd=0.5) == 3
        assert all(c["thinking_off"] and c["model"] == "openai/gpt-6-luna" and c["max_tokens"] == 1000 for c in llm.calls)
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [r["item_id"] for r in lines] == ["o0", "o1", "o2"] and all(r["verdict"] == "removed" for r in lines)
        again = FakeLLM()
        assert adj.run_tasks(tasks, adj.Checkpoint(path), again, model="openai/gpt-6-luna", max_usd=0.5) == 0 and again.calls == []

    def test_a_failing_call_aborts_and_keeps_what_was_already_bought(self, tmp_path):
        tasks, path = tasks_for(3), tmp_path / "a.jsonl"
        llm = FakeLLM(fail_on=2)
        with pytest.raises(RuntimeError, match="transient failure"):
            adj.run_tasks(tasks, adj.Checkpoint(path), llm, model="openai/gpt-6-luna", max_usd=0.5)
        assert [json.loads(line)["item_id"] for line in path.read_text(encoding="utf-8").splitlines()] == ["o0"]
        resume = FakeLLM()
        assert adj.run_tasks(tasks, adj.Checkpoint(path), resume, model="openai/gpt-6-luna", max_usd=0.5) == 2

    def test_a_torn_last_line_is_ignored_when_the_checkpoint_is_read(self, tmp_path):
        path = tmp_path / "a.jsonl"
        path.write_text(json.dumps({"key": "k1", "verdict": "removed"}) + '\n{"key": "k2", "verd', encoding="utf-8")
        assert set(adj.Checkpoint(path).records) == {"k1"}

    def test_a_different_model_repays_because_the_model_is_part_of_the_key(self):
        assert {t.key for t in tasks_for(1, "m1")}.isdisjoint({t.key for t in tasks_for(1, "m2")})


# --------------------------------------------------------------------------- the default call

class TestDefaultCall:
    def test_an_anthropic_shaped_model_goes_through_llm_json(self, monkeypatch):
        seen = {}

        def fake_llm_json(prompt, cls, *, model, max_tokens, thinking_off):
            seen.update(model=model, max_tokens=max_tokens, thinking_off=thinking_off)
            return cls(verdict="removed")

        monkeypatch.setattr("semigraph.llm.llm_json", fake_llm_json)
        out = adj.default_llm_json("p", adj.AdjudicationVerdict, model="anthropic/claude-sonnet-5", max_tokens=300)
        assert out.verdict == "removed" and seen == {"model": "anthropic/claude-sonnet-5", "max_tokens": 300, "thinking_off": True}

    def test_a_gpt6_model_uses_the_provider_aware_text_call_and_validates_the_json(self, monkeypatch):
        replies = iter(['Sure! {"verdict": "maybe"}', '```json\n{"verdict": "same", "quote": "abc"}\n```'])
        calls = []

        def fake_llm_text(prompt, *, model, max_tokens, reasoning_effort=None):
            calls.append((prompt, model, max_tokens, reasoning_effort))
            return next(replies)

        monkeypatch.setattr("semigraph.retrieval.answerer.llm_text", fake_llm_text)
        out = adj.default_llm_json("p", adj.AdjudicationVerdict, model="openai/gpt-6-luna", max_tokens=300)
        assert (out.verdict, out.quote) == ("same", "abc")
        assert len(calls) == 2 and "not valid JSON" in calls[1][0] and calls[0][1:] == ("openai/gpt-6-luna", 300, "none")   # no hidden reasoning

    def test_an_invalid_reply_three_times_raises_instead_of_guessing(self, monkeypatch):
        monkeypatch.setattr("semigraph.retrieval.answerer.llm_text", lambda prompt, *, model, max_tokens, reasoning_effort=None: "no json here")
        with pytest.raises(RuntimeError, match="not valid JSON after 3 attempts"):
            adj.default_llm_json("p", adj.AdjudicationVerdict, model="openai/gpt-6-luna", max_tokens=300)
