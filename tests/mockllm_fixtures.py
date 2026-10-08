"""Shared test doubles for the mock LLM tests (``tools/mockllm``): real prompts, a live mock on loopback, helpers.

Used by ``test_tools_mockllm.py`` and ``test_latency_smoke.py``. No provider, no database; the only socket is the loopback
listener of :class:`LiveMock`.
"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")      # no remote cost-map fetch when litellm loads

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
import uvicorn  # noqa: E402
from agent_fakes import edge_row, metric_rows, risk_row, rule_row, temporal_rows  # noqa: E402

from semigraph.config import get_settings  # noqa: E402
from semigraph.retrieval.answerer import CITE_RE, build_blocks, render_prompt, sources_from_context  # noqa: E402
from semigraph.retrieval.verify import verify_answer  # noqa: E402
from tools.mockllm import server  # noqa: E402

CONTEXTS = json.loads((ROOT / "tests" / "data" / "mockllm_contexts.json").read_text(encoding="utf-8"))["contexts"]
ADMIN = "mock-admin-token-for-tests-" + "z" * 12      # gitleaks:allow


class Case(NamedTuple):
    question: str
    prompt: str
    valid_ids: set
    context: str
    sources: dict


def build_case(question: str, chunks: list[dict], *, with_graph: bool = True) -> Case:
    """The real prompt for ``question`` over ``chunks`` (``chunk_id`` + ``text``), with every other block filled."""
    r = {"anchors": {"NVDA": "0001045810"}, "chunks": chunks, "edges": [edge_row(), rule_row()] if with_graph else [],
         "metrics": metric_rows() if with_graph else [], "risks": [risk_row(1)] if with_graph else [],
         "temporal": temporal_rows() if with_graph else []}
    blocks, context, valid_ids = build_blocks(r)
    return Case(question, render_prompt(question, blocks), valid_ids, context, sources_from_context(context))


def recorded_cases() -> list[Case]:
    return [build_case(c["question"], [{"chunk_id": k["chunk_id"], "text": k["text"]} for k in c["chunks"]])
            for c in CONTEXTS]


def make_app(**env):
    base = {"MOCKLLM_TIME_SCALE": "0", "MOCKLLM_SEED": "7", "MOCKLLM_ADMIN_TOKEN": ADMIN, "MOCKLLM_INVALID_ID_RATE": "0"}
    return server.create_app(env={**base, **{k: str(v) for k, v in env.items()}})


def chat_body(prompt: str, model: str = "mock-luna", **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": prompt}], "max_completion_tokens": 1200, **extra}


def text_of(response) -> str:
    return response.json()["choices"][0]["message"]["content"]


def verdict(case: Case, text: str, finish: str = "stop") -> list[str]:
    return verify_answer(text, set(CITE_RE.findall(text)), case.valid_ids, finish, case.context, sources=case.sources,
                         question=case.question)


def parse_sse(raw: str) -> list:
    events = []
    for block in raw.split("\n\n"):
        if block.startswith("data: "):
            payload = block[6:]
            events.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return events


UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


def snapshot_loggers(names=UVICORN_LOGGERS) -> dict:
    """The named loggers' level, handlers, propagation, disabled flag and filters, as copies."""
    snapshot = {}
    for name in names:
        lg = logging.getLogger(name)
        snapshot[name] = (lg.level, list(lg.handlers), lg.propagate, lg.disabled, list(lg.filters))
    return snapshot


def restore_loggers(snapshot: dict) -> None:
    for name, (level, handlers, propagate, disabled, filters) in snapshot.items():
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.handlers[:] = handlers
        lg.propagate, lg.disabled = propagate, disabled
        lg.filters[:] = filters


class LiveMock:
    """The app on a real socket (127.0.0.1, an ephemeral port) in a thread, so the real LiteLLM can talk to it.

    ``uvicorn.Config(log_level="error")`` rewrites the uvicorn loggers for the whole process (``uvicorn.error`` to ERROR, handlers
    on ``uvicorn`` / ``uvicorn.access``, ``uvicorn`` no longer propagating), so every later test that listens on ``uvicorn.error``
    (the drain's warnings, tests/test_serve_drain.py) would silently hear nothing. The mock therefore builds its ``Config`` only
    when it is entered, after a snapshot of the loggers, and puts them back when it exits, or when it fails to start.
    """

    def __init__(self, app):
        self.app = app
        self._loggers_before: dict | None = None
        self.server = self.thread = None

    def __enter__(self):
        self._loggers_before = snapshot_loggers()
        try:
            config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
            self.server = uvicorn.Server(config)
            self.thread = threading.Thread(target=self.server.run, daemon=True)
            self.thread.start()
            deadline = time.monotonic() + 15
            while not self.server.started:
                if time.monotonic() > deadline or not self.thread.is_alive():
                    raise RuntimeError("the live mock did not start")
                time.sleep(0.02)
            self.port = self.server.servers[0].sockets[0].getsockname()[1]
        except BaseException:
            self._stop()
            raise
        return self

    def __exit__(self, *exc):
        self._stop()

    def _stop(self) -> None:
        """Stop the server (if it was built) and leave the process's loggers as they were before ``__enter__``."""
        try:
            if self.server is not None:
                self.server.should_exit = True
            if self.thread is not None and self.thread.is_alive():
                self.thread.join(timeout=10)
        finally:
            if self._loggers_before is not None:
                restore_loggers(self._loggers_before)
                self._loggers_before = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture
def live(monkeypatch):
    """A live mock whose address ``OPENAI_API_BASE`` names. ``get_settings()`` is cached for the process, and the planner reads
    its ``api_base`` from it: a ``Settings`` cached while THIS mock's variables were set would send a later test's model call to
    a port nobody listens on any more. So the cache is emptied once the variables are set and again when the test is over
    (before ``monkeypatch`` puts the environment back, which nothing builds a ``Settings`` between)."""
    with LiveMock(make_app()) as mock:
        monkeypatch.setenv("OPENAI_API_BASE", mock.base + "/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "mock-key-for-tests")        # gitleaks:allow
        get_settings.cache_clear()
        try:
            yield mock
        finally:
            get_settings.cache_clear()


def metrics_of(mock: LiveMock) -> dict:
    import httpx
    return httpx.get(mock.base + "/metrics", timeout=10).json()
