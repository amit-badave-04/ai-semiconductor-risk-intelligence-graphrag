**Buyer-demo features, comparables, and demo plan for semigraph (2026-09-25).** [V] = seen live today, [U] = unverified or conflicting.

## 1. Ranked features

| # | Feature | Demo impact | Effort | Risk | Standard component | Milestone |
|---|---|---|---|---|---|---|
| 1 | Cited Q&A agent, visible steps, click-through to the source passage | H | M | M (latency, spend) | LangGraph 1.2.12 [V], existing `/api/evidence` | M1 |
| 2 | Freshness dashboard (as-of per source, newer filings and rules detected, two staleness types) | H | S-M | L | EDGAR submissions API, Federal Register API, per-span `as_of` | M1 |
| 3 | Eval-transparency and Limits page | H (trust) | S | L | existing `artifacts/eval_*.json` | M1 |
| 4 | API with OpenAPI docs and hashed keys | M | S | L | FastAPI native | M1 |
| 5 | Auth, roles, audit log, quotas | M (enabler) | M | M | see section 5 | M1 |
| 6 | Risk-change delta ("what changed since last filing") | H | M | M (false churn) | existing `temporal.py`, `difflib` redline, Cypher, no LLM at read time | M2 |
| 7 | Company risk dossier page | H | M | L | precomputed JSON, static Next.js | M2 |
| 8 | Export to Markdown/PDF with footnoted citations | M-H | S | L | print CSS; WeasyPrint 70.0 [V, PyPI] if needed | M2 |
| 9 | Conversation history and follow-ups | M | S-M | L | LangGraph state, `Conversation` nodes in Neo4j | M2 |
| 10 | Document upload plus updated-version diff | H | L | H (injection, tenant leak, parser cost) | Docling (MIT, pushed today [V]) or pypdf | M3 |
| 11 | MCP server, read-only tools on the public corpus | M-H | S-M | M | FastMCP 4.0.9 (2026-09-24), mcp 2.2.0 (2026-09-07) [V, PyPI JSON] | M3 |
| 12 | Watchlist plus email/webhook alerts | M-H | M | M | EDGAR/FR polling plus Resend | M4 |
| 13 | Admin console (ledger cost per answer, eval scores, Langfuse link) | M | M | L | Langfuse | M4 |
| 14 | Supply-chain map with chokepoint metric | M | M | M (looks toy-sized) | Cytoscape.js, precomputed | M4, optional |

- **Milestones:** they are my proposal. M1 is foundation and trust, M2 is analysis views, M3 is the differentiators, M4 is monitoring and polish.
- **Supply-chain map:** the graph has only 26 entities. Use "N filers name this supplier" as the chokepoint metric. GDS 2026.07 matches Neo4j 2026.07 [V], but on 26 nodes it is theater. If you use GDS, upgrade it together with the database.
- **Model routing and escalation:** show this in the admin console only if it is actually implemented.

## 2. What comparables offer

- **AlphaSense [V]:**
  - Cited generative search, Deep Research, Work Products (decks and tables), Office add-ins, enterprise shared-drive uploads, alerts, and Blackline "Show Changes" between filings.
  - SOC 2 Type 2, ISO 27001:2022, BYOK/BYOB. It claims "98% citation accuracy"; I saw no methodology.
  - MCP is [U]: the developer homepage lists "MCP tools", but a search summary says the MCP sample is local and not hosted, and that page returned 404 when I fetched it.
- **S&P Capital IQ Pro [V]:** ChatIQ with click-through citations, Document Intelligence, and an MCP-enabled architecture (March 2026). Client proprietary content in Document Intelligence v3 was a June 2026 roadmap item.
- **Hebbia [V]:** Matrix tables, uploads, connectors, SOC 2 Type 2, ISO 27001, ISO 42001, dedicated tenant, audit visibility.
- **Rogo [V]:** agents that produce Excel, memos and decks; SOC 2, ISO 27001.
- **Sayari [V]:** Graph, Signal, Guide, Pilot, and an API with change-monitoring notifications. It cites sources for findings. MCP is [U]: the API page does not mention it.
- **Resilinc, Interos, Everstream [V]:** event alerts, multi-tier supplier mapping, risk scores, AI agents. I found no MCP announcement from any of them [U, absence].
- **FactSet Revere and Bloomberg SPLC:**
  - FactSet: direct plus reverse relationships, more than 25,000 companies and 144,000 relationships (search summary of FactSet page).
  - Bloomberg: sourced from filings and transcripts. Its coverage counts conflict (20k vs 200k) [U].

**Recurring buyer expectations:** cited answers with source drill-down, private uploads, alerts, a filing redline, Office-format exports, API/MCP, SSO/SCIM plus SOC 2, and audit trail.

**Where we can credibly match:** all of these except SSO/SCIM, SOC 2, Office add-ins and coverage breadth.

**Positioning:** do not compete on coverage; these vendors are orders of magnitude larger. Compete on provenance on every edge, lineage-level delta (a risk re-disclosed, reworded or dropped, beyond AlphaSense's Blackline text diff), and published evals with cross-judge and failure taxonomy.

## 3. Repo and live findings that shape the demo

- **Time staleness:**
  - The corpus's newest filing is 2026-06-25.
  - Today's EDGAR submissions probe, 13 filers, [V]: 10 filers have a newer 10-Q or 10-K (nine 10-Qs plus MSFT's 10-K on 2026-07-29). MU, TSM and ASML have none.
  - The SEC's submissions API updates with under 1 s delay [V]. The limit is 10 requests per second with a declared User-Agent [V].
- **Query-scope staleness:**
  - The BIS file holds 13 rules through 2025-09-16.
  - The Federal Register API needs no key [V]. Since 2025-09-17 it returns 3 BIS rules matching "semiconductor", 2 for "advanced computing", 17 for "export controls" and 14 for "entity list" [V].
  - The ingestion's combined OR query returned only 1 for the same window. The cause is unverified, so spike it.
  - The newest hit is a polysilicon temporary final rule dated 2026-09-24, which matches "semiconductor".
- **Amendments:** the code has no supersession logic. `fetch_annual_risks` filters `form IN ('10-K','20-F')`, so AMD's 10-K/A of 2026-02-04 is outside the lineages. Whether its text sits in evidence spans needs a DB check. It is a natural "updated version" hook.
- **Delta caveats:** greedy clustering at cosine 0.75 can split a reworded risk into a false "dropped plus new" pair. Closure uses annual filings only, and ASML's 2023 and 2024 20-Fs are skipped. Show both texts plus the similarity score, and script the demo on NVDA or AMD.
- **Neo4j design gates:**
  - `SEARCH ... WHERE` filters in-index (2026.01+). It allows AND and `IN` (2026.06+), with no OR or NOT. Filter properties live on the searched node [V, docs summary].
  - Denormalize `is_current`, `superseded_at`, `version_id`, `workspace_id`, `cik` and `filing_date` onto `EvidenceSpan`. Isolation then becomes `e.workspace_id IN [$ws,'public']`.
  - Full-text in `SEARCH` needs 2026.09 [V]. You deploy 2026.07, so use `db.index.fulltext.queryNodes` for BM25. Full-text availability in Community is [U].
  - Community has no multi-database, RBAC, online backup or clustering [V] and is GPLv3 [V], so isolation is enforced in the app.

## 4. Honest disclaimers (exact text)

1. "Informational research tool. Not investment, legal or compliance advice."
2. "AI-generated answers from a fixed snapshot of public filings (as of [date per source]). They can be wrong or incomplete. Verify against the cited source." (An AI-generated label is due under EU AI Act Art. 50, applicable 2026-08-02 [V, secondary sources].)
3. "Coverage: 14 SEC filers, 59 filings, 13 BIS rules through 2025-09-16 [update on refresh]. Non-SEC filers such as Samsung are not covered. Absence here is not evidence of absence."
4. "The citation check confirms a cited passage was retrieved, not that it supports the claim." Faithfulness was 0.865–0.927 on 20 questions, LLM-judged.
5. "Not SOC 2 or ISO certified. Hosted on Fly.io, which states it is SOC 2 Type II audited [V, fly.io/compliance]." Never say the product is certified.
6. "Uploads are private to your workspace and deleted on request."

**Limits page sections:**
- coverage
- freshness (detected versus ingested)
- benchmark numbers with n=20 and cross-judge deltas
- delta method limits
- LLM-extracted edges
- single machine, no high availability
- security posture
- data handling

## 5. Enterprise-readiness set

| Item | Recommendation | Effort |
|---|---|---|
| Identity | Cloudflare Access as invite gate: free up to 50 users [V, Cloudflare-domain sources]; verify `Cf-Access-Jwt-Assertion` against the `<team>.cloudflareaccess.com/cdn-cgi/access/certs` certs [V]. Anonymous visitors stay read-only behind Turnstile. Add Clerk only if open signup is wanted: Hobby has 50k MRU, no SAML or MFA on free [V]. | S |
| Roles | viewer, analyst, admin via a FastAPI dependency; admin from an email allowlist | S |
| Audit log | Append-only `AuditEvent` nodes with a hash chain. Say "tamper-evident", not "tamper-proof". Langfuse audit logs are a licensed feature [V], so do not rely on them. | S-M |
| Quotas | Extend `guard.py` to per-user keys, daily dollar ceiling, and upload size/page/day caps | S |
| Upload isolation | `workspace_id` on every node, one repository function that always applies the tenant predicate, cross-tenant leak tests, delete-my-data endpoint, content treated as untrusted (OWASP LLM01 [V]). Logical, not physical, isolation; say so. | M-L |
| Observability | Langfuse core is MIT [V]; Hobby is 50k units per month, so sample traces during load tests. Existing ledger for cost. | S-M |

**Rejected auth options:**
- Supabase free pauses after 1 week idle [V], a real hazard before a demo.
- Auth.js is now part of Better Auth [V]. Better Auth is MIT and active [V], but whether the TypeScript library fits a static export plus a Python API is [U].
- Keycloak (Apache-2.0, 26.7.4 on 2026-09-16 [V]) only if a buyer requires self-hosted SSO.

**MCP auth:**
- Claude custom connectors accept authless, `static_headers` (beta), OAuth DCR and CIMD [V]. Do not build an OAuth server. The spec (2026-07-28 current [V]) deprecates DCR in favor of CIMD.
- Serve the public corpus only, reachable from 160.79.104.0/21. Upload data never goes through an authless endpoint.

**Scale claim:** claim 1,000 concurrent readers on the cached, static and SSE path, with live generations queued and bounded. `guard.py` limiters and `answer_slots` are process-local on one machine. Prove it with a mocked-LLM load test.

## 6. Storyline (7 minutes)

1. **0:00–0:45. Trust opener.** Dossier plus freshness banner: "snapshot to 2026-06-25; 10 newer filings and N newer rules detected today."
2. **0:45–2:15. Multi-hop question.** Show agent steps, click a citation, land on the highlighted filing passage.
3. **2:15–3:45. Delta.** Show NVDA or AMD "dropped and new risks" with side-by-side text and similarity. Cite the benchmark: vector RAG scored 0% on temporal questions, hybrid 100% (Sonnet judge), with the cross-judge gap disclosed.
4. **3:45–5:30. Updated version.** Upload a revised document, or ingest MSFT's 2026-07-29 10-K. The old chunks turn stale and the new ones current, and the same question now cites the new version.
5. **5:30–6:15. Export and MCP.** Export a cited memo. Ask the same question from Claude through the MCP connector.
6. **6:15–7:00. Close on trust.** Eval page, limits page, admin cost per answer, and "what we do not claim."
7. **Fallbacks:** the pre-cached answers, the kill switch, and a recorded backup.

## 7. Buyer objections and honest answers

- **Coverage:** small by design (14 filers). The value is provenance, not breadth. Extension is a pipeline run, not a rebuild.
- **Hallucination:** every claim carries a cited ID, and 0 of the cited IDs were absent from the retrieved context. That is membership, not entailment. Faithfulness is 0.865–0.927 and correctness 100% (Sonnet judge) or 90% (Haiku judge) on n=20.
- **Freshness:** snapshot plus live detection. New filings are flagged within seconds but are not analyzed until ingested.
- **Security:** no certification. It has app-level isolation, an audit trail and a deletion endpoint. Enterprise needs a security review.
- **Cost:** per-answer ledger and daily ceiling; uploads are size-capped.
- **Scale:** see section 5.
- **On-prem or HA:** Docker Compose is feasible. HA, RBAC and online backup require Neo4j Enterprise. Its price is [U]; say so plainly.
- **Model choice:** LiteLLM makes the model pluggable, but the published numbers belong to Sonnet 5. Any swap, including Haiku, needs the paid re-benchmark before we claim parity. Do not present "open-weight only" as settled.

## 8. Exclusions

- **PyMuPDF** is AGPL [V]. Exclude it from anything sold.
- **Bloomberg and FactSet counts, Cloudflare per-user price beyond the free tier, and Neo4j Enterprise pricing** are [U].

**Sources:**
- alpha-sense.com/platform/, alpha-sense.com/security/, help.alpha-sense.com (article 7225228), developer.alpha-sense.com
- prnewswire.com (S&P release 2024-11-12), a-teaminsight.com, docs.kensho.com/agentskills
- hebbia.com/security, rogo.com, sayari.com, sayari.com/platform/api/
- resilinc.ai, interos.ai, everstream.ai
- modelcontextprotocol.io/specification/versioning, claude.com/docs/connectors/building/authentication
- pypi.org/pypi/<pkg>/json, sec.gov API and FAQ pages
- federalregister.gov/api/v1
- neo4j.com/docs (operations manual, GDS, cypher-manual SEARCH), clerk.com/pricing, supabase.com/pricing
- authjs.dev, api.github.com (keycloak, better-auth, docling), langfuse.com/pricing and license-key page
- developers.cloudflare.com (Access JWT), fly.io/compliance, genai.owasp.org LLM01
