// GENERATED FILE - DO NOT EDIT.
// Written by scripts/export_ids.py from SPA_PAGES in src/semigraph/serve/ui_v2.py and FILERS in src/semigraph/universe.py.
// Regenerate with: uv run python -m scripts.export_ids
// tests/test_export_ids.py fails while this file is stale.

// The paths of the app's pages; {ticker} is the one path parameter.
export const SPA_PAGES = [
  "/",
  "/company/{ticker}/",
  "/workspace/",
  "/freshness/",
  "/method/",
] as const;

// The 13 SEC filers (a company page exists for each). Samsung has no SEC filings and is not here.
export const FILERS = [
  { ticker: "NVDA", name: "Nvidia" },
  { ticker: "AMD", name: "AMD" },
  { ticker: "INTC", name: "Intel" },
  { ticker: "AVGO", name: "Broadcom" },
  { ticker: "QCOM", name: "Qualcomm" },
  { ticker: "MU", name: "Micron" },
  { ticker: "AAPL", name: "Apple" },
  { ticker: "MSFT", name: "Microsoft" },
  { ticker: "AMZN", name: "Amazon" },
  { ticker: "GOOGL", name: "Alphabet" },
  { ticker: "META", name: "Meta" },
  { ticker: "TSM", name: "TSMC" },
  { ticker: "ASML", name: "ASML" },
] as const;

export type Ticker = (typeof FILERS)[number]["ticker"];
