"""I3a (docs/specs/m2-search.md R8, R9, S3, S7, S9): the bm25s keyword index, its tokenizer, its query caps, the loader and
the hydration step. No database: the loader and the hydration run on a fake driver."""

import re
import sys

import pytest

from semigraph.retrieval import keyword as K

# --- tokenizer ---

@pytest.mark.parametrize("text,expected", [
    ("3A090", ["3a090"]),
    ("ECCN 3A090.b", ["eccn", "3a090", "b"]),
    ("Form 10-K and 10-Q", ["form", "10-k", "10-q"]),
    ("H20 vs H200", ["h20", "vs", "h200"]),
    ("CoWoS-L packaging", ["cowos-l", "packaging"]),
    ("the export of chips to China", ["export", "chips", "china"]),     # the tiny fixed stopword list
    ("ＮＶＩＤＩＡ ﬁnance", ["nvidia", "finance"]),                             # NFKC: full width, ligature
    ("naïve café", ["naïve", "café"]),
    ("snake_case and -dash- and --", ["snake", "case", "dash"]),
    ("", []),
    ("   \t\n", []),
])
def test_tokenizer_cases(text, expected):
    assert K.tokenize(text) == expected


def test_tokenizer_drops_a_token_longer_than_the_term_cap():
    assert K.tokenize("a" * (K.MAX_TERM_CHARS + 1) + " ok") == ["ok"]
    assert K.tokenize("a" * K.MAX_TERM_CHARS) == ["a" * K.MAX_TERM_CHARS]


def test_tokenizer_is_versioned_and_deterministic():
    assert isinstance(K.TOKENIZER_VERSION, str) and K.TOKENIZER_VERSION
    assert K.tokenize("Export Controls, 3A090!") == K.tokenize("export controls 3a090")


# --- query handling (S3) ---

def row(cid, text, *, filer=1, form="10-K", acc=None, digest=None, valid_from="2025-02-26", current=True, mentions=(),
        context=""):
    return {"chunk_id": cid, "text": text, "filer_cik": filer, "form": form, "accession_no": acc or f"A-{filer}",
            "content_hash": digest or f"h-{cid}", "valid_from": valid_from, "is_current": current,
            "mention_ciks": list(mentions), "context": context}


CORPUS = [
    row("c1", "ECCN 3A090 controls advanced accelerators such as the H20 and H200", filer=1),
    row("c2", "CoWoS advanced packaging capacity is constrained", filer=2, mentions=[1]),
    row("c3", "Our annual report on Form 10-K describes export controls", filer=1),
    row("c4", "Memory pricing and HBM supply", filer=3, mentions=[1, 2]),
]


@pytest.fixture(scope="module")
def index():
    return K.build_index(CORPUS, snapshot_id="snap-1")


def ids(hits):
    return [h.chunk_id for h in hits]


@pytest.mark.parametrize("query,expected_first", [
    ("3A090", "c1"), ("what does 3a090 say", "c1"), ("H200", "c1"), ("10-K", "c3"), ("ＣｏＷｏＳ", "c2"),
    ("HBM", "c4"), ("export controls on accelerators", "c3"),
])
def test_keyword_query_cases(index, query, expected_first):
    hits = index.search(query)
    assert hits and hits[0].chunk_id == expected_first
    assert all(h.score > 0 for h in hits)


@pytest.mark.parametrize("query", ["", "   ", "*:*", "!!!???...,,,;;;", "\x00\x01\x1b", "-- -- --", "zzzzunknownterm", None, 42])
def test_empty_and_degenerate_queries_give_an_empty_result_never_an_error(index, query):
    assert index.search(query) == []
    assert index.search_legs(query, [1, 2]) == {f"keyword:{1}": [], f"keyword:{2}": [], "keyword:mention": []}


def test_a_ten_thousand_character_question_is_capped_not_refused(index):
    huge = "export " + "lorem ipsum dolor " * 700 + " 3a090"
    assert len(huge) > 10_000
    terms = K.query_terms(huge)
    assert len(terms) <= K.MAX_QUERY_TERMS
    assert index.search(huge)[:1]                    # still answers


def test_query_caps_terms_term_length_and_repeats():
    assert len(K.query_terms(" ".join(f"t{i}" for i in range(500)))) == K.MAX_QUERY_TERMS
    assert K.query_terms("h20 " * 5000) == ["h20"]
    assert K.query_terms("x" * 10_000) == []
    assert K.query_terms("export " * 3 + "controls") == ["export", "controls"]


def test_control_characters_mixed_scripts_and_syntax_do_not_widen_or_raise(index):
    assert ids(index.search("\x00export\x1b\x7f")) == ids(index.search("export"))
    assert index.search("*:* OR 3a090 AND NOT x")[:1] and index.search("пример 例 3A090")[0].chunk_id == "c1"
    assert ids(index.search('3a090" OR chunk_id:*')) == ids(index.search("3a090 or chunk_id"))      # no operators, just words


# --- search legs and masks (S7: scores computed once, masked per leg) ---

def test_filer_mask_and_mention_mask(index):
    legs = index.search_legs("advanced", [1, 2])
    assert ids(legs["keyword:1"]) == ["c1"] and ids(legs["keyword:2"]) == ["c2"]
    assert ids(legs["keyword:mention"]) == ["c2"]             # c1 is filer 1 and mentions nobody; c2 mentions anchor 1
    assert all(h.filer_cik == 2 for h in legs["keyword:2"])
    assert ids(index.search("hbm supply", mention_ciks=[2])) == ["c4"]
    assert index.search("hbm supply", mention_ciks=[99]) == []


def test_current_only_drops_superseded_rows():
    rows = [row("old", "export controls export controls", current=False), row("new", "export controls")]
    idx = K.build_index(rows)
    assert ids(idx.search("export controls")) == ["old", "new"]
    assert ids(idx.search("export controls", current_only=True)) == ["new"]
    assert ids(idx.search_legs("export controls", [1], current_only=True)["keyword:1"]) == ["new"]


def test_depth_and_deterministic_ties():
    rows = [row(f"c{i:03d}", "export controls") for i in range(80)]
    idx = K.build_index(list(reversed(rows)))
    hits = idx.search("export controls", depth=50)
    assert len(hits) == 50 and ids(hits) == [f"c{i:03d}" for i in range(50)]     # equal scores: chunk id decides


def test_hits_carry_metadata_only_never_text(index):
    assert "text" not in K.Hit._fields and "context" not in K.Hit._fields
    h = index.search("3a090")[0]
    assert (h.chunk_id, h.filer_cik, h.form, h.accession_no, h.content_hash, h.is_current, h.valid_from) == (
        "c1", 1, "10-K", "A-1", "h-c1", True, "2025-02-26")
    assert not any(isinstance(v, str) and "ECCN" in v for v in h)
    assert index.snapshot_id == "snap-1" and index.size == len(CORPUS) and index.with_context is True


def test_empty_corpus_and_rows_without_text_are_safe():
    assert K.build_index([]).search("export") == []
    idx = K.build_index([row("e", ""), row("f", "export")])
    assert ids(idx.search("export")) == ["f"]


def test_bad_row_shape_is_refused_at_build():
    with pytest.raises(ValueError):
        K.build_index([{"chunk_id": "x"}])


# --- the context field (OD4) ---

def test_the_context_field_is_indexed_but_never_returned():
    rows = [row("c1", "We depend on a single foundry.", context="NVIDIA NVDA 10-K 2025 I.1A Risk Factors"),
            row("c2", "Other text about packaging.", filer=2, context="Intel INTC 10-K 2025")]
    with_ctx, without = K.build_index(rows, with_context=True), K.build_index(rows, with_context=False)
    assert ids(with_ctx.search("nvda")) == ["c1"] and without.search("nvda") == []
    assert with_ctx.with_context and not without.with_context
    for hit in with_ctx.search("nvidia 2025"):
        assert "NVIDIA" not in repr(hit) and "Risk Factors" not in repr(hit)


def test_context_field_composition():
    got = K.context_field(company="NVIDIA Corporation", ticker="NVDA", form="10-K", filing_date="2025-02-26",
                          section_id="I.1A", sub_heading="Supply chain risks")
    assert got == "NVIDIA Corporation NVDA 10-K 2025 I.1A Supply chain risks"
    assert K.context_field(company=None, ticker=None, form="10-Q", filing_date=None, section_id=None, sub_heading=None) == "10-Q"


# --- the loader: one parameterised read query ---

class RowsDriver:
    """session().run(query, **params) over a fixed answer; records every query."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def session(self, **kw):
        driver = self

        class S:
            def __enter__(s):
                return s

            def __exit__(s, *e):
                return False

            def run(s, query, **params):
                driver.calls.append((query, params))
                rows = driver.answer(params) if callable(driver.answer) else driver.answer
                return [dict(r) for r in rows]
        return S()


def db_row(cid, **over):
    base = {"chunk_id": cid, "text": f"text of {cid}", "filer_cik": 1045810, "form": "10-K", "accession_no": "0001-25-1",
            "content_hash": f"h-{cid}", "valid_from": "2025-02-26", "is_current": True, "section_id": "I.1A",
            "sub_heading": "Risks", "filing_date": "2025-02-26", "company": "NVIDIA", "ticker": "NVDA",
            "mention_ciks": [1045810, 1046]}
    return {**base, **over}


def test_load_rows_runs_one_parameterised_read_query_and_builds_the_context():
    d = RowsDriver(lambda p: [db_row("c1"), db_row("c2", company=None, ticker=None, sub_heading=None)][:p["limit"]])
    rows = K.load_rows(d)
    assert len(d.calls) == 1
    query, params = d.calls[0]
    assert query is K.LOAD_ROWS_QUERY and params == {"limit": K.MAX_INDEX_ROWS + 1}
    assert "retrievable = true" in query and "$limit" in query and "%s" not in query
    assert not re.search(r"\b(SET|MERGE|CREATE|DELETE|REMOVE)\b", query)
    assert [r["chunk_id"] for r in rows] == ["c1", "c2"]
    assert rows[0]["context"] == "NVIDIA NVDA 10-K 2025 I.1A Risks"
    assert rows[1]["context"] == "10-K 2025 I.1A"
    idx = K.build_index(rows)
    assert ids(idx.search("nvda")) == ["c1"] and idx.search("text")[0].filer_cik == 1045810


def test_load_rows_refuses_a_row_count_over_the_bound():
    graph = [db_row(f"c{i}") for i in range(3)]
    d = RowsDriver(lambda p: graph[:p["limit"]])               # the query's LIMIT is honoured, as the database does
    with pytest.raises(ValueError):                            # asked for max_rows + 1, so the excess is seen and refused
        K.load_rows(d, max_rows=2)
    assert len(K.load_rows(d, max_rows=3)) == 3


# --- hydration: winners' text by id, retrievable re-checked ---

def test_hydration_drops_non_retrievable():
    store = {"a": {"text": "A text", "source_url": "u/a", "retrievable": True},
             "b": {"text": "B text", "source_url": "u/b", "retrievable": False}}

    def answer(params):                                   # what the real query does: IN $ids AND retrievable = true
        return [{"chunk_id": i, "text": store[i]["text"], "source_url": store[i]["source_url"]}
                for i in params["ids"] if i in store and store[i]["retrievable"]]
    d = RowsDriver(answer)
    got = K.hydrate(d, ["a", "b", "zz"])
    assert set(got) == {"a"} and got["a"]["text"] == "A text"
    (query, params), = d.calls
    assert query is K.HYDRATE_QUERY and params == {"ids": ["a", "b", "zz"]}
    assert "e.retrievable = true" in query and "e.chunk_id IN $ids" in query
    assert K.hydrate(d, []) == {} and len(d.calls) == 1   # no ids, no query


# --- supply chain / S9 ---

def test_the_keyword_module_imports_no_network_client_or_hub():
    src = open(K.__file__, encoding="utf-8").read()
    assert not re.search(r"^\s*(import|from)\s+(requests|httpx|urllib|huggingface_hub|aiohttp|socket)", src, re.M)
    assert "bm25s.hf" not in src and "save_to_hub" not in src and "load_from_hub" not in src


def test_bm25s_is_imported_lazily_so_the_module_import_stays_light():
    assert "semigraph.retrieval.keyword" in sys.modules
    assert not re.search(r"^(import|from)\s+bm25s", open(K.__file__, encoding="utf-8").read(), re.M)


def test_keyword_leg_does_not_import_scipy():
    """The API process never loads scipy (tests/test_serve_monitor_isolation.py), and ``import bm25s`` would, wherever scipy is
    installed (the serve image has it): building an index and searching must leave it out of ``sys.modules``."""
    import subprocess
    code = ("import sys\n"
            "import semigraph.retrieval.keyword as K\n"
            "idx = K.build_index([{'chunk_id': 'a', 'text': 'export controls on chips'}, {'chunk_id': 'b', 'text': 'memory'}])\n"
            "assert [h.chunk_id for h in idx.search('export controls')] == ['a']\n"
            "assert 'scipy' not in sys.modules and 'scipy.sparse' not in sys.modules, 'scipy was imported'\n")
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_scipy_that_is_already_loaded_is_left_alone():
    import scipy  # noqa: F401 - the dev environment has it
    with K._scipy_not_imported():
        assert sys.modules["scipy"] is not None
    assert sys.modules["scipy"] is not None


def test_a_null_is_current_is_not_current_for_current_only():
    """The dense legs filter ``is_current = true``, which drops a null: the keyword legs agree."""
    rows = [row("nul", "export controls", current=None), row("new", "export controls")]
    idx = K.build_index(rows)
    assert ids(idx.search("export controls", current_only=True)) == ["new"]
    assert sorted(ids(idx.search("export controls"))) == ["new", "nul"]
