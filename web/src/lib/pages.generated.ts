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

// The 13 SEC filers (a company page exists for each). Samsung has no SEC filings and is not here. annualForm is the
// annual report's form; quarterly is false for a filer with no quarterly 10-Q (TSMC and ASML file a 20-F only).
export const FILERS = [
  { ticker: "NVDA", name: "Nvidia", annualForm: "10-K", quarterly: true },
  { ticker: "AMD", name: "AMD", annualForm: "10-K", quarterly: true },
  { ticker: "INTC", name: "Intel", annualForm: "10-K", quarterly: true },
  { ticker: "AVGO", name: "Broadcom", annualForm: "10-K", quarterly: true },
  { ticker: "QCOM", name: "Qualcomm", annualForm: "10-K", quarterly: true },
  { ticker: "MU", name: "Micron", annualForm: "10-K", quarterly: true },
  { ticker: "AAPL", name: "Apple", annualForm: "10-K", quarterly: true },
  { ticker: "MSFT", name: "Microsoft", annualForm: "10-K", quarterly: true },
  { ticker: "AMZN", name: "Amazon", annualForm: "10-K", quarterly: true },
  { ticker: "GOOGL", name: "Alphabet", annualForm: "10-K", quarterly: true },
  { ticker: "META", name: "Meta", annualForm: "10-K", quarterly: true },
  { ticker: "TSM", name: "TSMC", annualForm: "20-F", quarterly: false },
  { ticker: "ASML", name: "ASML", annualForm: "20-F", quarterly: false },
] as const;

export type Ticker = (typeof FILERS)[number]["ticker"];
