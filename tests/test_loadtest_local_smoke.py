"""Opt-in local smoke: real Locust (headless, 20 VUs, 30 s) -> a local fake of the staging API -> scripts/loadtest_report.py.

OFF by default. It runs only when ``LOADTEST_RUN_SMOKE=1`` AND Locust is installed in the interpreter named by ``LOADTEST_PYTHON``
(default: this one). Two deliberate choices:

* the env flag is checked FIRST, and Locust is looked up with ``find_spec`` and run in a SUBPROCESS, never imported here
  (``pytest.importorskip("locust")`` would import it): ``import locust`` monkey-patches gevent into the whole interpreter, which
  would change the behaviour of every test that runs after it in the same session;
* Locust is not a dependency of the developer environment (it ships a pytest plugin and pulls in gevent). Install it somewhere
  else and point ``LOADTEST_PYTHON`` at it (a SHORT path on Windows: gevent's DLLs fail to load from a path over MAX_PATH):

      uv venv C:\\lgv --python 3.13
      uv pip install --python C:\\lgv\\Scripts\\python.exe "locust==2.46.7" psutil requests sse-starlette
      $env:LOADTEST_RUN_SMOKE = 1; $env:LOADTEST_PYTHON = "C:\\lgv\\Scripts\\python.exe"
      uv run pytest tests/test_loadtest_local_smoke.py -q

What it proves: the locustfile starts under real Locust, refuses nothing it should accept, drives the pre-registered iteration with
short think times, writes the raw records, and the report turns them into a verdict COMPUTED from the files (profile ``smoke``:
the offered-rate floor is scaled by 20/1000 and the staging-only inputs are skipped). It does not exercise the real API, the
mock LLM, the embedder or Fly: that needs worker A's mock and the staged server settings (see the harness plan, section 3).
"""

import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

if os.environ.get("LOADTEST_RUN_SMOKE") != "1":
    pytest.skip("opt-in: set LOADTEST_RUN_SMOKE=1 (and install locust, see this module's docstring)", allow_module_level=True)

LOCUST_PYTHON = os.environ.get("LOADTEST_PYTHON") or sys.executable
_probe = subprocess.run([LOCUST_PYTHON, "-c", "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('locust') else 1)"],
                        capture_output=True, timeout=60)
if _probe.returncode != 0:
    pytest.skip(f"locust is not installed for {LOCUST_PYTHON}", allow_module_level=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from loadtest_fake_server import ORIGIN_AUTH, FakeStagingServer  # noqa: E402

from tools.loadtest import cpu_watch, salt_check  # noqa: E402
from tools.loadtest.pool import load_pool  # noqa: E402

RUN_SECONDS = 30
VUS = 20
UPLOAD_VUS = 2


def _load_report():
    spec = importlib.util.spec_from_file_location("loadtest_report_smoke", ROOT / "scripts" / "loadtest_report.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["loadtest_report_smoke"] = module
    spec.loader.exec_module(module)
    return module


def test_locust_drives_the_pre_registered_iteration_and_the_report_computes_a_verdict(tmp_path, capsys):
    run_dir = tmp_path / "smoke-run"
    (run_dir / "cpu").mkdir(parents=True)
    result = salt_check.run_checks(load_pool())
    (run_dir / "salt_check.json").write_text(json.dumps(result), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT), "LOADTEST_RUN_ID": "smoke-local", "LOADTEST_ORIGIN_AUTH": ORIGIN_AUTH,
           "LOADTEST_OUT_DIR": str(run_dir), "LOADTEST_SHAPE": "off", "LOADTEST_PHASE": "steady", "LOADTEST_VUS": str(VUS),
           "LOADTEST_THINK_MIN_S": "1", "LOADTEST_THINK_MAX_S": "3", "LOADTEST_UPLOAD_PERIOD_S": "10",
           "LOADTEST_UPLOAD_VUS": str(UPLOAD_VUS), "LOADTEST_WORKER": "0"}
    with FakeStagingServer() as server:
        watch = cpu_watch.CpuWatch("gen-smoke", "generator", match="locust", interval_s=1.0)
        stop = threading.Event()
        sampler = threading.Thread(target=watch.run, args=(run_dir / "cpu" / "gen-smoke.jsonl",),
                                   kwargs={"duration_s": RUN_SECONDS + 15, "stop": stop}, daemon=True)
        sampler.start()
        done = subprocess.run(
            [LOCUST_PYTHON, "-m", "locust", "-f", str(ROOT / "tools" / "loadtest" / "locustfile.py"), "--headless",
             "-u", str(VUS + UPLOAD_VUS), "-r", "22", "-t", f"{RUN_SECONDS}s", "--host", server.url, "--only-summary"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=RUN_SECONDS + 90)
        stop.set()
        sampler.join(15)
        assert done.returncode == 0, done.stderr[-3000:]
        assert server.requests and all(r["headers"].get("x-origin-auth") == ORIGIN_AUTH for r in server.requests)
        assert all(r["headers"].get("x-load-run-id") == "smoke-local" for r in server.requests)
        assert len({r["headers"]["x-test-client-ip"] for r in server.requests}) >= VUS            # one address per VU
        live_questions = [a["body"]["question"] for a in server.asks if a["body"]["question"] not in server.cached_keys]
        assert live_questions
    report = _load_report()
    exit_code = report.main([str(run_dir), "--profile", "smoke", "--vus", str(VUS), "--fly-embed-s", "0.1"])
    printed = capsys.readouterr().out
    out = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert out["verdict"]["result"] in ("PASS", "FAIL", "VOID") and f"VERDICT: {out['verdict']['result']}" in printed
    assert exit_code == {"PASS": 0, "FAIL": 1, "VOID": 2}[out["verdict"]["result"]]
    metrics = out["metrics"]
    assert metrics["live_asks_sent"] > 100 and metrics["dropped"] == 0 and metrics["errors"] == 0
    assert out["validity"]["distinct_salted"] == out["validity"]["live_sends"] > 100 and out["validity"]["live_without_salt"] == 0
    assert metrics["live_ttfe_p95"] is not None and metrics["live_ttfe_p95"] < 1.5
    assert out["verdict"]["result"] == "PASS", out["verdict"]
