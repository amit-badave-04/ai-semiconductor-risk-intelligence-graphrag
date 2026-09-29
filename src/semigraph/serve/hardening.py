"""Process hardening for the API (M4, docs/v2/M4_PLAN.md section 5; second Opus review, security finding S1).

The upload parse sandbox runs as a child process under the SAME user id as the API. Its environment is allowlisted, but
on Linux any process of that uid can read another's ``/proc/<pid>/environ`` (the exec-time environment block: provider
keys, ``ADMIN_TOKEN``, the Neo4j password) and ``/proc/<pid>/mem`` unless the target is NON-DUMPABLE. Marking the API
process non-dumpable (``prctl(PR_SET_DUMPABLE, 0)``) makes its ``/proc`` entries root-owned, so a parser compromised by
a malicious upload cannot read the API's secrets. Root (``flyctl ssh console``) still can; core dumps are disabled.

Stdlib only: imported by ``serve.main`` at boot and by a subprocess test.
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger("semigraph.serve.hardening")

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3


def make_process_non_dumpable(platform: str = sys.platform, libc=None) -> bool:
    """``prctl(PR_SET_DUMPABLE, 0)`` on Linux; True when the process is now non-dumpable. Elsewhere False (no /proc
    exposure to guard). A failure is logged at ERROR (it re-opens the secret exposure) and never raises: the service
    must still start."""
    if not platform.startswith("linux"):
        return False
    try:
        libc = libc if libc is not None else ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            logger.error("prctl(PR_SET_DUMPABLE, 0) failed (errno %d): an upload parser could read this process's "
                         "environment", ctypes.get_errno())
            return False
        return libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        logger.exception("could not mark the process non-dumpable: an upload parser could read its environment")
        return False
