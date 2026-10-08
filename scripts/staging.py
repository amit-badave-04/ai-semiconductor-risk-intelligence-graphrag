"""The staging window tool (M5a I5; docs/v2/M5_DECISIONS.md 2.1 "Staging rules"): quote, create, seed, deploy, preflight,
reset-ledger, snapshot, destroy.

A window (W0 throttle probe, W1 embedder timing, W2 S12 calibration, W3 S7, W4 S2) is a set of THROWAWAY Fly apps created for a
test and destroyed after it. This tool is the only thing that creates, changes or destroys them; a mistake costs a refusal:

* ``quote`` prints the plan's section 2 table from ``deploy/staging/windows.json`` and calls nothing. Every spending command
  prints its window's row BEFORE the first ``flyctl`` call; ``create`` runs only with ``--approve-quote`` equal to that quote.
* Every ``flyctl`` call is an argument list through ONE function that refuses any app that is not a registered staging app.
* Secrets are generated per window, pushed ONLY on the stdin of ``flyctl secrets import``, kept in a state file OUTSIDE the
  repository (``--state-dir``, default ``~/.semigraph-staging``) and scrubbed from errors and snapshots; never in an argument
  list, a plan or a printed line. ``--plan`` prints every command and runs, writes and generates nothing.
* The database is seeded from the repository's own baked dump, never from an export of live (live rows carry address hashes).
  ``reset-ledger`` runs only with the API stopped (the in-process backend keeps its counters in memory).

    uv run python scripts/staging.py quote
    uv run python scripts/staging.py --plan create W4 --org <org>
    uv run python scripts/staging.py create W4 --org <org> --approve-quote 1.25

Every flyctl flag here that has not been run against this organisation is listed as UNVERIFIED in the build log.
"""

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))      # scripts/ is not a package: the quote module sits beside this file
from staging_quote import (  # noqa: E402,F401  (re-exported: the tests and other tools read them from here)
    REPO,
    UsageError,
    derived_usd,
    has_option,
    hourly_rate,
    load_windows,
    parse_class,
    quote_lines,
    quote_usd,
)

MONEY_TOLERANCE = 0.005
STOPPED_STATES = frozenset({"stopped", "suspended"})
DEPLOY_TIMEOUT_S, CALL_TIMEOUT_S, STOP_POLLS, STOP_POLL_S = 1800, 180, 30, 2.0
LOCAL_BOLT_PORT = 17687
TOOLS_TAR_COMMAND = "tar -czf - -C /out ."
GZIP_MAGIC = b"\x1f\x8b"
MAX_EXTRACT_BYTES = 512 * 1024 * 1024
REDACTED = "[redacted]"

# What `deploy --set` may change (the caps the sub-runs flip) and the shape of a value: nothing else reaches --env.
SETTABLE = frozenset({
    "MAX_QUERIES_PER_DAY", "MAX_SPEND_USD_PER_DAY", "PAID_PER_IP_PER_DAY", "PAID_SPEND_SHARE_PER_IP_USD", "RATE_LIMIT_QUESTIONS",
    "RATE_LIMIT_WINDOW_SECONDS", "FREE_RATE_LIMIT_QUESTIONS", "CACHE_READ_BUDGET_PER_S", "MAX_CONCURRENT_ANSWERS",
    "EMBED_SLOTS", "STATE_BACKEND", "MAX_UPLOADS_PER_DAY", "DB_THREAD_LIMIT"})
SET_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=([A-Za-z0-9_.:-]{1,32})$")
# What `deploy --build-arg` may change: the two embedder arguments of the main Dockerfile (window W1 builds the image with both
# models, KEEP_UNPATCHED=1), each with the values it accepts. Nothing else reaches --build-arg.
BUILD_ARGS = {"EMBEDDER_VARIANT": frozenset({"q8", "fp32"}), "KEEP_UNPATCHED": frozenset({"0", "1"})}
SIZE_OF_KIND = {"pepper": 48, "admin": 32, "origin": 48, "mock_key": 24, "db_password": 24, "mock_admin": 32, "turnstile": 16}
REQUIRED_API_SECRETS = ("IP_HASH_PEPPER", "ADMIN_TOKEN", "ORIGIN_AUTH_SECRET", "OPENAI_API_KEY", "NEO4J_PASSWORD")
FORBIDDEN_API_SECRETS = frozenset({"ANTHROPIC_API_KEY", "SEC_USER_AGENT", "TURNSTILE_SECRET_KEY", "LANGFUSE_SECRET_KEY",
                                   "LANGFUSE_PUBLIC_KEY", "EMBEDDING_API_KEY"})

# preflight / reset-ledger / snapshot Cypher (a clean boot writes seeded SvcAnswer rows and may write a SvcPolicy or a zero
# SvcDayCounter; everything that records a visit is what must be absent)
LEDGER_LABELS = ("SvcQuery", "SvcIpDay", "SvcUploadDay", "SvcLease", "SvcFreshness")
Q_LEDGER_NODES = ("MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'User' OR l IN "
                  f"{list(LEDGER_LABELS)!r}) RETURN count(n) AS n")
Q_DAY_COUNTERS = ("MATCH (c:SvcDayCounter) WHERE coalesce(c.paid, 0) <> 0 OR coalesce(c.spend_micro, 0) <> 0 "
                  "RETURN count(c) AS n")
Q_EXAMPLES = "MATCH (a:SvcAnswer {source: 'benchmark'}) RETURN count(a) AS n"
Q_KILL = "MATCH (p:SvcPolicy {key: 'kill_switch'}) RETURN p.value AS v"
Q_LEDGER_COUNTS = "MATCH (q:SvcQuery) RETURN q.status AS status, q.outcome AS outcome, q.cached AS cached, count(q) AS n"
Q_COUNTERS = "MATCH (c:SvcDayCounter) RETURN c.day AS day, c.paid AS paid, c.spend_micro AS spend_micro"
RESET_LABELS = ("SvcQuery", "SvcDayCounter", "SvcIpDay", "SvcUploadDay")
CACHE_DELETE = ("MATCH (a:SvcAnswer) WHERE a.source <> 'benchmark' OR a.source IS NULL "
                "CALL (a) { DETACH DELETE a } IN TRANSACTIONS OF 5000 ROWS")


def q_delete(label: str) -> str:
    return f"MATCH (n:{label}) CALL (n) {{ DETACH DELETE n }} IN TRANSACTIONS OF 5000 ROWS"


def q_count(label: str) -> str:
    return f"MATCH (n:{label}) RETURN count(n) AS n"


class RegistryError(ValueError):
    """A call named an app that is not a registered staging app."""


class FlyError(RuntimeError):
    """A flyctl call failed (the message carries the command words and a scrubbed stderr tail, never an argument secret)."""


class Result(NamedTuple):
    returncode: int
    stdout: bytes
    stderr: bytes


# ---- running flyctl --------------------------------------------------------------------------------------------------

class SubprocessRunner:
    """The real runner: an argument list, no shell, bytes in and out."""

    def run(self, args: list[str], *, stdin: str | None = None, cwd: str | None = None, timeout: float | None = None) -> Result:
        done = subprocess.run(args, input=None if stdin is None else stdin.encode("utf-8"), cwd=cwd, timeout=timeout,
                              capture_output=True, check=False)
        return Result(done.returncode, done.stdout or b"", done.stderr or b"")

    def start(self, args: list[str]) -> subprocess.Popen:
        return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class ProxyBolt:
    """A Bolt session to the staging database through ``flyctl proxy`` (the database has no public address)."""

    def __init__(self, tool: "Staging", state: dict):
        self.tool, self.state, self.proc, self.driver = tool, state, None, None

    def __enter__(self) -> "ProxyBolt":
        name = self.tool.windows["apps"]["neo4j"]["name"]
        args = [self.tool.flyctl, "proxy", f"{LOCAL_BOLT_PORT}:7687", "-a", name]
        self.tool._guard_registry(args[1:])
        if not hasattr(self.tool.runner, "start"):
            raise UsageError("this runner cannot open a proxy")
        self.proc = self.tool.runner.start(args)
        for _ in range(60):
            with socket.socket() as probe:
                probe.settimeout(0.5)
                if probe.connect_ex(("127.0.0.1", LOCAL_BOLT_PORT)) == 0:
                    break
            time.sleep(0.5)
        from neo4j import GraphDatabase                       # developer side only
        password = self.state["secrets"]["neo4j"]["NEO4J_AUTH"].split("/", 1)[1]
        self.driver = GraphDatabase.driver(f"bolt://127.0.0.1:{LOCAL_BOLT_PORT}", auth=("neo4j", password))
        self.driver.verify_connectivity()
        return self

    def run(self, cypher: str, **params: Any) -> list[dict]:
        with self.driver.session() as session:
            return [dict(record) for record in session.run(cypher, **params)]

    def __exit__(self, *exc: object) -> bool:
        if self.driver is not None:
            self.driver.close()
        if self.proc is not None:
            self.proc.terminate()
        return False


def default_secret(kind: str) -> str:
    return secrets.token_urlsafe(SIZE_OF_KIND[kind])


def _names_in(payload: bytes, key: str) -> set[str]:
    try:
        rows = json.loads(payload.decode("utf-8") or "[]")
    except (ValueError, UnicodeDecodeError):
        raise FlyError(f"could not read the {key} listing") from None
    return {str(row.get("Name") or row.get("name") or "") for row in rows if isinstance(row, dict)} - {""}


class Staging:
    def __init__(self, *, repo: Path = REPO, windows_path: Path | None = None, runner: Any = None,
                 out: Callable[[str], None] | None = None, err: Callable[[str], None] | None = None,
                 state_dir: Path | None = None, plan: bool = False, gen: Callable[[str], str] = default_secret,
                 bolt_factory: Callable[[dict], Any] | None = None, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.time, flyctl: str = "flyctl", dump_path: Path | None = None,
                 vectors_path: Path | None = None):
        self.repo = Path(repo).resolve()
        self.windows = load_windows(windows_path or self.repo / "deploy" / "staging" / "windows.json")
        self.runner = runner if runner is not None else SubprocessRunner()
        self.out = out or print
        self.err = err or (lambda line: print(line, file=sys.stderr))
        self.state_dir = Path(state_dir or os.environ.get("STAGING_STATE_DIR") or Path.home() / ".semigraph-staging")
        if self.state_dir.resolve().is_relative_to(self.repo):
            raise UsageError("the state directory holds the window's secrets: it must be outside the repository")
        self.plan, self.gen, self.bolt_factory = plan, gen, bolt_factory
        self.sleep, self.now, self.flyctl = sleep, now, flyctl
        self.dump_path = Path(dump_path) if dump_path else self.repo / "deploy" / "neo4j" / "seed" / "neo4j.dump"
        self.vectors_path = Path(vectors_path) if vectors_path else self.repo / "tools" / "s7" / "data" / "vectors.json"
        self.app_names = {app["name"] for app in self.windows["apps"].values()}
        self._known_secrets: set[str] = set()

    # ---- the one function every call goes through ---------------------------------------------------------------

    def _guard_registry(self, args: Sequence[str]) -> None:
        named = []
        for i, arg in enumerate(args):
            if arg in ("--app", "-a") and i + 1 < len(args):
                named.append(args[i + 1])
            elif arg.startswith("--app="):
                named.append(arg.split("=", 1)[1])
        if list(args[:2]) in (["apps", "destroy"], ["apps", "create"]) and len(args) > 2:
            named.append(args[2])
        if not named and args and args[0] in {"deploy", "secrets", "volumes", "machine", "ips", "ssh", "logs", "status",
                                              "config", "proxy"}:
            raise RegistryError(f"flyctl {args[0]} without an app")
        for name in named:
            if name not in self.app_names:
                raise RegistryError("refusing an app that is not a registered staging app")

    def _scrub(self, text: str) -> str:
        for value in sorted(self._known_secrets, key=len, reverse=True):
            text = text.replace(value, REDACTED)
        return text

    def _show(self, args: Sequence[str], stdin: str | None, cwd: str | None) -> None:
        line = "+ " + shlex.join([self.flyctl, *args]) + (f"   (cwd {cwd})" if cwd else "")
        if stdin is not None:
            names = ", ".join(row.split("=", 1)[0] for row in stdin.splitlines())
            line += f"   (stdin: {names}; values generated per window, never shown)"
        self.out(line)

    def _fly(self, args: Sequence[str], *, stdin: str | None = None, cwd: str | None = None,
             timeout: float = CALL_TIMEOUT_S, check: bool = True) -> Result:
        args = list(args)
        self._guard_registry(args)
        if self.plan:
            self._show(args, stdin, cwd)
            return Result(0, b"", b"")
        try:
            result = self.runner.run([self.flyctl, *args], stdin=stdin, cwd=cwd, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise FlyError(f"flyctl {' '.join(args[:3])} timed out after {timeout:g} s") from None
        except OSError as exc:
            raise FlyError(f"flyctl could not be started ({type(exc).__name__}): is it installed and on the PATH?") from None
        if check and result.returncode != 0:
            tail = self._scrub(result.stderr.decode("utf-8", "replace").strip()[-400:])
            raise FlyError(f"flyctl {' '.join(args[:3])} failed (exit {result.returncode}): {tail}")
        return result

    def _json(self, args: Sequence[str]) -> Any:
        result = self._fly(args)
        try:
            return json.loads(result.stdout.decode("utf-8") or "null")
        except ValueError:
            raise FlyError(f"flyctl {' '.join(args[:3])} did not return JSON") from None

    # ---- windows, state, quote -----------------------------------------------------------------------------------

    def _window(self, key: str) -> dict:
        if key not in self.windows["windows"]:
            raise UsageError(f"unknown window {key!r}")
        return self.windows["windows"][key]

    def _state_path(self, key: str) -> Path:
        return self.state_dir / f"{key}.json"

    def _load_state(self, key: str) -> dict | None:
        path = self._state_path(key)
        if not path.is_file():
            return None
        state = json.loads(path.read_text(encoding="utf-8"))
        self._known_secrets |= {v for group in state.get("secrets", {}).values() for v in group.values() if len(v) >= 8}
        return state

    def _require_state(self, key: str) -> dict:
        window = self._window(key)
        if self.plan:
            return {"window": key, "apps": {a: self.windows["apps"][a]["name"] for a in window["apps"]}, "secrets": {},
                    "option": None}
        state = self._load_state(key)
        if state is None:
            raise UsageError(f"window {key} was not created: run `staging.py create {key} --org ... --approve-quote ...`")
        quote = quote_usd(self.windows, key, state.get("option"))[0]
        if state.get("approved_quote_usd") is None or abs(state["approved_quote_usd"] - quote) > MONEY_TOLERANCE:
            raise UsageError(f"window {key} was not approved at its current quote (${quote:.2f}): destroy and create it again")
        return state

    def _print_quote(self, key: str, option: str | None = None) -> None:
        for line in quote_lines(self.windows, [key], option)[:2]:
            self.out(line)

    def quote(self, keys: Sequence[str] = (), option: str | None = None) -> int:
        keys = list(keys) or sorted(self.windows["windows"])
        for line in quote_lines(self.windows, keys, option):
            self.out(line)
        totals = [quote_usd(self.windows, k, option if has_option(self.windows["windows"][k], option or "\0") else None)
                  for k in keys]
        provider = sum(self.windows["windows"][k].get("provider_cap_usd", 0) for k in keys)
        self.out(f"Machine time: ${sum(q for q, _ in totals):.2f} (cap ${sum(c for _, c in totals):.2f})"
                 + (f"; provider spend up to ${provider:.2f}" if provider else ""))
        return 0

    # ---- create --------------------------------------------------------------------------------------------------

    def _generate_secrets(self, window: dict, run_id: str) -> dict:
        if self.plan:
            names = {"api": REQUIRED_API_SECRETS, "neo4j": ("NEO4J_AUTH",), "mockllm": ("MOCKLLM_ADMIN_TOKEN",),
                     "tools": ("S7_NEO4J_PASSWORD",), "loadgen": ("LOADTEST_ORIGIN_AUTH", "LOADTEST_TURNSTILE_TOKEN",
                                                                  "LOADTEST_RUN_ID")}
            return {a: {n: "<generated>" for n in names[a]} for a in window["apps"] if a in names
                    and (a != "tools" or "neo4j" in window["apps"])}
        cache: dict[str, str] = {}

        def kind(k: str) -> str:
            if k not in cache:
                cache[k] = self.gen(k)
            return cache[k]

        build = {
            "api": lambda: {"IP_HASH_PEPPER": kind("pepper"), "ADMIN_TOKEN": kind("admin"),
                            "ORIGIN_AUTH_SECRET": kind("origin"), "OPENAI_API_KEY": "mock-" + kind("mock_key"),
                            "NEO4J_PASSWORD": kind("db_password")},
            "neo4j": lambda: {"NEO4J_AUTH": "neo4j/" + kind("db_password")},
            "mockllm": lambda: {"MOCKLLM_ADMIN_TOKEN": kind("mock_admin")},
            "tools": lambda: {"S7_NEO4J_PASSWORD": kind("db_password")} if "neo4j" in window["apps"] else {},
            "loadgen": lambda: {"LOADTEST_ORIGIN_AUTH": kind("origin"), "LOADTEST_TURNSTILE_TOKEN": kind("turnstile"),
                                "LOADTEST_RUN_ID": run_id}}
        return {a: build[a]() for a in window["apps"] if build[a]()}

    def _check_approval(self, approve: str | None, quote: float) -> None:
        try:
            approved = float(approve)
        except (TypeError, ValueError):
            approved = None
        if approved is None or abs(approved - quote) > MONEY_TOLERANCE:
            raise UsageError(f"this window's quote is ${quote:.2f}: pass --approve-quote {quote:.2f} to confirm it")

    def create(self, key: str, *, org: str, region: str = "sin", approve_quote: str | None = None,
               option: str | None = None, use_network: bool = True) -> int:
        window = self._window(key)
        quote, cap = quote_usd(self.windows, key, option)
        self._print_quote(key, option)
        if not self.plan:
            if not org:
                raise UsageError("--org is required (or set FLY_ORG)")
            self._check_approval(approve_quote, quote)
            if self._state_path(key).exists():
                raise UsageError(f"window {key} already exists: destroy it first (its secrets stay in its state file)")
        stamp = datetime.fromtimestamp(self.now(), UTC)
        run_id = f"{key}-{stamp:%Y%m%dT%H%M%S}"
        network = f"stg-{key.lower()}-{int(self.now()) % 100000:05d}" if use_network else None
        state = {"window": key, "org": org, "region": region, "network": network, "run_id": run_id, "option": option,
                 "approved_quote_usd": quote, "cap_usd": cap, "created_at": stamp.isoformat(timespec="seconds"),
                 "apps": {a: self.windows["apps"][a]["name"] for a in window["apps"]},
                 "secrets": self._generate_secrets(window, run_id)}
        if self.plan:
            self.out("secrets: generated per window (never shown), pushed on stdin of `flyctl secrets import`")
        else:
            self._save_state(state)
        for app_key in window["apps"]:
            self._create_app(app_key, state)
        self.out(f"window {key} created: {', '.join(state['apps'].values())}; quote ${quote:.2f}, cap ${cap:.2f}")
        return 0

    def _save_state(self, state: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_path(state["window"])
        path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self._known_secrets |= {v for group in state["secrets"].values() for v in group.values() if len(v) >= 8}

    def _create_app(self, app_key: str, state: dict) -> None:
        app = self.windows["apps"][app_key]
        name = app["name"]
        args = ["apps", "create", name, "--org", state["org"]]
        self._fly([*args, "--network", state["network"]] if state["network"] else args)
        if app.get("volume"):
            volume = app["volume"]
            self._fly(["volumes", "create", volume["name"], "--app", name, "--region", state["region"],
                       "--size", str(volume["size_gb"]), "--yes"])
        if app.get("public"):
            self._fly(["ips", "allocate-v6", "--app", name])
            self._fly(["ips", "allocate-v4", "--shared", "--app", name])
        values = state["secrets"].get(app_key)
        if values:
            self._fly(["secrets", "import", "--app", name, "--stage"],
                      stdin="".join(f"{k}={v}\n" for k, v in values.items()))

    # ---- seed and deploy -----------------------------------------------------------------------------------------

    def _parse_sets(self, sets: Iterable[str]) -> list[str]:
        pairs = []
        for item in sets:
            match = SET_RE.match(item)
            if not match or match.group(1) not in SETTABLE:
                raise UsageError(f"--set accepts only {sorted(SETTABLE)} with a plain value, got {item.split('=')[0]!r}")
            pairs.append(item)
        return pairs

    @staticmethod
    def _parse_build_args(items: Iterable[str]) -> list[str]:
        pairs = []
        for item in items:
            name, _, value = item.partition("=")
            if name not in BUILD_ARGS or value not in BUILD_ARGS[name]:
                allowed = {k: sorted(v) for k, v in BUILD_ARGS.items()}
                raise UsageError(f"--build-arg accepts only {allowed}, got {name!r}")
            pairs.append(item)
        return pairs

    def _deploy_app(self, key: str, app_key: str, sets: list[str], klass: str | None,
                    build_args: Sequence[str] = ()) -> None:
        app = self.windows["apps"][app_key]
        window = self._window(key)
        klass = klass or next(m["class"] for m in window["machines"] if m["app"] == app_key)
        size, memory = parse_class(klass)
        cwd = (self.repo / app["context"]).resolve()
        config = os.path.relpath(self.repo / app["toml"], cwd).replace(os.sep, "/")
        args = ["deploy", "--app", app["name"], "--config", config, "--dockerfile", app["dockerfile"], "--remote-only",
                "--ha=false", "--vm-size", size, "--vm-memory", str(memory)]
        if not app.get("public"):
            args.append("--no-public-ips")
        for item in sets:
            args += ["--env", item]
        for item in build_args:
            args += ["--build-arg", item]
        self._fly(args, cwd=str(cwd), timeout=DEPLOY_TIMEOUT_S)
        self.out(f"deployed {app['name']} ({klass.replace(':', ' ')}), Dockerfile {app['dockerfile']}, context {app['context']}")

    def seed(self, key: str) -> int:
        window = self._window(key)
        if "neo4j" not in window["apps"]:
            raise UsageError(f"window {key} has no database")
        self._require_state(key)
        if not self.plan and not self.dump_path.is_file():
            raise UsageError(f"the baked dump {self.dump_path} is missing (see deploy/neo4j/seed/README.md)")
        self._print_quote(key)
        size = f" ({self.dump_path.stat().st_size / 1e6:.1f} MB)" if self.dump_path.is_file() else ""
        self.out(f"seed: the image bakes the repository's own dump {self.dump_path.name}{size}; the first boot of the empty "
                 "volume loads it. Nothing is exported from live.")
        self._deploy_app(key, "neo4j", [], None)
        return 0

    def _check_vectors(self) -> None:
        """The S7 replay reads the pool's question vectors from the image (the tools machine has no embedding model). A tools
        image built without them, or with vectors of another pool, is a paid window spent on a driver that cannot start."""
        pool = self.repo / "tools" / "loadtest" / "pool.json"
        problem = None
        if not self.vectors_path.is_file():
            problem = (f"{self.vectors_path.name} is missing: build it with `uv run python -m tools.s7.replay vectors` "
                       "(it is copied into the tools image)")
        else:
            try:
                built = json.loads(self.vectors_path.read_text(encoding="utf-8")).get("pool_sha256")
                current = json.loads(pool.read_text(encoding="utf-8")).get("sha256")
            except (OSError, ValueError):
                built = current = None
            if built is None or built != current:
                problem = f"{self.vectors_path.name} was built from another pool than tools/loadtest/pool.json: rebuild it"
        if problem is None:
            return
        if self.plan:
            self.out(f"NOTE (a real deploy would refuse): {problem}")
            return
        raise UsageError(problem)

    def deploy(self, key: str, app_key: str, *, sets: Sequence[str] = (), klass: str | None = None,
               build_args: Sequence[str] = ()) -> int:
        window = self._window(key)
        if app_key not in window["apps"]:
            raise UsageError(f"window {key} has no app {app_key!r} ({', '.join(window['apps'])})")
        if app_key == "neo4j":
            raise UsageError("the database is deployed by `seed` (it loads the baked dump)")
        pairs = self._parse_sets(sets)
        if pairs and app_key != "api":
            raise UsageError("--set changes the API's environment only")
        builds = self._parse_build_args(build_args)
        if builds and app_key != "api":
            raise UsageError("--build-arg changes the API image only")
        if app_key == "tools" and "neo4j" in window["apps"]:
            self._check_vectors()
        if klass is not None and klass not in self.windows["rates_30_day_usd"]:
            raise UsageError(f"{klass!r} is not a priced machine class ({sorted(self.windows['rates_30_day_usd'])})")
        self._require_state(key)
        self._print_quote(key)
        self._deploy_app(key, app_key, pairs, klass, builds)
        return 0

    # ---- preflight -----------------------------------------------------------------------------------------------

    def _bolt(self, state: dict) -> Any:
        return self.bolt_factory(state) if self.bolt_factory else ProxyBolt(self, state)

    def _expected_examples(self) -> int:
        path = self.repo / "src" / "semigraph" / "artifacts" / "examples.json"
        try:
            return max(1, len(json.loads(path.read_text(encoding="utf-8"))["examples"]))
        except (OSError, ValueError, KeyError):
            return 1

    @staticmethod
    def _n(rows: list[dict]) -> int:
        return int(rows[0]["n"]) if rows else 0

    def _database_checks(self, bolt: Any) -> list[tuple[str, bool, str]]:
        ledger, counters = self._n(bolt.run(Q_LEDGER_NODES)), self._n(bolt.run(Q_DAY_COUNTERS))
        examples, kill = self._n(bolt.run(Q_EXAMPLES)), bolt.run(Q_KILL)
        level = kill[0]["v"] if kill else None
        return [("ledger rows", ledger == 0, f"{ledger} ledger/session nodes (Svc ledger labels, User*) must be 0"),
                ("day counters", counters == 0, f"{counters} non-zero day counters must be 0"),
                ("examples", examples >= self._expected_examples(), f"{examples} cached examples, {self._expected_examples()} expected"),
                ("kill", level in (None, "off"), f"kill level {level!r} must be absent or off")]

    def _api_checks(self, state: dict) -> list[tuple[str, bool, str]]:
        api = self.windows["apps"]["api"]["name"]
        names = _names_in(self._fly(["secrets", "list", "--app", api, "--json"]).stdout, "secrets")
        missing, forbidden = sorted(set(REQUIRED_API_SECRETS) - names), sorted(FORBIDDEN_API_SECRETS & names)
        checks = [("secrets", not missing and not forbidden, f"missing {missing}, live-linked {forbidden}")]
        try:
            env = json.loads(self._fly(["config", "show", "--app", api]).stdout.decode("utf-8"))["env"]
            host = lambda url: urlparse(env.get(url, "")).hostname  # noqa: E731
            neo4j, mock = self.windows["apps"]["neo4j"]["name"], self.windows["apps"]["mockllm"]["name"]
            ok = (env.get("ENVIRONMENT") == "staging" and host("NEO4J_URI") == f"{neo4j}.internal"
                  and host("OPENAI_API_BASE") == f"{mock}.internal")
            detail = f"ENVIRONMENT={env.get('ENVIRONMENT')!r}, database host {host('NEO4J_URI')!r}, model host {host('OPENAI_API_BASE')!r}"
        except (ValueError, KeyError, UnicodeDecodeError, AttributeError):
            ok, detail = False, "could not read the deployed configuration"
        return [*checks, ("config", ok, detail)]

    def preflight(self, key: str) -> int:
        window = self._window(key)
        state = self._require_state(key)
        if self.plan:
            self.out(f"+ preflight {key}: bolt checks {[Q_LEDGER_NODES, Q_DAY_COUNTERS, Q_EXAMPLES, Q_KILL]}")
            if "api" in window["apps"]:
                self._api_checks(state)
            return 0
        results = []
        if "neo4j" in window["apps"]:
            with self._bolt(state) as bolt:
                results += self._database_checks(bolt)
        if "api" in window["apps"]:
            results += self._api_checks(state)
        for name, ok, detail in results:
            self.out(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
        failed = [name for name, ok, _ in results if not ok]
        self.out("preflight: PASS" if not failed else f"preflight: not clean ({len(failed)}): {', '.join(failed)}")
        return 1 if failed else 0

    # ---- reset-ledger ---------------------------------------------------------------------------------------------

    def _machines(self, app: str) -> list[dict]:
        rows = self._json(["machine", "list", "--app", app, "--json"]) or []
        return [m for m in rows if isinstance(m, dict) and m.get("state") != "destroyed"]

    def reset_ledger(self, key: str, *, restart_api: bool = False, cache: bool = False) -> int:
        window = self._window(key)
        state = self._require_state(key)
        api = self.windows["apps"]["api"]["name"] if "api" in window["apps"] else None
        queries = [q_delete(label) for label in RESET_LABELS] + ([CACHE_DELETE] if cache else [])
        if self.plan:
            if api:
                self.out(f"+ stop the running machines of {api}, then:")
            for query in queries:
                self.out(f"+ bolt: {query}")
            self.out(f"+ start the machines of {api} again" if api and restart_api else "")
            return 0
        running = []
        if api:
            running = [m for m in self._machines(api) if m.get("state") not in STOPPED_STATES]
            if running and not restart_api:
                raise UsageError(f"the staging API is running ({len(running)} machine(s)): stop it first, or pass "
                                 "--restart-api to stop, reset and start it. The in-process backend keeps its counters in "
                                 "memory and rebuilds them from the ledger only at boot, and a delete would run under live leases.")
            self._stop(api, running)
        with self._bolt(state) as bolt:
            for label in RESET_LABELS:
                before = self._n(bolt.run(q_count(label)))
                bolt.run(q_delete(label))
                self.out(f"reset {label}: {before} -> {self._n(bolt.run(q_count(label)))}")
            if cache:
                bolt.run(CACHE_DELETE)
                self.out("reset SvcAnswer: removed the answers that are not seeded examples")
        for machine in running:
            self._fly(["machine", "start", machine["id"], "--app", api])
        if running:
            self.out(f"restarted {len(running)} API machine(s): the counters were rebuilt from the empty ledger at boot")
        return 0

    def _stop(self, app: str, running: list[dict]) -> None:
        for machine in running:
            self._fly(["machine", "stop", machine["id"], "--app", app])
        for _ in range(STOP_POLLS):
            if all(m.get("state") in STOPPED_STATES for m in self._machines(app)):
                return
            self.sleep(STOP_POLL_S)
        raise FlyError(f"the machines of {app} did not stop in time")

    # ---- snapshot ---------------------------------------------------------------------------------------------------

    def _write(self, path: Path, data: bytes) -> None:
        for value in sorted(self._known_secrets, key=len, reverse=True):
            data = data.replace(value.encode("utf-8"), REDACTED.encode("utf-8"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _extract_tools(self, payload: bytes, out: Path) -> None:
        start = payload.find(GZIP_MAGIC)
        if start < 0:
            self.out("snapshot: the tools machine returned no archive")
            return
        total = 0
        with tarfile.open(fileobj=io.BytesIO(payload[start:]), mode="r:gz") as tar:
            for member in tar:
                parts = Path(member.name).parts
                if not member.isfile() or member.name.startswith(("/", "\\")) or ".." in parts:
                    continue
                total += member.size
                if total > MAX_EXTRACT_BYTES:
                    raise FlyError("the tools archive is larger than the extraction limit")
                name = "/".join(p for p in parts if p != ".")
                self._write(out / "tools-out" / name, tar.extractfile(member).read())

    def snapshot(self, key: str, out_dir: Path) -> int:
        window = self._window(key)
        state = self._require_state(key)
        out = Path(out_dir)
        for app_key in window["apps"]:
            name = self.windows["apps"][app_key]["name"]
            if self.plan:
                for args in (["status", "--app", name, "--json"], ["machine", "list", "--app", name, "--json"],
                             ["logs", "--app", name, "--no-tail"]):
                    self._fly(args)
                continue
            self._write(out / f"status-{name}.json", self._fly(["status", "--app", name, "--json"]).stdout)
            self._write(out / f"machines-{name}.json", self._fly(["machine", "list", "--app", name, "--json"]).stdout)
            self._write(out / f"logs-{name}.txt", self._fly(["logs", "--app", name, "--no-tail"], timeout=600).stdout)
            if app_key == "tools":
                self._extract_tools(self._fly(["ssh", "console", "--app", name, "-C", TOOLS_TAR_COMMAND], timeout=600).stdout, out)
        if self.plan:
            self.out(f"+ bolt: {Q_LEDGER_COUNTS}")
            return 0
        if "neo4j" in window["apps"]:
            with self._bolt(state) as bolt:
                counts = {"ledger": bolt.run(Q_LEDGER_COUNTS), "counters": bolt.run(Q_COUNTERS)}
            self._write(out / "ledger-counts.json", json.dumps(counts, indent=1, default=str).encode("utf-8"))
        files = [{"name": p.relative_to(out).as_posix(), "bytes": p.stat().st_size,
                  "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(out.rglob("*")) if p.is_file()
                 and p.name != "snapshot.json"]
        manifest = {"window": key, "taken_at": datetime.fromtimestamp(self.now(), UTC).isoformat(timespec="seconds"),
                    "files": files}
        self._write(out / "snapshot.json", json.dumps(manifest, indent=1).encode("utf-8"))
        self.out(f"snapshot: {len(files)} files in {out} (secrets scrubbed)")
        return 0

    # ---- destroy ----------------------------------------------------------------------------------------------------

    def destroy(self, key: str, *, yes: bool = False) -> int:
        window = self._window(key)
        self._print_quote(key)
        if self.plan:
            for app_key in window["apps"]:
                self._fly(["apps", "destroy", self.windows["apps"][app_key]["name"], "--yes"])
            return 0
        if not yes:
            raise UsageError(f"destroy removes every app of window {key} (machines, volumes, secrets): pass --yes")
        self._load_state(key)
        present = _names_in(self._fly(["apps", "list", "--json"]).stdout, "apps")
        removed, absent = [], []
        for app_key in window["apps"]:
            name = self.windows["apps"][app_key]["name"]
            if name not in present:
                absent.append(name)
                self.out(f"absent (nothing to remove): {name}")
                continue
            machines = [m.get("id") for m in self._machines(name)]
            volumes = [v.get("id") for v in (self._json(["volumes", "list", "--app", name, "--json"]) or [])]
            self._fly(["apps", "destroy", name, "--yes"])
            removed.append({"app": name, "machines": machines, "volumes": volumes})
            self.out(f"destroyed {name}: machines {machines or 'none'}, volumes {volumes or 'none'}")
        left = sorted(set(self.windows["apps"][a]["name"] for a in window["apps"])
                      & _names_in(self._fly(["apps", "list", "--json"]).stdout, "apps"))
        report = {"window": key, "destroyed": removed, "absent": absent, "still_listed": left}
        self.out("destroy report: " + json.dumps(report))
        if left:
            self.out(f"STILL LISTED after destroy: {', '.join(left)}; the window's state file is kept")
            return 1
        self.out(f"verified gone: all {len(window['apps'])} apps of window {key} are absent from `apps list`")
        self._state_path(key).unlink(missing_ok=True)
        return 0


# ---- the command line ---------------------------------------------------------------------------------------------------

def build_parser(windows: dict) -> argparse.ArgumentParser:
    keys, apps = sorted(windows["windows"]), sorted(windows["apps"])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--plan", action="store_true", default=argparse.SUPPRESS,
                        help="print every flyctl command and run nothing")
    parser = argparse.ArgumentParser(prog="staging.py", description=__doc__.splitlines()[0], parents=[common])
    parser.add_argument("--state-dir", type=Path, default=None, help="where the window's secrets live (outside the repo)")
    sub = parser.add_subparsers(dest="command", required=True)
    q = sub.add_parser("quote", parents=[common], help="print the cost table; calls nothing")
    q.add_argument("windows", nargs="*", metavar="WINDOW")
    q.add_argument("--option")
    c = sub.add_parser("create", parents=[common], help="create the window's apps, volume and secrets")
    c.add_argument("window", choices=keys)
    c.add_argument("--org", default=os.environ.get("FLY_ORG", ""))
    c.add_argument("--region", default="sin")
    c.add_argument("--approve-quote", default=None, metavar="USD")
    c.add_argument("--option")
    c.add_argument("--no-network", action="store_true", help="skip the per-window private network (UNVERIFIED flag)")
    for name, help_text in (("seed", "deploy the staging database from the baked dump"),
                            ("preflight", "check the window started clean"),
                            ("destroy", "destroy every app of the window and verify")):
        s = sub.add_parser(name, parents=[common], help=help_text)
        s.add_argument("window", choices=keys)
        if name == "destroy":
            s.add_argument("--yes", action="store_true")
    d = sub.add_parser("deploy", parents=[common], help="deploy one app of the window")
    d.add_argument("window", choices=keys)
    d.add_argument("app", choices=[a for a in apps if a != "neo4j"])
    d.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE")
    d.add_argument("--build-arg", dest="build_args", action="append", default=[], metavar="NAME=VALUE",
                   help="EMBEDDER_VARIANT=q8|fp32, KEEP_UNPATCHED=0|1 (the API image only)")
    d.add_argument("--class", dest="klass", default=None)
    r = sub.add_parser("reset-ledger", parents=[common], help="empty the ledger and counters (API stopped)")
    r.add_argument("window", choices=keys)
    r.add_argument("--restart-api", action="store_true")
    r.add_argument("--cache", action="store_true", help="also remove the answers that are not seeded examples")
    s = sub.add_parser("snapshot", parents=[common], help="collect the evidence files")
    s.add_argument("window", choices=keys)
    s.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, runner: Any = None, out: Callable[[str], None] | None = None,
         err: Callable[[str], None] | None = None, state_dir: Path | None = None, bolt_factory: Any = None,
         gen: Callable[[str], str] = default_secret, windows_path: Path | None = None) -> int:
    args = build_parser(load_windows(windows_path)).parse_args(argv)
    try:
        tool = Staging(runner=runner, out=out, err=err, state_dir=state_dir or args.state_dir, gen=gen,
                       plan=getattr(args, "plan", False), bolt_factory=bolt_factory, windows_path=windows_path)
        match args.command:
            case "quote":
                return tool.quote(args.windows, args.option)
            case "create":
                return tool.create(args.window, org=args.org, region=args.region, approve_quote=args.approve_quote,
                                   option=args.option, use_network=not args.no_network)
            case "seed":
                return tool.seed(args.window)
            case "deploy":
                return tool.deploy(args.window, args.app, sets=args.sets, klass=args.klass, build_args=args.build_args)
            case "preflight":
                return tool.preflight(args.window)
            case "reset-ledger":
                return tool.reset_ledger(args.window, restart_api=args.restart_api, cache=args.cache)
            case "snapshot":
                return tool.snapshot(args.window, args.out)
            case "destroy":
                return tool.destroy(args.window, yes=args.yes)
    except (UsageError, RegistryError, FlyError) as exc:
        (err or (lambda line: print(line, file=sys.stderr)))(f"staging: error: {exc}")
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
