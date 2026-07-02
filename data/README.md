# Data lake (git-ignored, reproducible from public APIs)

- `raw/` — immutable downloads: EDGAR filing HTML, XBRL companyfacts JSON, Federal Register docs
- `interim/` — parsed semantic trees and section extracts (sec-parser output)
- `processed/` — chunk stores (parquet), LLM extraction outputs (jsonl)

Rebuild everything by running notebooks 01-04 (Nvidia) or 12 (full universe).
