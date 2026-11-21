"""Finding B2 (docs/security/accepted-risks.md, 2026-11-21): the pinned python:3.13-slim base lags Debian's released fixes
(zlib1g CVE-2026-85091 fixed in 1:1.3.dfsg+really1.3.1-1+deb13u1; OpenSSL 3.5.9-1~deb13u1 in the trixie point release),
so the shipped stage applies them at build time. The step runs as root, before anything is installed on top, before the
setuid/setgid strip (a newly upgraded file must lose those bits too) and before the switch to the serving user, and leaves
no apt lists behind.
"""

import re
from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")


def final_stage() -> str:
    """The text of the last build stage (the image that ships)."""
    return DOCKERFILE[[m.start() for m in re.finditer(r"^FROM ", DOCKERFILE, re.M)][-1]:]


def run_steps(stage: str) -> list[str]:
    """Each RUN instruction with its continuation lines joined."""
    joined = re.sub(r"\\\n", " ", stage)
    return [line for line in joined.splitlines() if line.startswith("RUN ")]


def upgrade_step() -> str:
    steps = [s for s in run_steps(final_stage()) if "apt-get" in s and "upgrade" in s]
    assert len(steps) == 1, steps
    return steps[0]


def test_the_shipped_stage_applies_debians_released_fixes():
    step = upgrade_step()
    assert "apt-get update" in step
    assert re.search(r"apt-get -y --no-install-recommends upgrade|apt-get --no-install-recommends -y upgrade", step), step
    assert "rm -rf /var/lib/apt/lists/*" in step


def test_the_upgrade_runs_first_and_before_the_setuid_strip_and_the_user_switch():
    stage = final_stage()
    upgrade_step()                                      # exactly one such step
    at = stage.index("apt-get update")
    first_install = stage.index("uv pip install")
    setuid_strip = stage.index("-perm /6000")
    user_switch = stage.index("\nUSER app")
    assert at < first_install < setuid_strip < user_switch
