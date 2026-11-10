"""One builder of the service's HTTP app over fakes: no Neo4j, no embedder, no LLM (docs/specs/m5b-frontend.md R16, S13).

``tests/serve_state_fakes.py`` and ``tests/test_serve_api.py`` build small apps of their own for the gate tests. This module is
the shared one for the places that need the WHOLE surface answering: the fixture server that Playwright, axe and Lighthouse
run against (``tests/e2e/serve_fixture_app.py``), the byte-fixture recorder (``tests/test_sse_fixtures.py``, I2) and the
parity test that keeps this list of routers equal to ``main.create_app``'s.

It never imports ``semigraph.serve.main`` (``app = create_app()`` runs there at import and calls ``get_settings()``, which reads
``.env``) and never constructs ``Settings``: :class:`FakeSettings` is a plain class. Which route takes which source:

* Real router over the fakes: ``/api/config`` (ui_v2), ``/api/stats``, ``/api/examples``, ``/api/freshness`` (the monitor is
  off: the real "disabled" payload), ``/healthz`` (ok once :func:`patch_data_paths` has replaced ``run_cypher``), the
  workspace routes (503 while ``uploads_enabled`` is off).
* The three answer paths, patched (:func:`patch_answer_paths`): ``routes.aanswer_stream`` (hybrid, vector),
  ``routes.astream_workspace_answer``, and the ``semigraph.agent.stream_async`` module (agent).
* Hand-written JSON, NOT recorded (:func:`patch_data_paths`): the dossier and risk-changes of NVDA, TSM and ASML (``dossier.get_dossier`` /
  ``get_risk_changes`` patched) and one evidence chunk (``routes.run_cypher`` patched), from
  ``tests/e2e/fixture_data/*.handwritten.json``. Recording them would need the production graph. Any other ticker or id is a 404.
* Not served yet: the workspace upload and job pipeline (I3); those routes answer 503.

Every patch goes through a ``pytest.MonkeyPatch`` the caller owns, so a test undoes them and a server process simply keeps
them for its life.
"""

import asyncio
import contextlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from serve_state_fakes import FakeStateBackend, RouteSettings, install_state

from semigraph.retrieval import dossier
from semigraph.serve import dossier_routes, monitor_routes, routes, store, ui_v2, workspace_routes
from semigraph.serve.guard import RateLimiter
from semigraph.serve.limiters import make_limiters
from semigraph.serve.ui_v2 import DEFAULT_DIST_DIR

FIXTURE_DATA = Path(__file__).parent / "e2e" / "fixture_data"
FIXTURE_CHUNK_ID = "0001045810-26-000021:I.1:0320"
COMPANY_FIXTURE_TICKERS = ("NVDA", "TSM", "ASML")    # the companies with hand-written dossier and risk-changes fixtures
# The caps are lifted far above what a browser test does from one address: a per-address window would otherwise answer 429
# to the tenth page load of a Lighthouse run. (Production values are pinned by tests/test_serve_config_production.py.)
GENEROUS = 100_000


class FakeSettings(RouteSettings):
    """``RouteSettings`` (what ``routes`` and ``PaidStream`` read) plus the frontend settings and e2e-sized windows. An override
    must name an attribute that exists (a typo fails here instead of changing nothing)."""

    max_question_chars = 500
    read_rate_limit_per_minute = GENEROUS
    free_rate_limit_questions = GENEROUS
    rate_limit_questions = GENEROUS
    stats_cache_seconds = 0
    turnstile_site_key = ""
    turnstile_stub = False                  # True: any non-empty token passes the ask bot check, none is a 403 (no Cloudflare call)
    ui_v2_preview = False
    ui_v2 = False
    ui_dist_dir = DEFAULT_DIST_DIR
    # the upload limits the workspace routes read (the production values of config.py; uploads_enabled stays False by default)
    upload_max_bytes = 15 * 1024 * 1024
    max_uploads_per_day = 40
    workspace_ttl_hours = 24
    upload_max_documents = 3
    upload_max_versions = 5
    upload_max_workspace_pages = 120
    upload_max_pages = 30
    upload_max_tokens = 10000
    upload_max_workspace_tokens = 48000
    upload_min_mem_available_kb = 0         # the memory guard is off in the fixture server

    def __init__(self, **overrides):
        for name, value in overrides.items():
            if not hasattr(type(self), name):
                raise AttributeError(f"FakeSettings has no setting named {name!r}")
            setattr(self, name, value)


def install_fake_state(app: FastAPI, settings: FakeSettings, *, backend: FakeStateBackend | None = None) -> None:
    """The state ``main.lifespan`` would set on ``app.state`` (settings, drivers, windows, the paid-ask state), as fakes. Also
    used on an app ``main.create_app`` built, which a test starts with :func:`fake_lifespan` instead of the real one."""
    app.state.settings = settings
    app.state.driver = object()
    app.state.embedder = SimpleNamespace(name="fixture-embedder")
    app.state.graph_stats = {"nodes": {"Company": 26}, "relationships": 1, "deleted_risk_lineages": 0}
    app.state.snapshot = {"id": "fixture-snapshot", "as_of": "2026-09-30"}
    app.state.rate_limiter = RateLimiter(settings.rate_limit_questions, settings.rate_limit_window_seconds)
    app.state.free_rate_limiter = RateLimiter(settings.free_rate_limit_questions, settings.rate_limit_window_seconds)
    app.state.read_rate_limiter = RateLimiter(settings.read_rate_limit_per_minute, 60)
    install_state(app, backend=backend)


def fake_lifespan(settings: FakeSettings):
    """A lifespan that makes the limiters INSIDE the running loop (``with TestClient(app)`` keeps one loop for the whole test)
    and connects to nothing; ``app.router.lifespan_context = fake_lifespan(settings)`` replaces ``main.lifespan``."""
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.limiters = make_limiters(settings)
        app.state.loop = asyncio.get_running_loop()
        yield

    return lifespan


def build_app(settings: FakeSettings | None = None, *, backend: FakeStateBackend | None = None) -> FastAPI:
    """The app ``main.create_app`` builds, over fakes: the same routers in the same order, no docs or schema route, the fake
    state, and ``ui_v2.install`` last."""
    settings = settings or FakeSettings()
    app = FastAPI(lifespan=fake_lifespan(settings), docs_url=None, redoc_url=None, openapi_url=None)
    for router in (routes.router, monitor_routes.router, dossier_routes.router, workspace_routes.router):
        app.include_router(router)
    install_fake_state(app, settings, backend=backend)
    ui_v2.install(app, settings)
    return app


# ---------------------------------------------------------------- the three answer paths (S13)

def answer_events(question: str, strategy: str) -> list[dict]:
    """The events of a short live answer in the grammar the page reads (hand-written: the byte fixtures of I2 are recorded)."""
    text = f"Fixture answer to the question, with one citation [{FIXTURE_CHUNK_ID}]."
    return [{"event": "retrieval", "anchors": {"Nvidia": 1045810},
             "counts": {"edges": 2, "metrics": 1, "risks": 1, "temporal": 0, "chunks": 1}},
            {"event": "delta", "text": "Fixture answer to the question, "},
            {"event": "delta", "text": f"with one citation [{FIXTURE_CHUNK_ID}]."},
            {"event": "done", "answer": text, "citations": [FIXTURE_CHUNK_ID], "hallucinated": [],
             "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": 0.0,
             "chunk_ids": [FIXTURE_CHUNK_ID], "context_chars": 100, "strategy": strategy, "question": question}]


async def fake_answer_stream(question, driver, embedder, strategy="hybrid", **kw):
    for event in answer_events(question, strategy):
        yield event


def patch_answer_paths(mp: pytest.MonkeyPatch) -> None:
    """Replace all three paths a paid ask can take with the fake stream: the fixed path, the workspace writer, and the agent
    stream module (the route imports it lazily, so a stub module stands in; no langgraph is needed)."""
    mp.setattr(routes, "aanswer_stream", fake_answer_stream)
    mp.setattr(routes, "astream_workspace_answer", fake_answer_stream)
    stub = types.ModuleType("semigraph.agent.stream_async")
    stub.aagent_answer_stream = fake_answer_stream
    mp.setitem(sys.modules, "semigraph.agent", types.ModuleType("semigraph.agent"))
    mp.setitem(sys.modules, "semigraph.agent.stream_async", stub)


# ---------------------------------------------------------------- the data paths

def load_fixture(name: str, data_dir: Path = FIXTURE_DATA):
    return json.loads((data_dir / name).read_text(encoding="utf-8"))


def patch_data_paths(mp: pytest.MonkeyPatch, data_dir: Path = FIXTURE_DATA) -> None:
    """The store reads that touch the driver, the dossier, and the evidence query, answered from hand-written JSON."""
    # docs/specs/m5b-redesign.md R3: NVDA (10-K, USD), TSM (20-F, TWD, rules) and ASML (20-F, EUR, no rules); every other ticker 404s
    dossiers = {t: load_fixture(f"dossier_{t.lower()}.handwritten.json", data_dir) for t in COMPANY_FIXTURE_TICKERS}
    changes = {t: load_fixture(f"risk_changes_{t.lower()}.handwritten.json", data_dir) for t in COMPANY_FIXTURE_TICKERS}
    chunk = load_fixture("evidence_chunk.handwritten.json", data_dir)

    def fake_run_cypher(driver, query, **params):
        if query is routes.EVIDENCE_QUERY and params.get("id") == chunk["chunk_id"]:
            return [dict(chunk)]
        return []

    mp.setattr(store, "log_query", lambda driver, **kw: None)
    mp.setattr(store, "get_policy", lambda driver, key: None)
    mp.setattr(store, "ledger_summary", lambda driver: {"today": {"paid": 0}})
    mp.setattr(dossier, "get_dossier", lambda driver, ticker, **kw: dict(dossiers[ticker]) if ticker in dossiers else None)
    mp.setattr(dossier, "get_risk_changes", lambda driver, ticker, **kw: dict(changes[ticker]) if ticker in changes else None)
    mp.setattr(routes, "run_cypher", fake_run_cypher)
