"""C1 of the M1b review: EVERY ``align-items`` run replays every recorded model answer for free, whatever flags it has.

The flags only permit BUYING answers that are missing. Fake model, fake embedder, nothing paid, no network; the tables go to the
synthetic lake under ``tmp_path`` (never the real ``data/interim/risk_alignment``).
"""

import json

import pandas as pd
import pytest
from embedfix import make_embed
from test_items_pipeline import BAND_HK, BAND_PARA, P1, ZZZ, BandLLM, p1, passages_of
from test_passage_bands import BELOW

import lakefix
from semigraph.graph import adjudicate as adj
from semigraph.graph import items
from semigraph.graph import passage_adjudicate as pad
from semigraph.graph.passages import PairPassages, PassageParams

BELOW_ON = PassageParams(adjudicate_below_band=True)
RELAXED = pad.PassageAdjudicationParams(min_relatedness=0.0)          # the 62 floor is tested in test_passage_adjudicate
TABLES = ("pairs", "decisions", "passages")


@pytest.fixture
def full_lake(tmp_path, monkeypatch):
    """A lake with an item the aligner calls removed, two band sentences and one below-zone paraphrase in pair P1."""
    monkeypatch.setitem(lakefix.FILINGS, "24", [f"{lakefix.WAFER24} {BAND_HK[0]} {BAND_PARA[0]} {BELOW[0]}", lakefix.TAX, lakefix.PANDEMIC])
    monkeypatch.setitem(lakefix.FILINGS, "25", [f"{lakefix.WAFER25} {BAND_HK[1]} {BAND_PARA[1]} {BELOW[1]}", lakefix.TAX, lakefix.COMMERCIAL])
    return lakefix.build_lake(tmp_path)


class FullLLM(BandLLM):
    """Items: ``reworded`` without a quote (the removed pandemic item is rejected -> uncertain = present). Passages: the paraphrase and
    the below-zone paraphrase are ``same``, everything else ``different``."""

    def __call__(self, prompt, model_cls, *, model, max_tokens, thinking_off):
        if model_cls is not pad.PassageVerdict:
            self.item_calls += 1
            return model_cls(verdict="reworded", quote=None)
        if f"<<<\n{BELOW[0]}\n>>>" in prompt:
            self.passage_calls += 1
            line = next(ln for ln in prompt.splitlines() if ln.startswith("[") and BELOW[1] in ln)
            return model_cls(verdict="same", candidate=int(line[1:line.index("]")]), quote=BELOW[1][:60])
        return super().__call__(prompt, model_cls, model=model, max_tokens=max_tokens, thinking_off=thinking_off)


class Forbidden:
    """A model / embedder that fails the test when it is used."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("a run that may not buy called the model or the embedder")


def tables(settings, directory=None):
    directory = directory or items.alignment_dir(settings)
    return {name: items.table_path(directory, ZZZ, name).read_bytes() for name in TABLES}


def buy_everything(settings, **kwargs):
    llm = FullLLM()
    run = items.run_align_items(settings, [ZZZ], adjudicate=True, adjudicate_passages=True, passage_params=BELOW_ON, llm=llm,
                                embed=make_embed(), pas_params=RELAXED, max_usd=5.0, **kwargs)
    return llm, run


def wipe_tables(settings):
    for path in items.alignment_dir(settings).glob("*.parquet"):
        path.unlink()


def label_of(settings, item_suffix, directory=None):
    dec = pd.read_parquet(items.table_path(directory or items.alignment_dir(settings), ZZZ, "decisions"))
    row = dec[(dec["item_id"] == f"{lakefix.ACC['24']}:I.1A:{item_suffix}") & (dec["side"] == "older")].iloc[0]
    return row["label"], bool(row["adjudicated"])


# --------------------------------------------------------------------------- the headline: a plain run loses nothing that was bought

def test_a_plain_run_replays_item_band_and_below_answers_and_reproduces_the_bought_tables_byte_for_byte(full_lake):
    llm, _ = buy_everything(full_lake)
    assert llm.item_calls == 1 and llm.passage_calls >= 5                      # item + band + below answers were all bought
    bought = tables(full_lake)
    assert label_of(full_lake, "i002") == ("uncertain", True)                  # the model kept the pandemic item PRESENT
    wipe_tables(full_lake)
    llm2, embed = Forbidden(), Forbidden()
    plain = items.run_align_items(full_lake, [ZZZ], max_usd=100.0, llm=llm2, embed=embed, pas_params=RELAXED)
    assert llm2.calls == 0 and embed.calls == 0 and (plain.item_calls, plain.passage_calls) == (0, 0)
    assert tables(full_lake) == bought
    assert plain.item_verdicts_used == 1 and plain.passage_verdicts_used >= 5


def test_without_the_recorded_answers_the_same_plain_run_gives_the_aligner_only_tables(full_lake, tmp_path):
    """The contrast that makes the headline test mean something: the aligner alone calls the pandemic item removed."""
    buy_everything(full_lake)
    empty = tmp_path / "no_answers"
    items.run_align_items(full_lake, [ZZZ], out_dir=empty, pas_params=RELAXED)
    assert label_of(full_lake, "i002", empty) == ("removed", False)
    assert tables(full_lake, empty) != tables(full_lake)


def test_the_bought_below_zone_verdict_is_applied_by_a_plain_run_with_no_flag(full_lake):
    buy_everything(full_lake)
    wipe_tables(full_lake)
    items.run_align_items(full_lake, [ZZZ], pas_params=RELAXED)
    frame = p1(passages_of(full_lake))
    below = frame[frame["text"].str.contains(BELOW[0][:40], regex=False)]
    assert (below["kind"] == "reworded").all() and below["decided_by"].eq("sentence_reworded_llm").all() and len(below) == 1
    assert not frame[(frame["kind"] == "removed") & frame["text"].str.contains(BELOW[0][:40], regex=False)].shape[0]


def test_a_run_with_only_the_item_flag_still_replays_the_passage_answers_it_may_not_buy(full_lake):
    items.run_align_items(full_lake, [ZZZ], adjudicate_passages=True, passage_params=BELOW_ON, llm=FullLLM(), embed=make_embed(),
                          pas_params=RELAXED, max_usd=5.0)
    only_passages = tables(full_lake)
    assert label_of(full_lake, "i002") == ("removed", False)                    # no item answer yet
    llm = FullLLM()
    run = items.run_align_items(full_lake, [ZZZ], adjudicate=True, llm=llm, pas_params=RELAXED, max_usd=5.0)
    assert llm.item_calls == 1 and llm.passage_calls == 0 and run.passage_calls == 0        # bought the item answer only
    assert label_of(full_lake, "i002") == ("uncertain", True)                   # the item answer changed the pandemic label
    frame = p1(passages_of(full_lake))
    below = frame[frame["text"].str.contains(BELOW[0][:40], regex=False)]
    assert below["decided_by"].eq("sentence_reworded_llm").all() and len(below) == 1        # the passage answers were replayed, not lost
    assert tables(full_lake)["decisions"] != only_passages["decisions"]


def test_a_dry_run_without_flags_counts_the_replayed_item_answers(full_lake):
    buy_everything(full_lake)
    dry = items.run_align_items(full_lake, [ZZZ], dry_run=True, pas_params=RELAXED)
    row = next(r for r in dry.summary if r["pair_id"] == P1)
    assert row["older_removed"] == 0 and row["older_uncertain"] == 1 and row["adjudicated"] == 1
    assert dry.written == [] and dry.item_verdicts_used == 1


def test_answers_of_another_model_are_not_replayed(full_lake):
    buy_everything(full_lake)
    other = items.run_align_items(full_lake, [ZZZ], model="vendor/other-model", pas_params=RELAXED, dry_run=True)
    row = next(r for r in other.summary if r["pair_id"] == P1)
    assert row["older_removed"] == 1 and other.item_verdicts_used == 0 and other.passage_prompt_version == ""


# --------------------------------------------------------------------------- no flag, no purchase

def test_no_flag_means_no_purchase_whatever_max_usd_says_and_the_gaps_stay_aligner_only(full_lake):
    llm, embed = Forbidden(), Forbidden()
    run = items.run_align_items(full_lake, [ZZZ], max_usd=1000.0, llm=llm, embed=embed)
    assert llm.calls == 0 and embed.calls == 0 and run.estimate is None and run.passage_estimate is None
    assert label_of(full_lake, "i002") == ("removed", False)                    # nothing recorded, nothing bought: aligner-only
    assert not (items.alignment_dir(full_lake) / adj.CHECKPOINT_NAME).exists()
    assert not (items.alignment_dir(full_lake) / pad.CHECKPOINT_NAME).exists()


def test_the_below_flag_alone_never_buys_and_the_passage_flag_never_buys_item_answers(full_lake):
    llm = Forbidden()
    items.run_align_items(full_lake, [ZZZ], passage_params=BELOW_ON, llm=llm, max_usd=1000.0)          # below on, but no adjudicate_passages
    assert llm.calls == 0
    bought = FullLLM()
    items.run_align_items(full_lake, [ZZZ], adjudicate_passages=True, llm=bought, embed=make_embed(), pas_params=RELAXED, max_usd=5.0)
    assert bought.item_calls == 0 and bought.passage_calls > 0


# --------------------------------------------------------------------------- which passage version is replayed

def relabel(row, version, verdict=None):
    row = {**row, "prompt_version": version, "key": row["key"].replace(f"|{pad.PROMPT_VERSION}|", f"|{version}|")}
    if verdict:
        row = {**row, "verdict": verdict, "candidate": None, "quote": None}
    return row


def write(path, rows):
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def test_the_newest_prompt_version_on_disk_is_replayed_and_the_older_one_only_when_it_is_all_there_is(tmp_path, monkeypatch):
    monkeypatch.setitem(lakefix.FILINGS, "24", [f"{lakefix.WAFER24} {BAND_HK[0]} {BAND_PARA[0]}", lakefix.TAX, lakefix.PANDEMIC])
    monkeypatch.setitem(lakefix.FILINGS, "25", [f"{lakefix.WAFER25} {BAND_HK[1]} {BAND_PARA[1]}", lakefix.TAX, lakefix.COMMERCIAL])
    lake = lakefix.build_lake(tmp_path)
    items.run_align_items(lake, [ZZZ], adjudicate_passages=True, llm=BandLLM(), embed=make_embed(), max_usd=5.0)
    directory = items.alignment_dir(lake)
    path = directory / pad.CHECKPOINT_NAME
    v3 = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    v2_all_different = [relabel(r, pad.LEGACY_PROMPT_VERSION, "different") for r in v3]

    def replayed():
        wipe_tables(lake)
        run = items.run_align_items(lake, [ZZZ], llm=Forbidden())
        frame = p1(passages_of(lake))
        para = frame[frame["text"].str.contains("Long lead times")]
        return run, para

    write(path, v2_all_different + v3)                                          # both versions on disk: pas-v3 wins
    run, para = replayed()
    assert run.passage_prompt_version == pad.PROMPT_VERSION
    assert (para["kind"] == "reworded").all() and para["decided_by"].eq("sentence_reworded_llm").all()
    write(path, v2_all_different)                                               # only the legacy answers left: they are replayed
    run, para = replayed()
    assert run.passage_prompt_version == pad.LEGACY_PROMPT_VERSION
    assert (para["kind"] == "removed").all() and para["decided_by"].eq("sentence_absent_llm").all()


def test_replay_version_picks_the_newest_known_version_recorded_for_the_model_and_ignores_the_rest():
    def key(version, model="m"):
        return f"h1|h2|{version}|{model}"

    records = {key("pas-v2"): {}, key("pas-v3", "other"): {}, "not-a-key": {}, key("pas-v9"): {}}
    assert pad.replay_version(records, "m") == "pas-v2"
    assert pad.replay_version({**records, key("pas-v3"): {}}, "m") == "pas-v3"
    assert pad.replay_version({}, "m") is None and pad.replay_version(records, "unknown") is None
    assert pad.PROMPT_VERSIONS[-1] == pad.PROMPT_VERSION and pad.LEGACY_PROMPT_VERSION in pad.PROMPT_VERSIONS


def test_the_below_zone_is_replayed_only_when_the_replayed_version_holds_a_below_answer_for_the_model():
    def key(version, model="m"):
        return f"h1|h2|{version}|{model}"

    band = {key("pas-v3"): {"zone": "band"}}
    below = {**band, key("pas-v3", "other"): {"zone": "below"}}
    assert not pad.replay_has_below(band, "m", "pas-v3") and not pad.replay_has_below(below, "m", "pas-v3")
    assert pad.replay_has_below({**band, "h3|h4|pas-v3|m": {"zone": "below"}}, "m", "pas-v3")
    assert not pad.replay_has_below({"h3|h4|pas-v3|m": {"zone": "below"}}, "m", "pas-v2")


def test_turning_the_below_zone_on_without_answers_changes_no_passage(full_lake):
    """The safety of the auto-enabled replay: an unanswered below sentence is classified exactly as with the flag off."""
    quality = items.load_quality(items.items_dir(full_lake))
    frame, sections, chunks = items.read_ticker_inputs(full_lake, ZZZ)
    for pair in items.consecutive_pairs(frame, quality):
        work = items.prepare_pair(pair, frame, sections, chunks)
        ctx = items.BandContext({}, "m", pad.PassageAdjudicationParams(), pad.PROMPT_VERSION)
        for band in (None, ctx):
            off = items.finalize_pair(work, passage_params=PassageParams(), band=band)
            on = items.finalize_pair(work, passage_params=BELOW_ON, band=band)
            assert on.passages == off.passages and on.result == off.result


# --------------------------------------------------------------------------- one bad call does not abort the run (M2, at the run level)

class Flaky(FullLLM):
    """The first passage call raises a provider error; every other call answers."""

    def __init__(self):
        super().__init__()
        self.raised = False

    def __call__(self, prompt, model_cls, **kw):
        if model_cls is pad.PassageVerdict and not self.raised:
            self.raised = True
            raise __import__("litellm").exceptions.APIConnectionError(message="boom", llm_provider="openai", model="m")
        return super().__call__(prompt, model_cls, **kw)


def test_a_provider_error_during_a_purchase_leaves_a_finished_run_and_the_call_counts_are_calls_made(full_lake):
    llm = Flaky()
    run = items.run_align_items(full_lake, [ZZZ], adjudicate_passages=True, passage_params=BELOW_ON, llm=llm, embed=make_embed(),
                                pas_params=RELAXED, max_usd=5.0)
    recorded = adj.Checkpoint(items.alignment_dir(full_lake) / pad.CHECKPOINT_NAME).records
    assert run.passage_failed == 1 and run.passage_calls == len(recorded) + 1        # calls MADE, the failed one included
    assert len(run.written) >= 3 and all(p.exists() for p in run.written)
    again = FullLLM()
    fixed = items.run_align_items(full_lake, [ZZZ], adjudicate_passages=True, passage_params=BELOW_ON, llm=again, embed=make_embed(),
                                  pas_params=RELAXED, max_usd=5.0)
    assert fixed.passage_calls == 1 and again.passage_calls == 1                     # only the failed sentence is asked again


def test_the_shared_budget_charges_the_item_step_for_its_failed_calls_too(full_lake):
    class ItemFails(FullLLM):
        def __call__(self, prompt, model_cls, **kw):
            if model_cls is not pad.PassageVerdict:
                self.item_calls += 1
                raise RuntimeError("blip")
            return super().__call__(prompt, model_cls, **kw)

    run = items.run_align_items(full_lake, [ZZZ], adjudicate=True, adjudicate_passages=True, llm=ItemFails(), embed=make_embed(),
                                pas_params=RELAXED, max_usd=5.0)
    assert run.item_calls == 1 and run.item_failed == 1 and run.item_charged_usd > 0
    assert not (items.alignment_dir(full_lake) / adj.CHECKPOINT_NAME).exists()          # nothing was recorded for the failed call
    assert run.passage_budget_usd == pytest.approx(5.0 - run.item_charged_usd)


def test_the_replay_engine_used_for_the_counts_lists_no_candidates(full_lake):
    """Replay only needs keys and hashes: no candidate search (and so no embedding) happens in a plain run."""
    real = PairPassages.band_sentences
    seen = []

    def spy(self, *, candidates=True):
        seen.append(candidates)
        return real(self, candidates=candidates)

    buy_everything(full_lake)
    PairPassages.band_sentences = spy
    try:
        items.run_align_items(full_lake, [ZZZ], pas_params=RELAXED, llm=Forbidden(), embed=Forbidden())
    finally:
        PairPassages.band_sentences = real
    assert seen and not any(seen)
