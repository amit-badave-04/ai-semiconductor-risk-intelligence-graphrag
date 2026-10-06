"""Neo4j-persisted service state: policy flags, the query ledger, the answer cache.

Everything here survives machine restarts and auto-stops. Labels are
prefixed with ``Svc`` so they never collide with the knowledge-graph schema:

- ``(:SvcPolicy {key, value, updated_at})``  — kill_switch: ``on`` | ``retrieval_only`` | ``off``
- ``(:SvcQuery {id, day, ip_hash, ip_hash_v, strategy, cached, prompt_tokens,
  completion_tokens, cost_usd, created_at})`` — one row per answered question; ``ip_hash_v`` is the id of the
  pepper that made ``ip_hash`` (absent on rows written before the pepper; 0 once such a hash was nulled). A cached
  answer's row is written here (``log_query``); a PAID ask's row is the ``reserved`` row of ``serve.state`` (M5a I4: it
  adds ``status``, ``outcome``, ``estimate_micro``, ``cost_micro``, ``lease_until``, ``machine_id``, ...) and is settled
  by ``reconcile``, never written by ``log_query``
- ``(:SvcAnswer {key, question, strategy, answer, citations, hallucinated,
  usage_prompt, usage_completion, cost_usd, source, created_at})`` — cache
- ``(:SvcUploadDay {day, n, updated_at})`` — uploads accepted that UTC day, all workspaces (M4 ``MAX_UPLOADS_PER_DAY``)
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from neo4j import Driver

from ..graph.client import NO_UNRECOGNIZED_NOTIFICATIONS, run_cypher
from ..retrieval.answerer import template_fingerprint
from ..retrieval.verify import failed_check_names
from .state import ledger as state_ledger


def cache_key(question: str, strategy: str, snapshot_id: str = "", template: str | None = None) -> str:
    """Answer-cache key. Two things besides the question are part of it: the data snapshot id (a refresh of the graph can
    never serve an answer computed from older data) and the fingerprint of the answer prompt + context headers
    (:func:`semigraph.retrieval.answerer.template_fingerprint`; a prompt or template change can never replay an answer
    written under the old one). ``template`` defaults to the running build's."""
    norm = " ".join(question.lower().split()).rstrip("?.! ")
    prefix = f"{snapshot_id}|" if snapshot_id else ""
    return hashlib.sha256(f"{template or template_fingerprint()}|{prefix}{strategy}|{norm}".encode()).hexdigest()[:32]


def current_snapshot(driver: Driver) -> dict | None:
    """The newest ``Snapshot`` node of the served graph: ``{"id", "as_of"}`` or None."""
    rows = run_cypher(driver, """MATCH (s:Snapshot) RETURN s.id AS id, toString(s.as_of) AS as_of
        ORDER BY s.created_at DESC LIMIT 1""")
    return rows[0] if rows else None


def examples_match_snapshot(examples_doc: dict, snapshot_id: str) -> bool:
    """Seeded example answers were generated from ONE graph; they are served as cached
    answers only when the service runs that same snapshot (a file without a snapshot label
    predates versioning and is stale against any snapshot-stamped graph)."""
    if not snapshot_id:
        return True
    return examples_doc.get("snapshot_id") == snapshot_id


def examples_match_template(examples_doc: dict) -> bool:
    """Seeded answers were written under ONE answer prompt + context template; they are served as cached answers only
    by a build that runs that same template (a file without the fingerprint predates it and is refused)."""
    return examples_doc.get("template_fingerprint") == template_fingerprint()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


# ---- policy ---------------------------------------------------------------

def get_policy(driver: Driver, key: str) -> str | None:
    rows = run_cypher(driver, "MATCH (p:SvcPolicy {key: $key}) RETURN p.value AS v", key=key)
    return rows[0]["v"] if rows else None


def set_policy(driver: Driver, key: str, value: str) -> None:
    """Store a policy value. The kill switch holds a level: ``on`` | ``retrieval_only`` | ``off``. A value is not
    validated here (the reader fails closed on one it does not know: ``serve.state.backend.StateCore._parse_policy``
    reads it as ``on``); the admin route validates what it accepts.

    ROLLBACK: a pre-M5 image reads only ``on`` as stopped (:func:`kill_switch_on`), so it reads ``retrieval_only`` as
    ``off`` and would accept paid questions. Before rolling back to such an image, set the level to ``on`` or ``off``
    (the RUNBOOK rule; ``scripts/kill_switch.py`` refuses to store ``retrieval_only`` directly without a flag saying
    so)."""
    run_cypher(driver, "MERGE (p:SvcPolicy {key: $key}) SET p.value = $value, p.updated_at = $ts",
               key=key, value=value, ts=_now())


def kill_switch_on(driver: Driver, env_flag: bool) -> bool:
    """The pre-M5 reading of the kill switch (True only for ``on``); the serving path now asks ``serve.state`` backend
    ``kill_level()`` instead, which also stops on ``retrieval_only``, an unread level and a stale one. Kept for any
    caller of the old stored flag."""
    return env_flag or get_policy(driver, "kill_switch") == "on"


# ---- ledger ---------------------------------------------------------------

def paid_queries_today(driver: Driver) -> int:
    rows = run_cypher(driver, "MATCH (q:SvcQuery {day: $day, cached: false}) RETURN count(q) AS n",
                      day=_today())
    return rows[0]["n"]


def log_query(driver: Driver, *, ip_hash: str, strategy: str, cached: bool,
              usage: dict | None = None, cost_usd: float | None = None, workspace: bool = False,
              ip_hash_v: int | None = None) -> None:
    """One ledger row per answered question. ``workspace`` marks an ask over an upload workspace (M4): it counts against
    the same daily ceiling; the row never names the workspace. ``ip_hash_v`` is the id of the pepper that made
    ``ip_hash``; it is written on the row only when given (a row without it predates the pepper, see
    :func:`null_legacy_ip_hashes`)."""
    usage = usage or {}
    query = """CREATE (q:SvcQuery {id: $id, day: $day, ip_hash: $ip, strategy: $strategy,
               cached: $cached, prompt_tokens: $pt, completion_tokens: $ct, cost_usd: $cost,
               workspace: $workspace, created_at: $ts})"""
    params = dict(id=str(uuid.uuid4()), day=_today(), ip=ip_hash, strategy=strategy, cached=cached,
                  pt=usage.get("prompt_tokens"), ct=usage.get("completion_tokens"), cost=cost_usd, workspace=workspace,
                  ts=_now())
    if ip_hash_v is not None:
        query += "\n               SET q.ip_hash_v = $ipv"
        params["ipv"] = ip_hash_v
    run_cypher(driver, query, **params)


# A ledger row is "legacy" when it has an ip_hash but no ip_hash_v: the hash is the unsalted SHA-256 written before the
# pepper (docs/v2/M5_DECISIONS.md 1.4 item 4, decision 12), which the whole IPv4 space can brute-force.
_LEGACY_IP_HASH = "q.ip_hash IS NOT NULL AND q.ip_hash_v IS NULL"
COUNT_LEGACY_IP_HASHES = f"MATCH (q:SvcQuery) WHERE {_LEGACY_IP_HASH} RETURN count(q) AS n"
# ``CALL (q) {...} IN TRANSACTIONS`` commits every ``batch`` rows and cannot run inside an explicit transaction: it is
# sent as an auto-commit statement (``run_cypher``). The batch size is a validated int written into the text, as
# ``schema.reset_graph`` does with its own batch size.
NULL_LEGACY_IP_HASHES = (f"MATCH (q:SvcQuery) WHERE {_LEGACY_IP_HASH}\n"
                         "CALL (q) {{ SET q.ip_hash = null, q.ip_hash_v = 0 }} IN TRANSACTIONS OF {batch} ROWS")


def count_legacy_ip_hashes(driver: Driver) -> int:
    """How many ledger rows still carry an unsalted hash: what :func:`null_legacy_ip_hashes` would null (the dry
    run)."""
    rows = run_cypher(driver, COUNT_LEGACY_IP_HASHES, session_config_=NO_UNRECOGNIZED_NOTIFICATIONS)
    return rows[0]["n"]


def null_legacy_ip_hashes(driver: Driver, batch: int = 5000) -> int:
    """Null the unsalted ``ip_hash`` of every legacy ledger row and mark it ``ip_hash_v = 0``; returns how many rows
    it nulled. IRREVERSIBLE: the per-address history of those rows is gone (owner decision 12, 2026-10-03). Rows
    written with a pepper (``ip_hash_v`` set) are never touched, and running it again nulls nothing. Commits ``batch``
    rows at a time; a failure part-way leaves earlier batches nulled and is cured by running it again."""
    if type(batch) is not int or batch < 1:
        raise ValueError("batch must be a positive integer")
    before = count_legacy_ip_hashes(driver)
    if before == 0:
        return 0
    run_cypher(driver, NULL_LEGACY_IP_HASHES.format(batch=batch), session_config_=NO_UNRECOGNIZED_NOTIFICATIONS)
    return max(before - count_legacy_ip_hashes(driver), 0)


def reserve_daily_upload(driver: Driver, limit: int) -> bool:
    """Take one of today's ``limit`` upload slots (global, every workspace); False when the day is already full.

    Atomic under concurrency: the first ``SET`` takes the counter node's write lock BEFORE ``c.n`` is read, so two uploads
    racing for the last slot cannot both see ``n < limit`` (the lost-update pattern). ``SvcUploadDay.day`` is unique
    (:func:`ensure_indexes`), so ``MERGE`` cannot create two counters for one day."""
    if limit <= 0:
        return False
    rows = run_cypher(driver, """MERGE (c:SvcUploadDay {day: $day}) ON CREATE SET c.n = 0
        SET c._lock = true
        WITH c WHERE c.n < $limit
        SET c.n = c.n + 1, c.updated_at = $ts
        RETURN c.n AS n""", day=_today(), limit=limit, ts=_now())
    return bool(rows)


def ledger_summary(driver: Driver) -> dict:
    """Today (index-backed) and all-time (label scan, bounded by the write gates
    and cached by the stats endpoint) counts + estimated spend."""
    out = {"today": {"paid": 0, "cached": 0, "cost_usd": 0.0},
           "all_time": {"paid": 0, "cached": 0, "cost_usd": 0.0}}
    today = run_cypher(driver, """MATCH (q:SvcQuery {day: $day})
        RETURN q.cached AS cached, count(q) AS n, sum(coalesce(q.cost_usd, 0.0)) AS cost""",
                       day=_today())
    all_time = run_cypher(driver, """MATCH (q:SvcQuery)
        RETURN q.cached AS cached, count(q) AS n, sum(coalesce(q.cost_usd, 0.0)) AS cost""")
    for scope, rows in (("today", today), ("all_time", all_time)):
        for r in rows:
            out[scope]["cached" if r["cached"] else "paid"] += r["n"]
            out[scope]["cost_usd"] = round(out[scope]["cost_usd"] + (r["cost"] or 0.0), 4)
    return out


# ---- answer cache ---------------------------------------------------------

def get_answer(driver: Driver, key: str, ttl_hours: int) -> dict | None:
    rows = run_cypher(driver, """MATCH (a:SvcAnswer {key: $key})
        WHERE a.source = 'benchmark' OR datetime(a.created_at) > datetime() - duration({hours: $ttl})
        RETURN a.question AS question, a.strategy AS strategy, a.answer AS answer,
               a.citations AS citations, a.hallucinated AS hallucinated, a.source AS source,
               a.created_at AS created_at""", key=key, ttl=ttl_hours)
    return rows[0] if rows else None


def put_answer(driver: Driver, *, question: str, strategy: str, answer: str,
               citations: list[str], hallucinated: list[str], usage: dict | None = None,
               cost_usd: float | None = None, source: str = "live", snapshot_id: str = "") -> None:
    usage = usage or {}
    run_cypher(driver, """MERGE (a:SvcAnswer {key: $key})
        SET a.question = $question, a.strategy = $strategy, a.answer = $answer,
            a.citations = $citations, a.hallucinated = $hallucinated, a.source = $source,
            a.usage_prompt = $pt, a.usage_completion = $ct, a.cost_usd = $cost, a.created_at = $ts""",
               key=cache_key(question, strategy, snapshot_id), question=question, strategy=strategy,
               answer=answer, citations=citations, hallucinated=hallucinated, source=source,
               pt=usage.get("prompt_tokens"), ct=usage.get("completion_tokens"), cost=cost_usd, ts=_now())


_CHECKS_KEYS = ("citations_retrieved", "numbers_grounded", "has_citation")   # the shape ``answer_checks`` reports


@dataclass(frozen=True)
class SeedResult:
    """``seeded_ids``: the examples now in the cache; ``refused``: ``(id, reason)`` for every one that was not."""

    seeded_ids: tuple[str, ...]
    refused: tuple[tuple[str, str], ...]


def refusal_reason(example: dict) -> str | None:
    """Why an example may not be seeded: its stored ``checks`` (computed by ``scripts/build_examples.py`` with the same
    ``answer_checks`` the service runs) are missing, incomplete, or report a failure. None when it may."""
    checks = example.get("checks")
    if not isinstance(checks, dict) or not checks:
        return "no stored checks (regenerate the examples with scripts/build_examples.py)"
    if any(key not in checks for key in _CHECKS_KEYS):
        return "incomplete stored checks (regenerate the examples with scripts/build_examples.py)"
    failed = failed_check_names(checks)
    return f"failed checks: {', '.join(failed)}" if failed else None


def seed_examples(driver: Driver, examples: list[dict], snapshot_id: str = "") -> SeedResult:
    """Pre-load the benchmarked hybrid answers so example clicks are free, EXCEPT any whose stored checks are missing or
    failed: a cached replay carries no checks of its own, so an example that was never checked (or failed) would look clean
    for as long as it is served. Refused examples are returned with their reason (the caller logs them)."""
    seeded: list[str] = []
    refused: list[tuple[str, str]] = []
    for ex in examples:
        reason = refusal_reason(ex)
        if reason:
            refused.append((str(ex.get("id")), reason))
            continue
        put_answer(driver, question=ex["question"], strategy="hybrid", answer=ex["answer"],
                   citations=ex["citations"], hallucinated=ex["hallucinated"], source="benchmark",
                   snapshot_id=snapshot_id)
        seeded.append(ex["id"])
    return SeedResult(tuple(seeded), tuple(refused))


def ensure_indexes(driver: Driver) -> None:
    """Idempotent schema for the service labels. The first release created plain
    indexes named svc_policy_key / svc_answer_key; they are replaced by uniqueness
    constraints under new names (Neo4j refuses a constraint whose name an index owns). The statements of the state
    package (``serve.state.ledger.STATE_SCHEMA_STATEMENTS``: the unique ledger id, day counter and per-address day, and
    the status indexes) run last, so they exist before the first paid ask is served."""
    for stmt in ("DROP INDEX svc_policy_key IF EXISTS",
                 "DROP INDEX svc_answer_key IF EXISTS",
                 "CREATE CONSTRAINT svc_policy_key_unique IF NOT EXISTS FOR (p:SvcPolicy) REQUIRE p.key IS UNIQUE",
                 "CREATE CONSTRAINT svc_answer_key_unique IF NOT EXISTS FOR (a:SvcAnswer) REQUIRE a.key IS UNIQUE",
                 "CREATE INDEX svc_query_day IF NOT EXISTS FOR (q:SvcQuery) ON (q.day)",
                 "CREATE CONSTRAINT svc_upload_day_unique IF NOT EXISTS FOR (u:SvcUploadDay) REQUIRE u.day IS UNIQUE",
                 # M4 freshness monitor: one lease and one result node per key, even on two machines' first MERGE
                 "CREATE CONSTRAINT svc_lease_key_unique IF NOT EXISTS FOR (l:SvcLease) REQUIRE l.key IS UNIQUE",
                 "CREATE CONSTRAINT svc_freshness_key_unique IF NOT EXISTS "
                 "FOR (f:SvcFreshness) REQUIRE f.key IS UNIQUE",
                 *state_ledger.STATE_SCHEMA_STATEMENTS):
        run_cypher(driver, stmt)


def serialize(obj) -> str:
    return json.dumps(obj, default=str)
