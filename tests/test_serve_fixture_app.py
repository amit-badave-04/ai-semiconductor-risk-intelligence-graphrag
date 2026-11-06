"""The fixture server and its factory (docs/specs/m5b-frontend.md S13, R16): the process guards itself for its whole life, refuses
to start where it could reach a live system, and serves the same routes as ``main.create_app``.

``install_guards()`` replaces ``Settings.__init__``, ``get_settings`` and both litellm functions process-wide, so every test
that installs it undoes it in a ``finally`` (the ``guards`` fixture). The refusal rules are a pure function over an explicit
environment: nothing here reads the developer's ``os.environ`` (Claude Code's own shell exports ``ANTHROPIC_*``).
"""

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import litellm
import pytest
from fastapi.testclient import TestClient
from serve_app_factory import FIXTURE_CHUNK_ID, FakeSettings, build_app
from serve_state_fakes import fresh_drain  # noqa: F401 - a fixture

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
import serve_fixture_app as fixture_app  # noqa: E402

from semigraph import config  # noqa: E402
from semigraph.config import Settings  # noqa: E402
from semigraph.serve import main  # noqa: E402

pytestmark = pytest.mark.usefixtures("fresh_drain")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """A variable left in the developer's shell (or a CI job) must neither set nor clear what these tests build."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("LITELLM_MODE", "DEV")      # main() forces PRODUCTION in this process; this makes teardown put it back

SCRIPT = Path(__file__).parent / "e2e" / "serve_fixture_app.py"
INDEX_HTML = '<!doctype html><html><head><title>dist</title></head><body><div id="root"></div></body></html>'
QUESTION = "Which HBM suppliers does Nvidia depend on, and which export rules apply?"
CLEAN_ENV = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "HOME", "USERPROFILE", "TEMP", "TMP", "LANG")
             if name in os.environ}                                      # what a Python needs to start; no key, no flag
ORIGINALS = ("get_settings", "completion", "acompletion")


@pytest.fixture
def dist(tmp_path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    return root


@pytest.fixture
def guards():
    installed = fixture_app.install_guards()
    try:
        yield installed
    finally:
        installed.undo()


def originals() -> dict:
    return {"get_settings": config.get_settings, "completion": litellm.completion, "acompletion": litellm.acompletion}


def bound_to(originals_by_name: dict) -> list[str]:
    """Every ``semigraph.*`` module attribute that IS one of the originals."""
    wanted = {id(v): k for k, v in originals_by_name.items()}
    return sorted(f"{m.__name__}.{name}" for m in fixture_app.semigraph_modules()
                  for name, value in list(vars(m).items()) if id(value) in wanted)


# ---------------------------------------------------------------- the refusals (pure)

@pytest.mark.parametrize("env,host,expected", [
    ({}, "127.0.0.1", []),
    ({"ENVIRONMENT": "development", "PATH": "x"}, "127.0.0.1", []),
    ({"ENVIRONMENT": "production"}, "127.0.0.1", ["ENVIRONMENT is production"]),
    ({"ENVIRONMENT": " Production "}, "127.0.0.1", ["ENVIRONMENT is production"]),
    ({"OPENAI_API_KEY": "sk-x"}, "127.0.0.1", ["OPENAI_API_KEY is present in the environment"]),
    ({"ANTHROPIC_API_KEY": ""}, "127.0.0.1", ["ANTHROPIC_API_KEY is present in the environment"]),
    ({}, "0.0.0.0", ["it binds only 127.0.0.1, not '0.0.0.0'"]),
    ({}, "localhost", ["it binds only 127.0.0.1, not 'localhost'"]),
    ({}, "::1", ["it binds only 127.0.0.1, not '::1'"]),
    ({}, "", ["it binds only 127.0.0.1, not ''"]),
])
def test_the_refusal_rules(env, host, expected):
    assert fixture_app.refusals(env, host) == expected


def test_every_reason_is_listed_at_once():
    reasons = fixture_app.refusals({"ENVIRONMENT": "production", "OPENAI_API_KEY": "a", "ANTHROPIC_API_KEY": "b"}, "0.0.0.0")
    assert len(reasons) == 4


# ---------------------------------------------------------------- install_guards: the whole process

def test_the_guards_raise_record_and_leave_no_original_bound(guards):
    import semigraph.embeddings
    import semigraph.llm                                                    # both bind by name

    with pytest.raises(fixture_app.GuardViolation):
        Settings(_env_file=None)
    with pytest.raises(fixture_app.GuardViolation):
        config.get_settings()
    with pytest.raises(fixture_app.GuardViolation):
        semigraph.embeddings.get_settings()
    with pytest.raises(fixture_app.GuardViolation):
        litellm.completion(model="x", messages=[])
    with pytest.raises(fixture_app.GuardViolation):
        litellm.acompletion(model="x", messages=[])
    assert guards.hits == ["Settings()", "get_settings", "get_settings", "litellm.completion", "litellm.acompletion"]
    assert semigraph.llm.completion is litellm.completion and semigraph.llm.completion.__name__ == "refuse"
    guards.hits.clear()
    app = build_app(FakeSettings())                                           # building the app reaches none of them
    assert app is not None and guards.hits == []


def test_a_module_imported_while_the_guards_are_on_gets_the_original_back_on_undo():
    before = originals()
    installed = fixture_app.install_guards()
    probe = types.ModuleType("semigraph._guard_probe")                       # imported late: it binds the raiser by name
    probe.get_settings = config.get_settings
    sys.modules["semigraph._guard_probe"] = probe
    try:
        assert probe.get_settings is not before["get_settings"]
    finally:
        installed.undo()
        sys.modules.pop("semigraph._guard_probe", None)
    assert probe.get_settings is before["get_settings"]
    assert originals() == before and "__init__" not in vars(Settings)
    assert Settings(_env_file=None).ui_v2 is False                            # the real class works again


def refusers() -> list[str]:
    """Every ``semigraph.*`` module attribute that is a guard's raiser."""
    return sorted(f"{m.__name__}.{name}" for m in fixture_app.semigraph_modules()
                  for name, value in list(vars(m).items()) if callable(value) and getattr(value, "__name__", "") == "refuse")


def test_undo_restores_every_binding_and_the_class():
    before = originals()
    installed = fixture_app.install_guards()
    assert refusers()                                                         # the by-name bindings were rewritten
    installed()                                                               # the object is its own undo
    assert originals() == before and "__init__" not in vars(Settings)
    assert refusers() == [] and bound_to(before)                              # and the originals are bound again


def test_no_semigraph_attribute_is_an_original_while_guarded(guards):
    held = {attribute: original for _, attribute, original, _ in guards.swaps}
    assert set(held) == set(ORIGINALS)
    assert bound_to(held) == []                                               # the scan the spec asks for: none of the originals
    assert refusers()                                                         # ... and the raisers are what is bound instead


def test_a_call_whose_error_is_swallowed_still_shows_in_the_hit_list(guards):
    try:
        config.get_settings()
    except Exception:                                                          # noqa: BLE001 - what a broad except in a route does
        pass
    assert guards.hits == ["get_settings"]


# ---------------------------------------------------------------- every I1 flow, driven once, under the guards

def drive(client: TestClient, agent: bool) -> dict[str, int]:
    seen = {}
    for path in ("/api/config", "/api/stats", "/api/examples", "/api/freshness", "/api/company/NVDA/dossier",
                 "/api/company/NVDA/risk-changes", "/api/company/AMD/dossier", f"/api/evidence/{FIXTURE_CHUNK_ID}",
                 "/api/evidence/not-a-chunk", "/healthz", "/", "/legacy", "/v2/", "/v2/company/NVDA/"):
        seen[path] = client.get(path).status_code
    seen["POST hybrid"] = client.post("/api/ask", json={"question": QUESTION, "strategy": "hybrid"}).status_code
    if agent:
        seen["POST agent"] = client.post("/api/ask", json={"question": QUESTION, "strategy": "agent"}).status_code
    return seen


def test_every_flow_of_i1_runs_under_the_guards_and_reaches_no_settings_or_llm(guards, dist, monkeypatch):
    app = fixture_app.build_fixture_app(dist, site_key="dummy-site-key", ui_v2_preview=True, agent_enabled=True, mp=monkeypatch)
    assert guards.hits == []
    with TestClient(app) as client:
        seen = drive(client, agent=True)
        ask = client.post("/api/ask", json={"question": QUESTION, "strategy": "hybrid"})
        config_reply = client.get("/api/config").json()
    assert seen == {"/api/config": 200, "/api/stats": 200, "/api/examples": 200, "/api/freshness": 200,
                    "/api/company/NVDA/dossier": 200, "/api/company/NVDA/risk-changes": 200,
                    "/api/company/AMD/dossier": 404, f"/api/evidence/{FIXTURE_CHUNK_ID}": 200,
                    "/api/evidence/not-a-chunk": 400, "/healthz": 200, "/": 200, "/legacy": 200, "/v2/": 200,
                    "/v2/company/NVDA/": 200, "POST hybrid": 200, "POST agent": 200}
    assert '"event": "done"' in ask.text.replace('"event":"done"', '"event": "done"')
    assert config_reply == {"turnstile_site_key": "dummy-site-key"}          # from FakeSettings, never from a .env
    assert guards.hits == []


def test_a_route_that_reaches_get_settings_fails_the_flow_test(guards, dist, monkeypatch):
    app = fixture_app.build_fixture_app(dist, mp=monkeypatch)

    @app.get("/boom")
    async def boom():
        try:
            config.get_settings()
        except Exception:                                                      # noqa: BLE001 - swallowed on purpose
            return {"swallowed": True}

    with TestClient(app) as client:
        assert client.get("/boom").json() == {"swallowed": True}
    assert guards.hits == ["get_settings"]


# ---------------------------------------------------------------- the factory serves what create_app serves

def test_the_factory_has_the_same_routes_as_create_app(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: Settings(_env_file=None))
    real = {path: set(methods) for path, methods in main.create_app().openapi()["paths"].items()}
    fake = {path: set(methods) for path, methods in build_app(FakeSettings()).openapi()["paths"].items()}
    assert fake == real


def test_the_factory_serves_the_old_page_with_the_flags_off_and_the_app_with_them_on(dist):
    off = TestClient(build_app(FakeSettings(ui_dist_dir=dist)))
    on = TestClient(build_app(FakeSettings(ui_dist_dir=dist, ui_v2=True)))
    assert off.get("/v2/").status_code == 404 and on.get("/v2/").status_code == 200 and on.get("/").text == INDEX_HTML


def test_a_misspelt_setting_is_an_error_not_a_silent_default():
    with pytest.raises(AttributeError, match="ui_v2_previw"):
        FakeSettings(ui_v2_previw=True)


# ---------------------------------------------------------------- as a script

def run_script(*args: str, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=180,
                          env={**CLEAN_ENV, **(env or {})}, cwd=cwd)


def test_run_as_a_script_it_builds_under_the_guards_and_never_imports_serve_main(dist, tmp_path):
    done = run_script("--check", "--dist", str(dist), "--ui-v2-preview", "--site-key", "dummy", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report == {"serve_main_imported": False, "guard_hits": [], "config_status": 200,
                      "config": {"turnstile_site_key": "dummy"}, "spa_served": True, "litellm_local_cost_map": "True",
                      "litellm_mode": "PRODUCTION", "provider_keys_present": []}


@pytest.mark.parametrize("env,args,words", [
    ({"ENVIRONMENT": "production"}, [], "production"),
    ({"OPENAI_API_KEY": "x"}, [], "OPENAI_API_KEY"),
    ({"ANTHROPIC_API_KEY": "x"}, [], "ANTHROPIC_API_KEY"),
    ({}, ["--host", "0.0.0.0"], "127.0.0.1"),
])
def test_run_as_a_script_it_refuses_to_start_and_builds_nothing(dist, tmp_path, env, args, words):
    done = run_script("--check", "--dist", str(dist), *args, env=env, cwd=tmp_path)
    assert done.returncode == fixture_app.EXIT_REFUSED and words in done.stderr and done.stdout == ""


# ---------------------------------------------------------------- S13: the server never reads .env (litellm's import does, in DEV mode)

# litellm/__init__.py calls ``dotenv.load_dotenv()`` on import unless LITELLM_MODE is set to something other than "DEV"
# (the default). find_dotenv then walks up from litellm's own file, through the .venv, to the repo root, so ANY import of
# litellm in the server process loads the repo's .env (every provider key, every Neo4j credential) after the "no provider key
# in the environment" refusal has already passed. These tests run a child process in which load_dotenv is replaced by a
# recorder that reads no file, so nothing here ever opens a .env.
RECORDER = """
import os, sys
sys.path[:0] = [sys.argv[2], sys.argv[3]]
import dotenv, dotenv.main

calls = []
def record(*args, **kwargs):
    calls.append(True)            # reads no file
    return False
dotenv.load_dotenv = record
dotenv.main.load_dotenv = record

mode = sys.argv[1]
rc = 0
if mode == "control":
    import litellm                # noqa: F401
elif mode == "main":
    import serve_fixture_app
    rc = serve_fixture_app.main(["--check", "--dist", sys.argv[4]])
elif mode == "main-injected-key":
    import serve_fixture_app
    build = serve_fixture_app.build_fixture_app
    def build_and_load_a_key(*args, **kwargs):
        os.environ["ANTHROPIC_API_KEY"] = "injected-by-some-other-loader"
        return build(*args, **kwargs)
    serve_fixture_app.build_fixture_app = build_and_load_a_key
    rc = serve_fixture_app.main(["--check", "--dist", sys.argv[4]])
print("LOAD_DOTENV_CALLS", len(calls), "LITELLM_MODE", os.environ.get("LITELLM_MODE"))
sys.exit(rc)
"""


def run_recorder(mode: str, dist: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    script = tmp_path / "recorder.py"
    script.write_text(RECORDER, encoding="utf-8")
    return subprocess.run([sys.executable, str(script), mode, str(SCRIPT.parent), str(SCRIPT.parents[1]), str(dist)],
                          capture_output=True, text=True, timeout=180, env=dict(CLEAN_ENV), cwd=tmp_path)


def load_dotenv_calls(done: subprocess.CompletedProcess) -> int:
    line = next(line for line in done.stdout.splitlines() if line.startswith("LOAD_DOTENV_CALLS"))
    return int(line.split()[1])


def test_positive_control_a_bare_litellm_import_in_dev_mode_does_load_dotenv(dist, tmp_path):
    """Without this the zero below could be a recorder that never fires."""
    done = run_recorder("control", dist, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "LITELLM_MODE None" in done.stdout and load_dotenv_calls(done) >= 1, done.stdout


def test_the_fixture_server_process_never_calls_load_dotenv(dist, tmp_path):
    done = run_recorder("main", dist, tmp_path)
    assert done.returncode == 0, done.stderr
    assert load_dotenv_calls(done) == 0, done.stdout
    assert "LITELLM_MODE PRODUCTION" in done.stdout, done.stdout


def test_a_provider_key_that_appears_while_the_app_is_built_refuses_the_server_and_prints_no_report(dist, tmp_path):
    """The refusal at the top ran before litellm was imported; the same rule runs again after the build, so any other loader
    that put a key into the environment is caught too."""
    done = run_recorder("main-injected-key", dist, tmp_path)
    assert done.returncode == fixture_app.EXIT_REFUSED, done.stderr
    assert "ANTHROPIC_API_KEY" in done.stderr and "injected-by-some-other-loader" not in done.stderr + done.stdout
    assert '"config_status"' not in done.stdout


# ---------------------------------------------------------------- the entry point, in process (no guards are installed here)

def test_the_arguments_default_to_loopback_and_the_built_app(tmp_path):
    args = fixture_app.parse_args([])
    assert args.host == "127.0.0.1" and args.port == fixture_app.DEFAULT_PORT and args.dist == fixture_app.REPO_ROOT / "web" / "dist"
    assert not (args.ui_v2 or args.ui_v2_preview or args.agent or args.check) and args.site_key == ""
    assert fixture_app.parse_args(["--ui-v2", "--dist", str(tmp_path), "--port", "9"]).dist == tmp_path


@pytest.mark.parametrize("env,args", [({"ENVIRONMENT": "production"}, []), ({"OPENAI_API_KEY": "x"}, []),
                                      ({}, ["--host", "0.0.0.0"])])
def test_main_refuses_before_it_installs_a_guard_or_builds_anything(monkeypatch, capsys, dist, env, args):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    before = originals()
    assert fixture_app.main(["--check", "--dist", str(dist), *args]) == fixture_app.EXIT_REFUSED
    assert originals() == before and "__init__" not in vars(Settings)           # nothing was installed
    assert "refuses to start" in capsys.readouterr().err


def test_check_report_names_the_config_the_guards_and_the_app(guards, dist, monkeypatch):
    app = fixture_app.build_fixture_app(dist, site_key="k", ui_v2=True, mp=monkeypatch)
    report = fixture_app.check_report(app, guards)
    assert report["config"] == {"turnstile_site_key": "k"} and report["config_status"] == 200
    assert report["spa_served"] is True and report["guard_hits"] == []


def test_check_report_names_the_provider_keys_that_are_present_never_their_values(guards, dist, monkeypatch):
    monkeypatch.setenv("LITELLM_MODE", "PRODUCTION")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value")
    app = fixture_app.build_fixture_app(dist, mp=monkeypatch)
    report = fixture_app.check_report(app, guards)
    assert report["provider_keys_present"] == ["ANTHROPIC_API_KEY"] and report["litellm_mode"] == "PRODUCTION"
    assert "secret-value" not in json.dumps(report)


def test_main_forces_litellm_out_of_dev_mode_even_when_the_shell_says_dev(monkeypatch, dist):
    """Forced, not setdefault: a DEV in the developer's shell must not win. The refusal path is used so no guard is installed
    in this process; the variable is set first thing, before the refusals are even computed."""
    monkeypatch.setenv("LITELLM_MODE", "DEV")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert fixture_app.main(["--check", "--dist", str(dist)]) == fixture_app.EXIT_REFUSED
    assert os.environ["LITELLM_MODE"] == "PRODUCTION"
