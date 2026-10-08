"""scripts/staging.py: the staging window tool (M5a I5; docs/v2/M5_DECISIONS.md 2.1 "Staging rules").

``quote | create | seed | deploy | preflight | reset-ledger | snapshot | destroy``, every ``flyctl`` call a subprocess argument list,
nothing run for real. The runner is a stub for the whole file and ``subprocess.run`` / ``Popen`` are poisoned, so nothing can
escape it. What is pinned:

* the quote: the section 2 figures come from ``deploy/staging/windows.json`` and are printed BEFORE the first call (one shared
  event log of printed lines and runner calls); the arithmetic (30-day price / 720) is at most the quote, which is below the cap;
* money is gated: ``create`` refuses without ``--approve-quote`` equal to the quote, and every later spend command checks that the
  window was approved;
* secrets: generated per window (sentinel values in the tests), pushed ONLY on the stdin of ``flyctl secrets import``, never in an
  argument list, a printed line, a plan or a snapshot; the state file that keeps them lives outside the repository;
* ``--plan`` makes no call at all and writes nothing;
* the registry: a production app name is refused before the first call, and again at the one function every call goes through;
* each command's flyctl calls (create order, the explicit Dockerfile and context of every deploy, the stop -> reset -> start order
  of reset-ledger, the destroy listing and its verification) and preflight's every failing branch.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tarfile
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("staging_script", ROOT / "scripts" / "staging.py")
staging = importlib.util.module_from_spec(spec)
sys.modules["staging_script"] = staging
spec.loader.exec_module(staging)

WINDOWS = json.loads((ROOT / "deploy" / "staging" / "windows.json").read_text(encoding="utf-8"))


class Sentinels:
    """Secret values the test can search for everywhere: unique, long enough for the validators, never real."""

    def __init__(self):
        self.values = {}

    def __call__(self, kind):
        value = f"SENTINEL-{kind}-{len(self.values):02d}-" + "q" * 40
        self.values[kind] = value
        return value


class Log:
    def __init__(self):
        self.events = []

    def out(self, line=""):
        self.events.append(("out", str(line)))

    def lines(self):
        return [text for kind, text in self.events if kind == "out"]

    def calls(self):
        return [payload for kind, payload in self.events if kind == "run"]


class Run(staging.Result):
    pass


class FakeRunner:
    """Records every call into the shared log; answers from ``responses`` (first argument prefix that matches)."""

    def __init__(self, log, responses=()):
        self.log, self.responses, self.calls = log, list(responses), []

    def run(self, args, *, stdin=None, cwd=None, timeout=None):
        assert isinstance(args, list) and all(isinstance(a, str) for a in args), args
        call = {"args": list(args), "stdin": stdin, "cwd": cwd}
        self.calls.append(call)
        self.log.events.append(("run", call))
        for prefix, reply in self.responses:
            if args[:len(prefix)] == list(prefix):
                result = reply(call) if callable(reply) else reply
                if isinstance(result, staging.Result):
                    return result
                return staging.Result(0, result if isinstance(result, bytes) else str(result).encode(), b"")
        return staging.Result(0, b"", b"")


class FakeBolt:
    def __init__(self, handler):
        self.handler, self.queries = handler, []

    def run(self, cypher, **params):
        self.queries.append(cypher)
        return self.handler(cypher, params)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def nothing_escapes_the_stub(monkeypatch):
    def poisoned(*args, **kwargs):
        raise AssertionError("a real process was started")

    monkeypatch.setattr(subprocess, "run", poisoned)
    monkeypatch.setattr(subprocess, "Popen", poisoned)


def machines_json(*states):
    return json.dumps([{"id": f"m{i}", "state": s, "name": f"n{i}"} for i, s in enumerate(states)]).encode()


@pytest.fixture
def world(tmp_path):
    log, sentinels = Log(), Sentinels()
    runner = FakeRunner(log)
    holder = {"bolt": None}

    def make(plan=False, responses=(), bolt=None, **kwargs):
        runner.responses[:] = list(responses)
        holder["bolt"] = bolt
        tool = staging.Staging(runner=runner, out=log.out, err=log.out, state_dir=tmp_path / "state", plan=plan,
                               gen=sentinels, bolt_factory=(lambda state: holder["bolt"]) if bolt is not None else None,
                               sleep=lambda s: None, **kwargs)
        return tool

    return type("World", (), {"log": log, "runner": runner, "sentinels": sentinels, "make": staticmethod(make),
                              "state_dir": tmp_path / "state"})


def create_args(window="W4", **kw):
    return {"org": "test-org", "approve_quote": f"{WINDOWS['windows'][window]['quote_usd']:.2f}", **kw}


# --- the quote -----------------------------------------------------------------------------------------------------------------

SECTION_2 = {"W0": ("$0.05", "$0.10"), "W1": ("$0.08", "$0.15"), "W3": ("$0.45", "$0.60"), "W4": ("$1.25", "$2.00")}


def test_the_quote_reproduces_the_section_two_table(world):
    tool = world.make()
    assert tool.quote(["W0", "W1", "W2", "W3", "W4"]) == 0
    text = "\n".join(world.log.lines())
    for key, (quote, cap) in SECTION_2.items():
        row = next(line for line in world.log.lines() if line.startswith(key))
        assert f"{quote} ({cap})" in row
    w2 = next(line for line in world.log.lines() if line.startswith("W2"))
    assert "--max-usd 0.50" in w2
    assert "$0.25" in text and "$1.60" in text                   # the L5-only and the four-worker options
    assert world.runner.calls == []


def quote_columns(world, window, *, option=None):
    """The five ' | ' columns of one printed quote row: the window's own, or its option's (an indented 'option ...' row)."""
    world.make().quote([window])
    lines = world.log.lines()
    row = next(line for line in lines if (line.strip().startswith(f"option {option}") if option else line.startswith(window)))
    return [column.strip() for column in row.split(" | ")]


L5_ONLY_DURATION = "seed 20 + baseline 15 + drain soak <=120 + L5 60 min"


def test_the_l5_only_option_states_its_own_phases_not_the_phases_of_the_whole_window(world):
    """The option is priced at 3.6 h (seed, baseline, drain soak, L5); its row used to list the window's full duration, the
    control run and the L10 / L20 levels it does not run included. The file now gives it its own ``duration``: the four phases
    it runs, and the minutes in them (20 + 15 + at most 120 + 60 = 215, 3.58 h) fit the 3.6 h it is priced at."""
    window, option = quote_columns(world, "W3"), quote_columns(world, "W3", option="L5 only")
    assert "L20 60" in window[2] and "control 30" in window[2]            # the whole window runs all of them
    assert option[2] == L5_ONLY_DURATION and "3.6 h x" in option[3]       # the option's own text, priced at 3.6 h
    assert not any(phase in option[2] for phase in ("L10", "L20", "control"))
    minutes = sum(int(n) for n in re.findall(r"\b\d+\b", option[2]))             # "L5" is a name: no word boundary inside it
    hours = next(o["hours"] for o in WINDOWS["windows"]["W3"]["options"] if "L5 only" in o["name"])
    assert minutes == 20 + 15 + 120 + 60 and minutes / 60 <= hours


def test_an_option_without_its_own_duration_text_states_the_hours_it_is_priced_at(tmp_path):
    """The fallback of ``staging_quote._duration_text`` (an option in the file with no ``duration``): its priced hours, never the
    whole window's phases."""
    doc = json.loads(json.dumps(WINDOWS))
    del doc["windows"]["W3"]["options"][0]["duration"]
    path = tmp_path / "windows.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    log = Log()
    staging.Staging(runner=FakeRunner(log), out=log.out, windows_path=path, state_dir=tmp_path).quote(["W3"])
    row = next(line for line in log.lines() if line.strip().startswith("option L5 only"))
    assert row.split(" | ")[2].startswith("3.6 h") and "control" not in row.split(" | ")[2]


def test_an_options_own_duration_text_replaces_the_windows(tmp_path):
    doc = json.loads(json.dumps(WINDOWS))
    doc["windows"]["W3"]["options"][0]["duration"] = "seed 20 + baseline 15 + drain soak <=120 + L5 60 min"
    path = tmp_path / "windows.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    log = Log()
    staging.Staging(runner=FakeRunner(log), out=log.out, windows_path=path, state_dir=tmp_path).quote(["W3"])
    row = next(line for line in log.lines() if line.strip().startswith("option L5 only"))
    assert row.split(" | ")[2] == "seed 20 + baseline 15 + drain soak <=120 + L5 60 min"


def test_four_load_workers_means_the_master_plus_four_which_is_five_machines_and_the_label_says_so(world):
    """"4 load workers" counted only the workers: the plan's Locust master plus 4 workers is five generator machines, and the
    row, the arithmetic and the quote the owner approves ($1.60) all price five. The option's own name now says so, the
    machine counts in the file add up to it, and the row prints '5 x'."""
    w4 = WINDOWS["windows"]["W4"]
    option = next(o for o in w4["options"] if o["name"].startswith("4 load workers"))
    loadgen = [m for m in w4["machines"] if m["app"] == "loadgen"] + [m for m in option["extra_machines"] if m["app"] == "loadgen"]
    assert sum(m["count"] for m in loadgen) == 5 == 4 + 1                    # the window's 3 + the option's 2
    assert "master" in option["name"] and "5 generator machines" in option["name"] and "4 load workers" in option["name"]
    columns = quote_columns(world, "W4", option="4 load workers")
    assert "5 x performance-1x 2gb (loadgen)" in columns[1] and "3 x performance-1x 2gb (loadgen)" not in columns[1]


def test_the_four_worker_option_lists_every_load_machine_it_prices(world):
    """The option adds two generators to the window's three (the plan's Locust master plus 4 workers = five machines): its row
    said '3 x loadgen' while its arithmetic and quote already paid for five."""
    window, option = quote_columns(world, "W4"), quote_columns(world, "W4", option="4 load workers")
    assert "3 x performance-1x 2gb (loadgen)" in window[1] and "5 x" not in window[1]
    assert "5 x performance-1x 2gb (loadgen)" in option[1] and "3 x performance-1x 2gb (loadgen)" not in option[1]
    assert option[1].count("(loadgen)") == 1                               # one merged entry, not three plus two
    loadgen_hours = sum(float(term.split(" h")[0]) for term in option[3].split(" + ") if term.endswith("$0.0582") and " x " in term)
    assert loadgen_hours >= 5 * 3.0 + 3.5                                  # the five generators' 15 h are in the arithmetic (plus mockllm's 3.5)


def test_the_arithmetic_is_the_hourly_price_and_never_above_the_quote_which_is_below_the_cap():
    windows = staging.load_windows()
    assert staging.hourly_rate(windows, "shared-cpu-2x:4gb") == pytest.approx(0.044778, abs=1e-5)
    assert staging.hourly_rate(windows, "performance-2x:4gb") == pytest.approx(0.116347, abs=1e-5)
    assert staging.derived_usd(windows, "W0") == pytest.approx(0.0448, abs=5e-4)
    assert staging.derived_usd(windows, "W1") == pytest.approx(0.5 * 0.0448 + 0.5 * 0.1163, abs=1e-3)
    assert staging.derived_usd(windows, "W3") == pytest.approx(6.1 * (0.0118 + 0.0582), abs=0.005)
    assert staging.derived_usd(windows, "W4") == pytest.approx(3.5 * (0.1163 + 0.0118 + 0.0582) + 9 * 0.0582, abs=0.03)
    for key in ("W0", "W1", "W2", "W3", "W4"):
        quote, cap = staging.quote_usd(windows, key)
        assert staging.derived_usd(windows, key) <= quote + 0.005 and quote <= cap
    assert staging.quote_usd(windows, "W3", "L5 only")[0] == pytest.approx(0.25)
    assert staging.derived_usd(windows, "W3", "L5 only") <= 0.25 + 0.005
    assert staging.quote_usd(windows, "W4", "4 load workers")[0] == pytest.approx(1.60)


def test_the_quote_comes_from_the_constants_file_not_from_the_code(tmp_path):
    doc = json.loads(json.dumps(WINDOWS))
    doc["windows"]["W0"]["quote_usd"], doc["windows"]["W0"]["cap_usd"] = 0.07, 0.11
    path = tmp_path / "windows.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    log = Log()
    staging.Staging(runner=FakeRunner(log), out=log.out, windows_path=path, state_dir=tmp_path).quote(["W0"])
    assert "$0.07 ($0.11)" in "\n".join(log.lines())


def test_the_quote_is_printed_before_the_first_call_of_every_spending_command(world):
    tool = world.make(plan=True)
    for command in (lambda: tool.create("W4", **create_args()), lambda: tool.destroy("W4")):
        world.log.events.clear()
        command()
        first_call = next((i for i, (kind, _) in enumerate(world.log.events) if kind == "run"), None)
        quote_line = next(i for i, (kind, text) in enumerate(world.log.events) if kind == "out" and "$1.25 ($2.00)" in text)
        assert first_call is None or quote_line < first_call
    world.log.events.clear()
    world.make().create("W4", **create_args())
    first_call = next(i for i, (kind, _) in enumerate(world.log.events) if kind == "run")
    assert any("$1.25 ($2.00)" in text for kind, text in world.log.events[:first_call] if kind == "out")


# --- money is gated ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("approval", [None, "", "0.05", "1.26", "free"])
def test_create_refuses_without_the_approval_of_exactly_this_quote_and_calls_nothing(world, approval):
    with pytest.raises(staging.UsageError):
        world.make().create("W4", org="test-org", approve_quote=approval)
    assert world.runner.calls == [] and not world.state_dir.exists()


def test_create_needs_an_org(world):
    with pytest.raises(staging.UsageError):
        world.make().create("W4", org="", approve_quote="1.25")
    assert world.runner.calls == []


def test_spending_commands_need_a_created_and_approved_window(world):
    tool = world.make()
    for command in (lambda: tool.deploy("W4", "api"), lambda: tool.seed("W4"), lambda: tool.preflight("W4"),
                    lambda: tool.reset_ledger("W4"), lambda: tool.snapshot("W4", Path("x"))):
        with pytest.raises(staging.UsageError):
            command()
    assert world.runner.calls == []


# --- create and secrets ---------------------------------------------------------------------------------------------------------------

def create_commands(world, window="W4", **kwargs):
    world.make(**kwargs).create(window, **create_args(window))
    return [c["args"] for c in world.runner.calls]


def test_create_makes_the_apps_the_volume_the_public_addresses_and_stages_the_secrets_in_order(world):
    calls = create_commands(world)
    names = [c[:3] for c in calls]
    assert all(c[0] == "flyctl" for c in calls)
    created = [c[3] for c in calls if c[1:3] == ["apps", "create"]]
    assert created == ["semigraph-stg", "semigraph-neo4j-stg", "semigraph-mockllm", "semigraph-loadgen-stg"]
    for c in calls:
        if c[1:3] == ["apps", "create"]:
            assert c[c.index("--org") + 1] == "test-org" and "--network" in c
    assert ["flyctl", "volumes", "create", "neo4j_data"] == calls[next(i for i, c in enumerate(calls) if c[1] == "volumes")][:4]
    volume = next(c for c in calls if c[1] == "volumes")
    assert volume[volume.index("--app") + 1] == "semigraph-neo4j-stg" and volume[volume.index("--size") + 1] == "3"
    assert ["flyctl", "ips", "allocate-v6", "--app", "semigraph-stg"] in calls
    imports = [c for c in calls if c[1:3] == ["secrets", "import"]]
    assert {c[c.index("--app") + 1] for c in imports} == {"semigraph-stg", "semigraph-neo4j-stg", "semigraph-mockllm",
                                                          "semigraph-loadgen-stg"}
    assert all("--stage" in c for c in imports)
    assert names.index(["flyctl", "apps", "create"]) < names.index(["flyctl", "secrets", "import"])


def test_secrets_reach_only_the_stdin_of_secrets_import_never_an_argument_a_line_or_the_repository(world):
    create_commands(world)
    values = list(world.sentinels.values.values())
    assert values
    for call in world.runner.calls:
        for value in values:
            assert all(value not in arg for arg in call["args"]), call["args"]
        if call["stdin"] is not None:
            assert call["args"][1:3] == ["secrets", "import"]
    for value in values:
        assert all(value not in line for line in world.log.lines())
    by_app = {c["args"][c["args"].index("--app") + 1]: dict(line.split("=", 1) for line in c["stdin"].splitlines())
              for c in world.runner.calls if c["stdin"] is not None}
    api = by_app["semigraph-stg"]
    assert set(api) == {"IP_HASH_PEPPER", "ADMIN_TOKEN", "ORIGIN_AUTH_SECRET", "OPENAI_API_KEY", "NEO4J_PASSWORD"}
    assert api["OPENAI_API_KEY"].startswith("mock-") and len(api["IP_HASH_PEPPER"]) >= 32 and len(api["ADMIN_TOKEN"]) >= 32
    assert by_app["semigraph-neo4j-stg"]["NEO4J_AUTH"] == "neo4j/" + api["NEO4J_PASSWORD"]
    assert by_app["semigraph-loadgen-stg"]["LOADTEST_ORIGIN_AUTH"] == api["ORIGIN_AUTH_SECRET"]
    assert "ANTHROPIC_API_KEY" not in json.dumps(by_app) and "SEC_USER_AGENT" not in json.dumps(by_app)
    state = json.loads((world.state_dir / "W4.json").read_text(encoding="utf-8"))
    assert state["secrets"]["api"]["IP_HASH_PEPPER"] == api["IP_HASH_PEPPER"] and state["approved_quote_usd"] == 1.25
    assert (ROOT / "scripts" / "staging.py").read_text(encoding="utf-8").count("SENTINEL") == 0


def test_secrets_are_new_for_every_window(world):
    create_commands(world, "W3")
    first = dict(world.sentinels.values)
    world.runner.calls.clear()
    create_commands(world, "W4")
    assert first and all(world.sentinels.values[kind] != value for kind, value in first.items())
    states = [json.loads((world.state_dir / f"{w}.json").read_text(encoding="utf-8")) for w in ("W3", "W4")]
    assert states[0]["secrets"]["neo4j"] != states[1]["secrets"]["neo4j"]


def test_the_state_file_cannot_be_put_inside_the_repository(tmp_path):
    with pytest.raises(staging.UsageError):
        staging.Staging(state_dir=ROOT / "artifacts" / "staging-state", runner=FakeRunner(Log()))


def test_create_refuses_a_window_that_already_exists_and_leaves_its_secrets_alone(world):
    create_commands(world)
    before = (world.state_dir / "W4.json").read_text(encoding="utf-8")
    world.runner.calls.clear()
    with pytest.raises(staging.UsageError):
        world.make().create("W4", **create_args())
    assert world.runner.calls == [] and (world.state_dir / "W4.json").read_text(encoding="utf-8") == before


def test_a_failure_part_way_leaves_the_state_so_destroy_can_clean_up(world):
    bad = [(["flyctl", "volumes", "create"], staging.Result(1, b"", b"quota exceeded"))]
    with pytest.raises(staging.FlyError):
        world.make(responses=bad).create("W4", **create_args())
    assert (world.state_dir / "W4.json").is_file()


# --- --plan ---------------------------------------------------------------------------------------------------------------------

def test_plan_mode_makes_no_call_writes_nothing_and_prints_every_command_without_a_secret(world):
    tool = world.make(plan=True)
    tool.create("W4", org="test-org")                 # no approval needed: nothing is spent
    tool.destroy("W4")
    assert world.runner.calls == [] and not world.state_dir.exists()
    text = "\n".join(world.log.lines())
    for needle in ("flyctl apps create semigraph-stg", "flyctl volumes create neo4j_data", "flyctl secrets import",
                   "flyctl apps destroy semigraph-stg"):
        assert needle in text
    for value in world.sentinels.values.values():
        assert value not in text
    assert "<generated>" in text or "generated per window" in text


# --- the registry ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["destroy", "semigraph", "--yes"], ["deploy", "W4", "semigraph"], ["deploy", "semigraph-neo4j", "api"],
                                  ["create", "semigraph-neo4j", "--org", "o"], ["preflight", "semigraph"]])
def test_a_production_name_on_the_command_line_is_refused_before_any_call(argv, world, tmp_path):
    with pytest.raises(SystemExit) as e:
        staging.main(argv, runner=world.runner, out=world.log.out, state_dir=tmp_path)
    assert e.value.code == 2 and world.runner.calls == []


@pytest.mark.parametrize("args", [["apps", "destroy", "semigraph", "--yes"], ["apps", "destroy", "semigraph-neo4j"],
                                  ["status", "--app", "semigraph"], ["machine", "list", "-a", "semigraph-neo4j"],
                                  ["deploy", "--app=semigraph"], ["volumes", "list", "--app", "semigraph-neo4j"]])
def test_the_one_function_every_call_goes_through_refuses_a_production_app(world, args):
    tool = world.make()
    with pytest.raises(staging.RegistryError):
        tool._fly(args)
    assert world.runner.calls == []


def test_the_registry_has_no_live_app_and_every_registered_app_may_be_called(world):
    tool = world.make()
    tool._fly(["status", "--app", "semigraph-stg"])
    tool._fly(["apps", "destroy", "semigraph-neo4j-stg", "--yes"])
    assert len(world.runner.calls) == 2


def test_the_subprocess_runner_runs_an_argument_list_without_a_shell(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return subprocess.CompletedProcess(args, 0, b"out", b"err")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = staging.SubprocessRunner().run(["flyctl", "status"], stdin="a=b\n", cwd="somewhere", timeout=5)
    assert result == staging.Result(0, b"out", b"err")
    assert seen["args"] == ["flyctl", "status"] and not seen["kwargs"].get("shell")
    assert seen["kwargs"]["input"] == b"a=b\n" and seen["kwargs"]["cwd"] == "somewhere" and seen["kwargs"]["timeout"] == 5


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["flyctl"], 5), FileNotFoundError("flyctl")],
                         ids=["timeout", "no-flyctl"])
def test_a_timeout_or_a_missing_flyctl_is_a_clean_error_not_a_traceback(world, failure):
    class Broken:
        def run(self, args, **kwargs):
            raise failure

    tool = staging.Staging(runner=Broken(), out=world.log.out, state_dir=world.state_dir)
    with pytest.raises(staging.FlyError):
        tool._fly(["status", "--app", "semigraph-stg"])
    assert staging.main(["quote", "W0"], runner=Broken(), out=world.log.out, state_dir=world.state_dir) == 0


def test_a_failing_call_names_the_command_not_the_secrets_in_its_error(world):
    create_commands(world)
    secret = next(iter(world.sentinels.values.values()))
    bad = [(["flyctl", "deploy"], staging.Result(1, b"", f"boom {secret}".encode()))]
    with pytest.raises(staging.FlyError) as e:
        world.make(responses=bad).deploy("W4", "api")
    assert secret not in str(e.value) and "deploy" in str(e.value) and "[redacted]" in str(e.value)


# --- seed and deploy --------------------------------------------------------------------------------------------------------------

def test_seed_needs_the_baked_dump_to_exist_and_calls_nothing_without_it(world, tmp_path):
    create_commands(world, "W3")
    world.runner.calls.clear()
    with pytest.raises(staging.UsageError, match="neo4j.dump"):
        world.make(dump_path=tmp_path / "missing" / "neo4j.dump").seed("W3")
    assert world.runner.calls == []


def test_seed_deploys_the_baked_dump_with_the_dockerfile_and_context_named_and_never_takes_a_dump_argument(world, tmp_path):
    create_commands(world, "W3")
    world.runner.calls.clear()
    dump = tmp_path / "neo4j.dump"
    dump.write_bytes(b"not a real dump")
    world.make(dump_path=dump).seed("W3")
    deploy = next(c for c in world.runner.calls if c["args"][1] == "deploy")
    args = deploy["args"]
    assert args[args.index("--app") + 1] == "semigraph-neo4j-stg"
    assert args[args.index("--dockerfile") + 1] == "Dockerfile" and deploy["cwd"] == str(ROOT / "deploy" / "neo4j")
    assert args[args.index("--config") + 1] == "../staging/fly.neo4j-stg.toml"
    assert "--remote-only" in args and "--no-public-ips" in args and "--ha=false" in args
    assert args[args.index("--vm-size") + 1] == "shared-cpu-1x" and args[args.index("--vm-memory") + 1] == "1024"
    assert not any("dump" in a.lower() for a in args)
    assert any("neo4j.dump" in line for line in world.log.lines())          # says which file it loads


def test_deploy_api_names_the_dockerfile_the_context_the_class_and_the_overrides(world):
    create_commands(world)
    world.runner.calls.clear()
    world.make().deploy("W4", "api", sets=["MAX_QUERIES_PER_DAY=150", "PAID_PER_IP_PER_DAY=20"])
    call = next(c for c in world.runner.calls if c["args"][1] == "deploy")
    args = call["args"]
    assert call["cwd"] == str(ROOT)
    assert args[args.index("--config") + 1] == "deploy/staging/fly.stg.toml"
    assert args[args.index("--dockerfile") + 1] == "Dockerfile"
    assert args[args.index("--vm-size") + 1] == "performance-2x" and args[args.index("--vm-memory") + 1] == "4096"
    assert "--no-public-ips" not in args and "--remote-only" in args
    envs = [args[i + 1] for i, a in enumerate(args) if a == "--env"]
    assert envs == ["MAX_QUERIES_PER_DAY=150", "PAID_PER_IP_PER_DAY=20"]


def test_the_production_settings_sub_run_can_put_every_paid_ask_cap_back_including_the_address_share(world):
    from semigraph.config import PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD
    create_commands(world)
    world.runner.calls.clear()
    values = ["MAX_QUERIES_PER_DAY=150", "MAX_SPEND_USD_PER_DAY=10", "PAID_PER_IP_PER_DAY=20",
              f"PAID_SPEND_SHARE_PER_IP_USD={PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD}"]
    world.make().deploy("W4", "api", sets=values)
    args = next(c["args"] for c in world.runner.calls if c["args"][1] == "deploy")
    assert [args[i + 1] for i, a in enumerate(args) if a == "--env"] == values
    assert "PAID_SPEND_SHARE_PER_IP_USD=1.32" in values                 # the live ceiling, written the way ``--set`` passes it


def test_the_embedder_build_arguments_reach_the_deploy_of_the_api_image_for_the_timing_window(world):
    create_commands(world, "W1")
    world.runner.calls.clear()
    world.make().deploy("W1", "api", build_args=["EMBEDDER_VARIANT=fp32", "KEEP_UNPATCHED=1"])
    args = next(c["args"] for c in world.runner.calls if c["args"][1] == "deploy")
    assert [args[i + 1] for i, a in enumerate(args) if a == "--build-arg"] == ["EMBEDDER_VARIANT=fp32", "KEEP_UNPATCHED=1"]
    assert "--remote-only" in args and args[args.index("--dockerfile") + 1] == "Dockerfile"


@pytest.mark.parametrize("app, build_arg", [("api", "SEC_USER_AGENT=someone"), ("api", "KEEP_UNPATCHED=2"), ("api", "EMBEDDER_VARIANT=fp16"),
                                            ("api", "EMBEDDER_VARIANT"), ("api", "=fp32"), ("api", "PYTHON_IMAGE=evil"),
                                            ("mockllm", "KEEP_UNPATCHED=1")])
def test_only_the_two_embedder_build_arguments_with_their_own_values_are_accepted(world, app, build_arg):
    create_commands(world, "W4")
    world.runner.calls.clear()
    with pytest.raises(staging.UsageError):
        world.make().deploy("W4", app, build_args=[build_arg])
    assert world.runner.calls == []


def pool_sha():
    return json.loads((ROOT / "tools" / "loadtest" / "pool.json").read_text(encoding="utf-8"))["sha256"]


def test_the_s7_tools_image_is_not_deployed_without_the_pool_vectors_it_reads(world, tmp_path):
    create_commands(world, "W3")
    world.runner.calls.clear()
    with pytest.raises(staging.UsageError, match="vectors"):
        world.make(vectors_path=tmp_path / "missing" / "vectors.json").deploy("W3", "tools")
    assert world.runner.calls == []


def test_vectors_built_for_another_pool_are_refused_and_the_right_ones_deploy(world, tmp_path):
    create_commands(world, "W3")
    path = tmp_path / "vectors.json"
    path.write_text(json.dumps({"pool_sha256": "0" * 64, "vectors": {}}), encoding="utf-8")
    world.runner.calls.clear()
    with pytest.raises(staging.UsageError, match="another pool"):
        world.make(vectors_path=path).deploy("W3", "tools")
    assert world.runner.calls == []
    path.write_text(json.dumps({"pool_sha256": pool_sha(), "vectors": {}}), encoding="utf-8")
    world.make(vectors_path=path).deploy("W3", "tools")
    assert any(c["args"][1] == "deploy" for c in world.runner.calls)


def test_the_probe_window_needs_no_vectors_and_a_plan_only_warns(world, tmp_path):
    create_commands(world, "W0")
    world.runner.calls.clear()
    world.make(vectors_path=tmp_path / "missing.json").deploy("W0", "tools")                  # W0 has no database: no replay
    assert any(c["args"][1] == "deploy" for c in world.runner.calls)
    plan_world = world.make(plan=True, vectors_path=tmp_path / "missing.json")
    plan_world.deploy("W3", "tools")                                                          # --plan runs nothing: it only says so
    assert any("NOTE (a real deploy would refuse)" in line for line in world.log.lines())


@pytest.mark.parametrize("app, dockerfile", [("mockllm", "deploy/staging/Dockerfile.mockllm"),
                                             ("loadgen", "deploy/staging/Dockerfile.loadgen")])
def test_deploy_private_apps_use_their_dockerfile_from_the_repository_root_without_public_addresses(world, app, dockerfile):
    create_commands(world)
    world.runner.calls.clear()
    world.make().deploy("W4", app)
    args = next(c["args"] for c in world.runner.calls if c["args"][1] == "deploy")
    assert args[args.index("--dockerfile") + 1] == dockerfile and "--no-public-ips" in args


def test_the_class_follows_the_window_unless_named_and_must_be_a_priced_one(world):
    create_commands(world, "W0")
    world.runner.calls.clear()
    world.make().deploy("W0", "tools")
    args = world.runner.calls[-1]["args"]
    assert args[args.index("--vm-size") + 1] == "shared-cpu-2x" and args[args.index("--vm-memory") + 1] == "4096"
    with pytest.raises(staging.UsageError):
        world.make().deploy("W0", "tools", klass="performance-8x:32gb")


@pytest.mark.parametrize("setting", ["ANTHROPIC_API_KEY=x", "ADMIN_TOKEN=y", "NEO4J_URI=bolt://semigraph-neo4j.internal:7687",
                                     "MAX_QUERIES_PER_DAY", "MAX_QUERIES_PER_DAY=1;rm -rf", "max_queries_per_day=1", "=1",
                                     "FLY_APP_NAME=semigraph", "ENVIRONMENT=production", "TURNSTILE_STUB=false"])
def test_only_allowlisted_non_secret_settings_can_be_set_at_deploy(world, setting):
    create_commands(world)
    world.runner.calls.clear()
    with pytest.raises(staging.UsageError):
        world.make().deploy("W4", "api", sets=[setting])
    assert world.runner.calls == []


def test_deploy_refuses_an_app_the_window_does_not_have_and_neo4j_goes_through_seed(world):
    create_commands(world, "W0")
    world.runner.calls.clear()
    for app in ("api", "mockllm"):
        with pytest.raises(staging.UsageError):
            world.make().deploy("W0", app)
    create_commands(world, "W3")
    with pytest.raises(staging.UsageError):
        world.make().deploy("W3", "neo4j")


# --- preflight --------------------------------------------------------------------------------------------------------------------

SECRETS_OK = json.dumps([{"Name": n} for n in ("IP_HASH_PEPPER", "ADMIN_TOKEN", "ORIGIN_AUTH_SECRET", "OPENAI_API_KEY",
                                                "NEO4J_PASSWORD")]).encode()
CONFIG_OK = json.dumps({"env": {"ENVIRONMENT": "staging", "NEO4J_URI": "bolt://semigraph-neo4j-stg.internal:7687",
                                "OPENAI_API_BASE": "http://semigraph-mockllm.internal:8000/v1"}}).encode()
EXAMPLES = len(json.loads((ROOT / "src" / "semigraph" / "artifacts" / "examples.json").read_text(encoding="utf-8"))["examples"])


def healthy(cypher, params):
    return {staging.Q_LEDGER_NODES: [{"n": 0}], staging.Q_DAY_COUNTERS: [{"n": 0}],
            staging.Q_EXAMPLES: [{"n": EXAMPLES}], staging.Q_KILL: []}[cypher]


def preflight_world(world, *, secrets=SECRETS_OK, config=CONFIG_OK, handler=healthy):
    create_commands(world)
    world.runner.calls.clear()
    responses = [(["flyctl", "secrets", "list"], secrets), (["flyctl", "config", "show"], config)]
    bolt = FakeBolt(handler)
    return world.make(responses=responses, bolt=bolt), bolt


def test_preflight_passes_on_a_clean_seeded_staging(world):
    tool, bolt = preflight_world(world)
    assert tool.preflight("W4") == 0
    text = "\n".join(world.log.lines())
    assert "PASS" in text and "FAIL" not in text
    assert all(call["args"][1] in ("secrets", "config") for call in world.runner.calls)


@pytest.mark.parametrize("name, handler", [
    ("ledger rows", lambda c, p: [{"n": 3}] if c == staging.Q_LEDGER_NODES else healthy(c, p)),
    ("day counters", lambda c, p: [{"n": 1}] if c == staging.Q_DAY_COUNTERS else healthy(c, p)),
    ("examples", lambda c, p: [{"n": 0}] if c == staging.Q_EXAMPLES else healthy(c, p)),
    ("kill", lambda c, p: [{"v": "on"}] if c == staging.Q_KILL else healthy(c, p)),
    ("kill", lambda c, p: [{"v": "retrieval_only"}] if c == staging.Q_KILL else healthy(c, p)),
])
def test_preflight_fails_on_each_database_condition(world, name, handler):
    tool, _ = preflight_world(world, handler=handler)
    assert tool.preflight("W4") == 1
    assert f"FAIL {name}" in "\n".join(world.log.lines())


def test_a_kill_level_of_off_or_none_is_clean(world):
    tool, _ = preflight_world(world, handler=lambda c, p: [{"v": "off"}] if c == staging.Q_KILL else healthy(c, p))
    assert tool.preflight("W4") == 0


@pytest.mark.parametrize("secret", ["ANTHROPIC_API_KEY", "SEC_USER_AGENT", "TURNSTILE_SECRET_KEY", "LANGFUSE_SECRET_KEY"])
def test_preflight_fails_when_a_live_linked_secret_is_set_on_the_staging_api(world, secret):
    listed = json.loads(SECRETS_OK) + [{"Name": secret}]
    tool, _ = preflight_world(world, secrets=json.dumps(listed).encode())
    assert tool.preflight("W4") == 1 and f"FAIL secrets" in "\n".join(world.log.lines())


def test_preflight_fails_when_a_required_secret_is_missing(world):
    tool, _ = preflight_world(world, secrets=json.dumps([{"Name": "ADMIN_TOKEN"}]).encode())
    assert tool.preflight("W4") == 1


@pytest.mark.parametrize("env", [{"ENVIRONMENT": "production"},
                                 {"NEO4J_URI": "bolt://semigraph-neo4j.internal:7687"},
                                 {"OPENAI_API_BASE": "https://api.openai.com/v1"}])
def test_preflight_fails_when_the_deployed_config_points_anywhere_but_staging(world, env):
    doc = json.loads(CONFIG_OK)
    doc["env"].update(env)
    tool, _ = preflight_world(world, config=json.dumps(doc).encode())
    assert tool.preflight("W4") == 1 and "FAIL config" in "\n".join(world.log.lines())


def test_preflight_fails_when_the_deployed_config_cannot_be_read(world):
    tool, _ = preflight_world(world, config=b"not json")
    assert tool.preflight("W4") == 1


# --- reset-ledger -----------------------------------------------------------------------------------------------------------------

def reset_world(world, machines):
    create_commands(world)
    world.runner.calls.clear()
    state = {"machines": list(machines)}

    def listing(call):
        return machines_json(*state["machines"])

    def stopper(call):
        state["machines"] = ["stopped"] * len(state["machines"])
        return b""

    responses = [(["flyctl", "machine", "list"], listing), (["flyctl", "machine", "stop"], stopper),
                 (["flyctl", "machine", "start"], b"")]
    bolt = FakeBolt(lambda c, p: [{"n": 7}])
    return world.make(responses=responses, bolt=bolt), bolt


def test_reset_ledger_refuses_while_the_api_is_running(world):
    tool, bolt = reset_world(world, ["started"])
    with pytest.raises(staging.UsageError, match="stop"):
        tool.reset_ledger("W4")
    assert bolt.queries == [] and all(c["args"][1:3] != ["machine", "stop"] for c in world.runner.calls)


def test_reset_ledger_on_a_stopped_api_deletes_only_the_ledger_rows_and_counters(world):
    tool, bolt = reset_world(world, ["stopped"])
    assert tool.reset_ledger("W4") == 0
    deleting = [q for q in bolt.queries if "DELETE" in q]
    labels = " ".join(deleting)
    for label in ("SvcQuery", "SvcDayCounter", "SvcIpDay", "SvcUploadDay"):
        assert label in labels
    assert "SvcAnswer" not in labels and "SvcPolicy" not in labels and "Snapshot" not in labels
    assert all("IN TRANSACTIONS" in q for q in deleting)


def test_reset_ledger_with_cache_removes_the_live_answers_but_never_the_seeded_examples(world):
    tool, bolt = reset_world(world, ["stopped"])
    tool.reset_ledger("W4", cache=True)
    answers = [q for q in bolt.queries if "SvcAnswer" in q and "DELETE" in q]
    assert len(answers) == 1 and "source <> 'benchmark'" in answers[0]


def test_reset_ledger_restart_api_goes_stop_then_reset_then_start(world):
    tool, bolt = reset_world(world, ["started", "started"])
    events = []
    bolt.handler = lambda c, p: (events.append("db"), [{"n": 1}])[1]
    original = world.runner.run

    def recording(args, **kw):
        if args[1:3] in (["machine", "stop"], ["machine", "start"]):
            events.append(args[2])
        return original(args, **kw)

    world.runner.run = recording
    assert tool.reset_ledger("W4", restart_api=True) == 0
    assert events.index("stop") < events.index("db") < events.index("start")
    assert events.count("stop") == 2 and events.count("start") == 2


# --- snapshot and destroy -----------------------------------------------------------------------------------------------------------

def test_snapshot_writes_the_evidence_files_a_manifest_and_scrubs_every_secret(world, tmp_path):
    create_commands(world, "W3")
    secret = world.sentinels.values["db_password"]
    out = tmp_path / "snap"
    tarball = io.BytesIO()
    with tarfile.open(fileobj=tarball, mode="w:gz") as tar:
        payload = b'{"op":"x"}\n'
        info = tarfile.TarInfo("level.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    responses = [(["flyctl", "logs"], f"2026-10-08 neo4j started; password {secret}\nOutOfMemoryError: Java heap space\n"),
                 (["flyctl", "status"], json.dumps({"Name": "x", "note": secret})),
                 (["flyctl", "machine", "list"], machines_json("started")),
                 (["flyctl", "ssh", "console"], b"banner\n" + tarball.getvalue())]
    world.runner.calls.clear()
    bolt = FakeBolt(lambda c, p: [{"status": "settled", "n": 5}])
    assert world.make(responses=responses, bolt=bolt).snapshot("W3", out) == 0
    manifest = json.loads((out / "snapshot.json").read_text(encoding="utf-8"))
    names = {f["name"] for f in manifest["files"]}
    assert {"logs-semigraph-neo4j-stg.txt", "machines-semigraph-neo4j-stg.json", "status-semigraph-neo4j-stg.json",
            "tools-out/level.json"} <= names
    assert all(len(f["sha256"]) == 64 and f["bytes"] > 0 for f in manifest["files"])
    for path in out.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8", errors="replace"), path
    assert "OutOfMemoryError" in (out / "logs-semigraph-neo4j-stg.txt").read_text(encoding="utf-8")
    assert (out / "ledger-counts.json").is_file()


def destroy_world(world, present):
    create_commands(world)
    world.runner.calls.clear()
    apps = json.dumps([{"Name": name} for name in present]).encode()
    state = {"apps": list(present)}

    def listing(call):
        return json.dumps([{"Name": name} for name in state["apps"]]).encode()

    def destroyer(call):
        state["apps"].remove(call["args"][3])
        return b""

    responses = [(["flyctl", "apps", "list"], listing), (["flyctl", "apps", "destroy"], destroyer),
                 (["flyctl", "machine", "list"], machines_json("started")),
                 (["flyctl", "volumes", "list"], json.dumps([{"id": "vol_1", "name": "neo4j_data", "size_gb": 3}]).encode())]
    return world.make(responses=responses), apps


def test_destroy_needs_yes_then_lists_what_it_removed_verifies_and_forgets_the_secrets(world, tmp_path):
    tool, _ = destroy_world(world, ["semigraph-stg", "semigraph-neo4j-stg", "semigraph-mockllm", "semigraph-loadgen-stg"])
    with pytest.raises(staging.UsageError):
        tool.destroy("W4")
    assert not any(c["args"][1:3] == ["apps", "destroy"] for c in world.runner.calls)
    assert tool.destroy("W4", yes=True) == 0
    destroyed = [c["args"][3] for c in world.runner.calls if c["args"][1:3] == ["apps", "destroy"]]
    assert destroyed == ["semigraph-stg", "semigraph-neo4j-stg", "semigraph-mockllm", "semigraph-loadgen-stg"]
    assert all("--yes" in c["args"] for c in world.runner.calls if c["args"][1:3] == ["apps", "destroy"])
    text = "\n".join(world.log.lines())
    for name in destroyed:
        assert name in text
    assert "vol_1" in text and "m0" in text and "verified gone" in text.lower()
    assert not (world.state_dir / "W4.json").exists()
    listing = [i for i, c in enumerate(world.runner.calls) if c["args"][1:3] == ["machine", "list"]]
    assert listing and listing[0] < next(i for i, c in enumerate(world.runner.calls) if c["args"][1:3] == ["apps", "destroy"])


def test_destroy_skips_an_app_that_is_already_gone_and_reports_a_leftover(world):
    tool, _ = destroy_world(world, ["semigraph-stg"])
    assert tool.destroy("W4", yes=True) == 0
    assert "absent" in "\n".join(world.log.lines()).lower()
    assert [c["args"][3] for c in world.runner.calls if c["args"][1:3] == ["apps", "destroy"]] == ["semigraph-stg"]


def test_destroy_fails_loudly_when_an_app_is_still_listed_afterwards(world):
    create_commands(world)
    world.runner.calls.clear()
    stubborn = json.dumps([{"Name": "semigraph-stg"}]).encode()
    tool = world.make(responses=[(["flyctl", "apps", "list"], stubborn), (["flyctl", "machine", "list"], b"[]"),
                                 (["flyctl", "volumes", "list"], b"[]")])
    assert tool.destroy("W4", yes=True) == 1
    assert "still" in "\n".join(world.log.lines()).lower() and (world.state_dir / "W4.json").exists()


# --- the command line ----------------------------------------------------------------------------------------------------------------

def test_main_quote_needs_no_state_and_returns_zero(world, tmp_path, capsys):
    assert staging.main(["quote", "W3"], runner=world.runner, out=world.log.out, state_dir=tmp_path / "s") == 0
    assert "$0.45 ($0.60)" in "\n".join(world.log.lines()) and world.runner.calls == []


def test_main_maps_usage_errors_to_exit_two_and_check_failures_to_one(world, tmp_path):
    code = staging.main(["create", "W4", "--org", "o"], runner=world.runner, out=world.log.out, err=world.log.out,
                        state_dir=tmp_path / "s")
    assert code == 2 and world.runner.calls == []


def test_main_plan_flag_works_before_and_after_the_command(world, tmp_path):
    for argv in (["--plan", "create", "W1", "--org", "o"], ["create", "W1", "--org", "o", "--plan"]):
        assert staging.main(argv, runner=world.runner, out=world.log.out, state_dir=tmp_path / "s") == 0
    assert world.runner.calls == [] and not (tmp_path / "s").exists()


def test_the_script_names_no_live_app_and_no_pdf_library():
    text = (ROOT / "scripts" / "staging.py").read_text(encoding="utf-8")
    assert "pymupdf" not in text.lower() and "PyMuPDF" not in text
    quote_text = (ROOT / "scripts" / "staging_quote.py").read_text(encoding="utf-8")
    assert 'semigraph-neo4j"' not in text
    live_mentions = [line for line in quote_text.splitlines() if 'semigraph-neo4j"' in line]
    assert len(live_mentions) == 1 and live_mentions[0].startswith("LIVE_APP_NAMES")          # the deny-list, nowhere else
    assert "pymupdf" not in quote_text.lower()
    assert "semigraph.internal" not in text + quote_text and "fly.dev" not in text + quote_text
    assert len(text.splitlines()) < 800 and len(quote_text.splitlines()) < 800                  # the soft file-size ceiling
