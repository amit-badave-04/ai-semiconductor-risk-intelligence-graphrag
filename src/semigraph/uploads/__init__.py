"""Private upload workspaces (M4, docs/v2/M4_PLAN.md 4.2 and 4.4).

This package init is import-light on purpose (stdlib only): the route layer imports the workspace-id grammar from here. The
parsers (pypdfium2, pdfplumber, python-docx) are imported only inside the parse worker subprocess, Neo4j access lives in
``repo``, and nothing here is imported by the public SEC path.

Identifiers: a workspace id is 32 lower-case hex characters (not a secret: the 128-bit token is, and only its sha256 is stored);
a document id is 12 lower-case hex characters (it appears in ``doc:`` citations, :mod:`semigraph.retrieval.ids`).
"""

import re
import secrets

WORKSPACE_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
WORKSPACE_TOKEN_MAX_CHARS = 64          # a token is 22 url-safe characters; anything longer is refused before any lookup


def new_workspace_id() -> str:
    return secrets.token_hex(16)


def new_document_id() -> str:
    return secrets.token_hex(6)


def new_workspace_token() -> str:
    """128 bits, url-safe (22 characters). Shown to the client once; only its sha256 is stored."""
    return secrets.token_urlsafe(16)
