"""Ad-hoc operator scripts run isolated (``python -I``) from a root-only directory, never from ``/tmp`` (Opus security review of the
H1 diff, 2026-11-07).

The operator procedure in ``docs/RUNBOOK.md`` (and the docstring of ``scripts/null_legacy_ip_hashes.py``, which repeats it) stages the
script in ``/root/ops`` (``mkdir -m 700 -p /root/ops``), a directory only root can use, and runs it as
``sh -c 'cd /root/ops && python -I <script>.py'`` so the run does not depend on the ssh session's working directory. ``python -I``
leaves the script directory, the working directory and the user site off ``sys.path`` and ignores every ``PYTHON*`` variable; the
application settings still come from the machine's environment (none of them is a ``PYTHON*`` variable). ``-I`` is safe for this
image because the package is installed into site-packages, not found through ``PYTHONPATH``. Every ``python`` that the RUNBOOK runs
through ``flyctl ssh console -C`` carries ``-I``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STAGED_IN = "/root/ops"
SCRIPT = "null_legacy_ip_hashes.py"
TEXTS = ["docs/RUNBOOK.md", "scripts/null_legacy_ip_hashes.py"]        # the procedure and the script docstring that repeats it


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def console_commands(text: str) -> list[str]:
    """The double-quoted command of every ``flyctl ssh console ... -C "<command>"`` in a text."""
    return re.findall(r'ssh console[^\n]*?-C "([^"]*)"', text)


def python_invocations(command: str) -> list[str]:
    """Each ``python`` word in a console command, with what follows it up to the next shell separator."""
    return re.findall(r"(?<![\w./-])(python[0-9.]*\b[^;&|]*)", command)


@pytest.mark.parametrize("relative", TEXTS)
def test_no_documented_command_runs_python_from_tmp(relative):
    offending = [line.strip() for line in read(relative).splitlines() if re.search(r"\bpython[0-9.]*\b.*\s/tmp/", line)]
    assert offending == []


@pytest.mark.parametrize("relative", TEXTS)
def test_the_script_is_not_staged_in_tmp(relative):
    assert f"/tmp/{SCRIPT}" not in read(relative)


@pytest.mark.parametrize("relative", TEXTS)
def test_no_ssh_console_command_touches_tmp(relative):
    assert [c for c in console_commands(read(relative)) if "/tmp" in c] == []


def test_every_python_in_an_ssh_console_command_in_the_runbook_runs_isolated():
    """``-c``, ``-m`` and script runs alike, now or added later."""
    invocations = [i for command in console_commands(read("docs/RUNBOOK.md")) for i in python_invocations(command)]
    assert invocations, "the RUNBOOK documents no python run at all"
    for invocation in invocations:
        assert re.match(r"python[0-9.]*\s+-I\b", invocation), invocation


def test_every_python_in_an_ssh_console_command_in_the_script_docstring_runs_isolated():
    invocations = [i for command in console_commands(read("scripts/null_legacy_ip_hashes.py")) for i in python_invocations(command)]
    assert invocations, "the docstring documents no python run at all"
    for invocation in invocations:
        assert re.match(r"python[0-9.]*\s+-I\b", invocation), invocation


@pytest.mark.parametrize("relative", TEXTS)
def test_every_ssh_console_script_run_comes_from_the_root_only_directory_whatever_the_working_dir(relative):
    runs = [c for c in console_commands(read(relative)) if re.search(r"python[0-9.]*\s[^']*\.py", c)]
    assert runs, "the procedure documents no script run at all"
    for run in runs:
        assert re.fullmatch(rf"sh -c 'cd {STAGED_IN} && python -I {SCRIPT}( --yes)?'", run), run


@pytest.mark.parametrize("relative", TEXTS)
def test_the_null_script_is_staged_then_run_dry_then_run_for_real_in_that_order(relative):
    text = read(relative)
    steps = [
        f"mkdir -m 700 -p {STAGED_IN}",
        f"put scripts/{SCRIPT} {STAGED_IN}/{SCRIPT}",
        f"cd {STAGED_IN} && python -I {SCRIPT}'\"",
        f"cd {STAGED_IN} && python -I {SCRIPT} --yes'\"",
    ]
    positions = [text.find(step) for step in steps]
    assert all(p >= 0 for p in positions), dict(zip(steps, positions))
    assert positions == sorted(positions), "the steps are out of order"


def test_the_runbook_says_why_the_script_is_not_put_in_tmp():
    text = read("docs/RUNBOOK.md").lower()
    assert "/tmp" in text and "writable by everyone" in text and "-i" in text


def test_the_runtime_image_has_no_pythonpath_so_isolated_mode_still_finds_the_package():
    """``python -I`` ignores PYTHONPATH. The main image installs the package into site-packages (``uv pip install --system ... .``,
    not editable) and sets no PYTHONPATH, so the documented command can import ``semigraph`` and ``neo4j``."""
    dockerfile = read("Dockerfile")
    assert "PYTHONPATH" not in dockerfile
    assert re.search(r"uv pip install --system --no-cache --no-deps \.\s", dockerfile)
    assert "PYTHONPATH" not in read("fly.toml")


@pytest.fixture
def local_module(tmp_path):
    """A directory with a script that imports ``json`` and a same-named local ``json.py`` next to it."""
    (tmp_path / "json.py").write_text("LOCAL_COPY = True\n", encoding="utf-8")
    (tmp_path / "script.py").write_text("import json\nprint(getattr(json, 'LOCAL_COPY', False))\n", encoding="utf-8")
    return tmp_path


def run_script(flags: list[str], directory: Path, env_extra: dict[str, str] | None = None) -> str:
    inherited = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}   # keeps SYSTEMROOT and the rest
    env = {**inherited, **(env_extra or {})}
    done = subprocess.run([sys.executable, *flags, str(directory / "script.py")], capture_output=True, text=True, cwd=directory,
                          env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_the_control_without_isolated_mode_imports_a_same_named_local_module(local_module):
    """So the test below fails for the reason it claims to: the script directory really is first on ``sys.path``."""
    assert run_script([], local_module) == "True"


def test_the_control_ignores_python_variables_set_in_the_test_runner(local_module, monkeypatch):
    """``run_script`` starts from the ambient environment minus every ``PYTHON*`` variable, so a runner that sets
    ``PYTHONSAFEPATH`` (which drops the script directory from ``sys.path``) cannot make the control above pass for the wrong reason."""
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")
    assert run_script([], local_module) == "True"


def test_isolated_mode_ignores_a_same_named_local_module_and_pythonpath(local_module):
    other = local_module / "elsewhere"
    other.mkdir()
    (other / "json.py").write_text("LOCAL_COPY = True\n", encoding="utf-8")
    assert run_script(["-I"], local_module, {"PYTHONPATH": str(other)}) == "False"
