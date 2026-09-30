# M4: freshness monitor, document upload with versions, dossier / risk-change data (authoritative plan, 2026-09-29)

Status: pre-registered plan, written by the fable-architect before any M4 code. Supersedes `docs/v2/M4_UPLOAD_PLAN.md`
(2026-09-26, kept for its history) and PLAN.md section 5's upload design (the `U:{ws}:...` citation form, the Docling parse app,
RQ/Valkey/R2 are all obsolete). Verified against the code map and the live-web research report of 2026-09-29.
Owner order: M1b -> M3 -> M4 (M1b, M3 done and live; template fingerprint `4d0a62f5a0`, `AGENT_ENABLED=true` in production).
Test runner: `.venv/Scripts/python -m pytest` (never `uv run` under OneDrive). Every subagent names its model (sonnet workers, opus verifier).

**Revision 2 (main session, 2026-09-29, before any M4 code):** spike numbers S1 / S1b / S1c / S5 and the owner's decisions are
folded in (section 13). The API machine becomes `shared-cpu-2x` / 4 GB (owner: "Bigger machine"); the upload caps are
re-registered to the approved "about 2x the single-core option" (single-core option: ~15 pages / ~8k tokens) = **30 pages and
16,000 tokens per version**; G4 is rewritten; the five planner questions are resolved in section 13 (none needed the owner).
Where revision 2 and the text below it differ, the numbers in this revision are the ones implemented.

## 1. Scope

| Part | Thin (this milestone, ships) | Full (deliberately deferred) |
|---|---|---|
| Freshness monitor | Detects and surfaces: per-filer EDGAR submissions vs `Filing` nodes in the SERVED graph, Federal Register live count vs `ExportControl` nodes; background thread + Neo4j lease; `Svc*` persistence; public `GET /api/freshness`; admin `POST /api/admin/freshness/check`; GitHub Actions heartbeat; one header line in the page ("Data as of ... checked ... N pending") | Auto-promotion on a hit (PLAN.md section 4 says "promote on a hit": **explicit deviation, D1**), RSS/ETag feed, SEC-deadline "possible late filing", dashboard page (M5) |
| Upload workspace | PDF, DOCX, Markdown, TXT, HTML; explicit new versions; `what-changed` from `alignment.align` + `passages.compute_passages`; `/api/ask` with `workspace_id` (+ `as_of`) on a separate template; `doc:` citations; stale chips; 24 h TTL, sweeper, DELETE; Turnstile + quotas; upload panel in `index.html` | OCR / scanned PDFs, Docling, band adjudication (LLM), deep extraction (risk items from uploads), agent over uploads (rejected 400, D7), a separate page (M5) |
| Dossier + risk-change data | Two read-only GET endpoints backed by existing retriever queries, no LLM, no embedder; JSON only | UI (M5), export, watchlists, GDS centrality |

Out for M4: anything that changes live SEC answers; any DB-app redeploy; anything needing the data lake on the serve machine.
If time runs short the cut order is: HTML -> `as_of` on workspace asks -> DOCX -> page banner. Never cut: the leak harness,
the fingerprint pin, the examples byte-identical test, the isolation subprocess tests, the parity gate.

## 2. Verified constraints (file:line) and stale claims corrected

Confirmed (code map 2026-09-29):
- API machine `shared-cpu-1x` / `2gb` + 1 GB swap today (`fly.toml:9,44`), region `sin`; M4 resizes it to `shared-cpu-2x` / `4gb`
  (section 13). Neo4j `1gb`, heap 400m (`deploy/neo4j/fly.toml:17-18,30`), unchanged.
- `OnnxBackend.encode_passages` is an unbatched loop (`src/semigraph/embeddings_onnx.py:80-82`); measured in S1 / S1b / S1c.
  No lock or semaphore serialises the embedder anywhere in `src/semigraph` (grep 2026-09-29): an upload thread and a live-ask
  thread can call the one ONNX session concurrently (ONNX Runtime `Run` is thread-safe; S1c measured it).
- `template_fingerprint()` = sha of `ANSWER_PROMPT + CONTEXT_HEADERS` (`retrieval/answerer.py:77-81`, `context_layout.py:54-61`);
  `ContextBlocks` has exactly 6 fields and `format_full_context` zips `strict=True` (`answerer.py:84-97`). Pinned `4d0a62f5a0`
  in `tests/test_agent_seam.py:61` (already exists: M4_UPLOAD_PLAN section 5's "pin the fingerprint" item is DONE, not to do).
- `_EXCERPT_ID_LINE` matches only chunk ids (`answerer.py:249`); `sources_from_context` (`answerer.py:252-273`) is the only way
  an excerpt's text becomes a grounding source, so a `doc:` id needs the extension in section 4.2.
- Citation grammar lives in `retrieval/ids.py:18-43` and is COPIED as JS literals in `serve/static/index.html:95-99`.
- `evidence()` does an unguarded `_EVIDENCE[kind]` (`serve/routes.py:170-182`): a `doc` kind would 500 today.
- `agent/sanitize.py:110-111` `safe_id()` calls `classify_id()` directly: a `doc:` grammar would be allowlisted to the planner
  with no code change. Decision in section 4.2.
- `store.cache_key` has no workspace dimension (`serve/store.py:26-33`); `/api/ask` reads the cache at `routes.py:198` and writes
  at `routes.py:298-301` before any workspace concept exists.
- `graph/schema.py:132` and `serve/main.py:67` hard-code the string `'Svc'` instead of `SERVICE_LABEL_PREFIX` (`schema.py:35`);
  `main.py:68` counts ALL relationships unfiltered. Both must filter on `PRIVATE_LABEL_PREFIXES` (section 6).
- `ingestion/__init__.py:29-35` eagerly imports `xbrl` -> `pandas` (`ingestion/xbrl.py:51`): NOTHING under `semigraph.ingestion`
  imports in the serve image. `edgar.py`, `freshness.py`, `federal_register.py` themselves are stdlib + config + universe only.
  `graph/freshness.py:34` also imports pandas (a different module with the same name: the monitor must never import it).
- `edgar._identity()` raises without `SEC_USER_AGENT` (`ingestion/edgar.py:114-121`); `config.py:38` defaults it to `""`;
  absent from `fly.toml [env]` and from `scripts/push_fly_secrets.py:20-28`.
- `pending_filings` reads the lake manifest and may WRITE `company_tickers.json` (`ingestion/freshness.py:155-192`,
  `edgar.py:151-164`); neither exists in the image. The monitor resolves CIK from `Company.cik` and "known" from `Filing.accession_no`.
- `count_query_url(as_of)` is pure (`ingestion/federal_register.py:209-218`); `make_fetcher` tolerates an empty UA (`:258-261`).
- Serve-shipped CI job runs a hand-maintained filename list (`.github/workflows/tests.yml:76-80`): new tests must be named
  `tests/test_serve_*.py` (or `test_retrieval_*.py`, `test_ids.py`, `test_answerer_*.py`) to run on the shipped dependency set.
- `deploy/neo4j/restore-and-start.sh:9` loads the dump with `--overwrite-destination=true`: a future dump swap deletes every
  `User*` node and the `Svc*` ledger. Acceptable under the 24 h TTL; add to the RUNBOOK (section 11).
- `index.html` has exactly one inline `<script>` (`tests/ui/harness.mjs:31-32`); CSP `form-action 'none'` (`routes.py:30-35`):
  uploads go through `fetch()` + `FormData`, never a `<form>` submit. No static-file mount exists (`main.py:178-182`).
- `lifespan` (`main.py:151-175`) is the only place for background threads; teardown is the `finally` around `driver.close()`.
- Admin pattern: `_check_admin` + `X-Admin-Token` (`routes.py:334-355`). Rate limiters live on `app.state` (`main.py:165-169`).
- `examples.json` records carry `answer, checks, citations, hallucinated, id, question, type` but NO context (53 examples).

Stale claims in M4_UPLOAD_PLAN.md, corrected here:
1. Section 0 bullet 2 / section 2: "`alignment.py` + `passages.py` import as-is in the serve image" is FALSE. `graph/alignment.py:84-87`
   imports `rapidfuzz` and `scipy.optimize.linear_sum_assignment`; `graph/passages.py:113-119` and `graph/align_text.py:25` import
   `rapidfuzz`. Neither is in `deploy/requirements-serve.txt`. Decision: add `rapidfuzz` and `scipy` to the serve image (BSD / MIT,
   manylinux cp313 wheels; standard packages; the pure modules stay byte-identical); S5 re-measures RSS with them imported.
2. Section 5's dependency list must read: `python-multipart>=0.0.32, pdfplumber>=0.11.10, pypdfium2>=5.13, python-docx>=1.2,
   puremagic>=2.2, rapidfuzz, scipy`.
3. "A test pins the CURRENT fingerprint" is already done (`tests/test_agent_seam.py:61`).
4. The plan never said how the PUBLIC evidence route treats `doc:` ids: it returns 404 by design (the workspace id is not in the
   citation, so the public route cannot resolve one without leaking existence). Workspace evidence has its own token-gated route.
5. PLAN.md section 4's "promote on a hit" is replaced by detect-and-surface (D1; reasons in section 4.1).
6. Section 3's "Turnstile fail-closed on workspace creation and each upload" is kept and made stricter: when
   `TURNSTILE_SECRET_KEY` is empty the upload routes answer 503 in production (the `/api/ask` "allowed but logged" posture of
   `guard.verify_turnstile`, `guard.py:83-108`, does NOT apply to uploads).

## 3. Libraries (research report 2026-09-29, live PyPI / GitHub / OSV) and the caps

| Need | Choice | License | Maintenance evidence | Measured |
|---|---|---|---|---|
| PDF text + font metrics | **pypdfium2 5.13.0** primary via raw `FPDFText_GetUnicode` / `FPDFText_GetMatrix` (use `matrix.d` as effective size: the nominal `GetFontSize` was a constant 1.0 on a Word-exported PDF) / `FPDFText_GetFontWeight`; **pdfplumber 0.11.10** `extract_words(extra_attrs=["size","fontname"])` as cross-check (never `extract_text_lines(return_chars=True)`: 3x RSS) | BSD-3 + Apache-2 (PDFium; bundled natives all permissive) / MIT | 17 and 3 releases per 12 mo; 3 and 109 open issues; both pushed 2026-08 or later | 40-page real PDF (700 KB): pypdfium2 0.22-0.35 s, ~51-57 MB peak; pdfplumber 2.4-2.5 s, ~69 MB (words). Text yield 2,500-2,900 chars/page (>10x the 200 chars/page scanned threshold). Heading recall 0.94, precision 0.02-0.22: the header/footer filter is mandatory (section 4.2) |
| DOCX | **python-docx 1.2.0** via `Document.iter_inner_content()` (`.paragraphs` silently drops table text; verified) + run-level bold/size fallback (a bold 12 pt run still reports style "Normal"; verified) | MIT | last release 2025-06-16 (watch); repo commits 2026-08-01; docling-slim depends on it; hedge: docx2python 3.7.1 | ~0.02 s / ~41 MB (interpreter baseline) |
| Multipart | **python-multipart>=0.0.32** | Apache-2 | 12 releases per 12 mo; all 8 CVEs fixed by 0.0.31 | n/a |
| HTML | stdlib `html.parser` (selectolax 0.4.13, MIT, is the fallback if precision is ever needed; never bs4) | - | - | n/a |
| Magic bytes | **puremagic 2.2.0** (pure Python, no libmagic) | MIT | 6 releases per 12 mo (filetype: last release 2022-11) | identified PDF, DOCX and an .exe renamed .pdf correctly |
| Scheduler (TTL sweeper, monitor) | stdlib `threading` in the lifespan; **no APScheduler** (3.11.3 in reserve; 4.x is alpha, CVE-2026-31072) | - | - | n/a |
| Alignment | existing `graph/alignment.py` + `passages.py` (+ rapidfuzz, scipy added to the image) | MIT / BSD | - | S4 |
| Excluded, reconfirmed | pymupdf (AGPL, owner hard rule), docling/docling-slim (`[standard]` still mandates torch), markitdown (flattens font data, pulls magika + onnxruntime), python-magic (libmagic) | | | |

Whole resolved closure (uv pip compile, manylinux_2_28 cp313): zero copyleft; net-new wheels < 30 MB (cffi, cryptography, lxml,
pdfminer-six, pdfplumber, pillow, pycparser, pypdfium2, python-docx, python-multipart) plus rapidfuzz and scipy.
pdfplumber pins `pdfminer.six==20260107` exactly: watch pdfminer.six advisories directly (both 2025 CVEs already fixed).

Caps (revision 2; the embedding time sets them, not parse memory):
- 15 MiB per file; **30 pages per version** (checked after parsing, before chunking); **16,000 tokens per version** counted with
  the embedder's own tokenizer over the WHOLE version text (hash-keyed reuse makes a new version faster but never lets it past the
  cap; at ~2,700 chars/page the token cap binds first, around 22-25 pages of dense prose). Over-cap uploads fail the JOB with
  `too_many_pages` / `too_many_tokens` (both known only after parsing), never the route.
- Every embedded chunk is **<= 512 tokens** (S1b: memory is flat up to ~1k tokens, +950 MB at 1,537): `chunk_units` takes an
  injected `count_tokens` callable (the ONNX tokenizer in production, a conservative `len(text) // 3` in pure tests) and splits a
  chunk at the last sentence or whitespace boundary before 512 tokens; target 1,200 chars, char max 1,800, token max 512.
- `MAX_CHUNKS_PER_VERSION` = 120 (16k tokens / ~300 tokens per chunk ~ 55 chunks; 2x headroom for short paragraph units).
- 3 documents x 5 versions per workspace; 120 pages per workspace; **48,000 embedded tokens per workspace** (tokens actually
  embedded after reuse: ~3 max-size uploads of CPU per workspace).
- `MAX_UPLOADS_PER_DAY` = 40 (global): 40 x ~190 CPU-s ~ 2.1 CPU-hours/day ~ 8.8 % of one core on average, below the 12.5 %
  pooled baseline of a `shared-cpu-2x` machine, so upload traffic alone cannot drain the burst balance day over day.
- Parse subprocess: 90 s wall, `RLIMIT_CPU` 120 s, `RLIMIT_AS` 1 GiB on Linux with `MALLOC_ARENA_MAX=2` in the child environment
  (RLIMIT_AS caps VIRTUAL memory: glibc per-thread arenas and native libraries reserve address space, so the limit is proven by
  running the max-size fixture under it in the Linux serve-shipped job, not by reading `ru_maxrss`); output JSON <= 4 MiB.
- Embedding wall budget `UPLOAD_EMBED_TIMEOUT_S` = 1,200 s: generous on purpose, so a job on a CPU-throttled machine (risk 13)
  finishes slowly with visible progress instead of failing; the UI shows progress per chunk and an ETA from the job's own rate.

## 4. Architecture

### 4.1 Freshness monitor (detect and surface; never ingest)

D1, encoded: the monitor never promotes. The serve image has no pandas / sec-parser / torch; extraction is paid; M1b alignment is
adjudicated and reviewed; an unattended promotion would change live answers without review; and a new seed dump resets the
ledger, cache and kill switch. Promotion stays `ingest -> extract -> align-items -> build-graph -> dump -> DB deploy` (RUNBOOK
"Updating the graph"). This deviates from PLAN.md section 4 on purpose.

D3, encoded: make `src/semigraph/ingestion/__init__.py` lazy with PEP 562, exactly like `graph/__init__.py:16-19`: the five
`xbrl` names (`KEY_CONCEPTS, curate_metrics, download_companyfacts, extract_metrics, supplement_metrics_from_filing_xbrl`) resolve
through `__getattr__`; everything else stays an eager import. No code is copied. Pin: `tests/test_serve_monitor_isolation.py`
(subprocess, same shape as `tests/test_agent_isolation.py`) asserts `import semigraph.serve.monitor, semigraph.ingestion.freshness,
semigraph.ingestion.edgar, semigraph.ingestion.federal_register` leaves `pandas`, `semigraph.ingestion.xbrl` and
`semigraph.graph.freshness` out of `sys.modules`, and that `from semigraph.ingestion import curate_metrics` still works where pandas exists.

D4, encoded: `SEC_USER_AGENT` becomes a Fly SECRET (add to `FLY_KEYS["semigraph"]` in `scripts/push_fly_secrets.py`, value in
`.env.fly`), not a `fly.toml [env]` entry. Reason: the SEC identity string is "name email"; committing the owner's email to a
public repo is the wrong trade even though the value is not confidential. Missing value = the monitor reports `configured:false`
and idles (never raises at boot; the admin route answers 503).

D5, encoded. Module `src/semigraph/serve/monitor.py`:
- `check_once(driver, settings, *, fetch=None, today=None) -> FreshnessReport` (pure given `fetch`): reads
  `MATCH (c:Company) RETURN c.ticker, c.cik` and `MATCH (:Company)-[:FILED]->(f:Filing) RETURN f.accession_no`; for each ticker in
  `universe.FILERS` (tickers absent from the graph are listed as `unresolved`): `fetch_submissions(cik, fetch=...)` ->
  `records_from_submissions` -> `select_targets(records, annual_form, quarterly_form, ANNUAL_SINCE, as_of)`; pending = targets whose
  accession is not a `Filing` node. FR: `int(fetch(count_query_url(as_of))["count"])` vs `MATCH (x:ExportControl) RETURN count(x)`.
  Fair access: declared User-Agent, `time.sleep(0.15)` between requests (the existing pause), 20 s timeout, `with_retries` from
  `federal_register`. ETag: skipped (one small GET per filer per 6 h is not worth the state).
- `FreshnessMonitor(driver, settings)`: `start()` runs a daemon `threading.Thread`; first pass `FRESHNESS_BOOT_DELAY_S` (default
  300 s, past embedder warm-up) after boot, and it checks then only if `SvcFreshness.checked_at` is missing or older than
  `poll_hours` (the owner's STOP scales the API to zero, so a machine rarely lives 6 h; a wait-first loop would never check);
  afterwards wait `poll_hours` (default 6, `FRESHNESS_POLL_HOURS`) on a `threading.Event`, try the lease, run `check_once`,
  persist, repeat. `stop(timeout=5)` sets the event and joins. Never touches the event loop or `answer_slots`.
- Lease (one machine polls), one atomic statement:
  `MERGE (l:SvcLease {key:'freshness'}) WITH l WHERE l.until IS NULL OR l.until < $now OR l.holder = $me
   SET l.holder = $me, l.until = $now + duration({minutes: 30}) RETURN l.holder = $me AS ok`.
- Persistence (only `Svc*`): `SvcFreshness {key:'latest', checked_at, as_of, snapshot_id, status, error, pending_json, fr_json,
  unresolved_json, duration_s}` via `store.serialize`; `SvcLease`. `reset_graph` already keeps `Svc*`.
- Routes (`src/semigraph/serve/monitor_routes.py`, `router`): `GET /api/freshness` (public, `read_rate_limiter`) ->
  `{"configured": bool, "enabled": bool, "status": "ok|never|error|stale|unconfigured", "checked_at": iso|null,
  "snapshot_as_of": "2026-09-24", "next_check_at": iso|null, "pending_count": n,
  "pending_filings": [{"ticker","cik","form","filing_date","accession_no","period_of_report"}],
  "federal_register": {"graph_count": n, "live_count": n, "new_since": n}, "unresolved": ["TICKER"], "duration_s": f}`;
  `stale` = `checked_at` older than 2x `poll_hours`. `POST /api/admin/freshness/check` (`X-Admin-Token`, 401 otherwise): runs
  `check_once` in the threadpool with a 120 s bound, persists, returns the same JSON; 409 if a check is running; 503 if unconfigured.
- Heartbeat: `.github/workflows/freshness-heartbeat.yml`, cron every 6 h plus `workflow_dispatch`, `curl -sS
  https://semigraph.fly.dev/api/freshness` (public GET, read-only, no secret in CI). The app unreachable or answering 5xx from the
  Fly proxy (the owner's STOP scales it to zero on purpose) is a `::warning::` annotation and exit 0; a 200 whose `status` is
  `error`/`stale`/`unconfigured` fails the job. No `ADMIN_TOKEN` in GitHub (Q2 resolved read-only, section 13).
- Page: one line in the header of `index.html` from `/api/freshness` ("Data as of 2026-09-24 - checked <relative> - N filings
  pending" or "freshness check unavailable"); no per-citation badge changes (M2/M5 scope).
- Lifespan: `monitor.start_if_enabled(app)` after bootstrap when `FRESHNESS_ENABLED` (default false; true in `fly.toml`);
  `monitor.stop(app)` in the `finally` before `driver.close()`.
- Parity (D2): `scripts/freshness_parity.py --as-of 2026-09-24` runs `check_once(..., today=as_of)` against the local graph and
  `ingestion.freshness.pending_filings / federal_register_pending` against the lake and prints both sets and their symmetric
  difference; the JSON goes to `artifacts/freshness_parity.json`. Gate G6 in section 9.

### 4.2 Upload workspace

Package `src/semigraph/uploads/` (imports allowed: stdlib, `numpy`, `graph/alignment.py`, `graph/passages.py`, `graph/align_text.py`,
`hashing.py`, `versions.py` constants, `graph/client.run_cypher`, `embeddings.Embedder`; the parse worker alone imports pypdfium2 /
pdfplumber / python-docx; never `graph/items.py`, `parsing/*`, `graph/loaders.py`, `graph/temporal.py`, `compute_filing_versions`).

| Module | Owner | Responsibility (contract) |
|---|---|---|
| `uploads/gate.py` | A | `sniff(head: bytes, filename) -> kind` in `{"pdf","docx","html","md","txt"}` via puremagic (+ UTF-8 decode check for text kinds); `check_pdf_bytes(data)` rejects `/JavaScript /JS /Launch /OpenAction /AA /EmbeddedFile /RichMedia` and encrypted PDFs; `check_docx_zip(data)` (<= 200 members, uncompressed <= 60 MiB, no absolute or `..` names, only `word/`, `docProps/`, `_rels/`, `[Content_Types].xml`, `customXml/`); `GateError(code, message)`, code in `{unsupported_type, too_large, too_many_pages, active_content, encrypted, zip_bomb, scanned, empty}` |
| `uploads/parse.py`, `uploads/parse_worker.py` | A | `parse_document(data: bytes, kind) -> ParsedDoc` runs `sys.executable -m semigraph.uploads.parse_worker <kind>` with the bytes on stdin and JSON on stdout, `timeout=90`, the child sets `RLIMIT_AS=1 GiB` and `RLIMIT_CPU=120` itself as its first statements on Linux (section 14.2; never `preexec_fn`), child env adds `MALLOC_ARENA_MAX=2`, kill on timeout -> `ParseError("timeout")`; output > 4 MiB -> `ParseError("too_large")`. `ParsedDoc = {method: "pypdfium2|python-docx|html|text", pages: int, blocks: [{text, page, size, bold, kind_hint}], chars_per_page, warnings}`. PDF: pypdfium2 raw API with the `FPDFText_GetMatrix` scale as size and `GetFontWeight >= 600` as bold; pdfplumber `extract_words` only when pypdfium2 yields < 200 chars/page (cross-check before declaring "scanned"). DOCX: `iter_inner_content()` with the paragraph style name AND run-level bold/size. HTML: stdlib `HTMLParser`, `h1-h6` as headings, scripts/styles dropped |
| `uploads/units.py` | A | `canonical_text(blocks) -> str` (blocks joined with two newlines; `hashing.content_hash` does its own normalisation); `detect_units(blocks) -> [Unit{unit_id, kind: heading or paragraph, headline, char_start, char_end}]` with the MANDATORY running header/footer filter (a normalised line repeating on >= 50 % of pages, min 3 pages, is dropped first), heading = `size >= body_mode + 1.0 OR bold`, `<= 12 words`, no terminal period, not all digits; Markdown `#`; DOCX `Heading N` styles or the bold+size fallback; if headings cover < 3 units the document falls back to paragraph units; `chunk_units(text, units, *, count_tokens, max_tokens=512) -> [Chunk{seq, char_start, char_end, tokens}]` exact slices, target 1,200 chars, char max 1,800, token max 512 (split at the last sentence/whitespace boundary under the token max), never splitting inside a unit shorter than both maxima; `UnitRow = {item_id, text, headline, text_hash, unit_kind, char_start}` (the row shape `alignment.align` and `passages.compute_passages` take) |
| `uploads/changes.py` | A | `compare_versions(older: ParsedVersion, newer: ParsedVersion) -> ChangeReport` = lexical-only `alignment.align(older_rows, newer_rows, newer_text, embed=None, older_section_text=older_text)` then `passages.compute_passages(...)` with each side's `(chunk_id, char_start, char_end)`; `not_compared_reason` in `{identical_content, parse_method_mismatch, low_text_yield, heading_coverage_mismatch, too_many_units}` sets `items_compared=false`; `ChangeReport = {items_compared, not_compared_reason, added: [unit], removed: [unit], changed: [{older_unit_id, newer_unit_id, passages: [{quote, chunk_id, kind}]}], unchanged_count}`; every quote is asserted to be a substring of the text of the chunk it cites |
| `uploads/versions.py` | B | `next_version(existing: [int]) -> int`; `currency_updates(document_versions) -> [(version, is_current, status, valid_to)]` (the ordinal decides; older -> `is_current=false, status=versions.SUPERSEDED, valid_to=now`); `is_visible(version_row, as_of) -> bool` (`valid_from <= as_of < valid_to`) |
| `uploads/repo.py` | B | ALL Cypher for `User*` labels; every statement binds `$ws` and every `User*` node pattern carries `{workspace_id: $ws}`. API: `create_workspace(driver, ttl_hours) -> (workspace_id, token)`; `authenticate(driver, workspace_id, token) -> bool` (sha256 stored, `hmac.compare_digest`, unknown id and bad token both False); `touch(driver, ws)`; `list_documents(driver, ws)`; `put_version(driver, ws, doc_id, version, parsed, units, chunks, embeddings, change_report, older_version)` in ONE transaction (creates nodes, flips the older currency, writes `SUPERSEDES` and `UserPassage`); `chunk_texts(driver, ws, chunk_ids) -> {id: {text, is_current, version, status, valid_to}}`; `search_chunks(driver, ws, vec, k, as_of)` using the same filtered `SEARCH` form the SEC index uses in `retrieval/retriever.py` (never an unfiltered `db.index.vector.queryNodes`); `changes(driver, ws, doc_id, older, newer) -> ChangeReport`; `delete_workspace(driver, ws)`; `sweep_expired(driver, now) -> int` (idempotent, batches of 500); `quota(driver, ws) -> {docs, versions, pages}`; `put_job / get_job`. `tests/test_serve_upload_repo.py` imports every module-level query string and asserts each `(x:User...)` pattern has `workspace_id: $ws` and each statement text contains `$ws` |
| `uploads/jobs.py` | C | `UploadJob` state machine `received -> validating -> parsing -> chunking -> embedding -> comparing -> indexing -> ready or failed`; runs on a `threading.Thread` behind `app.state.upload_slots = BoundedSemaphore(1)`; an in-memory `queue.Queue` per job mirrored to `UserJob.state`; rejects the version when its whole text exceeds 16,000 tokens or 30 pages (`failed`, codes `too_many_tokens` / `too_many_pages`) or the workspace would exceed 48,000 embedded tokens (`workspace_quota`); embeds only chunks whose `text_hash` is not already embedded in the same document (hash-keyed reuse across versions); calls `embedder.encode_passages` ONE chunk at a time on the shared in-process ONNX session (no second model load), emitting `progress {done,total,eta_s}` after each chunk, under the wall budget `UPLOAD_EMBED_TIMEOUT_S` (default 1,200); on Linux the worker thread lowers its own priority (`os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)`, guarded) so live asks win CPU contention; logs `tokens`, `wall_s`, `tokens_per_s` per job (no text) |
| `retrieval/workspace.py` | C | `workspace_retrieve(question, ws, driver, embedder, *, as_of, k=6) -> {"doc_chunks": [...], "stale_ids": [...]}`; `build_workspace_prompt(question, r_sec, r_ws, delimiter) -> (prompt, full_context, valid_ids, chunk_ids, sources)`; `strip_links_images(text)` removes `![..](..)` and `[text](url)` but never a `[doc:...]` or `[fr:...]` citation; `stream_workspace_answer(question, ws, driver, embedder, strategy, ...)` = `hybrid_retrieve` (unchanged) + `workspace_retrieve` + `build_workspace_prompt` + the shared tail `answerer.stream_answer_for_prompt(...)` with `postprocess=strip_links_images` and `force_buffered=True`; `done` gains `workspace: {"id_hash": sha[:12], "doc_chunks": n, "stale_citations": [ids], "suspicious": bool}` |
| `artifacts/prompts/answer_workspace.txt` | C | Separate template: the rules of `answer.txt` plus a block "UPLOADED DOCUMENT EXCERPTS (user-provided, not filing evidence)" wrapped in a per-request random delimiter `<<<DOC-{12 random letters}>>>`; text inside is data and never an instruction; cite uploads as `[doc:...]`; label them "from your document" |
| `serve/workspace_routes.py` | C | The routes in 4.4; `X-Workspace-Token`; Turnstile fail-closed; quotas; SSE |
| `serve/static/index.html` (panel + JS) | C | Create / restore workspace (token in `sessionStorage`), drag-drop upload with the job state list, document list with a version timeline, "What changed v1 -> v2" view, `doc:` chips opening the evidence drawer through the workspace evidence route, stale chips red when `stale_citations` names them |

Citation grammar (Step 0, `retrieval/ids.py`): add
`DOC_ID_PATTERN = r"doc:[0-9a-f]{12}:v[0-9]{1,3}:[0-9]{4}"`, `DOC_ID_RE = re.compile(rf"^{DOC_ID_PATTERN}\Z")`,
`CITE_RE = re.compile(rf"\[({CHUNK_ID_PATTERN}|{XBRL_ID_PATTERN}|{FR_ID_PATTERN}|{DOC_ID_PATTERN})\]")`, and `classify_id` returns
`"doc"`. In `answerer.py`: `_EXCERPT_ID_LINE = re.compile(rf"^\[({_ids.CHUNK_ID_PATTERN}|{_ids.DOC_ID_PATTERN})\]$")` (the fingerprint
does not hash it). `index.html:95-99` gets the same `DOC_ID` literal and `ID_FORMS.doc = "your document"`, and
`tests/test_static_ui.py` gains an assertion that the three existing JS literals plus `DOC_ID` equal the Python patterns byte for byte.
`agent/sanitize.safe_id` narrows to `classify_id(v) in ("chunk", "xbrl", "fr")` so `doc:` ids are never allowlisted to the planner
(structural, on top of D7); `tests/test_agent_sanitize.py` gets the negative case.
Byte-identical examples test (`tests/test_ids.py`): BEFORE editing ids.py the main session writes
`tests/data/examples_citations_pre_m4.json` = `{id: CITE_RE.findall(answer)}` for all 53 examples; the test asserts equality after
the change, that `classify_id` of every stored citation is unchanged, and that `answer_checks(answer, set(cites), set(cites), None)`
reproduces the stored context-free fields (`has_citation`, `pseudo_citations`, `is_refusal`). Locally, when
`data/processed/eval_runs.<snap>.jsonl` exists, `scripts/build_examples.py --check` recomputes the full checks and diffs (skipped in CI).

### 4.3 The shared writer seam (SEC fingerprint untouched)

`stream_answer_for_context` (`answerer.py:600-631`) builds the six SEC blocks and then runs the tail. Step 0 extracts the tail into
`stream_answer_for_prompt(question, prompt, full_context, valid_ids, chunk_ids, sources, strategy, *, llm_stream=None,
escalation_model=None, escalation_stream=None, postprocess=None, force_buffered=False, **stream_kwargs)`; `stream_answer_for_context`
becomes a thin caller (same `retrieval` event, same `ctx`, `sources=sources_from_context(full_context)`), so `answer.txt`,
`CONTEXT_HEADERS`, `build_blocks` and `render_prompt` are not touched and `template_fingerprint()` stays `4d0a62f5a0`.
`postprocess` is applied to the buffered draft before release and to the final text (default identity); `force_buffered=True`
routes a live stream through the draft-verify-release path even without an escalation model (the workspace path needs the draft in
hand to strip links before any byte reaches the client). Existing `tests/test_answerer_*.py`, `test_escalation.py`,
`test_answer_checks_events.py` prove the refactor; one new test pins that `stream_answer_for_context` output is unchanged for a
scripted stream (event-by-event equality against the pre-refactor recording committed to `tests/data/answer_events_pre_m4.json`).
The workspace template reuses everything after the prompt: `TextStream`, `_draft_then_escalate`, `_live_events`, `answer_checks`,
`verify_answer`, `needs_strong_model`, the ledger and the `done` grammar.

### 4.4 Graph schema and API contracts

Labels (all carry `workspace_id`; `PRIVATE_LABEL_PREFIXES = ("Svc", "User")` in `graph/schema.py`):
`UserWorkspace {workspace_id, token_hash, created_at, expires_at, last_seen}`;
`UserDocument {workspace_id, document_id (12 hex), title, created_at, latest_version}`;
`UserVersion {workspace_id, document_id, version, content_hash, method, pages, chars, created_at, is_current, valid_from, valid_to,
status, items_compared, not_compared_reason, suspicious}`;
`UserUnit {workspace_id, document_id, version, unit_id, kind, headline, text_hash, char_start, char_end}`;
`UserChunk {workspace_id, document_id, version, chunk_id, seq, text, text_hash, char_start, char_end, embedding, is_current,
valid_from, valid_to, status}`; `UserPassage {workspace_id, document_id, older_version, newer_version, older_unit_id, newer_unit_id,
kind, quote, chunk_id}`; `UserJob {workspace_id, job_id, document_id, version, state, detail, error, created_at, updated_at}`.
Edges: `(UserWorkspace)-[:OWNS]->(UserDocument)-[:HAS_VERSION]->(UserVersion)-[:HAS_UNIT]->(UserUnit)`,
`(UserVersion)-[:HAS_CHUNK]->(UserChunk)`, `(UserVersion)-[:SUPERSEDES {kind:'rolled', items_compared, not_compared_reason}]->(UserVersion)`,
`(UserUnit)-[:SUCCEEDED_BY]->(UserUnit)`, `(UserUnit)-[:HAS_PASSAGE]->(UserPassage)`. No edge ever touches a public label (repo test).
DDL, appended identically to BOTH `artifacts/schema.cypher` and `src/semigraph/artifacts/schema.cypher` (byte-identical today):
`user_workspace_id`, `user_document_id`, `user_chunk_id`, `user_job_id` uniqueness constraints; range indexes
`user_workspace_expires (expires_at)`, `user_chunk_ws (workspace_id)`; and
`CREATE VECTOR INDEX user_chunk_embedding IF NOT EXISTS FOR (c:UserChunk) ON (c.embedding)
 WITH [c.workspace_id, c.is_current, c.document_id, c.version, c.valid_from, c.valid_to]
 OPTIONS {indexConfig: {`vector.dimensions`: 1024, `vector.similarity_function`: 'cosine'}}`.
`user_chunk_embedding` is NOT added to `REBUILD_INDEXES`; `apply_schema` (idempotent, runs at every boot, `main.py:79-88`) creates it
on the live DB at the first API deploy. `reset_graph` and `graph_stats` filter on `PRIVATE_LABEL_PREFIXES`, and `graph_stats`
counts relationships with `MATCH (a)-[r]->() WHERE NOT any(l IN labels(a) WHERE l STARTS WITH 'Svc' OR l STARTS WITH 'User')`.

API (all workspace routes except creation require `X-Workspace-Token`; any miss on id or token is 404 `{"detail":"workspace not found"}`
after a constant-time compare; every response carries `Cache-Control: no-store`):

| Route | Request | Response |
|---|---|---|
| `POST /api/workspace` | JSON `{"turnstile_token": str}`; per-IP limiter 3 per day; 503 when Turnstile is unconfigured; 403 on failure | 201 `{"workspace_id", "token", "expires_at", "limits": {"max_documents":3, "max_versions":5, "max_pages":120, "max_bytes":15728640, "max_pages_per_version":30, "max_tokens_per_version":16000, "max_workspace_tokens":48000}}` |
| `GET /api/workspace/{ws}` | header | 200 `{"workspace_id", "expires_at", "documents": [{"document_id","title","latest_version","versions":[{"version","created_at","pages","chars","is_current","status","items_compared","not_compared_reason","suspicious"}]}], "usage": {"documents","pages"}}` |
| `DELETE /api/workspace/{ws}` | header | 204; every `User*` node of the workspace gone in one transaction |
| `POST /api/workspace/{ws}/documents` | multipart: `file` (required), `turnstile_token` (required), `document_id` (optional: a new version of it), `title` (optional, <= 120 chars); per-IP limiter 10 per hour; streamed size cap; global `MAX_UPLOADS_PER_DAY` (default 40, `SvcUpload` day counter in `store.py`) | 202 `{"job_id", "document_id", "version"}`; 200 `{"unchanged": true, "document_id", "version"}` when the content hash equals the latest version; route errors (checked on bytes before the job starts): 413 `too_large`, 415 `unsupported_type`, 422 `active_content`/`encrypted`/`zip_bomb`, 429 quota (`{"detail", "code"}`); `scanned`, `empty`, `too_many_pages`, `too_many_tokens`, `workspace_quota`, `parse_failed`, `timeout` are known only after parsing and arrive as the job's `failed` event |
| `GET /api/workspace/{ws}/jobs/{job_id}` | header; SSE (`Accept: text/event-stream`) | events `event: job`, data `{"job_id","state","progress":{"done","total"}?,"detail"?}` for each transition, final `ready` data adds `{"document_id","version","chunks","units","items_compared","not_compared_reason","suspicious"}`, or `failed` adds `{"error":{"code","message"}}`; heartbeat comment every 10 s; the stream closes after `ready`/`failed`; a finished job replays its final event |
| `GET /api/workspace/{ws}/changes?document_id=&from=1&to=2` | header | 200 the `ChangeReport` JSON (`items_compared=false` still returns `added/removed` empty and the reason); 404 when a version is missing |
| `GET /api/workspace/{ws}/evidence/{doc_id}` | header; `DOC_ID_RE` | 200 `{"id","text","document_id","version","is_current","status","valid_to","superseded_by_version","title"}` (same field names as the SEC evidence drawer where they overlap); 404 otherwise |
| `POST /api/ask` (existing) | body gains `workspace_id: str|null`, `as_of: "YYYY-MM-DD"|null`; header `X-Workspace-Token` when `workspace_id` is set | with a workspace: no cache read or write, `strategy=agent` -> 400 `{"detail":"strategy=agent is not available with a workspace"}` (checked BEFORE Turnstile and the slot), `strategy` limited to `hybrid`; `retrieval` event gains `"doc_chunks": n`; `done` gains `workspace` (4.2) |
| `GET /api/evidence/{id}` (existing) | | a `doc:` id -> 404 `{"detail":"not a public evidence id"}` (explicit, never `KeyError`) |
| `GET /api/stats` (existing) | | gains `"uploads_enabled": bool, "freshness": {"status","checked_at","pending_count"}` |

Gate order for `/api/ask` with a workspace: validate question / strategy / `as_of` / `workspace_id` shape -> reject agent -> token
(404) -> kill switch -> daily ceiling -> Turnstile -> per-IP paid window -> slot. Nothing is written before the token check.

### 4.5 Dossier and risk-change data (D8: read-only, no LLM, no embedder)

`src/semigraph/retrieval/dossier.py` (pure shaping over `retriever.py` constants) + `src/semigraph/serve/dossier_routes.py`
(`read_rate_limiter`, 60 s in-process cache per ticker, `Cache-Control: public, max-age=60`):
- `GET /api/company/{ticker}/dossier` -> `{"company": {"ticker","name","cik"}, "data_as_of", "filings": [{"accession_no","form",
  "filing_date","is_current","status","superseded_by"}], "metrics": rows of `METRICS_QUERY` (`retriever.py:137-153`) for the latest
  two fiscal years, "active_risks": plain `MATCH (c:Company {cik:$cik})-[d:DISCLOSES_RISK {status:'Active'}]->(rf:RiskFactor)
  WHERE rf.is_current RETURN rf.risk_id, rf.summary, rf.category, d.last_evidenced_at ORDER BY d.last_evidenced_at DESC LIMIT 25`
  (no `SEARCH`, no `$vec`), "edges": `company_edges_query(1)` rows (`retriever.py:303-311`), "rules": `RULE_EDGES_QUERY` rows}`;
  404 for a ticker outside `universe.FILERS`.
- `GET /api/company/{ticker}/risk-changes?limit=20` -> `{"company", "pairs": [{"older": {"accession_no","form","filing_date"},
  "newer": {...}, "items": [{"kind": "new|dropped|changed", "headline", "older_item_id", "newer_item_id",
  "passages": [{"quote","chunk_id","side"}]}]}]}` from `TEMPORAL_QUERY` -> `select_temporal` -> `PASSAGES_QUERY`
  (`retriever.py:193-288, 399-451`); `limit` capped at 50.
Tests with a fake driver returning canned rows (`tests/test_serve_dossier.py`), plus one local integration check against the real
graph that NVDA's dossier lists the same current filings as `/api/evidence` reports for its chunks.

## 5. Security design

- Upload gate (in order, before any write): Turnstile (fail closed, 503 when unconfigured, no bypass route, no env override);
  per-IP windows (`RateLimiter` instances on `app.state`: `workspace_create_limiter`, `upload_limiter`); streamed size cap
  (read the body in 1 MiB pieces, abort at 15 MiB + 1); magic-byte sniff (extension is never trusted); PDF active-content and
  encryption byte scan; DOCX zip member / ratio / path checks; page count from the parser, rejected above 30 BEFORE chunking, and
  the version's token count, rejected above 16,000 BEFORE embedding; quotas
  (`repo.quota`); global daily upload counter. Bytes live only in memory and in the parse subprocess's stdin; never on disk, never
  in Neo4j, never in logs (log lines carry `ws_hash`, size, kind, code).
- Parse sandbox: separate process (`sys.executable -m semigraph.uploads.parse_worker`), stdin/stdout only, `RLIMIT_AS` 1 GiB (with `MALLOC_ARENA_MAX=2`) and
  `RLIMIT_CPU` 120 s on Linux, 90 s wall then `kill()`, output capped at 4 MiB, JSON parsed with `json.loads` only; a crash or timeout
  is `failed` with `code=parse_failed|timeout` and zero writes (the transaction opens only after `comparing`).
- Prompt injection: uploaded text appears only inside the workspace template's delimited block; the model output is buffered,
  `strip_links_images` runs before release; `suspicious` flag = heuristic (`ignore previous`, `system prompt`, `you are`, role
  markers, `<<<`, base64 runs) shown as a banner, never blocking; the agent is unreachable (D7 at the guard + `safe_id` narrowing +
  no tool touches `User*`); `verify_answer` still enforces `citations_retrieved` (a fake `[doc:...]` in the upload is not in
  `valid_ids`), `numbers_grounded` and pseudo-citations.
- Isolation / leak harness (`tests/integration/test_workspace_leak.py`, real local Neo4j, skipped without `NEO4J_URI`): two
  workspaces with distinct documents plus the public graph; asserts (a) an ask in W1 never returns a W2 `doc:` id or text, (b) the
  public ask (no workspace) issues no `User*` query (driver spy on statement text) and its `valid_ids` contain no `doc:` id,
  (c) `GET /api/workspace/W1/evidence/<W2 id>` is 404, `GET /api/evidence/<doc id>` is 404, (d) `/api/stats` node counts show no
  `User*` label and the relationship count equals the count before both workspaces existed, (e) the SEC vector `SEARCH` result set is
  identical before and after the uploads, (f) `DELETE` W1 leaves W2 intact and W1 unreachable, (g) `sweep_expired` after clock skew
  deletes only expired workspaces. The unit-level twin (`tests/test_serve_upload_repo.py`) runs in CI on every query string.
- Tokens: 128-bit `secrets.token_urlsafe(16)`, sha256 stored, `hmac.compare_digest`; a bad token and an unknown workspace are
  indistinguishable (404, same timing path: look up by id, compare against a dummy hash when absent).
- TTL: `expires_at = created_at + 24 h` (`WORKSPACE_TTL_HOURS`), sweeper thread every 15 min (`repo.sweep_expired`, idempotent,
  `WHERE w.expires_at < $now`, batches), plus `DELETE`. Extending the TTL is not offered (M5 may add it).
- Quotas and abuse: 3 docs x 5 versions, 120 pages and 48,000 embedded tokens per workspace; 15 MiB, 30 pages, 16,000 tokens,
  120 chunks per version; 40 uploads per day globally; one upload at a time on the machine
  (`upload_slots`); embedding never takes an `answer_slots` permit; `MAX_UPLOADS_PER_DAY`; kill switch also stops uploads
  (checked in the upload route); `UPLOADS_ENABLED=false` returns 503 on every workspace route (soft rollback).
- Dependencies: `pip-audit -r deploy/requirements-serve.txt` clean (or each finding recorded with a reason); no AGPL package in the
  resolved closure (a test parses the `.txt` and fails on `pymupdf`, `PyMuPDF`, `fitz`).

## 6. STEP 0: shared surfaces, edited serially by the main session before any fan-out

Commit each as its own `feat(m4)/refactor(m4)` step; all tests green after each.
1. `tests/data/examples_citations_pre_m4.json` (generated from the current ids.py) and `tests/data/answer_events_pre_m4.json`
   (recorded from a scripted `stream_answer_for_context` run); commit them BEFORE touching the grammar or the writer.
2. `retrieval/ids.py` (+ `tests/test_ids.py`), `retrieval/answerer.py` (`_EXCERPT_ID_LINE`, `stream_answer_for_prompt`),
   `agent/sanitize.py` (`safe_id`, + `tests/test_agent_sanitize.py`), `serve/static/index.html:95-99` (+ `tests/test_static_ui.py`).
   Assert `template_fingerprint() == "4d0a62f5a0"` still.
3. `graph/schema.py` (`PRIVATE_LABEL_PREFIXES`, `reset_graph` filter, `user_chunk_embedding` excluded from `REBUILD_INDEXES`),
   both `schema.cypher` copies (a test asserts they are byte-identical), `serve/main.py` (`graph_stats` label + relationship
   filters; lifespan hooks `monitor.start_if_enabled/stop`, `jobs.start_sweeper/stop`; include the three new routers),
   `serve/store.py` (`SvcUpload` day counter, `cache_key` unchanged; `put_answer/get_answer` never called with a workspace).
4. `config.py`: `freshness_enabled=False, freshness_poll_hours=6, freshness_boot_delay_s=300, uploads_enabled=False,
   workspace_ttl_hours=24, upload_max_bytes=15 MiB, upload_max_pages=30, upload_max_tokens=16000, upload_max_chunk_tokens=512,
   upload_max_documents=3, upload_max_versions=5, upload_max_workspace_pages=120, upload_max_workspace_tokens=48000,
   upload_max_chunks=120, upload_parse_timeout_s=90, upload_embed_timeout_s=1200, max_uploads_per_day=40,
   workspace_create_per_day=3, uploads_per_hour=10`; `.env.example` gets `SEC_USER_AGENT` and the flags.
5. `ingestion/__init__.py` lazy (PEP 562) + `tests/test_serve_monitor_isolation.py` skeleton (the import assertion only).
6. `deploy/requirements-serve.in` (+ python-multipart, pdfplumber, pypdfium2, python-docx, puremagic, rapidfuzz, scipy), recompile
   `deploy/requirements-serve.txt` with `uv pip compile`, `pyproject.toml` `serve` extra gets the same names, `uv lock`; rebuild the
   local serve-shipped venv (`.venv-serve`, short path, per the MAX_PATH note) from the new `.txt`.
7. `serve/routes.py` seam: `AskRequest.workspace_id/as_of`, the gate order of 4.4, cache bypass, `evidence()` 404 for `doc`,
   `_stream_fn` lazy-imports `semigraph.retrieval.workspace.stream_workspace_answer` when a workspace is set, `/api/stats` fields;
   `serve/guard.py`: `validate_strategy(..., workspace=bool)` rejects agent; `MSG_*` constants for upload errors.
8. Stub modules with the documented signatures so workers start on a green tree: `serve/monitor.py`, `serve/monitor_routes.py`,
   `serve/dossier_routes.py`, `serve/workspace_routes.py`, `uploads/__init__.py`, `retrieval/workspace.py` (each `router = APIRouter()`
   / `NotImplementedError` bodies), `scripts/push_fly_secrets.py` gains `SEC_USER_AGENT` in `FLY_KEYS["semigraph"]`.
9. `.github/workflows/tests.yml`: no glob change needed if every new test is `tests/test_serve_*.py`; add `tests/test_retrieval_workspace.py`
   (matches the existing `test_retrieval_*` glob). The `unit` job installs the new packages through `uv sync --extra serve`.

## 7. Work breakdown: three Sonnet workers on disjoint files (after Step 0; at most 3 at once; `model: sonnet`)

**Worker A: text pipeline (no Neo4j, no HTTP).** Owns `src/semigraph/uploads/{gate,parse,parse_worker,units,changes}.py`,
`tests/fixtures/uploads/` (text PDF 3 pages generated with pypdfium2/reportlab-free means or checked in; PDF with `/JavaScript`;
encrypted PDF; `python.exe`-style bytes renamed `.pdf`; DOCX with a table and a bold-run heading; zip-bomb DOCX built in the test;
MD v1/v2 with a known edit set: 1 added, 1 removed, 2 changed, 1 tense-only edit; HTML sample), `tests/test_serve_upload_gate.py`,
`tests/test_serve_upload_parse.py`, `tests/test_serve_upload_units.py`, `tests/test_serve_upload_changes.py`.
Acceptance: every security fixture -> the named `GateError` code with zero side effects; the parse worker is killed at the timeout
(a fixture worker that sleeps); on Linux the 30-page fixture parses successfully UNDER the real `RLIMIT_AS` (1 GiB) and the child's
`ru_maxrss` is printed and < 300 MB; `chunk_units` never emits a chunk above `max_tokens` (property test with a dense numeric
table fixture that exceeds 512 tokens inside 1,800 chars); the DOCX table
text appears in `blocks`; the bold-run heading is a heading; the running header/footer filter removes a line repeated on every page;
chunks are exact slices (`text[start:end]`); the MD v1 -> v2 report equals the known edit set with the tense-only edit absent and
every quote contained in its cited chunk; identical content -> `not_compared_reason="identical_content"`.

**Worker B: persistence, monitor, dossier.** Owns `src/semigraph/uploads/{repo,versions}.py`, `src/semigraph/serve/monitor.py`,
`src/semigraph/serve/monitor_routes.py`, `src/semigraph/retrieval/dossier.py`, `src/semigraph/serve/dossier_routes.py`,
`.github/workflows/freshness-heartbeat.yml`, `scripts/freshness_parity.py`, `tests/test_serve_upload_repo.py`,
`tests/test_serve_monitor.py`, `tests/test_serve_monitor_isolation.py` (fills the skeleton), `tests/test_serve_dossier.py`,
`tests/integration/test_workspace_leak.py`, `tests/integration/test_workspace_repo.py`.
Acceptance: every `User*` query binds `$ws` (static test); `put_version` is one transaction and flips currency; `search_chunks` uses
the filtered `SEARCH` and returns nothing for another workspace (integration); `check_once` with a recorded submissions JSON and a
recorded FR count reproduces a fixed pending list; the lease admits one holder and re-admits the same holder; `stop()` returns within
5 s while a check is sleeping; `/api/freshness` shape and `stale` computation; admin route 401/409/503 paths; heartbeat YAML lints;
`scripts/freshness_parity.py --as-of 2026-09-24` output recorded (G6); dossier and risk-changes JSON shapes from canned rows,
404 for unknown tickers, no embedder attribute touched (a fake embedder that raises on any call).

**Worker C: jobs, workspace answering, routes, UI.** Owns `src/semigraph/uploads/jobs.py`, `src/semigraph/retrieval/workspace.py`,
`src/semigraph/artifacts/prompts/answer_workspace.txt`, `src/semigraph/serve/workspace_routes.py`, `src/semigraph/serve/static/index.html`
(panel and JS only; the Step 0 regex lines are already in), `tests/ui/workspace.mjs`, `tests/test_serve_workspace_routes.py`,
`tests/test_serve_workspace_ask.py`, `tests/test_retrieval_workspace.py`, `tests/test_serve_upload_jobs.py`, `scripts/workspace_smoke.py`.
Acceptance: route contracts of 4.4 with fakes (`tests/test_serve_api.py` fixture style); Turnstile unconfigured -> 503, failed -> 403;
quotas -> 429 with codes; SSE event sequence for a scripted job; `strategy=agent` + workspace -> 400 before Turnstile (the
`install_agent_stream_that_must_not_run` pattern); a workspace ask never calls `store.get_answer/put_answer` (spy); `retrieval` and
`done` shapes; `strip_links_images` keeps `[doc:...]` and `[fr:...]`; an injected instruction in a fixture chunk is not obeyed by a
scripted model and `suspicious=true`; `template_fingerprint()` still `4d0a62f5a0` after the template file is added; UI pure functions
(`docChipLabel`, `staleClass`, `jobStateText`, `versionTimeline`) tested without a DOM; the single-inline-script invariant holds;
`scripts/workspace_smoke.py --max-usd 0.50` runs the local end-to-end (create, upload v1, ask, upload v2, ask, changes, evidence,
delete) against the local graph on the serve-shipped venv and writes `artifacts/workspace_smoke.json`.

Integration (main session, after all three): wire real modules through the stubs, run the full suite on both venvs, run the leak
harness and the smoke script, then hand the diff to the verifier (`model: opus`).

## 8. Spikes (each < $0.05 and < 1 h; results go into `artifacts/m4_spikes.json` and a line in this file's section 12)

| Id | Question | Method | Owner | When |
|---|---|---|---|---|
| S1 | ONNX passage throughput and RSS delta on the shipped backend | DONE 2026-09-29 (section 13): ~100 tokens/s + ~0.7 s per call on one thread; arena +616 MB flat to ~1k tokens | main |
| S1c | Concurrent query + passage embedding on ONE session | DONE 2026-09-29 (section 13): live query-embed latency unchanged, +390 MB peak | main |
| S2 | Filtered `SEARCH` on `user_chunk_embedding` works on Community 2026.07.1 | Create the index on the local graph, insert 3 `UserChunk` nodes in 2 workspaces, run the six-property filtered query | Worker B, first task |
| S3 | Linux parse RSS and slim-image import | The parse test prints `ru_maxrss`; the CI `serve-shipped` job log gives the Linux number (no Docker locally); also `python -c "import pypdfium2, pdfplumber, docx, puremagic, rapidfuzz, scipy.optimize"` in that job | Worker A (test), main (reads CI) |
| S4 | `align` + `compute_passages` on non-SEC text | MD v1/v2 fixture through `changes.compare_versions`; confirms no alignment change is needed | Worker A |
| S5 | Serve RSS on Fly today and after the image gains scipy/rapidfuzz | Today DONE (section 13: uvicorn VmRSS 916 MB, MemAvailable 1.41 GB of 2.02 GB, swap unused). After deploy: `VmHWM` of the uvicorn PID from `/proc/<pid>/status` (the guest is cgroup v1; `memory.current` does not exist) | main, at deploy |
| S6 | Monitor parity with the lake | `scripts/freshness_parity.py --as-of 2026-09-24`: symmetric difference of pending sets and FR counts | Worker B |
| S7 | SEC reachability from Fly | After deploy: admin `Check now` must reach `data.sec.gov` and `federalregister.gov` (no egress block) | main, at deploy |

## 9. Pre-registered M4 ship gate (fixed now, before the first line of M4 code; a failed item is never marked passed)

| Id | Item | Pass condition | Owner needed |
|---|---|---|---|
| G1 | Functional, local end-to-end on the serve-shipped venv against the local graph | `scripts/workspace_smoke.py` green: v1 answer cites a `doc:` id from v1; identical re-upload -> `unchanged`; v2 flips v1 chunks `is_current=false`, evidence says `superseded`, `stale_citations` names the v1 id on a follow-up ask; `as_of` before v2 returns v1; `changes` equals the MD edit set; DELETE then every route 404 | no |
| G2 | Security / leak | `tests/integration/test_workspace_leak.py` (a)-(g) green; every gate fixture rejected with zero writes; parse killed at timeout; injection fixture not obeyed, no link or image in the answer; Turnstile fail-closed proven (503 unconfigured in production settings, 403 bad token); `curl` without a token gets 404 on every workspace route | no |
| G3 | SEC path unchanged | `git diff 3f19aac..HEAD --stat` (3f19aac = last pre-M4 commit on `v2`) shows NO change under `graph/alignment.py`, `graph/passages.py`, `graph/align_text.py`, `graph/freshness.py`, `versions.py`, `artifacts/prompts/answer.txt`, `retrieval/context_layout.py`, `retrieval/retriever.py`; `template_fingerprint() == "4d0a62f5a0"`; the examples byte-identical test and the pre-refactor event recording test green; boot log seeds 53 examples; the M3 T0 suite green (`tests/test_agent_*.py`); `ACTIVE_RISKS_QUERY` and every retriever query untouched | no |
| G4 | Performance / memory (revision 2) | On the deployed `shared-cpu-2x` / 4 GB machine, a max-size version (a PDF of <= 30 pages whose text is 15,000-16,000 tokens) reaches `ready` in <= 300 s wall (S1 projects ~190 s of one core; 300 s stays inside the 500 s burst window). Recorded with it: the job's `tokens_per_s`, and the `/proc/stat` steal delta over the job, so a slow result is attributed to host throttling or to code. Peak memory: uvicorn `VmHWM` <= 2.4 GB plus the parse child's `ru_maxrss`, total <= 2.8 GB (70 % of 4 GB); no OOM in `flyctl logs`. 5 public asks during the upload all complete, p95 <= 16.9 s (the owner-accepted M3 figure). A run on a machine whose burst balance is exhausted is recorded as such and FAILS this gate; it is never re-run until it passes silently (risk 13 decides what the owner is asked) | no |
| G5 | Monitor: live poll (revision 3) | After deploy: admin `Check now` returns `status:"ok"` with `configured:true`, `duration_s` < 120, `pending_filings` non-empty or empty with a coherent `federal_register` block; `/api/freshness` public shape; `scripts/freshness_heartbeat.py` (the exact logic the workflow runs) exits 0 against the live URL and exits non-zero against a recorded `stale`/`error` body; `stop()` at a machine restart leaves no hung shutdown in the logs. The workflow file itself cannot fire from `v2` (GitHub runs `schedule` and first-time `workflow_dispatch` only from the default branch); it activates when `v2` lands on `master` (M6) | no |
| G6 | Monitor: parity (D2) | `scripts/freshness_parity.py --as-of 2026-09-24` on the current local graph: pending set == `semigraph freshness --as-of 2026-09-24` and FR `graph_count == stored_count`; any non-empty difference is recorded with its cause and is a FAIL unless the owner waives it (an amendment that the lake holds but the graph never loads is the expected cause; see section 12) | waiver only |
| G7 | CI | `unit`, `deepeval-wrapper`, `serve-shipped` and `security-scan` jobs green on the branch; the serve-shipped job log shows the new `tests/test_serve_upload_*.py`, `test_serve_monitor*.py`, `test_serve_workspace_*.py`, `test_serve_dossier.py`, `test_retrieval_workspace.py` files collected (grep the `-q` summary counts before/after); pip-audit clean or every finding listed with a reason; no AGPL in the closure; `node tests/ui/*.mjs` green | no |
| G8 | Paid budget | Total M4 spend <= $2.00 (smoke script `--max-usd 0.50` x at most 2 runs + owner click-through + G4 asks); every paid step capped; spend read from the ledger (`ops.ps1 status`) and recorded | no |
| G9 | Production checks not behind Turnstile | `/healthz` 200; `/api/stats` snapshot id unchanged, `uploads_enabled:true`, `freshness.status` present; `/api/examples` lists exactly the ids it listed before the deploy (51 on 2026-09-29); the machine reports 2 vCPUs (`nproc`) and ~4 GB (`MemTotal`); `/api/freshness` 200; `/api/company/NVDA/dossier` and `/risk-changes` 200 with non-empty data; `GET /api/evidence/doc:000000000000:v1:0001` 404; `POST /api/workspace` without a token 403; `POST /api/ask` with `strategy=agent` + `workspace_id` 400 | no |
| G10 | Owner click-through in the browser | Create a workspace, upload a PDF, ask a question that is answered with a `doc:` chip, upload v2, see the stale chip and the what-changed view, delete; the owner confirms in writing | yes |

Reported, not gated: heading-detection precision on the owner's own sample document (recall matters more; residual noise is
absorbed by `not_compared_reason`); DOCX heading precision; workspace answer latency.

## 10. Deploy, live verification, rollback (API app only; the DB app is never redeployed for M4)

1. Everything in section 9 except G4, G5, G9, G10 green locally and in CI; verifier (opus) report attached; branch pushed.
2. `python -m scripts.push_fly_secrets --only SEC_USER_AGENT --env .env` (the key is in `.env`, not `.env.fly`; the value goes
   over stdin and is never printed; restarts the API machine once; verify `/healthz`).
3. `fly.toml [env]`: `FRESHNESS_ENABLED = "true"`, `UPLOADS_ENABLED = "true"`; `[[vm]]`: `size = "shared-cpu-2x"`,
   `memory = "4gb"` (owner decision, section 13; committed with the deploy).
4. S5 baseline: `flyctl ssh console -a semigraph -C "sh -c 'grep -E \"VmRSS|VmHWM\" /proc/<uvicorn pid>/status; head -1 /proc/stat; grep MemAvailable /proc/meminfo'"`.
5. `flyctl deploy --ha=false --remote-only --yes` from the repo root. Boot applies the new DDL idempotently (`apply_schema`), creates
   `user_chunk_embedding` on the live 1 GB Neo4j (empty label: negligible memory), seeds the same 53 examples.
6. G9, then S7/G5 (`curl -X POST -H "X-Admin-Token: ..." https://semigraph.fly.dev/api/admin/freshness/check`), then G4 (S5 after),
   then G10 by the owner. Enable the heartbeat workflow (a manual `workflow_dispatch` run first).
7. Rollback: `flyctl deploy --image registry.fly.io/semigraph:<previous tag>` (tags from `flyctl releases -a semigraph`); the older
   image ignores the new labels and indexes (harmless, `Svc*`/`User*` filtered or absent). Soft rollback without a redeploy:
   `UPLOADS_ENABLED=false` / `FRESHNESS_ENABLED=false` in `fly.toml` (one deploy, no code change) or the kill switch (also stops
   uploads). VM rollback: `[[vm]]` back to `shared-cpu-1x` / `2gb` only together with `UPLOADS_ENABLED=false` (the caps assume
   2 vCPUs / 4 GB). Data rollback: `MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'User') DETACH DELETE n` via `cypher-shell`.
8. README / page copy: strengths only ("upload your own documents and ask questions that cite them; versions and what-changed;
   live freshness check against EDGAR"); limits (no OCR, 24 h retention, 30 pages / 16,000 tokens per version) live in docs/v2 and the RUNBOOK.

## 11. Cleanup (before the "done" report)

- Delete the research leftovers: `C:\Users\amit1\AppData\Local\Temp\m4bench\` and the session scratchpad `m4research\` folder (nothing in the repo references them).
- Spike scripts either become `scripts/*.py` with a test (`freshness_parity.py`, `workspace_smoke.py`) or are deleted; their results
  live in `artifacts/m4_spikes.json`, `artifacts/freshness_parity.json`, `artifacts/workspace_smoke.json` (no bytes of any upload).
- One-line pointers: top of `docs/v2/M4_UPLOAD_PLAN.md` ("superseded by M4_PLAN.md") and PLAN.md section 5 ("superseded by
  docs/v2/M4_PLAN.md; the `U:{ws}` citation form, the Docling app and RQ/Valkey/R2 are obsolete") - main session, not this planner.
- RUNBOOK: new sections "Freshness monitor" (what `pending` means, `Check now`, heartbeat, SEC_USER_AGENT secret, why it never
  promotes), "Uploads" (limits, TTL, `UPLOADS_ENABLED`, the leak harness), and a line under "Updating the graph": a dump swap
  deletes every live workspace and the ledger (24 h TTL makes this acceptable; avoid during a demo).
- `.env.example`: `SEC_USER_AGENT`, `FRESHNESS_ENABLED`, `UPLOADS_ENABLED`, `TURNSTILE_REQUIRED`.
- README: strengths-only paragraph for uploads, freshness and dossier data; the evaluation bullet unchanged.
- Remove the Step 0 stubs' `NotImplementedError` bodies (grep), the `tests/data/*_pre_m4.json` files stay (they are the pins).
- `git status` clean; `rtk gain` not needed; the local `.venv-serve` stays (documented in the RUNBOOK "Local development").

## 12. Risks and where the obvious implementation is wrong

1. **scipy in the serve image.** `alignment._assign_pairs` needs `linear_sum_assignment`; scipy's import is ~30-60 MB RSS, which is
   immaterial on the 4 GB machine (G4 budget 2.8 GB). `graph/alignment.py` stays byte-identical (G3); no lazy-import fallback, no
   vendored Hungarian solver (revision 2: the planner's Q5 is moot).
2. **Parity (G6) can fail for a legitimate reason.** `pending_filings` compares against the lake manifest, which holds unparsed
   amendments the graph never loads as `Filing` nodes ("an unparsed amendment is inert", PLAN.md section 4). Run S6 first; if the
   only differences are such amendments, the monitor's "known" set should include `Filing.superseded_by` / an `inert_accessions`
   list stamped on the `Snapshot` by `build-graph`... which is an offline change. Cheaper: the monitor reports those accessions
   under `pending_filings[].note = "amendment not parsed"` and G6 compares the sets minus that note, recorded as such. Owner decides.
3. **The obvious `Document.paragraphs` loop loses tables; the obvious `get_fontsize` is wrong** (both verified live). Worker A must
   use `iter_inner_content()` and `FPDFText_GetMatrix`.
4. **Heading precision is low (0.02-0.22).** Without the header/footer filter and the short-line rule the "what changed" report is
   noise; even with them, expect over-segmentation on dense documents; `not_compared_reason=heading_coverage_mismatch` guards the
   comparison, and the paragraph fallback guards retrieval.
5. **Streaming a workspace answer live would leak a link before it is stripped**: hence `force_buffered=True`. This costs
   first-token latency on workspace asks only (SEC asks unchanged).
6. **`graph_stats` is a boot-time snapshot** (`main.py:54-76`): the leak harness must assert on the query filter, not only on the
   number shown, or a leak hides until the next restart.
7. **Turnstile in production: secrets present, widget not proven.** `flyctl secrets list -a semigraph` shows `TURNSTILE_SECRET_KEY`,
   `TURNSTILE_SITE_KEY` and `TURNSTILE_REQUIRED` (names only; values never read). Present names do not prove a working widget or a
   matching key pair; G9 (403 without a token) and G10 (owner click-through succeeds) are the proof. If the key pair is wrong,
   uploads answer 503/403 by design and the owner fixes the keys (RUNBOOK "Enabling the Turnstile bot gate"); no bypass exists.
8. **A dump swap during a demo deletes workspaces** (restore script overwrites the whole database). RUNBOOK note; no code fix in M4.
9. **Neo4j 1 GB with a second vector index**: empty at deploy; a full workspace is <= 120 chunks x 1024 floats (~0.5 MB) per version,
   bounded by the quotas; still, `memory.current` on the DB machine is read once after G4 and recorded.
10. **`python-multipart` in FastAPI** requires the package present at import of the route module (`File(...)`): the serve-shipped
    CI job proves it; the `unit` job proves nothing about the image.
11. **Two modules named `freshness`** (`ingestion.freshness`, `graph.freshness` with pandas): the isolation test pins that the
    monitor imports only the former.
12. **Windows vs Linux**: `resource` and `preexec_fn` do not exist on Windows; the parse sandbox tests must skip the RLIMIT assertions
    there and the CI serve-shipped job (ubuntu) is the proof.
13. **Shared-CPU burst balance after START (the likeliest owner question).** Fly's docs: shared vCPUs get 5 ms per 80 ms each,
    pooled per machine (`shared-cpu-2x` = 10 ms / 80 ms = 12.5 % of one core), with a per-machine burst balance that starts at 5 s
    and caps at 500 s; the docs do not say whether a redeploy or a STOP/START (scale to 0, then deploy = a new machine) resets it.
    A max-size upload spends ~165 s of balance (190 s of one core minus the 12.5 % baseline); idle accrual is ~0.11 s per s, so a
    machine needs ~25 min idle after boot to absorb one max upload at full speed. Throttled from the start, the same upload would
    take ~25 min and live asks would slow with it. Design response (no cap change): the 1,200 s job budget finishes rather than
    fails, the job shows progress and an ETA, the upload thread runs at nice 10 so live asks win contention, and G4 records the steal
    delta. Measurement decides: right after the M4 deploy (a fresh machine) a 60 s CPU-bound probe over `flyctl ssh` records when
    steal starts rising. If a fresh machine throttles within seconds, the owner is asked BEFORE G10 to choose: (a) keep
    `shared-cpu-2x` and accept slower uploads in the first ~25 min after START (documented in the RUNBOOK), (b) a
    `performance-1x` machine (dedicated core; ~$60/month in `sin`, about +$43 over today, outside the approved quote), or
    (c) smaller caps. The cap is never shrunk without that answer.

## 13. Spike results, owner decisions, planner questions (revision 2, 2026-09-29)

Spike numbers (local desktop CPU, production settings `ONNX_THREADS=1`, the shipped q8 model; scripts kept in the session scratchpad,
numbers copied to `artifacts/m4_spikes.json` in Step 0):

| Spike | Result |
|---|---|
| S5 (production, read-only `flyctl ssh`) | uvicorn VmRSS 915,772 kB; MemAvailable 1,405,288 kB of 2,015,876 kB; swap 0 used; 1 vCPU; cgroup v1 (no `memory.current`); `/proc/stat` exposes steal (4,179 jiffies since the 2026-09-27 boot) |
| S1 (40 real lake chunks, median 270 tokens) | median 2.96 s, p90 9.7 s, max 15.4 s per passage; 93 tokens/s |
| S1b (fresh process per length) | 129 tok 1.95 s; 257 tok 2.86 s; 513 tok 4.87 s; 1,025 tok 10.13 s; 1,537 tok 17.23 s. ~100 tokens/s + ~0.7 s per call. Peak over the loaded model +616 MB (first-inference arena) flat up to ~1k tokens, +950 MB at 1,537 |
| S1c (one session, a 500-token passage loop in a background thread, a query every 1.5 s) | query-embed latency 1.23 s alone vs 1.28 s during the upload loop (max 1.28 s, n=11); passage 4.76 s (~105 tokens/s); peak working set +390 MB over the warm process |

Consequences: time is linear in total tokens (~190 s of one core for 16,000 tokens in ~55 chunks); chunks are capped at 512 tokens
(memory flat); one embedding job at a time, in-process, on the shared session (a subprocess would load a second ~1 GB model);
with 2 vCPUs a live ask's query embedding runs on the other core unaffected (S1c), which is what the bigger machine buys.

Owner decisions (AskUserQuestion, 2026-09-29):
- Upload embedding: **"Bigger machine"**: embedding stays private and local (same ONNX model in the API process); the API VM grows
  to about 2 vCPU / 4 GB; caps about 2x the single-core option (~15 pages / ~8k tokens -> 30 pages / 16,000 tokens). Quoted
  +$10-20/month while online. Priced for the real region: `sin` carries a 1.269 regional markup (docs.fly.io/about/pricing,
  2026-09-29): `shared-cpu-1x` 2 GB ~$17.54 -> `shared-cpu-2x` 4 GB ~$35.09 per 30 days online, **+$17.55**, inside the quote.
  STOP (scale to 0) still brings it to ~$0 compute.
- M3: both failed gates accepted on record (commit 3f19aac, `M3_AGENT_PLAN.md` section 11). Not an M4 precedent: every M4 gate
  that fails goes to the owner before any waiver.

Planner questions, resolved from evidence (none needed the owner):
- Q1 `SEC_USER_AGENT`: a Fly SECRET pushed from `.env.fly` (D4); the owner's email never enters the public repo.
- Q2 heartbeat: read-only public GET, no `ADMIN_TOKEN` in GitHub; unreachable = warning, not failure (section 4.1).
- Q3 parity G6: stays strict (exact equality). No exclusion is pre-registered; if S6 shows differences, they are recorded with
  their cause and the owner is asked (risk 2).
- Q4 Turnstile: the three secret names are deployed; a working widget is proven only by G9 / G10 (risk 7).
- Q5 scipy fallback: moot at 4 GB; `graph/alignment.py` stays untouched (risk 1).

## 14. Revision 3: contracts fixed at the end of Step 0 (2026-09-29, before any worker starts)

Step 0 landed as commits bde5b68..180df86 (pins, `doc:` grammar + `stream_answer_for_prompt`, `User*` schema + private-label
filters + `reserve_daily_upload`, settings, lazy `semigraph.ingestion`, serve dependencies, the `/api/ask` seam, stubs and
lifespan wiring) plus `Embedder.count_tokens`. Where the text above differs, this section and the stub modules win.

1. **Token counting:** `Embedder.count_tokens(text)` (ONNX and local backends; the remote backend cannot count and uploads refuse
   to start on it) counts in <= 2,000-character pieces, so the ONNX tokenizer's 8,192-token truncation can never cap a
   whole-version count. `units.chunk_units(..., count_tokens=embedder.count_tokens)` in production.
2. **Parse sandbox limits:** no `preexec_fn` (unsafe in a multi-threaded parent: the child can deadlock before exec).
   `parse_worker` sets `resource.setrlimit(RLIMIT_AS, 1 GiB)` and `RLIMIT_CPU` as its FIRST statements (Linux only), before
   importing any parser and before reading stdin; the parent passes `MALLOC_ARENA_MAX=2`, the wall timeout and the kill.
3. **Heartbeat:** built as `scripts/freshness_heartbeat.py` (tested) + `.github/workflows/freshness-heartbeat.yml` calling it;
   inert until the default branch has it (G5 revised above).
4. **`as_of`:** a date `D` means the workspace as it stood at the END of `D` (UTC): the cutoff is `D + 1 day` at 00:00 UTC;
   a version or chunk is visible iff `valid_from < cutoff AND valid_to >= cutoff`; current rows carry
   `valid_to = 9999-12-31T00:00:00Z`; both properties are zoned datetimes (S2 proved these filters in `SEARCH ... WHERE`).
5. **Grounding of uploaded text:** the workspace writer passes `sources` explicitly (SEC `sources_from_context` of its SEC
   context, merged with `{doc id: chunk text}`), so a figure cited to `[doc:...]` is checked against that chunk; a test pins it.
6. **Upload route order:** `UPLOADS_ENABLED` -> workspace token (404) -> kill switch -> Turnstile -> per-address upload window
   -> streamed size cap -> byte gate (sniff, active content, zip checks) -> quota -> non-blocking `upload_slots` acquire (busy:
   429 `busy`) -> `reserve_daily_upload` (full: 429 `daily_limit`, slot released) -> 202 and the job thread (which releases the
   slot on every path). Junk files never spend the daily budget.
7. **Turnstile on upload routes when unconfigured:** production 503; non-production allowed and logged (local development and
   the G1 smoke). No bypass route, no header, no env override in production.
8. **Test databases:** the leak harness and every `sweep_expired`/wipe test run ONLY on the throwaway instance (bolt 7898), which
   Worker B seeds with a tiny public fixture (two companies, a few `EvidenceSpan` nodes with embeddings); never on the real
   local graph (7699). G1's smoke runs on the real local graph: it applies the M4 DDL there (idempotent, as production boot
   will) and creates and deletes one workspace; accepted and recorded.
9. **Ownership:** workers never edit `config.py`, `routes.py`, `main.py`, `guard.py`, `store.py`, `ids.py`, `answerer.py`,
   `embeddings*.py`, the schema files, the requirements files, `pyproject.toml`/`uv.lock` or `tests/test_serve_api.py`; a
   needed setting or seam is reported back to the main session.

## 15. Revision 4: contracts changed by the Opus review of the first build (2026-09-29)

First build: commit 54f7d61 (Workers A, B, C; 5,419 + 2,972 tests green). Three independent Opus reviews (security,
correctness/contracts, reliability; reports in the session record) found 1 CRITICAL, 12 HIGH, 10 MEDIUM, 6 LOW (29, 21 distinct);
G3 (SEC path unchanged) was independently confirmed. G6 (parity) PASSED on 2026-09-29 with a negative control
(`artifacts/freshness_parity.json`). Contract changes (they win over sections 4, 5 and 14):

1. **`as_of`** is a date (`YYYY-MM-DD`: end of that UTC day) OR an instant with an explicit offset
   (`YYYY-MM-DDTHH:MM:SS[.f](Z|+HH:MM)`, normalized to UTC by `guard.validate_as_of`; years 2000-2100 only). An instant `T`
   shows every version created at or before `T` (cutoff = `T` + 1 microsecond, same `valid_from < cutoff AND valid_to >= cutoff`
   filter). The page offers "ask as of vN" using that version's own `created_at`.
2. **Turnstile on uploads** travels in the `X-Turnstile-Token` HEADER and is verified BEFORE the body is read.
3. **Job progress stream:** an ASYNC SSE generator (never a threadpool thread) over a per-job append-only event log in
   `JobRegistry` (fan-out: every watcher sees every event); ends on the terminal event or when the job is gone from the registry
   (then replays the persisted final state); at most 3 live watchers per job; workspace GET routes take the read-rate window.
4. **Workspace deletion is final:** `put_version`/`put_job` lock the `UserWorkspace` node first and refuse to write when it no
   longer exists (the job fails `workspace_deleted`); `delete_workspace`/the sweep lock it too; the sweeper also removes
   orphaned `User*` nodes, runs whenever a driver exists (independent of `UPLOADS_ENABLED`), and marks jobs left non-terminal by
   a dead process `failed` (`interrupted`) at start.
5. **Upload availability** = `UPLOADS_ENABLED` AND `app.state.uploads_ready` (set by `uploads.jobs` start;
   `routes.uploads_available`), used by `/api/stats`, `/api/ask` and every workspace route.
6. **What changed:** every unit the aligner marks reworded stays visible. Units with a sentence-level passage are `changed`;
   units without one go to a new `minor_rewordings` list (never folded into `unchanged_count`). Uploads use their OWN
   `PassageParams` (calibrated so a meaning reversal yields a passage and a tense-only edit does not); the SEC defaults and
   `graph/passages.py` are untouched (G3).
7. **Parse sandbox:** the child gets an ALLOWLISTED environment (no API keys, tokens or Neo4j credentials); PDFs additionally
   get a structural active-content scan INSIDE the sandbox (pdfminer walks every object, including object streams and
   `#xx`-escaped names) after the raw byte scan; the child reports the exception class of a parser failure (never its message).
8. **Caps enforced:** `upload_max_chunks` (120, job code `too_many_chunks`), the 120-page workspace cap (`workspace_quota`).
   Small units are packed into chunks up to the target size.
9. **Logs:** workspace answers log check COUNTS only; the uvicorn access log carries `<ws:hash>` instead of a workspace id,
   `<doc>` instead of a document citation and no workspace query string.
10. **Freshness:** a failed check never overwrites the last good result (status/error/`last_error_at` kept separately), is
    retried after 30 min, and the page line says the check failed instead of showing a count; `summary()` returns
    `never`/`unconfigured` shapes instead of None; `next_check_at` is computed; `SvcLease`/`SvcFreshness` keys are unique.
11. **Multipart:** at most 8 parts, parser work off the event loop, a malformed body is 400 (no-store), never 500.
12. **ReDoS-safe** `suspicious` heuristic (linear-time patterns, proven on 2M whitespace characters); `strip_links_images`
    also removes reference-style links, autolinks, HTML tags and data URIs.
13. **G1 smoke** asserts the currency flip, superseded evidence, a stale citation (via `as_of` = v1's instant) and 404 on every
    workspace route after DELETE.

## 16. Revision 5: the second Opus review (2026-09-30)

Round 3 (07b31f3) added the G2 route-level leak harness (13 tests, both venvs, a negative control; no leak found). G1 PASSED on
the real local graph (3ab9c91, `artifacts/workspace_smoke.json`: 8 of 8 required checks, $0.004). A second Opus review re-ran
the original repro scripts: 24 of 29 first-review findings fixed, 5 partially (6, 9, 15, 27, 29), plus 14 new (1 HIGH: the
freshness loop busy-spun when a due check was not admitted, a regression of 15.10; 3 MEDIUM; 10 LOW). Contract changes:

1. **Process hardening:** the API marks itself non-dumpable (`prctl(PR_SET_DUMPABLE, 0)`, `serve/hardening.py`) first in the
   lifespan, so a compromised parser child of the same uid cannot read `/proc/<api pid>/environ` (the exec-time block that
   holds the Fly secrets). Proven on Linux (WSL Ubuntu 24.04): without it a child reads the secret, with it `DENIED`; the
   Linux-only test runs in CI.
2. **Upload availability** additionally needs the Turnstile secret in production (`routes.uploads_available`), with a boot
   ERROR when it is missing, so the page never advertises routes that fail closed.
3. **Access log:** redaction decodes uvicorn's percent-quoted path and splits the query first.
4. **Freshness loop:** a non-admitted or failing due check waits at least 60 s before retrying; the lease is released at the
   end of every check.
5. The remaining items (negation flips next to ordinary rewordings, the `without` boilerplate, a bounded negation check, the
   interrupted-job start pass and lost terminal writes, the read-rate window before authentication, the ask-as-of selection,
   sequential multi-file uploads with fresh Turnstile tokens, smoke robustness, the scoped `CALL` form) are fixed in fix
   round 4; a third Opus review verifies them before the deploy.
6. **Contract additions in fix round 4:** the change report carries `negation_check_skipped` (sentence pairs the bounded
   negation check did not examine; never silent); `repo.fail_interrupted_jobs` restores a job whose version is already
   committed to `ready` instead of failing it; the job progress page reconnects up to 3 times before reporting a lost
   connection; every workspace route, uploads included, takes the in-memory read-rate window before its database lookup.
7. **Closing verification (third Opus review, bffb40f): PASS WITH CONDITIONS** from both verifiers; the conditions are
   closed in fix round 5: the access log re-escapes what it logs (decoding only for matching, so no client-supplied newline,
   escape or quote reaches it); the negation check is charged by token work with a shared-token pre-filter, sentences over
   200 tokens are excluded and counted, and the page shows the count; "not limited to" boilerplate is not a flip; a
   graceful shutdown mid-check releases the freshness lease; interrupted-job recovery requires the committed version to
   carry the SAME job id (`UserVersion.job_id`); the progress page retries a 5xx; the smoke marks a refused ask
   INCONCLUSIVE (exit 2).
8. **The what-changed comparison runs in a sandboxed subprocess** (`uploads/compare.py`, `compare_worker.py`, the shared
   runner `uploads/sandbox.py` also used by parsing): the API process never loads the aligner or scipy (pinned), the pure-
   Python comparison no longer competes for the GIL with request threads, and it is bounded by
   `UPLOAD_COMPARE_TIMEOUT_S` (120 s) plus the child's RLIMIT_CPU; on timeout or crash the report is
   `not_compared_reason = comparison_timeout | comparison_failed` and the version still becomes ready.
9. **Closing verification of fix round 5 (Opus, 46d6921): PASS WITH CONDITIONS.** The one deploy condition and the LOWs are
   closed in fix round 6: the "not limited to" boilerplate matcher is anchored to the three fixed phrases
   (`without limitation`, `but not limited to`, `including[,] not limited to`), so a scope reversal such as "is not limited
   to" / "is limited to" is a real negation change again (round 5's broader strip hid it); the sentence pairing inside the
   negation check uses rapidfuzz `Indel.normalized_similarity` instead of the cubic `lex_exact` (the verifier's 22 x 200-token
   low-diversity construction took 45.8 s before and about 0.01 s after, with the budget still charged; pinned by a test that
   also asserts the pairing ran rather than being skipped); a spawn failure (`OSError`)
   in the comparison subprocess returns `comparison_failed` like every other failure; the access log hashes the whole
   workspace-id path segment whatever its case or length and re-escapes a kept query string; the smoke prints every failed
   check before its INCONCLUSIVE exit.
10. **Verification of fix round 6 (Opus, 0b5d0f3): PASS WITH CONDITIONS.** Items 2-5 correct; one regression found and
    fixed in round 6b: the anchored matcher missed the most common SEC form, "include, but are not limited to" (51 of the
    242 "not limited to" occurrences in the local 10-K corpus were counted as negators, so dropping the phrase showed a
    false negation change). The matcher now accepts `but [are|is|was|were] not limited to`; the verifier's corpus scan
    strips 242 of 242 and its end-to-end repro is no longer reported. Also: malformed workspace paths (repeated slashes,
    any letter case, `%2F`) never log the raw id; stale `lex_exact` comments corrected (`_pair_similarity` is an upper
    bound of `lex_exact`: 24 of 6,510 real 10-K sentence pairings newly cross the 0.5 floor, none drop below). Known,
    documented limits: a "but not limited to" that is itself a scope statement is stripped too; rare forms ("and not
    limited to", "such as, not limited to") still count as a negator (none occur in the local corpus).
11. **Post-G10 follow-ups (2026-09-30):** fix 2 (owner's live G10 test) — every chunk-level read (evidence, search,
    current and as-of, and chunk texts) showed the LATEST uploaded file name for every version, because `put_version`
    always overwrote `UserDocument.title`, never a per-version one. Each `UserVersion` now stores its own title
    (`put_version`'s existing `title` argument), and the four reads return `coalesce(v.title, d.title)`, scoped by
    `workspace_id`, while `UserDocument.title` and the document list keep showing the latest version's title unchanged.
    A version written with no title stores the document's title as it was at upload time, if it had one (`coalesce($title, d.title)`; an untitled first version falls back to the current document title).
    Fix 1: the page's evidence cache dropped nothing when a new version arrived, so a chip opened before the upload
    kept showing its old payload (no "superseded by N"); every `doc:` entry is now dropped when a job reaches ready or
    failed, when the watcher gives up or the job is gone, and on a workspace switch, and a `doc:` fetch still in flight across any of
    these is shown but never cached (a generation counter). Fix 3: the "Deep research" hint showed whenever the agent
    was enabled, even for hybrid answers; it now shows only while "agent" is the selected strategy, re-synced on every
    change of the select and when a benchmark example forces hybrid. Opus verification: A (fixes) PASS, B (b4c7098,
    the scoped 01N52 notification filter) PASS; its LOWs were fixed before the deploy, with tests that fail first. A second Opus pass over those fixes
    (PASS WITH CONDITIONS) led to the README negation wording, the job-gone drop, and the drawer showing only the
    most recently clicked chip when two evidence fetches overlap.

## Audit trail

- 2026-09-29: plan written by fable-architect from the code map and the live-web research report (both read-only). No code changed.
- 2026-09-29 (revision 2, main session): S1 / S1b / S1c / S5 folded in; owner's "Bigger machine" decision (shared-cpu-2x / 4 GB,
  +$17.55 per 30 days online in `sin`); caps re-registered to 30 pages / 16,000 tokens per version, 512-token chunks, 48,000
  tokens per workspace, 40 uploads per day, 1,200 s embed budget; G3 diff base pinned to 3f19aac; G4 rewritten (300 s, 2.8 GB,
  steal recorded); G9 compares `/api/examples` ids; RLIMIT_AS 1 GiB proven under the limit on Linux; monitor checks at boot
  when stale; heartbeat tolerates the intentional scale-to-zero; risk 13 (burst balance) added. Still no code changed.
