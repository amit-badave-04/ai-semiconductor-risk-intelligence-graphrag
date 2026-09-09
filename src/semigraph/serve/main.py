"""FastAPI application factory + lifespan (connect, schema, cache seed, embedder warm-up).

    uvicorn semigraph.serve.main:app --host 0.0.0.0 --port 8080
"""

import contextlib
import logging
import threading
import time

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from .. import __version__
from ..artifacts import load_examples
from ..config import get_settings
from ..embeddings import Embedder
from ..graph.client import run_cypher
from ..graph.schema import apply_schema
from .guard import RateLimiter
from .routes import router
from . import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("semigraph.serve.main")

CONNECT_RETRY_S = 90  # the database machine may still be booting after START


def connect_with_retry(settings):
    """Neo4j driver with a startup grace window (the DB machine boots alongside us)."""
    from neo4j import GraphDatabase

    deadline = time.monotonic() + CONNECT_RETRY_S
    while True:
        try:
            driver = GraphDatabase.driver(settings.neo4j_uri,
                                          auth=(settings.neo4j_user, settings.neo4j_password))
            driver.verify_connectivity()
            logger.info("neo4j reachable at %s", settings.neo4j_uri)
            return driver
        except Exception as e:  # noqa: BLE001
            if time.monotonic() > deadline:
                raise RuntimeError(f"Neo4j unreachable at {settings.neo4j_uri} after {CONNECT_RETRY_S}s") from e
            logger.warning("neo4j not ready (%s) — retrying", type(e).__name__)
            time.sleep(3)


def graph_stats(driver) -> dict:
    labels = run_cypher(driver, """MATCH (n) WITH labels(n)[0] AS label, count(*) AS n
        WHERE NOT label STARTS WITH 'Svc' RETURN label, n ORDER BY n DESC""")
    rels = run_cypher(driver, "MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
    deleted = run_cypher(driver, "MATCH ()-[d:DISCLOSES_RISK {status:'Deleted'}]->() RETURN count(d) AS n")[0]["n"]
    return {"nodes": {r["label"]: r["n"] for r in labels}, "relationships": rels,
            "deleted_risk_lineages": deleted}


def bootstrap(settings):
    driver = connect_with_retry(settings)
    apply_schema(driver)              # idempotent — also rebuilds after a dump restore
    store.ensure_indexes(driver)
    n = store.seed_examples(driver, load_examples()["examples"])
    logger.info("seeded %d benchmark answers into the cache", n)
    embedder = Embedder()
    embedder.encode_query("warm-up: export controls and HBM supply")
    return driver, embedder, graph_stats(driver)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.is_production and not settings.turnstile_secret_key:
        logger.warning("production without Turnstile: cost is bounded only by the daily ceiling (%d) and the per-IP window",
                       settings.max_queries_per_day)
    driver, embedder, stats = await run_in_threadpool(bootstrap, settings)
    app.state.settings = settings
    app.state.driver = driver
    app.state.embedder = embedder
    app.state.graph_stats = stats
    app.state.rate_limiter = RateLimiter(settings.rate_limit_questions, settings.rate_limit_window_seconds)
    app.state.free_rate_limiter = RateLimiter(settings.free_rate_limit_questions,
                                              settings.rate_limit_window_seconds)
    app.state.answer_slots = threading.BoundedSemaphore(settings.max_concurrent_answers)
    logger.info("semigraph %s serving — graph: %s", __version__, stats)
    yield
    driver.close()


def create_app() -> FastAPI:
    app = FastAPI(title="semigraph", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.include_router(router)
    return app


app = create_app()
