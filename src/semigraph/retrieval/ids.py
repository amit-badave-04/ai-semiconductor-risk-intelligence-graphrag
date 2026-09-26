"""The citation-id grammar: the ONLY place that defines what counts as a citable id.

Three forms, each resolvable to something a reader can open:

- chunk id    ``<accession>:<section>:<seq>``          a passage of a filing (``0001045810-26-000021:I.1A:0361``)
- XBRL id     ``xbrl:<cik>:<metric>:<period_end>``     a reported financial fact = the ``Metric`` node id with a prefix
- rule id     ``fr:<document_number>``                 a Federal Register rule (``fr:2026-19537``, ``fr:C1-2026-16628``
                                                       for a correction)

A citation is one of these ids alone inside square brackets. Bracketed prose such as ``[Reported Metrics]`` or
``[<accession> lineage data]`` is deliberately NOT a citation: nothing can verify it, and it is exactly the kind
of pseudo-citation the live audit found in answers. Chunk ids keep the pre-M1b grammar byte for byte.
"""

import re

# Patterns (unanchored, for embedding in larger expressions).
CHUNK_ID_PATTERN = r"[0-9\-]+:[IVX]+\.[0-9A-Z]+:[0-9]{4}"
XBRL_ID_PATTERN = r"xbrl:[0-9]+:[a-z][a-z0-9_]*:[0-9]{4}-[0-9]{2}-[0-9]{2}"
FR_ID_PATTERN = r"fr:(?:C[0-9]-)?[0-9]{4}-[0-9]{4,6}"

#: ``findall`` yields the bare id of every well-formed citation in a text.
CITE_RE = re.compile(rf"\[({CHUNK_ID_PATTERN}|{XBRL_ID_PATTERN}|{FR_ID_PATTERN})\]")

# Anchored forms for validating an id that arrives as a route parameter. ``\Z``, never ``$``: ``$`` also matches just
# before a final newline, so an id followed by "%0A" would pass as an id.
CHUNK_ID_RE = re.compile(r"^[0-9\-]{10,30}:[IVX]+\.[0-9A-Z]+:[0-9]{4}\Z")
XBRL_ID_RE = re.compile(rf"^{XBRL_ID_PATTERN}\Z")
FR_ID_RE = re.compile(rf"^{FR_ID_PATTERN}\Z")

_XBRL_PREFIX = "xbrl:"
_FR_PREFIX = "fr:"


def classify_id(value: str) -> str | None:
    """``"chunk"``, ``"xbrl"``, ``"fr"`` or ``None`` for anything that is not a well-formed citation id."""
    if CHUNK_ID_RE.match(value):
        return "chunk"
    if XBRL_ID_RE.match(value):
        return "xbrl"
    if FR_ID_RE.match(value):
        return "fr"
    return None


def xbrl_id(cik: int | str, metric: str, period_end: str) -> str:
    """The citation id of a ``Metric`` node (its ``metric_id`` is ``{cik}:{metric}:{period_end}``)."""
    return f"{_XBRL_PREFIX}{int(cik)}:{metric}:{period_end}"


def metric_id_of(citation: str) -> str:
    """The ``Metric.metric_id`` a XBRL citation id points at."""
    if not XBRL_ID_RE.match(citation):
        raise ValueError(f"not an XBRL citation id: {citation!r}")
    return citation[len(_XBRL_PREFIX):]


def fr_id(document_number: str) -> str:
    """The citation id of a Federal Register rule (its ``ExportControl.rule_id`` is the document number)."""
    return f"{_FR_PREFIX}{document_number}"


def rule_id_of(citation: str) -> str:
    """The ``ExportControl.rule_id`` a Federal Register citation id points at."""
    if not FR_ID_RE.match(citation):
        raise ValueError(f"not a Federal Register citation id: {citation!r}")
    return citation[len(_FR_PREFIX):]
