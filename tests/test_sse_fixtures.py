"""The byte fixtures of the /api/ask and job event streams (docs/specs/m5b-frontend.md R15 and R16, M5b increment I2).

``tests/data/sse/`` holds what the page's stream client is tested against: each ``.sse`` file is the exact byte sequence a real
route sent (frames separated by a blank line, ``sep="\\n"``), recorded over fakes at $0 with the pings off, and each has an
``.expected.json`` sidecar of the decoded events. The sidecar is written from the event DICTS the route yielded just before
serialisation (the ``ServerSentEvent`` builders are wrapped, after ``PaidStream``'s own rewrites such as the generic error
detail and the cached ``done``), never by the TypeScript decoder under test, so the vitest comparison joins two independent
paths. ``refusals.json`` records the pre-stream JSON refusals with their real ``MSG_*`` text.

Two fixtures are hand-written and say so in their sidecar (``"source": "hand-written"``): ``ping_interleaved.sse`` and
``done_max_size.sse``. The job fixtures are ``route-recorded, job dicts constructed``: the real job route over the real
``JobRegistry``, with the job dicts built exactly as ``uploads/jobs.py`` emits them (the pipeline itself needs a parser, an
embedder and Neo4j, and is not run).

Default mode re-records everything in memory and compares it byte for byte with the committed files, so a fixture cannot go
stale. To rewrite them after a deliberate change to the bytes (a spec amendment and the owner's approval, R18):

    RECORD_SSE=1 uv run pytest tests/test_sse_fixtures.py

Nothing here touches Neo4j, the network, a model or ``.env``: the answer paths are the REAL async twins (``aanswer_stream``,
``aagent_answer_stream``, ``astream_workspace_answer``) over ``tests/agent_fakes.py`` and scripted writers.
"""

from __future__ import annotations

import contextlib
import importlib.util
import itertools
import json
import os
import re
import socket
import sys
from contextlib import aclosing
from pathlib import Path

import anyio
import pytest
from agent_fakes import LUNA, SONNET, FakeDriver, FakeEmbedder, FakeStream, make_settings
from fastapi.testclient import TestClient
from serve_app_factory import FakeSettings, build_app, patch_data_paths
from serve_state_fakes import FakeStateBackend, fresh_drain  # noqa: F401 - fresh_drain: a fixture

from semigraph.agent import graph as agent_graph
from semigraph.agent import stream_async
from semigraph.retrieval.answerer_async import aanswer_stream
from semigraph.retrieval.workspace_async import astream_workspace_answer
from semigraph.serve import drain, routes, stream_runtime, workspace_routes
from semigraph.serve.state import Denied
from semigraph.uploads import jobs

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
import serve_fixture_app as fixture_app  # noqa: E402

pytestmark = pytest.mark.usefixtures("fresh_drain")

HERE = Path(__file__).parent
SSE_DIR = HERE / "data" / "sse"
RECORDER_PATH = HERE / "data" / "record_events_pre_m5.py"
RECORDING = os.environ.get("RECORD_SSE") == "1"

NO_PING = 3600                  # sse-starlette treats ping=None as "the default 15 s", so the interval is made very long
WS_TOKEN = "t" * 43             # the route reads the header; the token check is patched
DOC = "0123456789ab"
JOB_READY, JOB_FAILED = "00112233445566ff", "ffeeddccbbaa9988"
HAND_WRITTEN = "hand-written"
ROUTE_RECORDED = "route-recorded"
JOBS_SOURCE = "route-recorded, job dicts constructed"
TERMINAL = {"done", "error"}
MAX_ANSWER_TOKENS = 2400        # LLM_ANSWER_MAX_TOKENS (fly.toml:30)
CHARS_PER_TOKEN = 6             # a deliberately generous upper bound (English text is about 4; citation ids and numbers cost more)

SSE_NAMES = ["cached_done", "live_hybrid", "agent_steps", "escalated", "workspace_done", "error_before_delta",
             "error_after_delta", "job_ready", "job_failed", "ping_interleaved", "done_max_size"]
FIXTURE_FILES = sorted([f"{n}.sse" for n in SSE_NAMES] + [f"{n}.expected.json" for n in SSE_NAMES] + ["refusals.json"])


def _load_recorder():
    spec = importlib.util.spec_from_file_location("record_events_pre_m5_for_sse_fixtures", RECORDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REAL_AGENT_TWIN = stream_async.aagent_answer_stream      # captured before install_agent replaces the module attribute
rec = _load_recorder()
QUESTION = rec.QUESTION
WS_ID = rec.WS_ID


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


NEO4J_BOLT_PORT = 7687          # Neo4j Desktop listens on localhost: loopback is open, the database is not


def _is_loopback(address) -> bool:
    return (isinstance(address, tuple) and address and address[0] in LOOPBACK
            and not (len(address) > 1 and address[1] == NEO4J_BOLT_PORT))


ACTIVE_GUARDS: list = []        # the guards of the running test (read by the test that proves they are on)


@pytest.fixture(autouse=True)
def guards():
    """The fixture server's guards (tests/e2e/serve_fixture_app.py::install_guards) around every test here, recording
    included: Settings(), get_settings() and litellm raise and log a hit, so a regression into one of the lazy ``.env`` or
    Neo4j fallbacks cannot pass quietly. The hit list must be empty when the test ends."""
    installed = fixture_app.install_guards()
    ACTIVE_GUARDS.append(installed)
    try:
        yield installed
        assert installed.hits == [], f"a guarded call was reached while recording: {installed.hits}"
    finally:
        ACTIVE_GUARDS.remove(installed)
        installed.undo()


def scripted_settings(**over):
    """``make_settings`` builds ``Settings(_env_file=None, ...)``: it reads no ``.env`` (the one thing the guard exists to
    keep out), but it is still a ``Settings()`` call, which the guard raises on. This lifts the class-level raiser for exactly
    that call and puts it back, so every OTHER ``Settings()`` (one with no ``_env_file=None``) still raises and logs."""
    from semigraph import config

    raiser = vars(config.Settings).get("__init__")
    if raiser is not None:
        del config.Settings.__init__
    try:
        return make_settings(**over)
    finally:
        if raiser is not None:
            config.Settings.__init__ = raiser


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Recording reaches no remote host and resolves no name. Loopback stays open: on Windows asyncio builds its self-pipe
    (and so every event loop) from a loopback socket pair. Every refused attempt is logged (a broad ``except`` could swallow
    the error) and the list must be empty when the test ends."""
    attempts: list[str] = []
    real_connect, real_connect_ex, real_resolve = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def guarded(name, real):
        def check(*args, **kwargs):
            target = args[1] if name.startswith("socket.") and len(args) > 1 else args[0] if args else None
            if name == "getaddrinfo":
                allowed = target in LOOPBACK or target is None
            else:
                allowed = _is_loopback(target)
            if not allowed:
                attempts.append(f"{name}({target!r})")
                raise OSError(f"{name}: recording a fixture must not touch the network")
            return real(*args, **kwargs)
        return check

    monkeypatch.setattr(socket.socket, "connect", guarded("socket.connect", real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded("socket.connect_ex", real_connect_ex))
    monkeypatch.setattr(socket, "getaddrinfo", guarded("getaddrinfo", real_resolve))
    yield
    assert attempts == [], f"network access while recording: {attempts}"


# ---------------------------------------------------------------- scripted model streams (the async side of agent_fakes)

class AsyncStream:
    """The async twin of ``agent_fakes.FakeStream``: its parts one at a time, then its failure."""

    def __init__(self, stream: FakeStream):
        self.parts, self.boom = list(stream.parts), stream.boom
        self.model, self.usage, self.finish_reason = stream.model, stream.usage, stream.finish_reason

    async def __aiter__(self):
        for part in self.parts:
            await anyio.sleep(0)
            yield part
        await anyio.sleep(0)
        if self.boom:
            raise self.boom


def asyncified(factory):
    return lambda prompt: AsyncStream(factory(prompt))


def writer_kwargs(inputs: dict) -> dict:
    """``llm_stream`` plus the escalation arguments of a scenario's inputs, as async streams. ``model`` is always named with an
    escalation (else the writer would read ``get_settings().answer_model``, which reads ``.env``)."""
    prompts: list[str] = []
    kwargs = rec._escalation_kwargs(inputs, prompts)
    if "escalation_stream" in kwargs:
        kwargs["escalation_stream"] = asyncified(kwargs["escalation_stream"])
    return {"llm_stream": asyncified(rec._writer_factory(inputs["writer"], prompts)), **kwargs}


def hybrid_inputs(parts, *, fail=None, escalation=None) -> dict:
    return {"writer": rec.writer(parts, usage=(1500, 60), fail=fail), "model": LUNA if escalation else None,
            "escalation": escalation}


# ---------------------------------------------------------------- the three answer paths: the REAL twins over fakes

def install_hybrid(mp, inputs: dict) -> None:
    async def twin(question, driver, embedder, strategy="hybrid", *, limiters, meter=None, **kw):
        events = aanswer_stream(question, FakeDriver.world(), FakeEmbedder(), strategy, limiters=limiters, **writer_kwargs(inputs))
        async with aclosing(events) as inner:
            async for event in inner:
                yield event

    mp.setattr(routes, "aanswer_stream", twin)


def install_agent(mp, inputs: dict) -> None:
    async def twin(question, driver, embedder, strategy="agent", *, limiters, meter=None, **kw):
        events = REAL_AGENT_TWIN(
            question, FakeDriver.world(), FakeEmbedder(), strategy, limiters=limiters, planner=rec._planner(inputs["planner"]),
            settings=scripted_settings(**inputs["settings"]), tracer=rec._CallLog(), **writer_kwargs(inputs))
        async with aclosing(events) as inner:
            async for event in inner:
                yield event

    mp.setattr(stream_async, "aagent_answer_stream", twin)


def install_workspace(mp, inputs: dict) -> None:
    async def twin(question, driver, embedder, *, strategy, limiters, workspace_id, as_of=None, meter=None, **kw):
        fake = rec.UploadFakeDriver(inputs["doc_chunks"], inputs["chunk_is_current"])
        events = astream_workspace_answer(question, fake, FakeEmbedder(), limiters=limiters, strategy=strategy,
                                          workspace_id=workspace_id, as_of=as_of, **writer_kwargs(inputs))
        async with aclosing(events) as inner:
            async for event in inner:
                yield event

    mp.setattr(routes, "astream_workspace_answer", twin)


# ---------------------------------------------------------------- the app, the tee, the capture

class StepClock:
    """``semigraph.agent.graph._now`` frozen into a deterministic ascending clock: ``done.agent.elapsed_s`` is then a real float
    (a string is not what the route sends, and the client's ``Number.isFinite`` check would reject it)."""

    def __init__(self):
        self._ticks = itertools.count()

    def __call__(self) -> float:
        return 1000.0 + 0.125 * next(self._ticks)


@contextlib.contextmanager
def serving(**overrides):
    """An app over fakes with the pings off, the agent clock frozen and a fresh drain, plus the tee: every event dict the
    routes hand to a ``ServerSentEvent`` builder (``events``). Yields ``(client, events, backend)``."""
    backend = overrides.pop("backend", None) or FakeStateBackend()
    with pytest.MonkeyPatch.context() as mp:
        new_drain = drain.Drain()
        mp.setattr(drain, "DRAIN", new_drain)
        mp.setattr(stream_runtime, "PING_SECONDS", NO_PING)
        mp.setattr(workspace_routes, "SSE_POLL_TIMEOUT_S", NO_PING)
        mp.setattr(agent_graph, "_now", StepClock())
        patch_data_paths(mp)
        mp.setattr(routes, "_workspace_token_ok", lambda driver, ws, token: True)
        mp.setattr(workspace_routes.repo, "touch", lambda driver, ws: None)
        events: list[dict] = []
        for module, name in ((stream_runtime, "sse_event"), (routes, "sse_event"), (workspace_routes, "_job_sse")):
            mp.setattr(module, name, _teed(getattr(module, name), events))
        app = build_app(FakeSettings(agent_enabled=True, uploads_enabled=True, **overrides), backend=backend)
        app.state.driver, app.state.embedder, app.state.uploads_ready = FakeDriver.world(), FakeEmbedder(), True
        app.state.mp = mp
        with TestClient(app) as client:
            yield client, events, backend
        assert new_drain.active == 0


def _teed(real, events: list[dict]):
    """``real`` that first records its event dict, serialised as the wire does (``json.dumps(default=str)``)."""
    def recording(event):
        events.append(json.loads(json.dumps(event, default=str)))
        return real(event)
    return recording


def ask(client, **body):
    return client.post("/api/ask", json={"question": QUESTION, **body})


def ws_ask(client, **body):
    return client.post("/api/ask", json={"question": rec.WS_QUESTION, "workspace_id": WS_ID, **body},
                       headers={"X-Workspace-Token": WS_TOKEN})


def sse_fixture(response, events: list[dict], source: str = ROUTE_RECORDED) -> tuple[bytes, dict]:
    assert response.status_code == 200 and response.headers["content-type"] == "text/event-stream; charset=utf-8"
    return response.content, {"source": source, "events": list(events)}


def _strong_ok() -> dict:
    return {"model": SONNET, "stream": rec.writer(rec.GOOD_PARTS, usage=(12000, 300), model=SONNET)}


def record_asks() -> dict[str, tuple[bytes, dict]]:
    out: dict[str, tuple[bytes, dict]] = {}
    agent = dict(rec.agent_scenarios())["agent_multi_step_live"]
    workspace = dict(rec.workspace_scenarios())["workspace_cited_answer"]
    with serving() as (client, events, _):
        install_hybrid(client.app.state.mp, hybrid_inputs(rec.GOOD_PARTS))
        out["live_hybrid"] = sse_fixture(ask(client), events)
        events.clear()
        out["cached_done"] = sse_fixture(ask(client), events)          # the same question again: the cache hit
    with serving() as (client, events, _):
        install_agent(client.app.state.mp, agent)
        out["agent_steps"] = sse_fixture(ask(client, strategy="agent"), events)
    with serving() as (client, events, _):
        install_hybrid(client.app.state.mp, hybrid_inputs([f"Nvidia depends on TSMC [{rec.FABRICATED}]."], escalation=_strong_ok()))
        out["escalated"] = sse_fixture(ask(client), events)
    with serving() as (client, events, _):
        install_workspace(client.app.state.mp, workspace)
        out["workspace_done"] = sse_fixture(ws_ask(client), events)
    with serving() as (client, events, _):
        install_hybrid(client.app.state.mp, hybrid_inputs([], fail=rec.failure("RuntimeError", "stream interrupted")))
        out["error_before_delta"] = sse_fixture(ask(client), events)
    with serving() as (client, events, _):
        install_hybrid(client.app.state.mp, hybrid_inputs(rec.GOOD_PARTS[:1], fail=rec.failure("RuntimeError", "stream interrupted")))
        out["error_after_delta"] = sse_fixture(ask(client), events)
    return out


# ---------------------------------------------------------------- the job stream (real route, real registry, constructed dicts)

def _job(job_id: str, state: str, version, **extra) -> dict:
    return {"job_id": job_id, "state": state, "document_id": DOC, "version": version, **extra}


def ready_job_dicts() -> list[dict]:
    j = JOB_READY
    return [_job(j, "received", None), _job(j, "validating", 1), _job(j, "parsing", 1), _job(j, "chunking", 1),
            _job(j, "embedding", 1, progress={"done": 0, "total": 12, "eta_s": None}),
            _job(j, "embedding", 1, progress={"done": 6, "total": 12, "eta_s": 4.5}),
            _job(j, "embedding", 1, progress={"done": 12, "total": 12, "eta_s": 0.0}),
            _job(j, "comparing", 1), _job(j, "indexing", 1),
            _job(j, "ready", 1, chunks=12, units=30, items_compared=False, not_compared_reason="first_version", suspicious=False)]


def failed_job_dicts() -> list[dict]:
    j = JOB_FAILED
    message = jobs.JOB_ERROR_MESSAGES["scanned"]
    return [_job(j, "received", None), _job(j, "validating", 1), _job(j, "parsing", 1),
            _job(j, "failed", 1, error={"code": "scanned", "message": message})]


def record_job(job_id: str, dicts: list[dict]) -> tuple[bytes, dict]:
    with serving() as (client, events, _):
        registry = jobs.registry(client.app)
        registry.create(WS_ID, job_id)
        for job in dicts:
            registry.append(WS_ID, job_id, job)               # the whole log, terminal event included: the stream never sleeps
        response = client.get(f"/api/workspace/{WS_ID}/jobs/{job_id}", headers={"X-Workspace-Token": WS_TOKEN})
        assert [e["state"] for e in events] == [d["state"] for d in dicts]
        return sse_fixture(response, events, JOBS_SOURCE)


# ---------------------------------------------------------------- the pre-stream refusals

def refusal(name: str, response, request: dict) -> dict:
    assert response.headers["content-type"].startswith("application/json"), name
    return {"name": name, "request": request, "status": response.status_code, "detail": response.json()["detail"],
            "retry_after": response.headers.get("retry-after")}


def record_refusals() -> dict:
    found: list[dict] = []
    with serving() as (client, _, _):
        found.append(refusal("bad_strategy", ask(client, strategy="magic"), {"POST": "/api/ask", "strategy": "magic"}))
        found.append(refusal("as_of_without_workspace", ask(client, as_of="2026-06-30"),
                             {"POST": "/api/ask", "as_of": "2026-06-30"}))
    with serving(turnstile_required=True) as (client, _, _):
        found.append(refusal("bot_check_failed", ask(client), {"POST": "/api/ask", "turnstile_required": True}))
    with serving() as (client, _, _):
        client.app.state.mp.setattr(routes, "_workspace_token_ok", lambda driver, ws, token: False)
        found.append(refusal("workspace_not_found", ws_ask(client), {"POST": "/api/ask", "workspace_id": "unknown"}))
    with serving() as (client, _, _):
        client.app.state.mp.setattr(workspace_routes.repo, "get_job", lambda driver, ws, job: None)
        response = client.get(f"/api/workspace/{WS_ID}/jobs/{JOB_READY}", headers={"X-Workspace-Token": WS_TOKEN})
        found.append(refusal("job_not_found", response, {"GET": "/api/workspace/{ws}/jobs/{job_id}"}))
    with serving(rate_limit_questions=1) as (client, _, _):
        install_hybrid(client.app.state.mp, hybrid_inputs(rec.GOOD_PARTS))
        assert ask(client).status_code == 200
        found.append(refusal("rate_limited", ask(client, question=QUESTION + " again"),
                             {"POST": "/api/ask", "second paid ask from one address": True}))
    with serving() as (client, _, backend):
        backend.deny.append(Denied.INFLIGHT)
        found.append(refusal("busy", ask(client), {"POST": "/api/ask", "in_flight_cap_full": True}))
    with serving(backend=FakeStateBackend(kill="on")) as (client, _, _):
        found.append(refusal("paused", ask(client), {"POST": "/api/ask", "kill_level": "on"}))
    with serving(backend=FakeStateBackend(kill="retrieval_only")) as (client, _, _):
        found.append(refusal("retrieval_only", ask(client), {"POST": "/api/ask", "kill_level": "retrieval_only"}))
    with serving() as (client, _, _):
        assert drain.DRAIN.begin()
        found.append(refusal("draining", ask(client), {"POST": "/api/ask", "draining": True}))
    return {"_about": {"what": "the pre-stream JSON refusals of POST /api/ask and the job stream, recorded from the real routes over "
                               "fakes: status, the JSON detail (the MSG_* text, unedited) and the Retry-After header",
                       "source": ROUTE_RECORDED, "regenerate": "RECORD_SSE=1 uv run pytest tests/test_sse_fixtures.py"},
            "refusals": found}


# ---------------------------------------------------------------- the two hand-written fixtures

def frame(event: str, data: object) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode("ascii")


def ping_interleaved() -> tuple[bytes, dict]:
    """sse-starlette's comment ping frames and an ``event: ping`` between the real events: the client must ignore them all."""
    events = [{"event": "retrieval", "anchors": {"Nvidia": 1045810},
               "counts": {"edges": 2, "metrics": 1, "risks": 1, "temporal": 0, "chunks": 1}},
              {"event": "delta", "text": "Nvidia depends on "},
              {"event": "delta", "text": "TSMC."},
              {"event": "done", "answer": "Nvidia depends on TSMC.", "citations": [], "hallucinated": [], "finish_reason": "stop",
               "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": 0.0, "strategy": "hybrid", "question": QUESTION}]
    comment = b": ping - 2026-11-08 00:00:00.000000+00:00\n\n"
    blocks = [frame(e["event"], e) for e in events]
    body = comment + blocks[0] + comment + frame("ping", {}) + blocks[1] + comment + blocks[2] + frame("ping", {}) + comment + blocks[3]
    return body, {"source": HAND_WRITTEN, "events": events,
                  "note": "the comment frames and the two `event: ping` frames carry no event of the stream and are not listed"}


def max_size_done() -> dict:
    """A ``done`` at the largest answer the server allows: 2,400 tokens, the citation list, the id list and the checks."""
    ids = [f"0001045810-26-000021:I.1A:{n:04d}" for n in range(1, 41)]
    sentence = "Nvidia states in this filing that its supply of advanced wafer capacity depends on TSMC [{id}]. "
    answer, n = "", 0
    while len(answer) < MAX_ANSWER_TOKENS * CHARS_PER_TOKEN:
        answer += sentence.format(id=ids[n % len(ids)])
        n += 1
    return {"event": "done", "answer": answer, "citations": ids, "hallucinated": [], "finish_reason": "stop",
            "usage": {"prompt_tokens": 18000, "completion_tokens": MAX_ANSWER_TOKENS}, "cost_usd": 0.0824,
            "chunk_ids": ids, "context_chars": 64000, "strategy": "hybrid", "question": QUESTION,
            "checks": {"citations_retrieved": True, "numbers_grounded": True, "unmatched_numbers": [], "pseudo_citations": [],
                       "echoed_numbers": [], "numbers_checked": 0, "has_citation": True, "is_refusal": False,
                       "removal_claims": []}}


def done_max_size() -> tuple[bytes, dict]:
    done = max_size_done()
    return frame("done", done), {"source": HAND_WRITTEN, "events": [done], "answer_tokens": MAX_ANSWER_TOKENS,
                                 "note": f"the answer is sized at {CHARS_PER_TOKEN} characters per token: a ceiling, not a recording"}


# ---------------------------------------------------------------- everything, as files

def render(document: dict) -> bytes:
    return (json.dumps(document, indent=1) + "\n").encode("utf-8")


def record_all() -> dict[str, bytes]:
    recorded = record_asks()
    recorded["job_ready"] = record_job(JOB_READY, ready_job_dicts())
    recorded["job_failed"] = record_job(JOB_FAILED, failed_job_dicts())
    recorded["ping_interleaved"] = ping_interleaved()
    recorded["done_max_size"] = done_max_size()
    files: dict[str, bytes] = {"refusals.json": render(record_refusals())}
    for name, (body, sidecar) in recorded.items():
        files[f"{name}.sse"] = body
        files[f"{name}.expected.json"] = render(sidecar)
    return files


_CACHE: dict[str, bytes] = {}


@pytest.fixture
def recorded() -> dict[str, bytes]:
    """Recorded once per run, inside the first test that asks (so the no-network guard is active)."""
    files = _CACHE or _CACHE.update(record_all()) or _CACHE
    if RECORDING:
        SSE_DIR.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (SSE_DIR / name).write_bytes(content)       # binary: a CRLF checkout would break the byte test
    return files


# ---------------------------------------------------------------- the tests

def parse_sse(body: bytes) -> list[dict]:
    """An independent reader of the wire (blocks split on a blank line, ``data:`` lines joined): comment frames and
    ``event: ping`` frames carry no stream event."""
    found = []
    for block in body.decode("utf-8").split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if not lines or lines[0] == "event: ping":
            continue
        data = "".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
        found.append(json.loads(data))
    return found


def test_the_directory_holds_exactly_the_documented_fixtures(recorded):
    assert sorted(p.name for p in SSE_DIR.iterdir() if p.name != ".gitkeep") == FIXTURE_FILES


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_a_fixture_is_exactly_what_the_routes_send_now(recorded, name):
    assert recorded[name] == (SSE_DIR / name).read_bytes()


def test_the_recorder_produced_the_documented_list(recorded):
    assert sorted(recorded) == FIXTURE_FILES


@pytest.mark.parametrize("name", SSE_NAMES)
def test_each_sidecar_equals_the_events_a_second_reader_decodes_from_the_bytes(name):
    body = (SSE_DIR / f"{name}.sse").read_bytes()
    sidecar = json.loads((SSE_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))
    assert parse_sse(body) == sidecar["events"], name
    assert sidecar["source"] in {ROUTE_RECORDED, JOBS_SOURCE, HAND_WRITTEN}


@pytest.mark.parametrize("name", SSE_NAMES)
def test_every_fixture_is_ascii_with_lf_only_and_ends_on_a_blank_line(name):
    body = (SSE_DIR / f"{name}.sse").read_bytes()
    body.decode("ascii")
    assert b"\r" not in body and body.endswith(b"\n\n")


def kinds_of(name: str) -> list[str]:
    return [e["event"] if "event" in e else "job" for e in json.loads((SSE_DIR / f"{name}.expected.json").read_text())["events"]]


def events_of(name: str) -> list[dict]:
    return json.loads((SSE_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))["events"]


def test_the_cached_done_is_one_cached_event_with_the_source_the_cache_adds():
    (done,) = events_of("cached_done")
    assert done["event"] == "done" and done["cached"] is True and done["source"] == "live" and "usage" not in done
    assert (SSE_DIR / "cached_done.sse").read_bytes().count(b"event:") == 1


def test_the_live_hybrid_answer_streams_retrieval_deltas_and_a_live_done():
    assert kinds_of("live_hybrid") == ["retrieval", "delta", "delta", "done"]
    done = events_of("live_hybrid")[-1]
    assert "cached" not in done and done["finish_reason"] == "stop" and done["checks"]["citations_retrieved"] is True


def test_the_agent_stream_has_steps_and_a_float_elapsed_time():
    assert kinds_of("agent_steps")[:3] == ["step", "step", "step"] and kinds_of("agent_steps")[-1] == "done"
    elapsed = events_of("agent_steps")[-1]["agent"]["elapsed_s"]
    assert isinstance(elapsed, float) and elapsed > 0


def test_the_escalated_stream_says_so_before_the_strong_models_answer():
    kinds = kinds_of("escalated")
    assert kinds.index("escalated") < kinds.index("delta") and kinds[-1] == "done"
    assert events_of("escalated")[-1]["escalated"] is True


def test_the_workspace_answer_carries_the_workspace_block():
    done = events_of("workspace_done")[-1]
    assert kinds_of("workspace_done")[0] == "retrieval" and set(done["workspace"]) >= {"id_hash", "doc_chunks", "stale_citations", "suspicious"}


def test_an_error_after_the_server_rewrote_it_carries_only_the_generic_detail():
    for name, delta_expected in (("error_before_delta", False), ("error_after_delta", True)):
        kinds = kinds_of(name)
        assert kinds[-1] == "error" and ("delta" in kinds) is delta_expected, name
        assert events_of(name)[-1] == {"event": "error", "detail": stream_runtime.MSG_FAILED}, name
        assert TERMINAL.isdisjoint(kinds[:-1])


def test_the_job_fixtures_walk_the_state_machine_to_a_terminal_state():
    ready, failed = events_of("job_ready"), events_of("job_failed")
    assert [j["state"] for j in ready][:4] == ["received", "validating", "parsing", "chunking"]
    assert ready[-1]["state"] == "ready" and ready[-1]["chunks"] == 12
    assert failed[-1]["state"] == "failed" and failed[-1]["error"]["code"] == "scanned"
    assert all(j["state"] not in jobs.TERMINAL_STATES for j in ready[:-1] + failed[:-1])


def test_the_ping_fixture_interleaves_comment_and_ping_frames_with_the_real_events():
    body = (SSE_DIR / "ping_interleaved.sse").read_bytes()
    assert len(re.findall(rb"(?m)^: ping", body)) == 4 and body.count(b"event: ping") == 2
    assert [e["event"] for e in events_of("ping_interleaved")] == ["retrieval", "delta", "delta", "done"]


def test_the_max_size_done_is_an_answer_of_the_largest_size_the_server_allows():
    done = events_of("done_max_size")[0]
    assert len(done["answer"]) >= MAX_ANSWER_TOKENS * CHARS_PER_TOKEN
    assert done["usage"]["completion_tokens"] == MAX_ANSWER_TOKENS == int(re.search(
        r"LLM_ANSWER_MAX_TOKENS\s*=\s*\"?(\d+)", (HERE.parent / "fly.toml").read_text(encoding="utf-8")).group(1))


def test_the_refusals_carry_the_real_messages_and_the_draining_retry_after():
    doc = json.loads((SSE_DIR / "refusals.json").read_text(encoding="utf-8"))
    by_name = {r["name"]: r for r in doc["refusals"]}
    assert {r["status"] for r in by_name.values()} == {400, 403, 404, 429, 503}
    assert by_name["bot_check_failed"]["detail"] == routes.MSG_BOT and by_name["bot_check_failed"]["status"] == 403
    assert by_name["workspace_not_found"]["detail"] == routes.MSG_WORKSPACE_NOT_FOUND
    assert by_name["rate_limited"]["detail"] == routes.MSG_RATE and by_name["busy"]["detail"] == stream_runtime.MSG_BUSY
    assert by_name["paused"]["detail"] == routes.MSG_PAUSED and by_name["retrieval_only"]["detail"] == routes.MSG_RETRIEVAL_ONLY
    assert by_name["draining"]["detail"] == routes.MSG_DRAINING and by_name["draining"]["retry_after"] == "30"
    assert by_name["job_not_found"]["detail"] == "job not found"
    assert all(r["retry_after"] is None for n, r in by_name.items() if n != "draining")


def test_a_set_of_the_unrecorded_cases_is_named_in_the_module_docstring():
    assert "route-recorded, job dicts constructed" in __doc__ and "hand-written" in __doc__



# ---------------------------------------------------------------- review fixes (I2 fixer)

def test_git_never_converts_the_line_endings_of_a_fixture():
    """No ``.gitattributes`` meant a Windows checkout (core.autocrlf=true) turned the LF fixtures into CRLF and broke the byte
    test. ``-text`` pins them: git neither normalises on commit nor converts on checkout."""
    import subprocess

    repo = HERE.parent
    for name in ("cached_done.sse", "cached_done.expected.json", "refusals.json"):
        out = subprocess.run(["git", "check-attr", "text", "eol", "--", f"tests/data/sse/{name}"], cwd=repo,
                             capture_output=True, text=True, check=True).stdout
        assert "text: unset" in out, f"{name}: {out!r}"


def test_recording_runs_under_the_fixture_server_guards():
    """Settings(), get_settings() and litellm are raisers for the whole recording, so a lazy fallback that would read the
    developer's ``.env`` or reach a local Neo4j fails loudly instead of landing in a committed fixture."""
    from semigraph import config

    (guards,) = ACTIVE_GUARDS           # installed by the autouse fixture: this test does not ask for it
    with pytest.raises(fixture_app.GuardViolation):
        config.get_settings()
    with pytest.raises(fixture_app.GuardViolation):
        config.Settings()
    assert len(guards.hits) == 2
    guards.hits.clear()
