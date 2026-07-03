"""BIS / Federal Register export-control rule ingestion
(ported from notebook 12 stage 7 — the ingestion half only; loading
``ExportControl`` nodes and ``AFFECTED_BY`` edges into Neo4j belongs to
the graph layer).

Queries the free Federal Register API for Bureau of Industry and Security
RULE documents (2022+) matching semiconductor / advanced-computing /
export-controls terms and caches the raw response at the notebook's exact
path: ``data/raw/federal_register_bis_rules.json``.

The API is free but DNS/network can blip, so the download retries with
backoff (5 s / 15 s / 45 s) before giving up.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import Settings, get_settings

logger = logging.getLogger("semigraph.ingestion.federal_register")

FR_PARAMS = {
    "conditions[agencies][]": "industry-and-security-bureau",
    "conditions[type][]": "RULE",
    "conditions[term]": 'semiconductor OR "advanced computing" OR "export controls"',
    "conditions[publication_date][gte]": "2022-01-01",
    "per_page": "50",
    "order": "newest",
    "fields[]": ["document_number", "title", "publication_date", "html_url", "abstract"],
}

# Linking heuristic (documented in notebook 12, refined in M6): a company is
# AFFECTED_BY a rule when it discloses an Export Controls-category risk whose
# evidence text matches the rule-title topic's keywords. Kept here so the
# graph loader and this ingestion module share one definition.
TOPIC_KEYWORDS = {  # rule-title keyword -> evidence-text keywords
    "entity list": ["entity list"],
    "advanced computing": ["advanced computing", "ai chip", "accelerator"],
    "semiconductor manufacturing": [
        "manufacturing equipment", "semiconductor manufacturing",
    ],
    "artificial intelligence": ["artificial intelligence", "ai diffusion"],
}

RETRY_WAITS_S = [5, 15, 45]


def fr_query_url() -> str:
    return (
        "https://www.federalregister.gov/api/v1/documents.json?"
        + urllib.parse.urlencode(FR_PARAMS, doseq=True)
    )


def rules_cache_path(settings: Settings) -> Path:
    return settings.raw_dir / "federal_register_bis_rules.json"


def download_bis_rules(settings: Settings | None = None) -> list[dict]:
    """Fetch (or reuse) BIS export-control rules; return the rule dicts.

    Each rule carries ``document_number`` (the graph's ``rule_id``),
    ``title``, ``publication_date``, ``html_url`` and ``abstract``.
    The raw API response is cached — delete the cache file to refresh.
    """
    settings = settings or get_settings()
    cache = rules_cache_path(settings)
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        url = fr_query_url()
        user_agent = settings.sec_user_agent.strip() or "semigraph"
        for attempt in range(4):  # free API, but DNS/network can blip
            try:
                req = urllib.request.Request(url, headers={"User-Agent": user_agent})
                cache.write_bytes(urllib.request.urlopen(req, timeout=30).read())
                break
            except (urllib.error.URLError, OSError) as e:
                if attempt == 3:
                    raise RuntimeError(
                        "Federal Register API unreachable after 4 tries — "
                        f"check internet/DNS: {e}"
                    ) from e
                wait = RETRY_WAITS_S[attempt]
                logger.warning("network error (%s) — retrying in %ss", e, wait)
                time.sleep(wait)
        logger.info("downloaded BIS rules -> %s", cache)
    rules = json.loads(cache.read_text(encoding="utf-8"))["results"]
    logger.info("%d BIS export-control rules loaded", len(rules))
    return rules
