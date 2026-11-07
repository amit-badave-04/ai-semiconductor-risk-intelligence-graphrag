"""Process hardening for the API (M4, docs/v2/M4_PLAN.md section 5; second Opus review, security finding S1).

The API marks itself non-dumpable (``prctl(PR_SET_DUMPABLE, 0)``): its ``/proc/<pid>`` entries (the environment block with the
provider keys, ``ADMIN_TOKEN`` and the Neo4j password, and the memory file) become root-owned, so the secrets stay private to the
API process and its own code, on the principle of least privilege. The upload workers are marked the same way
(``uploads.sandbox.apply_sandbox_limits``). Root (``flyctl ssh console``) can still read them; core dumps are disabled.

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
    exposure to guard). A failure is logged at ERROR (the hardening is then missing) and never raises: the service
    must still start."""
    if not platform.startswith("linux"):
        return False
    try:
        libc = libc if libc is not None else ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            logger.error("prctl(PR_SET_DUMPABLE, 0) failed (errno %d): the process is not marked non-dumpable",
                         ctypes.get_errno())
            return False
        return libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        logger.exception("could not mark the process non-dumpable")
        return False
