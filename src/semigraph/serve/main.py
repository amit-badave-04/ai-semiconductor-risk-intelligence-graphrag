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
from ..graph.client import DatabaseDriver, run_cypher
from ..graph.schema import apply_schema
from ..retrieval.answerer import template_fingerprint
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
            driver = DatabaseDriver(GraphDatabase.driver(settings.neo4j_uri,
                                                         auth=(settings.neo4j_user, settings.neo4j_password)),
                                    settings.neo4j_database)
            driver.verify_connectivity()
            logger.info("neo4j reachable at %s", settings.neo4j_uri)
            return driver
        except Exception as e:  # noqa: BLE001
            if time.monotonic() > deadline:
                raise RuntimeError(f"Neo4j unreachable at {settings.neo4j_uri} after {CONNECT_RETRY_S}s") from e
            logger.warning("neo4j not ready (%s) — retrying", type(e).__name__)
            time.sleep(3)


def graph_stats(driver) -> dict:
    """The public /api/stats graph block.

    ``removed_risk_items`` counts ``RiskItem`` nodes whose text was verified absent from a newer filing
    (``removed_in`` set) and that are headline units (real risk factors); ``removed_paragraphs`` counts the removed
    PARAGRAPH units (a 20-F filer's, or a filing with no risk-factor headlines: they are not risk factors and the page says
    so); ``risk_items`` is the total. All three are 0 on a graph built before RiskItems existed.
    The old ``deleted_risk_lineages`` (a count of ``DISCLOSES_RISK`` edges, not lineages) is deliberately gone.
    """
    labels = run_cypher(driver, """MATCH (n) WITH labels(n)[0] AS label, count(*) AS n
        WHERE NOT label STARTS WITH 'Svc' RETURN label, n ORDER BY n DESC""")
    rels = run_cypher(driver, "MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
    nodes = {r["label"]: r["n"] for r in labels}
    risk_items = nodes.get("RiskItem", 0)
    # Only query the label when it exists: an unknown label would log a warning on every start of an older graph.
    removed = (run_cypher(driver, """MATCH (i:RiskItem) WHERE i.removed_in IS NOT NULL
        RETURN coalesce(i.unit_kind, 'headline') AS kind, count(i) AS n""") if risk_items else [])
    by_kind = {r["kind"]: r["n"] for r in removed}
    return {"nodes": nodes, "relationships": rels, "risk_items": risk_items,
            "removed_risk_items": by_kind.get("headline", 0), "removed_paragraphs": by_kind.get("paragraph", 0)}


def bootstrap(settings):
    driver = connect_with_retry(settings)
    apply_schema(driver)              # idempotent — also rebuilds after a dump restore
    store.ensure_indexes(driver)
    snapshot = store.current_snapshot(driver)
    snapshot_id = (snapshot or {}).get("id", "")
    example_ids = seed_examples(driver, load_examples(), snapshot_id)
    embedder = Embedder()
    embedder.encode_query("warm-up: export controls and HBM supply")
    return driver, embedder, graph_stats(driver), snapshot, example_ids


def seed_examples(driver, examples: dict, snapshot_id: str) -> frozenset[str]:
    """Seed the saved example answers into the cache and return the ids that are now served (listed by /api/examples).

    Never raises for a bad examples file: the service must start. An examples file from another data snapshot or another
    answer-prompt template is refused whole, and an example whose stored checks are missing or failed is refused on its
    own; every refusal is logged with its reason, and a refused example is neither cached nor listed (a click on it would
    be a paid live call under a label that says "instant, cached")."""
    if not store.examples_match_snapshot(examples, snapshot_id):
        logger.warning("example answers were generated from another data snapshot than %s — NOT seeded; "
                       "regenerate them with scripts/build_examples.py before publishing", snapshot_id)
        return frozenset()
    if not store.examples_match_template(examples):
        logger.warning("example answers were generated under another answer-prompt template (%s) than this build (%s) — "
                       "NOT seeded; regenerate them with scripts/build_examples.py before publishing",
                       examples.get("template_fingerprint") or "no fingerprint", template_fingerprint())
        return frozenset()
    result = store.seed_examples(driver, examples["examples"], snapshot_id)
    for example_id, reason in result.refused:
        logger.warning("example %s refused: %s", example_id, reason)
    logger.info("seeded %d of %d benchmark answers into the cache (snapshot %s)", len(result.seeded_ids),
                len(examples["examples"]), snapshot_id or "none")
    return frozenset(result.seeded_ids)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.turnstile_required and not settings.turnstile_secret_key:
        logger.error("TURNSTILE_REQUIRED without TURNSTILE_SECRET_KEY: live questions will be refused")
    elif settings.is_production and not settings.turnstile_secret_key:
        logger.warning("production without Turnstile: cost is bounded only by the daily ceiling (%d) and the per-IP window",
                       settings.max_queries_per_day)
    driver, embedder, stats, snapshot, example_ids = await run_in_threadpool(bootstrap, settings)
    app.state.settings = settings
    app.state.driver = driver
    app.state.embedder = embedder
    app.state.graph_stats = stats
    app.state.snapshot = snapshot
    app.state.example_ids = example_ids
    app.state.snapshot_id = (snapshot or {}).get("id", "")
    app.state.rate_limiter = RateLimiter(settings.rate_limit_questions, settings.rate_limit_window_seconds)
    app.state.free_rate_limiter = RateLimiter(settings.free_rate_limit_questions,
                                              settings.rate_limit_window_seconds)
    app.state.read_rate_limiter = RateLimiter(settings.read_rate_limit_per_minute, 60)
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
