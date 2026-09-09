"""Neo4j-persisted service state: policy flags, the query ledger, the answer cache.

Everything here survives machine restarts and auto-stops. Labels are
prefixed with ``Svc`` so they never collide with the knowledge-graph schema:

- ``(:SvcPolicy {key, value, updated_at})``  — kill_switch
- ``(:SvcQuery {id, day, ip_hash, strategy, cached, prompt_tokens,
  completion_tokens, cost_usd, created_at})`` — one row per answered question
- ``(:SvcAnswer {key, question, strategy, answer, citations, hallucinated,
  usage_prompt, usage_completion, cost_usd, source, created_at})`` — cache
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime

from neo4j import Driver

from ..graph.client import run_cypher


def cache_key(question: str, strategy: str) -> str:
    norm = " ".join(question.lower().split()).rstrip("?.! ")
    return hashlib.sha256(f"{strategy}|{norm}".encode()).hexdigest()[:32]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


# ---- policy ---------------------------------------------------------------

def get_policy(driver: Driver, key: str) -> str | None:
    rows = run_cypher(driver, "MATCH (p:SvcPolicy {key: $key}) RETURN p.value AS v", key=key)
    return rows[0]["v"] if rows else None


def set_policy(driver: Driver, key: str, value: str) -> None:
    run_cypher(driver, "MERGE (p:SvcPolicy {key: $key}) SET p.value = $value, p.updated_at = $ts",
               key=key, value=value, ts=_now())


def kill_switch_on(driver: Driver, env_flag: bool) -> bool:
    return env_flag or get_policy(driver, "kill_switch") == "on"


# ---- ledger ---------------------------------------------------------------

def paid_queries_today(driver: Driver) -> int:
    rows = run_cypher(driver, "MATCH (q:SvcQuery {day: $day, cached: false}) RETURN count(q) AS n",
                      day=_today())
    return rows[0]["n"]


def log_query(driver: Driver, *, ip_hash: str, strategy: str, cached: bool,
              usage: dict | None = None, cost_usd: float | None = None) -> None:
    usage = usage or {}
    run_cypher(driver, """CREATE (q:SvcQuery {id: $id, day: $day, ip_hash: $ip, strategy: $strategy,
               cached: $cached, prompt_tokens: $pt, completion_tokens: $ct, cost_usd: $cost,
               created_at: $ts})""",
               id=str(uuid.uuid4()), day=_today(), ip=ip_hash, strategy=strategy, cached=cached,
               pt=usage.get("prompt_tokens"), ct=usage.get("completion_tokens"), cost=cost_usd, ts=_now())


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
               cost_usd: float | None = None, source: str = "live") -> None:
    usage = usage or {}
    run_cypher(driver, """MERGE (a:SvcAnswer {key: $key})
        SET a.question = $question, a.strategy = $strategy, a.answer = $answer,
            a.citations = $citations, a.hallucinated = $hallucinated, a.source = $source,
            a.usage_prompt = $pt, a.usage_completion = $ct, a.cost_usd = $cost, a.created_at = $ts""",
               key=cache_key(question, strategy), question=question, strategy=strategy,
               answer=answer, citations=citations, hallucinated=hallucinated, source=source,
               pt=usage.get("prompt_tokens"), ct=usage.get("completion_tokens"), cost=cost_usd, ts=_now())


def seed_examples(driver: Driver, examples: list[dict]) -> int:
    """Pre-load the benchmarked hybrid answers so example clicks are free."""
    for ex in examples:
        put_answer(driver, question=ex["question"], strategy="hybrid", answer=ex["answer"],
                   citations=ex["citations"], hallucinated=ex["hallucinated"], source="benchmark")
    return len(examples)


def ensure_indexes(driver: Driver) -> None:
    """Idempotent schema for the service labels. The first release created plain
    indexes named svc_policy_key / svc_answer_key; they are replaced by uniqueness
    constraints under new names (Neo4j refuses a constraint whose name an index owns)."""
    for stmt in ("DROP INDEX svc_policy_key IF EXISTS",
                 "DROP INDEX svc_answer_key IF EXISTS",
                 "CREATE CONSTRAINT svc_policy_key_unique IF NOT EXISTS FOR (p:SvcPolicy) REQUIRE p.key IS UNIQUE",
                 "CREATE CONSTRAINT svc_answer_key_unique IF NOT EXISTS FOR (a:SvcAnswer) REQUIRE a.key IS UNIQUE",
                 "CREATE INDEX svc_query_day IF NOT EXISTS FOR (q:SvcQuery) ON (q.day)"):
        run_cypher(driver, stmt)


def serialize(obj) -> str:
    return json.dumps(obj, default=str)
