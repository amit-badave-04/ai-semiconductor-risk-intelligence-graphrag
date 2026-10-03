"""FastAPI application factory + lifespan (connect, schema, cache seed, embedder warm-up).

    uvicorn semigraph.serve.main:app --host 0.0.0.0 --port 8080
"""

import asyncio
import contextlib
import hashlib
import importlib
import logging
import re
import threading
from urllib.parse import quote, unquote
import time

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from .. import __version__
from ..artifacts import load_examples
from ..config import get_settings
from ..embeddings import Embedder
from ..graph.client import DatabaseDriver, run_cypher
from ..graph.schema import PRIVATE_LABEL_PREFIXES, apply_schema, private_label_predicate
from ..retrieval.answerer import template_fingerprint
from ..uploads import jobs
from .embed import LimitedEmbedder
from .guard import RateLimiter
from .limiters import LoopLagMonitor, make_limiters
from .routes import router
from . import dossier_routes, hardening, monitor, monitor_routes, store, tracing, workspace_routes

SECONDS_PER_DAY = 86_400
SECONDS_PER_HOUR = 3_600

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("semigraph.serve.main")

# The whole path SEGMENT after /api/workspace/ is hashed, whatever its case or length: an uppercased or over-long id
# still carries the real id, so only the segment position (never its shape) decides what is redacted. Repeated
# slashes and any letter case are tolerated too (round-6 verification L4): a malformed spelling is a 404, but its
# access-log line must not carry the raw id either.
_WORKSPACE_PATH_RE = re.compile(r"^/+api/+workspace/+([^/]+)", re.IGNORECASE)
_DOC_ID_IN_PATH_RE = re.compile(r"doc:[0-9a-f]{12}:v[0-9]{1,3}:[0-9]{4}")
# Characters a logged path may carry literally (RFC 3986 path characters plus the <ws:...>/<doc> placeholders); every
# other character, including control characters and quotes, is percent-encoded.
_LOG_SAFE = "/:@!$&()*+,;=-._~<>"
# A kept query string additionally keeps "?" and "%" literal: its existing percent-escapes stay as uvicorn gave them
# (never double-encoded, never decoded), while a raw control character or quote is still percent-encoded.
_LOG_SAFE_QUERY = _LOG_SAFE + "?%"


def ws_hash(workspace_id: str) -> str:
    """The first 12 hex of sha256(workspace_id): what logs may carry instead of the id (docs/v2/M4_PLAN.md 5)."""
    return hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:12]


def redact_access_path(path: str) -> str:
    """An access-log path with no raw workspace id, no uploaded-document id and no workspace query string. uvicorn
    logs the PERCENT-QUOTED path (``doc%3A...``), so the route is decoded for MATCHING only; what is returned is
    re-escaped (:data:`_LOG_SAFE`), so a client can never write a newline, an escape sequence or a quote into the log.
    A query that carries a document id is dropped whole; any other query is logged with its existing escapes kept and
    every other unsafe character re-escaped (:data:`_LOG_SAFE_QUERY`)."""
    route, _, query = path.partition("?")
    route = unquote(route)
    match = _WORKSPACE_PATH_RE.match(route)
    if match:
        redacted = _DOC_ID_IN_PATH_RE.sub("<doc>", f"/api/workspace/<ws:{ws_hash(match.group(1))}>" + route[match.end():])
        return quote(redacted, safe=_LOG_SAFE)
    route = quote(_DOC_ID_IN_PATH_RE.sub("<doc>", route), safe=_LOG_SAFE)
    if not query or _DOC_ID_IN_PATH_RE.search(unquote(query)):
        return route
    return f"{route}?{quote(query, safe=_LOG_SAFE_QUERY)}"


class WorkspaceAccessLogFilter(logging.Filter):
    """Rewrites uvicorn's access-log record (args = client, method, path, http version, status) with
    :func:`redact_access_path`; never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_access_path(args[2]), *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(WorkspaceAccessLogFilter())

CONNECT_RETRY_S = 90  # the database machine may still be booting after START
AGENT_MODULE = "semigraph.agent.stream"
TRACER_SHUTDOWN_TIMEOUT_S = 3  # a hung Langfuse endpoint must not delay teardown (or the driver close after it) indefinitely


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

    ``removed_risk_items`` counts ``RiskItem`` nodes for which the text check found no matching text in a newer filing
    (``removed_in`` set) and that are headline units (real risk factors); ``removed_paragraphs`` counts the same for
    PARAGRAPH units (a 20-F filer's, or a filing with no risk-factor headlines: they are not risk factors and the page says
    so); ``risk_items`` is the total. All three are 0 on a graph built before RiskItems existed.
    The key names are the API contract and stay; the PAGE words the two counts as "risk factors / paragraphs that no longer
    stand alone (text check)", because held-out gold showed that such an item is gone as a standalone risk factor but that
    some (2 of 4 checked) were merged into another risk factor rather than dropped: the counts are not "verified removals".
    The old ``deleted_risk_lineages`` (a count of ``DISCLOSES_RISK`` edges, not lineages) is deliberately gone.
    """
    # Private state (Svc* service state, User* upload workspaces) is never counted: the public numbers must not reveal that a
    # workspace exists. A private node never links to a public one, so filtering on the start node is exact.
    private_first = " OR ".join(f"label STARTS WITH '{p}'" for p in PRIVATE_LABEL_PREFIXES)
    labels = run_cypher(driver, f"""MATCH (n) WITH labels(n)[0] AS label, count(*) AS n
        WHERE NOT ({private_first}) RETURN label, n ORDER BY n DESC""")
    rels = run_cypher(driver, f"MATCH (a)-[r]->() WHERE NOT {private_label_predicate('a')} RETURN count(r) AS n")[0]["n"]
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


def require_agent_package() -> None:
    """Import the agent at boot when ``AGENT_ENABLED``: a missing langgraph must stop the service HERE, not on the first agent
    question (and not after the Neo4j start-up retry either: this runs before ``bootstrap``)."""
    try:
        importlib.import_module(AGENT_MODULE)
    except Exception:
        logger.error("AGENT_ENABLED is set but %s cannot be imported: install the optional 'agent' extra "
                     "(pip install '.[agent]', which brings langgraph) or unset AGENT_ENABLED", AGENT_MODULE)
        raise


def shutdown_tracer(tracer) -> None:
    """Flush and stop the tracing client (the only blocking flush: a request never waits for it). Never raises."""
    try:
        getattr(tracer, "shutdown", lambda: None)()
    except Exception:  # noqa: BLE001 - a failing flush must not keep the database driver open
        logger.exception("tracer shutdown failed")


def shutdown_tracer_bounded(tracer) -> None:
    """``shutdown_tracer`` on a daemon thread, joined with a short timeout: a hung Langfuse endpoint (network stall on
    ``flush``/``shutdown``) must not delay teardown past ``TRACER_SHUTDOWN_TIMEOUT_S``, and must never keep
    ``driver.close()`` (called right after, in the lifespan's ``finally``) from running. The thread is a daemon and is
    simply abandoned if it does not finish in time — it does not stop the process from exiting, and ``shutdown_tracer``
    itself never raises, so there is nothing left to join later."""
    thread = threading.Thread(target=shutdown_tracer, args=(tracer,), daemon=True)
    thread.start()
    thread.join(TRACER_SHUTDOWN_TIMEOUT_S)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # FIRST, before any thread or the upload parse subprocess exists: a same-uid child must not be able to read this
    # process's secrets through /proc (docs/v2/M4_PLAN.md 5; a no-op off Linux).
    if hardening.make_process_non_dumpable():
        logger.info("process marked non-dumpable (its /proc entries are root-only)")
    settings = get_settings()
    if settings.agent_enabled:
        require_agent_package()
    if settings.turnstile_required and not settings.turnstile_secret_key:
        logger.error("TURNSTILE_REQUIRED without TURNSTILE_SECRET_KEY: live questions will be refused")
    elif settings.is_production and not settings.turnstile_secret_key:
        logger.warning("production without Turnstile: cost is bounded only by the daily ceiling (%d) and the per-IP window",
                       settings.max_queries_per_day)
    if settings.uploads_enabled and settings.is_production and not settings.turnstile_secret_key:
        logger.error("UPLOADS_ENABLED in production without TURNSTILE_SECRET_KEY: uploads stay unavailable "
                     "(the upload routes fail closed)")
    driver, embedder, stats, snapshot, example_ids = await run_in_threadpool(bootstrap, settings)
    # A no-op unless all three langfuse settings are set (only then is langfuse imported, hence off the event loop).
    app.state.tracer = await run_in_threadpool(tracing.get_tracer, settings)
    app.state.settings = settings
    app.state.driver = driver
    # ONE bound on concurrent query embeddings for every caller, plus a bounded cache of query vectors (serve/embed.py).
    app.state.embedder = LimitedEmbedder(embedder, settings.embed_slots)
    app.state.limiters = make_limiters(settings)      # must be built inside the running loop (this lifespan)
    app.state.graph_stats = stats
    app.state.snapshot = snapshot
    app.state.example_ids = example_ids
    app.state.snapshot_id = (snapshot or {}).get("id", "")
    app.state.rate_limiter = RateLimiter(settings.rate_limit_questions, settings.rate_limit_window_seconds)
    app.state.free_rate_limiter = RateLimiter(settings.free_rate_limit_questions,
                                              settings.rate_limit_window_seconds)
    app.state.read_rate_limiter = RateLimiter(settings.read_rate_limit_per_minute, 60)
    app.state.answer_slots = threading.BoundedSemaphore(settings.max_concurrent_answers)
    # M4 upload gates (docs/v2/M4_PLAN.md 4.4 and 5): per-address windows and ONE upload at a time on the machine
    # (embedding never takes an answer slot).
    app.state.workspace_create_limiter = RateLimiter(settings.workspace_create_per_day, SECONDS_PER_DAY)
    app.state.upload_limiter = RateLimiter(settings.uploads_per_hour, SECONDS_PER_HOUR)
    app.state.upload_slots = threading.BoundedSemaphore(1)
    # Background services, each only when its flag is on (freshness monitor, workspace TTL sweeper).
    monitor.start_if_enabled(app)
    jobs.start_if_enabled(app)
    app.state.loop_lag = LoopLagMonitor(settings.loop_lag_warn_ms)
    lag_task = asyncio.create_task(app.state.loop_lag.run())
    logger.info("semigraph %s serving — graph: %s", __version__, stats)
    yield
    lag_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await lag_task
    try:
        await run_in_threadpool(stop_background_services, app)
        await run_in_threadpool(shutdown_tracer_bounded, app.state.tracer)
    finally:
        driver.close()


def stop_background_services(app) -> None:
    """Stop the monitor and the sweeper before the driver closes (each bounded). Never raises: a failing stop must not keep
    the database driver open."""
    for name, stop in (("freshness monitor", monitor.stop), ("upload sweeper", jobs.stop)):
        try:
            stop(app)
        except Exception:  # noqa: BLE001
            logger.exception("stopping the %s failed", name)


def create_app() -> FastAPI:
    app = FastAPI(title="semigraph", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.include_router(router)
    for extra in (monitor_routes.router, dossier_routes.router, workspace_routes.router):   # M4
        app.include_router(extra)
    return app


app = create_app()
