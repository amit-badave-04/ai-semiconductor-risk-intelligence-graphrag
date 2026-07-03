"""Extraction output schemas — the contract the LLM must satisfy.

Ported from notebook 07 (PoC form, with the RICH Field descriptions: they are
injected into the extractor prompt via ``model_json_schema()`` and are part of
the prompt contract — do not slim them down).

Also home of :func:`normalize_category`, the SDK-side fix for the free-text
risk-category backlog: the extractor produced ~2,000 distinct free-text
categories at universe scale; rather than tightening the LLM schema (which
would burn correction turns), categories are post-mapped onto
``RISK_CATEGORIES`` at persist time.
"""

from pydantic import BaseModel, Field

RELATION_TYPES = ["SUPPLIES_TO", "DEPENDS_ON", "CUSTOMER_OF", "COMPETES_WITH"]
RISK_CATEGORIES = ["Supply Chain", "Geopolitical", "Export Controls", "Demand", "Competition",
                   "Technology", "Legal/Regulatory", "Financial", "Other"]


class Relation(BaseModel):
    source_entity: str = Field(description="Organization/product doing the acting, exactly as named in the text")
    relation: str = Field(description=f"One of {RELATION_TYPES}")
    target_entity: str = Field(description="The other organization/product, exactly as named in the text")
    evidence_quote: str = Field(description="VERBATIM quote (<=40 words) from the chunk that states this relationship")


class RiskFactor(BaseModel):
    summary: str = Field(description="One-sentence summary of the specific risk")
    category: str = Field(description=f"One of {RISK_CATEGORIES}")
    evidence_quote: str = Field(description="VERBATIM quote (<=40 words) from the chunk")


class Product(BaseModel):
    name: str = Field(description="Product/technology name as written, e.g. 'Blackwell', 'HBM3e', 'CoWoS'")
    type: str = Field(description="GPU | CPU | ASIC | Memory | Packaging | Equipment | Platform | Other")


class ChunkExtraction(BaseModel):
    relations: list[Relation] = []
    risk_factors: list[RiskFactor] = []
    products: list[Product] = []


class CriticVerdict(BaseModel):
    verdicts: list[bool] = Field(description="For each numbered claim, true only if the chunk explicitly supports it")


# --- free-text category -> canonical RISK_CATEGORIES mapping -------------------
# Keyword rules are checked IN ORDER; the first category with a matching keyword
# wins. Export Controls precedes Geopolitical (export-control text usually also
# mentions trade/China) and Supply Chain precedes Geopolitical for the same
# reason. Keywords are substring matches against the casefolded input.
_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("Export Controls", ("export control", "export restriction", "export license",
                         "entity list", "sanction", "trade restriction")),
    ("Supply Chain", ("supply", "supplier", "manufactur", "foundry", "fabrication",
                      "capacity constraint", "shortage", "logistics", "sourcing",
                      "raw material", "component", "third-party dependence",
                      "concentration of production")),
    ("Geopolitical", ("geopolit", "politic", "taiwan", "china", "tariff", "trade polic",
                      "trade tension", "trade barrier", "trade war", "macroeconomic",
                      "war", "conflict", "international relations", "cross-strait")),
    ("Competition", ("compet",)),
    ("Demand", ("demand", "customer concentration", "sales fluctuation", "seasonal",
                "market condition", "revenue concentration", "order cancellation")),
    ("Technology", ("technolog", "innovation", "product development", "obsolescence",
                    "cyber", "security breach", "information security",
                    "artificial intelligence", "infrastructure", "system failure")),
    ("Legal/Regulatory", ("legal", "regulat", "litigation", "lawsuit", "compliance",
                          "intellectual property", "patent", "antitrust", "privacy",
                          "data protection", "governance")),
    ("Financial", ("financ", "currency", "exchange rate", "interest rate", "liquidity",
                   "credit", "tax", "capital", "investment", "accounting", "impairment",
                   "indebtedness", "stock price", "dividend")),
]

_EXACT = {c.lower(): c for c in RISK_CATEGORIES}


def normalize_category(text: str) -> str:
    """Map a free-text risk category onto the ``RISK_CATEGORIES`` enum.

    Pure function: exact (case-insensitive) enum match first, then ordered
    keyword mapping, falling back to ``"Other"``. Applied at persist time
    (extraction and resolution) — never sent to the LLM.
    """
    if not text or not text.strip():
        return "Other"
    t = text.strip().lower()
    if t in _EXACT:
        return _EXACT[t]
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(k in t for k in keywords):
            return category
    return "Other"
