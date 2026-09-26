"""The BELOW zone in the align-items run (M1b): sentences with no counterpart at all (confidently removed / added) that a cached or bought
verdict may still settle, on the synthetic lake. Fake model, fake embedder, nothing paid, no network."""

import json

import pytest
from embedfix import make_embed
from test_items_pipeline import P1, ZZZ, file_bytes, p1, passages_of
from test_passage_bands import BELOW, TARIFF, TARIFF_QUOTE

import lakefix
from semigraph.graph import items
from semigraph.graph import passage_adjudicate as pad
from semigraph.graph.passages import PassageParams

BELOW_ON = PassageParams(adjudicate_below_band=True)
RELAXED = pad.PassageAdjudicationParams(min_relatedness=0.0)          # the 62 floor is tested in test_passage_adjudicate


@pytest.fixture
def below_lake(tmp_path, monkeypatch):
    """The synthetic lake whose wafer item holds a sentence in F24 and its low-overlap paraphrase in F25 (a confident removal)."""
    monkeypatch.setitem(lakefix.FILINGS, "24", [f"{lakefix.WAFER24} {BELOW[0]}", lakefix.TAX, lakefix.PANDEMIC])
    monkeypatch.setitem(lakefix.FILINGS, "25", [f"{lakefix.WAFER25} {BELOW[1]}", lakefix.TAX, lakefix.COMMERCIAL])
    return lakefix.build_lake(tmp_path)


class BelowLLM:
    """Says ``same`` (naming the candidate that is the paraphrase) for BELOW[0], ``different`` for everything else."""

    def __init__(self, same=True):
        self.same, self.calls = same, []

    def __call__(self, prompt, model_cls, *, model, max_tokens, thinking_off):
        self.calls.append(prompt)
        if self.same and f"<<<\n{BELOW[0]}\n>>>" in prompt:
            line = next(ln for ln in prompt.splitlines() if ln.startswith("[") and BELOW[1] in ln)
            return model_cls(verdict="same", candidate=int(line[1:line.index("]")]), quote=BELOW[1][:60])
        return model_cls(verdict="different")


def kinds_of(settings):
    """``(removed, added, reworded)`` passages of pair P1 about the BELOW sentences."""
    frame = p1(passages_of(settings))

    def pick(kind, needle):
        return frame[(frame["kind"] == kind) & frame["text"].str.contains(needle[:40], regex=False)]

    return pick("removed", BELOW[0]), pick("added", BELOW[1]), pick("reworded", BELOW[0])


def buy(settings, llm, **kwargs):
    return items.run_align_items(settings, [ZZZ], adjudicate_passages=True, passage_params=BELOW_ON, llm=llm, embed=make_embed(),
                                 pas_params=RELAXED, **kwargs)


def test_a_dry_run_counts_and_prices_each_zone_separately_and_embeds_nothing(below_lake):
    embed = make_embed()
    run = items.run_align_items(below_lake, [ZZZ], adjudicate_passages=True, dry_run=True, passage_params=BELOW_ON, embed=embed)
    assert embed.calls == [] and not items.alignment_dir(below_lake).exists()
    zones = run.passage_estimates
    assert list(zones) == ["band", "below"] and zones["below"].n_calls >= 2
    assert zones["below"].n_calls == zones["below"].n_items and zones["below"].worst_case_usd > 0
    assert run.passage_estimate.n_calls == sum(z.n_calls for z in zones.values())
    assert run.passage_estimate.worst_case_usd == pytest.approx(sum(z.worst_case_usd for z in zones.values()), abs=2e-6)
    row = next(r for r in run.summary if r["pair_id"] == P1)
    assert row["below"] >= 2 and row["below_answered"] == 0 and row["band_answered"] == 0
    table = items.format_summary(run.summary)
    assert "below" in table.splitlines()[0] and "bl-ans" in table.splitlines()[0] and "below-band sentences" in table


def test_without_the_below_flag_only_the_band_zone_is_planned_and_the_below_columns_are_dashes(below_lake):
    run = items.run_align_items(below_lake, [ZZZ], adjudicate_passages=True, dry_run=True)
    assert list(run.passage_estimates) == ["band"]
    assert all(r.get("below") is None for r in run.summary) and "below-band sentences" not in items.format_summary(run.summary)


def test_a_paraphrase_that_passes_the_gold_relatedness_floor_is_accepted_and_one_that_does_not_stays_removed(tmp_path, monkeypatch):
    """No relaxed rules here: the real floor (partial_ratio >= 62 of the quote vs the sentence) decides which below sentences a model
    may turn into rewordings. The TARIFF paraphrase has a quotable stretch over the floor; the BELOW paraphrase has none."""
    monkeypatch.setitem(lakefix.FILINGS, "24", [f"{lakefix.WAFER24} {TARIFF[0]} {BELOW[0]}", lakefix.TAX, lakefix.PANDEMIC])
    monkeypatch.setitem(lakefix.FILINGS, "25", [f"{lakefix.WAFER25} {TARIFF[1]} {BELOW[1]}", lakefix.TAX, lakefix.COMMERCIAL])
    settings = lakefix.build_lake(tmp_path)

    class Both:
        def __call__(self, prompt, model_cls, *, model, max_tokens, thinking_off):
            for old, new, quote in ((TARIFF[0], TARIFF[1], TARIFF_QUOTE), (BELOW[0], BELOW[1], BELOW[1][:60])):
                if f"<<<\n{old}\n>>>" in prompt:
                    line = next(ln for ln in prompt.splitlines() if ln.startswith("[") and new in ln)
                    return model_cls(verdict="same", candidate=int(line[1:line.index("]")]), quote=quote)
            return model_cls(verdict="different")

    items.run_align_items(settings, [ZZZ], adjudicate_passages=True, passage_params=BELOW_ON, llm=Both(), embed=make_embed())
    frame = p1(passages_of(settings))
    reworded = frame[frame["kind"] == "reworded"]
    removed = frame[frame["kind"] == "removed"]
    assert reworded["text"].str.contains(TARIFF[0][:40], regex=False).any() and reworded["decided_by"].eq("sentence_reworded_llm").any()
    assert removed["text"].str.contains(BELOW[0][:40], regex=False).any()                      # the model said same, the code refused
    assert not removed["text"].str.contains(TARIFF[0][:40], regex=False).any()


def test_the_dry_run_estimate_is_an_upper_bound_of_the_real_purchase(below_lake):
    dry = items.run_align_items(below_lake, [ZZZ], adjudicate_passages=True, dry_run=True, passage_params=BELOW_ON)
    llm = BelowLLM(same=False)
    run = buy(below_lake, llm)
    assert run.passage_estimate.n_calls == dry.passage_estimate.n_calls == run.passage_calls == len(llm.calls)
    assert run.passage_estimate.worst_case_usd <= dry.passage_estimate.worst_case_usd
    assert 0 < dry.passage_estimate.likely_usd < dry.passage_estimate.worst_case_usd


def test_a_verified_same_makes_the_wrong_removal_reworded_and_stops_the_addition(below_lake):
    run = buy(below_lake, BelowLLM(same=True))
    removed, added, reworded = kinds_of(below_lake)
    assert removed.empty and added.empty and len(reworded) == 1
    row = reworded.iloc[0]
    assert row["counterpart_text"] == BELOW[1] and row["decided_by"] == "sentence_reworded_llm" and row["band_adjudicated"]
    assert run.passage_verdicts_used >= 1
    records = [json.loads(line) for line in (items.alignment_dir(below_lake) / pad.CHECKPOINT_NAME).read_text(encoding="utf-8").splitlines()]
    assert {r["zone"] for r in records} >= {"below"} and {r["prompt_version"] for r in records} == {pad.PROMPT_VERSION}
    zones = [r["zone"] for r in records]
    assert zones == sorted(zones, key=pad.ZONES.index)                            # the band zone is bought before the below zone


def test_a_different_verdict_keeps_the_removal_and_the_addition_and_says_a_model_settled_them(below_lake):
    buy(below_lake, BelowLLM(same=False))
    removed, added, reworded = kinds_of(below_lake)
    assert len(removed) == 1 and len(added) == 1 and reworded.empty
    assert removed.iloc[0]["decided_by"] == "sentence_absent_llm" and removed.iloc[0]["band_adjudicated"]
    assert added.iloc[0]["decided_by"] == "sentence_absent_llm"


def test_without_the_below_flag_the_same_answers_change_nothing_and_no_below_sentence_is_asked(below_lake):
    llm = BelowLLM(same=True)
    run = items.run_align_items(below_lake, [ZZZ], adjudicate_passages=True, llm=llm, embed=make_embed(), pas_params=RELAXED)
    removed, added, reworded = kinds_of(below_lake)
    assert len(removed) == 1 and len(added) == 1 and reworded.empty and run.passage_calls == len(llm.calls)
    assert all(f"<<<\n{BELOW[0]}\n>>>" not in p for p in llm.calls)


def test_the_below_flag_off_run_is_byte_identical_to_a_run_that_never_heard_of_the_flag(below_lake):
    items.run_align_items(below_lake, [ZZZ])
    plain = file_bytes(below_lake)
    for path in items.alignment_dir(below_lake).glob("*.parquet"):
        path.unlink()
    items.run_align_items(below_lake, [ZZZ], passage_params=PassageParams(adjudicate_below_band=False))
    assert file_bytes(below_lake) == plain


def test_a_below_purchase_is_applied_by_a_replay_even_without_the_below_flag(below_lake):
    """C1 (was: `test_a_below_purchase_is_not_applied_by_a_replay_without_the_flag`): a recorded below answer is replayed by every run."""
    buy(below_lake, BelowLLM(same=True))
    llm = BelowLLM()
    run = items.run_align_items(below_lake, [ZZZ], adjudicate_passages=True, max_usd=0, llm=llm, embed=make_embed(), pas_params=RELAXED)
    removed, added, reworded = kinds_of(below_lake)
    assert llm.calls == [] and removed.empty and added.empty and len(reworded) == 1 and run.passage_calls == 0
    assert run.below_replayed and reworded.iloc[0]["decided_by"] == "sentence_reworded_llm"


def test_a_zero_budget_replay_of_a_below_purchase_with_the_flag_reproduces_the_tables(below_lake):
    buy(below_lake, BelowLLM(same=True))
    bought = file_bytes(below_lake)
    for path in items.alignment_dir(below_lake).glob("*.parquet"):
        path.unlink()
    llm, embed = BelowLLM(), make_embed()
    replay = items.run_align_items(below_lake, [ZZZ], adjudicate_passages=True, passage_params=BELOW_ON, max_usd=0, llm=llm, embed=embed,
                                   pas_params=RELAXED)
    assert llm.calls == [] and embed.calls == [] and replay.passage_calls == 0 and file_bytes(below_lake) == bought


def test_plan_band_tasks_lists_the_zones_and_only_builds_candidates_for_the_unanswered(below_lake):
    quality = items.load_quality(items.items_dir(below_lake))
    frame, sections, chunks = items.read_ticker_inputs(below_lake, ZZZ)
    pair = next(p for p in items.consecutive_pairs(frame, quality) if p["pair_id"] == P1)
    work = items.prepare_pair(pair, frame, sections, chunks)
    embed = make_embed()
    plan = items.plan_band_tasks(work, None, "m", {}, passage_params=BELOW_ON, embed=embed, cache_dir=None)
    assert plan.counts["below"][0] >= 2 and plan.counts["below"][1] == 0 and {t.zone for t in plan.tasks} >= {"below"}
    assert all(1 <= len(t.candidates) <= 8 for t in plan.tasks) and embed.calls
    answered = {t.key: {} for t in plan.tasks}
    embed2 = make_embed()
    again = items.plan_band_tasks(work, None, "m", answered, passage_params=BELOW_ON, embed=embed2, cache_dir=None)
    assert embed2.calls == [] and again.counts["below"] == (plan.counts["below"][0], plan.counts["below"][0])
    assert all(t.candidates == () for t in again.tasks)                              # answered sentences need no candidates
    with pytest.raises(pad.BudgetExceeded):
        items.plan_band_tasks(work, None, "m", {}, passage_params=BELOW_ON, embed=make_embed(), refuse_uncached=True)
