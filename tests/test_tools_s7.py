"""tools/s7: spike S7, Neo4j service state under sustained load (M5a I5; docs/v2/M5_DECISIONS.md section 3).

A Neo4j-only replay: no API, no LLM. The replay drives the REAL functions of the service (the state backend and its ledger
Cypher, ``store.get_answer`` / ``put_answer`` / ``log_query``, ``hybrid_retrieve`` with the question vector handed in and an
embedder that fails if it is ever called, and the real maintenance thread for the kill-level refresh, the lease renewals and the
sweep) against a fake driver that records every statement. Nothing here touches a database; the throwaway-server run is opt-in
(``S7_ITEST_URI``) and skipped by default.

Sections: the pre-registered limits and the mix; open-loop arrivals; the recorder and the gates; one level against the fake
driver (what each ask sends, who is counted, what an error does to the ledger); the vectors; the host guard; no Settings;
packaging. The report and the evidence reader have their own file, ``tests/test_tools_s7_report.py``.
"""

import ast
import json
import math
import os
import re
import sys
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_fakes import FakeDriver  # noqa: E402

from semigraph import config as config_module  # noqa: E402
from semigraph.config import Settings  # noqa: E402
from semigraph.graph.client import STATE_OP_TIMEOUT_DEFAULT_S  # noqa: E402
from semigraph.retrieval import retriever as retriever_module  # noqa: E402
from semigraph.retrieval import router as router_module  # noqa: E402
from semigraph.serve import limiters, store  # noqa: E402
from semigraph.serve.state import ledger  # noqa: E402
from tools.s7 import gates, limits, mix, plan, record, replay, vectors  # noqa: E402

S7_DIR = ROOT / "tools" / "s7"


# =====================================================================================================================
# the pre-registered limits and the mix
# =====================================================================================================================

def test_the_limits_are_the_ones_pre_registered_in_the_decisions_file():
    assert (limits.PRE_STREAM_P95_MS, limits.PRE_STREAM_P99_MS, limits.WRITES_P95_MS) == (50.0, 200.0, 100.0)
    assert limits.RETRIEVAL_P95_FACTOR == 1.5 and limits.BASELINE_LIVE_RATE == 0.5
    assert limits.ERROR_RATE_MAX == pytest.approx(0.001)
    assert limits.JUDGED_WINDOW_S == 1800 and limits.LEVEL_MINUTES == 60
    assert limits.LEVELS == (5, 10, 20) and limits.REQUIRED_LEVEL == 5
    decisions = (ROOT / "docs" / "v2" / "M5_DECISIONS.md").read_text(encoding="utf-8")
    section = decisions[decisions.index("**S7: Neo4j service state"):decisions.index("**S2: one performance-class")]
    for text in ("p95 <= 50 ms", "p99 <= 200 ms", "p95 <= 100 ms", "1.5 x", "errors <= 0.1%", "last 30 minutes", "Levels 5, 10 and 20"):
        assert text in section, text


def test_the_w3_schedule_adds_up_to_the_quoted_hours():
    minutes = {p.name: p.minutes for p in mix.PHASES.values()}
    assert minutes == {"baseline": 15, "soak": 120, "L5": 60, "control": 30, "L10": 60, "L20": 60}
    assert mix.SEED_MINUTES + sum(minutes.values()) == 365                    # 6.08 h: the 6.1 h of the W3 quote
    assert mix.SEED_MINUTES + sum(minutes[n] for n in ("baseline", "soak", "L5")) == 215        # the L5-only 3.6 h
    assert mix.PHASES["baseline"].live_rate == limits.BASELINE_LIVE_RATE
    assert [mix.PHASES[f"L{n}"].live_rate for n in limits.LEVELS] == [5, 10, 20]
    assert mix.PHASES["control"].mix == "today" and mix.PHASES["control"].live_rate == 5
    assert all(p.judged_s == limits.JUDGED_WINDOW_S for p in mix.PHASES.values() if p.name in ("L5", "L10", "L20", "control"))


def test_the_m5a_mix_and_todays_control_are_the_documented_ones():
    assert mix.M5A.live_pre == ("cache_read", "reserve", "retrieval") and mix.M5A.live_post == ("settle", "cache_put")
    assert mix.M5A.cached == ("cache_read", "cached_log") and mix.M5A.leases is True
    assert mix.TODAY.live_pre == ("cache_read", "policy_read", "count_read", "retrieval")
    assert mix.TODAY.live_post == ("paid_log", "cache_put") and mix.TODAY.leases is False
    assert mix.CACHED_PER_LIVE == pytest.approx(45 / 55)                         # "about 0.8 per live ask"
    assert set(mix.MIXES) == {"m5a", "today"}


def test_every_op_of_every_mix_belongs_to_a_gated_group_or_is_reported_only():
    gated = {op for ops in limits.GROUPS.values() for op in ops}
    for m in mix.MIXES.values():
        for op in (*m.live_pre, *m.live_post, *m.cached):
            assert op in gated, op
    assert set(limits.GROUPS) == {"pre_stream", "writes", "retrieval"}
    assert "retrieval" not in limits.GROUPS["writes"] and "reserve" in limits.GROUPS["pre_stream"]


def test_arrivals_are_open_loop_seeded_and_at_the_asked_rates():
    events = list(mix.arrivals(live_rate=20.0, cached_rate=20.0 * mix.CACHED_PER_LIVE, duration_s=200.0, seed=7))
    times = [t for t, _ in events]
    assert times == sorted(times) and 0 <= times[0] and times[-1] < 200.0
    live = sum(kind == "live" for _, kind in events)
    cached = len(events) - live
    assert abs(live - 4000) < 4 * math.sqrt(4000) and abs(cached - 3273) < 4 * math.sqrt(3273)
    assert events == list(mix.arrivals(20.0, 20.0 * mix.CACHED_PER_LIVE, 200.0, 7))
    assert events != list(mix.arrivals(20.0, 20.0 * mix.CACHED_PER_LIVE, 200.0, 8))
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert statistics_cv(gaps) > 0.8                                             # exponential gaps (cv 1), not a metronome


def statistics_cv(values):
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values)) / mean


def test_a_zero_rate_has_no_events_and_a_negative_one_is_refused():
    assert list(mix.arrivals(0.0, 0.0, 10.0, 1)) == []
    with pytest.raises(ValueError):
        list(mix.arrivals(-1.0, 0.0, 10.0, 1))


# =====================================================================================================================
# the recorder and the gates
# =====================================================================================================================

def test_the_recorder_writes_one_compact_line_per_sample_and_reads_them_back(tmp_path):
    rec = record.Recorder(tmp_path / "samples.jsonl")
    rec.add(1.5, "reserve", 12.345678, 11.0, "ok")
    rec.add(2.5, "retrieval", 80.0, 79.0, "err:ServiceUnavailable")
    rec.close()
    assert rec.n == 2
    lines = (tmp_path / "samples.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == [1.5, "reserve", 12.346, 11.0, "ok"]
    assert record.read_samples(tmp_path / "samples.jsonl")[1] == (2.5, "retrieval", 80.0, 79.0, "err:ServiceUnavailable")


def test_the_recorder_is_thread_safe_and_a_torn_last_line_is_skipped(tmp_path):
    rec = record.Recorder(tmp_path / "s.jsonl")

    def worker(k):
        for i in range(200):
            rec.add(float(i), "cache_read", 1.0, 1.0, "ok")

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    rec.close()
    assert len(record.read_samples(tmp_path / "s.jsonl")) == 1600
    with open(tmp_path / "s.jsonl", "a", encoding="utf-8") as handle:
        handle.write('[1.0, "reserve", 3.')
    assert len(record.read_samples(tmp_path / "s.jsonl")) == 1600


def test_the_state_gate_has_four_slots_and_refuses_after_its_wait_exactly_like_slot_call():
    gate = gates.Gate(4)
    assert all(gate.acquire(0.01) for _ in range(4))
    started = time.perf_counter()
    assert gate.acquire(0.05) is False and 0.04 <= time.perf_counter() - started < 0.5
    gate.release()
    assert gate.acquire(0.05) is True
    with pytest.raises(ValueError):
        gates.Gate(0)


def test_an_unbounded_wait_blocks_until_a_slot_frees():
    gate = gates.Gate(1)
    gate.acquire(None)
    threading.Timer(0.1, gate.release).start()
    started = time.perf_counter()
    assert gate.acquire(None) is True and time.perf_counter() - started >= 0.08


# =====================================================================================================================
# a level against the fake driver
# =====================================================================================================================

EXAMPLES = [{"question": f"Example question {i}?"} for i in range(4)]
POOL = [{"id": f"P{i}", "q": q} for i, q in enumerate(["Which companies does Nvidia depend on?",
                                                       "What was Nvidia's total revenue for fiscal 2025?",
                                                       "Which suppliers does AMD rely on for wafers?"])]
VECTORS = {p["id"]: [0.5] * 8 for p in POOL}
SNAPSHOT, TEMPLATE = "snap-test", "tpl-test"


class Tx:
    def __init__(self, world):
        self.world = world

    def run(self, cypher, params=None, **kw):
        return self.world.handle(getattr(cypher, "text", cypher), {**(params or {}), **kw})


class FakeSession:
    def __init__(self, world):
        self.world = world

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        return self.world.handle(getattr(query, "text", query), params)

    def execute_write(self, fn):
        assert getattr(fn, "timeout", 0) > 0, "a managed transaction must carry a server-side timeout"
        return fn(Tx(self.world))

    execute_read = execute_write


class FakeNeo4j:
    """Answers the retriever's queries from ``FakeDriver.world()`` and every state / store statement from in-memory dicts."""

    def __init__(self, *, delay_s=0.0, fail=None):
        self.retrieval = FakeDriver.world()
        self.delay_s, self.fail = delay_s, fail or (lambda text: None)
        self.lock = threading.Lock()
        self.statements, self.rows, self.answers, self.policy, self.schema = [], {}, {}, "off", []
        self.ledger_text = {name: getattr(ledger, name) for name in dir(ledger) if name.isupper() and isinstance(getattr(ledger, name), str)}

    def session(self, **config):
        return FakeSession(self)

    def seed_examples(self):
        for example in EXAMPLES:
            self.answers[store.cache_key(example["question"], "hybrid", SNAPSHOT, TEMPLATE)] = {"answer": "cached"}

    def count(self, constant):
        return sum(1 for text, _ in self.statements if text == self.ledger_text[constant])

    def handle(self, text, params):
        with self.lock:
            self.statements.append((text, params))
        if self.delay_s:
            time.sleep(self.delay_s)
        error = self.fail(text)
        if error:
            raise error
        if text in self.retrieval._names:
            return self.retrieval.answer(text, params)
        with self.lock:
            return self._state(text, params)

    def _state(self, text, params):
        L = self.ledger_text
        if text in (L["RESERVE_ROW"], L["RESERVE_COUNTED"]):
            self.rows[params["id"]] = {"status": "reserved", "cached": False, "ip": params["ip_hash"]}
            return [{"id": params["id"]}]
        if text in (L["SETTLE_ROW"], L["SETTLE_COUNTED"]):
            row = self.rows.get(params["id"])
            if row and row["status"] == "reserved":
                row["status"] = "settled"
                return [{"id": params["id"]}]
            return []
        if text == L["RENEW_ROW"]:
            return [{"id": params["id"]}] if self.rows.get(params["id"], {}).get("status") == "reserved" else []
        if text == L["EXPIRED_LEASES"]:
            return []
        if text == L["EXPIRE_RESERVED"]:
            return [{"n": 0}]
        if text == L["DAY_SUMS"]:
            return [{"paid": 0, "spend_micro": 0, "foreign": 0}]
        if text == L["IP_ROWS"]:
            return []
        if "MATCH (a:SvcAnswer {key: $key})" in text:
            hit = self.answers.get(params["key"])
            return [dict(hit, question="q", strategy="hybrid", citations=[], hallucinated=[], source="benchmark",
                         created_at="2026-10-08")] if hit else []
        if "MERGE (a:SvcAnswer {key: $key})" in text:
            self.answers[params["key"]] = {"answer": params["answer"]}
            return []
        if "MATCH (p:SvcPolicy {key: $key})" in text:
            return [{"v": self.policy}]
        if text.startswith("MATCH (q:SvcQuery {day: $day, cached: false}) RETURN count(q) AS n"):
            return [{"n": sum(1 for r in self.rows.values() if not r["cached"])}]
        if "CREATE (q:SvcQuery {id: $id, day: $day, ip_hash: $ip" in text:
            self.rows[params["id"]] = {"status": None, "cached": params["cached"], "ip": params["ip"]}
            return []
        if text.startswith("MATCH (q:SvcQuery) RETURN count(q)"):
            return [{"n": len(self.rows)}]
        if text.startswith("MATCH (a:SvcAnswer) RETURN count(a)"):
            return [{"n": len(self.answers)}]
        if "a.key IN $keys" in text:
            return [{"key": key} for key in params["keys"] if key in self.answers]
        if text.startswith(("CREATE CONSTRAINT", "CREATE INDEX", "DROP INDEX")):
            self.schema.append(text)
            return []
        if "q.ip_hash STARTS WITH $prefix" in text:
            return self._verify(params["prefix"])
        if "dbms.queryJmx" in text:
            return [{"up": 5000}]
        if text.strip().startswith("RETURN 1"):
            return [{"ok": 1}]
        raise AssertionError(f"the fake does not know this statement: {text[:100]!r}")

    def _verify(self, prefix):
        out = {}
        for row in self.rows.values():
            if row["ip"].startswith(prefix):
                key = (row["status"], row["cached"])
                out[key] = out.get(key, 0) + 1
        return [{"status": s, "cached": c, "n": n} for (s, c), n in out.items()]


def level_config(**kwargs):
    base = dict(phase="L5", mix="m5a", backend="inprocess", live_rate=25.0, duration_s=1.2, judged_s=0.6, hold_s=0.25,
                kill_refresh_s=0.15, lease_renew_s=0.1, lease_ttl_s=5.0, drain_s=10.0, probe_every_s=0.3, workers=16,
                seed=3, run_id="s7-test")
    return replay.LevelConfig(**{**base, **kwargs})


def inputs():
    return replay.ReplayInputs(pool=POOL, vectors=VECTORS, examples=EXAMPLES, snapshot_id=SNAPSHOT, template=TEMPLATE)


@pytest.fixture
def fake():
    world = FakeNeo4j()
    world.seed_examples()
    return world


def run_level(fake, tmp_path, **cfg):
    out = tmp_path / cfg.get("phase", "L5")
    result = replay.run_level(level_config(**cfg), read_driver=fake, state_driver=fake, inputs=inputs(), out_dir=out)
    return result, out


def ops_of(out):
    counts = {}
    for _, op, _, _, status in record.read_samples(out / "samples.jsonl"):
        counts.setdefault(op, {}).setdefault(status, 0)
        counts[op][status] += 1
    return counts


def test_a_level_sends_what_the_mix_says_and_every_ask_is_counted(fake, tmp_path):
    result, out = run_level(fake, tmp_path)
    live, cached = result["asks"]["live_started"], result["asks"]["cached_started"]
    assert live >= 10 and cached >= 8
    counts = ops_of(out)
    assert {op: sum(v.values()) for op, v in counts.items() if op in ("cache_read", "reserve", "retrieval", "settle", "cache_put",
                                                                      "cached_log")} == {
        "cache_read": live + cached, "reserve": live, "retrieval": live, "settle": live, "cache_put": live, "cached_log": cached}
    assert all(set(v) == {"ok"} for v in counts.values()), counts
    assert fake.count("RESERVE_ROW") == live and fake.count("SETTLE_ROW") == live
    assert result["errors"] == 0 and result["denied"] == 0
    assert sum(counts["renew"].values()) >= 1 and sum(counts["kill_read"].values()) >= 2 and sum(counts["sweep"].values()) >= 1


def test_retrieval_is_the_real_hybrid_retrieve_with_the_vector_handed_in_and_an_embedder_that_fails_if_called(fake, tmp_path):
    result, out = run_level(fake, tmp_path)
    live = result["asks"]["live_started"]
    per_ask = len(fake.retrieval.calls) / live
    assert per_ask == int(per_ask) and per_ask >= 5                           # "about 5" reads: counted, not assumed
    assert result["statements"]["retrieval"]["per_ask"] == pytest.approx(per_ask)
    assert result["errors"] == 0                                              # NoEmbedder.encode_query would have been an error
    with pytest.raises(AssertionError):
        replay.NoEmbedder().encode_query("x")


def test_a_cached_ask_is_a_cache_hit_and_a_logged_row_and_a_live_ask_is_a_miss(fake, tmp_path):
    result, out = run_level(fake, tmp_path)
    reads = [(t, p) for t, p in fake.statements if "MATCH (a:SvcAnswer {key: $key})" in t]
    keys = {store.cache_key(e["question"], "hybrid", SNAPSHOT, TEMPLATE) for e in EXAMPLES}
    hits = [p for _, p in reads if p["key"] in keys]
    assert len(hits) == result["asks"]["cached_started"] and len(reads) == len(hits) + result["asks"]["live_started"]
    logged = [p for t, p in fake.statements if "CREATE (q:SvcQuery {id: $id, day: $day, ip_hash: $ip" in t]
    assert len(logged) == result["asks"]["cached_started"] and all(p["cached"] is True for p in logged)
    live_keys = {p["key"] for _, p in reads if p["key"] not in keys}
    assert len(live_keys) == result["asks"]["live_started"]                   # every live question is salted: a miss, never a hit


def test_live_questions_are_salted_distinct_and_keep_the_pool_questions_vector(fake, tmp_path):
    run_level(fake, tmp_path)
    puts = [p for t, p in fake.statements if "MERGE (a:SvcAnswer {key: $key})" in t]
    assert len({p["key"] for p in puts}) == len(puts)


def test_the_ledger_is_verified_after_the_drain_and_nothing_is_lost(fake, tmp_path):
    result, out = run_level(fake, tmp_path)
    rows = json.loads((out / "ledger_rows.json").read_text(encoding="utf-8"))
    assert rows["paid_rows"] == result["asks"]["live_started"] and rows["reserved_rows"] == 0
    assert rows["cached_rows"] == result["asks"]["cached_started"]
    assert result["expected"] == {"paid_rows": rows["paid_rows"], "cached_rows": rows["cached_rows"]}
    assert rows["prefix"].startswith("s7:s7-test")


def test_level_json_records_the_config_the_clock_and_never_a_password(fake, tmp_path):
    result, out = run_level(fake, tmp_path)
    meta = json.loads((out / "level.json").read_text(encoding="utf-8"))
    assert meta["phase"] == "L5" and meta["mix"] == "m5a" and meta["live_rate"] == 25.0 and meta["judged_s"] == 0.6
    assert meta["wall_end"] > meta["wall_start"] > 1.7e9 and meta["config"]["state_slots"] == 4 and meta["config"]["db_slots"] == 32
    assert "password" not in json.dumps(meta).lower() and meta["driver"]["cpu_max"] >= 0
    assert (out / "driver.jsonl").is_file() and (out / "server.jsonl").is_file()
    probes = [json.loads(line) for line in (out / "server.jsonl").read_text(encoding="utf-8").splitlines()]
    assert probes and all(p["ok"] for p in probes) and probes[0]["uptime_ms"] == 5000


def test_the_neo4j_backend_uses_the_counted_statements(fake, tmp_path):
    result, out = run_level(fake, tmp_path, backend="neo4j")
    assert fake.count("RESERVE_COUNTED") == result["asks"]["live_started"] and fake.count("RESERVE_ROW") == 0
    assert fake.count("SETTLE_COUNTED") == result["asks"]["live_started"]
    assert result["errors"] == 0 and result["denied"] == 0


def test_todays_control_has_no_reserve_no_settle_no_lease_and_the_old_reads(fake, tmp_path):
    result, out = run_level(fake, tmp_path, phase="control", mix="today")
    live, cached = result["asks"]["live_started"], result["asks"]["cached_started"]
    counts = {op: sum(v.values()) for op, v in ops_of(out).items()}
    assert counts == {"cache_read": live + cached, "policy_read": live, "count_read": live, "retrieval": live,
                      "paid_log": live, "cache_put": live, "cached_log": cached}
    assert fake.count("RESERVE_ROW") == 0 and fake.count("SETTLE_ROW") == 0 and fake.count("RENEW_ROW") == 0
    rows = json.loads((out / "ledger_rows.json").read_text(encoding="utf-8"))
    assert rows["paid_rows"] == live and rows["cached_rows"] == cached


def test_a_failing_retrieval_still_settles_the_lease_and_is_counted_as_an_error(tmp_path):
    seen = {"n": 0}
    world = FakeNeo4j()
    world.seed_examples()
    names = {text: name for text, name in world.retrieval._names.items()}

    def fail_excerpts(text):
        if names.get(text) == "excerpts":
            seen["n"] += 1
            return RuntimeError("excerpts blew up")

    world.fail = fail_excerpts
    result, out = run_level(world, tmp_path)
    assert seen["n"] >= 1 and result["errors"] >= seen["n"]
    counts = ops_of(out)
    assert counts["retrieval"].get("err:RuntimeError", 0) == seen["n"]
    assert sum(counts["settle"].values()) == result["asks"]["live_started"]            # the lease was not leaked
    rows = json.loads((out / "ledger_rows.json").read_text(encoding="utf-8"))
    assert rows["reserved_rows"] == 0 and rows["paid_rows"] == result["asks"]["live_started"]


def memory_error():
    """What the driver raises for a server memory error: a ``TransientError`` whose ``code`` is the server's."""
    from neo4j.exceptions import TransientError
    kind = type("TransientError", (TransientError,), {"code": "Neo.TransientError.General.MemoryPoolOutOfMemoryError"})
    return kind("SECRET-MARKER 203.0.113.9 the question text")


def test_a_server_memory_error_reaches_the_status_the_report_reads_never_its_message(tmp_path):
    """The state package wraps every driver error in ``StateUnavailable`` and retrieval lets it through: either way the status
    must carry the SERVER's code, or an out-of-memory in the middle of a level would look like any other failure."""
    from tools.s7 import evidence
    names = {text: name for text, name in FakeDriver.world()._names.items()}
    seen = {"retrieval": 0, "cache": 0}

    def fail(text):
        if names.get(text) == "excerpts" and seen["retrieval"] < 2:
            seen["retrieval"] += 1
            return memory_error()
        if "MATCH (a:SvcAnswer {key: $key})" in text and seen["cache"] < 2:
            seen["cache"] += 1
            return memory_error()

    world = FakeNeo4j(fail=fail)
    world.seed_examples()
    result, out = run_level(world, tmp_path)
    statuses = {s[4] for s in record.read_samples(out / "samples.jsonl")}
    code = "Neo.TransientError.General.MemoryPoolOutOfMemoryError"
    assert f"err:TransientError:{code}" in statuses                       # raised straight through retrieval
    assert f"err:StateUnavailable:{code}" in statuses                     # wrapped by the state package, the cause's code kept
    assert not any("SECRET-MARKER" in s or "203.0.113.9" in s or "question text" in s for s in statuses)
    verdict = evidence.out_of_memory(evidence.Evidence(sample_statuses=tuple(sorted(statuses))), 0.0, 1.0)
    assert verdict.passed is False
    assert result["errors"] >= 4


def test_an_exception_without_a_server_code_keeps_its_plain_status_and_a_hostile_code_is_not_copied():
    from tools.s7 import flows
    assert flows.failure_status(RuntimeError("x")) == "err:RuntimeError"
    error = RuntimeError("x")
    error.code = "Neo.ClientError.Statement.SyntaxError; DROP DATABASE"
    assert flows.failure_status(error) == "err:RuntimeError"
    error.code = "Neo.ClientError.Statement.SyntaxError"
    assert flows.failure_status(error) == "err:RuntimeError:Neo.ClientError.Statement.SyntaxError"


def test_a_level_records_the_size_of_the_ledger_and_the_cache_it_started_and_ended_on(fake, tmp_path):
    fake.rows["old-1"] = {"status": "settled", "cached": False, "ip": "somebody-else"}
    result, out = run_level(fake, tmp_path)
    counts = result["table_counts"]
    assert counts["start"] == {"svc_query": 1, "svc_answer": len(EXAMPLES)}
    assert counts["end"]["svc_query"] == 1 + result["asks"]["live_started"] + result["asks"]["cached_started"]
    assert counts["end"]["svc_answer"] == len(EXAMPLES) + result["asks"]["live_started"]


def test_a_denied_reserve_is_an_error_not_a_silent_skip(tmp_path):
    world = FakeNeo4j()
    world.seed_examples()
    result, out = run_level(world, tmp_path, max_inflight=1, hold_s=0.5, live_rate=40.0)
    assert result["denied"] >= 1 and result["errors"] >= result["denied"]
    assert any(status.startswith("denied:") for status in ops_of(out)["reserve"])


def test_the_state_slot_wait_is_part_of_what_the_route_sees_and_a_full_gate_is_a_noslot_error(tmp_path):
    world = FakeNeo4j(delay_s=0.04)
    world.seed_examples()
    result, out = run_level(world, tmp_path, state_slots=1, state_op_timeout_s=0.05, live_rate=40.0, duration_s=1.0, workers=24)
    statuses = {s for per_op in ops_of(out).values() for s in per_op}
    assert "noslot" in statuses and result["errors"] >= 1
    rows = json.loads((out / "ledger_rows.json").read_text(encoding="utf-8"))
    assert rows["reserved_rows"] == 0                                                     # settles wait as long as it takes


def test_latency_is_measured_from_the_scheduled_start_so_a_queue_is_visible(tmp_path):
    world = FakeNeo4j(delay_s=0.03)
    world.seed_examples()
    result, out = run_level(world, tmp_path, workers=2, live_rate=30.0, duration_s=1.0, drain_s=30.0)
    first = [s for s in record.read_samples(out / "samples.jsonl") if s[1] == "cache_read"]
    assert max(s[2] - s[3] for s in first) > 100.0                      # total minus execution: the time waiting for a worker
    assert result["lag"]["p99_s"] > 0.1


def test_a_missing_seeded_example_is_refused_before_the_first_ask(tmp_path):
    world = FakeNeo4j()                                                 # not seeded
    with pytest.raises(replay.NotPrepared):
        replay.run_level(level_config(), read_driver=world, state_driver=world, inputs=inputs(), out_dir=tmp_path / "x")
    assert not (tmp_path / "x" / "samples.jsonl").exists() or (tmp_path / "x" / "samples.jsonl").read_text(encoding="utf-8") == ""


def test_the_shipped_examples_seed_and_every_example_a_level_asks_for_is_cached(tmp_path):
    """A failure that would cost a window: ``prepare`` seeds what ``seed_examples`` accepts (an example whose stored checks
    failed is refused), so the cached asks must be drawn from exactly those, or the pre-check would refuse the level."""
    from semigraph.retrieval.answerer import template_fingerprint
    doc = json.loads((ROOT / "src" / "semigraph" / "artifacts" / "examples.json").read_text(encoding="utf-8"))
    template = template_fingerprint()
    assert doc["template_fingerprint"] == template                          # else the service refuses to serve them too
    world = FakeNeo4j()
    seeded = replay.prepare_examples(world, doc, doc["snapshot_id"], template=template)
    asked = replay.seedable_examples(doc)
    assert seeded == len(asked) >= 1
    assert all(store.refusal_reason(example) is None for example in asked)
    inputs = replay.ReplayInputs(pool=POOL, vectors=VECTORS, examples=asked, snapshot_id=doc["snapshot_id"], template=template)
    result = replay.run_level(level_config(duration_s=0.6, judged_s=0.3, hold_s=0.1), read_driver=world, state_driver=world,
                              inputs=inputs, out_dir=tmp_path / "L5")
    assert result["errors"] == 0 and result["asks"]["cached_started"] >= 1
    if len(asked) < len(doc["examples"]):                                   # the refused ones would NOT be cached
        refused = next(e for e in doc["examples"] if store.refusal_reason(e))
        with pytest.raises(replay.NotPrepared):
            replay.run_level(level_config(), read_driver=world, state_driver=world, out_dir=tmp_path / "x",
                             inputs=replay.ReplayInputs(pool=POOL, vectors=VECTORS, examples=[refused], snapshot_id=doc["snapshot_id"],
                                                        template=template))


def test_prepare_creates_the_schema_and_seeds_the_examples_it_is_given(tmp_path):
    world = FakeNeo4j()
    examples_doc = {"snapshot_id": SNAPSHOT, "template_fingerprint": TEMPLATE, "examples": [
        {"id": f"E{i}", "question": e["question"], "answer": "a", "citations": [], "hallucinated": [],
         "checks": {"citations_retrieved": True, "numbers_grounded": True, "has_citation": True}} for i, e in enumerate(EXAMPLES)]}
    seeded = replay.prepare_examples(world, examples_doc, SNAPSHOT, template=TEMPLATE)
    assert seeded == len(EXAMPLES) and len(world.answers) == len(EXAMPLES)
    # the schema the service creates at boot, state package included: S7 must time the indexed database live runs on
    assert set(ledger.STATE_SCHEMA_STATEMENTS) <= set(world.schema)
    assert any("svc_answer_key_unique" in statement for statement in world.schema)
    assert replay.prepare_examples(world, {**examples_doc, "snapshot_id": "snap-other"}, SNAPSHOT, template=TEMPLATE) == 0
    assert replay.prepare_examples(world, {**examples_doc, "template_fingerprint": "tpl-other"}, SNAPSHOT, template=TEMPLATE) == 0


# =====================================================================================================================
# the vectors and the host guard
# =====================================================================================================================

class _Embedder:
    def __init__(self, dim=8):
        self.dim = dim

    def encode_query(self, text):
        return [float(len(text)), 0.25] + [0.0] * (self.dim - 2)


def test_vectors_are_built_with_an_injected_embedder_keyed_by_pool_id_and_validated_on_load(tmp_path):
    out = tmp_path / "vectors.json"
    pool = {"live": POOL, "sha256": "abc"}
    doc = vectors.build(pool, out, _Embedder(), dim=8)
    assert doc["count"] == 3 and doc["dim"] == 8 and doc["pool_sha256"] == "abc"
    loaded = vectors.load(out, dim=8, pool_sha256="abc")
    assert set(loaded) == {"P0", "P1", "P2"} and loaded["P1"][0] == float(len(POOL[1]["q"]))
    with pytest.raises(ValueError):
        vectors.load(out, dim=1024)


def test_vectors_refuse_a_file_built_for_another_pool(tmp_path):
    out = tmp_path / "vectors.json"
    vectors.build({"live": POOL, "sha256": "abc"}, out, _Embedder(), dim=8)
    with pytest.raises(ValueError, match="another pool"):
        vectors.load(out, dim=8, pool_sha256="something-else")


def test_vectors_refuse_a_missing_question_and_a_wrong_dimension(tmp_path):
    out = tmp_path / "v.json"
    vectors.build({"live": POOL, "sha256": "x"}, out, _Embedder(), dim=8)
    doc = json.loads(out.read_text(encoding="utf-8"))
    del doc["vectors"]["P2"]
    doc["count"] = 2
    out.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="no vector"):
        vectors.check_covers(vectors.load(out, dim=8), POOL)
    doc["count"] = 3                                                    # a count that does not match is an incomplete file
    out.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        vectors.load(out, dim=8)
    with pytest.raises(ValueError):
        vectors.build({"live": POOL, "sha256": "x"}, out, _Embedder(dim=7), dim=8)


def test_a_vector_with_a_number_that_is_not_finite_is_refused(tmp_path):
    class Bad:
        def encode_query(self, text):
            return [float("nan")] + [0.0] * 7

    with pytest.raises(ValueError, match="finite"):
        vectors.build({"live": POOL, "sha256": "x"}, tmp_path / "v.json", Bad(), dim=8)
    assert not (tmp_path / "v.json").exists()


def test_the_real_pool_has_the_shape_the_vectors_and_the_replay_read():
    pool = json.loads((ROOT / "tools" / "loadtest" / "pool.json").read_text(encoding="utf-8"))
    assert isinstance(pool["sha256"], str) and len(pool["sha256"]) == 64
    assert len(pool["live"]) == pool["size"] == 300
    ids = [item["id"] for item in pool["live"]]
    assert len(set(ids)) == 300 and all(isinstance(item["q"], str) and item["q"] for item in pool["live"])
    assert max(len(item["q"]) for item in pool["live"]) <= 500 - len(plan.salted("", 0))        # a salted ask still fits the 500-char cap
    assert all(item["question"] for item in pool["examples"])
    fake_vectors = {item_id: [0.0] * 4 for item_id in ids}
    vectors.check_covers(fake_vectors, pool["live"])                                           # the helper accepts the real shape
    replay.ReplayInputs(pool=pool["live"], vectors=fake_vectors, examples=pool["examples"], snapshot_id="s", template="t")


@pytest.mark.parametrize("uri", ["bolt://semigraph-neo4j-stg.internal:7687", "bolt://localhost:7898", "bolt://127.0.0.1:7898",
                                 "neo4j://[::1]:7898", "bolt://semigraph-neo4j-stg.internal", "bolt+s://semigraph-neo4j-stg.internal:7687"])
def test_the_replay_may_target_a_throwaway_loopback_server_or_the_staging_database(uri):
    assert replay.check_neo4j_uri(uri) == uri


@pytest.mark.parametrize("uri", ["bolt://semigraph-neo4j.internal:7687", "bolt://semigraph.internal:7687", "bolt://example.com:7687",
                                 "bolt://semigraph-neo4j.fly.dev:7687", "bolt://[fdaa::3]:7687", "http://localhost:7474",
                                 "bolt://semigraph-neo4j-stg.internal.evil.com:7687", "bolt://stg:7687", "", "bolt://",
                                 "bolt://localhost:7687", "bolt://localhost", "bolt://127.0.0.1:7687", "neo4j://[::1]:7687",
                                 "bolt://localhost:7699", "bolt://127.0.0.1:7699", "bolt://localhost:notaport"])
def test_the_replay_refuses_the_live_database_the_developers_databases_and_anything_else(uri):
    with pytest.raises(ValueError):
        replay.check_neo4j_uri(uri)


def test_the_staging_host_the_replay_allows_is_the_one_the_window_registry_names():
    windows = json.loads((ROOT / "deploy" / "staging" / "windows.json").read_text(encoding="utf-8"))
    assert replay.STAGING_NEO4J_HOST == windows["apps"]["neo4j"]["name"] + ".internal"
    assert urlparse(replay.DEFAULT_URI).hostname == replay.STAGING_NEO4J_HOST
    toml = tomllib.loads((ROOT / "deploy" / "staging" / "fly.stg.toml").read_text(encoding="utf-8"))
    assert urlparse(toml["env"]["NEO4J_URI"]).hostname == replay.STAGING_NEO4J_HOST


def _all_actions(parser):
    for action in parser._actions:
        yield action
        for sub in getattr(action, "choices", None) or {}:
            child = action.choices[sub] if isinstance(action.choices, dict) else None
            if child is not None:
                yield from _all_actions(child)


def test_the_password_comes_from_the_environment_and_never_the_command_line(monkeypatch):
    args = replay.parse_args(["run", "--phase", "L5", "--out", "x"])
    destinations = {action.dest for action in _all_actions(replay.build_parser())}
    assert {"phase", "out", "uri", "workers"} <= destinations                       # the walk reaches the sub-commands' options
    assert not any(word in dest.lower() for dest in destinations for word in ("password", "passwd", "secret", "token", "auth"))
    monkeypatch.delenv("S7_NEO4J_PASSWORD", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        replay.password_from_env({})
    assert "S7_NEO4J_PASSWORD" in str(exit_info.value)
    assert replay.password_from_env({"S7_NEO4J_PASSWORD": "pw-for-test"}) == "pw-for-test"     # gitleaks:allow
    assert args.phase == "L5"


def test_the_output_directory_defaults_to_the_tools_machines_out_directory(monkeypatch):
    assert replay.parse_args(["run", "--phase", "L5"]).out is None                # then $S7_OUT_DIR/PHASE (fly.tools.toml: /out)
    toml = tomllib.loads((ROOT / "deploy" / "staging" / "fly.tools.toml").read_text(encoding="utf-8"))
    assert toml["env"]["S7_OUT_DIR"] == "/out"
    assert "S7_OUT_DIR" in (S7_DIR / "replay.py").read_text(encoding="utf-8")


def test_a_run_without_its_vectors_or_pool_says_what_to_build_before_it_connects_to_anything(tmp_path):
    pool = str(ROOT / "tools" / "loadtest" / "pool.json")
    with pytest.raises(SystemExit, match="vectors"):
        replay.main(["run", "--phase", "L5", "--out", str(tmp_path / "o"), "--pool", pool, "--vectors", str(tmp_path / "nope.json")], env={})
    with pytest.raises(SystemExit, match="missing"):
        replay.main(["run", "--phase", "L5", "--out", str(tmp_path / "o"), "--pool", str(tmp_path / "nopool.json"),
                     "--vectors", str(tmp_path / "nope.json")], env={})
    assert not (tmp_path / "o").exists()


@pytest.mark.parametrize("argv, rate, duration, judged, salt", [
    (["run", "--phase", "L10", "--out", "x"], 10, 3600, 1800, 4 * replay.SALT_BLOCK),
    (["run", "--phase", "L5", "--out", "x", "--duration-s", "60"], 5, 60, 30, 2 * replay.SALT_BLOCK),
    (["run", "--phase", "baseline", "--out", "x"], 0.5, 900, 600, 0),
    (["run", "--phase", "soak", "--out", "x"], 5, 7200, None, replay.SALT_BLOCK),
    (["run", "--phase", "control", "--out", "x", "--live-rate", "2", "--salt-start", "7"], 2, 1800, 1800, 7),
])
def test_a_phase_runs_at_its_pre_registered_rate_and_length_with_its_own_salt_block(argv, rate, duration, judged, salt):
    cfg = replay._config_for(replay.parse_args(argv))
    assert (cfg.live_rate, cfg.duration_s, cfg.judged_s, cfg.salt_start) == (rate, duration, judged, salt)
    assert cfg.mix == mix.PHASES[cfg.phase].mix


def test_a_salt_block_holds_more_asks_than_the_busiest_phase_makes():
    assert max(p.live_rate * p.minutes * 60 for p in mix.PHASES.values()) * 1.2 < replay.SALT_BLOCK
    assert replay.SALT_BLOCK * len(mix.PHASES) <= plan.MAX_COUNTER + 1


def test_the_salt_never_shows_the_year_detectors_a_year():
    year_retriever, year_router = retriever_module._YEAR, re.compile(rf"\b{router_module._YEAR}\b")
    for counter in list(range(0, 120_000)) + [999_999, 20_199, 201_999]:
        text = plan.salted("Which companies does Nvidia depend on?", counter)
        assert not year_retriever.search(text) and not year_router.search(text), counter
    with pytest.raises(ValueError):
        plan.salted("q", plan.MAX_COUNTER + 1)
    with pytest.raises(ValueError):
        plan.salted("q", -1)


def test_the_gate_sizes_and_bounds_are_the_services_own_constants():
    default = plan.LevelConfig(phase="L5", mix="m5a", live_rate=1, duration_s=1, judged_s=None, run_id="x")
    fields = Settings.model_fields
    assert (default.state_slots, default.db_slots) == (limiters.STATE_THREADS, fields["db_thread_limit"].default) == (4, 32)
    assert default.state_op_timeout_s == STATE_OP_TIMEOUT_DEFAULT_S == 1.0
    assert (default.kill_refresh_s, default.kill_stale_s, default.lease_ttl_s, default.lease_renew_s) == tuple(
        fields[name].default for name in ("kill_switch_refresh_s", "kill_switch_stale_s", "lease_ttl_s", "lease_renew_s"))
    with pytest.raises(ValueError):
        plan.LevelConfig(phase="L5", mix="bogus", live_rate=1, duration_s=1, judged_s=None, run_id="x")
    with pytest.raises(ValueError):
        plan.LevelConfig(phase="L5", mix="m5a", backend="redis", live_rate=1, duration_s=1, judged_s=None, run_id="x")


def live_service_settings(monkeypatch):
    """The production configuration the replay stands for: fly.toml's [env] and the code default of every other field."""
    from semigraph.serve import estimate
    clear_settings_environment(monkeypatch)
    env = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))["env"]
    names = ("answer_model", "escalation_model", "agent_planner_model", "llm_answer_max_tokens", "llm_input_price_per_mtok",
             "llm_output_price_per_mtok", "agent_max_model_calls", "max_question_chars")
    values = {name: env.get(name.upper(), Settings.model_fields[name].default) for name in names}
    return estimate, Settings(_env_file=None, **values), env


def test_the_replays_reserve_is_the_services_live_hybrid_estimate_computed_from_its_arithmetic_not_typed(monkeypatch):
    """A live ask of the replay holds a lease at the estimate of the live hybrid ask (the hybrid worst case, 709,573
    micro-dollars: Luna draft once, Sonnet twice at 2.0 characters per token). It was typed (580,089, the figure before Sonnet
    had a ratio of its own), so the replay went on admitting two hybrid asks in flight per address after the service
    admitted one. It is now read off ``serve/estimate``."""
    estimate, live, env = live_service_settings(monkeypatch)
    assert (plan.LIVE_ANSWER_MODEL, plan.LIVE_ESCALATION_MODEL, plan.LIVE_ANSWER_MAX_TOKENS) == (
        env["ANSWER_MODEL"], env["ESCALATION_MODEL"], int(env["LLM_ANSWER_MAX_TOKENS"]))      # fly.toml's, so a change there shows here
    assert plan.ESTIMATE_MICRO == plan.live_hybrid_estimate_micro() == estimate.estimate_micro("hybrid", live) == 709_573
    source = (S7_DIR / "plan.py").read_text(encoding="utf-8")
    assert "580_089" not in source and "709_573" not in source, "the estimate is computed, not typed"


def test_the_replays_reserve_follows_the_estimate_when_the_services_arithmetic_changes(monkeypatch):
    """Computed, not a copy: Sonnet put back at the old single 2.5 characters per token gives the old 580,089."""
    from decimal import Decimal

    from semigraph.serve import estimate
    assert plan.live_hybrid_estimate_micro() == 709_573
    monkeypatch.setitem(estimate.CHARS_PER_TOKEN_BY_MODEL, "anthropic/claude-sonnet-5", Decimal("2.5"))
    assert plan.live_hybrid_estimate_micro() == 580_089


def test_the_replays_reserve_is_computed_without_building_a_settings(monkeypatch):
    """The module's rule is that no ``Settings`` is built here (its production validators refuse the tools machine's app): the
    estimate is read from the code defaults and the live model strings, through a plain namespace."""
    def refuse(*args, **kwargs):
        raise AssertionError("a Settings was built")

    monkeypatch.setattr(Settings, "__init__", refuse)
    monkeypatch.setattr(Settings, "model_construct", classmethod(lambda cls, *a, **k: refuse()))
    assert plan.live_hybrid_estimate_micro() == 709_573


# =====================================================================================================================
# no Settings, ever
# =====================================================================================================================

def clear_settings_environment(monkeypatch):
    for key in list(os.environ):
        if key.upper() in {name.upper() for name in Settings.model_fields}:
            monkeypatch.delenv(key, raising=False)


def test_a_settings_made_on_the_tools_machine_is_refused_which_is_why_the_replay_builds_none(monkeypatch):
    """The reason for the rule. The tools app IS registered (as staging) since the staging validators, so its refusal is
    not an unknown name any more: the machine's own environment (fly.tools.toml, FLY_APP_NAME set by Fly) names no
    ENVIRONMENT, and with one a staging ``Settings`` needs the staging database, the mock and the origin secret, none of
    which a replay machine holds. Either way a ``Settings`` made there is refused, so the replay must never build one."""
    tools_env = tomllib.loads((Path(__file__).resolve().parents[1] / "deploy" / "staging" / "fly.tools.toml")
                              .read_text(encoding="utf-8"))["env"]
    clear_settings_environment(monkeypatch)
    for key, value in tools_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FLY_APP_NAME", "semigraph-tools-stg")
    with pytest.raises(Exception, match="ENVIRONMENT must be staging on the Fly app semigraph-tools-stg"):
        Settings(_env_file=None)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    with pytest.raises(Exception, match="NEO4J_URI must be on the staging database host"):
        Settings(_env_file=None)


def test_a_settings_made_on_the_staging_database_app_is_refused_as_an_unknown_app(monkeypatch):
    """``semigraph-neo4j-stg`` runs none of our Python and is left out of ``FLY_APP_ENVIRONMENTS`` on purpose (only its
    host name is allowed), so a ``Settings`` made under that name is refused by name, in any environment."""
    clear_settings_environment(monkeypatch)
    monkeypatch.setenv("FLY_APP_NAME", "semigraph-neo4j-stg")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    with pytest.raises(Exception, match="FLY_APP_NAME is not an app this build knows"):
        Settings(_env_file=None)


def test_a_level_builds_no_settings_even_on_an_app_the_production_config_refuses(monkeypatch, tmp_path):
    built = []

    def refuse(self, *args, **kwargs):
        built.append(1)
        raise AssertionError("a Settings was built")

    monkeypatch.setenv("FLY_APP_NAME", "semigraph-tools-stg")
    monkeypatch.setenv("ENVIRONMENT", "production")
    config_module.get_settings.cache_clear()
    monkeypatch.setattr(Settings, "__init__", refuse)
    world = FakeNeo4j()
    world.seed_examples()
    result, out = run_level(world, tmp_path)
    assert built == [] and result["errors"] == 0 and result["driver_errors"] == 0
    assert result["asks"]["live_started"] >= 10
    config_module.get_settings.cache_clear()
    for mix_name, backend in (("today", "inprocess"), ("m5a", "neo4j")):
        result, _ = run_level(world, tmp_path, phase=f"{mix_name}-{backend}", mix=mix_name, backend=backend, run_id=f"nosettings-{backend}")
        assert built == [] and result["driver_errors"] == 0
    config_module.get_settings.cache_clear()


IMPORT_PROBE = r"""
import os, runpy, socket, sys
def refuse(*args, **kwargs):
    raise AssertionError("a socket was used")
socket.socket.connect = refuse
socket.create_connection = refuse
socket.getaddrinfo = refuse
sys.argv = ["replay", "run", "--phase", "L5", "--out", sys.argv[1], "--vectors", sys.argv[2], "--pool", sys.argv[3]]
try:
    runpy.run_module("tools.s7.replay", run_name="__main__")
except SystemExit as stop:
    print("EXIT:", stop.code)
print("COST_MAP:", os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP"))
"""


def test_a_fresh_interpreter_on_the_tools_app_imports_everything_without_a_settings_a_socket_or_the_cost_map_fetch(tmp_path):
    """The in-process tests cannot show this: by then every module was imported. A clean process on the tools machine's own app
    name (which the production config refuses) imports the whole replay, reaches the missing-vectors exit, builds no ``Settings``,
    opens no socket, and has told LiteLLM not to fetch its cost map."""
    import subprocess
    env = {key: value for key, value in os.environ.items() if key != "LITELLM_LOCAL_MODEL_COST_MAP"}
    env.update(FLY_APP_NAME="semigraph-tools-stg", ENVIRONMENT="production", PYTHONPATH=str(ROOT))
    done = subprocess.run([sys.executable, "-c", IMPORT_PROBE, str(tmp_path / "out"), str(tmp_path / "missing.json"),
                           str(ROOT / "tools" / "loadtest" / "pool.json")], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "EXIT:" in done.stdout and "missing" in done.stdout, done.stdout
    assert "COST_MAP: True" in done.stdout, done.stdout
    assert not (tmp_path / "out").exists()
    dockerfile = (ROOT / "deploy" / "staging" / "Dockerfile.tools").read_text(encoding="utf-8")
    assert "LITELLM_LOCAL_MODEL_COST_MAP=True" in dockerfile


# =====================================================================================================================
# packaging
# =====================================================================================================================

def imports_of(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module or "")
            imported |= {f"{node.module}.{a.name}" for a in node.names}
    return imported


PURE = ["limits.py", "mix.py", "record.py", "gates.py", "report.py", "vectors.py", "evidence.py", "counting.py", "schedule.py"]


@pytest.mark.parametrize("module", PURE)
def test_the_pure_modules_are_stdlib_only(module):
    tops = {name.split(".")[0] for name in imports_of(S7_DIR / module)}
    assert tops <= set(sys.stdlib_module_names) | {"tools"}, tops


def test_the_modules_that_import_the_service_need_nothing_the_serve_requirements_do_not_ship():
    shipped = {line.split("==")[0].strip().lower().replace("-", "_")
               for line in (ROOT / "deploy" / "requirements-serve.txt").read_text(encoding="utf-8").splitlines()
               if "==" in line and not line.startswith(("#", " "))}
    for module in ("plan.py", "flows.py", "watch.py", "replay.py"):
        tops = {name.split(".")[0] for name in imports_of(S7_DIR / module)} - set(sys.stdlib_module_names) - {"semigraph", "tools"}
        assert tops <= {"neo4j"} and tops <= shipped, (module, tops)


def test_every_tools_module_the_s7_and_probe_packages_import_is_copied_into_the_tools_image():
    dockerfile = (ROOT / "deploy" / "staging" / "Dockerfile.tools").read_text(encoding="utf-8")
    copied = {line.split()[1].rstrip("/") for line in dockerfile.splitlines() if line.startswith("COPY ") and "--from" not in line}

    def covered(dotted):
        parts = dotted.split(".")
        return any("/".join(parts[:k]) in copied for k in range(1, len(parts) + 1)) or (parts == ["tools"] and "tools/__init__.py" in copied)

    imported = {name for package in ("s7", "probe") for path in (ROOT / "tools" / package).glob("*.py")
                for name in imports_of(path) if name.split(".")[0] == "tools"}
    assert imported and all(covered(name) for name in imported), sorted(n for n in imported if not covered(n))
    assert {"tools/__init__.py", "tools/s7", "tools/probe", "tools/loadtest/pool.json", "src"} <= copied      # and the data the CLI reads


def test_no_module_of_the_package_names_a_live_app_a_pdf_library_or_grows_past_the_ceiling():
    for path in S7_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "pymupdf" not in text.lower() and "fitz" not in text.lower()
        assert "semigraph-neo4j.internal" not in text and "semigraph.internal" not in text, path.name
        assert len(text.splitlines()) < 800, path.name


def test_the_baseline_is_judged_after_five_minutes_of_its_fifteen():
    baseline = mix.PHASES["baseline"]
    assert baseline.judged_s == 15 * 60 - limits.BASELINE_WARMUP_S == 600


@pytest.mark.skipif(not os.environ.get("S7_ITEST_URI"),
                    reason="opt-in: S7_ITEST_URI=bolt://localhost:7898 and S7_NEO4J_PASSWORD for a THROWAWAY Neo4j (never 7687 or 7699)")
def test_a_level_against_a_throwaway_neo4j(tmp_path):                      # pragma: no cover - opt-in
    """The real Cypher of the state writes, the answer cache, the ledger verification and the uptime probe, on a real server.

    The graph of a throwaway server is empty, so the retrieval statements may fail (no vector index): that is not what this
    proves. It proves every STATE operation succeeds, the ledger holds exactly the rows the asks made, and the probe ran."""
    from neo4j import GraphDatabase

    from semigraph.graph.client import DatabaseDriver, make_state_driver
    from semigraph.retrieval.answerer import template_fingerprint

    uri, password = replay.check_neo4j_uri(os.environ["S7_ITEST_URI"]), replay.password_from_env(os.environ)
    reading = DatabaseDriver(GraphDatabase.driver(uri, auth=("neo4j", password)), "neo4j")
    state = make_state_driver(SimpleNamespace(neo4j_uri=uri, neo4j_user="neo4j", neo4j_password=password, neo4j_database="neo4j",
                                              state_op_timeout_s=1.0))
    template = template_fingerprint()
    try:
        examples_doc = {"snapshot_id": "", "template_fingerprint": template, "examples": [
            {"id": f"E{i}", "question": e["question"], "answer": "a", "citations": [], "hallucinated": [],
             "checks": {"citations_retrieved": True, "numbers_grounded": True, "has_citation": True}}
            for i, e in enumerate(EXAMPLES)]}
        assert replay.prepare_examples(reading, examples_doc, "", template=template) == len(EXAMPLES)
        cfg = level_config(run_id=f"itest-{int(time.time())}", live_rate=5.0, duration_s=4.0, judged_s=2.0, hold_s=0.5, drain_s=30.0,
                           probe_every_s=1.0, kill_refresh_s=1.0, lease_renew_s=1.0, lease_ttl_s=10.0)
        result = replay.run_level(cfg, read_driver=reading, state_driver=state,
                                  inputs=replay.ReplayInputs(pool=POOL, vectors=VECTORS, examples=EXAMPLES, snapshot_id="",
                                                             template=template), out_dir=tmp_path / "L5")
        rows = json.loads((tmp_path / "L5" / "ledger_rows.json").read_text(encoding="utf-8"))
        state_ops = {"cache_read", "reserve", "settle", "cache_put", "cached_log", "kill_read", "renew", "sweep"}
        failed = [s for s in record.read_samples(tmp_path / "L5" / "samples.jsonl") if s[1] in state_ops and s[4] != "ok"]
        assert failed == [] and result["driver_errors"] == 0 and result["unfinished"] == 0
        assert rows["paid_rows"] == result["expected"]["paid_rows"] and rows["reserved_rows"] == 0
        assert rows["cached_rows"] == result["expected"]["cached_rows"]
        probes = [json.loads(line) for line in (tmp_path / "L5" / "server.jsonl").read_text(encoding="utf-8").splitlines()]
        assert probes and all(p["ok"] for p in probes)
    finally:
        reading.close()
        state.close()
