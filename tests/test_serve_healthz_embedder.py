"""/healthz reports which embedder variant is loaded (8-bit or pre-dequantized fp32) and the summary that proves it.

Two NEW fields, ``embedder_variant`` and ``embedder_fidelity``, are added to the body of a healthy answer; ``status``,
``db`` and ``embedder`` are exactly what they were, and a degraded answer (503) is untouched. The route in
``serve/routes.py`` stays as it is: ``serve/main.py`` serves ``/healthz`` through a thin wrapper that calls it and merges the
two fields in, so an app built from ``routes.router`` alone (the other test fixtures) answers the old body. The embedder is
stubbed at the place the real one sits: a ``LimitedEmbedder`` over an ``Embedder`` facade whose ``_impl`` is the backend.
"""

import contextlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from semigraph import embeddings
from semigraph.serve import main, routes
from semigraph.serve.embed import LimitedEmbedder
from semigraph.serve.limiters import make_limiters

FIDELITY = {"nodes": 196, "max_node_abs_diff": 0.0, "cosine_min": 0.9999999998, "cosine_mean": 0.9999999999,
            "source_sha256": "0b2937d85ef4f8ff"}
OLD_FIELDS = {"status": "ok", "db": True}


class StubBackend:
    """What ``Embedder._impl`` is for the onnx backend: a name, a variant and the fidelity summary."""

    def __init__(self, name: str, variant, fidelity):
        self.name, self.variant, self.fidelity = name, variant, fidelity


def wrapped(backend) -> LimitedEmbedder:
    facade = object.__new__(embeddings.Embedder)
    facade._impl, facade.name, facade.backend = backend, getattr(backend, "name", "x"), "onnx"
    return LimitedEmbedder(facade, 1)


def make_client(embedder, monkeypatch):
    @contextlib.asynccontextmanager
    async def lifespan(app):
        app.state.limiters = make_limiters(SimpleNamespace(embed_slots=1, db_thread_limit=2))   # inside the running loop
        yield

    app = main.create_app()
    app.router.lifespan_context = lifespan                                   # the real one connects to Neo4j
    app.state.driver, app.state.embedder = object(), embedder
    monkeypatch.setattr(routes, "run_cypher", lambda driver, query, **params: [{"ok": 1}])
    return TestClient(app)


# ---------------------------------------------------------------- the fields, from the wrapped embedder

def test_the_fp32_variant_and_its_fidelity_summary_are_reported(monkeypatch):
    backend = StubBackend("onnx:model_fp32.onnx", "fp32", FIDELITY)

    with make_client(wrapped(backend), monkeypatch) as client:
        reply = client.get("/healthz")

    assert reply.status_code == 200
    assert reply.json() == {**OLD_FIELDS, "embedder": "onnx:model_fp32.onnx", "embedder_variant": "fp32",
                            "embedder_fidelity": FIDELITY}


def test_the_8_bit_variant_has_no_fidelity_summary(monkeypatch):
    backend = StubBackend("onnx:model_q8.onnx", "q8", None)

    with make_client(wrapped(backend), monkeypatch) as client:
        body = client.get("/healthz").json()

    assert body == {**OLD_FIELDS, "embedder": "onnx:model_q8.onnx", "embedder_variant": "q8", "embedder_fidelity": None}


def test_a_backend_without_a_variant_reports_nulls_and_the_service_still_answers(monkeypatch):
    local = SimpleNamespace(name="Qwen/Qwen3-Embedding-0.6B")                  # the sentence-transformers backend

    with make_client(wrapped(local), monkeypatch) as client:
        body = client.get("/healthz").json()

    assert body == {**OLD_FIELDS, "embedder": "Qwen/Qwen3-Embedding-0.6B", "embedder_variant": None,
                    "embedder_fidelity": None}


def test_the_existing_fields_keep_their_values_and_order(monkeypatch):
    with make_client(wrapped(StubBackend("onnx:model_fp32.onnx", "fp32", FIDELITY)), monkeypatch) as client:
        body = client.get("/healthz").json()

    assert list(body)[:3] == ["status", "db", "embedder"]
    assert body["status"] == "ok" and body["db"] is True and body["embedder"] == "onnx:model_fp32.onnx"


def test_a_degraded_answer_is_unchanged(monkeypatch):
    def down(driver, query, **params):
        raise ConnectionError("neo4j is down")

    with make_client(wrapped(StubBackend("onnx:model_fp32.onnx", "fp32", FIDELITY)), monkeypatch) as client:
        monkeypatch.setattr(routes, "run_cypher", down)
        reply = client.get("/healthz")

    assert reply.status_code == 503 and reply.json() == {"status": "degraded", "db": False}


def test_the_route_module_alone_still_answers_the_old_body(monkeypatch):
    """An app that mounts ``routes.router`` without ``main`` (the other serve fixtures) sees no new fields."""
    @contextlib.asynccontextmanager
    async def lifespan(app):
        app.state.limiters = make_limiters(SimpleNamespace(embed_slots=1, db_thread_limit=2))
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(routes.router)
    app.state.driver = object()
    app.state.embedder = wrapped(StubBackend("onnx:model_fp32.onnx", "fp32", FIDELITY))
    monkeypatch.setattr(routes, "run_cypher", lambda driver, query, **params: [{"ok": 1}])

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {**OLD_FIELDS, "embedder": "onnx:model_fp32.onnx"}


# ---------------------------------------------------------------- the helper

@pytest.mark.parametrize("embedder, expected", [
    (wrapped(StubBackend("n", "fp32", FIDELITY)), ("fp32", FIDELITY)),
    (wrapped(StubBackend("n", "q8", None)), ("q8", None)),
    (StubBackend("n", "fp32", FIDELITY), ("fp32", FIDELITY)),                    # an unwrapped backend works too
    (wrapped(SimpleNamespace(name="n")), (None, None)),
    (None, (None, None)),
    (wrapped(StubBackend("n", 3, "text")), (None, None)),                        # wrong types are not passed through
    (object(), (None, None)),
], ids=["wrapped-fp32", "wrapped-q8", "bare-backend", "no-variant", "no-embedder", "wrong-types", "plain-object"])
def test_embedder_health_fields_never_raise_and_never_pass_through_wrong_types(embedder, expected):
    variant, fidelity = expected

    assert main.embedder_health_fields(embedder) == {"embedder_variant": variant, "embedder_fidelity": fidelity}


def test_the_fidelity_in_the_reply_is_a_copy_not_the_backends_own_dict():
    backend = StubBackend("n", "fp32", dict(FIDELITY))

    reply = main.embedder_health_fields(wrapped(backend))
    reply["embedder_fidelity"]["nodes"] = -1

    assert backend.fidelity["nodes"] == 196
