"""ALL Cypher for the ``User*`` labels (M4, docs/v2/M4_PLAN.md 4.2 and 4.4).

STEP 0 STUB: signatures only; Worker B implements. Invariants the implementation must keep (tests/test_serve_upload_repo.py
checks every module-level query string):

- every statement binds ``$ws`` and every ``User*`` node pattern carries ``{workspace_id: $ws}``;
- no relationship ever touches a public label; each private node carries exactly one label;
- tokens: only ``sha256(token)`` is stored; ``authenticate`` compares with ``hmac.compare_digest`` and runs the same path (a
  dummy hash) for an unknown workspace, so an unknown id and a wrong token are indistinguishable;
- vector search uses the filtered ``SEARCH ... WHERE c.workspace_id = $ws ...`` form on ``user_chunk_embedding`` (never an
  unfiltered ``db.index.vector.queryNodes``);
- ``put_version`` writes a version, its units, chunks, embeddings, passages and the currency flip in ONE transaction.
"""

from __future__ import annotations


def create_workspace(driver, ttl_hours: int) -> tuple[str, str, str]:
    """Create a workspace; returns ``(workspace_id, token, expires_at_iso)``. The token is shown once and never stored."""
    raise NotImplementedError("uploads.repo: M4 Worker B")


def authenticate(driver, workspace_id: str, token: str) -> bool:
    """True when ``token`` opens ``workspace_id`` and the workspace has not expired (constant-time, see module docstring)."""
    raise NotImplementedError("uploads.repo: M4 Worker B")
