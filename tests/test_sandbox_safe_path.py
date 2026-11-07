"""The sandboxed upload workers import only from the image's own install and ``PYTHONPATH`` (2026-11-07).

Two entries stay off a worker's ``sys.path``, and its argv does it:

- the working directory: ``python -m module`` puts it first, ahead of ``site-packages``. ``-P`` (Python 3.11+, the image has
  3.13) drops that entry.
- the user site directory (``~/.local/lib/python3.x/site-packages``): ``site`` finds it through the passwd entry even when the
  child's environment has no ``HOME``. ``-s`` turns the user site off.

Both are argv flags, not ``PYTHONSAFEPATH`` / ``PYTHONNOUSERSITE`` in ``child_env``: the child's environment is an exact
allowlist (``test_child_env_allowlist_is_exactly_the_documented_set``), and a flag does not depend on what the parent's
environment holds. ``-I`` would do both but also implies ``-E``, which drops the ``PYTHONPATH`` the child needs to find
``semigraph`` (under pytest it is not installed). Both child command builders carry the flags: ``sandbox.run_sandboxed`` (the
comparison worker) and ``parse._worker_command`` (the parse worker, which handles the untrusted bytes first).
"""

from __future__ import annotations

import json
import sys

import pytest

from semigraph.uploads import parse, sandbox

MARKER = "SAME-NAMED-PACKAGE-RAN-IN-THE-CHILD"
FLAGS = ["-s", "-P"]          # no user site, no working directory on sys.path


def test_the_one_child_command_builder_puts_the_flags_before_the_module():
    cmd = sandbox.module_command("semigraph.uploads.compare_worker", "a", "b")
    assert cmd == [sys.executable, *FLAGS, "-m", "semigraph.uploads.compare_worker", "a", "b"]


def test_run_sandboxed_starts_its_child_with_the_flags(monkeypatch):
    seen = {}

    def capture(cmd, stdin, **kwargs):
        seen["cmd"] = cmd
        return sandbox.SandboxResult(stdout=b"{}", returncode=0)

    monkeypatch.setattr(sandbox, "run_sandboxed_command", capture)
    sandbox.run_sandboxed("semigraph.uploads.compare_worker", [], b"{}", timeout_s=5, max_output_bytes=1024)
    assert seen["cmd"][:4] == [sys.executable, *FLAGS, "-m"], seen["cmd"]


def test_the_parse_worker_is_started_with_the_flags():
    cmd = parse._worker_command("pdf", 30)
    assert cmd == [sys.executable, *FLAGS, "-m", "semigraph.uploads.parse_worker", "pdf", "30"]


def write_a_same_named_package(directory):
    """A ``semigraph`` package in ``directory`` whose ``compare_worker`` prints a marker instead of doing the real work."""
    package = directory / "semigraph" / "uploads"
    package.mkdir(parents=True)
    (directory / "semigraph" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "compare_worker.py").write_text(f'print("{MARKER}")\n', encoding="utf-8")


def test_a_same_named_package_in_the_working_directory_is_not_imported_by_a_sandboxed_child(monkeypatch, tmp_path):
    """End to end: the child is started with a working directory that holds a ``semigraph`` package of the real one's name. The real
    worker answers (``{"error": "comparison_failed"}`` for an empty request: the shape of its own failure report), the other
    package never runs."""
    write_a_same_named_package(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = sandbox.run_sandboxed("semigraph.uploads.compare_worker", [], b"{}", timeout_s=60, max_output_bytes=1024 * 1024)
    assert MARKER.encode() not in result.stdout
    assert json.loads(result.stdout)["error"] == "comparison_failed"


def test_the_control_without_the_path_flag_does_import_the_same_named_package(monkeypatch, tmp_path):
    """Without ``-P`` the working directory's package wins, so the test above fails for the reason it claims to and not for some
    other one."""
    write_a_same_named_package(tmp_path)
    monkeypatch.chdir(tmp_path)
    cmd = [sys.executable, "-m", "semigraph.uploads.compare_worker"]
    result = sandbox.run_sandboxed_command(cmd, b"{}", timeout_s=60, max_output_bytes=1024 * 1024, env=sandbox.child_env())
    assert MARKER.encode() in result.stdout


def user_site_enabled(interpreter: str, flags: list[str]) -> bool:
    """What ``python <flags> -m site`` reports for the user site, run under the real child environment."""
    cmd = [interpreter, *flags, "-m", "site"]
    result = sandbox.run_sandboxed_command(cmd, b"", timeout_s=60, max_output_bytes=1024 * 1024, env=sandbox.child_env())
    text = result.stdout.decode("utf-8", "replace")
    assert "ENABLE_USER_SITE:" in text, text
    return "ENABLE_USER_SITE: True" in text


def test_a_sandboxed_child_does_not_read_the_user_site_directory():
    """The flags are taken from ``module_command`` itself and put on the base interpreter: a virtual environment (this repo's
    ``.venv`` is one) switches the user site off by itself, which would hide a missing ``-s``. The control run drops only
    ``-s``; where the base interpreter has the user site off already (``PYTHONNOUSERSITE``, a locked-down build) the two cannot
    be told apart and the test says so."""
    base = getattr(sys, "_base_executable", sys.executable)
    flags = sandbox.module_command("site")[1:-2]            # between the interpreter and ``-m site``
    assert flags == FLAGS
    if not user_site_enabled(base, [f for f in flags if f != "-s"]):
        pytest.skip("the user site is already off for this interpreter, so -s cannot be told apart here")
    assert user_site_enabled(base, flags) is False
