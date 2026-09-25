"""BIS / Federal Register export-control rule ingestion
(ported from notebook 12 stage 7 — the ingestion half only; loading
``ExportControl`` nodes and ``AFFECTED_BY`` edges into Neo4j belongs to
the graph layer).

**Recall fix (M1).** v1 sent the term query ``semiconductor OR "advanced
computing" OR "export controls"``; the Federal Register API evaluates it as
AND (14 hits, while the terms alone return 43/24/79 and 166 BIS RULEs exist
since 2022-01-01), so v1 stored 13 rules and missed the 50%-affiliates rule,
the H200 case-by-case policy, the UAE country-group change and more. There is
no term query any more: every Bureau of Industry and Security document of type
RULE published since 2022-01-01 is fetched (paginated), stored raw and dated,
and triaged locally by the pure, deterministic ``classify_rule``.

The cache ``data/raw/federal_register_bis_rules.json`` keeps its top-level
``results`` list (the graph loader reads it) and gains ``count``,
``retrieved_at`` (ISO, UTC), ``query`` (what was fetched) and, per result,
``kind`` / ``topics`` / ``relevant``.

The API is free but DNS/network can blip, so requests retry with backoff
(5 s / 15 s / 45 s) before giving up.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from ..config import Settings, get_settings
from .edgar import atomic_write_text, parse_as_of

logger = logging.getLogger("semigraph.ingestion.federal_register")

FR_BASE_URL = "https://www.federalregister.gov/api/v1/documents.json"
AGENCY_SLUG = "industry-and-security-bureau"
RULE_TYPE = "RULE"
SINCE = "2022-01-01"
PAGE_SIZE = 1000            # the API maximum; the full BIS RULE set is ~170 documents
MAX_PAGES = 50              # runaway-pagination guard (50 x 1000 >> any real result set)
FR_FIELDS = [
    "document_number", "title", "publication_date", "html_url", "pdf_url",
    "abstract", "effective_on", "citation", "type",
]

# NOTE: deliberately no ``conditions[term]`` — see the module docstring.
FR_PARAMS = {
    "conditions[agencies][]": AGENCY_SLUG,
    "conditions[type][]": RULE_TYPE,
    "conditions[publication_date][gte]": SINCE,
    "per_page": str(PAGE_SIZE),
    "order": "newest",
    "fields[]": FR_FIELDS,
}

# LEGACY. Linking heuristic (documented in notebook 12, refined in M6): a
# company is AFFECTED_BY a rule when it discloses an Export Controls-category
# risk whose evidence text matches the rule-title topic's keywords. Kept here
# because the graph loader and tests import it; the per-rule ``relevant`` flag
# and ``topics`` written by ``classify_rule`` supersede it for triage.
TOPIC_KEYWORDS = {  # rule-title keyword -> evidence-text keywords
    "entity list": ["entity list"],
    "advanced computing": ["advanced computing", "ai chip", "accelerator"],
    "semiconductor manufacturing": [
        "manufacturing equipment", "semiconductor manufacturing",
    ],
    "artificial intelligence": ["artificial intelligence", "ai diffusion"],
}

RETRY_WAITS_S = [5, 15, 45]
HTTP_TIMEOUT_S = 30

Fetch = Callable[[str], dict]


# ------------------------------------------------------- classification

RULE_KINDS = (
    "advanced_computing",
    "semiconductor_equipment",
    "affiliates_rule",
    "licensing_policy",
    "ai_model_controls",
    "entity_list_additions",
    "other",
)
# Kinds whose rules can plausibly be tied to a company's disclosed export-control
# risk. Entity List actions name specific entities; they are kept as
# ExportControl nodes but never linked to companies.
RELEVANT_KINDS = frozenset(
    {"advanced_computing", "semiconductor_equipment", "affiliates_rule",
     "licensing_policy", "ai_model_controls"}
)

_FLAGS = re.IGNORECASE
_AFFILIATES_TITLE = re.compile(r"affiliates of certain listed entities", _FLAGS)
_AFFILIATES_TEXT = re.compile(r"at least 50 percent owned|50 percent[- ]owned", _FLAGS)
_LICENSING_TITLE = re.compile(
    r"license review polic|license exception|country groups?\b|favorable treatment"
    r"|validated end[- ]?user|\bveu\b|\bic\)? designer",
    _FLAGS,
)
# Licensing language also appears in rules on Russia, firearms, Syria, drones
# and Cambodia; it only counts as chip policy with chip / compute / AI context.
_CHIP_CONTEXT = re.compile(
    r"semiconductor|advanced computing|artificial intelligence|\bchips?\b|data center"
    r"|integrated circuit \(ic\)|\bic\)? designer|3a090|4a090",
    _FLAGS,
)
_AI_TITLE = re.compile(r"artificial intelligence|\bai\b|model weights", _FLAGS)
_ADVANCED_TITLE = re.compile(r"advanced computing|supercomputer", _FLAGS)
_SEMI_TITLE = re.compile(r"semiconductor manufacturing", _FLAGS)
_ENTITY_LIST_NAME = re.compile(r"entity list|unverified list|military end[- ]user list", _FLAGS)
_ENTITY_LIST_ACTION = re.compile(r"addition|removal|revision|modif|correction", _FLAGS)
_ADVANCED_ABSTRACT = re.compile(r"advanced computing|3a090|4a090|supercomputer", _FLAGS)
_AI_ABSTRACT = re.compile(r"artificial intelligence|model weights", _FLAGS)
_SEMI_ABSTRACT = re.compile(r"semiconductor", _FLAGS)

_TOPIC_PATTERNS = {
    "entity list": re.compile(r"entity list", _FLAGS),
    "affiliates rule": re.compile(
        _AFFILIATES_TITLE.pattern + "|" + _AFFILIATES_TEXT.pattern, _FLAGS
    ),
    "advanced computing": _ADVANCED_ABSTRACT,
    "semiconductor manufacturing": re.compile(r"semiconductor manufacturing", _FLAGS),
    "artificial intelligence": _AI_ABSTRACT,
}
# Licensing topics only count with chip context, exactly like the kind.
_LICENSING_TOPIC_PATTERNS = {
    "license review": re.compile(r"license review polic", _FLAGS),
    "country group": re.compile(r"country groups?\b", _FLAGS),
    "validated end-user": re.compile(r"validated end[- ]?user|\bveu\b", _FLAGS),
    "ic designer": re.compile(r"\bic\)? designer", _FLAGS),
}


def _is_entity_list_title(title: str) -> bool:
    return bool(_ENTITY_LIST_NAME.search(title) and _ENTITY_LIST_ACTION.search(title))


def _kind_from_title(title: str, text: str) -> str | None:
    """The kind a title alone decides, in precedence order, or None."""
    if _AFFILIATES_TITLE.search(title) or _AFFILIATES_TEXT.search(text):
        return "affiliates_rule"
    # licensing before advanced computing: "License Review Policy for Advanced
    # Computing Commodities" changes policy, not classification. It also beats
    # entity_list_additions so a mixed Entity List + VEU rule stays relevant.
    if _LICENSING_TITLE.search(title) and _CHIP_CONTEXT.search(text):
        return "licensing_policy"
    if _AI_TITLE.search(title):
        return "ai_model_controls"
    if _ADVANCED_TITLE.search(title):
        return "advanced_computing"
    if _SEMI_TITLE.search(title):
        return "semiconductor_equipment"
    if _is_entity_list_title(title):
        return "entity_list_additions"
    return None


def _kind_from_abstract(abstract: str) -> str | None:
    """Fallback for titles with no signal (e.g. "Commerce Control List Additions
    and Revisions; ..."): the abstract names the regime."""
    if _ADVANCED_ABSTRACT.search(abstract):
        return "advanced_computing"
    if _AI_ABSTRACT.search(abstract):
        return "ai_model_controls"
    if _SEMI_ABSTRACT.search(abstract):
        return "semiconductor_equipment"
    return None


def _topics(text: str) -> list[str]:
    found = {name for name, pat in _TOPIC_PATTERNS.items() if pat.search(text)}
    if _CHIP_CONTEXT.search(text):
        found |= {name for name, pat in _LICENSING_TOPIC_PATTERNS.items() if pat.search(text)}
    return sorted(found)


def classify_rule(title: str, abstract: str | None) -> dict:
    """Deterministically triage a BIS rule from its title and abstract.

    Returns ``{"kind": one of RULE_KINDS, "topics": sorted list[str],
    "relevant": bool}``. Pure and case-insensitive; a missing abstract is fine
    (7 of the 166 stored rules have none). Precedence is title-first: an Entity
    List addition whose abstract cites advanced computing stays an
    ``entity_list_additions`` rule; a mixed Entity-List + VEU-removal rule is
    ``licensing_policy`` (relevant wins, for recall when linking companies).
    """
    title = (title or "").strip()
    abstract = (abstract or "").strip()
    text = f"{title} {abstract}"
    kind = _kind_from_title(title, text) or _kind_from_abstract(abstract) or "other"
    return {"kind": kind, "topics": _topics(text), "relevant": kind in RELEVANT_KINDS}


# ----------------------------------------------------------- HTTP / fetch

def fr_query_url() -> str:
    """First-page URL of the full-BIS-RULE query (no term condition)."""
    return FR_BASE_URL + "?" + urllib.parse.urlencode(FR_PARAMS, doseq=True)


def count_query_url(as_of: date | str | None = None) -> str:
    """Count-only query: same conditions (+ ``publication_date <= as_of``), one
    cheap field. The API ignores ``per_page`` below 20, so read ``count``."""
    params = {k: v for k, v in FR_PARAMS.items() if k not in ("fields[]", "per_page", "order")}
    params["fields[]"] = "document_number"
    params["per_page"] = "20"
    bound = parse_as_of(as_of)
    if bound is not None:
        params["conditions[publication_date][lte]"] = bound.isoformat()
    return FR_BASE_URL + "?" + urllib.parse.urlencode(params, doseq=True)


def _http_get_json(url: str, user_agent: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _is_transient(error: OSError) -> bool:
    """Network blips and 5xx/429 retry; other 4xx are our bug and raise at once."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code >= 500 or error.code == 429
    return True


def with_retries(fetch: Fetch) -> Fetch:
    """Wrap ``fetch`` with the 5 s / 15 s / 45 s retry-and-backoff policy."""
    tries = len(RETRY_WAITS_S) + 1

    def fetch_with_retries(url: str) -> dict:
        for attempt in range(tries):
            try:
                return fetch(url)
            except OSError as e:  # URLError, HTTPError and timeouts are all OSError
                if not _is_transient(e):
                    raise
                if attempt == tries - 1:
                    raise RuntimeError(
                        f"Federal Register API unreachable after {tries} tries — "
                        f"check internet/DNS: {e}"
                    ) from e
                wait = RETRY_WAITS_S[attempt]
                logger.warning("network error (%s) — retrying in %ss", e, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    return fetch_with_retries


def make_fetcher(settings: Settings) -> Fetch:
    """The real, retrying fetcher (no I/O until called)."""
    user_agent = settings.sec_user_agent.strip() or "semigraph"
    return with_retries(lambda url: _http_get_json(url, user_agent))


def fetch_all_bis_rules(fetch: Fetch, *, url: str | None = None) -> tuple[list[dict], int]:
    """Fetch every BIS RULE since 2022-01-01, following ``next_page_url``.

    Returns ``(rules unioned by document_number in API order, pages fetched)``.
    A count that disagrees with what was received is logged (not fatal).
    """
    next_url: str | None = url or fr_query_url()
    rules: dict[str, dict] = {}
    expected: int | None = None
    pages = 0
    while next_url:
        if pages >= MAX_PAGES:
            raise RuntimeError(
                f"Federal Register pagination exceeded {MAX_PAGES} pages — aborting"
            )
        page = fetch(next_url)
        pages += 1
        expected = page.get("count", expected) if expected is None else expected
        for rule in page.get("results", []):
            rules.setdefault(rule["document_number"], rule)
        next_url = page.get("next_page_url")
    if expected is not None and expected != len(rules):
        logger.warning(
            "Federal Register reported %s documents but %d were received", expected, len(rules)
        )
    return list(rules.values()), pages


# ----------------------------------------------------------------- cache

def rules_cache_path(settings: Settings) -> Path:
    return settings.raw_dir / "federal_register_bis_rules.json"


def _classified(rule: dict) -> dict:
    """A copy of ``rule`` carrying kind/topics/relevant (recomputed if absent)."""
    if {"kind", "topics", "relevant"} <= set(rule):
        return dict(rule)
    return {**rule, **classify_rule(rule.get("title", ""), rule.get("abstract"))}


def load_cached_rules(settings: Settings) -> dict | None:
    """The v2 cache payload, or None when absent, unreadable or a legacy v1
    file (no ``retrieved_at``)."""
    cache = rules_cache_path(settings)
    if not cache.exists():
        return None
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("%s unreadable (%s) — refetching", cache, e)
        return None
    if "retrieved_at" not in payload:
        logger.warning("%s is a legacy v1 cache (no retrieved_at) — refetching", cache)
        return None
    return payload


def read_stored_rules(settings: Settings) -> list[dict]:
    """Whatever rules the cache file holds (any version, legacy v1 included);
    empty when absent or unreadable. Read-only, no reclassification."""
    cache = rules_cache_path(settings)
    if not cache.exists():
        return []
    try:
        return list(json.loads(cache.read_text(encoding="utf-8")).get("results", []))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("%s unreadable (%s) — treating as empty", cache, e)
        return []


def _query_description(first_url: str, pages: int) -> dict:
    return {
        "agency": AGENCY_SLUG,
        "type": RULE_TYPE,
        "publication_date_gte": SINCE,
        "per_page": PAGE_SIZE,
        "order": FR_PARAMS["order"],
        "fields": FR_FIELDS,
        "url": first_url,
        "pages": pages,
    }


def _refresh_cache(settings: Settings, fetch: Fetch | None) -> dict:
    fetch = with_retries(fetch) if fetch is not None else make_fetcher(settings)
    first_url = fr_query_url()
    rules, pages = fetch_all_bis_rules(fetch, url=first_url)
    payload = {
        "description": "All BIS RULE documents since 2022-01-01, locally classified (semigraph M1)",
        "count": len(rules),
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "query": _query_description(first_url, pages),
        "results": [_classified(r) for r in rules],
    }
    cache = rules_cache_path(settings)
    cache.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cache, json.dumps(payload, indent=2, ensure_ascii=False))
    logger.info("downloaded %d BIS rules (%d pages) -> %s", len(rules), pages, cache)
    return payload


def rules_published_by(rules: Sequence[dict], as_of: date | None) -> list[dict]:
    """Rules with ``publication_date <= as_of`` (all of them when as_of is None)."""
    if as_of is None:
        return list(rules)
    cutoff = as_of.isoformat()
    return [r for r in rules if r["publication_date"] <= cutoff]


def download_bis_rules(
    settings: Settings | None = None,
    *,
    refresh: bool = False,
    as_of: date | str | None = None,
    fetch: Fetch | None = None,
) -> list[dict]:
    """Fetch (or reuse) every BIS export-control RULE since 2022; return them.

    Each rule carries ``document_number`` (the graph's ``rule_id``), ``title``,
    ``publication_date``, ``html_url``, ``pdf_url``, ``abstract``,
    ``effective_on``, ``citation``, ``type`` and the local triage fields
    ``kind`` / ``topics`` / ``relevant``.

    The cache is reused unless ``refresh=True`` or it is a legacy v1 file (no
    ``retrieved_at``), which is refetched. ``as_of`` filters the *returned*
    list to ``publication_date <= as_of``; the cache always holds everything.
    Classification is written at fetch time (a rule missing it is classified
    on read); ``refresh=True`` re-derives it after ``classify_rule`` changes.
    ``fetch(url) -> dict`` is the network seam.
    """
    settings = settings or get_settings()
    payload = None if refresh else load_cached_rules(settings)
    if payload is None:
        payload = _refresh_cache(settings, fetch)
    rules = [_classified(r) for r in payload["results"]]
    visible = rules_published_by(rules, parse_as_of(as_of))
    logger.info(
        "%d BIS export-control rules loaded (%d relevant)",
        len(visible), sum(1 for r in visible if r["relevant"]),
    )
    return visible
