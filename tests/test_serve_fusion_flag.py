"""I3a (docs/specs/m2-search.md R11, R13, S6; OD8): RETRIEVAL_FUSION wiring. The setting, the fingerprint element that exists
only when the flag is on, the answer-cache key and the seeded examples that differ across it, the serving paths that pass the
fusion runtime, and the fail-closed index build. No database, no network."""

import hashlib
import logging
import types

import pytest
from agent_fakes import FakeDriver, FakeEmbedder
from fastapi.testclient import TestClient
from serve_state_fakes import fresh_drain  # noqa: F401 - a fixture

import semigraph.retrieval.answerer as answerer_mod
from semigraph.config import Settings
from semigraph.retrieval import answerer_async, fusion, workspace_async
from semigraph.retrieval import keyword as K
from semigraph.retrieval.answerer import CONTEXT_HEADERS, CONTEXT_VERSION, template_fingerprint
from semigraph.serve import main, store
from semigraph.serve import stream_runtime as sr
from semigraph.serve.limiters import make_limiters

SNAP = "snap-20260924-7feaaf9bfe"
CLEAN = {"citations_retrieved": True, "numbers_grounded": True, "numbers_checked": 1, "unmatched_numbers": [],
         "echoed_numbers": [], "pseudo_citations": [], "has_citation": True, "is_refusal": False,
         "unsupported_removal_claim": False, "unsupported_removal_sentences": []}


def example(id_="N1"):
    return {"id": id_, "type": "numeric", "question": f"Question {id_}?", "answer": "A [c1].", "citations": ["c1"],
            "hallucinated": [], "checks": CLEAN}


# --- the setting ---

def test_the_flag_defaults_off_and_reads_the_environment(monkeypatch):
    assert Settings(_env_file=None).retrieval_fusion is False
    monkeypatch.setenv("RETRIEVAL_FUSION", "true")
    assert Settings(_env_file=None).retrieval_fusion is True


def test_production_accepts_the_flag_either_way():
    base = dict(environment="production", ip_hash_pepper="p" * 48, turnstile_required=True, turnstile_secret_key="s",
                client_ip_header="fly-client-ip")
    assert Settings(_env_file=None, **base).retrieval_fusion is False
    assert Settings(_env_file=None, retrieval_fusion=True, **base).retrieval_fusion is True


# --- the fingerprint: an element only when on ---

def test_fingerprint_is_2a6910aa5d_with_the_flag_off():
    assert template_fingerprint() == template_fingerprint(fusion=False) == "2a6910aa5d"


def test_fusion_on_changes_fingerprint_and_cache_key(monkeypatch):
    on, off = template_fingerprint(fusion=True), template_fingerprint()
    parts = [answerer_mod.ANSWER_PROMPT, CONTEXT_VERSION, *CONTEXT_HEADERS,
             f"fusion={fusion.FUSION_VERSION}:{fusion.serving_digest()}", f"tokenizer={K.TOKENIZER_VERSION}"]
    assert on != off and on == hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:10]
    # the fusion version and the tokenizer version each move it, and neither moves the flag-off fingerprint
    monkeypatch.setattr(fusion, "FUSION_VERSION", "next")
    assert template_fingerprint(fusion=True) != on and template_fingerprint() == off
    monkeypatch.undo()
    monkeypatch.setattr(K, "TOKENIZER_VERSION", "k2")
    assert template_fingerprint(fusion=True) != on and template_fingerprint() == off
    # the answer cache key follows
    assert store.cache_key("What about Nvidia?", "hybrid", SNAP, fusion=True) != store.cache_key("What about Nvidia?", "hybrid", SNAP)
    assert store.cache_key("What about Nvidia?", "hybrid", SNAP) == store.cache_key("What about Nvidia?", "hybrid", SNAP, fusion=False)


def test_put_answer_writes_under_the_key_of_its_flag(monkeypatch):
    seen = []
    monkeypatch.setattr(store, "run_cypher", lambda driver, query, **p: seen.append(p))
    kw = dict(question="Q?", strategy="hybrid", answer="A", citations=[], hallucinated=[], snapshot_id=SNAP)
    store.put_answer(object(), **kw)
    store.put_answer(object(), fusion=True, **kw)
    assert seen[0]["key"] == store.cache_key("Q?", "hybrid", SNAP)
    assert seen[1]["key"] == store.cache_key("Q?", "hybrid", SNAP, fusion=True) != seen[0]["key"]


# --- seeded examples refused across the flag ---

@pytest.fixture
def puts(monkeypatch):
    seen = []
    monkeypatch.setattr(store, "put_answer", lambda driver, **kw: seen.append(kw))
    return seen


def doc(fusion_on: bool):
    return {"source": "x", "snapshot_id": SNAP, "template_fingerprint": template_fingerprint(fusion=fusion_on),
            "examples": [example()]}


def test_examples_refused_across_the_flag(puts, caplog):
    with caplog.at_level(logging.WARNING, logger="semigraph.serve.main"):
        assert main.seed_examples(None, doc(False), SNAP, fusion=True) == frozenset() and puts == []
        assert main.seed_examples(None, doc(True), SNAP) == frozenset() and puts == []
    assert sum("another answer-prompt template" in r.getMessage() for r in caplog.records) == 2
    assert main.seed_examples(None, doc(False), SNAP) == {"N1"} and len(puts) == 1 and "fusion" not in puts[0]
    puts.clear()
    assert main.seed_examples(None, doc(True), SNAP, fusion=True) == {"N1"} and puts[0]["fusion"] is True


def test_examples_match_template_is_per_flag():
    assert store.examples_match_template(doc(False)) and not store.examples_match_template(doc(True))
    assert store.examples_match_template(doc(True), fusion=True) and not store.examples_match_template(doc(False), fusion=True)


# --- the serving paths ---

def runtime():
    idx = K.build_index([{"chunk_id": "c1", "text": "export controls"}], with_context=True, snapshot_id=SNAP)
    return fusion.FusionRuntime(fusion.SERVING_CONFIG, idx)


def test_serving_config_is_the_measured_legs_with_the_context_index():
    c = fusion.SERVING_CONFIG
    assert (c.mention, c.filer, c.period, c.keyword, c.keyword_context, c.current_only) == (True,) * 6
    assert c.mention_current_only is True                        # decision 011 (option D): every leg current-only when on
    import importlib.util
    import sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("m2_bench_for_flag", Path(__file__).resolve().parents[1] / "scripts" / "m2_bench.py")
    bench = importlib.util.module_from_spec(spec)
    sys.modules["m2_bench_for_flag"] = bench
    spec.loader.exec_module(bench)
    assert bench.arm_config("all") == c                          # the bench measures what is served
    assert runtime().snapshot_id == SNAP


def test_helpers_pass_the_runtime_only_for_hybrid_and_workspace_asks_and_only_when_present():
    rt = runtime()
    st = types.SimpleNamespace(fusion=rt)
    assert sr.fusion_twin_kwargs(st, "hybrid", False) == {"fusion": rt} == sr.fusion_twin_kwargs(st, "hybrid", True)
    assert sr.fusion_twin_kwargs(st, "vector", False) == {}
    assert sr.fusion_twin_kwargs(st, "agent", False) == {}                  # the agent prefetch keeps its own setting
    assert sr.fusion_twin_kwargs(st, "agent", True) == {"fusion": rt}       # a workspace ask is never the agent
    assert sr.fusion_cache_kwargs(st) == {"fusion": True}
    for none in (types.SimpleNamespace(fusion=None), types.SimpleNamespace(), types.SimpleNamespace(fusion=object())):
        assert sr.fusion_twin_kwargs(none, "hybrid", False) == {} and sr.fusion_cache_kwargs(none) == {}


def test_paid_stream_hands_the_runtime_to_the_twin_and_caches_under_the_flag_key():
    rt = runtime()
    got = {}

    class Gen:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            return None

    def twin(question, driver, embedder, **kw):
        got.update(kw)
        return Gen()
    s = Settings(_env_file=None)
    st = types.SimpleNamespace(settings=s, driver=None, embedder=None, limiters=None, state=None, fusion=rt)
    lease = types.SimpleNamespace(lease_id="l")
    for strategy, workspace, expected in (("hybrid", None, rt), ("vector", None, None), ("hybrid", {"workspace_id": "w"}, rt)):
        got.clear()
        sr.PaidStream(st, "Q?", strategy, "iph", SNAP, workspace, lease=lease, twin=twin)._open_stream()
        assert got.get("fusion") is expected
    got.clear()
    st_off = types.SimpleNamespace(settings=s, driver=None, embedder=None, limiters=None, state=None)
    sr.PaidStream(st_off, "Q?", "hybrid", "iph", SNAP, None, lease=lease, twin=twin)._open_stream()
    assert "fusion" not in got                                                # flag off: the call is exactly what it was


class Stop(Exception):
    pass


def capture_hybrid(monkeypatch, module):
    seen = {}

    def fake(question, driver, embedder, **kw):
        seen.update(kw)
        raise Stop
    monkeypatch.setattr(module, "hybrid_retrieve", fake)
    return seen


async def first_event(agen):
    async for _ in agen:
        break


def run_async(coro):
    import asyncio
    return asyncio.run(coro)


def limiters():
    return make_limiters(Settings(_env_file=None))


def test_aanswer_stream_passes_the_fusion_config_and_index_only_when_given(monkeypatch):
    rt = runtime()
    seen = capture_hybrid(monkeypatch, answerer_async)
    for kw, expected in (({"fusion": rt}, {"fusion": rt.config, "keyword_index": rt.index}), ({}, {})):
        seen.clear()
        with pytest.raises(Stop):
            run_async(first_event(answerer_async.aanswer_stream("Nvidia?", FakeDriver.world(), FakeEmbedder(), limiters=limiters(),
                                                               **kw)))
        assert {k: v for k, v in seen.items() if k in ("fusion", "keyword_index")} == expected


def test_a_workspace_ask_fuses_only_its_sec_part(monkeypatch):
    rt = runtime()
    seen = capture_hybrid(monkeypatch, workspace_async)
    with pytest.raises(Stop):
        run_async(first_event(workspace_async.astream_workspace_answer(
            "Nvidia?", FakeDriver.world(), FakeEmbedder(), limiters=limiters(), workspace_id="w" * 16, fusion=rt)))
    assert seen["fusion"] is rt.config and seen["keyword_index"] is rt.index


# --- the index is built only when the flag is on, and fails closed ---

class Drv:
    def __init__(self, rows=None, boom=None):
        self.rows, self.boom, self.calls = rows or [], boom, 0

    def session(self, **kw):
        drv = self

        class S:
            def __enter__(s):
                return s

            def __exit__(s, *e):
                return False

            def run(s, q, **p):
                drv.calls += 1
                if drv.boom:
                    raise drv.boom
                return drv.rows
        return S()


def db_row(cid, text):
    return {"chunk_id": cid, "text": text, "filer_cik": 1, "form": "10-K", "accession_no": "A", "content_hash": cid,
            "valid_from": "2025-01-01", "is_current": True, "section_id": "I.1A", "sub_heading": None,
            "filing_date": "2025-01-01", "company": "NVIDIA", "ticker": "NVDA", "mention_ciks": []}


def test_flag_off_builds_nothing_and_touches_nothing():
    drv = Drv()
    assert K.build_fusion_runtime(drv, Settings(_env_file=None), SNAP) is None and drv.calls == 0


def test_flag_on_builds_the_index_keyed_by_the_snapshot(caplog):
    drv = Drv([db_row("c1", "export controls"), db_row("c2", "memory pricing")])
    with caplog.at_level(logging.INFO, logger="semigraph.retrieval.keyword"):
        rt = K.build_fusion_runtime(drv, Settings(_env_file=None, retrieval_fusion=True), SNAP)
    assert rt.config == fusion.SERVING_CONFIG and rt.snapshot_id == SNAP and rt.index.size == 2 and drv.calls == 1
    assert rt.index.with_context and [h.chunk_id for h in rt.index.search("nvda memory")] == ["c2", "c1"]
    assert any("keyword index built" in r.getMessage() and "2 spans" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("drv", [Drv(boom=RuntimeError("neo4j is down")), Drv(rows=[]), Drv(rows=[{"chunk_id": "x"}])])
def test_flag_on_index_failure_falls_back_to_todays_path(drv, caplog):
    with caplog.at_level(logging.ERROR, logger="semigraph.retrieval.keyword"):
        assert K.build_fusion_runtime(drv, Settings(_env_file=None, retrieval_fusion=True), SNAP) is None
    assert any("RETRIEVAL_FUSION" in r.getMessage() and "today's retrieval path" in r.getMessage() for r in caplog.records)
    st = types.SimpleNamespace(fusion=None)                                   # what app.state holds then
    assert sr.fusion_twin_kwargs(st, "hybrid", False) == {} and sr.fusion_cache_kwargs(st) == {}


def test_a_failed_index_build_means_the_ask_runs_the_old_excerpt_query(monkeypatch):
    """No runtime -> no ``fusion`` kwarg -> hybrid_retrieve(fusion=None): the single EXCERPTS_QUERY, as with the flag off."""
    seen = capture_hybrid(monkeypatch, answerer_async)
    with pytest.raises(Stop):
        run_async(first_event(answerer_async.aanswer_stream("Nvidia?", FakeDriver.world(), FakeEmbedder(), limiters=limiters(),
                                                           **sr.fusion_twin_kwargs(types.SimpleNamespace(fusion=None), "hybrid", False))))
    assert "fusion" not in seen and "keyword_index" not in seen


# --- the lifespan ---

pytestmark = pytest.mark.usefixtures("fresh_drain")


def lifespan_app(monkeypatch, **over):
    from test_serve_lifespan import Boot, install_boot
    boot = install_boot(monkeypatch, Boot())
    settings = Settings(_env_file=None, **over)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    return boot


def test_the_lifespan_publishes_what_bootstrap_built_and_a_plain_result_means_no_fusion(monkeypatch):
    boot = lifespan_app(monkeypatch)
    with TestClient(main.create_app()) as client:
        assert client.app.state.fusion is None                   # a bootstrap result with no ``fusion`` attribute
    rt = runtime()

    def booted(settings):
        out = main.Booted(boot.bootstrap(settings))
        out.fusion = rt
        return out
    monkeypatch.setattr(main, "bootstrap", booted)
    with TestClient(main.create_app()) as client:
        assert client.app.state.fusion is rt


class BootFakes:
    """What ``bootstrap`` touches (as tests/test_serve_seeding.py), with a driver that serves the keyword loader."""

    def __init__(self, monkeypatch, doc, driver):
        self.puts = []
        monkeypatch.setattr(main, "connect_with_retry", lambda settings: driver)
        monkeypatch.setattr(main, "apply_schema", lambda d: None)
        monkeypatch.setattr(main, "graph_stats", lambda d: {"nodes": {}})
        monkeypatch.setattr(main, "load_examples", lambda: doc)
        monkeypatch.setattr(main, "Embedder", lambda: type("E", (), {"encode_query": lambda self, q: [0.0]})())
        monkeypatch.setattr(store, "ensure_indexes", lambda d: None)
        monkeypatch.setattr(store, "current_snapshot", lambda d: {"id": SNAP, "as_of": "2026-09-24"})
        monkeypatch.setattr(store, "put_answer", lambda d, **kw: self.puts.append(kw))


def test_flag_on_with_a_failed_index_serves_and_seeds_like_the_flag_off(monkeypatch, caplog):
    """One predicate for seeding, cache keys and the serving path: the runtime. The index could not be built, so the flag-off
    examples are seeded under the flag-off fingerprint and no fusion runtime is published."""
    fakes = BootFakes(monkeypatch, doc(False), Drv(boom=RuntimeError("down")))
    with caplog.at_level(logging.ERROR, logger="semigraph.retrieval.keyword"):
        booted = main.bootstrap(Settings(_env_file=None, retrieval_fusion=True))
    assert booted.fusion is None and booted[4] == frozenset({"N1"}) and len(fakes.puts) == 1 and "fusion" not in fakes.puts[0]
    assert any("RETRIEVAL_FUSION" in r.getMessage() for r in caplog.records)


def test_flag_on_with_a_built_index_refuses_the_flag_off_examples_and_publishes_the_runtime(monkeypatch):
    BootFakes(monkeypatch, doc(False), Drv([db_row("c1", "export controls")]))
    booted = main.bootstrap(Settings(_env_file=None, retrieval_fusion=True))
    assert booted.fusion is not None and booted.fusion.snapshot_id == SNAP and booted[4] == frozenset()
    fakes = BootFakes(monkeypatch, doc(True), Drv([db_row("c1", "export controls")]))
    assert main.bootstrap(Settings(_env_file=None, retrieval_fusion=True))[4] == frozenset({"N1"}) and fakes.puts[0]["fusion"] is True


def test_flag_off_bootstrap_builds_no_index_and_seeds_as_before(monkeypatch):
    drv = Drv(boom=RuntimeError("must not be touched"))
    BootFakes(monkeypatch, doc(False), drv)
    booted = main.bootstrap(Settings(_env_file=None))
    assert booted.fusion is None and booted[4] == frozenset({"N1"}) and drv.calls == 0


def test_the_sync_workspace_writer_fuses_only_its_sec_part_too(monkeypatch):
    from semigraph.retrieval import workspace as ws
    rt = runtime()
    seen = capture_hybrid(monkeypatch, ws)
    with pytest.raises(Stop):
        next(ws.stream_workspace_answer("Nvidia?", FakeDriver.world(), FakeEmbedder(), workspace_id="w" * 16, fusion=rt))
    assert seen["fusion"] is rt.config and seen["keyword_index"] is rt.index
    seen.clear()
    with pytest.raises(Stop):
        next(ws.stream_workspace_answer("Nvidia?", FakeDriver.world(), FakeEmbedder(), workspace_id="w" * 16))
    assert "fusion" not in seen and "keyword_index" not in seen


# --- I3a review fixes ---

def test_an_as_of_workspace_ask_never_uses_the_current_only_keyword_index(monkeypatch):
    """OD8: ``as_of`` falls back to the dense legs for the SEC part (async and sync twins)."""
    rt = runtime()
    seen = capture_hybrid(monkeypatch, workspace_async)
    with pytest.raises(Stop):
        run_async(first_event(workspace_async.astream_workspace_answer(
            "Nvidia?", FakeDriver.world(), FakeEmbedder(), limiters=limiters(), workspace_id="w" * 16, fusion=rt,
            as_of="2025-01-01")))
    assert seen["fusion"].keyword is False and seen["keyword_index"] is None and seen["fusion"].filer is True
    from semigraph.retrieval import workspace as ws_sync
    seen = capture_hybrid(monkeypatch, ws_sync)
    with pytest.raises(Stop):
        next(ws_sync.stream_workspace_answer("Nvidia?", FakeDriver.world(), FakeEmbedder(), workspace_id="w" * 16, fusion=rt,
                                             as_of="2025-01-01"))
    assert seen["fusion"].keyword is False and seen["keyword_index"] is None


def test_index_failure_log_carries_the_reason_for_a_value_error(caplog):
    with caplog.at_level(logging.ERROR, logger="semigraph.retrieval.keyword"):
        K.build_fusion_runtime(Drv(rows=[]), Settings(_env_file=None, retrieval_fusion=True), SNAP)
    assert any("no retrievable spans" in r.getMessage() for r in caplog.records)


def test_a_change_to_serving_config_changes_the_fingerprint(monkeypatch):
    before = template_fingerprint(fusion=True)
    monkeypatch.setattr(fusion, "SERVING_CONFIG", fusion.FusionConfig(filer=True, keyword=True, keyword_context=True))
    assert template_fingerprint(fusion=True) != before
    assert template_fingerprint() == "2a6910aa5d"
