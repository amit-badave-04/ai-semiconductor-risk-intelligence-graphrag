"""Sentence-level source-text gold (eval/gold.py): validators, sampling, packets and passage scoring.

Every fixture score is asserted against the module's thresholds, so a change of scorer or threshold fails loudly here
instead of silently steering a test onto the wrong branch.
"""

import json
import random
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from semigraph.eval import gold

# --- fixtures: one sentence per label class, and the other filing's text -----------------------------------------

S_PRESENT = "We are subject to complex laws, rules and regulations that affect our sales and operations abroad."
S_REWORDED = "Export controls could significantly reduce our revenue from customers located in China."
S_REMOVED = "The Notified Advanced Computing process has not resulted in approvals for exports of products to customers in China."

Q_TENSE = "We were subject to complex laws, rules and regulations that affected our sales and operations abroad."
Q_REWORD = "Export controls may substantially reduce our revenue from customers located in Mainland China."
Q_PARTIAL = "Approvals for exports of products to customers in China have been limited by license requirements."
Q_UNRELATED = "We depend on TSMC for manufacturing capacity and advanced packaging services worldwide."
Q_NUMBERS = "1234567890 1234567890 1234567890 1234567890 1234567890"

OTHER = " ".join([Q_TENSE, Q_REWORD, Q_PARTIAL, Q_UNRELATED, Q_NUMBERS, "Market volatility could affect the value of our stock."])

SENTS = [{"sentence_id": "a:I.1A:i001#s000", "item_id": "a:I.1A:i001", "text": S_PRESENT},
         {"sentence_id": "a:I.1A:i001#s001", "item_id": "a:I.1A:i001", "text": S_REWORDED},
         {"sentence_id": "a:I.1A:i002#s000", "item_id": "a:I.1A:i002", "text": S_REMOVED}]
P, R, X = (s["sentence_id"] for s in SENTS)


def lab(sid, kind, **kw):
    return {"sentence_id": sid, "label": kind, **kw}


def validate(labels, side="older", other=OTHER, sentences=SENTS):
    return gold.validate_sentence_annotation(labels, sentences, other, side=side)


def reason(labels, **kw):
    v = validate(labels, **kw)
    assert not v.accepted, "the label was expected to be rejected"
    return v.rejected[0][1]


def test_fixture_scores_sit_on_the_intended_side_of_every_threshold():
    sim = gold.sentence_similarity
    assert sim(S_PRESENT, Q_TENSE) >= gold.SENT_VERBATIM_SIM                       # tense-only edit: a copy
    assert gold.SENT_REWORDED_MIN_SIM <= sim(S_REWORDED, Q_REWORD) < gold.SENT_VERBATIM_SIM
    assert gold.SENT_PRESENT_MIN_SIM <= sim(S_REMOVED, Q_PARTIAL) < gold.SENT_VERBATIM_SIM
    assert sim(S_REMOVED, Q_UNRELATED) < gold.SENT_PRESENT_MIN_SIM
    assert sim(S_REMOVED, Q_NUMBERS) < gold.SENT_REWORDED_MIN_SIM
    assert len(Q_NUMBERS) >= gold.SENT_MIN_QUOTE_CHARS
    assert (gold.SENT_MIN_QUOTE_CHARS, gold.SENT_PRESENT_MIN_SIM, gold.SENT_REWORDED_MIN_SIM, gold.SENT_VERBATIM_SIM) \
        == (30, 70, 45, 92)


# --- `present` ---------------------------------------------------------------------------------------------------

def test_present_accepts_a_verbatim_quote_of_the_same_sentence():
    v = validate([lab(P, "present", quote=S_PRESENT.replace("We are", "We were").replace("affect ", "affected "))])
    assert v.accepted == {P: "present"} and not v.rejected


def test_present_accepts_a_tense_only_edit_and_ignores_case_whitespace_and_smart_quotes():
    q = "  we WERE subject to complex laws,\nrules and regulations that AFFECTED our sales and operations abroad. "
    assert validate([lab(P, "present", quote=q)]).accepted == {P: "present"}


def test_present_accepts_a_partial_overlap_at_or_above_the_relatedness_floor():
    assert validate([lab(X, "present", quote=Q_PARTIAL)]).accepted == {X: "present"}


def test_present_rejects_a_quote_that_is_not_in_the_source_text():
    assert "not found" in reason([lab(P, "present", quote="Nvidia will pay no fines whatsoever, none, ever again.")])


def test_present_rejects_a_quote_too_short_to_prove_anything():
    assert "short" in reason([lab(P, "present", quote="subject to complex laws")])
    assert "short" in reason([lab(P, "present", quote="x" * (gold.SENT_MIN_QUOTE_CHARS - 1))])


def test_present_rejects_a_real_quote_that_is_unrelated_to_the_sentence():
    assert "not related" in reason([lab(X, "present", quote=Q_UNRELATED)])


def test_missing_or_blank_quote_is_rejected_with_a_missing_field_reason():
    assert "missing field 'quote'" in reason([lab(P, "present")])
    assert "missing field 'quote'" in reason([lab(P, "reworded", quote="   ")])
    assert "missing field 'quote'" in reason([lab(P, "present", quote=12345)])


# --- `reworded` --------------------------------------------------------------------------------------------------

def test_reworded_accepts_a_differently_worded_counterpart():
    assert validate([lab(R, "reworded", quote=Q_REWORD)]).accepted == {R: "reworded"}


def test_reworded_rejects_a_near_verbatim_copy_which_is_present_not_reworded():
    assert "near-verbatim" in reason([lab(P, "reworded", quote=Q_TENSE)])


def test_reworded_rejects_a_quote_below_the_relatedness_floor():
    assert "not related" in reason([lab(X, "reworded", quote=Q_NUMBERS)])


def test_reworded_rejects_a_fabricated_quote():
    assert "not found" in reason([lab(R, "reworded", quote="Chinese customers will never face any licence requirements.")])


# --- `removed` ---------------------------------------------------------------------------------------------------

TERMS = ["Notified Advanced Computing", "NAC process", "approvals for exports"]


def test_removed_accepts_three_distinct_terms_when_the_sentence_is_really_absent():
    assert validate([lab(X, "removed", search_terms=TERMS)]).accepted == {X: "removed"}


def test_removed_needs_three_distinct_terms_of_four_or_more_characters():
    assert "search_terms" in reason([lab(X, "removed", search_terms=TERMS[:2])])
    assert "search_terms" in reason([lab(X, "removed", search_terms=["NAC process", "nac PROCESS", " nac process "])])
    assert "search_terms" in reason([lab(X, "removed", search_terms=["NAC", "HK", "PRC", "China"])])
    assert "search_terms" in reason([lab(X, "removed", search_terms=["NAC process", "", None, 42])])


def test_a_term_shorter_than_four_characters_does_not_count_towards_the_three():
    assert "search_terms" in reason([lab(X, "removed", search_terms=["NAC", "PRC", "approvals for exports", "NAC process"])])
    assert validate([lab(X, "removed", search_terms=["NAC", "PRC", "NAC process", "approvals", "Advanced"])]).accepted


def test_removed_rejects_missing_or_malformed_search_terms():
    assert "search_terms" in reason([lab(X, "removed")])
    assert "search_terms" in reason([lab(X, "removed", search_terms="Notified, Advanced, Computing")])


def test_removed_is_contradicted_when_the_sentence_occurs_verbatim_in_the_source_text():
    v = validate([lab(X, "removed", search_terms=TERMS)], other=OTHER + " " + S_REMOVED.upper())
    assert not v.accepted and "still in the source text" in v.rejected[0][1]


def test_removed_is_contradicted_by_a_near_verbatim_copy_but_not_by_a_related_rewording():
    edited = S_REMOVED.replace("has not resulted", "had not resulted")
    assert gold.sentence_similarity(S_REMOVED, edited) >= gold.SENT_VERBATIM_SIM
    r = reason([lab(X, "removed", search_terms=TERMS)], other=OTHER + " " + edited)
    assert "near-verbatim" in r
    # a genuinely different wording is not a contradiction: whether it is `removed` or `reworded` is the labeller's call
    assert validate([lab(X, "removed", search_terms=TERMS)], other=OTHER + " " + Q_PARTIAL).accepted


# --- structure: ids, fields, sides -------------------------------------------------------------------------------

def test_unknown_missing_duplicate_and_malformed_entries_are_rejected_with_a_reason_each():
    v = validate([lab("zzz#s000", "present", quote=Q_TENSE),
                  {"label": "present", "quote": Q_TENSE},
                  "not an object",
                  {"sentence_id": ["unhashable"], "label": "present"},
                  {"sentence_id": P},
                  lab(P, "maybe", quote=Q_TENSE),
                  lab(R, "reworded", quote=Q_REWORD),
                  lab(R, "reworded", quote=Q_REWORD)])
    assert v.accepted == {R: "reworded"}
    reasons = [r for _, r in v.rejected]
    assert any("unknown sentence" in r for r in reasons)
    assert any("missing field 'sentence_id'" in r for r in reasons)
    assert any("not an object" in r for r in reasons)
    assert any("missing field 'label'" in r for r in reasons)
    assert any("unknown label 'maybe'" in r for r in reasons)
    assert any("duplicate" in r for r in reasons)
    assert v.missing == [P, X]                     # sentence order, unlabelled only


def test_the_side_decides_which_absent_label_is_allowed():
    assert "unknown label 'added'" in reason([lab(X, "added", search_terms=TERMS)], side="older")
    assert "unknown label 'removed'" in reason([lab(X, "removed", search_terms=TERMS)], side="newer")


def test_newer_side_added_is_validated_like_removed_with_the_direction_reversed():
    older_text = " ".join([Q_TENSE, Q_REWORD, Q_UNRELATED])           # the OLDER filing is the source text
    ok = validate([lab(X, "added", search_terms=TERMS)], side="newer", other=older_text)
    assert ok.accepted == {X: "added"}
    contradicted = validate([lab(P, "added", search_terms=TERMS)], side="newer", other=older_text)
    assert not contradicted.accepted and "near-verbatim" in contradicted.rejected[0][1]
    both = validate([lab(P, "present", quote=Q_TENSE), lab(R, "reworded", quote=Q_REWORD)], side="newer", other=older_text)
    assert both.accepted == {P: "present", R: "reworded"}


def test_an_unknown_side_is_a_programming_error():
    with pytest.raises(ValueError, match="side"):
        gold.validate_sentence_annotation([], SENTS, OTHER, side="sideways")


def test_validation_of_an_empty_annotation_reports_every_sentence_as_missing():
    v = validate([])
    assert v.accepted == {} and v.rejected == [] and v.missing == [P, R, X]


# --- agreement statistics reuse the item-level functions ---------------------------------------------------------

def test_agreement_and_consensus_work_on_the_sentence_label_set():
    a = {P: "present", R: "reworded", X: "removed"}
    b = {P: "present", R: "reworded", X: "removed"}
    c = {P: "present", R: "present", X: "reworded"}
    agg = gold.aggregate({"a": a, "b": b, "c": c})
    assert agg.consensus == {P: "present", R: "reworded", X: "removed"} and agg.needs_adjudication == []
    assert 0.0 < agg.pairwise_agreement < 1.0 and agg.alpha < 1.0
    assert gold.aggregate({"a": a, "b": b}).alpha == pytest.approx(1.0)
    disputed = gold.aggregate({"a": {P: "present"}, "b": {P: "reworded"}, "c": {P: "removed"}})
    assert disputed.consensus == {} and disputed.needs_adjudication == [P]
    assert gold.majority_label(["present", "present", "removed"]) == "present"


# --- the labelling instructions ----------------------------------------------------------------------------------

def test_instructions_state_the_blind_protocol_the_three_labels_and_the_edge_rules():
    text = gold.SENTENCE_LABELLING_INSTRUCTIONS
    assert text == gold.sentence_instructions("older")
    for needle in ("never see any algorithm output", "present", "reworded", "removed", "quote", "search_terms",
                   "30 characters", "4 characters", "sentence_id"):
        assert needle in text
    assert "tense" in text and "number" in text                     # tense/number-only edits are present
    assert "another risk factor" in text                            # a moved sentence is present
    assert "search the other text again" in text                    # unsure between removed and reworded
    assert "added" not in text                                      # the older side never uses `added`


def test_newer_side_instructions_use_added_and_name_the_older_filing_as_the_source():
    text = gold.sentence_instructions("newer")
    assert "added" in text and "never see any algorithm output" in text and "OLDER" in text
    assert "removed" not in text                                    # the newer side never uses `removed`
    with pytest.raises(ValueError):
        gold.sentence_instructions("both")


# --- sentence splitting ------------------------------------------------------------------------------------------

def make_item(item_id, n_sentences, *, char_start=None, headline=None):
    tag = item_id.split(":")[-1]
    text = " ".join(f"Item {tag} sentence {i:03d} describes one distinct supply chain risk factor." for i in range(n_sentences))
    item = {"item_id": item_id, "headline": headline or f"Headline of {item_id}", "text": text}
    if char_start is not None:
        item["char_start"] = char_start
    return item


def test_item_sentences_number_the_raw_split_and_carry_item_and_section_offsets():
    item = {"item_id": "acc:I.1A:i003", "headline": "H", "char_start": 1000,
            "text": "Too short. This sentence is long enough to be labelled by a reader. Tiny. And here is another long enough sentence."}
    sents = gold.item_sentences(item)
    assert [s["sentence_id"] for s in sents] == ["acc:I.1A:i003#s001", "acc:I.1A:i003#s003"]     # 0-based over the raw split
    first = sents[0]
    assert item["text"][first["start"]:first["end"]] == first["text"]
    assert (first["section_start"], first["section_end"]) == (1000 + first["start"], 1000 + first["end"])
    assert first["item_id"] == "acc:I.1A:i003"


def test_item_sentences_ids_survive_a_change_of_the_minimum_length():
    item = make_item("i", 3, char_start=0)
    loose = gold.item_sentences(item, min_chars=10)
    strict = gold.item_sentences(item, min_chars=500)
    assert [s["sentence_id"] for s in loose] == ["i#s000", "i#s001", "i#s002"] and strict == []


def test_item_sentences_without_a_char_start_have_no_section_offsets():
    sents = gold.item_sentences(make_item("i", 2))
    assert all(s["section_start"] is None and s["section_end"] is None for s in sents)


# --- sampling ----------------------------------------------------------------------------------------------------

def make_pair_items():
    """Ten items of growing size; the largest (i09) is the flagship."""
    items = [make_item(f"a:I.1A:i{n:02d}", 2 + n, char_start=10_000 * n) for n in range(10)]
    consensus = {i["item_id"]: "unchanged" for i in items}
    consensus.update({"a:I.1A:i01": "reworded", "a:I.1A:i02": "merged"})
    return items, consensus


def test_the_sample_is_seeded_deterministic_and_independent_of_input_order_and_seed_is_recorded():
    items, consensus = make_pair_items()
    a = gold.sentence_sample(items, consensus, pair_id="P1", seed=7, k=4)
    b = gold.sentence_sample(list(reversed(items)), consensus, pair_id="P1", seed=7, k=4)
    c = gold.sentence_sample(items, consensus, pair_id="P1", seed=8, k=4)
    ids = lambda s: [i["item_id"] for i in s["items"]]  # noqa: E731
    assert ids(a) == ids(b) and a == b
    assert ids(a) != ids(c)
    assert a["seed"] == 7 and a["rng_key"] == "7|P1|older" and a["k"] == 4 and a["min_chars"] == gold.SENT_MIN_CHARS
    assert ids(a) == sorted(ids(a))


def test_a_different_pair_or_side_draws_a_different_stream():
    items, consensus = make_pair_items()
    keys = {gold.sentence_sample(items, consensus, pair_id=p, seed=1, k=3)["rng_key"] for p in ("P1", "P2")}
    assert keys == {"1|P1|older", "1|P2|older"}
    carried = {i["item_id"]: "carried" for i in items}
    assert gold.sentence_sample(items, carried, pair_id="P1", side="newer", seed=1, k=3)["rng_key"] == "1|P1|newer"


def test_the_largest_eligible_item_is_always_included_whatever_the_seed():
    items, consensus = make_pair_items()
    for seed in range(25):
        sample = gold.sentence_sample(items, consensus, pair_id="P1", seed=seed, k=3)
        assert "a:I.1A:i09" in [i["item_id"] for i in sample["items"]]
        assert len(sample["items"]) == 3 and sample["flagship_item_id"] == "a:I.1A:i09"


def test_removed_disputed_and_unlabelled_items_are_never_sampled():
    items, consensus = make_pair_items()
    consensus["a:I.1A:i09"] = "removed"                     # the biggest item is gone: it cannot be the flagship
    del consensus["a:I.1A:i08"]                              # disputed / unlabelled at item level
    sample = gold.sentence_sample(items, consensus, pair_id="P1", seed=3, k=50)
    chosen = {i["item_id"] for i in sample["items"]}
    assert "a:I.1A:i09" not in chosen and "a:I.1A:i08" not in chosen and len(chosen) == 8
    assert sample["flagship_item_id"] == "a:I.1A:i07"
    assert sample["overall_largest_item_id"] == "a:I.1A:i09" and "not eligible" in sample["flagship_note"]


def test_k_counts_the_flagship_and_larger_k_than_eligible_takes_every_eligible_item():
    items, consensus = make_pair_items()
    assert len(gold.sentence_sample(items, consensus, pair_id="P", k=1)["items"]) == 1
    assert len(gold.sentence_sample(items, consensus, pair_id="P", k=6)["items"]) == 6
    assert len(gold.sentence_sample(items, consensus, pair_id="P", k=99)["items"]) == 10
    with pytest.raises(ValueError, match="k"):
        gold.sentence_sample(items, consensus, pair_id="P", k=0)


def test_items_without_a_labellable_sentence_are_skipped_and_an_empty_pair_yields_an_empty_sample():
    items = [{"item_id": "t", "headline": "", "text": "Tiny."}]
    sample = gold.sentence_sample(items, {"t": "unchanged"}, pair_id="P")
    assert sample["items"] == [] and sample["flagship_item_id"] is None and sample["n_sentences"] == 0
    assert gold.sentence_sample([], {}, pair_id="P")["items"] == []


def test_newer_side_samples_only_carried_items():
    items, _ = make_pair_items()
    consensus = {i["item_id"]: "carried" for i in items}
    consensus["a:I.1A:i09"] = "new"
    consensus["a:I.1A:i00"] = "unchanged"                   # an older-side label is not valid on the newer side
    sample = gold.sentence_sample(items, consensus, pair_id="P", side="newer", k=99)
    assert {i["item_id"] for i in sample["items"]} == {f"a:I.1A:i{n:02d}" for n in range(1, 9)}


def test_the_sentence_cap_drops_the_smallest_non_flagship_items_first_and_records_them():
    items, consensus = make_pair_items()
    full = gold.sentence_sample(items, consensus, pair_id="P", seed=1, k=99, max_sentences=10_000)
    per_item = {i["item_id"]: len(i["sentences"]) for i in full["items"]}
    assert per_item["a:I.1A:i09"] == 11 and full["dropped_items"] == [] and full["n_sentences"] == sum(per_item.values())
    capped = gold.sentence_sample(items, consensus, pair_id="P", seed=1, k=99, max_sentences=40)
    kept = {i["item_id"] for i in capped["items"]}
    dropped = [d["item_id"] for d in capped["dropped_items"]]
    assert capped["n_sentences"] <= 40 and "a:I.1A:i09" in kept and dropped and dropped[0] == "a:I.1A:i00"
    assert dropped == sorted(dropped)                                             # smallest first == lowest index here
    assert set(dropped).isdisjoint(kept) and set(dropped) | kept == set(per_item)
    assert all(set(d) == {"item_id", "chars", "n_sentences"} for d in capped["dropped_items"])
    assert not capped["flagship_exceeds_cap"]


def test_a_flagship_that_alone_exceeds_the_cap_is_kept_and_flagged():
    items, consensus = make_pair_items()
    sample = gold.sentence_sample(items, consensus, pair_id="P", k=5, max_sentences=3)
    assert [i["item_id"] for i in sample["items"]] == ["a:I.1A:i09"]
    assert sample["flagship_exceeds_cap"] is True and sample["n_sentences"] == 11
    assert len(sample["dropped_items"]) == 4


def test_the_sample_never_reads_anything_but_item_texts_and_gold_consensus():
    items, consensus = make_pair_items()
    with_junk = [{**i, "predicted": "removed", "sim_embed": 0.1} for i in items]
    assert gold.sentence_sample(with_junk, consensus, pair_id="P", seed=5)["items"] == \
        gold.sentence_sample(items, consensus, pair_id="P", seed=5)["items"]
    sample = gold.sentence_sample(items, consensus, pair_id="P")
    assert set(sample["items"][0]) == {"item_id", "headline", "char_start", "char_end", "sentences"}


def test_sampled_sentences_are_ids_texts_and_offsets_inside_their_item():
    items, consensus = make_pair_items()
    item_by_id = {i["item_id"]: i for i in items}
    for entry in gold.sentence_sample(items, consensus, pair_id="P", k=4)["items"]:
        item = item_by_id[entry["item_id"]]
        assert entry["char_start"] == item["char_start"] and entry["char_end"] == item["char_start"] + len(item["text"])
        for s in entry["sentences"]:
            assert s["sentence_id"].startswith(entry["item_id"] + "#s")
            assert item["text"][s["start"]:s["end"]] == s["text"]
            assert s["section_start"] == item["char_start"] + s["start"]


def test_random_module_state_is_untouched_by_sampling():
    items, consensus = make_pair_items()
    random.seed(99)
    before = random.random()
    random.seed(99)
    gold.sentence_sample(items, consensus, pair_id="P", seed=1)
    assert random.random() == before


# --- the labeller packet -----------------------------------------------------------------------------------------

def test_sentence_packet_holds_sentences_the_full_other_text_and_the_rules_and_nothing_else():
    items, consensus = make_pair_items()
    sample = gold.sentence_sample(items, consensus, pair_id="P", k=3)
    meta = {"pair_id": "P", "ticker": "NVDA", "split": "development", "side": "older"}
    packet = gold.build_sentence_packet(meta, sample["items"], OTHER)
    assert set(packet) == {"pair", "side", "items", "other_section_text", "instructions"}
    assert packet["pair"] == meta and packet["side"] == "older" and packet["other_section_text"] == OTHER
    assert [i["item_id"] for i in packet["items"]] == [i["item_id"] for i in sample["items"]]
    for entry, src in zip(packet["items"], sample["items"]):
        assert set(entry) == {"item_id", "headline", "sentences"}
        assert [set(s) for s in entry["sentences"]] == [{"sentence_id", "text"}] * len(src["sentences"])
    data_only = json.dumps({k: v for k, v in packet.items() if k != "instructions"}).lower()
    for leaked in ("consensus", "unchanged", "merged", "carried", "lineage", "algorithm", "predicted", "seed", "dropped",
                   "eligible"):
        assert leaked not in data_only
    assert packet["instructions"] == gold.sentence_instructions("older")


def test_sentence_packet_for_the_newer_side_carries_the_newer_rules():
    meta = {"pair_id": "P", "side": "newer"}
    packet = gold.build_sentence_packet(meta, [], "older text")
    assert packet["side"] == "newer" and packet["instructions"] == gold.sentence_instructions("newer")
    assert gold.build_sentence_packet({"pair_id": "P"}, [], "x")["side"] == "older"
    assert gold.build_sentence_packet({"pair_id": "P"}, [], "x", side="newer")["side"] == "newer"
    with pytest.raises(ValueError):
        gold.build_sentence_packet({"pair_id": "P", "side": "middle"}, [], "x")


# --- spans for scoring from the frozen gold ----------------------------------------------------------------------

def test_sentence_spans_and_gold_records_round_trip_through_a_frozen_entry():
    items, consensus = make_pair_items()
    sample = gold.sentence_sample(items, consensus, pair_id="P", k=2)
    spans = gold.sentence_spans(sample)
    first = sample["items"][0]["sentences"][0]
    assert spans[first["sentence_id"]] == [first["item_id"], first["section_start"], first["section_end"]]
    labels = {sid: "removed" if n % 2 else "present" for n, sid in enumerate(sorted(spans))}
    entry = {"labels": labels, "spans": spans}
    records = gold.gold_sentence_records(entry)
    assert [r["sentence_id"] for r in records] == sorted(labels)
    assert all(set(r) == {"sentence_id", "item_id", "section_start", "section_end", "label"} for r in records)
    assert {r["sentence_id"]: r["label"] for r in records} == labels
    # labels without a span cannot be scored: refuse loudly
    with pytest.raises(ValueError, match="span"):
        gold.gold_sentence_records({"labels": {"x#s000": "removed"}, "spans": {}})


# --- scoring passages --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Pas:
    kind: str
    item_id: str
    char_start: int
    char_end: int
    text: str = ""


def rec(n, label, item="it1", start=None):
    """Gold sentence n (0-based) of an item laid out every 100 characters, 80 wide."""
    start = 100 * n if start is None else start
    return {"sentence_id": f"{item}#s{n:03d}", "item_id": item, "section_start": start, "section_end": start + 80, "label": label}


GOLD10 = [rec(n, label) for n, label in enumerate(
    ["present", "removed", "removed", "present", "reworded", "removed", "present", "present", "removed", "present"])]


def test_perfect_passages_give_perfect_scores_and_the_third_class_is_in_the_confusion_matrix():
    passages = [Pas("removed", "it1", 100, 280), Pas("reworded", "it1", 400, 480),
                Pas("removed", "it1", 500, 580), Pas("removed", "it1", 800, 880)]
    m = gold.score_passages(passages, GOLD10)
    assert (m.sentence["tp"], m.sentence["fp"], m.sentence["fn"]) == (4, 0, 0)
    assert m.sentence["precision"] == 1.0 and m.sentence["recall"] == 1.0
    assert (m.passage["tp"], m.passage["fp"], m.passage["unverifiable"]) == (3, 0, 0)
    assert m.passage["precision"] == 1.0 and m.passage["recall"] == 1.0 and m.passage["n_gold_runs"] == 3
    assert m.confusion["removed"] == {"removed": 4} and m.confusion["reworded"] == {"reworded": 1}
    assert m.confusion["present"] == {"present": 5}
    assert m.n_gold_sentences == 10 and m.n_gold_positive == 4 and m.uncertain_rate == 0.0
    assert m.false_positive_ids == [] and m.false_negative_ids == [] and m.positive_kind == "removed"


def test_false_positives_and_false_negatives_are_listed_and_counted_at_both_levels():
    passages = [Pas("removed", "it1", 100, 180),                       # s001: right
                Pas("removed", "it1", 300, 380),                       # s003 is present: false positive
                Pas("removed", "it1", 400, 480)]                       # s004 is reworded: false positive
    m = gold.score_passages(passages, GOLD10)
    assert (m.sentence["tp"], m.sentence["fp"], m.sentence["fn"]) == (1, 2, 3)
    assert m.sentence["precision"] == pytest.approx(1 / 3) and m.sentence["recall"] == pytest.approx(1 / 4)
    assert m.false_positive_ids == ["it1#s003", "it1#s004"]
    assert m.false_negative_ids == ["it1#s002", "it1#s005", "it1#s008"]
    assert (m.passage["tp"], m.passage["fp"]) == (1, 2) and m.passage["precision"] == pytest.approx(1 / 3)
    assert (m.passage["recalled"], m.passage["n_gold_runs"]) == (0, 3)
    assert m.confusion["reworded"] == {"removed": 1} and m.confusion["removed"]["present"] == 3


def test_a_wrongly_flagged_reworded_passage_is_a_present_to_reworded_confusion_not_a_drop():
    m = gold.score_passages([Pas("reworded", "it1", 0, 80)], GOLD10)
    assert m.sentence["fp"] == 0 and m.confusion["present"] == {"reworded": 1, "present": 4} and m.sentence["precision"] is None


def test_a_gold_sentence_is_covered_when_at_least_half_of_it_lies_inside_the_passage():
    just = [Pas("removed", "it1", 160, 400)]           # s001 spans 100-180: 20 of 80 inside (25%); s002 200-280 fully inside
    m = gold.score_passages(just, [rec(1, "removed"), rec(2, "removed")])
    assert (m.sentence["tp"], m.sentence["fn"]) == (1, 1)
    half = gold.score_passages([Pas("removed", "it1", 140, 400)], [rec(1, "removed")])      # exactly 40 of 80 inside
    assert half.sentence["tp"] == 1
    trimmed = gold.score_passages([Pas("removed", "it1", 105, 175)], [rec(1, "removed")])   # whitespace-trimmed splitter
    assert trimmed.sentence["tp"] == 1


def test_a_passage_of_another_item_never_covers_a_gold_sentence_even_at_the_same_offsets():
    m = gold.score_passages([Pas("removed", "other", 100, 180)], [rec(1, "removed")])
    assert m.sentence["tp"] == 0 and m.sentence["fn"] == 1 and m.ignored_passages == 1


def test_passages_outside_the_sampled_items_and_of_the_other_direction_are_ignored():
    passages = [Pas("removed", "not-sampled", 0, 500), Pas("added", "it1", 100, 180), Pas("removed", "it1", 100, 180)]
    m = gold.score_passages(passages, GOLD10)
    assert m.ignored_passages == 2 and m.sentence["tp"] == 1 and m.passage["tp"] == 1


def test_newer_side_scores_added_passages_as_the_positive_class():
    gold_new = [rec(0, "added", item="n1"), rec(1, "present", item="n1"), rec(2, "reworded", item="n1")]
    passages = [Pas("added", "n1", 0, 80), Pas("removed", "n1", 100, 180), Pas("reworded", "n1", 200, 280)]
    m = gold.score_passages(passages, gold_new, side="newer")
    assert m.positive_kind == "added" and (m.sentence["tp"], m.sentence["fp"], m.sentence["fn"]) == (1, 0, 0)
    assert m.ignored_passages == 1 and m.confusion["reworded"] == {"reworded": 1}
    with pytest.raises(ValueError, match="side"):
        gold.score_passages([], gold_new, side="both")


def test_uncertain_passages_are_reported_as_a_rate_and_are_not_counted_as_drops_or_as_wrong():
    m = gold.score_passages([Pas("uncertain", "it1", 100, 180), Pas("removed", "it1", 200, 280)], GOLD10)
    assert m.uncertain == 1 and m.uncertain_rate == pytest.approx(0.1)
    assert m.sentence["tp"] == 1 and m.sentence["fp"] == 0
    assert m.sentence["fn"] == 3 and m.false_negative_ids == ["it1#s001", "it1#s005", "it1#s008"]
    assert m.confusion["removed"] == {"uncertain": 1, "removed": 1, "present": 2}


def test_uncertain_gold_positives_count_against_recall_exactly_like_score_predictions():
    m = gold.score_passages([Pas("uncertain", "it1", 100, 180)], [rec(1, "removed")])
    assert m.sentence["recall"] == 0.0 and m.sentence["precision"] is None and m.uncertain == 1
    assert m.false_negative_ids == ["it1#s001"]


def test_passage_precision_uses_the_majority_of_the_gold_sentences_it_covers():
    gold3 = [rec(0, "removed"), rec(1, "removed"), rec(2, "present")]
    good = gold.score_passages([Pas("removed", "it1", 0, 280)], gold3)                # 2 of 3 removed: correct
    assert good.passage["tp"] == 1 and good.passage["fp"] == 0
    tie = gold.score_passages([Pas("removed", "it1", 0, 180)], [rec(0, "removed"), rec(1, "present")])
    assert tie.passage["tp"] == 0 and tie.passage["fp"] == 1                          # 1 of 2 is not a majority


def test_a_passage_covering_no_gold_sentence_is_unverifiable_and_left_out_of_precision():
    m = gold.score_passages([Pas("removed", "it1", 5000, 5100)], GOLD10)
    assert m.passage["unverifiable"] == 1 and m.passage["precision"] is None and m.passage["tp"] == 0


def test_passage_recall_counts_a_gold_run_found_when_most_of_it_is_covered_even_by_split_passages():
    run = [rec(n, "removed") for n in range(4)] + [rec(4, "present")]
    split = gold.score_passages([Pas("removed", "it1", 0, 180), Pas("removed", "it1", 200, 380)], run)
    assert (split.passage["n_gold_runs"], split.passage["recalled"]) == (1, 1) and split.passage["recall"] == 1.0
    minority = gold.score_passages([Pas("removed", "it1", 0, 180)], run)              # 2 of 4 is not a majority
    assert minority.passage["recalled"] == 0 and minority.passage["recall"] == 0.0
    assert minority.sentence["recall"] == pytest.approx(0.5)


def test_a_present_sentence_between_two_removed_ones_makes_two_gold_runs():
    g = [rec(0, "removed"), rec(1, "present"), rec(2, "removed")]
    m = gold.score_passages([Pas("removed", "it1", 0, 80)], g)
    assert m.passage["n_gold_runs"] == 2 and m.passage["recalled"] == 1 and m.passage["recall"] == 0.5


def test_dict_passages_and_duck_typed_objects_score_the_same_as_dataclasses():
    as_dicts = [{"kind": "removed", "item_id": "it1", "char_start": 100, "char_end": 180, "text": "x"}]
    as_ns = [SimpleNamespace(kind="removed", item_id="it1", char_start=100, char_end=180, text="x")]
    ref = gold.score_passages([Pas("removed", "it1", 100, 180)], GOLD10)
    assert gold.score_passages(as_dicts, GOLD10) == ref and gold.score_passages(as_ns, GOLD10) == ref
    with pytest.raises(ValueError, match="char_end"):
        gold.score_passages([{"kind": "removed", "item_id": "it1", "char_start": 1}], GOLD10)


def test_empty_inputs_give_undefined_precision_zero_recall_and_no_crash():
    none = gold.score_passages([], GOLD10)
    assert none.sentence["precision"] is None and none.sentence["recall"] == 0.0 and none.passage["precision"] is None
    assert none.passage["recall"] == 0.0 and none.false_negative_ids == ["it1#s001", "it1#s002", "it1#s005", "it1#s008"]
    empty = gold.score_passages([], [])
    assert empty.n_gold_sentences == 0 and empty.uncertain_rate == 0.0
    assert empty.sentence["precision"] is None and empty.sentence["recall"] is None and empty.passage["recall"] is None
    no_positive = gold.score_passages([Pas("removed", "it1", 0, 80)], [rec(0, "present")])
    assert no_positive.sentence["recall"] is None and no_positive.sentence["precision"] == 0.0


def test_gold_sentences_without_section_offsets_cannot_be_scored():
    bad = {"sentence_id": "it1#s000", "item_id": "it1", "section_start": None, "section_end": None, "label": "removed"}
    with pytest.raises(ValueError, match="offsets"):
        gold.score_passages([], [bad])
    with pytest.raises(ValueError, match="label"):
        gold.score_passages([], [{**bad, "section_start": 0, "section_end": 5, "label": "maybe"}])


def test_metrics_serialise_to_plain_json():
    m = gold.score_passages([Pas("removed", "it1", 100, 180)], GOLD10)
    d = m.to_dict()
    assert json.loads(json.dumps(d)) == d and d["sentence"]["tp"] == 1


# --- named must-hit needles --------------------------------------------------------------------------------------

def test_must_hit_reports_which_needles_a_removed_passage_covers():
    passages = [Pas("removed", "it1", 0, 10, text="...the Notified Advanced Computing, or NAC, process has not resulted in approvals..."),
                Pas("reworded", "it1", 0, 10, text="the sentence about Hong Kong is only reworded"),
                {"kind": "removed", "text": "we transitioned some operations out of China\nand Hong Kong"}]
    hits = gold.must_hit(passages, ["Notified Advanced Computing", "out of China and Hong Kong", "AI Diffusion", "Hong Kong is only"])
    assert hits == {"Notified Advanced Computing": True, "out of China and Hong Kong": True,
                    "AI Diffusion": False, "Hong Kong is only": False}


def test_must_hit_kind_is_configurable_and_empty_inputs_are_fine():
    added = [Pas("added", "n", 0, 5, text="April 2025 H20 licensing")]
    assert gold.must_hit(added, ["H20 licensing"], kind="added") == {"H20 licensing": True}
    assert gold.must_hit(added, ["H20 licensing"]) == {"H20 licensing": False}
    assert gold.must_hit([], ["x"]) == {"x": False} and gold.must_hit([], []) == {}


# --- eligibility by votes: a disputed-but-present item stays sampleable -------------------------------------------

def test_a_three_way_split_among_present_labels_keeps_the_largest_item_as_the_flagship():
    items, consensus = make_pair_items()
    del consensus["a:I.1A:i09"]                                        # no strict majority among reworded/merged/unchanged
    votes = {"a:I.1A:i09": {"a": "reworded", "b": "merged", "c": "unchanged"}}
    without = gold.sentence_sample(items, consensus, pair_id="P", k=99)
    assert "a:I.1A:i09" not in without["eligible_item_ids"] and without["flagship_item_id"] == "a:I.1A:i08"
    sample = gold.sentence_sample(items, consensus, pair_id="P", item_votes=votes, k=3)
    assert sample["flagship_item_id"] == "a:I.1A:i09" and sample["flagship_note"] is None
    assert sample["eligibility_basis"]["a:I.1A:i09"] == "vote_majority"
    assert sample["eligibility_basis"]["a:I.1A:i00"] == "consensus"
    assert "a:I.1A:i09" in [i["item_id"] for i in sample["items"]]


def test_vote_eligibility_needs_a_strict_majority_of_present_votes_and_never_overrules_a_consensus():
    items, consensus = make_pair_items()
    for item_id in ("a:I.1A:i09", "a:I.1A:i08", "a:I.1A:i07"):
        del consensus[item_id]
    votes = {"a:I.1A:i09": {"a": "removed", "b": "unchanged"},                     # 1 of 2: not a strict majority
             "a:I.1A:i08": {"a": "removed", "b": "reworded", "c": "merged"},       # 2 of 3 say still present
             "a:I.1A:i07": ["removed", "removed", "unchanged"],                     # a plain list of ballots also works
             "a:I.1A:i06": {"a": "removed", "b": "removed", "c": "removed"}}       # has a consensus (unchanged): votes ignored
    sample = gold.sentence_sample(items, consensus, pair_id="P", item_votes=votes, k=99)
    assert "a:I.1A:i09" not in sample["eligible_item_ids"] and "a:I.1A:i07" not in sample["eligible_item_ids"]
    assert sample["eligibility_basis"]["a:I.1A:i08"] == "vote_majority" and sample["flagship_item_id"] == "a:I.1A:i08"
    assert sample["eligibility_basis"]["a:I.1A:i06"] == "consensus"
    consensus["a:I.1A:i06"] = "removed"
    assert "a:I.1A:i06" not in gold.sentence_sample(items, consensus, pair_id="P", item_votes=votes, k=99)["eligible_item_ids"]


def test_the_manifest_lists_every_eligible_item_so_the_draw_is_reproducible_from_it():
    items, consensus = make_pair_items()
    sample = gold.sentence_sample(items, consensus, pair_id="P", seed=4, k=4)
    assert sample["eligible_item_ids"] == sorted(i["item_id"] for i in items) and sample["n_eligible_items"] == 10
    pool = [i for i in sample["eligible_item_ids"] if i != sample["flagship_item_id"]]
    redraw = random.Random(sample["rng_key"]).sample(pool, sample["k"] - 1)
    assert sorted(redraw + [sample["flagship_item_id"]]) == [i["item_id"] for i in sample["items"]]
