"""No committed file carries an absolute local path: a Windows user folder names its owner, and the repo is public.

Write a repo-relative path, or a placeholder such as ``<repo>/``, ``<session-scratchpad>/`` or ``%TEMP%\\``. Only tracked text files are read, so a
local output that git ignores never fails this.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()      # its examples below are the forms it looks for
# PENDING the owner's decision: Claude Code's per-user permission file was committed; its paths are live rules (a placeholder would break them),
# so the fix is to untrack it, not to edit it. Drop this entry when it is untracked.
PENDING = {".claude/settings.local.json"}
# a user folder by drive letter (C:\Users\NAME, JSON-escaped too), as Git Bash writes it (/c/Users/NAME), or inside the slug of a Claude
# project folder (C--Users-NAME-...); a placeholder (C:\Users\<you>) is not a leak
LOCAL_PATH = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/<\s\"'`]|/[A-Za-z]/Users/[^/<\s]|\b[A-Za-z]--Users-[^\s<-]", re.IGNORECASE)


def tracked_files() -> list[Path]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout to list the tracked files")
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [ROOT / name for name in listed.decode("utf-8").split("\0") if name and name != SELF and name not in PENDING]


@pytest.mark.parametrize("text", [r"C:\Users\someone\repo", r"c:\\Users\\someone\\repo", "C:/Users/someone/repo", "/c/Users/someone/repo",
                                  r"Temp\claude\C--Users-someone-OneDrive-Documents"])
def test_the_pattern_finds_every_form_a_local_user_folder_takes(text):
    assert LOCAL_PATH.search(text)


@pytest.mark.parametrize("text", [r"C:\Users\<you>\repo", "<repo>/docs/RUNBOOK.md", r"<session-scratchpad>/w1d\boot-1.logs.txt", r"%TEMP%\m4bench",
                                  "USERPROFILE, LOCALAPPDATA", "artifacts\\eval_report.v2d-deployed.json", "/tmp/w1d-burn-8ea78607/samples.jsonl"])
def test_placeholders_and_relative_paths_are_not_flagged(text):
    assert not LOCAL_PATH.search(text)


def test_no_committed_file_holds_an_absolute_local_path():
    files = tracked_files()
    assert files, "git listed no tracked file"
    hits = []
    for path in files:
        if not path.is_file():                                         # deleted in the working tree, not yet committed
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:                                       # binary (.docx, .gz): nothing to read line by line
            continue
        for number, line in enumerate(data.decode("utf-8", errors="ignore").splitlines(), 1):
            if LOCAL_PATH.search(line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    assert not hits, "absolute local paths (use a repo-relative path or a placeholder): " + ", ".join(hits)
