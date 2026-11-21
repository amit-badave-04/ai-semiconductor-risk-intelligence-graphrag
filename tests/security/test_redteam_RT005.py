"""RT-005 (Medium, CWE-359): a personal webmail address committed to the PUBLIC repository.

The repo's own policy (fly.toml and scripts/push_fly_secrets.py) says the SEC User-Agent identity must never be in the
public repo, yet tracked notebooks hard-code a personal webmail address as the SEC_USER_AGENT default.

This test scans every git-TRACKED text file for an address at a personal-webmail domain. Placeholder domains
(example.com / example.org) are not webmail and never match. On failure it lists FILE PATHS ONLY: the matched address
is never put into a local, an assertion operand or the message, so the test output cannot re-publish it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 5 * 1024 * 1024
BINARY_SNIFF_BYTES = 8192

# local-part @ a personal webmail provider . a TLD (e.g. .com, .co.uk, .me)
_PERSONAL_WEBMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@(?:gmail|googlemail|yahoo|outlook|hotmail|icloud|protonmail|proton)\.[A-Za-z]{2,}"
    r"(?:\.[A-Za-z]{2,})?",
    re.IGNORECASE,
)


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8", errors="replace").split("\0") if p]


def _has_personal_webmail(path: Path) -> bool:
    """True when the file is a tracked text file that contains a personal-webmail address (the text never escapes)."""
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        return False
    data = path.read_bytes()
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return False
    return _PERSONAL_WEBMAIL_RE.search(data.decode("utf-8", errors="ignore")) is not None


def _offending_paths() -> list[str]:
    return sorted(rel for rel in _tracked_files() if _has_personal_webmail(REPO_ROOT / rel))


def test_rt005_no_personal_webmail_address_in_any_tracked_file():
    offenders = _offending_paths()
    assert not offenders, (
        f"RT-005: {len(offenders)} tracked file(s) contain a personal webmail address (address redacted): "
        + ", ".join(offenders))
