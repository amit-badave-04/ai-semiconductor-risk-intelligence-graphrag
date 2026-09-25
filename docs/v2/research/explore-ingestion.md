**Facts only. Paths are relative to the repo root, `C:\Users\amit1\OneDrive\Documents\Projects\ai-semiconductor-risk-intelligence-graphrag`.**

I had no Bash or parquet access. Filing dates come from filenames on disk. Per-filing chunk and token counts are not available; only the universe totals from notebook 12 output are.

## (1) Universe and selection rules
- The universe is `FILERS` in `src/semigraph/ingestion/edgar.py:35-49`. It is duplicated in `src/semigraph/extraction/extractor.py:34-42`. Entries are ticker to (name, annual form, quarterly form).
  - 10-K/10-Q filers: NVDA, AMD, INTC, AVGO, QCOM, MU, AAPL, MSFT, AMZN, GOOGL, META.
  - 20-F only (no quarterly): TSM, ASML.
  - That is 13 filers. The CLI help text says "14-company universe" (`cli.py:36`).
  - Samsung is not in `FILERS`. It is an entity-only node.
- CIKs come from `data/raw/edgar/company_tickers.json` (`edgar.py:103-116`) or from the manifest.
- Selection (`edgar.py:130-136`):
  - Annuals: `company.get_filings(form=annual_form)` filtered to `filing_date.year >= ANNUAL_SINCE`. `ANNUAL_SINCE = 2023` (`edgar.py:52`), and the `--annual-since` argument defaults to it.
  - Quarterly: `get_filings(form=quarterly_form).latest(1)`, exactly one 10-Q.
  - The `10-K/A` form is not filtered out (see the AMD amendment below).
- The only hard-coded caps are `ANNUAL_SINCE`, the single latest 10-Q, and `HIST_ANNUALS = 1` (`extractor.py:48`).

## (2) On-disk filings under `data/raw/edgar/<TICKER>/`
Latest filing date on disk per company:

| Ticker | Latest annual on disk | Latest 10-Q on disk |
|---|---|---|
| NVDA | 10-K 2026-02-25 | 2026-05-20 |
| AMD | 10-K 2026-02-04 (plus 10-K/A same day) | 2026-05-06 |
| INTC | 10-K 2026-01-23 | 2026-04-24 |
| AVGO | 10-K 2025-12-18 | 2026-06-09 |
| QCOM | 10-K 2025-11-05 | 2026-04-29 |
| MU | 10-K 2025-10-03 | 2026-06-25 |
| AAPL | 10-K 2025-10-31 | 2026-05-01 |
| MSFT | 10-K 2025-07-30 | 2026-04-29 |
| AMZN | 10-K 2026-02-06 | 2026-04-30 |
| GOOGL | 10-K 2026-02-05 | 2026-04-30 |
| META | 10-K 2026-01-29 | 2026-04-30 |
| TSM | 20-F 2026-04-16 | none |
| ASML | 20-F 2026-02-25 | none |

Earlier annuals on disk:
- NVDA, AMD, INTC, AMZN, GOOGL, META: 2023, 2024, 2025 and 2026 dates (six per filer with the 10-Q, five for INTC/AMZN/GOOGL/META).
- AVGO, QCOM, MU, AAPL, MSFT: FY2023, FY2024, FY2025 (three annuals plus the 10-Q).
- TSM, ASML: four 20-Fs each, 2023 to 2026.

Filings likely missing by 2026-09-25 (inferred from fiscal calendars, not verified against EDGAR):
- Newer 10-Qs. Each filer's stored 10-Q is from April to June 2026.
  - The June-quarter 10-Qs from calendar-year filers were due about early August.
  - NVDA's July-quarter 10-Q was due about late August.
  - MU's May-quarter 10-Q is the one on disk (2026-06-25).
- Possibly the newest annuals: MSFT FY26 (about 2026-07-30) and MU FY26 (about early October 2026, probably not yet filed by 2026-09-25).
- Whatever the 10-Q looks like for AVGO after its 2026-06-09 filing (its next 10-Q is due about September).
- NVDA has no 10-K on disk between 2023 and 2026 other than the four listed. Its FY ends late January.

Federal Register:
- `data/raw/federal_register_bis_rules.json` has `count: 13`.
- The latest `publication_date` is **2025-09-16**. The earliest is 2022-10-13.
- The query has `per_page=50` and no pagination, so 13 results is the full set matching the query (`federal_register.py:27-35`).

## (3) What `semigraph ingest` does (`cli.py:33-60`)
The order is EDGAR download, XBRL metrics, Federal Register download, segment, chunk. It makes no LLM calls.

Idempotency and skipping:
- `download_filings` (`edgar.py:160-210`):
  - A ticker already in `manifest_universe.json` is skipped **entirely** (`edgar.py:191-197`).
  - This is per-ticker, not per-filing. A new 10-K or 10-Q for an existing ticker will **not** be fetched.
  - Within `_acquire_company`, files that already exist are not re-downloaded (`edgar.py:141`), but that function is never reached for a manifested ticker.
- `extract_metrics` (`xbrl.py:147-193`): skips a ticker if `<TICKER>_key_metrics.parquet` exists. The companyfacts JSON is cached forever (`xbrl.py:80-90`). Refreshing needs the raw JSON and parquet deleted.
- `download_bis_rules` (`federal_register.py:64-94`): reuses the cache if it exists ("delete the cache file to refresh").
- `segment_filings` (`segmentation.py:195-251`): skips per accession if the interim parquet exists. This one is incremental per filing.
- `chunk_filings` (`chunker.py:218-305`): skips a ticker if its chunk parquet exists and is non-empty (`chunker.py:253-264`). New filings for an existing ticker are therefore ignored unless the parquet is deleted.
  - NVDA's `nvda_chunks.parquet` uses notebook-04 chunk ids (global row index, not `accession:section:seq`), and the code intentionally never rewrites it (`chunker.py:29-33`).

Blockers for pulling data up to 2026-09-25:
- No date window or upper cap exists. Nothing hard-codes an end date.
- To pull new filings you must either remove the ticker from `manifest_universe.json`, or add a new incremental function.
  - Deleting a ticker's manifest entry re-lists all filings via edgartools.
  - Existing HTML is not re-fetched (`edgar.py:141`), and the manifest rows are rebuilt.
- Manifest, key-metrics parquet, chunks parquet and the FR cache all need cache invalidation for refreshes.
- `SEC_USER_AGENT` must be set in `.env` (`edgar.py:66-73`).

## (4) Downstream stages for new filings

| Stage | Incremental? | Notes |
|---|---|---|
| Segment | Yes, per accession (`segmentation.py:226-229`) | Runs automatically inside `ingest`. |
| Chunk | No, per ticker (`chunker.py:253`) | Delete the ticker's chunk parquet to rebuild. Non-NVDA chunk ids are deterministic, so old ids stay stable. Delete `section_texts` too. |
| Extract | Yes, per chunk (`extractor.py:92-112`, checkpoint jsonl append and flush at `:223-224`) | Scope recomputes as the latest annual (all sections) + latest 10-Q + the prior annual (risk section only), so **the scope shifts and only chunks not already done are billed**. Cost estimate and confirmation come first (`cli.py:91-97`). |
| Resolve | No, per ticker (`resolution.py:97-159`) | Rewrites the whole `*_extractions_resolved.jsonl` and the report parquet. It is free, deterministic, and runs after extraction in `build-graph --extract`. |
| Embed | Cached, but any change to a ticker's chunk set re-encodes that whole ticker (`embeddings.py:105-128`) | The cache is keyed by the exact chunk_id list. Embeddings are local and free. |
| Graph load | MERGE-based, idempotent, 100-row UNWIND batches (`loaders.py:4`) | Companies, filings/sections, metrics, evidence spans (only for the extraction scope), knowledge, export controls. |
| Temporal closure | Recomputed globally each run (`temporal.py:173`) | A lineage is closed when the company's latest annual filing lacks it, using the max `filing_date` (`temporal.py:96-113`). It must re-run after any new annual. |

Extraction is the only paid stage. `HIST_ANNUALS = 1` means a newly added annual pushes the previous latest annual into risk-only scope. Its non-risk chunks stop being in scope, but they stay in the jsonl.

## (5) Volume
- Universe totals from notebook 12 output (`notebooks/12_full_universe_ingestion.ipynb` around line 287): **4,933 chunks, 1,716,535 tokens** across every filing chunked, using the KEEP sections below.
  - KEEP sections (`segmentation.py:51-55`): 10-K `I.1`, `I.1A`, `II.7`; 10-Q `I.2`, `II.1A`; 20-F `I.3`, `I.4`, `I.5`.
- One point-in-time snapshot (notebook 12 line ~558): 427 chunks and 191,884 tokens remained to extract.
- `docs/PROJECT_STATUS.md` says about 2,300 chunks extracted and about 2,500 EvidenceSpans in the graph.
- Chunking parameters (`chunker.py:50-51`): `TARGET_TOKENS = 700`, `MAX_TOKENS = 1100`, tiktoken `cl100k_base`.
- Estimated extraction cost formula (`extractor.py:115-140`) uses Sonnet prices of $3/$15 per Mtok. `config.py:45-47` says $2/$10, so the two are inconsistent.
  - Rough average: 1.72M tokens over about 59 filings is about 29k tokens per filing.
  - A 10-K is about 80 to 100 chunks, and a 10-Q is fewer.

## (6) Per-filer gotchas
- **Intel:** its 10-K has no "Item N." headings. `segmentation.py:39-75` uses the page-header fallback (`segment_by_pageheaders`, `PAGEHEAD_MAP`, `PAGEHEAD_RE`), for example "Risk Factors44".
- **ASML:** the 20-F cover has a junk "Item 17 ☐ 18 ☐" heading, so the fallback triggers on "no KEEP sections" rather than "no rows" (`segmentation.py:164-172`).
  - ASML's 2023 and 2024 20-Fs are unmarkable and are warned about and skipped (`segmentation.py:235-239`).
  - Only `risk factors` is mapped for the 20-F fallback (`segmentation.py:74`).
  - Its 2025/2026 filings may work.
- **TSMC/ASML** file 20-F, annual only. Items 3/4/5 are kept.
- **Samsung** does not file with the SEC (`edgar.py:17-18`).
- **sec-parser:** version 0.58 ships only `Edgar10QParser`, so it is used for 10-Ks too. It misses some `TopSectionTitle`s, hence the custom regex segmentation (`segmentation.py:4-9`, `:184`).
- **Fiscal years:**
  - NVDA's fiscal year ends in late January.
  - XBRL curation keeps only periods over 300 days and the first-filed value per `end` date (`xbrl.py:129-138`), and never trusts fiscal-year labels.
  - MSFT's fiscal year ends June, AAPL's September, QCOM's late September, MU's late August/early September, AVGO's late October/early November.
- **AMD:** the on-disk set includes a 10-K/A dated 2026-02-04 (`10-K-A_...`). Extraction keys on `form == annual_form` ("10-K"), so the amendment is excluded from scope. `xbrl.py` `ANNUAL_FORMS` is also `{"10-K", "20-F"}`.
- **MSFT:** the latest 10-Q accession prefix is `0001193125-26-...` (filed via a filing agent). It is a different prefix from the earlier annuals.
- **Neo4j-related risk:** `build-graph` needs a live Neo4j. The graph shape (`Filing`, `FilingSection`, `EvidenceSpan` and so on) is schema-locked to `artifacts/schema.cypher`, and the embedding dimension is 1024 (`config.py:32`).
- **Other:**
  - `data/processed/eval_runs.jsonl` exists.
  - The data lake is git-ignored (`data/README.md`).
  - The stale `docs/PROJECT_STATUS.md` still says "14-company" and "59 filings".

## Key files
- `src/semigraph/ingestion/edgar.py`
- `src/semigraph/ingestion/xbrl.py`
- `src/semigraph/ingestion/federal_register.py`
- `src/semigraph/parsing/segmentation.py`
- `src/semigraph/parsing/chunker.py`
- `src/semigraph/extraction/extractor.py`
- `src/semigraph/extraction/resolution.py`
- `src/semigraph/graph/loaders.py`
- `src/semigraph/graph/temporal.py`
- `src/semigraph/embeddings.py`
- `src/semigraph/cli.py`
- `src/semigraph/config.py`
- `docs/PROJECT_STATUS.md`
- `data/raw/edgar/manifest_universe.json`
- `data/raw/federal_register_bis_rules.json`
