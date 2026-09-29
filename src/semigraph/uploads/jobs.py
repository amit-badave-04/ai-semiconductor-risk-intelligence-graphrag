"""Upload jobs and the workspace TTL sweeper (M4, docs/v2/M4_PLAN.md 4.2 and 5).

STEP 0 STUB: the lifecycle hooks below are final (wired into ``serve.main.lifespan``); Worker C implements the rest:

- ``UploadJob``: ``received -> validating -> parsing -> chunking -> embedding -> comparing -> indexing -> ready | failed``, on a
  worker thread behind ``app.state.upload_slots`` (one upload at a time on the machine); parsing happens ONLY in the sandboxed
  parse subprocess (``uploads.parse``); embedding uses the shared in-process ONNX session, one chunk at a time, with progress
  events, at nice 10 on Linux, under ``upload_embed_timeout_s``; the version is refused above ``upload_max_pages`` /
  ``upload_max_tokens`` (whole text) or when the workspace would exceed ``upload_max_workspace_tokens``.
- The sweeper deletes expired workspaces every 15 minutes (``repo.sweep_expired``, idempotent).
- This module must stay import-light: no parser import at module level (tests/test_serve_monitor_isolation.py).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("semigraph.uploads.jobs")


def start_if_enabled(app) -> None:
    """Start the TTL sweeper when ``UPLOADS_ENABLED``; ``app.state.upload_sweeper`` is None otherwise."""
    app.state.upload_sweeper = None
    if not app.state.settings.uploads_enabled:
        return
    raise NotImplementedError("upload jobs and sweeper: M4 Worker C (docs/v2/M4_PLAN.md 4.2)")


def stop(app) -> None:
    """Stop the sweeper (bounded); a no-op when none runs. Called before the database driver closes."""
    sweeper = getattr(app.state, "upload_sweeper", None)
    if sweeper is not None:
        sweeper.stop()
