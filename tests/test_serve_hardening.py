"""The API process is non-dumpable on Linux, so a compromised upload parser (same uid) cannot read its secrets through
/proc (second Opus review of M4, security finding S1; docs/v2/M4_PLAN.md section 5)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from semigraph.serve import hardening

SRC = Path(__file__).resolve().parents[1] / "src"


class FakeLibc:
    def __init__(self, set_rc=0, dumpable_after=0):
        self.calls, self.set_rc, self.dumpable_after = [], set_rc, dumpable_after

    def prctl(self, option, *args):
        self.calls.append((option, args))
        return self.set_rc if option == hardening.PR_SET_DUMPABLE else self.dumpable_after


def test_linux_sets_the_process_non_dumpable_and_confirms_it():
    libc = FakeLibc()
    assert hardening.make_process_non_dumpable("linux", libc) is True
    assert libc.calls[0] == (hardening.PR_SET_DUMPABLE, (0, 0, 0, 0))


def test_other_platforms_do_nothing():
    libc = FakeLibc()
    assert hardening.make_process_non_dumpable("win32", libc) is False and libc.calls == []


def test_a_failing_prctl_is_logged_and_never_raises(caplog):
    with caplog.at_level("ERROR", logger="semigraph.serve.hardening"):
        assert hardening.make_process_non_dumpable("linux", FakeLibc(set_rc=-1)) is False
    assert any("PR_SET_DUMPABLE" in r.getMessage() for r in caplog.records)


PROBE = """
import os, subprocess, sys
sys.path.insert(0, {src!r})
if sys.argv[1] == "harden":
    from semigraph.serve.hardening import make_process_non_dumpable
    assert make_process_non_dumpable() is True
child = ("import os, sys\\n"
         "try:\\n"
         "    data = open('/proc/%d/environ' % os.getppid(), 'rb').read()\\n"
         "    print('READ', b'probe-secret-value' in data)\\n"
         "except PermissionError:\\n"
         "    print('DENIED')\\n")
out = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, env={{"PATH": os.environ.get("PATH", "")}})
print(out.stdout.strip())
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc and prctl exist on Linux only")
@pytest.mark.parametrize("mode,expected", [("plain", "READ True"), ("harden", "DENIED")])
def test_a_same_uid_child_can_read_the_parents_secrets_only_when_the_parent_is_dumpable(mode, expected):
    """The control ("plain") proves the exposure is real on this kernel; "harden" proves the fix closes it. The secret
    sits in the parent's EXEC-TIME environment, exactly where a Fly secret lives (a later os.environ change is not in
    /proc/<pid>/environ)."""
    done = subprocess.run([sys.executable, "-c", PROBE.format(src=str(SRC)), mode], capture_output=True, text=True,
                          env={**os.environ, "SEMIGRAPH_PROBE_SECRET": "probe-secret-value"})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == expected
