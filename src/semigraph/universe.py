"""The company universe and scope constants — the single source of truth.

These tables were copy-pasted into ingestion, extraction and graph loading
(notebook 12 heritage). They live here now; the old module attributes
(``edgar.FILERS``, ``extractor.FILERS``, ``loaders.FILERS`` ...) are re-exports
so existing imports keep working.
"""

# ticker: (canonical name, annual form, quarterly form or None) — the 13 SEC
# filers. TSMC and ASML are foreign private issuers filing 20-F (annual only).
# The canonical name is what the extractor prompt resolves "we"/"our" to.
FILERS: dict[str, tuple[str, str, str | None]] = {
    "NVDA": ("Nvidia", "10-K", "10-Q"),
    "AMD": ("AMD", "10-K", "10-Q"),
    "INTC": ("Intel", "10-K", "10-Q"),
    "AVGO": ("Broadcom", "10-K", "10-Q"),
    "QCOM": ("Qualcomm", "10-K", "10-Q"),
    "MU": ("Micron", "10-K", "10-Q"),
    "AAPL": ("Apple", "10-K", "10-Q"),
    "MSFT": ("Microsoft", "10-K", "10-Q"),
    "AMZN": ("Amazon", "10-K", "10-Q"),
    "GOOGL": ("Alphabet", "10-K", "10-Q"),
    "META": ("Meta", "10-K", "10-Q"),
    "TSM": ("TSMC", "20-F", None),
    "ASML": ("ASML", "20-F", None),
}

# ticker: (canonical name, tier) — the 14-company universe. Samsung does not
# file with the SEC: it is an entity-only Company node with a synthetic
# negative cik, so it appears here but not in FILERS.
UNIVERSE: dict[str, tuple[str, str]] = {
    "MSFT": ("Microsoft", "Hyperscaler"),
    "AMZN": ("Amazon", "Hyperscaler"),
    "GOOGL": ("Alphabet", "Hyperscaler"),
    "META": ("Meta", "Hyperscaler"),
    "NVDA": ("Nvidia", "Silicon Designer"),
    "AMD": ("AMD", "Silicon Designer"),
    "AVGO": ("Broadcom", "Silicon Designer"),
    "QCOM": ("Qualcomm", "Silicon Designer"),
    "INTC": ("Intel", "IDM"),
    "TSM": ("TSMC", "Manufacturer"),
    "ASML": ("ASML", "Manufacturer"),
    "MU": ("Micron", "Memory"),
    "SSNLF": ("Samsung", "Memory"),
    "AAPL": ("Apple", "Ecosystem Anchor"),
}

# form -> the section id that holds the risk factors (Item 1A / Item 3.D)
RISK_SECTIONS: dict[str, str] = {"10-K": "I.1A", "10-Q": "II.1A", "20-F": "I.3"}

# Annual reports filed in this calendar year or later are in the corpus
# (covers the AI capex supercycle).
ANNUAL_SINCE = 2023

# How many PRIOR annuals contribute risk-only chunks to the extraction scope;
# 1 = latest + one prior = two time points per company for the bitemporal
# lineages. This governs NEW extraction spend only — graph loading takes every
# chunk that already has an extraction record, so history is never lost.
HIST_ANNUALS = 1
