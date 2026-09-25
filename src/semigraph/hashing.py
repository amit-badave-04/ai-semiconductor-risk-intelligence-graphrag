"""Content fingerprints for evidence text.

``content_hash`` is stamped on every EvidenceSpan so a span can be recognised
across rebuilds, chunk-id schemes and snapshots.
"""

import hashlib
import unicodedata


def content_hash(text: str) -> str:
    """SHA-256 hex digest of the canonical form of ``text``.

    FROZEN SPEC — changing any step invalidates every hash already stored in
    a graph, cache or citation, so do not touch it; add a new function with a
    new name instead. The canonical form is, in this order:

    1. Unicode NFKC normalisation (composed/decomposed forms and compatibility
       characters such as ligatures, fullwidth letters and NBSP fold together).
    2. Every run of whitespace (``str.split()`` semantics: spaces, tabs,
       newlines, and any other Unicode whitespace) collapsed to one U+0020,
       and leading/trailing whitespace removed.
    3. Case is PRESERVED (``Entity List`` and ``entity list`` differ).
    4. UTF-8 encode, SHA-256, lowercase hex.
    """
    canonical = " ".join(unicodedata.normalize("NFKC", text).split())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
