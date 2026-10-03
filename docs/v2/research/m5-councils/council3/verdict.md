## Where the Council Agrees
- H1: same-origin from the API image at an unlinked `noindex` `/v2/`, `/legacy` kept, Cloudflare deferred to M6. All five chose it over H2 and H3.
- MCP and API keys: admin-issued keys, off in production until the owner enables them.
- Omit the "risks dropped" badge (2026-09-26 audit: mostly unreliable on Nvidia).
- Top risk: a security contract lost in the port.

## Where the Council Clashes
- Framework: F1 (Expansionist), F2 (First Principles, Outsider, Executor), F3 as ES modules (Contrarian). Several arguments, chiefly the Contrarian's, assumed a "strict" script CSP; addendum 1-2 shows today's already allows `'unsafe-inline'`, like Next's no-nonce policy, and FastAPI sets it either way. ES modules would also break the single-file pins.
- MCP/keys: protect as a channel (Expansionist) or await a named consumer (Outsider).

## Blind Spots the Council Caught
Reviewers caught: unmeasured port cost; Lighthouse missing keyboard traps; Turnstile tokens across remounts; a promoted build possibly differing from the tested one; no buyer-feedback, deep-link, workspace or rollback plan; keyed traffic without a global cap or revocation; no lockfile.

## The Recommendation
**(a) Hosting.** Now, H1: a Node build stage in the Dockerfile, `StaticFiles` mounted after all API routes, `/v2/` first; no new cost, hostname or vendor. M6: the domain (about $10-15/year, not re-verified), Access (ends open access, needs payment details) and any edge, decided on Workers Static Assets, not Pages. H2 rejected: new public hostname, 429s above 100k/day Worker-first requests, the 125 s proxy timeout, SSE buffering, and (assumption) per-IP caps collapsing behind the proxy.

**(b) Framework.** Same under every option: the CSP header (kept identical) and the three known gaps (fixed in place); the 149-test and pin port is roughly equal for F1 and F2 (cheap only if the single file is kept). What differs is F1's AI SDK half: a new byte-fixture-pinned `/api/chat` encoder, majors about twice yearly, the plan already a major behind (`ai@6`; `ai@7` current), and SDK rendering we replace with our own. Next versus Vite differs little and is unmeasured: Next's static-export footguns versus a Vite deep-link fallback in FastAPI (assumed). F3 cannot absorb the new views in a ~1,040-line inline file.
Recommend: drop the AI SDK and encoder; keep the SSE grammar behind a thin typed client. F2 (Vite + React) first, narrowly; Next static export with that client second, honouring the recorded framework; F1 as planned last. Cost: amending the M5b plan; no money.
Data-driven option: the Ask view (stream, chips, Turnstile, drawer) in F1 and F2, equal time boxes, scored on identical CSP header, zero console CSP violations, fresh Turnstile token under double mount, and trailing-slash routes 200. Proposed rule: keep F1 if it passes all four within 125% of F2's effort, encoder included; otherwise F2. It prices effort, not SDK churn.

**(c) Rollout.** Promote `/v2/` to `/` on written owner sign-off after:
1. Every old assertion and pin mapped to a new test or a written obsolete reason.
2. CSP header byte-identical.
3. Lighthouse ≥ 95, axe in CI, scripted keyboard tests (drawer, drop zone), one manual pass.
4. Turnstile fresh per ask and upload across remounts, fail-closed; CSP keeps `https://challenges.cloudflare.com` in `script-src` and `frame-src`.
5. The same artifact at both paths, or a full gate re-run on the promoted build.
6. Tested deep links, in-flight workspace continuity (shared origin and `sessionStorage`) and one-deploy rollback.
7. `/legacy` kept through the M6 cutover.
8. Named buyers' feedback on `/v2/` before the swap.
9. Reproducible Node build: lockfile, exact versions, pinned Node, CI job.

**(d) Exposure.** MCP and API keys: admin-issued, revocable, hashed, flag off in production. All keyed traffic shares one global admission cap, built on M5a's admission control, so it cannot starve paid asks; per-key quotas; short-TTL key caching, not a Neo4j read per request (assumption on cost); per-key logs, and the production flag as kill switch, since keys bypass Turnstile. Name a first consumer before building (none is named). Enabling needs explicit owner approval.

**(e) Follow-up.** Build behind a flag after M5a; enable on approval. Each follow-up costs one paid ask under both caps (per-IP and daily); the rewrite call is token-capped and logged. Browser-held prior turns are attacker-controlled: labelled untrusted, separate template, rewritten question re-validated as user input, never cached, injection-tested. The badge stays omitted.

**(f) M5c.** Build in order: PDF/Markdown export, the follow-up, the supply-chain map. Defer MCP/keys (until a consumer is named) and the admin console (auth unspecified). Drop nothing.

**(g) Order.** M5a first. In parallel: live-page gap fixes, the test classification, the spike if chosen, and the scaffold plus non-answer views at `/v2/`. Ask/Workspace streaming follows once M5a's async answer contract is frozen (inference). M5c after promotion; keyed traffic and the follow-up only once M5a admission control exists.

## The One Thing to Do First
Classify all 149 node tests and every pytest source pin as port, rewrite or obsolete. Every option needs it; it measures the port cost (addendum 4) and becomes gate 1.

## Decisions for the Owner
1. Framework: F2, Next with thin client, F1 as recorded, or spike first (two time boxes). Recommend F2; no money; blocks the M5b scaffold.
2. Hosting: H1 instead of "Cloudflare Pages" in M5; Workers decision in M6. Recommend yes; $0; blocks the Dockerfile stage.
3. Amend the plan with gates 1-9 and name the buyers who preview `/v2/`. Recommend yes; blocks promotion.
4. MCP/keys: name a consumer or defer to M6; enabling is separate. Recommend defer; blocks that build.
5. Follow-up behind a flag, one-ask accounting. Recommend yes; blocks that build.
6. Confirm the badge omission. Recommend yes; closes the original PLAN item.
7. Approve the (f) deferrals. Recommend yes; nothing is cut; blocks the M5c schedule.

Decided by the council, no owner approval needed:
- No H2; no new hostname.
- Fix the three known gaps on the live page now.
- Run the test classification; it ships nothing.
