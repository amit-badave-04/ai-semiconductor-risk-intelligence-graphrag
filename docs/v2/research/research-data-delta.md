## DATA DELTA to 2026-09-25 (live, read-only)

Nothing was written to the repo. Only `SEC_USER_AGENT` was read from `.env`; the header was present and used. Requests ran at about 2 per second. Scratch scripts are in the session scratchpad.

**Universe (VERIFIED, repo).** `src/semigraph/ingestion/edgar.py:35-49` defines 13 SEC filers. The CLI help calls this a "14-company universe"; the 14th is Samsung, which files nothing with the SEC (edgar.py docstring). Selection is every annual filed in 2023 or later (`ANNUAL_SINCE`, `edgar.py:52`) plus `latest(1)` 10-Q. On disk are 59 filings, 56 chunked. The three without chunks are AMD 10-K/A `0000002488-26-000021` and the ASML 20-Fs `0000937966-24-000008` and `-23-000014`.

### Per-company table

Sources: `https://data.sec.gov/submissions/CIK##########.json` (VERIFIED live) and `data/raw/edgar/manifest_universe.json`.

| Ticker | Last on disk | New to ingest (filed, period, accession) |
|---|---|---|
| NVDA | 10-Q 2026-05-20 | 10-Q 2026-08-26, 2026-07-26, 0001045810-26-000075 |
| AMD | 10-Q 2026-05-06 | 10-Q 2026-08-05, 2026-06-27, 0000002488-26-000123 |
| INTC | 10-Q 2026-04-24 | 10-Q 2026-07-24, 2026-06-27, 0000050863-26-000157 |
| AVGO | 10-Q 2026-06-09 | 10-Q 2026-09-10, 2026-08-02, 0001730168-26-000080 |
| QCOM | 10-Q 2026-04-29 | 10-Q 2026-07-29, 2026-06-28, 0000804328-26-000086 |
| MU | 10-Q 2026-06-25 | none (FY ended 2026-09-03) |
| AAPL | 10-Q 2026-05-01 | 10-Q 2026-07-31, 2026-06-27, 0000320193-26-000020 |
| MSFT | 10-Q 2026-04-29 | **10-K FY26** 2026-07-29, 2026-06-30, 0001193125-26-323660 |
| AMZN | 10-Q 2026-04-30 | 10-Q 2026-07-31, 2026-06-30, 0001018724-26-000026 |
| GOOGL | 10-Q 2026-04-30 | 10-Q 2026-07-23, 2026-06-30, 0001652044-26-000071 |
| META | 10-Q 2026-04-30 | 10-Q 2026-07-30, 2026-06-30, 0001628280-26-050705 |
| TSM | 20-F 2026-04-16 | none (annual 20-F only) |
| ASML | 20-F 2026-02-25 | none (annual 20-F only) |

**Total: 10 new filings (9 10-Q, 1 10-K).** There are no new 10-K/A or 20-F filings.

- **No new in-scope filing:** MU's 10-K is due after the cutoff (FY25's was filed 2025-10-03), so about early October is my inference from that pattern (UNVERIFIED). TSM and ASML file annual 20-Fs, and the next ones fall in 2027. Since the last ingest, TSM has filed 25 6-Ks and ASML 4.
- **Not yet due by 2026-09-25 (UNVERIFIED, inferred from prior filing dates):** AAPL and QCOM 10-Ks in late October or early November, AVGO in December.
- **Missing annuals:** none. All annuals since 2023 are on disk.
- **Skipped 10-Qs (by design):** AVGO 2026-03-11, QCOM 2026-02-04, MU 2026-03-19, AAPL 2026-01-30 and MSFT 2026-01-28 are absent only because v1 ingests `latest(1)` 10-Q. They are optional backfill, not gaps.

### Federal Register (VERIFIED, `https://www.federalregister.gov/api/v1/documents.json`)

**The v1 query has a recall bug.** The API treats `semiconductor OR "advanced computing" OR "export controls"` as an AND. The proof:
- The combined query returns 14 documents from 2022 through 2026-09-25.
- The same terms searched singly return 43, 24 and 79 documents. With "Entity List" (99) the union is 142 documents, out of 166 BIS RULEs in total.
- Probes confirm the intersection behaviour: `semiconductor OR "advanced computing"` returned 2 documents, not 3.
- The v1 cache holds 13 documents, ending 2025-09-16.

**Same parameters, 2025-09-17 to 2026-09-25: 1 new rule.**
- 2026-14132 (2026-07-14), "Enhanced Favorable Treatment for the UAE". It moves the UAE from Country Groups D:3/D:4 to A:5, effective 2026-07-10.

**Relevant BIS rules v1 missed in the window (my reading of titles and abstracts):**
- **2025-19001 (2025-09-30):** the 50%-affiliates Entity List rule, effective 2025-09-29.
- **2025-19846 (2025-11-12):** suspends the 50% rule for one year, to 2026-11-09.
- **2025-19508 (2025-10-09):** 29 Entity List additions, of which China 19, Turkey 9 and UAE 1.
- **2025-19858 (2025-11-12):** removes 1 China entity and 6 aliases.
- **2026-00789 (2026-01-15):** H200 and equivalent chips to China and Macau move from presumption of denial to case-by-case review.
- **2026-06851 (2026-04-09):** authorized IC designer deadline extended to 2026-12-31.
- **2026-17230 and 2026-17231 (2026-08-24):** an Entity List removal (Turkey) and removal of two Arrow Electronics HK addresses.
- **2026-19537 (2026-09-24):** polysilicon stockpiling, only marginally relevant.

**Also missed by v1 before the window:**
- 2025-16735 (2025-09-02): revocation of China VEU authorizations.
- 2025-05426 and 2025-05427 (2025-03-28): Entity List additions.
- 2025-02655 (2025-02-14): advanced-computing due-diligence amendment.

**Non-RULE items (VERIFIED):**
- 2025-06591 (2025-04-16): BIS Notice requesting comments on the Section 232 semiconductor investigation.
- 2026-01052 (2026-01-20): Presidential Document, "Adjusting Imports of Semiconductors, Semiconductor Manufacturing Equipment, and Their Derivative Products".

No Federal Register document rescinding the AI Diffusion rule appeared in the searches. A search for "Artificial Intelligence Diffusion" since April 2025 returned only the UAE rule and the Unified Agenda. Nothing from the searches supports an H20 rule either (UNVERIFIED beyond that).

### XBRL Company Facts (VERIFIED, `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`)

| Ticker | On disk, max period end | Live, max period end | Note |
|---|---|---|---|
| NVDA | 2026-04-26 | 2026-07-26 (filed 2026-08-26) | Curated annual metrics unchanged: last annual period 2026-01-25 |
| MSFT | annual 2025-06-30 | annual 2026-06-30 | The only new annual metric-period (4 curated rows) |
| MU, ASML | unchanged | unchanged | |
| TSM | ifrs-full ends 2024-12-31 | ifrs-full ends 2024-12-31 | See below |

**TSM gap.** The FY2025 20-F (0001628280-26-025362) is on EDGAR with inline XBRL. Live Company Facts carries its `dei` block but no `ifrs-full` facts after FY2024. The cause is UNVERIFIED. So v1's `TSM_key_metrics.parquet` and the live API both lack FY2025. Recovering it means parsing the filing's own inline XBRL.

### Volume and cost estimate (VERIFIED from `data/processed/chunks/*`)

Old corpus: 4,933 chunks and 1,716,535 tokens across 56 chunked filings. Per filing:
- 10-Q: 62.6 chunks and 21.0k tokens.
- 10-K: 99.3 chunks and 34.1k tokens.
- 20-F: 61.7 chunks and 25.9k tokens.

I proxied each new filing by that company's latest on-disk filing. The 9 new 10-Qs come to about 564 chunks and 180k tokens. MSFT FY26 10-K adds about 62 chunks and 30k tokens, using FY25 as proxy. **Total is about 630 to 660 chunks and about 210 to 225k tokens, roughly a 13% increase.**

- **Extraction cost.** Using the code's own formula (`extractor.py:115-140`: 800-token overhead, 300 output tokens per chunk, critic on 25% of chunks) at Sonnet 5 $2/$10 (taken from the task text, not re-verified), the figure is about $3.5, with a worst case of about $5.2. `estimate_extraction_cost` hard-codes $3/$15 instead (stale). At those constants it would print about $5.1, or about $7.7 worst case.
- **Scope side effect.** Under `extraction_scope`, the new 10-Qs replace the old "latest 10-Q" chunks. MSFT FY26 becomes the full-extraction annual, and FY25 drops to risk-only. Embedding the new chunks locally is negligible.

### Pipeline traps for v2 (VERIFIED in code)

- `download_filings` skips any ticker already in the manifest (`edgar.py:191-197`), so a plain re-run fetches nothing new. It needs per-accession incremental logic.
- Three other steps skip when their cache exists: `download_bis_rules` (`federal_register.py:73`), `download_companyfacts` (`xbrl.py:81`) and `extract_metrics` (`xbrl.py:169`).
- `extraction_scope` picks the latest 10-Q by `accession_no.max()` (`extractor.py:88`), a string sort, not by `filing_date`. It fails if a filer changes filing-agent prefix.

### Other valid sources v1 did not use (one line each)

- SEC 8-K, mostly Items 1.01, 2.02 and 7.01/8.01: there are about 47 since the last ingest across the filers, plus 25 TSM 6-Ks and 4 ASML 6-Ks (VERIFIED counts).
- Section 232 and Section 301 Federal Register notices and Presidential Documents (VERIFIED to exist).
- The BIS website press releases and the Entity List page (UNVERIFIED, not fetched).
- Samsung and SK Hynix filings on Korea's DART system (UNVERIFIED, recalled).
