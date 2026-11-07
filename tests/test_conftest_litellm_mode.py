"""A pytest process must never load the owner's ``.env`` just because a test imported litellm (M5b I1 follow-up to S13).

``litellm/__init__.py`` calls ``dotenv.load_dotenv()`` on import unless ``LITELLM_MODE`` is set to something other than ``DEV`` (the
default). ``find_dotenv`` then walks up from litellm's own package directory, through the virtual environment, to the repository root
where the owner's ``.env`` lives, so any pytest process on a developer machine that imports litellm would put every provider key and
Neo4j credential into ``os.environ``. ``tests/conftest.py`` therefore forces ``LITELLM_MODE=PRODUCTION`` before anything can import litellm.

The children below are real pytest sessions over a scratch suite: this repository's ``conftest.py`` is copied byte for byte into it and
loaded by pytest's own conftest discovery, and a plugin loaded first (``-p``) replaces ``dotenv.load_dotenv`` with a recorder that reads no
file. Nothing here opens a ``.env``.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CONFTEST = Path(__file__).with_name("conftest.py")
CLEAN_ENV = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "HOME", "USERPROFILE", "TEMP", "TMP", "LANG")
             if name in os.environ}                                      # what a Python needs to start; no key, no flag, no LITELLM_MODE

# loaded with -p, so it runs before the entry-point plugins and before any conftest
RECORDER_PLUGIN = '''
import dotenv
import dotenv.main

calls = []


def record(*args, **kwargs):
    calls.append(True)            # reads no file
    return False


dotenv.load_dotenv = record
dotenv.main.load_dotenv = record
'''

PROBE_TEST = '''
import os
import sys


def test_probe():
    import litellm  # noqa: F401
    print("PROBE load_dotenv_calls", len(sys.modules["dotenv_recorder"].calls), "litellm_mode", os.environ.get("LITELLM_MODE"))
'''


def run_child_session(tmp_path: Path, *, conftest: bool, env: dict[str, str] | None = None) -> tuple[int, str]:
    """(load_dotenv calls, LITELLM_MODE) seen by a child pytest session that imports litellm; the mode is "None" when it was unset."""
    suite, plugins = tmp_path / "suite", tmp_path / "plugins"
    suite.mkdir()
    plugins.mkdir()
    (suite / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")        # the child's rootdir is the scratch suite, never this repository
    (suite / "test_probe.py").write_text(PROBE_TEST, encoding="utf-8")
    (plugins / "dotenv_recorder.py").write_text(RECORDER_PLUGIN, encoding="utf-8")
    shutil.copyfile(CONFTEST, suite / "conftest.py")
    command = [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", "-p", "dotenv_recorder"]
    if not conftest:
        command.append("--noconftest")
    done = subprocess.run(command, capture_output=True, text=True, timeout=180, cwd=suite,
                          env={**CLEAN_ENV, "PYTHONPATH": str(plugins), **(env or {})})
    assert done.returncode == 0, done.stdout[-800:] + done.stderr[-800:]
    words = next(line for line in done.stdout.splitlines() if line.startswith("PROBE ")).split()
    return int(words[2]), words[4]


def test_this_session_runs_litellm_in_production_mode():
    assert os.environ.get("LITELLM_MODE") == "PRODUCTION"
    imported = sys.modules.get("litellm")           # not imported here: that would load the .env in a session without the fix
    if imported is not None:                        # litellm read the variable when its __init__ ran, so this proves the import order
        assert imported.litellm_mode == "PRODUCTION"


def test_positive_control_a_session_without_the_conftest_loads_dotenv_when_litellm_is_imported(tmp_path):
    """Without this the zero below could be a recorder that never fires."""
    calls, mode = run_child_session(tmp_path, conftest=False)
    assert calls >= 1 and mode == "None"


@pytest.mark.parametrize("exported", [{}, {"LITELLM_MODE": "DEV"}], ids=["variable-unset", "variable-exported-as-DEV"])
def test_a_session_with_the_conftest_never_calls_load_dotenv_when_litellm_is_imported(tmp_path, exported):
    """A shell that exports LITELLM_MODE=DEV does not switch the dotenv loading back on: the conftest forces the value."""
    calls, mode = run_child_session(tmp_path, conftest=True, env=exported)
    assert calls == 0 and mode == "PRODUCTION"
