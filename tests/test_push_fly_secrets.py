"""``scripts/push_fly_secrets.py``: how it reads ``.env.fly`` (the push itself is covered with the pepper in
``tests/test_serve_guard_pepper.py``).

A file saved by Windows tools (Notepad, PowerShell 5.1 ``Out-File``) can begin with a UTF-8 byte-order mark and use CRLF
line ends. Read as plain UTF-8 the mark becomes part of the FIRST key name (``\\ufeffANTHROPIC_API_KEY``), so that key was
silently "absent" and never pushed. Values here are fixtures, not keys.
"""

import codecs
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def push():
    spec = importlib.util.spec_from_file_location("push_fly_secrets_under_test", ROOT / "scripts" / "push_fly_secrets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIRST_KEY = "ANTHROPIC_API_KEY"
CONTENT = (f"{FIRST_KEY}=first-fixture-value\r\n"                                  # gitleaks:allow
           "# a comment\r\n"                                                       # the key is on line 1: the BOM sits on it
           "ADMIN_TOKEN=\"quoted-fixture-value\"\r\n"                              # gitleaks:allow
           "\r\n"
           "LLM_MODEL = spaced-fixture-value \r\n")                                # gitleaks:allow
EXPECTED = {FIRST_KEY: "first-fixture-value", "ADMIN_TOKEN": "quoted-fixture-value",
            "LLM_MODEL": "spaced-fixture-value"}


def write(tmp_path: Path, *, bom: bool, text: str = CONTENT) -> Path:
    path = tmp_path / ".env.fly"
    path.write_bytes((codecs.BOM_UTF8 if bom else b"") + text.encode("utf-8"))
    return path


@pytest.mark.parametrize("bom", [False, True], ids=["no BOM", "BOM"])
def test_parse_env_reads_the_same_keys_with_or_without_a_bom_and_with_crlf(push, tmp_path, bom):
    assert push.parse_env(write(tmp_path, bom=bom)) == EXPECTED


def test_a_bom_does_not_hide_the_first_key(push, tmp_path):
    values = push.parse_env(write(tmp_path, bom=True))
    assert FIRST_KEY in values and not any(key.startswith("﻿") for key in values)


def test_a_file_that_is_only_a_bom_and_a_comment_has_no_keys(push, tmp_path):
    assert push.parse_env(write(tmp_path, bom=True, text="# nothing yet\r\n")) == {}


def test_the_push_sends_the_first_key_of_a_bom_file_and_prints_only_names(push, tmp_path, monkeypatch, capsys):
    sent = {}

    def fake_run(cmd, input=None, **kw):
        sent.update(cmd=cmd, input=input)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(push.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["push_fly_secrets", "--env", str(write(tmp_path, bom=True)), "--flyctl", "flyctl",
                                      "--only", f"{FIRST_KEY},ADMIN_TOKEN"])
    with pytest.raises(SystemExit) as stop:
        push.main()
    assert stop.value.code == 0
    assert sent["input"] == f"{FIRST_KEY}=first-fixture-value\nADMIN_TOKEN=quoted-fixture-value\n"
    out = capsys.readouterr().out
    assert FIRST_KEY in out and "first-fixture-value" not in out and "quoted-fixture-value" not in out
    assert "skipping" not in out                                       # the first key was found, not reported as absent
