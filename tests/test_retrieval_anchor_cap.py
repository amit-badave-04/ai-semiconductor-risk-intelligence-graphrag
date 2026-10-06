"""The anchor cap (M5a, docs/v2/M5A_BUILD_PLAN.md step 0-I4): naming many companies must not grow the prompt.

Every per-company block of the context (rules, metrics, the temporal block, the active-risk search) scales with the
number of anchors, and a 500-character question can name all 26 detectable companies. ``retriever.MAX_ANCHORS`` keeps
the FIRST ``MAX_ANCHORS`` companies in detection order (the order of the canonical entity dictionary, not the order
the question names them) and says so, in the retrieval dict (``anchors_dropped``) and in the context the writer reads.

The fake driver here answers every query with rows built from the ids it is given, so a bigger id list gives a bigger
context: a cap that did not bite would show up as a bigger prompt, and a query that still received a dropped company
would show up in the recorded parameters. No Neo4j and no network.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from semigraph.artifacts import load_benchmark, load_canonical_entities, load_examples
from semigraph.retrieval import answerer, context_layout
from semigraph.retrieval import retriever as R

ROOT = Path(__file__).resolve().parents[1]

# The detection order, written out here (the canonical dictionary's order: the 13 SEC filers first, then Samsung, then
# the companies that have no filings).
CANONICAL_ORDER = ["Nvidia", "AMD", "Intel", "Broadcom", "Qualcomm", "TSMC", "ASML", "Micron", "Samsung", "Apple",
                   "Microsoft", "Amazon", "Alphabet", "Meta", "SK Hynix", "Arm", "OpenAI", "Anthropic", "Marvell",
                   "GlobalFoundries", "Foxconn", "CoreWeave", "xAI", "Oracle", "Tesla", "Huawei"]
IDS = {name: spec["entity_id"] for name, spec in load_canonical_entities().items()}
NAME_OF = {eid: name for name, eid in IDS.items()}
THIRTEEN_FILERS = ["Nvidia", "AMD", "Intel", "Broadcom", "Qualcomm", "TSMC", "ASML", "Micron", "Apple", "Microsoft",
                   "Amazon", "Alphabet", "Meta"]
FIXED_QUESTION = "Compare the supply chain exposure of the companies named."      # the same text in every prompt below
NOTE_PREFIX = "Note for this question: it names more companies than this answer covers; not covered: "
OLD, NEW = "0001045810-25-000023", "0001045810-26-000021"


def question_naming(names: list[str]) -> str:
    return "Compare the supply chain exposure of " + ", ".join(names) + "."


Q_ALL_FILERS = question_naming(THIRTEEN_FILERS)
Q_NAMED_YEARS = ("Did Nvidia, AMD, Intel, Broadcom and Qualcomm remove any risk factor between their FY2019 and FY2020 "
                 "annual reports?")


def note_section(dropped: list[str]) -> str:
    """The section the note adds to the temporal block, as it appears in the prompt (sections: a blank line apart)."""
    return "\n\n" + NOTE_PREFIX + ", ".join(dropped) + "."


# --- a driver whose answers scale with the ids it is given --------------------------------------------------------

class _Session:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        kind = self.driver.classify(query)
        self.driver.calls.append((kind, params))
        return getattr(self.driver, "rows_" + kind)(params)


class CikDriver:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.table = {R.company_edges_query(2): "edges", R.RULE_EDGES_QUERY: "rules", R.METRICS_QUERY: "metrics",
                      R.ACTIVE_RISKS_QUERY: "risks", R.TEMPORAL_QUERY: "temporal", R.PASSAGES_QUERY: "passages",
                      R.EXCERPTS_QUERY: "excerpts", R.ANNUAL_PAIRS_QUERY: "pairs",
                      R.TEMPORAL_SELECTED_QUERY: "temporal_selected"}

    def session(self, **kw):
        return _Session(self)

    def classify(self, query):
        return self.table.get(query, "other")

    def params_of(self, kind):
        return [p for k, p in self.calls if k == kind]

    @staticmethod
    def rows_edges(p):
        return [{"source": NAME_OF[i], "relation": "COMPETES_WITH", "target": "Zeta", "status": "Active", "quote": "q",
                 "chunk_ids": [f"{abs(i):010d}-26-000001:I.1:0001"]} for i in p["ids"]]

    @staticmethod
    def rows_rules(p):
        return [{"source": NAME_OF[i], "relation": "AFFECTED_BY", "target": f"Rule {abs(i)} for {NAME_OF[i]}",
                 "status": "Active", "quote": "q", "chunk_ids": [], "rule_id": f"2024-{abs(i) % 100000:05d}",
                 "date": "2024-01-02", "url": "u", "kind": "rule", "link_source": "federal_register",
                 "link_method": "keyword", "external": True} for i in p["ids"]]

    @staticmethod
    def rows_metrics(p):
        return [{"cik": i, "company": NAME_OF[i], "metric": "revenue", "value": 1e9, "unit": "USD",
                 "period_start": "2024-01-01", "period_end": "2024-12-31"} for i in p["ids"]]

    @staticmethod
    def rows_risks(p):
        return [{"company": NAME_OF[p["cik"]], "summary": "s", "category": "c", "score": 0.5,
                 "chunk_id": f"{abs(p['cik']):010d}-26-000001:I.1A:0001"}]

    @staticmethod
    def rows_pairs(p):
        return [{"company": NAME_OF[i], "cik": i, "older_accession": OLD, "newer_accession": NEW, "older_form": "10-K",
                 "older_date": "2025-02-26", "newer_form": "10-K", "newer_date": "2026-02-25", "compared": True,
                 "not_compared_reason": None, "is_current": True, "older_period_end": "2024-01-28", "older_fy": 2024,
                 "newer_period_end": "2025-01-26", "newer_fy": 2025, "newer_has_items": True, "older_has_items": True}
                for i in p["ids"]]

    @staticmethod
    def rows_temporal(p):
        rows = []
        for i in p["ids"]:
            common = {"company": NAME_OF[i], "cik": i, "older_accession": OLD, "newer_accession": NEW,
                      "older_form": "10-K", "older_date": "2025-02-26", "newer_form": "10-K",
                      "newer_date": "2026-02-25"}
            blank = dict.fromkeys(("item_id", "headline", "older_headline", "unit_kind", "section_id", "seq", "length",
                                   "decided_by", "sim_embed", "sim_lex", "lineage", "lead_text"))
            rows.append({**blank, "older_chunk_ids": [], "newer_chunk_ids": [], **common, "change": "pair"})
            rows.append({**blank, **common, "change": "removed", "item_id": f"{OLD}:I.1A:{abs(i)}", "seq": 1,
                         "headline": f"Risk of {NAME_OF[i]}", "unit_kind": "headline", "section_id": "I.1A",
                         "length": 900, "older_chunk_ids": [f"{abs(i):010d}-25-000023:I.1A:0001"],
                         "newer_chunk_ids": []})
        return rows

    rows_temporal_selected = rows_temporal

    @staticmethod
    def rows_passages(p):
        return []

    @staticmethod
    def rows_excerpts(p):
        return [{"chunk_id": f"0000000001-26-00000{n}:I.1:0001", "score": 0.9, "text": "text", "source_url": "u"}
                for n in range(2)]

    @staticmethod
    def rows_other(p):
        return []


class Embedder:
    def encode_query(self, question):
        return [0.1, 0.2]


def retrieve(question: str) -> tuple[dict, CikDriver]:
    driver = CikDriver()
    return R.hybrid_retrieve(question, driver, Embedder()), driver


def prompt_of(r: dict) -> str:
    blocks, _, _ = answerer.build_blocks(r)
    return answerer.render_prompt(FIXED_QUESTION, blocks)


# --- the constant and the pure function ---------------------------------------------------------------------------

def test_max_anchors_is_four_which_is_twice_the_most_any_recorded_question_names():
    assert R.MAX_ANCHORS == 4


def _question_pools() -> dict[str, list[str]]:
    pools = {"benchmark": [row["q"] for row in load_benchmark()],
             "examples": [row["question"] for row in load_examples()["examples"]]}
    agent = ROOT / "artifacts" / "agent_benchmark.json"
    if agent.exists():
        pools["agent_benchmark"] = [row["q"] for row in json.loads(agent.read_text(encoding="utf-8"))["questions"]]
    return pools


def test_no_recorded_question_is_capped_so_the_replay_fixtures_and_benchmarks_never_see_the_cap():
    pools = _question_pools()
    assert len(pools) >= 2
    observed = {name: max(len(R.detect_anchors(q)) for q in qs) for name, qs in pools.items()}
    assert max(observed.values()) == 2, observed          # the figure the cap's margin is measured against
    assert R.MAX_ANCHORS >= 2 * max(observed.values())


def test_cap_anchors_keeps_the_first_in_detection_order_and_names_the_rest_in_order():
    anchors = {name: IDS[name] for name in CANONICAL_ORDER[:6]}
    kept, dropped = R.cap_anchors(anchors)
    assert kept == {name: IDS[name] for name in CANONICAL_ORDER[:R.MAX_ANCHORS]}
    assert list(kept) == CANONICAL_ORDER[:R.MAX_ANCHORS]
    assert dropped == CANONICAL_ORDER[R.MAX_ANCHORS:6]
    assert list(anchors) == CANONICAL_ORDER[:6]                  # the input is not touched


@pytest.mark.parametrize("n", range(5))
def test_cap_anchors_leaves_a_question_at_or_under_the_cap_exactly_as_it_was(n):
    anchors = {name: IDS[name] for name in CANONICAL_ORDER[:n]}
    kept, dropped = R.cap_anchors(anchors)
    assert kept == anchors and dropped == []


# --- detection order ----------------------------------------------------------------------------------------------

def test_detection_order_is_the_canonical_dictionary_order_not_the_order_the_question_names_them():
    assert list(load_canonical_entities()) == CANONICAL_ORDER
    reversed_question = question_naming(list(reversed(THIRTEEN_FILERS)))
    assert list(R.detect_anchors(reversed_question)) == THIRTEEN_FILERS
    assert list(R.detect_anchors(question_naming(["Huawei", "Samsung", "Micron", "Nvidia"]))) == [
        "Nvidia", "Micron", "Samsung", "Huawei"]


def test_detection_order_does_not_depend_on_the_hash_seed():
    """Each company's aliases are a SET, so the order they are tried in varies per process; the order of the companies
    must not."""
    question = Q_ALL_FILERS + " Also Huawei, Samsung and Anthropic."
    code = ("from semigraph.retrieval.retriever import detect_anchors; "
            f"print(list(detect_anchors({question!r})))")
    outputs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT,
                              env={**os.environ, "PYTHONHASHSEED": seed}).stdout for seed in ("1", "2")}
    assert len(outputs) == 1


# --- hybrid_retrieve: the cap bites -------------------------------------------------------------------------------

def test_a_question_naming_every_filer_keeps_the_first_max_anchors_and_lists_the_rest_in_order():
    r, _ = retrieve(Q_ALL_FILERS)
    assert len(R.detect_anchors(Q_ALL_FILERS)) == 13
    assert list(r["anchors"]) == THIRTEEN_FILERS[:R.MAX_ANCHORS]
    assert r["anchors"] == {name: IDS[name] for name in THIRTEEN_FILERS[:R.MAX_ANCHORS]}
    assert r["anchors_dropped"] == THIRTEEN_FILERS[R.MAX_ANCHORS:]
    assert r["anchor_defaulted"] is False


@pytest.mark.parametrize("question", [Q_ALL_FILERS, Q_NAMED_YEARS])
def test_every_query_gets_only_the_kept_ids_and_the_risk_search_runs_once_per_kept_anchor(question):
    r, driver = retrieve(question)
    kept = [name for name in CANONICAL_ORDER if name in R.detect_anchors(question)][:R.MAX_ANCHORS]
    kept_ids = [IDS[name] for name in kept]
    id_queries = [(kind, p) for kind, p in driver.calls if "ids" in p]
    assert {kind for kind, _ in id_queries} >= {"edges", "rules", "metrics", "excerpts"}
    assert all(p["ids"] == kept_ids for _, p in id_queries), id_queries
    risks = driver.params_of("risks")
    assert [p["cik"] for p in risks] == kept_ids and len(risks) == R.MAX_ANCHORS
    assert {row["company"] for row in r["metrics"]} == set(kept)
    assert list(r["anchors"]) == kept


def test_a_question_naming_more_than_the_cap_gives_the_prompt_of_one_naming_exactly_the_cap_plus_the_note():
    """Non-vacuous: the driver's rows scale with the ids it gets, and the dropped companies are not in the context."""
    capped, _ = retrieve(Q_ALL_FILERS)
    exact, _ = retrieve(question_naming(THIRTEEN_FILERS[:R.MAX_ANCHORS]))
    capped_prompt, exact_prompt = prompt_of(capped), prompt_of(exact)
    assert "anchors_dropped" not in exact
    note = note_section(THIRTEEN_FILERS[R.MAX_ANCHORS:])
    assert len(capped_prompt) == len(exact_prompt) + len(note)
    assert capped_prompt.replace(note, "") == exact_prompt
    for name in THIRTEEN_FILERS[R.MAX_ANCHORS:]:
        assert f"Rule {abs(IDS[name])} for {name}" not in capped_prompt and f"Risk of {name}" not in capped_prompt


@pytest.mark.parametrize("n", [R.MAX_ANCHORS + 1, 9, 13])
def test_the_prompt_grows_only_by_the_note_however_many_more_companies_the_question_names(n):
    base = len(prompt_of(retrieve(question_naming(THIRTEEN_FILERS[:R.MAX_ANCHORS]))[0]))
    r, _ = retrieve(question_naming(THIRTEEN_FILERS[:n]))
    assert len(prompt_of(r)) == base + len(note_section(THIRTEEN_FILERS[R.MAX_ANCHORS:n]))


def test_a_company_without_filings_that_the_dictionary_lists_early_takes_a_slot_ahead_of_later_filers():
    """Detection order is the dictionary's order, not "filers first": Samsung (no filings) is listed before Apple."""
    r, _ = retrieve(question_naming(["Huawei", "Samsung", "Tesla", "Micron", "Apple", "Nvidia", "Intel"]))
    assert list(r["anchors"]) == ["Nvidia", "Intel", "Micron", "Samsung"]
    assert r["anchors_dropped"] == ["Apple", "Tesla", "Huawei"]


# --- the question that is not capped is exactly what it was -------------------------------------------------------

@pytest.mark.parametrize("names", [[], ["Nvidia"], ["Nvidia", "AMD"], ["Nvidia", "AMD", "Intel", "Broadcom"]])
def test_a_question_at_or_under_the_cap_gets_no_dropped_key_and_no_note(names):
    question = question_naming(names) if names else "What risks matter most for the AI supply chain this year?"
    r, driver = retrieve(question)
    assert "anchors_dropped" not in r
    assert r["temporal_notices"] == []
    assert "Note for this question" not in prompt_of(r)
    assert list(r["anchors"]) == names
    if not names:
        assert r["anchor_defaulted"] is True and r["anchors"] == {}
        assert driver.params_of("edges")[0]["ids"] == [R.DEFAULT_ANCHOR_CIK]


def test_the_result_keys_of_an_uncapped_question_are_the_ones_the_old_shape_test_pins():
    r, _ = retrieve("How does Nvidia depend on TSMC?")
    assert set(r) == {"anchors", "edges", "metrics", "metric_periods", "risks", "temporal", "temporal_pairs",
                      "temporal_passages", "temporal_notices", "chunks", "anchor_defaulted"}


# --- the note reaches the writer and cannot be mistaken for a finding ---------------------------------------------

def test_the_note_is_a_note_for_line_in_the_temporal_block_the_prompt_already_tells_the_writer_to_state():
    r, _ = retrieve(Q_ALL_FILERS)
    (notice,) = r["temporal_notices"]
    assert notice["cik"] is None and notice["company"] == "this question"
    blocks, context, _ = answerer.build_blocks(r)
    assert blocks.temporal_block.splitlines()[-1] == note_section(THIRTEEN_FILERS[R.MAX_ANCHORS:]).strip()
    assert context.count("Note for this question:") == 1
    assert 'carries a "Note for <company>:" line' in " ".join(answerer.ANSWER_PROMPT.split())


def test_the_note_supports_no_removal_claim_and_adds_no_citable_id():
    r, _ = retrieve(Q_ALL_FILERS)
    _, context, valid = answerer.build_blocks(r)
    _, bare_context, bare_valid = answerer.build_blocks({**r, "temporal_notices": []})
    assert valid == bare_valid
    assert context_layout.removal_supported_ids(context) == context_layout.removal_supported_ids(bare_context)


def test_a_planner_tool_call_about_a_kept_company_never_drops_the_note():
    """KNOWN GAP, not asserted here: a tool call that adds a company the cap DROPPED (the planner reads the whole
    question) leaves that name in ``anchors_dropped`` and in the note, which would then say "not covered" about a
    company whose blocks the tool added. The fix belongs in ``agent/merge.add_anchors`` (not part of this change)."""
    from semigraph.agent import merge
    r, _ = retrieve(Q_ALL_FILERS)
    other = {"cik": IDS["Intel"], "company": "Intel", "text": "latest comparison shown"}
    merged = merge.merge_temporal(r, items=[], pairs=[], passages=[], notices=[other])
    assert [n["company"] for n in merged["temporal_notices"]] == ["this question", "Intel"]
    kept = merge.add_anchors(merged, {"Intel": IDS["Intel"]})
    assert kept["anchors_dropped"] == r["anchors_dropped"] and list(kept["anchors"]) == list(r["anchors"])


def test_the_note_comes_after_the_pair_notices_of_the_kept_companies():
    r, _ = retrieve(Q_NAMED_YEARS)
    assert r["anchors_dropped"] == ["Qualcomm"]
    assert [n["company"] for n in r["temporal_notices"]] == ["Nvidia", "AMD", "Intel", "Broadcom", "this question"]
    assert r["temporal_notices"][-1]["text"] == "it names more companies than this answer covers; not covered: Qualcomm"


def test_the_retrieval_dict_is_plain_data_the_event_stream_can_serialise():
    r, _ = retrieve(Q_ALL_FILERS)
    assert json.loads(json.dumps(r["anchors_dropped"])) == THIRTEEN_FILERS[R.MAX_ANCHORS:]
