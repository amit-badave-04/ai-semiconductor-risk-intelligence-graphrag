# Upload pipeline discovery: bring-your-own documents and updated versions

I wrote nothing to the repo and did not touch `.env`. Scratch files went to the scratchpad, and I deleted the 506 MB Hugging Face cache (`C:\hf_tmp`) afterwards. `git status` now shows edits to chunker, embeddings, extractor, config and tests. The tree was clean at session start and I did not make them, so a concurrent agent did. I left them alone. The new append-only chunker fits the versioning design below.

## 0. Leak paths in the current code (blocking)
- **Answer cache.** `store.cache_key(question, strategy)` has no workspace in it, so a private answer would be served to the next public visitor. Include the workspace id and a per-workspace data version in the key, or skip caching for workspace answers.
- **Retrieval sees any node.** `hybrid_retrieve` walks the four relation types and `AFFECTED_BY` from real `Company` nodes, and `evidence_embedding` and `risk_embedding` have no scope filter. Uploads must never create edges between canonical companies or `DISCLOSES_RISK` from them. That also protects `apply_closure`, `fetch_annual_risks` and `link_affected_by`.
- **Citation grammar.** `CITE_RE` and `CHUNK_ID_RE` only match SEC-shaped ids. Upload citations would count as hallucinated and `/api/evidence/{id}` would return 400. Use a distinct id such as `U:{ws}:{doc}:v{n}:{seq}` and a token-scoped evidence route.
- **Rate limiter.** `guard.RateLimiter` is process-local by design ("ONE machine"). It breaks if the API scales out for 1,000 users, so move it to Valkey.

## 1. Parsing stack (PyPI live; timings measured on this Windows 11, Python 3.13, CPU-torch box)
| Package | Version | License | Verdict |
|---|---|---|---|
| **Docling** | 2.130.0 (2026-09-22) | MIT; models Apache-2.0/CDLA-permissive ([repo](https://github.com/docling-project/docling)) | **Primary** |
| **MarkItDown** | 0.1.8 | MIT | **Fallback** |
| pypdfium2 | 5.13.0 | BSD/Apache, no advisories | Use for page count and validation |
| pdfplumber | 0.11.10 | MIT | 30 tables found in 0.65 s on a 30-page PDF |
| pdfminer.six | 20260107 | MIT | Two High advisories (pickle code execution, Nov 2025); pin at least 20251230 |
| python-docx | 1.2.0 | MIT | Entity resolution off (checked in source) |
| Unstructured | 0.27.8 | Apache-2.0 | Reject: 73 packages including spacy and numba; layout mode is blocked on Windows with Python 3.13 by its markers; the repo pushes the hosted service |
| trafilatura | 2.2.0 | Apache-2.0 | Reject: dropped my HTML table |
| PyMuPDF and pymupdf4llm | 1.28.2 | AGPL-3.0 or paid | Exclude |
| marker-pdf | 2.0.0 | Code Apache-2.0; weights free only under $5M revenue or funding | Exclude |
| LlamaParse | n/a | Paid | Free tier is real (10K credits, then $1.25 per 1,000, 48 h cache). Excluded because it sends buyer documents to a third party ([pricing](https://www.llamaindex.ai/pricing)) |
| pypdf | 6.19.0 | BSD | Not for untrusted input: 10 denial-of-service advisories, Aug–Sep 2026 ([advisories](https://github.com/py-pdf/pypdf/security/advisories)) |

**Docling measurements**
- Warm speed is 1.18 s/page on a 30-page PDF with a table on every page.
- All 30 tables were recovered, with headings and page plus bounding-box provenance.
- Memory is about 1.0 GB steady and 2.1 GB peak. The first run takes 41 s because it downloads models.
- Models are 172 MB (layout) plus 358 MB (TableFormer), about 500 MB in total.

**Docling install weight and limits**
- On Linux, `pip install docling` pulls the CUDA `nvidia-*` wheels. Pin the CPU torch index.
- Docling's default OCR downloads RapidOCR models from modelscope.cn at runtime. Turn OCR off and bake the models into the image.
- The Docling parts of the API and image have to be tested on Linux.
- Docling does not fit the 2 GB API machine, and no document may be parsed there.
- A Windows model cache under a long path hit the MAX_PATH error, so set `HF_HOME` short.
- `docling-slim` (no torch, 52 packages) converted DOCX, HTML and MD in under 0.1 s, so those formats can stay light. It lacked the `packaging` dependency for PDFs.
- Its no-model `NativePdfPipeline` emits line-level text with no headings or tables, so it is not a usable PDF fallback ([options](https://docling-project.github.io/docling/usage/advanced_options/)).
- The paper reports 2.4–2.6 GB peak on 225 pages ([arXiv 2408.09869](https://arxiv.org/html/2408.09869v5)).
- Docling had 7 advisories in 2026. The HTML-backend one is fixed in 2.94.0 and the ODF file-read one in 2.120.3, so 2.130.0 covers both.
- Restrict `allowed_formats` to PDF, DOCX, HTML and MD. Keep `htmlrender`, LaTeX, XBRL, USPTO and METS off. Keep local and remote fetch off. Set `max_num_pages=50` and `max_file_size`.

**Fallback**
- MarkItDown `[pdf,docx]` has no torch and took about 120 ms per document.
- Its PDFs lose headings, so it is degraded.
- It pins `magika~=0.6.1`, which conflicts with magika 1.x.
- Scanned PDFs with no text layer are rejected with a clear message.

## 2. Upload security
- **File type.** Use an allowlist by content, never extension. A hand-rolled check, cross-checked by `puremagic` 2.2.0, caught a disguised exe and a PDF renamed `.docx`. puremagic cannot tell DOCX from PPTX and returns nothing for `.md`.
  - PDF: `%PDF-` header.
  - DOCX: zip containing `word/document.xml`.
  - TXT/MD/HTML: strict UTF-8 decode with no NUL bytes.
  - `filetype` and `python-magic` are unmaintained since 2022.
- **Size and zip bombs.**
  - Cap size with a streaming byte counter through the API. R2 presigned URLs support GET, HEAD, PUT and DELETE only, with no POST policy and no size cap ([docs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)).
  - Cap pages, zip entries and absolute uncompressed size. Do not use a ratio cap: a tiny 17-entry DOCX had a 23.7:1 ratio.
- **PDF active content.** Reject `/JavaScript`, `/Launch`, `/OpenAction` and `/EmbeddedFile`. Never serve originals back.
- **Filenames.** Store by uuid. Keep only a sanitized display name (strip path, control and bidi characters, length cap).
- **Quotas.** Per-workspace document count, MB, pages and version count. Rate-limit per IP and per workspace. Make Turnstile required on upload; the Free plan is unlimited challenges, 20 widgets ([plans](https://developers.cloudflare.com/turnstile/plans/)).
- **Storage.** Use R2 for transient originals with a 1-day lifecycle rule: 10 GB, 1M Class A and 10M Class B operations free, egress free ([pricing](https://developers.cloudflare.com/r2/pricing/), [lifecycle](https://developers.cloudflare.com/r2/buckets/object-lifecycles/)). Delete right after the parse. Keep only chunks in Neo4j.
- **Malware scanning.** Make ClamAV optional and off by default.
  - ClamAV 1.5.4 (2026-08-07) fixed ZIP, PDF and XAR parser CVEs, so the scanner is itself attack surface ([releases](https://github.com/Cisco-Talos/clamav/releases)).
  - Official docs say 3 GiB minimum RAM ([Docker page](https://docs.clamav.net/manual/Installing/Docker.html)), so it only fits the parse machine.
  - The VirusTotal public API is out: no commercial use, and it shares samples ([terms](https://docs.virustotal.com/reference/public-vs-premium-api)).
  - MetaDefender's free tier is 25 requests a day, from a secondary source.
  - The sandbox plus never storing or serving files does more than a scan.
- **Prompt injection** ([OWASP LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/), [Spotlighting](https://arxiv.org/pdf/2403.14720), [design patterns](https://arxiv.org/abs/2506.08837)).
  - Uploaded text goes only into the final tool-less synthesis call, in a randomly delimited or datamarked block labelled `source_type=user_upload`.
  - Do not let retrieved upload text drive tool calls.
  - Strip markdown images and non-allowlisted links from output, since image URLs are a data-exfiltration channel.
  - Flag suspicious chunks with heuristics rather than blocking them.
  - Show "user-provided, not filing evidence" in the UI.
  - A classifier is optional. ProtectAI deberta v2 (about 184M parameters, license not confirmed) scored better than Prompt Guard 2 in a 2026 comparison I saw via search only.

## 3. Multi-tenancy on Neo4j Community
- **One database only.** "Installations of Community Edition can have exactly one standard database" ([ops manual](https://neo4j.com/docs/operations-manual/current/database-administration/)). Community has no RBAC or property-level access control ([comparison](https://neo4j.com/docs/operations-manual/current/introduction/)). Isolation is therefore application-level.
- **Recommended design.** A separate label set (`UserDocument`, `UserVersion`, `UserChunk`, `UserRisk`) plus a mandatory `workspace_id`, through one `WorkspaceRepo`.
  - Give uploads their own vector index: `CREATE VECTOR INDEX user_chunk_embedding ... WITH [c.workspace_id, c.is_current]`.
  - Live public queries cannot see uploads by construction, and the existing indexes need no rebuild.
  - In-index filtering is GA since 2026.02, including Community, on `LIST<FLOAT>` embeddings ([blog](https://neo4j.com/blog/genai/vector-search-with-filters-in-neo4j-v2026-01-preview/)).
  - The `SEARCH WHERE` grammar allows comparisons, AND, and (from 2026.06) IN. It does not allow `<>`, `OR`, `NOT` or null checks ([SEARCH clause](https://neo4j.com/docs/cypher-manual/current/clauses/search/)). Staleness must therefore be a boolean `is_current`, not `superseded_at IS NULL`.
  - A post-filter after the LIMIT would let 11k public chunks crowd out a small tenant.
  - Retrieval runs two SEARCHes (public plus workspace) and merges them by RRF (reciprocal rank fusion) in Python.
- **Workspace identity.** A 128-bit token, with only its sha256 stored. No accounts.
- **TTL.** Community has no native TTL, so a sweeper deletes `Workspace {expires_at < now}` in batches. I did not verify APOC on the deployed image.
- **Enforcement.** A CI leak harness seeds two workspaces plus public data and asserts every retriever, cache and evidence path returns zero foreign rows.
- **Alternative.** A second small Neo4j app for user data ($0.0082/h at 1 GB) gives physical separation and nightly wipes, at the cost of app-level merging. Keep the repo interface so you can swap.
- **Not exercised live.** I could not run Neo4j here. Smoke-test `WITH [...]` and `IN` on 2026.07 in step 1. The current docs version is 2026.09.

## 4. Processing architecture
- **Queue.** RQ 2.12.0 (BSD-2) plus Valkey 9.1.2 ([RQ needs Redis 5+ or Valkey 7.2+](https://github.com/rq/rq), [Valkey](https://valkey.io/download/)).
  - It forks a work-horse process per job and kills it on timeout, which suits untrusted CPU-bound parsing.
  - Job status via `job.meta`, custom job ids, `Retry`, and burst mode so the worker exits when idle.
  - Windows dev uses SpawnWorker or eager mode.
- **Rejected queues.** arq is maintenance-only ([repo](https://github.com/python-arq/arq)). Dramatiq 2.2.1 is LGPL and thread-based. Celery is heavy and not Windows-friendly. Taskiq (0.12.6) suits asyncio, not CPU-bound parsing.
- **Broker hosting.** Use self-hosted Valkey on a 256 MB Fly machine, about $2 a month at $0.0028/h. Upstash's free tier is 500K commands a month ([Upstash](https://upstash.com/pricing/redis)), which an idle worker's polling may exhaust (my estimate).
- **Fly prices.** I used the per-hour figures. The monthly column came back inconsistent from my fetch. A performance-1x with 4 GB is $0.0591/h and shared-cpu-1x with 2 GB is $0.0154/h ([pricing](https://docs.fly.io/about/pricing/)).
- **Two stages, two Fly apps** (secrets are per app).
  - **Stage 1, parse (`semigraph-parse`).** An on-demand performance-1x 4 GB machine with baked models and no Neo4j or LLM secrets. It reads the file, writes normalized JSON blocks to R2 through presigned URLs, and exits.
  - **Stage 2, ingest (trusted).** It chunks, diffs, embeds only changed chunks, optionally extracts, and writes to Neo4j.
- **Job states.** `queued → parsing → chunking → embedding → [extracting] → indexing → ready`, plus `failed` and `canceled`. The API streams progress over SSE by polling `job.meta`.
- **Idempotency.** `job_id = sha256(ws|doc_key|file_sha256)`. An identical re-upload is a no-op.
- **Cost control.** Default to extraction-free mode (chunk and embed only). Deep analysis is opt-in behind `upload_llm_model`, with a cost preview. I left the model choice to the other track.
  - With the repo's constants in `estimate_extraction_cost` (800-token overhead, 300 output tokens, $3/$15 per Mtok), a 50-page document is about $0.47, roughly $0.01 per page.
  - The formula is `n_chunks × (800 + avg_tokens) × p_in + n_chunks × 300 × p_out`, scaled by your chosen model.
  - A version 2 with 10% changed chunks costs about $0.05.
  - Cap tokens per document and per workspace, and log spend to the existing ledger.
  - The extraction cache is keyed on chunk hash, prompt version and model.

## 5. Updated-document semantics
- **Identity.** An explicit "upload new version of X" action gives a deterministic `document_key`. Auto-suggest a match, but never merge silently, using filename stem stripped of `v2`, `final` and dates, title, and embedding-centroid similarity of 0.8 or higher. An identical file hash is a no-op.
- **Diff granularity.**
  - Diffing 700-token packed chunks fails, because one inserted paragraph shifts every later boundary.
  - Hash normalized blocks (paragraphs, tables, headings) and diff at heading-section level with `difflib.SequenceMatcher`.
  - Re-chunk and re-embed only sections whose hash changed. Add fuzzy match (rapidfuzz 3.14.6) to label modified against added-plus-removed.
- **Lifecycle.**
  - Chunks are unchanged, replaced, or superseded (never deleted).
  - Each carries `version`, `valid_from`, `valid_to`, `recorded_at`, `superseded_at` and `is_current`.
  - Default retrieval uses `is_current = true`. As-of queries use range filters on the date properties.
  - Show the version on every citation. Bump the workspace data version on every change so stale answers cannot be served.
- **What-changed report.** `compute_temporal_states` and `cluster_lineages` are pure and reusable. Map cik to `document_key`, accession_no to `version_id`, and filing_date to the version date.
  - The fetch needs its own query, because the existing one hardcodes `form IN ['10-K','20-F']`.
  - The function emits dropped lineages only. Add small helpers for new and changed.
  - Same-day versions never close lineages, since it compares dates with `<`. A buyer demo uploads v1 and v2 minutes apart, so use a version ordinal or synthetic dates.
  - The extraction-free fallback is a section-level added, removed or modified diff.
- **Prior art.** `langchain-core` 1.6.5 `index()` has `incremental`, `full` and `scoped_full` cleanup, but only hard-deletes and has no soft-supersede. Borrow the design, do not adopt it.

## 6. Ordered implementation (S ≤ 0.5 d, M = 1–2 d, L = 3–5 d)
1. **M.** `WorkspaceRepo`, labels, `user_chunk_embedding`, token, sweeper, cache-key fix, leak-test harness, Neo4j smoke test.
2. **M.** Upload gate: streaming cap, content allowlist, zip and page limits, Turnstile-required, quotas, Valkey limiter.
3. **L.** Parse app: Docling image (CPU torch, baked models, `allowed_formats`), RQ plus Valkey, burst start, MarkItDown fallback.
4. **M.** Blocks to section hash to chunks, with upload chunk ids, reusing the chunker's heading-flush logic.
5. **M.** Ingest stage: embed changed sections, write, SSE progress.
6. **M.** Answer path: two-SEARCH RRF, delimited `user_upload` block, new regexes and `valid_ids`, token-scoped evidence route.
7. **M.** Version chain, supersede flags, as-of queries, section diff.
8. **M.** Optional extraction with budget and cost preview, plus the what-changed report with new and changed helpers.
9. **S–M.** Injection flagging, retention, red-team fixtures, optional ClamAV.
10. **M.** Demo UI: version timeline, freshness badges, what-changed view.

## Unverified
- Fly shared-CPU Docling speed (my estimate: 2–3× slower than this box).
- Filtered `SEARCH` on Community 2026.07, including `IN`.
- MetaDefender terms, and ProtectAI's license.
- The Docling and pypdf advisory pages I opened are primary; the other CVE lists came through aggregator search results.
