"""Spike S7: the Neo4j-only replay (M5a I5; docs/v2/M5_DECISIONS.md section 3).

No API and no model: a driver on its own machine sends the DATABASE the operations an ask makes, using the service's own state,
store and retrieval functions (``tools.s7.flows``), at open-loop Poisson rates, against the staging clone of live's Neo4j.

    python -m tools.s7.replay prepare                  # indexes + the cached examples (once per database)
    python -m tools.s7.replay run --phase L5 --out /out/L5
    python -m tools.s7.report /out --out s7.json [--evidence SNAPSHOT_DIR]

Safety, enforced here and tested: the target must be the staging database host or a loopback address that is NOT the local
development ports (7687, 7699); the password comes from ``S7_NEO4J_PASSWORD`` and is never an argument, a log line or a file; no
``Settings`` is built (the production validators refuse an app name they do not know), the drivers and the state configuration are
made from plain values.
"""

import argparse
import dataclasses
import json
import math
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

# Before the first semigraph import: LiteLLM (loaded by serve.store -> retrieval.answerer) downloads its model-price table from
# GitHub when it is imported, unless told to use its bundled copy. The tools machine must make no call it was not asked to make.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from semigraph.graph.client import run_cypher  # noqa: E402
from semigraph.serve import store  # noqa: E402
from semigraph.serve.state.backend import BoundedDriver, StateDrivers, day_of, make_backend  # noqa: E402
from semigraph.serve.state.maintenance import MaintenanceThread  # noqa: E402

from tools.s7 import mix, plan, vectors  # noqa: E402
from tools.s7.counting import CountedDriver, Tally  # noqa: E402
from tools.s7.flows import Gates, MeteredBackend, NoEmbedder, Runtime, ip_prefix  # noqa: E402
from tools.s7.gates import Gate  # noqa: E402
from tools.s7.plan import LevelConfig, ReplayInputs  # noqa: E402
from tools.s7.record import Recorder  # noqa: E402
from tools.s7.schedule import Dispatcher  # noqa: E402
from tools.s7.watch import CpuSampler, ServerProbe, table_counts, verify_ledger  # noqa: E402

__all__ = ["LevelConfig", "ReplayInputs", "NoEmbedder", "NotPrepared", "run_level", "prepare_examples", "seedable_examples",
           "check_neo4j_uri",
           "password_from_env", "parse_args", "build_parser", "main"]

STAGING_NEO4J_HOST = "semigraph-neo4j-stg.internal"       # = deploy/staging/windows.json apps.neo4j.name + ".internal" (tested)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
FORBIDDEN_LOCAL_PORTS = frozenset({7687, 7699})           # the developer's own databases
SCHEMES = frozenset({"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"})
DEFAULT_URI = f"bolt://{STAGING_NEO4J_HOST}:7687"
PASSWORD_ENV = "S7_NEO4J_PASSWORD"
EXAMPLES_PRECHECK = "MATCH (a:SvcAnswer) WHERE a.key IN $keys RETURN a.key AS key"
SALT_BLOCK = 100_000                                        # counters per phase: a rerun of a phase needs a new salt_start


class NotPrepared(RuntimeError):
    """The database does not hold what a level needs (the cached examples): run ``prepare`` first. Raised before any ask."""


def check_neo4j_uri(uri: str) -> str:
    """The URI if it names the staging database or a loopback address that is not a development port; else ValueError."""
    try:
        parsed = urlparse(uri)
        host, port = parsed.hostname, parsed.port or 7687
    except ValueError:
        raise ValueError("not a database address") from None
    if parsed.scheme not in SCHEMES or not host:
        raise ValueError("a bolt:// or neo4j:// address with a host is required")
    if host == STAGING_NEO4J_HOST:
        return uri
    if host in LOOPBACK_HOSTS and port not in FORBIDDEN_LOCAL_PORTS:
        return uri
    raise ValueError("the replay may target the staging database or a throwaway loopback server (never 7687 or 7699)")


def password_from_env(env: Mapping[str, str]) -> str:
    value = env.get(PASSWORD_ENV, "")
    if not value:
        raise SystemExit(f"{PASSWORD_ENV} is not set: the database password is read from the environment only")
    return value


# ---- preparing the database --------------------------------------------------------------------------------------------

def prepare_examples(driver: Any, examples_doc: Mapping, snapshot_id: str, *, template: str) -> int:
    """The schema the service creates at boot (``store.ensure_indexes``, so the replay times the indexed database live runs on)
    and the cached examples, through the service's own ``seed_examples``. Returns how many were seeded; 0 when the file was
    written for another snapshot or another prompt template (they would never be served, as the service itself decides)."""
    store.ensure_indexes(driver)
    if not store.examples_match_snapshot(dict(examples_doc), snapshot_id) or examples_doc.get("template_fingerprint") != template:
        return 0
    return len(store.seed_examples(driver, list(examples_doc["examples"]), snapshot_id).seeded_ids)


def seedable_examples(examples_doc: Mapping) -> list[dict]:
    """The examples ``prepare`` can cache: the service's own ``seed_examples`` refuses one whose stored checks are missing or
    failed (a cached replay carries no checks of its own). The cached asks of a level are drawn from exactly these."""
    return [dict(example) for example in examples_doc["examples"] if store.refusal_reason(example) is None]


def _precheck(driver: Any, inputs: ReplayInputs) -> None:
    keys = sorted({store.cache_key(e["question"], plan.STRATEGY, inputs.snapshot_id, inputs.template) for e in inputs.examples})
    found = {row["key"] for row in run_cypher(driver, EXAMPLES_PRECHECK, keys=keys)}
    if len(found) < len(keys):
        raise NotPrepared(f"{len(keys) - len(found)} of {len(keys)} cached examples are not in the database: "
                          "run `python -m tools.s7.replay prepare` (for this snapshot and template)")


# ---- one level -----------------------------------------------------------------------------------------------------------

def _settings(cfg: LevelConfig) -> SimpleNamespace:
    """The few values the state package reads, as plain attributes. NOT a ``Settings``: its production validators refuse an
    app they do not know, and nothing here needs them. Every cap is off; the lease timings are the level's."""
    return SimpleNamespace(
        state_backend=cfg.backend, max_queries_per_day=0, max_spend_usd_per_day=0, paid_per_ip_per_day=0,
        paid_spend_share_per_ip_usd=0, max_concurrent_answers=cfg.max_inflight, kill_switch=False,
        kill_switch_refresh_s=cfg.kill_refresh_s, kill_switch_stale_s=cfg.kill_stale_s,
        state_op_timeout_s=cfg.state_op_timeout_s, lease_ttl_s=cfg.lease_ttl_s, lease_renew_s=cfg.lease_renew_s,
        machine_id=f"s7-{cfg.run_id}", ip_hash_version=1)


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(1, math.ceil(round(q * len(ordered), 9))) - 1] if ordered else 0.0


def run_level(cfg: LevelConfig, *, read_driver: Any, state_driver: Any, inputs: ReplayInputs, out_dir: Path) -> dict:
    """Run one phase and write its raw files to ``out_dir``; returns the content of ``level.json``.

    Raises :class:`NotPrepared` before the first ask (and before any file is written) if a cached example is missing."""
    _precheck(read_driver, inputs)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rec, tally = Recorder(out_dir / "samples.jsonl"), Tally()
    reading, stating = CountedDriver(read_driver, tally), CountedDriver(state_driver, tally)
    gates = Gates(Gate(cfg.state_slots), Gate(cfg.db_slots), Gate(cfg.pool_slots))
    dispatcher = Dispatcher(cfg.workers)
    store_driver = BoundedDriver(stating, cfg.state_op_timeout_s)
    backend = make_backend(_settings(cfg), StateDrivers(state=stating)) if cfg.mix == "m5a" else None
    counts_start = table_counts(state_driver)
    wall_start, t0 = time.time(), time.perf_counter()
    probe = ServerProbe(state_driver, cfg.probe_every_s, out_dir / "server.jsonl", t0=t0)
    cpu = CpuSampler(out_dir / "driver.jsonl")
    runtime = Runtime(cfg=cfg, inputs=inputs, rec=rec, tally=tally, gates=gates, read_driver=reading,
                      store_driver=store_driver, backend=backend, dispatcher=dispatcher, t0=t0)
    maintenance = None
    try:
        if backend is not None:
            maintenance = MaintenanceThread(MeteredBackend(backend, runtime), _settings(cfg), backend.registry)
            maintenance.start()                       # the first kill-level read happens here, as at the service's boot
        probe.start()
        cpu.start()
        events = mix.arrivals(cfg.live_rate, cfg.live_rate * mix.CACHED_PER_LIVE, cfg.duration_s, cfg.seed)
        unfinished = dispatcher.drive(runtime.arrivals(events), t0, t0 + cfg.duration_s + cfg.hold_s + cfg.drain_s)
        wall_end = time.time()
    finally:
        dispatcher.close()
        probe.stop()
        cpu.stop()
        if maintenance is not None:
            maintenance.stop()                        # flushes the settles that were waiting for a retry
        rec.close()
    prefix = ip_prefix(cfg.run_id)
    counts_end = table_counts(state_driver)
    rows = verify_ledger(state_driver, prefix, wall_start, wall_end)
    (out_dir / "ledger_rows.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    lags = list(dispatcher.lags)
    result = {
        "phase": cfg.phase, "mix": cfg.mix, "backend": cfg.backend, "live_rate": cfg.live_rate, "duration_s": cfg.duration_s,
        "judged_s": cfg.judged_s, "hold_s": cfg.hold_s, "run_id": cfg.run_id, "seed": cfg.seed,
        "wall_start": wall_start, "wall_end": wall_end, "day": day_of(wall_start),
        "asks": {"live_started": tally.get("live_started"), "cached_started": tally.get("cached_started")},
        "errors": tally.get("errors"), "denied": tally.get("denied"), "unfinished": unfinished,
        "driver_errors": len(dispatcher.crashes), "driver_error_classes": sorted(set(dispatcher.crashes)),
        "settle_not_charged": tally.get("settle_not_charged"),
        "expected": {"paid_rows": tally.get("granted") if cfg.mix == "m5a" else tally.get("paid_logged"),
                     "cached_rows": tally.get("cached_logged")},
        "driver": cpu.summary(),
        "lag": {"p99_s": round(_percentile(lags, 0.99), 4), "max_s": round(max(lags, default=0.0), 4), "n": len(lags)},
        "probe": {"count": probe.count, "failures": probe.failures},
        "statements": tally.statements_report(),
        "gates": {"state": cfg.state_slots, "db": cfg.db_slots, "pool": cfg.pool_slots},
        "table_counts": {"start": counts_start, "end": counts_end},
        "salt": {"start": cfg.salt_start, "last": cfg.salt_start + max(tally.get("live_started") - 1, 0)},
        "config": dataclasses.asdict(cfg),
    }
    (out_dir / "level.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


# ---- the command line ----------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tools.s7.replay", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--uri", default=os.environ.get("S7_NEO4J_URI", DEFAULT_URI),
                        help="the database (default: the staging one); the password is read from S7_NEO4J_PASSWORD")
    common.add_argument("--database", default="neo4j")
    common.add_argument("--examples", type=Path, default=Path("src/semigraph/artifacts/examples.json"))
    run = sub.add_parser("run", parents=[common], help="run one phase")
    run.add_argument("--phase", choices=sorted(mix.PHASES), required=True)
    run.add_argument("--out", type=Path, default=None, help="default: $S7_OUT_DIR/PHASE (S7_OUT_DIR is /out on the tools machine)")
    run.add_argument("--run-id", default=None)
    run.add_argument("--backend", choices=("inprocess", "neo4j"), default="inprocess")
    run.add_argument("--pool", type=Path, default=Path("tools/loadtest/pool.json"))
    run.add_argument("--vectors", type=Path, default=Path("tools/s7/data/vectors.json"))
    run.add_argument("--live-rate", type=float, default=None, help="override the phase's live asks per second")
    run.add_argument("--duration-s", type=float, default=None, help="override the phase's length (a rehearsal)")
    run.add_argument("--hold-s", type=float, default=None)
    run.add_argument("--workers", type=int, default=plan.DEFAULT_WORKERS)
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--salt-start", type=int, default=None)
    sub.add_parser("prepare", parents=[common], help="create the schema and cache the examples")
    vec = sub.add_parser("vectors", help="embed the pool's questions (developer machine, the production embedder)")
    vec.add_argument("--pool", type=Path, default=Path("tools/loadtest/pool.json"))
    vec.add_argument("--out", type=Path, default=Path("tools/s7/data/vectors.json"))
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _drivers(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[Any, Any]:
    from neo4j import GraphDatabase

    from semigraph.graph.client import DatabaseDriver, make_state_driver

    uri = check_neo4j_uri(args.uri)
    password = password_from_env(env)
    reading = DatabaseDriver(GraphDatabase.driver(uri, auth=("neo4j", password)), args.database)
    state = make_state_driver(SimpleNamespace(neo4j_uri=uri, neo4j_user="neo4j", neo4j_password=password,
                                              neo4j_database=args.database, state_op_timeout_s=plan.STATE_OP_TIMEOUT_DEFAULT_S))
    return reading, state


def _examples(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _cmd_prepare(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from semigraph.retrieval.answerer import template_fingerprint

    reading, state = _drivers(args, env)
    try:
        snapshot = store.current_snapshot(reading)
        snapshot_id = snapshot["id"] if snapshot else ""
        seeded = prepare_examples(reading, _examples(args.examples), snapshot_id, template=template_fingerprint())
    finally:
        reading.close()
        state.close()
    print(f"prepared: snapshot {snapshot_id or '(none)'}, {seeded} cached example(s)")
    return 0 if seeded else 1


def _cmd_vectors(args: argparse.Namespace) -> int:
    from semigraph.embeddings import Embedder

    pool = json.loads(args.pool.read_text(encoding="utf-8"))
    doc = vectors.build(pool, args.out, Embedder())
    print(f"vectors: {doc['count']} questions, dimension {doc['dim']}, pool {str(doc['pool_sha256'])[:12]}")
    return 0


def _config_for(args: argparse.Namespace) -> LevelConfig:
    phase = mix.PHASES[args.phase]
    duration = args.duration_s if args.duration_s is not None else phase.minutes * 60.0
    judged = phase.judged_s if args.duration_s is None else (None if phase.judged_s is None else min(phase.judged_s, duration / 2))
    extra = {} if args.hold_s is None else {"hold_s": args.hold_s}
    salt = args.salt_start if args.salt_start is not None else SALT_BLOCK * list(mix.PHASES).index(args.phase)
    return LevelConfig(phase=phase.name, mix=phase.mix, backend=args.backend,
                       live_rate=phase.live_rate if args.live_rate is None else args.live_rate, duration_s=duration,
                       judged_s=judged, run_id=args.run_id or f"{args.phase}-{int(time.time())}", workers=args.workers,
                       seed=args.seed, salt_start=salt, **extra)


def _cmd_run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from semigraph.retrieval.answerer import template_fingerprint

    for path, how in ((args.pool, "it ships with tools/loadtest"), (args.vectors, "build it with `python -m tools.s7.replay vectors`")):
        if not Path(path).is_file():
            raise SystemExit(f"{path} is missing: {how}")
    pool = json.loads(args.pool.read_text(encoding="utf-8"))
    vecs = vectors.load(args.vectors, pool_sha256=pool.get("sha256"))
    vectors.check_covers(vecs, pool["live"])
    reading, state = _drivers(args, env)
    try:
        snapshot = store.current_snapshot(reading)
        inputs = ReplayInputs(pool=pool["live"], vectors=vecs, examples=seedable_examples(_examples(args.examples)),
                              snapshot_id=snapshot["id"] if snapshot else "", template=template_fingerprint())
        result = run_level(_config_for(args), read_driver=reading, state_driver=state, inputs=inputs,
                           out_dir=args.out or Path(env.get("S7_OUT_DIR", "s7_out")) / args.phase)
    finally:
        reading.close()
        state.close()
    print(f"{result['phase']}: {result['asks']} errors={result['errors']} denied={result['denied']} "
          f"unfinished={result['unfinished']} driver_cpu_p95={result['driver']['cpu_p95']:.0%}")
    return 0


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = parse_args(argv)
    env = os.environ if env is None else env
    if args.command == "vectors":
        return _cmd_vectors(args)
    return _cmd_prepare(args, env) if args.command == "prepare" else _cmd_run(args, env)


if __name__ == "__main__":
    sys.exit(main())
