"""graph/items.py: the align-items pipeline on a synthetic lake (no network, no model, no Neo4j)."""

import json

import pandas as pd
import pyarrow.parquet as pq
import pytest

import lakefix
from semigraph.graph import adjudicate as adj
from semigraph.graph import items
from semigraph.graph import passage_adjudicate as pad
from semigraph.graph.alignment import AlignParams
from semigraph.graph.passages import Passage

ZZZ = "ZZZ"
P1 = f"ZZZ-{lakefix.ACC['24']}-{lakefix.ACC['25']}"
P2 = f"ZZZ-{lakefix.ACC['25']}-{lakefix.ACC['26']}"


@pytest.fixture
def lake(tmp_path):
    return lakefix.build_lake(tmp_path)


def read(settings, name):
    return pd.read_parquet(items.table_path(items.alignment_dir(settings), ZZZ, name))


def file_bytes(settings):
    return {p.name: p.read_bytes() for p in sorted(items.alignment_dir(settings).iterdir())}


# --------------------------------------------------------------------------- small pure helpers

def test_item_rows_are_in_filing_order_with_plain_python_values():
    frame = pd.DataFrame(lakefix.build_filing("24")[0]).sample(frac=1.0, random_state=1)
    frame.loc[frame["seq"] == 1, "headline"] = None                              # a paragraph unit
    rows = items.item_rows(frame, lakefix.ACC["24"])
    assert [r["seq"] for r in rows] == [0, 1, 2] and rows[1]["headline"] == ""
    assert isinstance(rows[0]["char_start"], int) and isinstance(rows[0]["filer_cik"], int) and rows[0]["chunk_ids"] == [f"{lakefix.ACC['24']}:I.1A:0000"]


def test_chunk_spans_are_those_of_one_section_of_one_filing():
    chunks = pd.DataFrame([{"accession_no": "a", "section_id": "I.1A", "chunk_id": "a:I.1A:0000", "char_start": 0, "char_end": 10},
                           {"accession_no": "a", "section_id": "I.1", "chunk_id": "a:I.1:0000", "char_start": 0, "char_end": 99},
                           {"accession_no": "b", "section_id": "I.1A", "chunk_id": "b:I.1A:0000", "char_start": 0, "char_end": 10}])
    assert items.section_chunk_spans(chunks, "a", "I.1A") == [("a:I.1A:0000", 0, 10)]


def test_overlap_is_half_open_ignores_empty_chunks_and_keeps_text_order():
    spans = [("c2", 100, 200), ("c0", 0, 50), ("c1", 50, 100), ("c9", 60, 60)]
    assert items.overlapping_chunk_ids(spans, 40, 100) == ("c0", "c1")
    assert items.overlapping_chunk_ids(spans, 50, 51) == ("c1",)          # c0 ends AT 50: not overlapping
    assert items.overlapping_chunk_ids(spans, 200, 300) == ()


def test_the_alignment_tables_have_fixed_schemas():
    assert [f.name for f in items.DECISION_SCHEMA][:13] == [
        "item_id", "accession_no", "side", "label", "matched_item_id", "decided_by", "sim_embed", "sim_lex", "headline_ratio",
        "quote", "quote_span_start", "quote_span_end", "adjudicated"]
    assert {"older_accession", "newer_accession", "filer_cik", "counterpart_chunk_ids", "chunk_fallback"} <= {f.name for f in items.PASSAGE_SCHEMA}
    assert [f.name for f in items.PAIR_SCHEMA] == ["pair_id", "ticker", "older_accession", "newer_accession", "older_date",
                                                   "newer_date", "comparable", "not_compared_reason"]


# --------------------------------------------------------------------------- the run

def test_every_consecutive_pair_is_recorded_and_the_decisions_follow_the_text(lake):
    run = items.run_align_items(lake, [ZZZ])
    assert len(run.written) == 3 and not run.dry_run and run.estimate is None
    pairs = read(lake, "pairs")
    assert list(pairs["pair_id"]) == [P1, P2] and pairs["comparable"].all() and pairs["not_compared_reason"].isna().all()
    dec = read(lake, "decisions")
    older = dec[(dec["pair_id"] == P1) & (dec["side"] == "older")].set_index("item_id")["label"]
    assert list(older) == ["reworded", "unchanged", "removed"]
    newer = dec[(dec["pair_id"] == P1) & (dec["side"] == "newer")].set_index("item_id")["label"]
    assert list(newer) == ["carried", "carried", "new"]
    assert set(dec["adjudicated"]) == {False}


def test_a_middle_filing_item_appears_once_per_side_with_its_own_pair(lake):
    items.run_align_items(lake, [ZZZ])
    dec = read(lake, "decisions")
    middle = f"{lakefix.ACC['25']}:I.1A:i001"
    rows = dec[dec["item_id"] == middle]
    assert sorted(zip(rows["side"], rows["pair_id"])) == [("newer", P1), ("older", P2)]


def test_matched_item_ids_point_at_the_other_side(lake):
    items.run_align_items(lake, [ZZZ])
    dec = read(lake, "decisions")
    reworded = dec[(dec["label"] == "reworded") & (dec["side"] == "older")].iloc[0]
    assert reworded["matched_item_id"] == f"{lakefix.ACC['25']}:I.1A:i000"
    carried = dec[(dec["item_id"] == reworded["matched_item_id"]) & (dec["side"] == "newer")].iloc[0]
    assert carried["matched_item_id"] == reworded["item_id"] and carried["label"] == "carried"


def test_the_changed_passages_carry_their_pair_filer_and_chunk_ids(lake):
    items.run_align_items(lake, [ZZZ])
    passages = read(lake, "passages").set_index("kind")
    removed, added = passages.loc["removed"], passages.loc["added"]
    assert removed["text"].startswith("Our foundry contract expires") and removed["item_id"].startswith(lakefix.ACC["24"])
    assert (removed["older_accession"], removed["newer_accession"], removed["filer_cik"]) == (lakefix.ACC["24"], lakefix.ACC["25"], lakefix.CIK)
    assert list(removed["chunk_ids"]) == [f"{lakefix.ACC['24']}:I.1A:0000"] and not removed["chunk_fallback"]
    assert added["item_id"].startswith(lakefix.ACC["25"]) and list(added["chunk_ids"]) == [f"{lakefix.ACC['25']}:I.1A:0000"]
    assert list(removed["counterpart_chunk_ids"]) == [] and passages["pair_id"].eq(P1).all()


def test_a_second_run_writes_byte_identical_files_and_leaves_no_temp_file(lake):
    items.run_align_items(lake, [ZZZ])
    first = file_bytes(lake)
    items.run_align_items(lake, [ZZZ])
    assert file_bytes(lake) == first and not [n for n in first if n.endswith(".tmp")]
    assert set(first) == {f"ZZZ_{n}.parquet" for n in ("pairs", "decisions", "passages")}


def test_the_parquet_types_are_fixed_even_when_a_table_is_empty(lake):
    items.run_align_items(lake, [ZZZ])
    schema = pq.read_schema(items.table_path(items.alignment_dir(lake), ZZZ, "passages"))
    assert schema.equals(items.PASSAGE_SCHEMA, check_metadata=False)
    items.write_table(lake.data_dir / "empty.parquet", [], items.DECISION_SCHEMA)
    assert pq.read_table(lake.data_dir / "empty.parquet").num_rows == 0
    assert pq.read_schema(lake.data_dir / "empty.parquet").equals(items.DECISION_SCHEMA, check_metadata=False)


# --------------------------------------------------------------------------- pairs that are not compared

@pytest.fixture
def poor_lake(tmp_path):
    return lakefix.build_lake(tmp_path, quality={"25": {"low_coverage": True, "coverage": 0.71}})


def test_a_pair_with_an_untrustworthy_side_is_recorded_with_its_reason_and_produces_nothing_else(poor_lake):
    run = items.run_align_items(poor_lake, [ZZZ])
    pairs = read(poor_lake, "pairs")
    assert not pairs["comparable"].any() and pairs["not_compared_reason"].str.contains("71.0%").all()
    assert len(read(poor_lake, "decisions")) == 0 and len(read(poor_lake, "passages")) == 0
    table = items.format_summary(run.summary)
    assert table.count("NOT COMPARED") == 2 and "71.0%" in table and "0 pair(s) compared, 2 not compared" in table


def test_only_the_untrustworthy_pair_is_skipped(tmp_path):
    settings = lakefix.build_lake(tmp_path, quality={"24": {"section_suspect": True}})
    items.run_align_items(settings, [ZZZ])
    pairs = read(settings, "pairs").set_index("pair_id")["comparable"]
    assert (pairs[P1], pairs[P2]) == (False, True)
    dec = read(settings, "decisions")
    assert set(dec["pair_id"]) == {P2}


# --------------------------------------------------------------------------- passage rows: chunk ids

def work_with_chunks():
    older_rows = [{"item_id": "o0", "chunk_ids": ["o:I.1A:0000", "o:I.1A:0001"], "filer_cik": 7}]
    newer_rows = [{"item_id": "n0", "chunk_ids": ["n:I.1A:0000"], "filer_cik": 7}]
    pair = {"pair_id": "T-o-n", "ticker": "T", "older_accession": "o", "newer_accession": "n", "older_date": "d", "newer_date": "e",
            "comparable": True, "not_compared_reason": None}
    return items.PairWork(pair, older_rows, newer_rows, "", "", [("o:I.1A:0000", 0, 100)], [("n:I.1A:0000", 0, 50), ("n:I.1A:0001", 50, 100)], None)


def outcome_with(passages):
    return items.PairOutcome(work_with_chunks(), None, None, tuple(passages))


def passage(kind="removed", **kw):
    base = dict(passage_id="o0:r000", kind=kind, item_id="o0", seq=0, text="t", char_start=10, char_end=20, counterpart_text=None,
                counterpart_span=None, similarity=None, chunk_ids=("o:I.1A:0000",), decided_by="sentence_absent")
    return Passage(**{**base, **kw})


def test_a_reworded_passage_lists_the_chunks_of_the_newer_filing_that_overlap_its_counterpart():
    reworded = passage("reworded", passage_id="o0:w000", counterpart_text="cp", counterpart_span=(40, 60), similarity=0.7,
                       decided_by="sentence_reworded")
    (row,) = items.passage_rows(outcome_with([reworded]))
    assert row["counterpart_chunk_ids"] == ["n:I.1A:0000", "n:I.1A:0001"]
    assert (row["counterpart_start"], row["counterpart_end"]) == (40, 60) and row["similarity"] == 0.7


def test_a_passage_with_no_chunk_of_its_own_falls_back_to_its_items_first_chunk_and_says_so():
    (row,) = items.passage_rows(outcome_with([passage(chunk_ids=())]))
    assert row["chunk_ids"] == ["o:I.1A:0000"] and row["chunk_fallback"] is True
    (own,) = items.passage_rows(outcome_with([passage()]))
    assert own["chunk_ids"] == ["o:I.1A:0000"] and own["chunk_fallback"] is False


def test_an_added_passage_falls_back_to_its_newer_items_chunk():
    (row,) = items.passage_rows(outcome_with([passage("added", passage_id="n0:a000", item_id="n0", chunk_ids=())]))
    assert row["chunk_ids"] == ["n:I.1A:0000"] and row["chunk_fallback"] is True and row["counterpart_chunk_ids"] == []


def test_duplicate_passage_ids_are_refused_before_the_constraint_would_reject_them():
    with pytest.raises(items.AlignItemsError, match="duplicate passage_id"):
        items.check_unique_passages("T", [{"passage_id": "x"}, {"passage_id": "x"}])


# --------------------------------------------------------------------------- inputs

def test_tickers_are_discovered_from_the_item_files_and_an_unknown_one_is_refused(lake):
    assert items.discover_tickers(lake) == [ZZZ] and items.discover_tickers(lake, ["zzz"]) == [ZZZ]
    with pytest.raises(items.AlignItemsError, match="no risk-item parquet for \\['NOPE'\\]"):
        items.discover_tickers(lake, ["NOPE"])


def test_a_missing_input_file_says_what_to_run(lake):
    (lake.chunks_dir / "ZZZ_chunks.parquet").unlink()
    with pytest.raises(items.AlignItemsError, match="not found"):
        items.run_align_items(lake, [ZZZ])


def test_the_loader_precondition_names_the_command_to_run(lake):
    with pytest.raises(items.AlignItemsError, match="run `semigraph align-items` first"):
        items.require_alignment(lake, [ZZZ])
    items.run_align_items(lake, [ZZZ])
    items.require_alignment(lake, [ZZZ])                                  # now silent
    assert items.missing_alignment(lake, [ZZZ]) == []


def test_the_nvda_lake_files_keep_their_lowercase_names(tmp_path):
    settings = lakefix.build_lake(tmp_path, ticker="NVDA", cik=1045810)
    for sub, upper, lower in (("interim/section_texts", "NVDA_section_texts.parquet", "nvda_section_texts.parquet"),
                              ("processed/chunks", "NVDA_chunks.parquet", "nvda_chunks.parquet")):
        directory = tmp_path / "data" / sub
        (directory / upper).rename(directory / lower)
    run = items.run_align_items(settings, ["NVDA"])
    assert len(run.written) == 3


# --------------------------------------------------------------------------- adjudication in the run

class FakeLLM:
    def __init__(self, verdict="removed", quote=None):
        self.calls, self.verdict, self.quote = 0, verdict, quote

    def __call__(self, prompt, model_cls, *, model, max_tokens, thinking_off):
        self.calls += 1
        return model_cls(verdict=self.verdict, quote=self.quote)


def test_adjudication_is_off_by_default_so_no_checkpoint_and_no_call(lake):
    items.run_align_items(lake, [ZZZ], llm=FakeLLM())
    assert not (items.alignment_dir(lake) / adj.CHECKPOINT_NAME).exists()


def test_a_dry_run_reports_the_estimate_writes_nothing_and_never_calls_the_model(lake):
    llm = FakeLLM()
    run = items.run_align_items(lake, [ZZZ], adjudicate=True, dry_run=True, llm=llm)
    assert llm.calls == 0 and run.written == [] and not items.alignment_dir(lake).exists()
    assert run.estimate.n_items == 1 and run.estimate.n_calls == 1 and run.estimate.worst_case_usd > 0     # the removed pandemic item


def test_a_run_whose_worst_case_is_over_the_cap_is_refused_before_any_call(lake):
    llm = FakeLLM()
    with pytest.raises(adj.BudgetExceeded, match="exceeds --max-usd"):
        items.run_align_items(lake, [ZZZ], adjudicate=True, max_usd=1e-6, llm=llm)
    assert llm.calls == 0 and not items.alignment_dir(lake).exists()


def test_an_adjudicated_run_records_the_verdict_and_never_repays_on_a_rerun(lake):
    llm = FakeLLM()
    items.run_align_items(lake, [ZZZ], adjudicate=True, llm=llm)
    assert llm.calls == 1
    dec = read(lake, "decisions")
    removed = dec[(dec["side"] == "older") & (dec["label"] == "removed")].iloc[0]
    assert removed["adjudicated"] and removed["decided_by"] == "llm"
    assert dec["adjudicated"].sum() == 1
    checkpoint = [json.loads(line) for line in (items.alignment_dir(lake) / adj.CHECKPOINT_NAME).read_text(encoding="utf-8").splitlines()]
    assert len(checkpoint) == 1 and checkpoint[0]["prompt_version"] == adj.PROMPT_VERSION
    first = file_bytes(lake)
    items.run_align_items(lake, [ZZZ], adjudicate=True, llm=llm)
    assert llm.calls == 1 and file_bytes(lake) == first


def test_a_model_that_says_the_removed_item_survives_without_a_quote_leaves_it_present(lake):
    items.run_align_items(lake, [ZZZ], adjudicate=True, llm=FakeLLM("reworded", None))
    dec = read(lake, "decisions")
    pandemic = dec[(dec["item_id"] == f"{lakefix.ACC['24']}:I.1A:i002") & (dec["side"] == "older")].iloc[0]
    assert pandemic["label"] == "uncertain" and pandemic["adjudicated"]
    assert not (dec["label"] == "removed").any()


def test_the_lexical_only_aligner_is_used_no_embedding_is_ever_requested(lake):
    items.run_align_items(lake, [ZZZ], align_params=AlignParams())
    dec = read(lake, "decisions")
    assert dec["sim_embed"].isna().all()


# --------------------------------------------------------------------------- the printed table

def test_the_summary_table_counts_labels_and_passages_per_pair(lake):
    run = items.run_align_items(lake, [ZZZ])
    row = next(r for r in run.summary if r["pair_id"] == P1)
    assert (row["older_reworded"], row["older_unchanged"], row["older_removed"], row["newer_new"]) == (1, 1, 1, 1)
    assert (row["passages_removed"], row["passages_added"], row["passages_reworded"]) == (1, 1, 0)
    text = items.format_summary(run.summary)
    assert "REMOVED" in text and "2 pair(s) compared, 0 not compared" in text and "removed items 1, new items 2" in text


# --------------------------------------------------------------------------- rows of the tables (hand-built outcomes)

def test_a_dry_run_table_shows_dashes_where_passages_were_not_computed(lake):
    run = items.run_align_items(lake, [ZZZ], dry_run=True)
    assert all(r["passages_removed"] is None for r in run.summary)
    text = items.format_summary(run.summary)
    assert "not computed (dry run)" in text and "removed items 1" in text


def hand_outcome(*, newer_changed=True):
    from semigraph.graph.alignment import AlignmentResult, Evidence, NewerDecision, OlderDecision

    older = OlderDecision("o0", "merged", "n0", "llm", Evidence(quote="q", quote_span=(3, 9), lex_sim=0.5, headline_ratio=88.0))
    before = NewerDecision("n0", "new", None, "unmatched", Evidence())
    after = NewerDecision("n0", "carried", "o0", "llm", Evidence()) if newer_changed else before
    pair = {"pair_id": "T-o-n", "ticker": "T", "older_accession": "o", "newer_accession": "n", "older_date": "d", "newer_date": "e",
            "comparable": True, "not_compared_reason": None}
    work = items.PairWork(pair, [], [], "", "", [], [], AlignmentResult((older,), (before,), AlignParams()))
    return items.PairOutcome(work, AlignmentResult((older,), (after,), AlignParams()), work.result, (), frozenset({"o0"}))


def test_a_decision_row_carries_the_evidence_the_quote_span_and_the_pair_and_older_rows_come_first():
    rows = items.decision_rows(hand_outcome())
    assert [(r["side"], r["item_id"], r["accession_no"]) for r in rows] == [("older", "o0", "o"), ("newer", "n0", "n")]
    older = rows[0]
    assert (older["label"], older["matched_item_id"], older["decided_by"], older["adjudicated"]) == ("merged", "n0", "llm", True)
    assert (older["quote"], older["quote_span_start"], older["quote_span_end"], older["headline_ratio"], older["sim_lex"]) == ("q", 3, 9, 88.0, 0.5)
    assert older["sim_embed"] is None and older["pair_id"] == "T-o-n"


def test_a_newer_decision_that_adjudication_changed_is_flagged_adjudicated_and_an_unchanged_one_is_not():
    changed = items.decision_rows(hand_outcome(newer_changed=True))[1]
    same = items.decision_rows(hand_outcome(newer_changed=False))[1]
    assert changed["adjudicated"] is True and changed["label"] == "carried" and changed["matched_item_id"] == "o0"
    assert same["adjudicated"] is False and same["label"] == "new"


def test_a_pair_that_was_not_compared_has_no_decision_and_no_passage_rows():
    pair = {"pair_id": "T-o-n", "ticker": "T", "older_accession": "o", "newer_accession": "n", "older_date": "d", "newer_date": "e",
            "comparable": False, "not_compared_reason": "why"}
    outcome = items.finalize_pair(items.PairWork(pair))
    assert items.decision_rows(outcome) == [] and items.passage_rows(outcome) == []
    assert items.pair_rows([outcome])[0]["not_compared_reason"] == "why"
    assert items.summary_row(outcome) == {"pair_id": "T-o-n", "ticker": "T", "older_date": "d", "newer_date": "e", "compared": False,
                                          "not_compared_reason": "why"}


def test_the_files_do_not_depend_on_the_order_the_pairs_were_processed_in(lake, tmp_path):
    from semigraph.graph.item_pairs import consecutive_pairs
    from semigraph.parsing.risk_item_quality import load_quality

    inputs = items.read_ticker_inputs(lake, ZZZ)
    pairs = consecutive_pairs(inputs[0], load_quality(items.items_dir(lake)))
    outcomes = [items.finalize_pair(items.prepare_pair(p, *inputs)) for p in pairs]
    items.write_ticker(tmp_path / "a", ZZZ, outcomes)
    items.write_ticker(tmp_path / "b", ZZZ, outcomes[::-1])
    for name in ("pairs", "decisions", "passages"):
        assert items.table_path(tmp_path / "a", ZZZ, name).read_bytes() == items.table_path(tmp_path / "b", ZZZ, name).read_bytes()


def test_a_stale_alignment_is_refused_before_anything_is_reset(lake):
    """An item file rebuilt after `align-items` leaves alignment files that describe items that no longer exist."""
    items.run_align_items(lake, [ZZZ])
    items.require_alignment(lake, [ZZZ])                                           # consistent: silent
    path = lake.interim_dir / "risk_items" / "ZZZ_risk_items.parquet"
    frame = pd.read_parquet(path)
    frame[frame["item_id"] != f"{lakefix.ACC['24']}:I.1A:i002"].to_parquet(path, index=False)
    with pytest.raises(items.AlignItemsError, match="do not match the risk-item file.*run `semigraph align-items` first"):
        items.require_alignment(lake, [ZZZ])


def test_no_risk_items_at_all_is_refused_with_both_commands_to_run(lake):
    with pytest.raises(items.AlignItemsError, match="run `semigraph risk-items` and `semigraph align-items` first"):
        items.require_alignment(lake, [])


# --------------------------------------------------------------------------- passage adjudication in the run (the lexical band)

BAND_HK = ("Following these export controls, we transitioned certain testing, validation and distribution operations out of China and Hong Kong.",
           "Certain of our testing and distribution operations remain in China and Hong Kong where we lease warehouse space for finished products.")
BAND_PARA = ("Long lead times for advanced packaging capacity make it difficult to respond to sudden changes in demand.",
             "Advanced packaging capacity has long lead times, so responding to sudden shifts in customer demand is difficult for us.")
PARA_QUOTE = "Advanced packaging capacity has long lead times, so responding to sudden shifts"     # verbatim slice of BAND_PARA[1]


@pytest.fixture
def band_lake(tmp_path, monkeypatch):
    """The synthetic lake with two band sentences in the wafer item of F24 and their lookalikes in F25 (pair P1: 4 band sentences)."""
    monkeypatch.setitem(lakefix.FILINGS, "24", [f"{lakefix.WAFER24} {BAND_HK[0]} {BAND_PARA[0]}", lakefix.TAX, lakefix.PANDEMIC])
    monkeypatch.setitem(lakefix.FILINGS, "25", [f"{lakefix.WAFER25} {BAND_HK[1]} {BAND_PARA[1]}", lakefix.TAX, lakefix.COMMERCIAL])
    return lakefix.build_lake(tmp_path)


class BandLLM:
    """Answers the passage prompts (schema: verdict, candidate, quote) and, separately, the item prompts (verdict, quote)."""

    def __init__(self, *, paraphrase="same"):
        self.item_calls, self.passage_calls, self.paraphrase = 0, 0, paraphrase

    def __call__(self, prompt, model_cls, *, model, max_tokens, thinking_off):
        if model_cls is not pad.PassageVerdict:
            self.item_calls += 1
            return model_cls(verdict="removed")
        self.passage_calls += 1
        if self.paraphrase == "same" and f"<<<\n{BAND_PARA[0]}\n>>>" in prompt:
            line = next(ln for ln in prompt.splitlines() if ln.startswith("[") and BAND_PARA[1] in ln)
            return model_cls(verdict="same", candidate=int(line[1:line.index("]")]), quote=PARA_QUOTE)
        return model_cls(verdict="different")


def passages_of(settings, out_dir=None):
    return pd.read_parquet(items.table_path(out_dir or items.alignment_dir(settings), ZZZ, "passages"))


def p1(frame):
    return frame[frame["pair_id"] == P1]


def test_the_passage_table_has_a_band_adjudicated_column_last_and_boolean():
    fields = list(items.PASSAGE_SCHEMA)
    assert fields[-1].name == "band_adjudicated" and str(fields[-1].type) == "bool"


def test_a_plain_run_marks_band_sentences_reworded_by_the_band_and_calls_no_model(band_lake):
    llm = BandLLM()
    run = items.run_align_items(band_lake, [ZZZ], llm=llm)
    assert llm.passage_calls == 0 and run.passage_estimate is None and run.passage_verdicts_used == 0
    frame = p1(passages_of(band_lake))
    reworded = frame[frame["kind"] == "reworded"]
    assert len(reworded) >= 1 and reworded["decided_by"].eq("sentence_reworded_band").all() and not reworded["band_adjudicated"].any()
    assert not (items.alignment_dir(band_lake) / pad.CHECKPOINT_NAME).exists()
    assert all(r.get("band") is None for r in run.summary)                     # nothing was enumerated: no flag, no cache


def test_a_dry_run_lists_the_band_estimate_writes_nothing_and_calls_nothing(band_lake):
    llm = BandLLM()
    run = items.run_align_items(band_lake, [ZZZ], adjudicate_passages=True, dry_run=True, llm=llm)
    assert llm.passage_calls == 0 and run.written == [] and not items.alignment_dir(band_lake).exists()
    est = run.passage_estimate
    assert (est.n_items, est.n_calls, est.n_cached) == (4, 4, 0) and est.worst_case_usd > 0 and run.passage_budget_usd == 0.5
    row = next(r for r in run.summary if r["pair_id"] == P1)
    assert row["band"] == 4 and row["band_answered"] == 0 and row["passages_removed"] is None


def test_a_run_over_the_cap_is_refused_before_any_call_and_writes_nothing(band_lake):
    llm = BandLLM()
    with pytest.raises(pad.BudgetExceeded, match="exceeds --max-usd"):
        items.run_align_items(band_lake, [ZZZ], adjudicate_passages=True, max_usd=1e-9, llm=llm)
    assert llm.passage_calls == 0 and not items.alignment_dir(band_lake).exists()


def test_with_the_flag_the_band_is_settled_by_the_verdicts_and_the_files_say_so(band_lake):
    llm = BandLLM()
    run = items.run_align_items(band_lake, [ZZZ], adjudicate_passages=True, llm=llm)
    assert llm.passage_calls == 4 and llm.item_calls == 0 and run.passage_calls == 4 and run.passage_verdicts_used == 4
    frame = p1(passages_of(band_lake))
    removed = frame[frame["kind"] == "removed"]
    hk = removed[removed["text"].str.contains("Following these export controls")].iloc[0]
    assert hk["decided_by"] == "sentence_absent_llm" and hk["band_adjudicated"]
    para = frame[(frame["kind"] == "reworded") & frame["text"].str.contains("Long lead times")].iloc[0]
    assert para["counterpart_text"] == BAND_PARA[1] and para["decided_by"] == "sentence_reworded_llm" and para["band_adjudicated"]
    added = frame[frame["kind"] == "added"]
    assert added["text"].str.contains("Certain of our testing").any()              # the lookalike is added: its own verdict says so
    assert not added["text"].str.contains("Advanced packaging capacity has long").any()   # covered by the verified paraphrase
    lines = [json.loads(line) for line in (items.alignment_dir(band_lake) / pad.CHECKPOINT_NAME).read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 4 and {ln["prompt_version"] for ln in lines} == {pad.PROMPT_VERSION}
    row = next(r for r in run.summary if r["pair_id"] == P1)
    assert row["band"] == 4 and row["band_answered"] == 4


def test_cached_verdicts_are_replayed_without_the_flag_and_the_files_are_byte_identical(band_lake):
    items.run_align_items(band_lake, [ZZZ], adjudicate_passages=True, llm=BandLLM())
    bought = file_bytes(band_lake)
    for path in items.alignment_dir(band_lake).glob("*.parquet"):
        path.unlink()
    llm = BandLLM()
    replay = items.run_align_items(band_lake, [ZZZ], llm=llm)                      # NO --adjudicate-passages
    assert llm.passage_calls == 0 and replay.passage_verdicts_used == 4 and replay.passage_calls == 0
    assert file_bytes(band_lake) == bought and pad.CHECKPOINT_NAME in bought       # three parquets rebuilt from the cache alone
    again = items.run_align_items(band_lake, [ZZZ], adjudicate_passages=True, llm=llm)      # with the flag: nothing left to buy
    assert llm.passage_calls == 0 and again.passage_estimate.n_calls == 0 and file_bytes(band_lake) == bought


def test_without_a_cache_the_same_run_differs_so_the_replay_is_really_used(band_lake):
    items.run_align_items(band_lake, [ZZZ], adjudicate_passages=True, llm=BandLLM())
    cached = p1(passages_of(band_lake))
    (items.alignment_dir(band_lake) / pad.CHECKPOINT_NAME).unlink()
    items.run_align_items(band_lake, [ZZZ])
    plain = p1(passages_of(band_lake))
    lookalike = lambda f: f[(f["kind"] == "removed") & f["text"].str.contains("Following these export controls")]     # noqa: E731
    assert len(lookalike(cached)) == 1 and lookalike(plain).empty


def test_a_different_model_does_not_use_the_cached_answers(band_lake):
    items.run_align_items(band_lake, [ZZZ], adjudicate_passages=True, llm=BandLLM())
    run = items.run_align_items(band_lake, [ZZZ], adjudicate_passages=True, dry_run=True, llm=BandLLM(), model="vendor/other-model")
    assert run.passage_estimate.n_cached == 0 and run.passage_estimate.n_calls == 4


def test_a_paraphrase_the_model_calls_different_is_removed_but_only_by_that_verdict(band_lake):
    items.run_align_items(band_lake, [ZZZ], adjudicate_passages=True, llm=BandLLM(paraphrase="different"))
    frame = p1(passages_of(band_lake))
    assert frame[(frame["kind"] == "removed") & frame["text"].str.contains("Long lead times")].shape[0] == 1


def test_the_passage_checkpoint_follows_out_dir(band_lake, tmp_path):
    out = tmp_path / "elsewhere"
    items.run_align_items(band_lake, [ZZZ], adjudicate_passages=True, llm=BandLLM(), out_dir=out)
    assert (out / pad.CHECKPOINT_NAME).exists() and not items.alignment_dir(band_lake).exists()
    assert not passages_of(band_lake, out).empty


def test_both_adjudications_share_the_cap_the_passage_step_gets_what_the_item_step_left(band_lake):
    llm = BandLLM()
    run = items.run_align_items(band_lake, [ZZZ], adjudicate=True, adjudicate_passages=True, max_usd=0.5, llm=llm)
    assert llm.item_calls == 1 and llm.passage_calls > 0
    item_records = adj.Checkpoint(items.alignment_dir(band_lake) / adj.CHECKPOINT_NAME).records
    spent = sum(r["est_usd_upper_bound"] for r in item_records.values())
    assert run.passage_budget_usd == pytest.approx(0.5 - spent) and 0 < spent < 0.5


def test_the_passage_step_is_refused_when_the_item_step_left_too_little_and_nothing_is_written(band_lake):
    item_worst = items.run_align_items(band_lake, [ZZZ], adjudicate=True, dry_run=True).estimate.worst_case_usd
    llm = BandLLM()
    with pytest.raises(pad.BudgetExceeded, match="exceeds --max-usd"):
        items.run_align_items(band_lake, [ZZZ], adjudicate=True, adjudicate_passages=True, max_usd=item_worst + 1e-5, llm=llm)
    assert llm.item_calls == 1 and llm.passage_calls == 0 and not list(items.alignment_dir(band_lake).glob("*.parquet"))


def test_a_dry_run_with_both_flags_shows_the_remaining_budget_after_the_item_estimate(band_lake):
    run = items.run_align_items(band_lake, [ZZZ], adjudicate=True, adjudicate_passages=True, dry_run=True, max_usd=0.5)
    assert run.passage_budget_usd == pytest.approx(0.5 - run.estimate.worst_case_usd)


def test_the_summary_table_shows_the_band_columns(band_lake):
    run = items.run_align_items(band_lake, [ZZZ], adjudicate_passages=True, dry_run=True)
    header, *_ = items.format_summary(run.summary).splitlines()
    assert "band" in header and "b-ans" in header


def test_passage_rows_carry_the_adjudication_flag():
    fake = passage("removed", band_adjudicated=True, decided_by="sentence_absent_llm")
    (row,) = items.passage_rows(outcome_with([fake]))
    assert row["band_adjudicated"] is True and row["decided_by"] == "sentence_absent_llm"
    (plain,) = items.passage_rows(outcome_with([passage()]))
    assert plain["band_adjudicated"] is False
